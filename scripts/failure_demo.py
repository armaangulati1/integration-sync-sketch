"""The failure-mode demo.

Injects, in one run, every failure the engine is built to survive, and shows the handling
with clear log output:

  * duplicates       -> the second copy is a no-op ('unchanged')
  * out-of-order     -> newer content wins; the stale copy is 'conflict_ignored' with audit
  * malformed        -> poison records go straight to the dead-letter queue
  * transient errors -> a flaky write is retried with backoff, then succeeds
  * permanent failure-> a write that never succeeds is dead-lettered after the retry budget
  * reprocessing     -> after the flake is 'fixed', the dead-letter queue is drained

Sleeps are stubbed so the demo runs instantly; the backoff delays are still logged.

Run:  .venv/bin/python scripts/failure_demo.py
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from integration_sync.crm_store import CrmStore  # noqa: E402
from integration_sync.errors import TransientError  # noqa: E402
from integration_sync.logging_config import configure_logging  # noqa: E402
from integration_sync.models import (  # noqa: E402
    SOURCE_CALENDAR,
    SOURCE_EMAIL,
    CanonicalRecord,
    RawRecord,
)
from integration_sync.pipeline import dead_letter_rebuilders  # noqa: E402
from integration_sync.sync_engine import SyncEngine  # noqa: E402

T0 = "2026-04-01T09:00:00+00:00"
T1 = "2026-04-01T10:00:00+00:00"
T2 = "2026-04-01T11:00:00+00:00"


def cal_payload(subject: str, occurred: str, email: str, name: str, account: str) -> dict:
    return {
        "kind": "meeting", "occurred_at": occurred, "subject": subject, "body": "SYNTHETIC",
        "contact_email": email, "contact_name": name,
        "account_name": account, "account_domain": email.split("@", 1)[1] if "@" in email else "",
    }


class FaultInjector:
    """A downstream that fails a scheduled number of times per natural key, then succeeds."""

    def __init__(self, schedule: dict[str, int]) -> None:
        self.remaining = dict(schedule)

    def __call__(self, record: CanonicalRecord) -> None:
        left = self.remaining.get(record.natural_key, 0)
        if left > 0:
            self.remaining[record.natural_key] = left - 1
            raise TransientError(f"simulated flaky write for {record.natural_key}")


def calendar_reader(_since: str | None) -> Iterable[RawRecord]:
    good = cal_payload("SYNTHETIC kickoff", T0,
                       "dana.rivers@northgate-health.example", "Dana Rivers", "Northgate Health")
    flaky = cal_payload("SYNTHETIC flaky write", T0,
                        "sam.okafor@lakeside-labs.example", "Sam Okafor", "Lakeside Labs")
    poison_hard = cal_payload("SYNTHETIC permanent fail", T0,
                             "priya.nadel@riverbend-clinic.example", "Priya Nadel", "Riverbend")
    return [
        # a clean record
        RawRecord(SOURCE_CALENDAR, "evt-good@sketch", good),
        # an exact duplicate of it -> should be unchanged
        RawRecord(SOURCE_CALENDAR, "evt-good@sketch", good),
        # newer content, then older content of the same key -> update then conflict_ignored
        RawRecord(SOURCE_CALENDAR, "evt-conf@sketch",
                  cal_payload("SYNTHETIC v2 (newer)", T2,
                              "dana.rivers@northgate-health.example", "Dana Rivers", "Northgate")),
        RawRecord(SOURCE_CALENDAR, "evt-conf@sketch",
                  cal_payload("SYNTHETIC v1 (older)", T1,
                              "dana.rivers@northgate-health.example", "Dana Rivers", "Northgate")),
        # a record whose write flakes twice, then succeeds
        RawRecord(SOURCE_CALENDAR, "evt-flaky@sketch", flaky),
        # a record whose write never succeeds -> dead-letter after retries
        RawRecord(SOURCE_CALENDAR, "evt-poison-write@sketch", poison_hard),
        # malformed: missing organizer email (invalid contact) -> poison
        RawRecord(SOURCE_CALENDAR, "evt-nomail@sketch",
                  cal_payload("SYNTHETIC no email", T0, "not-an-email", "Nobody", "")),
        # malformed: missing natural key -> poison
        RawRecord(SOURCE_CALENDAR, None,
                  cal_payload("SYNTHETIC no uid", T0,
                              "x@lakeside-labs.example", "X", "Lakeside Labs")),
    ]


def email_reader(_since: str | None) -> Iterable[RawRecord]:
    ok = {
        "kind": "email", "occurred_at": T0, "subject": "SYNTHETIC hi", "body": "SYNTHETIC",
        "contact_email": "sam.okafor@lakeside-labs.example", "contact_name": "Sam Okafor",
        "account_name": "Lakeside Labs", "account_domain": "lakeside-labs.example",
    }
    bad_date = dict(ok, occurred_at="not-a-date")
    return [
        RawRecord(SOURCE_EMAIL, "<msg-ok@sketch>", ok),
        # malformed: unparseable date -> poison
        RawRecord(SOURCE_EMAIL, "<msg-baddate@sketch>", bad_date),
    ]


def main() -> None:
    configure_logging()
    store = CrmStore(":memory:")
    store.init_schema()

    # evt-flaky fails twice then succeeds (retried); evt-poison-write always fails (dead-letter).
    fault = FaultInjector({"evt-flaky@sketch": 2, "evt-poison-write@sketch": 99})
    engine = SyncEngine(
        store, attempts=3, base_delay=0.25, factor=2.0,
        sleep=lambda _d: None,  # do not actually sleep during the demo
        fault=fault,
    )

    print("\n########## FAILURE-MODE DEMO ##########")
    print("\n--- calendar source ---")
    cal = engine.sync_source(SOURCE_CALENDAR, calendar_reader)
    print("\n--- email source ---")
    eml = engine.sync_source(SOURCE_EMAIL, email_reader)

    print("\n=== per-source outcome ===")
    for s in (cal, eml):
        print(f"  {s.source:<9} created={s.created} unchanged={s.unchanged} "
              f"updated={s.updated} conflict_ignored={s.conflict_ignored} "
              f"retries={s.retries} dead_lettered={s.dead_lettered}")

    print("\n=== dead-letter queue ===")
    for row in store.list_dead_letter():
        print(f"  [{row['category']:<18}] {row['source']}/{row['natural_key']} "
              f"(failures={row['failure_count']}): {row['error']}")

    print("\n=== conflict audit trail for evt-conf@sketch ===")
    for a in store.audit_for(SOURCE_CALENDAR, "evt-conf@sketch"):
        print(f"  {a['action']:<16} {a['detail']}")

    # Now 'fix' the transient dependency and drain the queue. evt-poison-write becomes healthy;
    # the two malformed records stay poison and remain queued (correctly).
    print("\n--- reprocessing dead-letter queue (transient dependency now healthy) ---")
    fault.remaining["evt-poison-write@sketch"] = 0
    resolved = engine.reprocess_dead_letter(dead_letter_rebuilders())
    print(f"  resolved {resolved} row(s)")

    print("\n=== dead-letter queue after reprocessing ===")
    remaining = store.list_dead_letter()
    for row in remaining:
        print(f"  [{row['category']:<18}] {row['source']}/{row['natural_key']} still queued")

    print("\n=== final store ===")
    snap = store.snapshot()
    print(f"  {snap}")

    # Assertions so the demo self-checks.
    assert cal.unchanged == 1, cal.unchanged                 # the duplicate
    assert cal.created == 3, cal.created                     # good + conf(newer) + flaky
    assert cal.conflict_ignored == 1, cal.conflict_ignored   # conf(older)
    # evt-flaky backs off twice then succeeds (2); evt-poison-write backs off twice then
    # exhausts its 3-attempt budget and is dead-lettered (2). Total backoffs = 4.
    assert cal.retries == 4, cal.retries
    assert cal.dead_lettered == 3, cal.dead_lettered         # 2 poison + 1 exhausted write
    assert eml.dead_lettered == 1, eml.dead_lettered         # bad date
    assert resolved == 1, resolved                           # only the write-failure recovers
    assert len(remaining) == 3, len(remaining)               # 3 genuine poison rows remain
    store.close()
    print("\nOK: every failure mode handled as designed.")


if __name__ == "__main__":
    main()
