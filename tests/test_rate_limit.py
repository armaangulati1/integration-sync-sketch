"""Rate limiting: a real token bucket, and a client that actually waits it out.

The limiter here is not a counter that fires on the third call. It is a token bucket over an
injected clock, and the ONLY thing that drains it is time advancing. The fake sleep in these
tests advances that clock, so a test that claims "the client backed off and then succeeded"
is claiming something the bucket genuinely enforced: under-sleeping is refused again, and
the first admitted call happens exactly when the window rolls.
"""

from __future__ import annotations

import pytest

from integration_sync.errors import RateLimitError
from integration_sync.ticket_source import (
    SyntheticTicketApi,
    _Bucket,
    fetch_pages,
    read_tickets,
)


class FakeClock:
    """A clock the fake sleep moves. Nothing else advances it, so waits are load-bearing."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def issue(key: str, updated: str) -> dict:
    return {
        "key": key,
        "fields": {
            "summary": f"SYNTHETIC {key}",
            "status": "Open",
            "assignee": {"emailAddress": "dana.rivers@northgate-health.example"},
            "duedate": "2026-04-10",
            "updated": updated,
            "customfield_milestone": "M1",
        },
    }


def five_issues() -> list[dict]:
    return [issue(f"OPS-10{i}", f"2026-04-0{i + 1}T09:00:00+00:00") for i in range(5)]


# -- the bucket itself --------------------------------------------------------


def test_bucket_admits_up_to_the_limit():
    clock = FakeClock()
    bucket = _Bucket(limit=2, window=60.0, clock=clock)
    assert bucket.take() is None
    assert bucket.take() is None


def test_bucket_refuses_past_the_limit_and_reports_the_real_wait():
    clock = FakeClock()
    bucket = _Bucket(limit=2, window=60.0, clock=clock)
    bucket.take()
    bucket.take()
    assert bucket.take() == pytest.approx(60.0)


def test_under_sleeping_is_still_refused():
    # The proof that the limiter is real: waiting less than it asked does not get you in.
    clock = FakeClock()
    bucket = _Bucket(limit=2, window=60.0, clock=clock)
    bucket.take()
    bucket.take()
    wait = bucket.take()
    clock.sleep(wait - 1.0)
    assert bucket.take() == pytest.approx(1.0)  # refused again, 1s still owed
    clock.sleep(1.0)
    assert bucket.take() is None  # admitted exactly when the window rolls


# -- the client ---------------------------------------------------------------


def test_client_honors_retry_after_and_gets_every_page():
    clock = FakeClock()
    api = SyntheticTicketApi(
        five_issues(), page_size=2, limit=2, window=60.0, clock=clock
    )
    pages = list(fetch_pages(api, sleep=clock.sleep, attempts=4, base=0.5))

    # 5 issues at 2 per page is 3 pages; the quota allows 2 calls per 60s window.
    assert [len(p["issues"]) for p in pages] == [2, 2, 1]
    assert api.refusals == 1  # the third page was throttled once
    assert api.calls == 3  # and all three pages were eventually served
    # It waited the server's number, not its own 0.5s schedule.
    assert clock.slept == [pytest.approx(60.0)]


def test_client_falls_back_to_exponential_when_no_retry_after_is_offered():
    # A server that refuses without saying when to come back. The client must use its own
    # schedule, and the bucket only drains once the cumulative wait clears the window.
    clock = FakeClock()
    api = SyntheticTicketApi(
        five_issues(), page_size=2, limit=2, window=2.0, clock=clock,
        advertise_retry_after=False,
    )
    pages = list(fetch_pages(api, sleep=clock.sleep, attempts=4, base=0.5, factor=2.0))

    assert [len(p["issues"]) for p in pages] == [2, 2, 1]
    # 0.5 and 1.0 both land inside the 2s window and are refused; 2.0 clears it.
    assert clock.slept == [pytest.approx(0.5), pytest.approx(1.0), pytest.approx(2.0)]
    assert api.refusals == 3


def test_exhausted_retry_budget_raises_rather_than_returning_short():
    # A short budget against a long window: the client must surface the throttle, never
    # quietly return a partial page set that a caller would mistake for the whole board.
    clock = FakeClock()
    api = SyntheticTicketApi(
        five_issues(), page_size=2, limit=2, window=600.0, clock=clock,
        advertise_retry_after=False,
    )
    with pytest.raises(RateLimitError):
        list(fetch_pages(api, sleep=clock.sleep, attempts=2, base=0.5))
    assert clock.slept == [pytest.approx(0.5)]  # exactly attempts-1 waits


def test_reader_survives_throttling_and_yields_all_tickets():
    clock = FakeClock()
    api = SyntheticTicketApi(
        five_issues(), page_size=2, limit=1, window=30.0, clock=clock
    )
    records = list(read_tickets(api, sleep=clock.sleep))
    assert [r.natural_key for r in records] == [
        "OPS-100", "OPS-101", "OPS-102", "OPS-103", "OPS-104",
    ]
    assert api.refusals > 0  # it really was throttled on the way
    assert len(clock.slept) == api.refusals


def test_cursor_limits_what_the_endpoint_even_returns():
    clock = FakeClock()
    api = SyntheticTicketApi(five_issues(), page_size=10, clock=clock)
    records = list(read_tickets(api, "2026-04-03T09:00:00+00:00", sleep=clock.sleep))
    assert [r.natural_key for r in records] == ["OPS-103", "OPS-104"]
