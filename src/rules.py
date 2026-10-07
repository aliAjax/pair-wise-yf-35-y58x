from datetime import datetime

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

RESULTED_SAMPLE_STATUSES = ("analyzed", "adverse", "cleared", "voided")


def parse_date(value):
    """Return a date from an ISO date or datetime string."""
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).date()


def parse_dt(value):
    """Return an aware/naive datetime from an ISO date or datetime string."""
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    # date-only values describe the whole day for scheduling windows.
    if len(text) == 10:
        return parsed
    return parsed


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def license_status_on(inspector, on_date):
    """
    License state of an inspector record as seen on a given test date:
      verified -> license_from/license_to present and covering the date
      expired  -> dates present but the date is outside their coverage
      unknown  -> legacy record without usable license dates
    """
    data = (inspector or {}).get("data", {})
    if inspector and inspector.get("status") == "revoked":
        return "revoked"
    raw_from = data.get("license_from")
    raw_to = data.get("license_to")
    if not raw_from or not raw_to:
        return "unknown"
    try:
        valid_from = parse_date(raw_from)
        valid_to = parse_date(raw_to)
    except ValueError:
        return "unknown"
    if valid_from <= on_date <= valid_to:
        return "verified"
    return "expired"


def assess_dispatch_eligibility(inspector, athlete, on_date):
    """
    Gate shared by dispatch and by the post-update cascade. A dispatch is
    released only when the inspector license is valid on the test day and
    the inspector does not share a team with the tested athlete.
    Returns (ok, reason).
    """
    if inspector is None:
        return False, "inspector record not found"
    if inspector.get("status") != "active":
        return False, "inspector is not active"
    status = license_status_on(inspector, on_date)
    if status == "unknown":
        return False, "inspector license state is unknown on test date"
    if status in ("expired", "revoked"):
        return False, "inspector license is %s on test date" % status
    team = (athlete or {}).get("data", {}).get("team")
    inspector_team = inspector["data"].get("team")
    if team and inspector_team and team == inspector_team:
        return False, "inspector belongs to the same team as the athlete"
    return True, "ok"


def _validate_athlete(actor, data, lookup):
    if len(data.get("discipline", "")) < 2:
        raise ValidationError("discipline is too short")


def _validate_inspector(actor, data, lookup):
    if not str(data.get("license_no", "")).strip():
        raise ValidationError("license_no is required")
    if not str(data.get("name", "")).strip():
        raise ValidationError("name is required")
    raw_from = data.get("license_from")
    raw_to = data.get("license_to")
    if not raw_from or not raw_to:
        raise ValidationError("license_from and license_to are required")
    try:
        valid_from = parse_date(raw_from)
        valid_to = parse_date(raw_to)
    except ValueError:
        raise ValidationError("license dates must be ISO dates")
    if valid_to < valid_from:
        raise ValidationError("license_to precedes license_from")


def _validate_assignment(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete or athlete["status"] != "active":
        raise ValidationError("assignment requires an active athlete")
    inspector = _find_one(lookup, "inspector", "id", data.get("inspector_id"))
    if not inspector:
        raise ValidationError("assignment requires an inspector record")
    try:
        window_start = parse_dt(data.get("window_start"))
        window_end = parse_dt(data.get("window_end"))
    except (ValueError, TypeError):
        raise ValidationError("window_start and window_end must be ISO datetimes")
    if window_end <= window_start:
        raise ValidationError("window_end must be after window_start")
    ok, reason = assess_dispatch_eligibility(
        inspector, athlete, window_start.date()
    )
    if not ok:
        raise ValidationError("dispatch rejected: " + reason)
    # Fast pre-check; the dispatch transaction re-checks under a write lock so
    # concurrent dispatchers cannot both slip through.
    for other in lookup("assignment", "inspector_id", inspector["id"]) or []:
        if other["status"] != "assigned":
            continue
        try:
            other_start = parse_dt(other["data"]["window_start"])
            other_end = parse_dt(other["data"]["window_end"])
        except (KeyError, ValueError):
            continue
        if window_start < other_end and other_start < window_end:
            raise ConflictError(
                "inspector already assigned in an overlapping window"
            )


def _validate_sample(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete or athlete["status"] != "active":
        raise ValidationError("sample requires an active athlete")
    if not data.get("sample_code", "").strip():
        raise ValidationError("sample_code is required")
    assignment_id = data.get("assignment_id")
    if not assignment_id:
        raise ValidationError("sample must be created from a dispatch assignment")
    assignment = _find_one(lookup, "assignment", "id", assignment_id)
    if not assignment:
        raise ValidationError("assignment not found: " + str(assignment_id))
    if assignment["status"] != "assigned":
        raise ConflictError(
            "assignment is not open for sampling: " + assignment["status"]
        )
    if assignment["data"]["athlete_id"] != data["athlete_id"]:
        raise ValidationError("sample athlete does not match the assignment")
    inspector = _find_one(
        lookup, "inspector", "id", assignment["data"]["inspector_id"]
    )
    ok, reason = assess_dispatch_eligibility(
        inspector, athlete, parse_date(assignment["data"]["window_start"])
    )
    if not ok:
        raise ValidationError("dispatch no longer eligible: " + reason)


def _validate_case(actor, data, lookup):
    sample = _find_one(lookup, "sample", "id", data.get("sample_id"))
    if not sample or sample["status"] != "adverse":
        raise ValidationError("case requires an adverse sample")
    return {"inspector_id": sample["data"].get("inspector_id")}


def _validate_report_adverse(actor, entity, data, lookup):
    if entity["data"].get("result") != "adverse":
        raise ValidationError("only an adverse lab result can open a case")
    return {"confirmed_by": actor.user_id}


def _validate_case_decision(actor, entity, data, lookup):
    if data.get("decision") not in ("sanction", "no_sanction"):
        raise ValidationError("decision must be sanction or no_sanction")
    if entity["data"].get("inspector_revision_unresolved"):
        raise InvalidTransition(
            "inspector record was revised after this case was closed; "
            "reconfirm the case before deciding again"
        )
    return {"decided_by": actor.user_id}


def _validate_case_reconfirm(actor, entity, data, lookup):
    if data.get("decision") not in ("sanction", "no_sanction"):
        raise ValidationError("decision must be sanction or no_sanction")
    return {
        "reconfirmed_by": actor.user_id,
        "inspector_revision_unresolved": False,
    }


def _validate_inspector_update(actor, entity, data, lookup):
    fields = ("name", "team", "license_no", "license_from", "license_to")
    if not any(field in data for field in fields):
        raise ValidationError("no updatable inspector field provided")
    patch = {field: data[field] for field in fields if field in data}
    merged = dict(entity["data"])
    merged.update(patch)
    try:
        if parse_date(merged["license_to"]) < parse_date(merged["license_from"]):
            raise ValidationError("license_to precedes license_from")
    except KeyError:
        pass
    except ValueError:
        raise ValidationError("license dates must be ISO dates")
    return patch


CUSTOM_CREATE = {
    "athlete": _validate_athlete,
    "inspector": _validate_inspector,
    "assignment": _validate_assignment,
    "sample": _validate_sample,
    "case": _validate_case,
}
CUSTOM_TRANSITIONS = {
    ("sample", "report_adverse"): _validate_report_adverse,
    ("case", "decide"): _validate_case_decision,
    ("case", "resolve_appeal"): _validate_case_decision,
    ("case", "reconfirm"): _validate_case_reconfirm,
    ("inspector", "update_record"): _validate_inspector_update,
}


class RuleEngine:
    ALIASES = {
        "athletes": "athlete",
        "inspectors": "inspector",
        "assignments": "assignment",
        "samples": "sample",
        "cases": "case",
    }
    INITIAL_STATUS = {
        "athlete": "active",
        "inspector": "active",
        "assignment": "assigned",
        "sample": "scheduled",
        "case": "open",
    }
    TRANSITIONS = {
        "athlete": {
            "retire": (("active",), "retired"),
        },
        "inspector": {
            "update_record": (("active", "revoked"), "active"),
            "revoke": (("active",), "revoked"),
        },
        "assignment": {
            "fulfill": (("assigned",), "fulfilled"),
            "cancel": (("assigned",), "cancelled"),
        },
        "sample": {
            "collect": (("scheduled",), "collected"),
            "seal": (("collected",), "sealed"),
            "ship": (("sealed",), "in_transit"),
            "receive": (("in_transit",), "received"),
            "analyze": (("received",), "analyzed"),
            "report_adverse": (("analyzed",), "adverse"),
            "clear": (("analyzed",), "cleared"),
        },
        "case": {
            "provisional_suspend": (("open",), "suspended"),
            "schedule_hearing": (("suspended",), "hearing"),
            "decide": (("hearing",), "closed"),
            "appeal": (("closed",), "appeal"),
            "resolve_appeal": (("appeal",), "closed"),
            "reconfirm": (("reconfirm",), "closed"),
        },
    }
    CREATE_REQUIRED = {
        "athlete": ("name", "discipline"),
        "inspector": ("name", "license_no", "license_from", "license_to"),
        "assignment": ("athlete_id", "inspector_id", "window_start", "window_end"),
        "sample": ("athlete_id", "sample_code", "event", "assignment_id"),
        "case": ("athlete_id", "sample_id", "alleged_rule"),
    }
    ACTION_REQUIRED = {
        ("inspector", "update_record"): (),
        ("assignment", "cancel"): ("reason",),
        ("sample", "collect"): ("collected_at",),
        ("sample", "seal"): ("seal_id",),
        ("sample", "ship"): ("carrier",),
        ("sample", "receive"): ("lab_id",),
        ("sample", "analyze"): ("result",),
        ("sample", "clear"): ("reason",),
        ("case", "provisional_suspend"): ("reason",),
        ("case", "schedule_hearing"): ("hearing_at",),
        ("case", "decide"): ("decision",),
        ("case", "appeal"): ("grounds",),
        ("case", "resolve_appeal"): ("decision",),
        ("case", "reconfirm"): ("decision",),
    }
    CREATE_ROLES = {
        "athlete": ("admin", "panel"),
        "inspector": ("admin",),
        "assignment": ("admin", "dispatcher"),
        "sample": ("admin", "inspector", "dispatcher"),
        "case": ("admin", "panel"),
    }
    ROLE_ACTIONS = {
        "retire": ("admin", "panel"),
        "revoke": ("admin",),
        "update_record": ("admin",),
        "fulfill": ("admin", "inspector", "dispatcher"),
        "cancel": ("admin", "dispatcher"),
        "collect": ("admin", "inspector"),
        "seal": ("admin", "inspector"),
        "ship": ("admin", "inspector"),
        "receive": ("admin", "lab"),
        "analyze": ("admin", "lab"),
        "report_adverse": ("admin", "lab"),
        "clear": ("admin", "lab"),
        "provisional_suspend": ("admin", "panel"),
        "schedule_hearing": ("admin", "panel"),
        "decide": ("admin", "panel"),
        "appeal": ("admin", "panel"),
        "resolve_appeal": ("admin", "panel"),
        "reconfirm": ("admin", "panel"),
    }

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
        extra = custom(actor, data, lookup) if custom else None
        if extra:
            data.update(extra)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
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
