"""Operational metrics: the numbers the dashboard renders.

Two kinds of test live here. The time and age helpers are pure and are tested directly,
including the boundaries, because an off-by-one on an age band is how an ops view starts
lying quietly. The aggregate functions are tested against a store populated by the REAL
sync engine rather than by hand-inserted rows, so a change to the engine that breaks a
metric fails here instead of being papered over by a fixture that agrees with itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from integration_sync import ops_metrics as om
from integration_sync import sync_engine
from integration_sync.crm_store import CrmStore
from integration_sync.errors import TransientError
from integration_sync.models import SOURCE_CALENDAR, SOURCE_EMAIL
from integration_sync.sync_engine import SyncEngine

from .conftest import make_raw, mem_reader

AS_OF = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)


# -- pure helpers -------------------------------------------------------------


def test_parse_iso_assumes_utc_when_a_stamp_carries_no_zone():
    assert om.parse_iso("2026-04-10T09:00:00") == datetime(2026, 4, 10, 9, tzinfo=UTC)


def test_parse_iso_normalizes_an_offset_to_utc():
    assert om.parse_iso("2026-04-10T05:00:00-04:00") == datetime(2026, 4, 10, 9, tzinfo=UTC)


def test_an_unparseable_stamp_degrades_to_none_rather_than_raising():
    # One bad row should dim a tile, not take the page down.
    assert om.parse_iso("not a timestamp") is None
    assert om.parse_iso(None) is None
    assert om.age_hours("not a timestamp", AS_OF) is None


def test_age_hours_measures_backwards_and_never_goes_negative():
    assert om.age_hours("2026-04-10T09:00:00+00:00", AS_OF) == pytest.approx(3.0)
    # A clock-skewed future stamp reads as zero rather than as a negative age.
    assert om.age_hours("2026-04-11T00:00:00+00:00", AS_OF) == 0.0


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        (0.0, "under 1h"),
        (0.99, "under 1h"),
        (1.0, "1h to 24h"),  # the boundary belongs to the higher band
        (23.99, "1h to 24h"),
        (24.0, "1d to 7d"),
        (167.99, "1d to 7d"),
        (168.0, "over 7d"),
        (None, "unknown"),
    ],
)
def test_age_bands_at_their_boundaries(hours, expected):
    assert om.age_band(hours) == expected


def test_bucket_key_floors_to_the_hour_and_to_the_day():
    assert om.bucket_key("2026-04-10T09:47:31+00:00") == "2026-04-10T09:00:00+00:00"
    assert om.bucket_key("2026-04-10T09:47:31+00:00", om.BUCKET_DAY) == "2026-04-10T00:00:00+00:00"


def test_an_unknown_bucket_is_refused():
    with pytest.raises(ValueError, match="unknown bucket"):
        om.bucket_key("2026-04-10T09:00:00+00:00", "fortnight")


def test_bucket_range_fills_the_gaps_between_two_stamps():
    # A line drawn only through buckets that have data implies activity that did not happen.
    keys = om.bucket_range("2026-04-10T09:10:00+00:00", "2026-04-10T12:05:00+00:00")
    assert keys == [
        "2026-04-10T09:00:00+00:00",
        "2026-04-10T10:00:00+00:00",
        "2026-04-10T11:00:00+00:00",
        "2026-04-10T12:00:00+00:00",
    ]


def test_bucket_range_is_empty_when_the_window_is_inverted():
    assert om.bucket_range("2026-04-10T12:00:00+00:00", "2026-04-10T09:00:00+00:00") == []


# -- fixtures built by the real engine ---------------------------------------


def populate(store: CrmStore) -> SyncEngine:
    """One created record, one updated record, one conflict, and one poison record."""
    engine = SyncEngine(store, sleep=lambda _d: None)
    engine.sync_source(
        SOURCE_CALENDAR,
        mem_reader(
            [
                make_raw("cal-1", occurred_at="2026-04-01T09:00:00+00:00"),
                make_raw("cal-2", occurred_at="2026-04-02T09:00:00+00:00"),
                make_raw(None, occurred_at="2026-04-02T10:00:00+00:00"),  # no key: poison
            ]
        ),
        advance_cursor=False,
    )
    # A genuine content change on cal-1, then a stale copy of it that must lose.
    engine.sync_source(
        SOURCE_CALENDAR,
        mem_reader([make_raw("cal-1", occurred_at="2026-04-03T09:00:00+00:00", subject="Moved")]),
        advance_cursor=False,
    )
    engine.sync_source(
        SOURCE_CALENDAR,
        mem_reader([make_raw("cal-1", occurred_at="2026-04-01T09:00:00+00:00", subject="Old")]),
        advance_cursor=False,
    )
    return engine


def test_the_funnel_accounts_for_every_record_the_engine_decided_on(store):
    populate(store)
    result = om.funnel(store)

    assert result.ingested == 3  # cal-1, cal-2, and the keyless record
    assert result.validated == 2
    assert result.upserted == 2
    assert result.failed_validation == 1
    assert result.failed_write == 0
    assert result.dead_lettered_open == 1
    assert result.upsert_rate == pytest.approx(2 / 3)


def test_the_funnel_stages_never_widen_as_they_descend(store):
    # The property that makes it a funnel at all, asserted rather than assumed.
    populate(store)
    counts = [count for _label, count in om.funnel(store).stages[:3]]
    assert counts == sorted(counts, reverse=True)


def test_an_empty_store_reports_zeroes_and_no_divide_by_zero(store):
    result = om.funnel(store)
    assert result.ingested == 0
    assert result.upsert_rate is None


def test_a_write_failure_is_counted_separately_from_a_validation_failure(store):
    # The two failures need different humans: one is a data bug, one is a dependency.
    def always_fail(_record):
        raise TransientError("SYNTHETIC downstream failure")

    engine = SyncEngine(store, sleep=lambda _d: None, attempts=2, fault=always_fail)
    engine.sync_source(
        SOURCE_EMAIL,
        mem_reader([make_raw("eml-1", occurred_at="2026-04-01T09:00:00+00:00",
                             source=SOURCE_EMAIL)]),
    )
    result = om.funnel(store)
    assert result.failed_write == 1
    assert result.failed_validation == 0
    assert result.validated == 1  # it passed validation; the WRITE is what failed
    assert result.upserted == 0


def test_the_funnel_splits_on_the_engines_own_category_constants(store):
    """The split above is only trustworthy if both sides agree on the category strings.

    ``ops_metrics`` used to re-declare them. A rename in the engine would then have left the
    funnel matching strings nobody writes any more, and ``failed_write`` would have read 0
    forever with a green suite behind it.

    Asserting the two constants are equal would not catch that, because CPython interns
    identifier-shaped literals and a copy would compare equal anyway. So this drives the
    queue with whatever the ENGINE currently calls each category and asserts the funnel
    still classifies it. A rename on one side only makes this fail.
    """
    assert om.POISON_PREFIX == sync_engine.POISON_PREFIX
    assert om.CATEGORY_TRANSIENT == sync_engine.CATEGORY_TRANSIENT

    with store.transaction():
        store.add_dead_letter(
            source=SOURCE_EMAIL, natural_key="bad-shape", payload_json="{}",
            error="SYNTHETIC", category=sync_engine.CATEGORY_NO_KEY,
        )
        store.add_dead_letter(
            source=SOURCE_EMAIL, natural_key="flaky-write", payload_json="{}",
            error="SYNTHETIC", category=sync_engine.CATEGORY_TRANSIENT,
        )
    result = om.funnel(store)

    assert result.failed_validation == 1
    assert result.failed_write == 1
    # The control: neither count is picking up both rows, so the split is real.
    assert result.ingested == 2


def test_a_reprocessed_record_stops_counting_as_lost(store):
    # A dead-letter row with a ledger entry beside it is history, not backlog.
    calls = {"n": 0}

    def fail_once(_record):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise TransientError("SYNTHETIC downstream failure")

    engine = SyncEngine(store, sleep=lambda _d: None, attempts=2, fault=fail_once)
    raw = make_raw("eml-1", occurred_at="2026-04-01T09:00:00+00:00", source=SOURCE_EMAIL)
    engine.sync_source(SOURCE_EMAIL, mem_reader([raw]), advance_cursor=False)
    assert om.funnel(store).failed_write == 1  # the control: it really was lost first

    engine.sync_source(SOURCE_EMAIL, mem_reader([raw]), advance_cursor=False)
    recovered = om.funnel(store)
    assert recovered.failed_write == 0
    assert recovered.upserted == 1


def test_throughput_splits_creates_updates_and_conflicts_per_source(store):
    populate(store)
    rows = {row.source: row for row in om.throughput_by_source(store, AS_OF)}
    calendar = rows[SOURCE_CALENDAR]

    assert calendar.records == 2
    assert calendar.created == 2
    assert calendar.updated == 1
    assert calendar.conflicts == 1
    assert calendar.dead_lettered_open == 1
    assert calendar.last_write_age_hours is not None


def test_a_source_with_only_a_cursor_still_appears(store):
    # A source that has synced nothing yet is exactly the one worth seeing on a dashboard.
    store.set_cursor(SOURCE_EMAIL, None)
    assert [row.source for row in om.throughput_by_source(store, AS_OF)] == [SOURCE_EMAIL]


def test_writes_over_time_counts_only_real_writes(store):
    populate(store)
    series = om.writes_over_time(store)
    assert sum(b.created for b in series) == 2
    assert sum(b.updated for b in series) == 1
    # The conflict and the drift notes are audit rows too, and must not inflate the series.
    assert sum(b.total for b in series) == 3


def test_writes_over_time_is_empty_on_a_store_that_never_wrote(store):
    assert om.writes_over_time(store) == []


def test_the_dead_letter_report_puts_the_oldest_row_first(store):
    with store.transaction():
        store.add_dead_letter(
            source=SOURCE_EMAIL, natural_key="new", payload_json="{}",
            error="SYNTHETIC", category="poison",
        )
    # Backdate one row so the ordering has something real to sort on.
    store.conn.execute(
        "INSERT INTO dead_letter (source, natural_key, payload_json, error, category, "
        "failure_count, first_seen_at, last_seen_at, resolved) "
        "VALUES (?, 'old', '{}', 'SYNTHETIC', 'transient_exhausted', 3, ?, ?, 0)",
        (SOURCE_EMAIL, "2026-04-01T12:00:00+00:00", "2026-04-01T12:00:00+00:00"),
    )
    report = om.dead_letter_report(store, AS_OF)

    assert report.depth == 2
    assert [row.natural_key for row in report.rows] == ["old", "new"]
    assert report.rows[0].age_hours == pytest.approx(9.0 * 24)
    assert report.rows[0].band == "over 7d"
    assert report.by_source[SOURCE_EMAIL] == 2
    assert report.oldest_hours == pytest.approx(9.0 * 24)


def test_a_resolved_row_leaves_the_queue(store):
    with store.transaction():
        store.add_dead_letter(
            source=SOURCE_EMAIL, natural_key="k", payload_json="{}",
            error="SYNTHETIC", category="poison",
        )
    assert om.dead_letter_report(store, AS_OF).depth == 1  # the control
    row = store.list_dead_letter()[0]
    store.resolve_dead_letter(int(row["dl_id"]))
    assert om.dead_letter_report(store, AS_OF).depth == 0


def test_cursor_lag_separates_a_quiet_source_from_a_stopped_job(store):
    store.set_cursor(SOURCE_CALENDAR, "2026-04-09T12:00:00+00:00")
    # ``set_cursor`` stamps ``updated_at`` with the real clock, which is far LATER than the
    # fixed AS_OF, and ``age_hours`` floors a future stamp at 0. Left alone, the staleness
    # assertion below could not fail whatever the code did. So the row is restamped to a
    # real 30 minutes before AS_OF: a job that ran recently, against data a day old.
    store.conn.execute(
        "UPDATE sync_state SET updated_at = ? WHERE source = ?",
        ((AS_OF - timedelta(minutes=30)).isoformat(), SOURCE_CALENDAR),
    )
    lags = {row.source: row for row in om.cursor_lag(store, AS_OF)}
    calendar = lags[SOURCE_CALENDAR]

    assert calendar.lag_hours == pytest.approx(24.0)  # the DATA is a day behind
    assert calendar.staleness_hours == pytest.approx(0.5)  # the JOB ran half an hour ago
    # The pairing is the whole point: conflating these two is the classic misread.
    assert calendar.staleness_hours < 1.0 < calendar.lag_hours


def test_a_wedged_job_shows_high_staleness_on_the_same_cursor(store):
    # The control for the test above. Same data lag, stopped worker, and the two numbers
    # separate in the opposite direction.
    store.set_cursor(SOURCE_CALENDAR, "2026-04-09T12:00:00+00:00")
    store.conn.execute(
        "UPDATE sync_state SET updated_at = ? WHERE source = ?",
        ((AS_OF - timedelta(hours=8)).isoformat(), SOURCE_CALENDAR),
    )
    calendar = om.cursor_lag(store, AS_OF)[0]

    assert calendar.lag_hours == pytest.approx(24.0)
    assert calendar.staleness_hours == pytest.approx(8.0)
    assert calendar.staleness_hours > 1.0


def test_a_cursor_that_was_never_set_reports_unknown_lag_not_zero(store):
    store.set_cursor(SOURCE_CALENDAR, None)
    assert om.cursor_lag(store, AS_OF)[0].lag_hours is None


def test_deal_amounts_sum_exactly_in_decimal(store):
    # 0.1 + 0.2 in binary float is the reason this function does not use float.
    for i, amount in enumerate(("0.10", "0.20", "48000.00")):
        store.upsert_deal(
            deal_id=f"d{i}", name="SYNTHETIC", stage="contractsent", pipeline="default",
            amount=amount, close_date="", owner_ref="", contact_email="",
            is_open=True, updated_at="2026-04-01T00:00:00+00:00",
        )
    summary = om.deals_by_stage(store)
    assert [(s.stage, s.deals, s.amount) for s in summary] == [
        ("contractsent", 3, "48000.30")
    ]


def test_a_non_numeric_deal_amount_does_not_poison_the_total(store):
    store.upsert_deal(
        deal_id="d1", name="SYNTHETIC", stage="qualifiedtobuy", pipeline="default",
        amount="not a number", close_date="", owner_ref="", contact_email="",
        is_open=True, updated_at="2026-04-01T00:00:00+00:00",
    )
    assert om.deals_by_stage(store)[0].amount == "0.00"
