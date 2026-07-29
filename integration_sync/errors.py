"""Error taxonomy for the sync engine.

The engine treats two failure classes very differently:

- ``TransientError``  -> retryable. The engine retries with exponential backoff. If it
  still fails after the last attempt, the record is dead-lettered rather than lost.
- ``PoisonError``     -> not retryable. The record itself is malformed, so retrying would
  fail identically forever. It goes straight to the dead-letter queue for human review.

Anything else that escapes is a bug in the engine, not a data problem, and is allowed to
propagate so it is loud rather than silently swallowed.
"""

from __future__ import annotations


class SyncError(Exception):
    """Base class for all sync-engine errors."""


class TransientError(SyncError):
    """A failure that may succeed if retried (a simulated flaky dependency, a lock, etc.).

    Raising this is the contract for "retry me with backoff".
    """


class RateLimitError(TransientError):
    """A quota rejection from a source API: the 429-equivalent.

    It is a ``TransientError`` because it always succeeds eventually, but it carries one
    extra piece of information the generic retry path does not have: ``retry_after``, the
    server's own instruction for how long to wait. A client that ignores that number and
    falls back to its own exponential schedule either waits too long (throughput lost) or
    hammers the endpoint again too soon (quota never drains). The fetch loop in
    ``ticket_source`` honors ``retry_after`` when present and uses its own backoff schedule
    only when it is absent.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PoisonError(SyncError):
    """A record that can never succeed because it is structurally invalid.

    Raising this is the contract for "do not retry me, dead-letter me". Carries the
    natural key when one could be recovered from the malformed record, so the dead-letter
    row is addressable.
    """

    def __init__(self, message: str, *, natural_key: str | None = None) -> None:
        super().__init__(message)
        self.natural_key = natural_key
