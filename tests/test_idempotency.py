"""Idempotency: running a sync twice on unchanged data changes nothing.

Proven two ways: a full re-scan (cursors reset) reports every record 'unchanged' and leaves
the table snapshot identical, and duplicate records within a single batch collapse to one.
"""

from __future__ import annotations

from integration_sync.models import SOURCE_CALENDAR
from integration_sync.sync_engine import SyncEngine

from .conftest import make_raw, mem_reader


def test_full_rescan_is_a_noop(store):
    records = [
        make_raw("evt-1@sketch", occurred_at="2026-03-02T09:00:00+00:00"),
        make_raw("evt-2@sketch", occurred_at="2026-03-03T09:00:00+00:00"),
        make_raw("evt-3@sketch", occurred_at="2026-03-04T09:00:00+00:00"),
    ]
    engine = SyncEngine(store)
    reader = mem_reader(records)

    first = engine.sync_source(SOURCE_CALENDAR, reader)
    assert first.created == 3
    snap1 = store.snapshot()

    # Reset the cursor so the reader hands the engine every record again.
    store.set_cursor(SOURCE_CALENDAR, None)
    second = engine.sync_source(SOURCE_CALENDAR, reader)

    assert second.created == 0
    assert second.updated == 0
    assert second.unchanged == 3
    assert store.snapshot() == snap1  # nothing written on the second pass


def test_duplicates_within_a_batch_collapse(store):
    same = make_raw("evt-dup@sketch", occurred_at="2026-03-02T09:00:00+00:00")
    records = [same, same, same]  # same record delivered three times
    engine = SyncEngine(store)

    stats = engine.sync_source(SOURCE_CALENDAR, mem_reader(records))

    assert stats.created == 1
    assert stats.unchanged == 2
    assert store.count("activities") == 1
    assert store.count("sync_records") == 1


def test_snapshot_stable_across_many_runs(store):
    records = [make_raw(f"evt-{i}@sketch", occurred_at=f"2026-03-0{i}T09:00:00+00:00")
               for i in range(1, 6)]
    engine = SyncEngine(store)
    reader = mem_reader(records)

    engine.sync_source(SOURCE_CALENDAR, reader)
    snap = store.snapshot()
    for _ in range(4):
        store.set_cursor(SOURCE_CALENDAR, None)
        engine.sync_source(SOURCE_CALENDAR, reader)
    assert store.snapshot() == snap
