"""The sync engine: the piece that makes syncing external systems into the CRM correct.

Guarantees, and how each is achieved:

Idempotent
    Every record has a natural key (source-provided stable id) and a content hash. The
    ``sync_records`` ledger stores the last hash seen per key. A record whose hash matches
    the ledger is a no-op: no writes, no audit rows. So running a sync twice on unchanged
    data changes nothing. Duplicate records within a batch collapse the same way.

Incremental
    Each source has a cursor (high-water mark) in ``sync_state``. The reader is handed the
    cursor and yields only records newer than it. After a successful pass the cursor
    advances to the newest timestamp handled, so the next run does the minimum work.

Conflict policy (documented: last-write-wins by event time, with an audit trail)
    When the same key arrives with a *different* hash, it is a real change. The winner is
    the record with the later ``occurred_at`` (ties broken deterministically by hash). The
    losing (stale) record is ignored, and every create / update / conflict decision is
    written to ``audit_log`` so the resolution is never silent. The conflict path is itself
    idempotent: a stale record re-seen is not re-audited.

Retryable
    The write of a record is wrapped in exponential backoff. Only ``TransientError`` is
    retried. Because all of a record's writes happen in one transaction that rolls back on
    failure, a retry re-applies cleanly with no partial state.

Dead-lettered
    A structurally invalid record (``PoisonError``) goes straight to the dead-letter queue.
    A record that still fails after exhausting its retry budget is dead-lettered too, so
    nothing is silently dropped. Dead-letter rows carry the payload, the error, a category,
    and a failure count, and can be reprocessed once the underlying problem is fixed.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .crm_store import CrmStore
from .errors import PoisonError, TransientError
from .hashing import content_hash
from .models import CanonicalRecord, RawRecord
from .normalize import normalize
from .retry import call_with_retry

# Category marker for a record that arrived with no natural key. It is stored under a
# stable content-derived key so the DLQ can dedupe it, but it is NOT auto-recoverable on
# reprocess: without a source id, only a human can decide its identity.
CATEGORY_NO_KEY = "poison_no_key"

log = logging.getLogger("sync")

# A fault hook simulates a flaky downstream write. It is called with the record about to be
# committed and may raise TransientError. Returning normally means "the write succeeded".
FaultHook = Callable[[CanonicalRecord], None]

# A reader is handed the current cursor and yields the raw records after it.
Reader = Callable[[str | None], Iterable[RawRecord]]


@dataclass
class SyncStats:
    """Per-run outcome counts. The demo prints these; the tests assert on them."""

    source: str
    created: int = 0
    unchanged: int = 0
    updated: int = 0
    conflict_ignored: int = 0
    dead_lettered: int = 0
    retries: int = 0
    cursor_before: str | None = None
    cursor_after: str | None = None
    dead_letter_keys: list[str] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return self.created + self.unchanged + self.updated + self.conflict_ignored

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "created": self.created,
            "unchanged": self.unchanged,
            "updated": self.updated,
            "conflict_ignored": self.conflict_ignored,
            "dead_lettered": self.dead_lettered,
            "retries": self.retries,
            "cursor_before": self.cursor_before,
            "cursor_after": self.cursor_after,
        }


class SyncEngine:
    def __init__(
        self,
        store: CrmStore,
        *,
        attempts: int = 3,
        base_delay: float = 0.05,
        factor: float = 2.0,
        max_delay: float | None = 2.0,
        jitter: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        fault: FaultHook | None = None,
    ) -> None:
        self.store = store
        self.attempts = attempts
        self.base_delay = base_delay
        self.factor = factor
        self.max_delay = max_delay
        self.jitter = jitter
        self.sleep = sleep
        self.fault = fault

    # -- public API ----------------------------------------------------------

    def sync_source(self, source: str, reader: Reader, *, advance_cursor: bool = True) -> SyncStats:
        """Run one incremental pass of ``source`` through ``reader``."""
        cursor_before = self.store.get_cursor(source)
        stats = SyncStats(source=source, cursor_before=cursor_before)
        max_seen = cursor_before

        for raw in reader(cursor_before):
            # 1) Validate. Poison records never reach the write path.
            try:
                record = normalize(raw)
            except PoisonError as exc:
                self._dead_letter(raw, str(exc), category="poison", key=exc.natural_key)
                stats.dead_lettered += 1
                stats.dead_letter_keys.append(exc.natural_key or "<no-key>")
                log.warning("%s dead-letter (poison): %s", source, exc)
                continue

            # 2) Apply with retry/backoff. Exhausted transient failures are dead-lettered.
            try:
                decision = self._apply_with_retry(record, stats)
            except TransientError as exc:
                self._dead_letter(
                    raw, f"retry budget exhausted: {exc}",
                    category="transient_exhausted", key=record.natural_key,
                )
                stats.dead_lettered += 1
                stats.dead_letter_keys.append(record.natural_key)
                log.warning(
                    "%s dead-letter (transient, %d attempts): %s",
                    source, self.attempts, record.natural_key,
                )
                max_seen = _max_iso(max_seen, record.occurred_at.isoformat())
                continue

            self._count(stats, decision)
            max_seen = _max_iso(max_seen, record.occurred_at.isoformat())

        if advance_cursor:
            self.store.set_cursor(source, max_seen)
        stats.cursor_after = max_seen
        return stats

    def reprocess_dead_letter(
        self, reader_by_source: dict[str, Callable[[str, dict], RawRecord]]
    ) -> int:
        """Re-run every unresolved dead-letter row through the pipeline.

        ``reader_by_source`` rebuilds a ``RawRecord`` from the stored natural key and payload
        for each source. A row that now succeeds is marked resolved; one that fails again
        stays queued with a bumped failure count. Returns the number of rows resolved.
        """
        resolved = 0
        for row in self.store.list_dead_letter(include_resolved=False):
            source = row["source"]
            build = reader_by_source.get(source)
            if build is None:
                continue
            payload = json.loads(row["payload_json"])
            # A no-key row is handed back its (absent) key so it re-poisons rather than
            # being silently accepted under a machine-derived key.
            nk = None if row["category"] == CATEGORY_NO_KEY else row["natural_key"]
            raw = build(nk, payload)
            try:
                record = normalize(raw)
                self._apply_with_retry(record, SyncStats(source=source))
            except (PoisonError, TransientError) as exc:
                self.store.add_dead_letter(
                    source=source, natural_key=row["natural_key"],
                    payload_json=row["payload_json"], error=str(exc),
                    category=row["category"],
                )
                log.warning("reprocess failed for %s/%s: %s", source, row["natural_key"], exc)
                continue
            self.store.resolve_dead_letter(int(row["dl_id"]))
            resolved += 1
            log.info("reprocess resolved %s/%s", source, row["natural_key"])
        return resolved

    # -- internals -----------------------------------------------------------

    def _apply_with_retry(self, record: CanonicalRecord, stats: SyncStats) -> str:
        def on_retry(attempt: int, delay: float, exc: TransientError) -> None:
            stats.retries += 1
            log.warning(
                "%s/%s transient failure (attempt %d), backing off %.3fs: %s",
                record.source, record.natural_key, attempt, delay, exc,
            )

        return call_with_retry(
            lambda: self._apply_once(record),
            attempts=self.attempts,
            base=self.base_delay,
            factor=self.factor,
            max_delay=self.max_delay,
            jitter=self.jitter,
            sleep=self.sleep,
            on_retry=on_retry,
        )

    def _apply_once(self, record: CanonicalRecord) -> str:
        """Apply one record atomically. Returns the decision string.

        The whole unit (contact + account + activity + ledger + audit) commits together, or
        the injected fault rolls it all back before commit so a retry starts clean.
        """
        source, key = record.source, record.natural_key
        existing = self.store.get_sync_record(source, key)

        if existing is not None and existing["content_hash"] == record.content_hash:
            # Idempotent no-op: identical content already synced.
            log.info("%s/%s unchanged", source, key)
            return "unchanged"

        decision = self._decide(record, existing)
        if decision == "conflict_ignored":
            # Losing (stale) write. Record the decision once, then stop; do not touch state.
            if not self.store.has_audit(
                source=source, natural_key=key, action="conflict_ignored",
                new_hash=record.content_hash,
            ):
                with self.store.transaction():
                    self.store.add_audit(
                        source=source, natural_key=key, action="conflict_ignored",
                        old_hash=existing["content_hash"] if existing else None,
                        new_hash=record.content_hash,
                        detail=f"incoming occurred_at {record.occurred_at.isoformat()} "
                               f"older than stored; last-write-wins keeps stored",
                    )
            log.info("%s/%s conflict ignored (stale)", source, key)
            return "conflict_ignored"

        # created or updated: do the writes inside one transaction.
        with self.store.transaction():
            contact_id = self.store.upsert_contact(record.contact_email, record.contact_name)
            account_id = self.store.upsert_account(record.account_name, record.account_domain)
            self.store.upsert_activity(
                source=source, natural_key=key, contact_id=contact_id, account_id=account_id,
                kind=record.kind, subject=record.subject, body=record.body,
                occurred_at=record.occurred_at.isoformat(),
            )
            version = 1 if existing is None else int(existing["version"]) + 1
            # Fault injection point: simulate the downstream write failing AFTER staging the
            # rows. Raising here rolls the whole transaction back, so a retry is clean.
            if self.fault is not None:
                self.fault(record)
            self.store.put_sync_record(source, key, record.content_hash, version)
            self.store.add_audit(
                source=source, natural_key=key, action=decision,
                old_hash=existing["content_hash"] if existing else None,
                new_hash=record.content_hash,
                detail=f"version {version}",
            )
        log.info("%s/%s %s (v%d)", source, key, decision, version)
        return decision

    def _decide(self, record: CanonicalRecord, existing) -> str:
        if existing is None:
            return "created"
        # Same key, different hash -> conflict resolved by last-write-wins on event time.
        stored_activity = self.store.get_activity(record.source, record.natural_key)
        stored_occurred = stored_activity["occurred_at"] if stored_activity else ""
        incoming = record.occurred_at.isoformat()
        if incoming > stored_occurred:
            return "updated"
        if incoming < stored_occurred:
            return "conflict_ignored"
        # Equal timestamps, different content: deterministic tie-break by hash.
        stored_hash = existing["content_hash"]
        return "updated" if record.content_hash > stored_hash else "conflict_ignored"

    def _dead_letter(
        self, raw: RawRecord, error: str, *, category: str, key: str | None
    ) -> None:
        if key is None:
            # Stable, content-derived key so re-seeing the same keyless record dedupes in
            # the DLQ instead of piling up new rows (Python's built-in hash is not stable
            # across processes, so a real digest is used).
            digest = content_hash(raw.payload)[:16]
            natural_key = f"{raw.source}:no-key:{digest}"
            category = CATEGORY_NO_KEY
        else:
            natural_key = key
        with self.store.transaction():
            self.store.add_dead_letter(
                source=raw.source, natural_key=natural_key,
                payload_json=json.dumps(raw.payload, default=str),
                error=error, category=category,
            )

    @staticmethod
    def _count(stats: SyncStats, decision: str) -> None:
        if decision == "created":
            stats.created += 1
        elif decision == "unchanged":
            stats.unchanged += 1
        elif decision == "updated":
            stats.updated += 1
        elif decision == "conflict_ignored":
            stats.conflict_ignored += 1


def _max_iso(a: str | None, b: str | None) -> str | None:
    candidates = [x for x in (a, b) if x is not None]
    return max(candidates) if candidates else None
