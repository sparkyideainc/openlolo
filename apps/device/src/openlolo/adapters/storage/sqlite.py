"""Durable outcomes, keyed request fingerprints; never payloads or typed text."""

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from pathlib import Path
from uuid import UUID

from openlolo.domain.errors import OpenLoloError

# Upper bound on one operations page as sent to the box page; rows past the budget are left
# for the next page.
PAGE_BYTES = 24 * 1024
PAGE_LIMIT = 200


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL,
                fingerprint TEXT NOT NULL, state TEXT NOT NULL, result TEXT,
                created REAL NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS profiles (device TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS logins (digest TEXT PRIMARY KEY, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (digest TEXT PRIMARY KEY, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS oauth_clients (
                client_id TEXT PRIMARY KEY, value TEXT NOT NULL, approved INTEGER NOT NULL,
                created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS oauth_grants (
                digest TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL,
                family TEXT NOT NULL, value TEXT NOT NULL, expires REAL NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS owner_labels (owner TEXT PRIMARY KEY, label TEXT NOT NULL);
        """)
        self._migrate()
        # Changes on every write, survives a restart (seeded from the clock) and never repeats
        # on timestamp collisions; the box page watches it in the status digest to refetch history.
        self.revision = int(time.time() * 1000)
        self.db.execute("INSERT OR IGNORE INTO secrets VALUES ('fingerprint', ?)", (secrets.token_hex(32),))
        self.secret = bytes.fromhex(
            self.db.execute("SELECT value FROM secrets WHERE name='fingerprint'").fetchone()[0]
        )
        self.db.commit()

    def _migrate(self):
        """Idempotent, additive schema changes for databases created by older releases."""
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(oauth_clients)")}
        if "issuer" not in columns:
            self.db.execute("ALTER TABLE oauth_clients ADD COLUMN issuer TEXT")
        self.db.execute("CREATE INDEX IF NOT EXISTS operations_created ON operations(created DESC, id DESC)")
        # The Bluetooth companion-app channel is gone: its per-phone keys and label-code secret with it.
        self.db.execute("DROP TABLE IF EXISTS app_keys")
        self.db.execute("DELETE FROM secrets WHERE name IN ('setup_code', 'setup')")
        self.db.commit()

    def _bump(self) -> None:
        self.revision += 1

    def journal_revision(self) -> int:
        return self.revision

    def recover_interrupted(self):
        self.db.execute("UPDATE operations SET state='OUTCOME_UNKNOWN' WHERE state='DISPATCHED'")
        self.db.execute("UPDATE operations SET state='CANCELLED' WHERE state='QUEUED'")
        self.db.commit()
        self._bump()

    def existing(self, operation_id, owner, kind, payload):
        row = self.db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        if row is None:
            return None
        fingerprint = hmac.new(
            self.secret, json.dumps([kind, payload], sort_keys=True).encode(), hashlib.sha256
        ).hexdigest()
        if row["owner"] != owner or not hmac.compare_digest(row["fingerprint"], fingerprint):
            raise OpenLoloError("OPERATION_CONFLICT")
        return self._public(row)

    def reserve(self, operation_id, owner, kind, payload):
        try:
            UUID(operation_id)
        except (ValueError, TypeError):
            raise OpenLoloError("INVALID_OPERATION_ID", "Use a UUID operation ID", 400) from None
        fingerprint = hmac.new(
            self.secret, json.dumps([kind, payload], sort_keys=True).encode(), hashlib.sha256
        ).hexdigest()
        previous = self.db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        if previous:
            if previous["owner"] != owner or not hmac.compare_digest(previous["fingerprint"], fingerprint):
                raise OpenLoloError("OPERATION_CONFLICT")
            return self._public(previous), False
        now = time.time()
        self.db.execute(
            "INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)",
            (operation_id, owner, kind, fingerprint, "QUEUED", None, now, now),
        )
        self.db.commit()
        self._bump()
        return self.get(operation_id, owner), True

    @staticmethod
    def _public(row):
        return {
            k: (json.loads(row[k]) if row[k] else None) if k == "result" else row[k]
            for k in ("id", "kind", "state", "result", "created", "updated")
        }

    def get(self, operation_id, owner):
        row = self.db.execute(
            "SELECT * FROM operations WHERE id=? AND owner=?", (operation_id, owner)
        ).fetchone()
        if row is None:
            raise OpenLoloError("OPERATION_NOT_FOUND", status=404)
        return self._public(row)

    def list_operations(self, limit=50, before=None):
        """Newest first, across owners, each labelled with the client that owns the session."""
        limit = max(1, min(int(limit), 200))
        query = (
            "SELECT o.*, COALESCE(l.label, 'unknown') AS client FROM operations o "
            "LEFT JOIN owner_labels l ON l.owner = o.owner"
        )
        params: tuple = ()
        if before is not None:
            query += " WHERE o.created < ?"
            params = (float(before),)
        query += " ORDER BY o.created DESC, o.id DESC LIMIT ?"
        rows = self.db.execute(query, (*params, limit)).fetchall()
        return [{**self._public(row), "client": row["client"]} for row in rows]

    @staticmethod
    def _summary(row) -> dict:
        """One journal row as the box page lists it: no result payload, just an error code when it failed."""
        summary = {k: row[k] for k in ("id", "kind", "state", "created", "updated")}
        summary["client"] = row["client"]
        result = json.loads(row["result"]) if row["result"] else None
        if isinstance(result, dict) and isinstance(result.get("code"), str):
            summary["error_code"] = result["code"]
        return summary

    @staticmethod
    def cursor_after(row: dict) -> str:
        return f"{row['created']!r}:{row['id']}"

    @staticmethod
    def _parse_cursor(cursor: str) -> tuple[float, str]:
        created, _, operation_id = cursor.partition(":")
        try:
            return float(created), operation_id
        except ValueError:
            raise OpenLoloError("INVALID_REQUEST", "cursor is not a journal cursor", 400) from None

    def page_operations(self, limit=20, cursor=None, budget=PAGE_BYTES) -> dict:
        """Newest first, stable under identical timestamps: the cursor is ``(created, id)`` of the
        last row served, and ``has_more`` is set when rows remain or the byte budget stopped the
        page early. Rows are summaries; ``get`` serves the full result of one operation."""
        limit = max(1, min(int(limit), PAGE_LIMIT))
        query = (
            "SELECT o.*, COALESCE(l.label, 'unknown') AS client FROM operations o "
            "LEFT JOIN owner_labels l ON l.owner = o.owner"
        )
        params: tuple = ()
        if cursor:
            created, operation_id = self._parse_cursor(cursor)
            query += " WHERE o.created < ? OR (o.created = ? AND o.id < ?)"
            params = (created, created, operation_id)
        query += " ORDER BY o.created DESC, o.id DESC LIMIT ?"
        rows = self.db.execute(query, (*params, limit + 1)).fetchall()
        has_more = len(rows) > limit
        page: list[dict] = []
        size = 0
        for row in rows[:limit]:
            summary = self._summary(row)
            size += len(json.dumps(summary, separators=(",", ":")))
            if page and size > budget:
                has_more = True
                break
            page.append(summary)
        return {
            "operations": page,
            "next_cursor": self.cursor_after(page[-1]) if has_more and page else None,
            "has_more": has_more,
        }

    def label_owner(self, owner, label):
        self.db.execute("INSERT OR REPLACE INTO owner_labels VALUES (?,?)", (owner, label))
        self.db.commit()
        self._bump()

    def update(self, operation_id, state, result=None):
        self.db.execute(
            "UPDATE operations SET state=?,result=?,updated=? WHERE id=?",
            (state, json.dumps(result) if result is not None else None, time.time(), operation_id),
        )
        self.db.commit()
        self._bump()
        return self._public(
            self.db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        )

    def profile(self, device):
        row = self.db.execute("SELECT value FROM profiles WHERE device=?", (device,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_profile(self, device, value):
        self.db.execute("INSERT OR REPLACE INTO profiles VALUES (?,?)", (device, json.dumps(value)))
        self.db.commit()

    def close(self):
        self.db.close()
