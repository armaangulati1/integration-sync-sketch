"""An operational dashboard over the sync store.

Deliberately thin. Every number on this page is computed by a function in
``integration_sync.ops_metrics``, which is tested without a browser, so this file is layout
and nothing else. If a tile here is wrong, the bug is in the metrics module and there is a
test that should have caught it.

What it answers, in the order an on-call person actually asks:

1. Is anything stuck?          dead-letter depth, and how OLD the oldest row is
2. Is the job still running?   cursor staleness, which is not the same as data lag
3. What did it do?             writes per hour per source
4. Where do records die?       the ingested -> validated -> upserted -> dead-lettered funnel
5. What is in the CRM?         deals by stage, summed exactly

Run:
    .venv/bin/streamlit run dashboard/app.py

It reads the database written by the demos. Point it somewhere else with SYNC_DB:
    SYNC_DB=data/other.db .venv/bin/streamlit run dashboard/app.py

Everything it displays is synthetic, self-authored demo data.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import streamlit as st

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from integration_sync import ops_metrics as om  # noqa: E402
from integration_sync.crm_store import CrmStore  # noqa: E402

DEFAULT_DB = REPO / "data" / "hubspot_demo.db"

# Above this, the dead-letter queue stops being a queue and starts being an incident. It is
# a demo threshold on a demo dataset, not a calibrated SLO, and the caption says so.
DLQ_ALERT_DEPTH = 5
DLQ_ALERT_AGE_HOURS = 24.0


def resolve_db_path() -> Path:
    return Path(os.environ.get("SYNC_DB") or DEFAULT_DB)


def fmt_hours(hours: float | None) -> str:
    if hours is None:
        return "unknown"
    if hours < 1:
        return f"{hours * 60:.0f} min"
    if hours < 48:
        return f"{hours:.1f} h"
    return f"{hours / 24:.1f} d"


def main() -> None:
    st.set_page_config(page_title="Sync operations", layout="wide")
    st.title("Sync operations")
    st.caption(
        "Synthetic, self-authored demo data. No real CRM, calendar, mailbox, or ticket "
        "system is connected."
    )

    db_path = resolve_db_path()
    if not db_path.exists():
        st.error(f"No sync store at {db_path}.")
        st.markdown(
            "Create one first:\n\n"
            "```bash\n"
            ".venv/bin/python scripts/hubspot_demo.py\n"
            "```"
        )
        return

    store = CrmStore(db_path)
    store.init_schema()
    as_of = datetime.now(UTC)

    dlq = om.dead_letter_report(store, as_of)
    counts = om.funnel(store)
    sources = om.throughput_by_source(store, as_of)
    lags = om.cursor_lag(store, as_of)

    # 1. Is anything stuck?
    top = st.columns(4)
    top[0].metric("Records in the store", counts.upserted)
    top[1].metric("Dead-letter depth", dlq.depth)
    top[2].metric("Oldest queued item", fmt_hours(dlq.oldest_hours))
    top[3].metric(
        "Upsert rate", "n/a" if counts.upsert_rate is None else f"{counts.upsert_rate:.1%}"
    )

    if dlq.depth >= DLQ_ALERT_DEPTH or (dlq.oldest_hours or 0) >= DLQ_ALERT_AGE_HOURS:
        st.warning(
            f"Dead-letter queue is {dlq.depth} deep, oldest {fmt_hours(dlq.oldest_hours)}. "
            f"Demo thresholds ({DLQ_ALERT_DEPTH} rows or {DLQ_ALERT_AGE_HOURS:.0f}h), "
            "not a calibrated SLO."
        )

    # 2. Is the job still running?
    st.subheader("Cursors")
    st.caption(
        "Data lag is how far behind the newest synced record is. Job staleness is how long "
        "since the cursor was touched. A quiet source shows high lag and low staleness. A "
        "wedged worker shows high staleness."
    )
    st.dataframe(
        [
            {
                "source": row.source,
                "cursor": row.cursor or "never set",
                "data lag": fmt_hours(row.lag_hours),
                "job staleness": fmt_hours(row.staleness_hours),
            }
            for row in lags
        ],
        width="stretch",
        hide_index=True,
    )

    # 3. What did it do?
    st.subheader("Writes over time")
    st.caption(
        "Creates and updates per hour, from the append-only audit log. An idempotent no-op "
        "writes no audit row, so a flat stretch means nothing changed, not that nothing was "
        "polled."
    )
    series = om.writes_over_time(store, bucket=om.BUCKET_HOUR)
    if series:
        buckets = sorted({b.bucket for b in series})
        names = sorted({b.source for b in series})
        chart = {
            name: [
                next((b.total for b in series if b.bucket == key and b.source == name), 0)
                for key in buckets
            ]
            for name in names
        }
        st.bar_chart({"hour": buckets, **chart}, x="hour", width="stretch")
    else:
        st.info("No writes recorded yet.")

    # 4. Per-source throughput.
    st.subheader("Per-source throughput")
    st.dataframe(
        [
            {
                "source": row.source,
                "records": row.records,
                "created": row.created,
                "updated": row.updated,
                "conflicts ignored": row.conflicts,
                "drift notes": row.drift_notes,
                "dead-lettered": row.dead_lettered_open,
                "last write": fmt_hours(row.last_write_age_hours) + " ago"
                if row.last_write_age_hours is not None
                else "never",
            }
            for row in sources
        ],
        width="stretch",
        hide_index=True,
    )

    # 5. Where do records die?
    st.subheader("Funnel")
    st.caption(
        "Reconstructed from the store's terminal state, not counted at read time. A record "
        "dead-lettered on one run and reprocessed on the next is counted once, as upserted."
    )
    funnel_columns = st.columns(4)
    previous: int | None = None
    for column, (label, value) in zip(funnel_columns, counts.stages, strict=True):
        delta = None if previous is None or label.startswith("dead") else value - previous
        column.metric(label, value, delta=delta, delta_color="inverse")
        previous = value
    st.caption(
        f"Failed validation: {counts.failed_validation}. Failed write after retries: "
        f"{counts.failed_write}. Those need different people: one is a data problem, the "
        "other is a dependency problem."
    )

    # 6. The queue itself.
    st.subheader("Dead-letter queue")
    if dlq.rows:
        st.dataframe(
            [
                {
                    "source": row.source,
                    "key": row.natural_key,
                    "category": row.category,
                    "attempts": row.failure_count,
                    "age": fmt_hours(row.age_hours),
                    "band": row.band,
                    "error": row.error,
                }
                for row in dlq.rows
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.success("Queue is empty.")

    # 7. The CRM side.
    st.subheader("Deals by stage")
    st.caption("Amounts summed in Decimal, never float, and rendered exactly.")
    deals = om.deals_by_stage(store)
    if deals:
        st.dataframe(
            [{"stage": d.stage, "deals": d.deals, "amount": d.amount} for d in deals],
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No deals synced. Run scripts/hubspot_demo.py.")

    store.close()


main()
