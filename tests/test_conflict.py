"""Conflict resolution: last-write-wins by event time, with a full audit trail.

The engine only treats a repeat key as a conflict when the content hash differs. The winner
is the later occurred_at; ties break deterministically by hash. Every decision is audited,
and the conflict path is itself idempotent.
"""

from __future__ import annotations

from integration_sync.models import SOURCE_CALENDAR
from integration_sync.sync_engine import SyncEngine

from .conftest import make_raw, mem_reader


def test_newer_content_wins_and_updates(store):
    engine = SyncEngine(store)
    v1 = make_raw("evt-x@sketch", occurred_at="2026-03-02T09:00:00+00:00", subject="v1")
    v2 = make_raw("evt-x@sketch", occurred_at="2026-03-02T12:00:00+00:00", subject="v2 newer")

    engine.sync_source(SOURCE_CALENDAR, mem_reader([v1]))
    # Reset cursor so the edited (newer) version is re-read.
    store.set_cursor(SOURCE_CALENDAR, None)
    stats = engine.sync_source(SOURCE_CALENDAR, mem_reader([v2]))

    assert stats.updated == 1
    activity = store.get_activity(SOURCE_CALENDAR, "evt-x@sketch")
    assert activity["subject"] == "v2 newer"

    actions = [a["action"] for a in store.audit_for(SOURCE_CALENDAR, "evt-x@sketch")]
    assert actions == ["created", "updated"]

    ledger = store.get_sync_record(SOURCE_CALENDAR, "evt-x@sketch")
    assert ledger["version"] == 2


def test_older_content_is_ignored_with_audit(store):
    engine = SyncEngine(store)
    newer = make_raw("evt-y@sketch", occurred_at="2026-03-02T12:00:00+00:00", subject="newer")
    older = make_raw("evt-y@sketch", occurred_at="2026-03-02T09:00:00+00:00", subject="older")

    # Deliver newer first, then the stale older copy in the same batch (out of order).
    stats = engine.sync_source(SOURCE_CALENDAR, mem_reader([newer, older]))

    assert stats.created == 1
    assert stats.conflict_ignored == 1
    activity = store.get_activity(SOURCE_CALENDAR, "evt-y@sketch")
    assert activity["subject"] == "newer"  # stale write did not overwrite

    actions = [a["action"] for a in store.audit_for(SOURCE_CALENDAR, "evt-y@sketch")]
    assert actions == ["created", "conflict_ignored"]


def test_conflict_path_is_idempotent(store):
    engine = SyncEngine(store)
    newer = make_raw("evt-z@sketch", occurred_at="2026-03-02T12:00:00+00:00", subject="newer")
    older = make_raw("evt-z@sketch", occurred_at="2026-03-02T09:00:00+00:00", subject="older")

    engine.sync_source(SOURCE_CALENDAR, mem_reader([newer, older]))
    snap = store.snapshot()

    # Re-run the exact same out-of-order batch: the stale record must not re-audit.
    store.set_cursor(SOURCE_CALENDAR, None)
    engine.sync_source(SOURCE_CALENDAR, mem_reader([newer, older]))
    assert store.snapshot() == snap


def test_equal_timestamp_ties_break_deterministically(store):
    engine = SyncEngine(store)
    # Same key, same timestamp, different content -> deterministic winner by hash.
    a = make_raw("evt-tie@sketch", occurred_at="2026-03-02T09:00:00+00:00", subject="alpha")
    b = make_raw("evt-tie@sketch", occurred_at="2026-03-02T09:00:00+00:00", subject="bravo")

    engine.sync_source(SOURCE_CALENDAR, mem_reader([a, b]))
    first_winner = store.get_activity(SOURCE_CALENDAR, "evt-tie@sketch")["subject"]

    # Present in the opposite order in a fresh store; the winner must be the same.
    other = type(store)(":memory:")
    other.init_schema()
    SyncEngine(other).sync_source(SOURCE_CALENDAR, mem_reader([b, a]))
    second_winner = other.get_activity(SOURCE_CALENDAR, "evt-tie@sketch")["subject"]
    other.close()

    assert first_winner == second_winner
