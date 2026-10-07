import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine, assess_dispatch_eligibility, license_status_on
from src.service import DomainService

ADMIN = Actor("admin", "admin")
DISPATCHER = Actor("disp-1", "dispatcher")
PANEL = Actor("panel-1", "panel")

WINDOW = ("2026-03-10T09:00:00", "2026-03-10T11:00:00")


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())

    def tearDown(self):
        self.tmp.cleanup()

    def _inspector(self, license_from="2025-01-01", license_to="2026-12-31",
                   team=None, actor=ADMIN, **extra):
        data = {
            "name": "Officer",
            "license_no": "LIC-" + license_to,
            "license_from": license_from,
            "license_to": license_to,
        }
        if team:
            data["team"] = team
        data.update(extra)
        return self.service.create(actor, "inspector", data)

    def _athlete(self, team=None, name="A. Rider"):
        data = {"name": name, "discipline": "cycling"}
        if team:
            data["team"] = team
        return self.service.create(ADMIN, "athlete", data)

    def _dispatch(self, athlete, inspector, window=WINDOW, actor=DISPATCHER,
                  start=None, end=None):
        start = start or window[0]
        end = end or window[1]
        return self.service.create(
            actor, "assignment",
            {
                "athlete_id": athlete["id"],
                "inspector_id": inspector["id"],
                "window_start": start,
                "window_end": end,
            },
        )

    # ------------------------------------------------------------ eligibility

    def test_dispatcher_role_can_assign(self):
        athlete = self._athlete()
        inspector = self._inspector()
        assignment = self._dispatch(athlete, inspector)
        self.assertEqual(assignment["status"], "assigned")
        self.assertEqual(assignment["data"]["test_date"], "2026-03-10")

    def test_viewer_role_cannot_assign(self):
        athlete = self._athlete()
        inspector = self._inspector()
        with self.assertRaises(PermissionDenied):
            self._dispatch(athlete, inspector, actor=Actor("v", "viewer"))

    def test_expired_license_on_test_day_rejected(self):
        athlete = self._athlete()
        inspector = self._inspector(license_from="2020-01-01", license_to="2025-12-31")
        with self.assertRaises(ValidationError) as caught:
            self._dispatch(athlete, inspector)
        self.assertIn("expired", str(caught.exception))

    def test_license_valid_boundary_day(self):
        inspector_entity = self._inspector(license_to="2026-03-10")
        athlete_entity = self._athlete()
        inspector = self.service.get(inspector_entity["id"])
        athlete = self.service.get(athlete_entity["id"])
        from datetime import date
        ok, _ = assess_dispatch_eligibility(inspector, athlete, date(2026, 3, 10))
        self.assertTrue(ok)

    def test_same_team_rejected(self):
        athlete = self._athlete(team="sky")
        inspector = self._inspector(team="sky")
        with self.assertRaises(ValidationError) as caught:
            self._dispatch(athlete, inspector)
        self.assertIn("same team", str(caught.exception))

    def test_overlapping_window_for_same_inspector_rejected(self):
        athlete_1 = self._athlete(name="One")
        athlete_2 = self._athlete(name="Two")
        inspector = self._inspector()
        self._dispatch(athlete_1, inspector)
        with self.assertRaises(ConflictError):
            self._dispatch(athlete_2, inspector,
                           start="2026-03-10T10:00:00",
                           end="2026-03-10T12:00:00")

    def test_non_overlapping_windows_allowed(self):
        athlete_1 = self._athlete(name="One")
        athlete_2 = self._athlete(name="Two")
        inspector = self._inspector()
        self._dispatch(athlete_1, inspector)
        second = self._dispatch(
            athlete_2, inspector,
            start="2026-03-10T11:00:00", end="2026-03-10T12:00:00",
        )
        self.assertEqual(second["status"], "assigned")

    def test_unknown_license_state_rejected(self):
        # Legacy inspector without license dates cannot be dispatched.
        inspector = self.repo.create_entity(
            "legacy-inspector", "inspector", "active",
            {"name": "Legacy", "license_no": "OLD-1"}, "migration",
        )
        athlete = self._athlete()
        with self.assertRaises(ValidationError):
            self._dispatch(athlete, inspector)

    # ------------------------------------------------------- concurrency guard

    def test_concurrent_dispatch_only_one_wins(self):
        athlete = self._athlete()
        inspector_a = self._inspector(license_no="LIC-A")
        inspector_b = self._inspector(license_no="LIC-B")
        errors = []
        barrier = threading.Barrier(2)

        def run(inspector):
            barrier.wait()
            try:
                self._dispatch(athlete, inspector)
            except ConflictError as exc:
                errors.append(str(exc))

        t1 = threading.Thread(target=run, args=(inspector_a,))
        t2 = threading.Thread(target=run, args=(inspector_b,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(len(errors), 1)
        assignments = self.service.list("assignment")
        self.assertEqual(len(assignments), 1)

    # ------------------------------------------------- dispatch / sample linkage

    def test_sample_fulfills_assignment_and_inherits_inspector(self):
        athlete = self._athlete()
        inspector = self._inspector()
        assignment = self._dispatch(athlete, inspector)
        sample = self.service.create(
            DISPATCHER, "sample",
            {"athlete_id": athlete["id"], "sample_code": "S-1",
             "event": "oop", "assignment_id": assignment["id"]},
        )
        self.assertEqual(sample["data"]["inspector_id"], inspector["id"])
        self.assertEqual(
            self.service.get(assignment["id"])["status"], "fulfilled"
        )
        with self.assertRaises(ConflictError):
            self.service.create(
                DISPATCHER, "sample",
                {"athlete_id": athlete["id"], "sample_code": "S-2",
                 "event": "oop", "assignment_id": assignment["id"]},
            )

    def test_sample_requires_assignment(self):
        athlete = self._athlete()
        with self.assertRaises(ValidationError):
            self.service.create(
                DISPATCHER, "sample",
                {"athlete_id": athlete["id"], "sample_code": "S-1",
                 "event": "oop"},
            )

    def test_cancel_assignment_releases_slot_for_redispatch(self):
        athlete = self._athlete()
        inspector = self._inspector()
        assignment = self._dispatch(athlete, inspector)
        self.service.transition(
            DISPATCHER, assignment["id"], "cancel", {"reason": "athlete ill"},
        )
        second = self._dispatch(athlete, inspector)
        self.assertEqual(second["status"], "assigned")

    # --------------------------------------------------------- inspector cascade

    def test_revision_voids_pending_samples_and_flags_closed_case(self):
        athlete = self._athlete(team="south")
        inspector = self._inspector(team="north")
        assignment = self._dispatch(athlete, inspector)
        sample = self.service.create(
            DISPATCHER, "sample",
            {"athlete_id": athlete["id"], "sample_code": "S-1",
             "event": "oop", "assignment_id": assignment["id"]},
        )
        self.service.transition(
            ADMIN, sample["id"], "collect", {"collected_at": WINDOW[0]}
        )
        # A second athlete already has an adverse result from this inspector.
        athlete_2 = self._athlete(name="Two", team="south")
        assignment_2 = self._dispatch(
            athlete_2, inspector,
            start="2026-03-11T09:00:00", end="2026-03-11T11:00:00",
        )
        sample_2 = self.service.create(
            DISPATCHER, "sample",
            {"athlete_id": athlete_2["id"], "sample_code": "S-2",
             "event": "oop", "assignment_id": assignment_2["id"]},
        )
        for action, payload in [
            ("collect", {"collected_at": "2026-03-11T09:30:00"}),
            ("seal", {"seal_id": "SEAL-2"}),
            ("ship", {"carrier": "C"}),
            ("receive", {"lab_id": "LAB"}),
            ("analyze", {"result": "adverse"}),
            ("report_adverse", {}),
        ]:
            sample_2 = self.service.transition(ADMIN, sample_2["id"], action, payload)
        case = self.service.create(
            PANEL, "case",
            {"athlete_id": athlete_2["id"], "sample_id": sample_2["id"],
             "alleged_rule": "AAS"},
        )
        self.service.transition(
            PANEL, case["id"], "provisional_suspend", {"reason": "r"}
        )
        self.service.transition(
            PANEL, case["id"], "schedule_hearing", {"hearing_at": "2026-05-01"}
        )
        self.service.transition(PANEL, case["id"], "decide", {"decision": "sanction"})

        revised = self.service.transition(
            ADMIN, inspector["id"], "update_record", {"team": "east"}
        )
        self.assertEqual(revised["data"]["revision"], 2)

        # Collected but no result yet -> void and open for redispatch.
        self.assertEqual(self.service.get(sample["id"])["status"], "voided")
        self.assertTrue(
            self.service.get(sample["id"])["data"]["needs_redispatch"]
        )
        # The old slot was released: the athlete can be re-dispatched.
        reassign = self._dispatch(athlete, revised)
        self.assertEqual(reassign["status"], "assigned")

        # Resulted sample stays intact but its closed case must be reconfirmed.
        self.assertEqual(self.service.get(sample_2["id"])["status"], "adverse")
        reconfirm_case = self.service.get(case["id"])
        self.assertEqual(reconfirm_case["status"], "reconfirm")
        self.assertTrue(
            reconfirm_case["data"]["inspector_revision_unresolved"]
        )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                PANEL, case["id"], "appeal", {"grounds": "g"}
            )
        closed = self.service.transition(
            PANEL, case["id"], "reconfirm", {"decision": "sanction"}
        )
        self.assertEqual(closed["status"], "closed")
        self.assertFalse(closed["data"]["inspector_revision_unresolved"])

    def test_revision_cancels_open_assignment(self):
        athlete = self._athlete()
        inspector = self._inspector()
        assignment = self._dispatch(athlete, inspector)
        self.service.transition(
            ADMIN, inspector["id"], "update_record", {"license_to": "2030-01-01"}
        )
        self.assertEqual(
            self.service.get(assignment["id"])["status"], "cancelled"
        )
        # Slot released.
        again = self._dispatch(
            athlete, self.service.get(inspector["id"]),
            start=WINDOW[0], end=WINDOW[1],
        )
        self.assertEqual(again["status"], "assigned")

    def test_only_admin_can_revise_inspector(self):
        inspector = self._inspector()
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                DISPATCHER, inspector["id"], "update_record", {"team": "x"}
            )

    def test_license_status_helper(self):
        from datetime import date
        inspector = {"status": "active", "data": {
            "license_from": "2026-01-01", "license_to": "2026-06-30"}}
        self.assertEqual(
            license_status_on(inspector, date(2026, 6, 30)), "verified"
        )
        self.assertEqual(
            license_status_on(inspector, date(2026, 7, 1)), "expired"
        )
        legacy = {"status": "active", "data": {"license_no": "X"}}
        self.assertEqual(
            license_status_on(legacy, date(2026, 1, 1)), "unknown"
        )

    # --------------------------------------------------------------- backfill

    def test_backfill_fills_license_state_and_queues_gaps(self):
        # Inspector with complete license history: samples resolve to states.
        good = self.repo.create_entity(
            "insp-good", "inspector", "active",
            {"name": "Good", "license_no": "G1",
             "license_from": "2025-01-01", "license_to": "2026-12-31"},
            "migration",
        )
        expired = self.repo.create_entity(
            "insp-exp", "inspector", "active",
            {"name": "Expired", "license_no": "E1",
             "license_from": "2020-01-01", "license_to": "2025-01-01"},
            "migration",
        )
        # Legacy inspector missing license dates entirely.
        unknown = self.repo.create_entity(
            "insp-unk", "inspector", "active",
            {"name": "Unknown", "license_no": "U1"},
            "migration",
        )
        athlete = self._athlete()
        self.repo.create_entity(
            "sample-good", "sample", "scheduled",
            {"athlete_id": athlete["id"], "sample_code": "OLD-1",
             "event": "oop", "collected_at": "2026-02-01T08:00:00",
             "inspector_id": good["id"]},
            "migration",
        )
        self.repo.create_entity(
            "sample-expired", "sample", "scheduled",
            {"athlete_id": athlete["id"], "sample_code": "OLD-2",
             "event": "oop", "collected_at": "2026-02-01T08:00:00",
             "inspector_id": expired["id"]},
            "migration",
        )
        self.repo.create_entity(
            "sample-unknown", "sample", "scheduled",
            {"athlete_id": athlete["id"], "sample_code": "OLD-3",
             "event": "oop", "collected_at": "2026-02-01T08:00:00",
             "inspector_id": unknown["id"]},
            "migration",
        )
        # Sample linked only by legacy license number gets matched.
        self.repo.create_entity(
            "sample-byname", "sample", "scheduled",
            {"athlete_id": athlete["id"], "sample_code": "OLD-4",
             "event": "oop", "collected_at": "2026-02-01T08:00:00",
             "inspector_license_no": "G1"},
            "migration",
        )
        # Truly un-linkable sample goes to manual review.
        self.repo.create_entity(
            "sample-orphan", "sample", "scheduled",
            {"athlete_id": athlete["id"], "sample_code": "OLD-5",
             "event": "oop", "collected_at": "2026-02-01T08:00:00",
             "inspector_license_no": "MISSING"},
            "migration",
        )

        report = self.service.backfill_license_status(ADMIN)
        self.assertGreaterEqual(report["samples"]["verified"], 2)
        self.assertEqual(report["samples"]["expired"], 1)
        self.assertEqual(report["samples"]["unknown"], 1)
        self.assertGreaterEqual(report["queued"], 2)  # unknown + orphan

        good_sample = self.service.get("sample-byname")
        self.assertEqual(good_sample["data"]["license_state"], "verified")
        self.assertEqual(good_sample["data"]["inspector_id"], good["id"])

        queue = self.service.list_review()
        reasons = " ".join(item["reason"] for item in queue)
        self.assertIn("license", reasons)
        # Idempotent: a second run changes nothing.
        second = self.service.backfill_license_status(ADMIN)
        self.assertEqual(second["samples"]["verified"], 0)
        self.assertEqual(len(self.service.list_review()), len(queue))

        # Admin resolves a queued item.
        item = [i for i in queue if i["entity_id"] == "sample-orphan"][0]
        resolved = self.service.resolve_review(ADMIN, item["id"], "checked paper copy")
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(
            len(self.service.list_review(status="open")), len(queue) - 1
        )

    def test_backfill_requires_admin(self):
        with self.assertRaises(PermissionDenied):
            self.service.backfill_license_status(DISPATCHER)


if __name__ == "__main__":
    unittest.main()
