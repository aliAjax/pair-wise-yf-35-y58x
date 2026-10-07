import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class InspectorAssignmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.dispatcher = Actor("dispatcher", "dispatcher")
        self.lab = Actor("lab", "lab")

    def tearDown(self):
        self.tmp.cleanup()

    def _inspector(self, name="I. One", license_expiry="2027-12-31", team="team-a"):
        return self.service.create(
            self.admin, "inspector",
            {"name": name, "license_no": "LIC-" + name, "license_expiry": license_expiry, "team": team},
        )

    def _athlete(self, name="A. Rider", team="team-b"):
        return self.service.create(
            self.admin, "athlete",
            {"name": name, "discipline": "cycling", "team": team},
        )

    def _assignment(self, inspector, athlete, scheduled_at="2026-03-01T10:00:00Z"):
        return self.service.create(
            self.dispatcher, "assignment",
            {"inspector_id": inspector["id"], "athlete_id": athlete["id"], "scheduled_at": scheduled_at},
        )

    def test_inspector_requires_fields(self):
        with self.assertRaises(ValidationError):
            self.service.create(self.admin, "inspector", {"name": "I. One"})
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin, "inspector",
                {"name": "I. One", "license_no": "L", "license_expiry": "not-a-date", "team": "t"},
            )

    def test_dispatcher_releases_assignment_and_creates_sample(self):
        inspector = self._inspector()
        athlete = self._athlete()
        assignment = self._assignment(inspector, athlete)
        self.assertEqual(assignment["status"], "pending")
        released = self.service.release_assignment(self.dispatcher, assignment["id"])
        self.assertEqual(released["status"], "released")
        samples = self.service.list("sample")
        self.assertEqual(len(samples), 1)
        sample = samples[0]
        self.assertEqual(sample["data"]["inspector_id"], inspector["id"])
        self.assertEqual(sample["data"]["assignment_id"], assignment["id"])
        self.assertEqual(sample["data"]["license_status"], "valid")
        self.assertEqual(sample["status"], "scheduled")

    def test_non_dispatcher_cannot_release(self):
        inspector = self._inspector()
        athlete = self._athlete()
        assignment = self._assignment(inspector, athlete)
        with self.assertRaises(PermissionDenied):
            self.service.release_assignment(Actor("viewer", "viewer"), assignment["id"])

    def test_release_rejects_expired_license_on_inspection_day(self):
        inspector = self._inspector(license_expiry="2026-01-01")
        athlete = self._athlete()
        assignment = self._assignment(inspector, athlete, scheduled_at="2026-03-01T10:00:00Z")
        result = self.service.release_assignment(self.dispatcher, assignment["id"])
        self.assertEqual(result["status"], "rejected")
        self.assertIn("expired", result["data"]["rejection_reason"])
        self.assertEqual(self.service.list("sample"), [])

    def test_release_rejects_same_team_as_athlete(self):
        inspector = self._inspector(team="team-a")
        athlete = self._athlete(team="team-a")
        assignment = self._assignment(inspector, athlete)
        result = self.service.release_assignment(self.dispatcher, assignment["id"])
        self.assertEqual(result["status"], "rejected")
        self.assertIn("same team", result["data"]["rejection_reason"])

    def test_release_rejects_double_booking_same_inspector_same_window(self):
        inspector = self._inspector()
        athlete = self._athlete()
        first = self._assignment(inspector, athlete, scheduled_at="2026-03-01T10:00:00Z")
        self.assertEqual(self.service.release_assignment(self.dispatcher, first["id"])["status"], "released")
        second = self._assignment(inspector, athlete, scheduled_at="2026-03-01T11:00:00Z")
        result = self.service.release_assignment(self.dispatcher, second["id"])
        self.assertEqual(result["status"], "rejected")
        self.assertIn("already booked", result["data"]["rejection_reason"])

    def test_release_allows_same_inspector_far_apart(self):
        inspector = self._inspector()
        athlete = self._athlete()
        first = self._assignment(inspector, athlete, scheduled_at="2026-03-01T10:00:00Z")
        self.assertEqual(self.service.release_assignment(self.dispatcher, first["id"])["status"], "released")
        second = self._assignment(inspector, athlete, scheduled_at="2026-04-01T10:00:00Z")
        self.assertEqual(self.service.release_assignment(self.dispatcher, second["id"])["status"], "released")

    def test_concurrent_releases_only_one_succeeds(self):
        inspector = self._inspector()
        athlete = self._athlete()
        a1 = self._assignment(inspector, athlete, scheduled_at="2026-03-01T10:00:00Z")
        a2 = self._assignment(inspector, athlete, scheduled_at="2026-03-01T10:30:00Z")
        barrier = threading.Barrier(2)
        results = []

        def release(assignment_id):
            barrier.wait()
            results.append(self.service.release_assignment(self.dispatcher, assignment_id)["status"])

        t1 = threading.Thread(target=release, args=(a1["id"],))
        t2 = threading.Thread(target=release, args=(a2["id"],))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(sorted(results), ["rejected", "released"])

    def test_inspector_update_voids_samples_without_results(self):
        inspector = self._inspector()
        athlete = self._athlete()
        assignment = self._assignment(inspector, athlete)
        self.service.release_assignment(self.dispatcher, assignment["id"])
        sample = self.service.list("sample")[0]
        self.assertEqual(sample["status"], "scheduled")
        self.service.transition(self.admin, inspector["id"], "update", {"license_expiry": "2028-06-30"})
        sample = self.service.get(sample["id"])
        self.assertEqual(sample["status"], "void")
        self.assertTrue(sample["data"]["needs_redispatch"])

    def test_inspector_update_flags_cases_for_reconfirm(self):
        inspector = self._inspector()
        athlete = self._athlete()
        assignment = self._assignment(inspector, athlete, scheduled_at="2026-05-01T10:00:00Z")
        self.service.release_assignment(self.dispatcher, assignment["id"])
        sample = [s for s in self.service.list("sample") if s["data"]["assignment_id"] == assignment["id"]][0]
        self.service.transition(self.admin, sample["id"], "collect", {"collected_at": "2026-05-01T12:00:00Z"})
        self.service.transition(self.admin, sample["id"], "seal", {"seal_id": "SEAL-1"})
        self.service.transition(self.admin, sample["id"], "ship", {"carrier": "C"})
        self.service.transition(self.lab, sample["id"], "receive", {"lab_id": "LAB-1"})
        self.service.transition(self.lab, sample["id"], "analyze", {"result": "adverse"})
        self.service.transition(self.lab, sample["id"], "report_adverse", {})
        case = self.service.create(
            self.admin, "case",
            {"athlete_id": athlete["id"], "sample_id": sample["id"], "alleged_rule": "sub-1"},
        )
        self.assertEqual(case["data"]["inspector_id"], inspector["id"])
        self.assertIsNone(case["data"].get("needs_reconfirm"))
        self.service.transition(self.admin, inspector["id"], "update", {"team": "team-z"})
        case = self.service.get(case["id"])
        self.assertTrue(case["data"]["needs_reconfirm"])
        reconfirmed = self.service.transition(self.admin, case["id"], "reconfirm", {})
        self.assertFalse(reconfirmed["data"]["needs_reconfirm"])
        self.assertEqual(reconfirmed["data"]["reconfirmed_by"], "admin")

    def test_backfill_flags_old_samples_without_inspector(self):
        athlete = self._athlete()
        old = self.service.create(
            self.admin, "sample",
            {"athlete_id": athlete["id"], "sample_code": "OLD-1", "event": "competition"},
        )
        result = self.service.backfill_license_status(self.admin)
        self.assertIn(old["id"], result["manual_verification"])
        old = self.service.get(old["id"])
        self.assertTrue(old["data"]["needs_manual_verification"])

    def test_backfill_sets_license_status_for_inspector_samples(self):
        inspector = self._inspector(license_expiry="2027-12-31")
        athlete = self._athlete()
        assignment = self._assignment(inspector, athlete, scheduled_at="2026-03-01T10:00:00Z")
        self.service.release_assignment(self.dispatcher, assignment["id"])
        sample = [s for s in self.service.list("sample") if s["data"]["assignment_id"] == assignment["id"]][0]
        result = self.service.backfill_license_status(self.admin)
        self.assertIn(sample["id"], result["updated"])
        sample = self.service.get(sample["id"])
        self.assertEqual(sample["data"]["license_status"], "valid")

    def test_backfill_flags_expired_license_samples_for_manual_review(self):
        inspector = self._inspector(license_expiry="2026-01-01")
        athlete = self._athlete()
        sample = self.service.create(
            self.admin, "sample",
            {"athlete_id": athlete["id"], "sample_code": "S-EXP", "event": "out-of-competition",
             "inspector_id": inspector["id"], "scheduled_at": "2026-03-01T10:00:00Z"},
        )
        self.service.backfill_license_status(self.admin)
        sample = self.service.get(sample["id"])
        self.assertEqual(sample["data"]["license_status"], "expired")
        self.assertTrue(sample["data"]["needs_manual_verification"])


if __name__ == "__main__":
    unittest.main()
