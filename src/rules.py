from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_date(value, field):
    try:
        datetime.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        raise ValidationError("%s must be a valid YYYY-MM-DD date" % field)


def _validate_inspector(actor, data, lookup):
    for field in ("name", "license_no", "license_expiry", "team"):
        if not data.get(field):
            raise ValidationError("missing required field: " + field)
    _validate_date(data["license_expiry"], "license_expiry")


def _validate_inspector_update(actor, entity, data, lookup):
    allowed = ("name", "license_no", "license_expiry", "team")
    patch = {key: data[key] for key in allowed if key in data and data[key] is not None}
    if "license_expiry" in patch:
        _validate_date(patch["license_expiry"], "license_expiry")
    if not patch:
        raise ValidationError("inspector update requires at least one field")
    return patch


def _validate_assignment(actor, data, lookup):
    inspector = _find_one(lookup, "inspector", "id", data.get("inspector_id"))
    if not inspector or inspector["status"] != "active":
        raise ValidationError("assignment requires an active inspector")
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete or athlete["status"] != "active":
        raise ValidationError("assignment requires an active athlete")
    scheduled_at = data.get("scheduled_at")
    if not scheduled_at:
        raise ValidationError("scheduled_at is required")
    try:
        datetime.fromisoformat(str(scheduled_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise ValidationError("scheduled_at must be a valid ISO datetime")


def _validate_athlete(actor, data, lookup):
    if len(data.get("discipline", "")) < 2:
        raise ValidationError("discipline is too short")


def _validate_sample(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete or athlete["status"] != "active":
        raise ValidationError("sample requires an active athlete")
    if not data.get("sample_code", "").strip():
        raise ValidationError("sample_code is required")


def _validate_case(actor, data, lookup):
    sample = _find_one(lookup, "sample", "id", data.get("sample_id"))
    if not sample or sample["status"] != "adverse":
        raise ValidationError("case requires an adverse sample")


def _validate_report_adverse(actor, entity, data, lookup):
    if entity["data"].get("result") != "adverse":
        raise ValidationError("only an adverse lab result can open a case")
    return {"confirmed_by": actor.user_id}


def _validate_case_decision(actor, entity, data, lookup):
    if data.get("decision") not in ("sanction", "no_sanction"):
        raise ValidationError("decision must be sanction or no_sanction")
    return {"decided_by": actor.user_id}


CUSTOM_CREATE = {'athlete': _validate_athlete, 'sample': _validate_sample, 'case': _validate_case, 'inspector': _validate_inspector, 'assignment': _validate_assignment}
CUSTOM_TRANSITIONS = {('sample', 'report_adverse'): _validate_report_adverse, ('case', 'decide'): _validate_case_decision, ('case', 'resolve_appeal'): _validate_case_decision, ('inspector', 'update'): _validate_inspector_update}


class RuleEngine:
    ALIASES = {'athletes': 'athlete', 'samples': 'sample', 'cases': 'case', 'inspectors': 'inspector', 'assignments': 'assignment'}
    INITIAL_STATUS = {'athlete': 'active', 'sample': 'scheduled', 'case': 'open', 'inspector': 'active', 'assignment': 'pending'}
    TRANSITIONS = {'athlete': {'retire': (('active',), 'retired')}, 'sample': {'collect': (('scheduled',), 'collected'), 'seal': (('collected',), 'sealed'), 'ship': (('sealed',), 'in_transit'), 'receive': (('in_transit',), 'received'), 'analyze': (('received',), 'analyzed'), 'report_adverse': (('analyzed',), 'adverse'), 'clear': (('analyzed',), 'cleared')}, 'case': {'provisional_suspend': (('open',), 'suspended'), 'schedule_hearing': (('suspended',), 'hearing'), 'decide': (('hearing',), 'closed'), 'appeal': (('closed',), 'appeal'), 'resolve_appeal': (('appeal',), 'closed')}, 'inspector': {'update': (('active',), 'active')}}
    CREATE_REQUIRED = {'athlete': ('name', 'discipline'), 'sample': ('athlete_id', 'sample_code', 'event'), 'case': ('athlete_id', 'sample_id', 'alleged_rule'), 'inspector': ('name', 'license_no', 'license_expiry', 'team'), 'assignment': ('inspector_id', 'athlete_id', 'scheduled_at')}
    ACTION_REQUIRED = {('sample', 'collect'): ('collected_at',), ('sample', 'seal'): ('seal_id',), ('sample', 'ship'): ('carrier',), ('sample', 'receive'): ('lab_id',), ('sample', 'analyze'): ('result',), ('sample', 'clear'): ('reason',), ('case', 'provisional_suspend'): ('reason',), ('case', 'schedule_hearing'): ('hearing_at',), ('case', 'decide'): ('decision',), ('case', 'appeal'): ('grounds',), ('case', 'resolve_appeal'): ('decision',)}
    CREATE_ROLES = {'athlete': ('admin', 'panel'), 'sample': ('admin', 'inspector'), 'case': ('admin', 'panel'), 'inspector': ('admin',), 'assignment': ('dispatcher', 'admin')}
    ROLE_ACTIONS = {'retire': ('admin', 'panel'), 'collect': ('admin', 'inspector'), 'seal': ('admin', 'inspector'), 'ship': ('admin', 'inspector'), 'receive': ('admin', 'lab'), 'analyze': ('admin', 'lab'), 'report_adverse': ('admin', 'lab'), 'clear': ('admin', 'lab'), 'provisional_suspend': ('admin', 'panel'), 'schedule_hearing': ('admin', 'panel'), 'decide': ('admin', 'panel'), 'appeal': ('admin', 'panel'), 'resolve_appeal': ('admin', 'panel'), 'reconfirm': ('admin', 'panel'), 'update': ('admin',)}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        if kind == "case" and action == "reconfirm":
            self._ensure_role(actor, ("admin", "panel"))
            return entity["status"], {"needs_reconfirm": False, "reconfirmed_by": actor.user_id}
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
