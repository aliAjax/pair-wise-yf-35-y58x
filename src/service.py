from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine


ASSIGNMENT_WINDOW_HOURS = 24.0
NO_RESULT_STATUSES = ("scheduled", "collected", "sealed", "in_transit", "received")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        if kind == "case":
            sample = self._lookup("sample", "id", payload.get("sample_id"))
            if sample and sample[0]["data"].get("inspector_id"):
                payload["inspector_id"] = sample[0]["data"]["inspector_id"]
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
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
        if updated["kind"] == "inspector" and action == "update":
            self._cascade_inspector_update(updated, actor)
        return updated

    def release_assignment(self, actor, assignment_id):
        assignment = self.repository.get_entity(assignment_id)
        if not assignment:
            raise NotFoundError("assignment not found: " + assignment_id)
        if assignment["kind"] != "assignment":
            raise ValidationError("not an assignment: " + assignment_id)
        if assignment["status"] != "pending":
            raise ValidationError("assignment is not pending: " + assignment_id)
        self.rules._ensure_role(actor, ("dispatcher", "admin"))
        data = dict(assignment["data"])
        sample_payload = {
            "athlete_id": data["athlete_id"],
            "sample_code": "S-" + uuid4().hex[:12],
            "event": data.get("event", "out-of-competition"),
            "scheduled_at": data["scheduled_at"],
        }
        released, sample, reject_reason = self.repository.atomic_release(
            assignment_id,
            ASSIGNMENT_WINDOW_HOURS,
            actor.user_id,
            str(uuid4()),
            sample_payload,
        )
        if reject_reason:
            self.audit.record(assignment_id, actor, "release", "pending", "rejected", {"reason": reject_reason})
            return released
        self.audit.record(
            assignment_id,
            actor,
            "release",
            "pending",
            "released",
            {"sample_id": sample["id"], "license_status": "valid"},
        )
        return released

    def _cascade_inspector_update(self, inspector, actor):
        inspector_id = inspector["id"]
        for sample in self.repository.list_entities(kind="sample"):
            if sample["data"].get("inspector_id") != inspector_id:
                continue
            if sample["status"] not in NO_RESULT_STATUSES:
                continue
            merged = dict(sample["data"])
            merged["needs_redispatch"] = True
            merged["void_reason"] = "inspector record updated; sample had no result"
            updated = self.repository.update_entity(sample["id"], sample["version"], "void", merged)
            self.audit.record(
                sample["id"], actor, "void", sample["status"], "void",
                {"inspector_id": inspector_id, "reason": "inspector record updated; sample had no result"},
            )
        for case in self.repository.list_entities(kind="case"):
            if case["data"].get("inspector_id") != inspector_id:
                continue
            if case["data"].get("needs_reconfirm"):
                continue
            merged = dict(case["data"])
            merged["needs_reconfirm"] = True
            updated = self.repository.update_entity(case["id"], case["version"], case["status"], merged)
            self.audit.record(
                case["id"], actor, "reconfirm_required", case["status"], case["status"],
                {"inspector_id": inspector_id},
            )

    def backfill_license_status(self, actor):
        self.rules._ensure_role(actor, ("admin",))
        inspectors = {item["id"]: item for item in self.repository.list_entities(kind="inspector")}
        updated = []
        manual = []
        for sample in self.repository.list_entities(kind="sample"):
            data = dict(sample["data"])
            inspection_date = str(
                data.get("collected_at") or data.get("scheduled_at") or sample["created_at"]
            )[:10]
            inspector_id = data.get("inspector_id")
            license_status = None
            if inspector_id:
                inspector = inspectors.get(inspector_id)
                if inspector and inspector["data"].get("license_expiry"):
                    license_expiry = inspector["data"]["license_expiry"]
                    license_status = "valid" if str(license_expiry) >= inspection_date else "expired"
            if license_status is None:
                data["needs_manual_verification"] = True
                manual.append(sample["id"])
            else:
                data["license_status"] = license_status
                if license_status == "expired":
                    data["needs_manual_verification"] = True
                updated.append(sample["id"])
            self.repository.update_entity(sample["id"], sample["version"], sample["status"], data)
        return {"updated": updated, "manual_verification": manual}

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
