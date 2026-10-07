import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


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

    @contextmanager
    def transaction(self):
        """
        A single write transaction. BEGIN IMMEDIATE takes the database write
        lock up-front, so competing dispatchers are serialised instead of
        both passing the eligibility checks.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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
                    to_status TEXT,
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
                CREATE TABLE IF NOT EXISTS dispatch_slots (
                    slot_key TEXT PRIMARY KEY,
                    assignment_id TEXT NOT NULL,
                    athlete_id TEXT NOT NULL,
                    test_date TEXT NOT NULL,
                    inspector_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_slots_inspector
                    ON dispatch_slots(inspector_id, test_date);
                CREATE TABLE IF NOT EXISTS review_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_by TEXT,
                    resolved_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_review_status
                    ON review_queue(status, id);
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

    def get_entity(self, entity_id, connection=None):
        sql = "SELECT * FROM entities WHERE id = ?"
        params = (entity_id,)
        if connection is not None:
            row = connection.execute(sql, params).fetchone()
        else:
            with self._connect() as own:
                row = own.execute(sql, params).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None, connection=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = "SELECT * FROM entities" + where + " ORDER BY created_at, id"
        if connection is not None:
            rows = connection.execute(sql, params).fetchall()
        else:
            with self._connect() as own:
                rows = own.execute(sql, params).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value, connection=None):
        return [
            entity
            for entity in self.list_entities(kind=kind, connection=connection)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def txn_insert_entity(self, connection, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection.execute(
            "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
            (entity_id, kind, status, payload, actor_id, now, now),
        )
        return self.get_entity(entity_id, connection)

    def txn_update_entity(self, connection, entity_id, next_status, data,
                         expected_version=None):
        """Update inside an open transaction, honouring optimistic version."""
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
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
            (next_status, payload, now, entity_id, current_version),
        )
        return self.get_entity(entity_id, connection)

    def txn_patch_entity(self, connection, entity_id, patch, status=None):
        """
        Overwrite data (and optionally status) without a version bump. Used by
        the legacy backfill, which annotates historical rows rather than
        advancing their state machine.
        """
        now = utcnow()
        entity = self.get_entity(entity_id, connection)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        data = dict(entity["data"])
        data.update(patch)
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        if status:
            connection.execute(
                "UPDATE entities SET data = ?, status = ?, updated_at = ? WHERE id = ?",
                (payload, status, now, entity_id),
            )
        else:
            connection.execute(
                "UPDATE entities SET data = ?, updated_at = ? WHERE id = ?",
                (payload, now, entity_id),
            )
        return self.get_entity(entity_id, connection)

    def update_entity(self, entity_id, expected_version, status, data):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self.txn_update_entity(
                connection, entity_id, status, data, expected_version
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def txn_append_audit(self, connection, entity_id, actor_id, actor_role,
                         action, from_status, to_status, detail):
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

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            self.txn_append_audit(
                connection, entity_id, actor_id, actor_role, action,
                from_status, to_status, detail,
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [self._audit_from_row(row) for row in rows]

    @staticmethod
    def _audit_from_row(row):
        return {
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

    def acquire_dispatch_slot(self, connection, slot_key, assignment_id,
                              athlete_id, test_date, inspector_id):
        """
        The single-winner guard. Two dispatchers for the same athlete on the
        same test day race on this PRIMARY KEY; only one INSERT succeeds.
        """
        try:
            connection.execute(
                "INSERT INTO dispatch_slots(slot_key, assignment_id, athlete_id, test_date, inspector_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (slot_key, assignment_id, athlete_id, test_date, inspector_id, utcnow()),
            )
        except sqlite3.IntegrityError:
            raise ConflictError(
                "athlete already has a dispatch for test date " + test_date
            )

    def release_dispatch_slot(self, connection, assignment_id):
        connection.execute(
            "DELETE FROM dispatch_slots WHERE assignment_id = ?", (assignment_id,)
        )

    def get_dispatch_slot(self, connection, slot_key):
        row = connection.execute(
            "SELECT * FROM dispatch_slots WHERE slot_key = ?", (slot_key,)
        ).fetchone()
        return dict(row) if row else None

    def enqueue_review(self, entity_id, kind, reason, detail, created_by,
                       connection=None):
        payload = json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)
        sql = (
            "INSERT INTO review_queue(entity_id, kind, reason, detail, status, created_by, created_at) "
            "VALUES (?, ?, ?, ?, 'open', ?, ?)"
        )
        params = (entity_id, kind, reason, payload, created_by, utcnow())
        if connection is not None:
            cur = connection.execute(sql, params)
            return cur.lastrowid
        with self._connect() as own:
            cur = own.execute(sql, params)
            return cur.lastrowid

    def resolve_review(self, review_id, resolved_by):
        now = utcnow()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM review_queue WHERE id = ?", (review_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("review item not found: " + str(review_id))
            connection.execute(
                "UPDATE review_queue SET status = 'resolved', resolved_by = ?, resolved_at = ? "
                "WHERE id = ?",
                (resolved_by, now, review_id),
            )

    @staticmethod
    def _review_from_row(row):
        return {
            "id": row["id"],
            "entity_id": row["entity_id"],
            "kind": row["kind"],
            "reason": row["reason"],
            "detail": json.loads(row["detail"]),
            "status": row["status"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "resolved_by": row["resolved_by"],
            "resolved_at": row["resolved_at"],
        }

    def list_review(self, status="open"):
        with self._connect() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM review_queue WHERE status = ? ORDER BY id",
                    (status,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM review_queue ORDER BY id"
                ).fetchall()
        return [self._review_from_row(row) for row in rows]

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
