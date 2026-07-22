"""Incremental sync: the cursor advances and later runs do only the new work."""

from __future__ import annotations

from integration_sync.models import SOURCE_CALENDAR
from integration_sync.sync_engine import SyncEngine

from .conftest import make_raw, mem_reader


def test_cursor_advances_to_newest_processed(store):
    records = [
        make_raw("evt-1@sketch", occurred_at="2026-03-02T09:00:00+00:00"),
        make_raw("evt-2@sketch", occurred_at="2026-03-05T09:00:00+00:00"),
        make_raw("evt-3@sketch", occurred_at="2026-03-03T09:00:00+00:00"),
    ]
    engine = SyncEngine(store)
    stats = engine.sync_source(SOURCE_CALENDAR, mem_reader(records))

    assert stats.cursor_before is None
    assert stats.cursor_after == "2026-03-05T09:00:00+00:00"  # the newest, not the last
    assert store.get_cursor(SOURCE_CALENDAR) == "2026-03-05T09:00:00+00:00"


def test_second_run_only_processes_newer_records(store):
    engine = SyncEngine(store)
    initial = [
        make_raw("evt-1@sketch", occurred_at="2026-03-02T09:00:00+00:00"),
        make_raw("evt-2@sketch", occurred_at="2026-03-03T09:00:00+00:00"),
    ]
    first = engine.sync_source(SOURCE_CALENDAR, mem_reader(initial))
    assert first.created == 2

    # A newer record appears in the source; the old ones are unchanged.
    grown = initial + [make_raw("evt-3@sketch", occurred_at="2026-03-06T09:00:00+00:00")]
    second = engine.sync_source(SOURCE_CALENDAR, mem_reader(grown))

    # Only the new record was even read (cursor filtered the two old ones).
    assert second.created == 1
    assert second.unchanged == 0
    assert second.processed == 1
    assert store.count("activities") == 3
    assert store.get_cursor(SOURCE_CALENDAR) == "2026-03-06T09:00:00+00:00"


def test_no_new_records_is_zero_work(store):
    engine = SyncEngine(store)
    records = [make_raw("evt-1@sketch", occurred_at="2026-03-02T09:00:00+00:00")]
    engine.sync_source(SOURCE_CALENDAR, mem_reader(records))
    again = engine.sync_source(SOURCE_CALENDAR, mem_reader(records))
    assert again.processed == 0
    assert again.dead_lettered == 0


def test_advance_cursor_false_leaves_cursor_untouched(store):
    engine = SyncEngine(store)
    records = [make_raw("evt-1@sketch", occurred_at="2026-03-02T09:00:00+00:00")]
    engine.sync_source(SOURCE_CALENDAR, mem_reader(records), advance_cursor=False)
    assert store.get_cursor(SOURCE_CALENDAR) is None
