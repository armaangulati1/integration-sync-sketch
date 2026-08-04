"""Operational metrics over the sync store: what a person on call would actually ask.

The dashboard is a thin renderer. Every number it shows is computed here, by a function
that takes a store and an ``as_of`` instant and returns a dataclass, so the interesting
part is testable without a browser and without importing a UI framework.

Two decisions are worth stating up front, because they are what make the numbers honest.

**The funnel is reconstructed from terminal state, not counted at read time.** The engine
does not keep a running tally of records it was handed. What the store holds is where each
record ENDED UP: a row in ``sync_records`` if it landed, a row in ``dead_letter`` if it did
not. So the funnel is derived from the union of those two, and a record that was
dead-lettered on Monday and reprocessed successfully on Tuesday is counted once, as
upserted, with a resolved dead-letter row beside it. The consequence to state plainly: this
is a picture of the current world, not an event log of every attempt ever made.

**The time series comes from ``audit_log``, not from ``sync_records``.** The audit log is
append-only and records every create and update as a separate row with its own timestamp,
so it can answer "what happened between 09:00 and 10:00". ``sync_records`` holds only the
LAST write per key, so a series built from it would silently relocate a record's history to
its most recent write. The honest limitation of using the audit log: an idempotent no-op
writes no audit row by design, so this series measures WRITES, not reads. A quiet hour on
the chart means nothing changed, not that nothing was polled.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .crm_store import CrmStore

# Dead-letter categories the engine writes. Anything starting with "poison" failed the
# validation boundary; "transient_exhausted" passed validation and failed on the write.
POISON_PREFIX = "poison"
CATEGORY_TRANSIENT = "transient_exhausted"

BUCKET_HOUR = "hour"
BUCKET_DAY = "day"

# Age bands for the dead-letter queue. A queue that is deep is one problem; a queue that is
# OLD is a different and usually worse one, because it means nobody is draining it.
AGE_BANDS: tuple[tuple[str, float], ...] = (
    ("under 1h", 1.0),
    ("1h to 24h", 24.0),
    ("1d to 7d", 168.0),
    ("over 7d", float("inf")),
)


# -- pure time helpers --------------------------------------------------------


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string to an aware UTC datetime, or return None.

    Returns None rather than raising, because these values come from a database that a
    human may have edited, and one unparseable row should degrade a dashboard tile rather
    than take the whole page down.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def age_hours(then: str | None, as_of: datetime) -> float | None:
    """Hours between ``then`` and ``as_of``. Never negative: a future stamp reads as 0."""
    parsed = parse_iso(then)
    if parsed is None:
        return None
    return max(0.0, (as_of - parsed).total_seconds() / 3600.0)


def age_band(hours: float | None) -> str:
    if hours is None:
        return "unknown"
    for label, ceiling in AGE_BANDS:
        if hours < ceiling:
            return label
    return AGE_BANDS[-1][0]


def bucket_key(value: str | None, bucket: str = BUCKET_HOUR) -> str | None:
    """Floor a timestamp to the start of its hour or day, as an ISO string."""
    parsed = parse_iso(value)
    if parsed is None:
        return None
    if bucket == BUCKET_DAY:
        floored = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
    elif bucket == BUCKET_HOUR:
        floored = parsed.replace(minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"unknown bucket {bucket!r}, expected {BUCKET_HOUR!r} or {BUCKET_DAY!r}")
    return floored.isoformat()


def bucket_range(start: str, end: str, bucket: str = BUCKET_HOUR) -> list[str]:
    """Every bucket key from ``start`` to ``end`` inclusive, with no gaps.

    Gap filling matters on an ops chart. A line drawn only through the buckets that have
    data implies steady activity across a window where the sync was in fact stopped.
    """
    first, last = parse_iso(start), parse_iso(end)
    if first is None or last is None or first > last:
        return []
    step = timedelta(days=1) if bucket == BUCKET_DAY else timedelta(hours=1)
    cursor = parse_iso(bucket_key(start, bucket))
    stop = parse_iso(bucket_key(end, bucket))
    keys: list[str] = []
    assert cursor is not None and stop is not None
    while cursor <= stop:
        keys.append(cursor.isoformat())
        cursor += step
    return keys


# -- the funnel ---------------------------------------------------------------


@dataclass(frozen=True)
class Funnel:
    """Where records ended up, reconstructed from the store's terminal state.

    ``ingested`` is every distinct ``(source, natural_key)`` the engine reached a decision
    about. ``validated`` removes the ones that failed the validation boundary and never
    recovered. ``upserted`` is what is actually in the ledger. ``dead_lettered`` is the
    queue depth right now, which is why it is not simply ``ingested - upserted``: a resolved
    dead-letter row is history, not backlog.
    """

    ingested: int
    validated: int
    upserted: int
    dead_lettered_open: int
    failed_validation: int
    failed_write: int

    @property
    def stages(self) -> list[tuple[str, int]]:
        return [
            ("ingested", self.ingested),
            ("validated", self.validated),
            ("upserted", self.upserted),
            ("dead-lettered (open)", self.dead_lettered_open),
        ]

    @property
    def upsert_rate(self) -> float | None:
        """Share of ingested records that made it into the ledger. None on an empty store."""
        return None if self.ingested == 0 else self.upserted / self.ingested


def funnel(store: CrmStore) -> Funnel:
    ingested = _scalar(
        store,
        "SELECT COUNT(*) FROM ("
        "  SELECT source, natural_key FROM sync_records"
        "  UNION"
        "  SELECT source, natural_key FROM dead_letter"
        ")",
    )
    upserted = _scalar(store, "SELECT COUNT(*) FROM sync_records")
    # Only dead-letter rows with no counterpart in the ledger are still lost. A record that
    # was dead-lettered and later reprocessed is present in both tables and has recovered.
    unrecovered = (
        "SELECT d.category FROM dead_letter d "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM sync_records s "
        "  WHERE s.source = d.source AND s.natural_key = d.natural_key"
        ")"
    )
    failed_validation = _scalar(
        store, f"SELECT COUNT(*) FROM ({unrecovered}) WHERE category LIKE '{POISON_PREFIX}%'"
    )
    failed_write = _scalar(
        store, f"SELECT COUNT(*) FROM ({unrecovered}) WHERE category = '{CATEGORY_TRANSIENT}'"
    )
    open_dlq = _scalar(store, "SELECT COUNT(*) FROM dead_letter WHERE resolved = 0")
    return Funnel(
        ingested=ingested,
        validated=ingested - failed_validation,
        upserted=upserted,
        dead_lettered_open=open_dlq,
        failed_validation=failed_validation,
        failed_write=failed_write,
    )


# -- per-source throughput ----------------------------------------------------


@dataclass(frozen=True)
class SourceThroughput:
    source: str
    records: int
    created: int
    updated: int
    conflicts: int
    drift_notes: int
    dead_lettered_open: int
    last_write_at: str | None
    last_write_age_hours: float | None


def throughput_by_source(store: CrmStore, as_of: datetime | None = None) -> list[SourceThroughput]:
    """One row per source that has ever been seen, ordered by name for stable rendering."""
    as_of = as_of or datetime.now(UTC)
    sources = sorted(
        {
            str(row["source"])
            for row in store.conn.execute(
                "SELECT source FROM sync_records "
                "UNION SELECT source FROM dead_letter "
                "UNION SELECT source FROM sync_state"
            ).fetchall()
        }
    )
    out: list[SourceThroughput] = []
    for source in sources:
        actions = {
            str(row["action"]): int(row["n"])
            for row in store.conn.execute(
                "SELECT action, COUNT(*) AS n FROM audit_log WHERE source = ? GROUP BY action",
                (source,),
            ).fetchall()
        }
        last_write = store.conn.execute(
            "SELECT MAX(at) AS at FROM audit_log WHERE source = ? "
            "AND action IN ('created', 'updated')",
            (source,),
        ).fetchone()["at"]
        out.append(
            SourceThroughput(
                source=source,
                records=_scalar(
                    store, "SELECT COUNT(*) FROM sync_records WHERE source = ?", (source,)
                ),
                created=actions.get("created", 0),
                updated=actions.get("updated", 0),
                conflicts=actions.get("conflict_ignored", 0),
                drift_notes=actions.get("schema_drift", 0),
                dead_lettered_open=_scalar(
                    store,
                    "SELECT COUNT(*) FROM dead_letter WHERE source = ? AND resolved = 0",
                    (source,),
                ),
                last_write_at=last_write,
                last_write_age_hours=age_hours(last_write, as_of),
            )
        )
    return out


# -- writes over time ---------------------------------------------------------


@dataclass(frozen=True)
class WriteBucket:
    bucket: str
    source: str
    created: int
    updated: int

    @property
    def total(self) -> int:
        return self.created + self.updated


def writes_over_time(
    store: CrmStore, *, bucket: str = BUCKET_HOUR, fill_gaps: bool = True
) -> list[WriteBucket]:
    """Creates and updates per time bucket per source, from the append-only audit log.

    Only ``created`` and ``updated`` count. An idempotent no-op writes no audit row, so a
    flat stretch on this series means nothing CHANGED, not that nothing was polled. That
    distinction is the one a reader of this chart is most likely to get wrong.
    """
    rows = store.conn.execute(
        "SELECT at, source, action FROM audit_log "
        "WHERE action IN ('created', 'updated') ORDER BY at"
    ).fetchall()
    if not rows:
        return []

    counts: dict[tuple[str, str], dict[str, int]] = {}
    for row in rows:
        key = bucket_key(row["at"], bucket)
        if key is None:
            continue
        entry = counts.setdefault((key, str(row["source"])), {"created": 0, "updated": 0})
        entry[str(row["action"])] += 1

    sources = sorted({source for _bucket, source in counts})
    if fill_gaps:
        keys = bucket_range(str(rows[0]["at"]), str(rows[-1]["at"]), bucket)
    else:
        keys = sorted({b for b, _source in counts})
    return [
        WriteBucket(
            bucket=key,
            source=source,
            created=counts.get((key, source), {}).get("created", 0),
            updated=counts.get((key, source), {}).get("updated", 0),
        )
        for key in keys
        for source in sources
    ]


# -- the dead-letter queue ----------------------------------------------------


@dataclass(frozen=True)
class DeadLetterRow:
    source: str
    natural_key: str
    category: str
    error: str
    failure_count: int
    first_seen_at: str
    age_hours: float | None
    band: str


@dataclass(frozen=True)
class DeadLetterReport:
    depth: int
    rows: list[DeadLetterRow]
    by_band: dict[str, int]
    by_source: dict[str, int]
    oldest_hours: float | None


def dead_letter_report(store: CrmStore, as_of: datetime | None = None) -> DeadLetterReport:
    """The open queue, oldest first, because age is what turns a backlog into an incident."""
    as_of = as_of or datetime.now(UTC)
    rows: list[DeadLetterRow] = []
    for row in store.list_dead_letter(include_resolved=False):
        hours = age_hours(row["first_seen_at"], as_of)
        rows.append(
            DeadLetterRow(
                source=str(row["source"]),
                natural_key=str(row["natural_key"]),
                category=str(row["category"]),
                error=str(row["error"]),
                failure_count=int(row["failure_count"]),
                first_seen_at=str(row["first_seen_at"]),
                age_hours=hours,
                band=age_band(hours),
            )
        )
    rows.sort(key=lambda r: (-(r.age_hours or 0.0), r.source, r.natural_key))

    by_band: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for row in rows:
        by_band[row.band] = by_band.get(row.band, 0) + 1
        by_source[row.source] = by_source.get(row.source, 0) + 1
    ages = [r.age_hours for r in rows if r.age_hours is not None]
    return DeadLetterReport(
        depth=len(rows),
        rows=rows,
        by_band=by_band,
        by_source=by_source,
        oldest_hours=max(ages) if ages else None,
    )


# -- cursor lag ---------------------------------------------------------------


@dataclass(frozen=True)
class CursorLag:
    source: str
    cursor: str | None
    updated_at: str
    lag_hours: float | None
    staleness_hours: float | None


def cursor_lag(store: CrmStore, as_of: datetime | None = None) -> list[CursorLag]:
    """Two different lags per source, and conflating them is the classic misread.

    ``lag_hours`` is how far the watermark itself is behind now, which is a statement about
    the DATA: a source whose newest record is three days old has a three-day lag even if the
    sync ran a second ago. ``staleness_hours`` is how long since the cursor row was last
    touched, which is a statement about the JOB: it grows when the sync stops running.

    A quiet source shows high lag and low staleness. A wedged worker shows high staleness.
    """
    as_of = as_of or datetime.now(UTC)
    rows = store.conn.execute(
        "SELECT source, cursor, updated_at FROM sync_state ORDER BY source"
    ).fetchall()
    return [
        CursorLag(
            source=str(row["source"]),
            cursor=row["cursor"],
            updated_at=str(row["updated_at"]),
            lag_hours=age_hours(row["cursor"], as_of),
            staleness_hours=age_hours(row["updated_at"], as_of),
        )
        for row in rows
    ]


# -- deal pipeline (the CRM side of the dashboard) ----------------------------


@dataclass(frozen=True)
class DealSummary:
    stage: str
    deals: int
    amount: str


def deals_by_stage(store: CrmStore) -> list[DealSummary]:
    """Deal count and exact summed amount per stage.

    The sum is done in Decimal, not float, and rendered back as a string. A pipeline total
    that has drifted by a cent is a total nobody trusts again.
    """
    from decimal import Decimal, InvalidOperation

    totals: dict[str, tuple[int, Decimal]] = {}
    for row in store.all_deals():
        stage = str(row["stage"]) or "unset"
        count, amount = totals.get(stage, (0, Decimal("0")))
        try:
            value = Decimal(str(row["amount"]) or "0")
        except InvalidOperation:
            value = Decimal("0")
        totals[stage] = (count + 1, amount + value)
    return [
        DealSummary(stage=stage, deals=count, amount=f"{amount:.2f}")
        for stage, (count, amount) in sorted(totals.items())
    ]


# -- internals ----------------------------------------------------------------


def _scalar(store: CrmStore, sql: str, params: tuple = ()) -> int:
    row: sqlite3.Row | None = store.conn.execute(sql, params).fetchone()
    return 0 if row is None else int(row[0])
