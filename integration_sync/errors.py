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


class PoisonError(SyncError):
    """A record that can never succeed because it is structurally invalid.

    Raising this is the contract for "do not retry me, dead-letter me". Carries the
    natural key when one could be recovered from the malformed record, so the dead-letter
    row is addressable.
    """

    def __init__(self, message: str, *, natural_key: str | None = None) -> None:
        super().__init__(message)
        self.natural_key = natural_key
