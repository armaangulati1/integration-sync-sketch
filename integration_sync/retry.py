"""Retry-with-exponential-backoff primitive, isolated so it is unit-testable in the open.

Design choices:

- The delay schedule is a *pure function* of its inputs, so tests can assert the exact
  sequence of sleeps without any wall-clock dependence.
- Jitter is optional and off by default. When on, it uses full jitter (a random value in
  ``[0, delay]``) which is the AWS-recommended shape for avoiding thundering herds. The
  jitter source is injectable so it, too, is deterministic under test.
- The runner sleeps through an injected ``sleep`` callable, so tests pass a fake that
  records durations instead of actually blocking.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import TypeVar

from .errors import TransientError

T = TypeVar("T")


def backoff_delays(
    *,
    attempts: int,
    base: float,
    factor: float = 2.0,
    max_delay: float | None = None,
) -> list[float]:
    """Return the delays *between* attempts for a given retry budget.

    For ``attempts`` total tries there are ``attempts - 1`` waits. Delay i is
    ``base * factor**i``, optionally capped at ``max_delay``.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    delays: list[float] = []
    for i in range(attempts - 1):
        delay = base * (factor**i)
        if max_delay is not None:
            delay = min(delay, max_delay)
        delays.append(delay)
    return delays


def call_with_retry(
    func: Callable[[], T],
    *,
    attempts: int,
    base: float,
    factor: float = 2.0,
    max_delay: float | None = None,
    jitter: bool = False,
    sleep: Callable[[float], None],
    rand: Callable[[], float] = random.random,
    on_retry: Callable[[int, float, TransientError], None] | None = None,
) -> T:
    """Call ``func`` up to ``attempts`` times, backing off on ``TransientError``.

    Only ``TransientError`` is retried. Any other exception propagates immediately, since
    it is either a poison record (handled by the caller) or a genuine bug. If the final
    attempt still raises ``TransientError``, that error is re-raised so the caller can
    dead-letter the record.
    """
    delays = backoff_delays(attempts=attempts, base=base, factor=factor, max_delay=max_delay)
    last_error: TransientError | None = None
    for attempt in range(attempts):
        try:
            return func()
        except TransientError as exc:
            last_error = exc
            if attempt >= attempts - 1:
                break
            delay = delays[attempt]
            if jitter:
                delay = rand() * delay
            if on_retry is not None:
                on_retry(attempt + 1, delay, exc)
            sleep(delay)
    assert last_error is not None
    raise last_error
