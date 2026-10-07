import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError, ValidationError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    @staticmethod
    def _hours_between(a, b):
        first = datetime.fromisoformat(str(a).replace("Z", "+00:00"))
        second = datetime.fromisoformat(str(b).replace("Z", "+00:00"))
        return abs((first - second).total_seconds()) / 3600.0

    def atomic_release(self, assignment_id, window_hours, actor_id, sample_id, sample_payload):
        """Atomically check eligibility and release or reject an assignment.

        The license, team-conflict and double-booking checks all run inside one
        BEGIN IMMEDIATE transaction, so concurrent dispatchers cannot both
        release assignments for the same inspector within the same time window,
        and an inspector record cannot change between the check and the release.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (assignment_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("assignment not found: " + assignment_id)
            assignment = self._entity_from_row(row)
            if assignment["kind"] != "assignment":
                raise ValidationError("not an assignment: " + assignment_id)
            if assignment["status"] != "pending":
                raise ValidationError("assignment is not pending: " + assignment_id)
            data = assignment["data"]
            inspector_id = data.get("inspector_id")
            athlete_id = data.get("athlete_id")
            scheduled_at = data.get("scheduled_at")
            inspector_row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (inspector_id,)
            ).fetchone()
            athlete_row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (athlete_id,)
            ).fetchone()
            inspector = self._entity_from_row(inspector_row) if inspector_row else None
            athlete = self._entity_from_row(athlete_row) if athlete_row else None
            reason = None
            if not inspector or inspector["status"] != "active":
                reason = "inspector not found or not active"
            elif not athlete or athlete["status"] != "active":
                reason = "athlete not found or not active"
            else:
                license_expiry = inspector["data"].get("license_expiry")
                if not license_expiry or str(license_expiry) < str(scheduled_at)[:10]:
                    reason = "inspector license expired on the inspection day"
                elif inspector["data"].get("team") and inspector["data"].get("team") == athlete["data"].get("team"):
                    reason = "inspector is on the same team as the athlete"
            if reason is None:
                others = connection.execute(
                    "SELECT * FROM entities WHERE kind = 'assignment' AND status = 'released' AND id != ?",
                    (assignment_id,),
                ).fetchall()
                for other_row in others:
                    other = self._entity_from_row(other_row)
                    if other["data"].get("inspector_id") != inspector_id:
                        continue
                    other_scheduled = other["data"].get("scheduled_at")
                    if other_scheduled and self._hours_between(other_scheduled, scheduled_at) < window_hours:
                        reason = "inspector already booked within the time window"
                        break
            now = utcnow()
            if reason:
                rejected = dict(data)
                rejected["rejection_reason"] = reason
                rejected["rejected_at"] = now
                connection.execute(
                    "UPDATE entities SET status = 'rejected', version = version + 1, data = ?, updated_at = ? "
                    "WHERE id = ? AND version = ?",
                    (json.dumps(rejected, ensure_ascii=False, sort_keys=True), now, assignment_id, assignment["version"]),
                )
                connection.commit()
                return self.get_entity(assignment_id), None, reason
            released = dict(data)
            released["released_at"] = now
            connection.execute(
                "UPDATE entities SET status = 'released', version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (json.dumps(released, ensure_ascii=False, sort_keys=True), now, assignment_id, assignment["version"]),
            )
            sample_data = dict(sample_payload)
            sample_data["inspector_id"] = inspector_id
            sample_data["assignment_id"] = assignment_id
            sample_data["license_status"] = "valid"
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, 'sample', 'scheduled', 1, ?, ?, ?, ?)",
                (sample_id, json.dumps(sample_data, ensure_ascii=False, sort_keys=True), actor_id, now, now),
            )
            connection.commit()
            return self.get_entity(assignment_id), self.get_entity(sample_id), None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
