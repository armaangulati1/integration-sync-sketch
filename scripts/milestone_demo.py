"""The ticketing and milestone demo.

Runs the whole layer against the synthetic project, with both integration hazards live:

  * the ticket endpoint is RATE LIMITED and refuses calls past its quota, so the client has
    to back off and wait the window out to see the whole board,
  * and it starts DRIFTING at page two (a renamed due date, a status that becomes an object,
    an unrecognized field), so the connector has to resolve the new shape without losing
    data and without mistaking a rename for an edit.

Then it builds the project plan, propagates slip along the dependency edges, runs the
escalation rules, and writes the report to data/milestone_report.txt.

The clock is fake and the sleeps are fake, so a demo that "waits out" a 60 second quota
window finishes instantly. The waiting is real to the rate limiter, which is the part that
matters: the bucket only drains because the clock moved.

Run:  .venv/bin/python scripts/milestone_demo.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from integration_sync import report  # noqa: E402
from integration_sync.crm_store import CrmStore  # noqa: E402
from integration_sync.escalation import (  # noqa: E402
    evaluate,
    record,
    tickets_by_milestone,
)
from integration_sync.logging_config import configure_logging  # noqa: E402
from integration_sync.milestones import build_plan, load_milestones  # noqa: E402
from integration_sync.models import SOURCE_TICKETS  # noqa: E402
from integration_sync.sync_engine import SyncEngine  # noqa: E402
from integration_sync.ticket_source import (  # noqa: E402
    SyntheticTicketApi,
    load_ticket_export,
    read_tickets,
    ticket_projector,
)
from scripts import generate_synthetic_data  # noqa: E402

DATA = REPO / "data"
REPORT_PATH = DATA / "milestone_report.txt"

# The date the report is computed for. Fixed so the demo is reproducible.
AS_OF = date(2026, 4, 20)


class FakeClock:
    """A clock the fake sleep moves. The rate limiter reads it, so waits are load-bearing."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def drift_the_payload(issue: dict) -> dict:
    """The schema change the source rolls out mid-stream."""
    fields = dict(issue["fields"])
    fields["due_date"] = fields.pop("duedate")          # renamed
    fields["status"] = {"name": fields["status"]}       # scalar became an object
    fields["request_type"] = "implementation"           # a field we have never seen
    return {**issue, "fields": fields}


def main() -> None:
    configure_logging()
    generate_synthetic_data.main()

    store = CrmStore(":memory:")
    store.init_schema()

    clock = FakeClock()
    api = SyntheticTicketApi(
        load_ticket_export(str(DATA / "tickets.json")),
        page_size=2,
        limit=2,              # two calls per window
        window=60.0,          # a one minute quota window
        clock=clock,
        drift_fn=drift_the_payload,
        drift_from_page=1,    # page one is the old shape, everything after is the new one
    )
    engine = SyncEngine(
        store, base_delay=0.0, sleep=lambda _d: None,
        projectors={SOURCE_TICKETS: ticket_projector},
    )

    print("\n########## TICKETING + MILESTONE DEMO ##########")
    print("\n--- syncing the ticket board (rate limited, drifting at page 2) ---")
    stats = engine.sync_source(
        SOURCE_TICKETS, lambda since: read_tickets(api, since, sleep=clock.sleep)
    )
    print(
        f"  created={stats.created} unchanged={stats.unchanged} updated={stats.updated} "
        f"dead_lettered={stats.dead_lettered} cursor->{stats.cursor_after}"
    )
    print(f"  endpoint: {api.calls} calls served, {api.refusals} throttled")
    print(f"  client waited out {sum(clock.slept):.0f}s of quota across "
          f"{len(clock.slept)} backoff(s)")

    drift_rows = [r for r in store.conn.execute(
        "SELECT natural_key, detail FROM audit_log WHERE action = 'schema_drift' "
        "ORDER BY natural_key, detail"
    )]
    print(f"\n--- schema drift observed on {len({r['natural_key'] for r in drift_rows})} "
          f"ticket(s), {len(drift_rows)} note(s) ---")
    for line in sorted({r["detail"] for r in drift_rows}):
        print(f"  {line}")

    print("\n--- re-syncing the SAME drifted board (idempotency check) ---")
    store.set_cursor(SOURCE_TICKETS, None)
    again = engine.sync_source(
        SOURCE_TICKETS, lambda since: read_tickets(api, since, sleep=clock.sleep)
    )
    print(f"  created={again.created} unchanged={again.unchanged} updated={again.updated}")

    print("\n--- loading the project plan ---")
    loaded = load_milestones(store, str(DATA / "milestones.json"))
    print(f"  {loaded} milestones")

    plan = build_plan(store, AS_OF)
    grouped = tickets_by_milestone(store)
    escalations = evaluate(plan, grouped, as_of=AS_OF)
    new_rows = record(store, escalations, as_of=AS_OF)
    repeat_rows = record(store, escalations, as_of=AS_OF)

    counts = {
        key: (sum(1 for t in rows if t["is_open"]), len(rows))
        for key, rows in grouped.items()
    }
    text = report.render(plan, escalations, as_of=AS_OF, ticket_counts=counts)
    REPORT_PATH.write_text(text, encoding="utf-8")
    print("\n" + text)
    print(f"report written to {REPORT_PATH.relative_to(REPO)}")

    # Self-checks, so a zero exit code means something.
    assert stats.created == 6, stats.created
    assert stats.dead_lettered == 0, stats.dead_lettered
    assert api.refusals >= 1, "the quota was never actually hit"
    assert drift_rows, "the drifted pages produced no drift notes"
    assert again.updated == 0, "a re-sync of drifted data rewrote rows"
    assert again.unchanged == 6, again.unchanged
    assert new_rows == len(escalations), (new_rows, len(escalations))
    assert repeat_rows == 0, "the same escalation was recorded twice for one date"
    store.close()
    print("\nOK: throttled read, drift absorbed, plan projected, escalations raised.")


if __name__ == "__main__":
    main()
