from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .rules import (
    RESULTED_SAMPLE_STATUSES,
    RuleEngine,
    assess_dispatch_eligibility,
    license_status_on,
    parse_date,
    parse_dt,
)


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    # ------------------------------------------------------------------ create

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        if kind == "assignment":
            entity = self._dispatch(actor, payload)
        elif kind == "sample":
            entity = self._create_sample(actor, payload)
        else:
            entity = self._create_generic(actor, kind, payload)
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity["id"])
        return entity

    def _create_generic(self, actor, kind, payload):
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        data = dict(payload)
        if kind == "inspector":
            data["revision"] = 1
        entity = self.repository.create_entity(entity_id, kind, status, data, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        return entity

    def _dispatch(self, actor, payload):
        # Fast checks give clear 4xx messages; the transaction repeats every
        # decision that another dispatcher could have invalidated meanwhile.
        self.rules.validate_create(actor, "assignment", payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        athlete_id = payload["athlete_id"]
        inspector_id = payload["inspector_id"]
        window_start = parse_dt(payload["window_start"])
        test_date = window_start.date().isoformat()
        slot_key = athlete_id + "|" + test_date
        data = dict(payload)
        data["test_date"] = test_date
        try:
            with self.repository.transaction() as connection:
                athlete = self.repository.get_entity(athlete_id, connection)
                inspector = self.repository.get_entity(inspector_id, connection)
                ok, reason = assess_dispatch_eligibility(
                    inspector, athlete, window_start.date()
                )
                if not ok:
                    raise ValidationError("dispatch rejected: " + reason)
                window_end = parse_dt(data["window_end"])
                for other in self.repository.find_entities(
                    "assignment", "inspector_id", inspector_id, connection
                ):
                    if other["status"] != "assigned":
                        continue
                    other_start = parse_dt(other["data"]["window_start"])
                    other_end = parse_dt(other["data"]["window_end"])
                    if window_start < other_end and other_start < window_end:
                        raise ConflictError(
                            "inspector already assigned in an overlapping window"
                        )
                self.repository.acquire_dispatch_slot(
                    connection, slot_key, entity_id,
                    athlete_id, test_date, inspector_id,
                )
                entity = self.repository.txn_insert_entity(
                    connection, entity_id, "assignment", "assigned",
                    data, actor.user_id,
                )
                self.repository.txn_append_audit(
                    connection, entity_id, actor.user_id, actor.role,
                    "create", None, "assigned",
                    {"kind": "assignment", "slot_key": slot_key},
                )
        except ConflictError as exc:
            # Same slot raced by another dispatcher: surface as a plain reject.
            raise ConflictError(str(exc))
        return entity

    def _create_sample(self, actor, payload):
        self.rules.validate_create(actor, "sample", payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        with self.repository.transaction() as connection:
            assignment = self.repository.get_entity(payload["assignment_id"], connection)
            if not assignment or assignment["status"] != "assigned":
                raise ConflictError("assignment is not open for sampling")
            if assignment["data"]["athlete_id"] != payload["athlete_id"]:
                raise ValidationError("sample athlete does not match the assignment")
            data = dict(payload)
            data["inspector_id"] = assignment["data"]["inspector_id"]
            data["scheduled_for"] = assignment["data"]["window_start"]
            data["test_date"] = assignment["data"].get("test_date")
            data["inspector_revision"] = assignment["data"].get(
                "inspector_revision", 1
            )
            sample = self.repository.txn_insert_entity(
                connection, entity_id, "sample", "scheduled",
                data, actor.user_id,
            )
            fulfilled = self.repository.txn_update_entity(
                connection, assignment["id"], "fulfilled",
                dict(assignment["data"], sample_id=entity_id),
            )
            self.repository.txn_append_audit(
                connection, entity_id, actor.user_id, actor.role,
                "create", None, "scheduled",
                {"kind": "sample", "assignment_id": assignment["id"]},
            )
            self.repository.txn_append_audit(
                connection, assignment["id"], actor.user_id, actor.role,
                "fulfill", "assigned", "fulfilled",
                {"sample_id": entity_id},
            )
        return sample

    # ------------------------------------------------------------- transitions

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "inspector" and action == "update_record":
            return self._revise_inspector(
                actor, entity, dict(data or {}), expected_version
            )
        if kind == "assignment" and action == "cancel":
            return self._cancel_assignment(
                actor, entity, dict(data or {}), expected_version
            )
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _cancel_assignment(self, actor, entity, data, expected_version):
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, "cancel", data, self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        with self.repository.transaction() as connection:
            updated = self.repository.txn_update_entity(
                connection, entity["id"], next_status, merged, expected
            )
            # Cancelling frees the slot so the athlete can be re-dispatched.
            self.repository.release_dispatch_slot(connection, entity["id"])
            self.repository.txn_append_audit(
                connection, entity["id"], actor.user_id, actor.role,
                "cancel", entity["status"], next_status, {"patch": patch},
            )
        return updated

    def _revise_inspector(self, actor, entity, data, expected_version):
        """
        A corrected inspector record fans out to every object relying on it:
          * still-open assignments are cancelled and their slots released;
          * samples without a lab result are voided and must be redone;
          * closed cases built on those samples must be reconfirmed;
          * cases still in progress are flagged so the panel sees the change.
        Everything moves in one write transaction.
        """
        expected = int(expected_version) if expected_version is not None else entity["version"]
        _, patch = self.rules.validate_transition(
            actor, entity, "update_record", data, self._lookup
        )
        new_data = dict(entity["data"])
        new_data.update(patch)
        new_revision = int(new_data.get("revision") or 1) + 1
        new_data["revision"] = new_revision
        cascade = {"assignments": [], "samples": [], "cases": []}
        with self.repository.transaction() as connection:
            inspector = self.repository.txn_update_entity(
                connection, entity["id"], "active", new_data, expected
            )
            for assignment in self.repository.find_entities(
                "assignment", "inspector_id", entity["id"], connection
            ):
                if assignment["status"] != "assigned":
                    continue
                self.repository.release_dispatch_slot(connection, assignment["id"])
                self.repository.txn_update_entity(
                    connection, assignment["id"], "cancelled",
                    dict(assignment["data"],
                         cancel_reason="inspector_record_revised",
                         inspector_revision=new_revision),
                )
                self.repository.txn_append_audit(
                    connection, assignment["id"], actor.user_id, actor.role,
                    "system_cancel", assignment["status"], "cancelled",
                    {"reason": "inspector_record_revised",
                     "inspector_revision": new_revision},
                )
                cascade["assignments"].append(assignment["id"])
            for sample in self.repository.find_entities(
                "sample", "inspector_id", entity["id"], connection
            ):
                if sample["status"] in RESULTED_SAMPLE_STATUSES:
                    continue
                self.repository.txn_update_entity(
                    connection, sample["id"], "voided",
                    dict(sample["data"],
                         void_reason="inspector_record_revised",
                         inspector_revision_voided=new_revision,
                         needs_redispatch=True),
                )
                if sample["data"].get("assignment_id"):
                    self.repository.release_dispatch_slot(
                        connection, sample["data"]["assignment_id"]
                    )
                self.repository.txn_append_audit(
                    connection, sample["id"], actor.user_id, actor.role,
                    "system_void", sample["status"], "voided",
                    {"reason": "inspector_record_revised",
                     "inspector_revision": new_revision},
                )
                cascade["samples"].append(sample["id"])
            for case in self.repository.find_entities(
                "case", "inspector_id", entity["id"], connection
            ):
                if case["status"] in ("closed", "appeal"):
                    self.repository.txn_update_entity(
                        connection, case["id"], "reconfirm",
                        dict(case["data"],
                             inspector_revision_unresolved=True,
                             inspector_revision_flagged=new_revision,
                             prior_status=case["status"]),
                    )
                    self.repository.txn_append_audit(
                        connection, case["id"], actor.user_id, actor.role,
                        "system_flag_reconfirm", case["status"], "reconfirm",
                        {"inspector_revision": new_revision},
                    )
                else:
                    self.repository.txn_patch_entity(
                        connection, case["id"],
                        {"inspector_revision_pending": new_revision},
                    )
                    self.repository.txn_append_audit(
                        connection, case["id"], actor.user_id, actor.role,
                        "system_flag", case["status"], case["status"],
                        {"inspector_revision": new_revision},
                    )
                cascade["cases"].append(case["id"])
            self.repository.txn_append_audit(
                connection, entity["id"], actor.user_id, actor.role,
                "update_record", "active", "active",
                {"patch": patch, "revision": new_revision, "cascade": cascade},
            )
        return inspector

    # --------------------------------------------------------- legacy backfill

    def backfill_license_status(self, actor):
        """
        Rebuild the license view for records created before license state was
        tracked. The state is evaluated against each record's own test date.
        Anything that cannot be determined from stored license dates goes to
        the manual review queue instead of being guessed.
        """
        if actor.role != "admin":
            raise PermissionDenied("only admin can run the legacy backfill")
        report = {
            "inspectors": {"verified": 0, "expired": 0, "unknown": 0},
            "samples": {"verified": 0, "expired": 0, "unknown": 0},
            "assignments": {"verified": 0, "expired": 0, "unknown": 0},
            "queued": 0,
        }

        def resolve_inspector(record, connection):
            inspector = None
            inspector_id = record.get("inspector_id")
            if inspector_id:
                inspector = self.repository.get_entity(inspector_id, connection)
            if inspector is None:
                license_no = record.get("inspector_license_no")
                if license_no:
                    matches = self.repository.find_entities(
                        "inspector", "license_no", license_no, connection
                    )
                    if len(matches) == 1:
                        inspector = matches[0]
            return inspector

        def stamp(kind, entity, inspector, test_date, patch, connection):
            state = license_status_on(inspector, test_date)
            patch["license_state"] = state
            patch["backfilled"] = True
            self.repository.txn_patch_entity(connection, entity["id"], patch)
            self.repository.txn_append_audit(
                connection, entity["id"], actor.user_id, actor.role,
                "legacy_backfill", entity["status"], entity["status"],
                {"kind": kind, "license_state": state,
                 "test_date": test_date.isoformat()},
            )
            report[kind][state] += 1
            if state == "unknown":
                reason = "license state cannot be determined for test date"
            elif state == "expired":
                reason = "license expired on test date"
            else:
                return
            self.repository.enqueue_review(
                entity["id"], kind, reason,
                {"test_date": test_date.isoformat(), "license_state": state},
                actor.user_id, connection,
            )
            report["queued"] += 1

        with self.repository.transaction() as connection:
            # Inspector records themselves: evaluate against today.
            for inspector in self.repository.list_entities(
                kind="inspector", connection=connection
            ):
                data = inspector["data"]
                if data.get("license_state") or data.get("license_state_at"):
                    continue
                state = license_status_on(inspector, parse_date(_today()))
                self.repository.txn_patch_entity(
                    connection, inspector["id"],
                    {"license_state": state,
                     "license_state_at": _today()},
                )
                report["inspectors"][state] += 1
                if state == "unknown":
                    self.repository.enqueue_review(
                        inspector["id"], "inspector",
                        "legacy record has no usable license dates",
                        {"license_state": state}, actor.user_id, connection,
                    )
                    report["queued"] += 1

            for sample in self.repository.list_entities(
                kind="sample", connection=connection
            ):
                data = sample["data"]
                if data.get("backfilled"):
                    continue
                test_raw = (
                    data.get("collected_at")
                    or data.get("scheduled_for")
                    or data.get("test_date")
                )
                if not test_raw:
                    self.repository.txn_patch_entity(
                        connection, sample["id"], {"backfilled": True}
                    )
                    self.repository.enqueue_review(
                        sample["id"], "sample",
                        "legacy sample has no test date for license backfill",
                        {}, actor.user_id, connection,
                    )
                    report["queued"] += 1
                    continue
                inspector = resolve_inspector(data, connection)
                if inspector is None:
                    self.repository.txn_patch_entity(
                        connection, sample["id"], {"backfilled": True}
                    )
                    self.repository.enqueue_review(
                        sample["id"], "sample",
                        "legacy sample cannot be linked to an inspector record",
                        {"test_date": str(test_raw)[:10]},
                        actor.user_id, connection,
                    )
                    report["queued"] += 1
                    continue
                test_date = parse_date(test_raw)
                patch = {"inspector_id": inspector["id"]}
                stamp("samples", sample, inspector, test_date, patch, connection)

            for assignment in self.repository.list_entities(
                kind="assignment", connection=connection
            ):
                data = assignment["data"]
                if data.get("backfilled"):
                    continue
                test_raw = data.get("window_start") or data.get("test_date")
                if not test_raw:
                    self.repository.txn_patch_entity(
                        connection, assignment["id"], {"backfilled": True}
                    )
                    self.repository.enqueue_review(
                        assignment["id"], "assignment",
                        "legacy assignment has no test date for license backfill",
                        {}, actor.user_id, connection,
                    )
                    report["queued"] += 1
                    continue
                inspector = resolve_inspector(data, connection)
                if inspector is None:
                    self.repository.txn_patch_entity(
                        connection, assignment["id"], {"backfilled": True}
                    )
                    self.repository.enqueue_review(
                        assignment["id"], "assignment",
                        "legacy assignment cannot be linked to an inspector record",
                        {"test_date": str(test_raw)[:10]},
                        actor.user_id, connection,
                    )
                    report["queued"] += 1
                    continue
                test_date = parse_date(test_raw)
                patch = {"inspector_id": inspector["id"]}
                stamp("assignments", assignment, inspector, test_date, patch, connection)

        return report

    # ----------------------------------------------------------------- review

    def list_review(self, status="open"):
        return self.repository.list_review(status=status)

    def resolve_review(self, actor, review_id, note=None):
        if actor.role not in ("admin", "panel"):
            raise PermissionDenied("only admin or panel can resolve review items")
        self.repository.resolve_review(review_id, actor.user_id)
        items = self.repository.list_review(status=None)
        for item in items:
            if item["id"] == int(review_id):
                self.repository.append_audit(
                    item["entity_id"], actor.user_id, actor.role,
                    "review_resolved", None, None,
                    {"review_id": item["id"], "note": note or ""},
                )
                return item
        raise NotFoundError("review item not found: " + str(review_id))

    # ----------------------------------------------------------------- reads

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
            return self.repository.list_entities(kind=kind, status=status)
        return self.repository.list_entities(status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)


def _today():
    from datetime import date

    return date.today().isoformat()
