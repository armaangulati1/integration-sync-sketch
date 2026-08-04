"""The HubSpot connector demo, and the seeder for the ops dashboard.

Two modes, one code path.

Recorded (the default, and what CI runs)
    The client is pointed at the cassettes in ``fixtures/hubspot/``. Contacts page three
    deep with a 429 in the middle, one contact has no email and is dead-lettered, and one
    deal points at a contact that was never synced and is dead-lettered too. Then the whole
    contact sync is re-run to prove it changes nothing.

Live (``--live``, only after a human has created a sandbox)
    The client is pointed at the real API with a private app token from the environment.
    Nothing else changes: same client, same readers, same engine, same assertions about
    idempotency. See RUNBOOK_HUBSPOT.md. Live mode refuses to start without a token rather
    than silently falling back to fixtures and reporting a sync that never happened.

Either way it writes data/hubspot_demo.db, which is what the dashboard reads.

Run:  .venv/bin/python scripts/hubspot_demo.py
      .venv/bin/python scripts/hubspot_demo.py --live
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from integration_sync import ops_metrics as om  # noqa: E402
from integration_sync.crm_store import CrmStore  # noqa: E402
from integration_sync.hubspot_source import (  # noqa: E402
    build_client,
    contact_email_resolver,
    deal_projector,
    read_hubspot_contacts,
    read_hubspot_deals,
)
from integration_sync.logging_config import configure_logging  # noqa: E402
from integration_sync.models import (  # noqa: E402
    SOURCE_HUBSPOT_CONTACTS,
    SOURCE_HUBSPOT_DEALS,
)
from integration_sync.pipeline import build_readers, sync_all  # noqa: E402
from integration_sync.sync_engine import SyncEngine  # noqa: E402
from scripts import generate_synthetic_data  # noqa: E402

DATA = REPO / "data"
FIXTURES = REPO / "fixtures" / "hubspot"
DB_PATH = DATA / "hubspot_demo.db"
EVIDENCE = REPO / "evidence"

# The cassettes are written for a two-record page so pagination is exercised with a handful
# of records. A live run has no such constraint and uses the documented maximum.
RECORDED_PAGE_SIZE = 2
LIVE_PAGE_SIZE = 100


class FakeSleep:
    """Records the waits a throttled run asked for, without spending them."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


def real_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="talk to the real HubSpot API using HUBSPOT_PRIVATE_APP_TOKEN",
    )
    args = parser.parse_args(argv)
    configure_logging()

    sleeper = real_sleep if args.live else FakeSleep()
    page_size = LIVE_PAGE_SIZE if args.live else RECORDED_PAGE_SIZE

    DATA.mkdir(exist_ok=True)
    DB_PATH.unlink(missing_ok=True)
    store = CrmStore(DB_PATH)
    store.init_schema()
    engine = SyncEngine(
        store,
        base_delay=0.0,
        sleep=lambda _d: None,
        projectors={SOURCE_HUBSPOT_DEALS: deal_projector},
    )

    mode = "LIVE" if args.live else "RECORDED"
    print(f"\n########## HUBSPOT CONNECTOR DEMO ({mode}) ##########")

    # The three file sources are synced first so the dashboard has more than one surface on
    # it. They are unrelated to HubSpot and are only here to make the ops view realistic.
    generate_synthetic_data.main()
    file_stats = sync_all(engine, build_readers(DATA))
    print("\n--- baseline: the three file sources ---")
    for stat in file_stats:
        print(f"  {stat.source}: created={stat.created} dead_lettered={stat.dead_lettered}")

    print("\n--- syncing HubSpot contacts ---")
    contacts_client = build_client(
        live=args.live, fixtures_dir=FIXTURES, sleep=sleeper,
        cassettes=("contacts",), page_size=page_size,
    )
    contact_stats = engine.sync_source(
        SOURCE_HUBSPOT_CONTACTS,
        lambda since: read_hubspot_contacts(contacts_client, since),
    )
    print(
        f"  created={contact_stats.created} unchanged={contact_stats.unchanged} "
        f"updated={contact_stats.updated} dead_lettered={contact_stats.dead_lettered} "
        f"cursor->{contact_stats.cursor_after}"
    )
    print(
        f"  client: {contacts_client.pages_fetched} page(s) fetched, "
        f"{contacts_client.throttles} throttle(s) waited out"
    )
    # Same rule as the deal rows below: a natural key is a record id from the source, so
    # against a live sandbox it identifies somebody's record. Live mode gets a count.
    if contact_stats.dead_lettered:
        if args.live:
            print(f"  dead-lettered: {contact_stats.dead_lettered} record(s) "
                  f"(no email address, so no contact to attach to; keys withheld in live mode)")
        else:
            print(f"  dead-lettered: {', '.join(contact_stats.dead_letter_keys)} "
                  f"(no email address, so no contact to attach to)")

    print("\n--- syncing HubSpot deals (associations resolved against synced contacts) ---")
    deals_client = build_client(
        live=args.live, fixtures_dir=FIXTURES, sleep=sleeper,
        cassettes=("deals",), page_size=page_size,
    )
    resolver = contact_email_resolver(store)
    deal_stats = engine.sync_source(
        SOURCE_HUBSPOT_DEALS,
        lambda since: read_hubspot_deals(deals_client, since, resolve_contact_email=resolver),
    )
    print(
        f"  created={deal_stats.created} unchanged={deal_stats.unchanged} "
        f"dead_lettered={deal_stats.dead_lettered} cursor->{deal_stats.cursor_after}"
    )
    if deal_stats.dead_lettered:
        if args.live:
            print(f"  dead-lettered: {deal_stats.dead_lettered} record(s) (associated contact "
                  f"not present, so the deal has nobody to attach to; keys withheld in live mode)")
        else:
            print(f"  dead-lettered: {', '.join(deal_stats.dead_letter_keys)} "
                  f"(associated contact not present, so the deal has nobody to attach to)")
    # Record contents are printed for the CASSETTES only. Against a live sandbox the same
    # line would put deal names and contact addresses on a console whose output people
    # paste into issues and transcripts, and whatever is in a sandbox is still somebody's
    # data. Live mode gets counts. This matches what _write_live_evidence already promises.
    if args.live:
        print(f"  {len(store.all_deals())} deal row(s) projected (contents withheld in live mode)")
    else:
        for deal in store.all_deals():
            print(f"  deal {deal['deal_id']}: {deal['name']} | stage={deal['stage']} "
                  f"| amount={deal['amount']} | contact={deal['contact_email']}")

    print("\n--- re-syncing the SAME contacts (idempotency check) ---")
    store.set_cursor(SOURCE_HUBSPOT_CONTACTS, None)
    replay_client = build_client(
        live=args.live, fixtures_dir=FIXTURES, sleep=sleeper,
        cassettes=("contacts",), page_size=page_size,
    )
    again = engine.sync_source(
        SOURCE_HUBSPOT_CONTACTS,
        lambda since: read_hubspot_contacts(replay_client, since),
        advance_cursor=False,
    )
    print(f"  created={again.created} unchanged={again.unchanged} updated={again.updated}")

    print("\n--- what the dashboard will show ---")
    as_of = datetime.now(UTC)
    counts = om.funnel(store)
    for label, value in counts.stages:
        print(f"  {label:<22} {value}")
    print(f"  upsert rate            {counts.upsert_rate:.1%}")
    print("\n  per source:")
    for row in om.throughput_by_source(store, as_of):
        print(f"    {row.source:<18} records={row.records:<3} created={row.created:<3} "
              f"updated={row.updated:<3} dlq_open={row.dead_lettered_open}")
    dlq = om.dead_letter_report(store, as_of)
    print(f"\n  dead-letter depth: {dlq.depth}")
    # Same rule as the deal rows above: an error string can carry the payload that caused
    # it, so live mode reports the shape of the queue and not its contents.
    if args.live:
        for category, count in sorted(_count_by_category(dlq.rows).items()):
            print(f"    {category}: {count}")
    else:
        for row in dlq.rows:
            print(f"    {row.source}/{row.natural_key} [{row.category}] {row.error[:60]}")

    print(f"\nstore written to {DB_PATH.relative_to(REPO)}")
    print("view it with:  .venv/bin/streamlit run dashboard/app.py")

    if args.live:
        _write_live_evidence(store, contact_stats, deal_stats, contacts_client)
    else:
        _self_check(store, contact_stats, deal_stats, again, contacts_client, sleeper)

    store.close()
    print("\nOK: HubSpot contacts and deals synced, throttle survived, re-run changed nothing.")


def _count_by_category(rows) -> dict[str, int]:
    """Queue shape without queue contents, for the live path."""
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.category] = counts.get(row.category, 0) + 1
    return counts


def _self_check(store, contact_stats, deal_stats, again, client, sleeper) -> None:
    """Assertions that only hold against the cassettes, so a zero exit means something."""
    assert contact_stats.created == 4, contact_stats.created
    assert contact_stats.dead_lettered == 1, contact_stats.dead_lettered
    assert contact_stats.dead_letter_keys == ["704"], contact_stats.dead_letter_keys
    assert client.pages_fetched == 3, client.pages_fetched
    assert client.throttles == 1, "the cassette's 429 was never hit"
    # Two waits, not one: the idempotency replay reads a fresh cassette and is throttled
    # again. Every wait is the server's Retry-After of 10s, never the local 0.5s schedule.
    assert sleeper.slept == [10.0, 10.0], sleeper.slept
    assert deal_stats.created == 2, deal_stats.created
    assert deal_stats.dead_lettered == 1, deal_stats.dead_lettered
    assert again.created == 0 and again.updated == 0, "a re-sync rewrote unchanged rows"
    assert again.unchanged == 4, again.unchanged
    assert store.get_deal("8801")["amount"] == "48000.00", "the deal amount did not round-trip"
    assert store.get_deal("8803") is None, "a dead-lettered deal was projected anyway"
    counts = om.funnel(store)
    stages = [n for _label, n in counts.stages[:3]]
    assert stages == sorted(stages, reverse=True), stages


def _write_live_evidence(store, contact_stats, deal_stats, client) -> None:
    """Record what a live run actually did, for the evidence directory.

    Deliberately records counts and timings only. No contact names, no email addresses, and
    no token: whatever is in a sandbox is still somebody's data.
    """
    EVIDENCE.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    path = EVIDENCE / f"hubspot_live_run_{stamp}.txt"
    lines = [
        f"HubSpot LIVE run, {datetime.now(UTC).isoformat()}",
        "Counts only. No record contents, no credentials.",
        "",
        f"contacts: created={contact_stats.created} unchanged={contact_stats.unchanged} "
        f"updated={contact_stats.updated} dead_lettered={contact_stats.dead_lettered}",
        f"deals:    created={deal_stats.created} unchanged={deal_stats.unchanged} "
        f"dead_lettered={deal_stats.dead_lettered}",
        f"client:   pages_fetched={client.pages_fetched} throttles={client.throttles}",
        f"store:    {store.snapshot()}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nlive evidence written to {path.relative_to(REPO)}")


if __name__ == "__main__":
    main()
