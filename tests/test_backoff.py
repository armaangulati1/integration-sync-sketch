"""Retry with exponential backoff: the pure delay schedule and the engine's use of it."""

from __future__ import annotations

import pytest

from integration_sync.errors import PoisonError, TransientError
from integration_sync.models import SOURCE_CALENDAR
from integration_sync.retry import backoff_delays, call_with_retry
from integration_sync.sync_engine import SyncEngine

from .conftest import make_raw, mem_reader


def test_backoff_delays_are_exponential():
    assert backoff_delays(attempts=4, base=0.1, factor=2.0) == [0.1, 0.2, 0.4]


def test_backoff_delays_respect_cap():
    assert backoff_delays(attempts=5, base=1.0, factor=10.0, max_delay=5.0) == [1.0, 5.0, 5.0, 5.0]


def test_single_attempt_has_no_delays():
    assert backoff_delays(attempts=1, base=1.0) == []


def test_call_with_retry_succeeds_after_transient_failures():
    calls = {"n": 0}
    slept: list[float] = []

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientError("still flaky")
        return "ok"

    result = call_with_retry(
        flaky, attempts=5, base=0.1, factor=2.0, sleep=slept.append
    )
    assert result == "ok"
    assert calls["n"] == 3
    assert slept == [0.1, 0.2]  # two backoffs before the third, successful attempt


def test_call_with_retry_reraises_after_budget():
    slept: list[float] = []

    def always_fail():
        raise TransientError("nope")

    with pytest.raises(TransientError):
        call_with_retry(always_fail, attempts=3, base=0.1, factor=2.0, sleep=slept.append)
    assert slept == [0.1, 0.2]  # exactly attempts-1 backoffs


def test_non_transient_errors_are_not_retried():
    slept: list[float] = []

    def poison():
        raise PoisonError("bad record")

    with pytest.raises(PoisonError):
        call_with_retry(poison, attempts=5, base=0.1, sleep=slept.append)
    assert slept == []  # a poison error is never retried


def test_full_jitter_stays_within_bounds():
    # rand returns 0.5, so each delay is halved; jitter never exceeds the base schedule.
    slept: list[float] = []
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientError("flaky")
        return "ok"

    call_with_retry(
        flaky, attempts=5, base=1.0, factor=2.0, jitter=True,
        rand=lambda: 0.5, sleep=slept.append,
    )
    assert slept == [0.5, 1.0]  # 0.5 * [1.0, 2.0]


def test_engine_retries_transient_write_then_succeeds(store):
    attempts_seen = {"n": 0}

    def fault(_record):
        attempts_seen["n"] += 1
        if attempts_seen["n"] < 3:
            raise TransientError("flaky downstream")

    engine = SyncEngine(store, attempts=5, base_delay=0.0, sleep=lambda _d: None, fault=fault)
    stats = engine.sync_source(
        SOURCE_CALENDAR,
        mem_reader([make_raw("evt-r@sketch", occurred_at="2026-03-02T09:00:00+00:00")]),
    )
    assert stats.created == 1
    assert stats.retries == 2
    assert stats.dead_lettered == 0
    assert store.count("activities") == 1


def test_engine_rolls_back_on_transient_failure(store):
    # A fault that fires on the first attempt must leave no partial rows behind.
    state = {"first": True}

    def fault(_record):
        if state["first"]:
            state["first"] = False
            raise TransientError("flaky")

    engine = SyncEngine(store, attempts=3, base_delay=0.0, sleep=lambda _d: None, fault=fault)
    engine.sync_source(
        SOURCE_CALENDAR,
        mem_reader([make_raw("evt-rb@sketch", occurred_at="2026-03-02T09:00:00+00:00")]),
    )
    # Exactly one activity, one contact, one ledger row: the rolled-back attempt left nothing.
    assert store.count("activities") == 1
    assert store.count("contacts") == 1
    assert store.count("sync_records") == 1
