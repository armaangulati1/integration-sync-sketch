"""The CRM-shaped SQLite target store, plus the sync bookkeeping tables.

Two groups of tables live here:

Domain (what a CRM actually holds)
    contacts     one row per person, keyed by email
    accounts     one row per company/account, keyed by name
    activities   the unified timeline: calendar events and emails land here as activities,
                 each linked to a contact and account, uniquely keyed by (source, natural_key)

Sync bookkeeping (how the engine stays correct)
    sync_records the idempotency ledger: the last content hash + version seen per
                 (source, natural_key). This is what makes a re-run a no-op.
    sync_state   the incremental cursor (high-water mark) per source.
    audit_log    an append-only trail of every create / update / conflict decision.
    dead_letter  poison and retry-exhausted records, with failure counts, for reprocessing.

The store deliberately manages transactions explicitly (autocommit off via manual BEGIN)
so the engine can make "write the activity + advance the idempotency ledger + append the
audit row" atomic, and roll the whole thing back if the downstream write fails.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    contact_id  INTEGER PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    full_name   TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id  INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    domain      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
    activity_id INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    natural_key TEXT NOT NULL,
    contact_id  INTEGER REFERENCES contacts(contact_id),
    account_id  INTEGER REFERENCES accounts(account_id),
    kind        TEXT NOT NULL,
    subject     TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (source, natural_key)
);

CREATE TABLE IF NOT EXISTS sync_records (
    source         TEXT NOT NULL,
    natural_key    TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    version        INTEGER NOT NULL DEFAULT 1,
    first_synced_at TEXT NOT NULL,
    last_synced_at  TEXT NOT NULL,
    PRIMARY KEY (source, natural_key)
);

CREATE TABLE IF NOT EXISTS sync_state (
    source     TEXT PRIMARY KEY,
    cursor     TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id    INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    source      TEXT NOT NULL,
    natural_key TEXT NOT NULL,
    action      TEXT NOT NULL,
    old_hash    TEXT,
    new_hash    TEXT,
    detail      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS dead_letter (
    dl_id         INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,
    natural_key   TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    error         TEXT NOT NULL,
    category      TEXT NOT NULL,
    failure_count INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    resolved      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source, natural_key)
);

CREATE INDEX IF NOT EXISTS idx_activities_contact ON activities(contact_id);
CREATE INDEX IF NOT EXISTS idx_audit_key ON audit_log(source, natural_key);
CREATE INDEX IF NOT EXISTS idx_dl_unresolved ON dead_letter(resolved);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class CrmStore:
    """A thin, explicit SQLite wrapper for the CRM target and sync bookkeeping."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        # isolation_level=None -> autocommit off is managed by us via BEGIN/COMMIT.
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._in_tx = False

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- transaction control -------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """A real, rollback-on-exception transaction around a unit of work."""
        self.conn.execute("BEGIN")
        self._in_tx = True
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        finally:
            self._in_tx = False

    # -- domain upserts ------------------------------------------------------

    def upsert_contact(self, email: str, full_name: str) -> int:
        now = _now()
        cur = self.conn.execute("SELECT contact_id FROM contacts WHERE email = ?", (email,))
        row = cur.fetchone()
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO contacts (email, full_name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (email, full_name, now, now),
            )
            return int(cur.lastrowid)
        contact_id = int(row["contact_id"])
        if full_name:
            self.conn.execute(
                "UPDATE contacts SET full_name = ?, updated_at = ? WHERE contact_id = ?",
                (full_name, now, contact_id),
            )
        return contact_id

    def upsert_account(self, name: str, domain: str) -> int:
        now = _now()
        cur = self.conn.execute("SELECT account_id FROM accounts WHERE name = ?", (name,))
        row = cur.fetchone()
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO accounts (name, domain, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (name, domain, now, now),
            )
            return int(cur.lastrowid)
        return int(row["account_id"])

    def upsert_activity(
        self,
        *,
        source: str,
        natural_key: str,
        contact_id: int,
        account_id: int,
        kind: str,
        subject: str,
        body: str,
        occurred_at: str,
    ) -> int:
        now = _now()
        cur = self.conn.execute(
            "SELECT activity_id FROM activities WHERE source = ? AND natural_key = ?",
            (source, natural_key),
        )
        row = cur.fetchone()
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO activities "
                "(source, natural_key, contact_id, account_id, kind, subject, body, "
                " occurred_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source, natural_key, contact_id, account_id, kind, subject, body,
                 occurred_at, now),
            )
            return int(cur.lastrowid)
        activity_id = int(row["activity_id"])
        self.conn.execute(
            "UPDATE activities SET contact_id = ?, account_id = ?, kind = ?, subject = ?, "
            "body = ?, occurred_at = ?, updated_at = ? WHERE activity_id = ?",
            (contact_id, account_id, kind, subject, body, occurred_at, now, activity_id),
        )
        return activity_id

    # -- idempotency ledger --------------------------------------------------

    def get_sync_record(self, source: str, natural_key: str) -> sqlite3.Row | None:
        cur = self.conn.execute(
            "SELECT * FROM sync_records WHERE source = ? AND natural_key = ?",
            (source, natural_key),
        )
        return cur.fetchone()

    def put_sync_record(
        self, source: str, natural_key: str, content_hash: str, version: int
    ) -> None:
        now = _now()
        existing = self.get_sync_record(source, natural_key)
        if existing is None:
            self.conn.execute(
                "INSERT INTO sync_records "
                "(source, natural_key, content_hash, version, first_synced_at, last_synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (source, natural_key, content_hash, version, now, now),
            )
        else:
            self.conn.execute(
                "UPDATE sync_records SET content_hash = ?, version = ?, last_synced_at = ? "
                "WHERE source = ? AND natural_key = ?",
                (content_hash, version, now, source, natural_key),
            )

    # -- incremental cursor --------------------------------------------------

    def get_cursor(self, source: str) -> str | None:
        cur = self.conn.execute("SELECT cursor FROM sync_state WHERE source = ?", (source,))
        row = cur.fetchone()
        return None if row is None else row["cursor"]

    def set_cursor(self, source: str, cursor: str | None) -> None:
        now = _now()
        self.conn.execute(
            "INSERT INTO sync_state (source, cursor, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(source) DO UPDATE SET cursor = excluded.cursor, "
            "updated_at = excluded.updated_at",
            (source, cursor, now),
        )

    # -- audit trail ---------------------------------------------------------

    def add_audit(
        self,
        *,
        source: str,
        natural_key: str,
        action: str,
        old_hash: str | None,
        new_hash: str | None,
        detail: str = "",
    ) -> None:
        self.conn.execute(
            "INSERT INTO audit_log (at, source, natural_key, action, old_hash, new_hash, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_now(), source, natural_key, action, old_hash, new_hash, detail),
        )

    def has_audit(
        self, *, source: str, natural_key: str, action: str, new_hash: str | None
    ) -> bool:
        """True if this exact audit decision was already recorded.

        Used to keep the conflict path idempotent: a stale record re-seen on a later full
        scan must not append a duplicate 'conflict_ignored' row.
        """
        cur = self.conn.execute(
            "SELECT 1 FROM audit_log WHERE source = ? AND natural_key = ? AND action = ? "
            "AND IFNULL(new_hash, '') = IFNULL(?, '') LIMIT 1",
            (source, natural_key, action, new_hash),
        )
        return cur.fetchone() is not None

    def audit_for(self, source: str, natural_key: str) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM audit_log WHERE source = ? AND natural_key = ? ORDER BY audit_id",
            (source, natural_key),
        )
        return cur.fetchall()

    # -- dead letter queue ---------------------------------------------------

    def add_dead_letter(
        self,
        *,
        source: str,
        natural_key: str,
        payload_json: str,
        error: str,
        category: str,
    ) -> None:
        """Insert or bump a dead-letter row, keyed by (source, natural_key).

        A repeat failure of the same record increments its failure count rather than
        creating a duplicate, and re-opens it (resolved -> 0) if it had been resolved.
        """
        now = _now()
        cur = self.conn.execute(
            "SELECT dl_id, failure_count FROM dead_letter WHERE source = ? AND natural_key = ?",
            (source, natural_key),
        )
        row = cur.fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO dead_letter "
                "(source, natural_key, payload_json, error, category, failure_count, "
                " first_seen_at, last_seen_at, resolved) VALUES (?, ?, ?, ?, ?, 1, ?, ?, 0)",
                (source, natural_key, payload_json, error, category, now, now),
            )
        else:
            self.conn.execute(
                "UPDATE dead_letter SET payload_json = ?, error = ?, category = ?, "
                "failure_count = failure_count + 1, last_seen_at = ?, resolved = 0 "
                "WHERE dl_id = ?",
                (payload_json, error, category, now, int(row["dl_id"])),
            )

    def list_dead_letter(self, *, include_resolved: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM dead_letter"
        if not include_resolved:
            sql += " WHERE resolved = 0"
        sql += " ORDER BY dl_id"
        return self.conn.execute(sql).fetchall()

    def resolve_dead_letter(self, dl_id: int) -> None:
        self.conn.execute(
            "UPDATE dead_letter SET resolved = 1, last_seen_at = ? WHERE dl_id = ?",
            (_now(), dl_id),
        )

    # -- read helpers (used by demos and tests) ------------------------------

    def count(self, table: str) -> int:
        allowed = {
            "contacts", "accounts", "activities", "sync_records",
            "audit_log", "dead_letter", "sync_state",
        }
        if table not in allowed:
            raise ValueError(f"unknown table {table!r}")
        return int(self.conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])

    def get_activity(self, source: str, natural_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM activities WHERE source = ? AND natural_key = ?",
            (source, natural_key),
        ).fetchone()

    def all_activities(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM activities ORDER BY occurred_at, source, natural_key"
        ).fetchall()

    def snapshot(self) -> dict[str, Any]:
        """A compact dict of table counts, handy for asserting 'nothing changed'."""
        return {
            t: self.count(t)
            for t in ("contacts", "accounts", "activities", "sync_records",
                      "audit_log", "dead_letter")
        }
