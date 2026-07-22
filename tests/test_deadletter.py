"""Dead-lettering: poison and retry-exhausted records are captured, never lost, and can be
reprocessed once the underlying problem is fixed."""

from __future__ import annotations

from integration_sync.errors import TransientError
from integration_sync.models import SOURCE_CALENDAR
from integration_sync.pipeline import dead_letter_rebuilders
from integration_sync.sync_engine import SyncEngine

from .conftest import make_raw, mem_reader


def test_poison_records_go_to_dead_letter(store):
    engine = SyncEngine(store)
    records = [
        make_raw("evt-ok@sketch", occurred_at="2026-03-02T09:00:00+00:00"),
        make_raw("evt-bad-date@sketch", occurred_at="not-a-date"),          # unparseable
        make_raw("evt-bad-email@sketch", occurred_at="2026-03-02T09:00:00+00:00",
                 email="not-an-email"),                                     # invalid contact
        make_raw(None, occurred_at="2026-03-02T09:00:00+00:00"),            # missing key
    ]
    stats = engine.sync_source(SOURCE_CALENDAR, mem_reader(records))

    assert stats.created == 1
    assert stats.dead_lettered == 3
    dl = store.list_dead_letter()
    categories = sorted(row["category"] for row in dl)
    assert categories == ["poison", "poison", "poison_no_key"]


def test_transient_exhaustion_dead_letters(store):
    def always_fail(_record):
        raise TransientError("permanently flaky downstream")

    engine = SyncEngine(store, attempts=3, base_delay=0.0, sleep=lambda _d: None,
                        fault=always_fail)
    stats = engine.sync_source(
        SOURCE_CALENDAR,
        mem_reader([make_raw("evt-p@sketch", occurred_at="2026-03-02T09:00:00+00:00")]),
    )
    assert stats.dead_lettered == 1
    assert store.count("activities") == 0  # nothing was ever committed
    row = store.list_dead_letter()[0]
    assert row["category"] == "transient_exhausted"


def test_repeat_failure_bumps_count_not_rows(store):
    def always_fail(_record):
        raise TransientError("flaky")

    engine = SyncEngine(store, attempts=2, base_delay=0.0, sleep=lambda _d: None,
                        fault=always_fail)
    rec = [make_raw("evt-again@sketch", occurred_at="2026-03-02T09:00:00+00:00")]

    engine.sync_source(SOURCE_CALENDAR, mem_reader(rec))
    store.set_cursor(SOURCE_CALENDAR, None)
    engine.sync_source(SOURCE_CALENDAR, mem_reader(rec))

    dl = store.list_dead_letter()
    assert len(dl) == 1               # one row, not two
    assert dl[0]["failure_count"] == 2


def test_reprocess_resolves_fixed_records_only(store):
    # A downstream that fails on the first sync but is healthy at reprocess time.
    healthy = {"ok": False}

    def fault(_record):
        if not healthy["ok"]:
            raise TransientError("flaky at first")

    engine = SyncEngine(store, attempts=2, base_delay=0.0, sleep=lambda _d: None, fault=fault)
    records = [
        make_raw("evt-recoverable@sketch", occurred_at="2026-03-02T09:00:00+00:00"),
        make_raw("evt-poison@sketch", occurred_at="bad-date"),  # stays poison forever
    ]
    engine.sync_source(SOURCE_CALENDAR, mem_reader(records))
    assert len(store.list_dead_letter()) == 2

    healthy["ok"] = True  # fix the transient dependency
    resolved = engine.reprocess_dead_letter(dead_letter_rebuilders())

    assert resolved == 1                       # only the transient one recovers
    remaining = store.list_dead_letter()
    assert len(remaining) == 1
    assert remaining[0]["natural_key"] == "evt-poison@sketch"
    assert store.count("activities") == 1      # the recovered record is now applied


def test_no_key_records_are_not_auto_recovered(store):
    engine = SyncEngine(store)
    engine.sync_source(
        SOURCE_CALENDAR,
        mem_reader([make_raw(None, occurred_at="2026-03-02T09:00:00+00:00")]),
    )
    dl = store.list_dead_letter()
    assert len(dl) == 1
    assert dl[0]["category"] == "poison_no_key"

    # Even though the payload is otherwise valid, a record with no source id must not be
    # silently accepted under a machine-derived key on reprocess.
    resolved = engine.reprocess_dead_letter(dead_letter_rebuilders())
    assert resolved == 0
    assert len(store.list_dead_letter()) == 1
