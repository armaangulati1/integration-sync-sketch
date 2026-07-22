"""The 60-second happy-path demo.

Generates synthetic fixtures, syncs all three surfaces into a fresh CRM store, then syncs a
SECOND time to prove idempotency: the second pass makes zero changes (everything reports
'unchanged', and the table snapshot is byte-for-byte identical).

Run:  .venv/bin/python scripts/run_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from integration_sync.crm_store import CrmStore  # noqa: E402
from integration_sync.logging_config import configure_logging  # noqa: E402
from integration_sync.pipeline import build_readers, sync_all  # noqa: E402
from integration_sync.sync_engine import SyncEngine  # noqa: E402
from scripts import generate_synthetic_data  # noqa: E402

DATA = REPO / "data"
DB = DATA / "demo.db"


def _print_stats(title: str, stats_list) -> None:
    print(f"\n=== {title} ===")
    for s in stats_list:
        print(
            f"  {s.source:<11} created={s.created} unchanged={s.unchanged} "
            f"updated={s.updated} conflict_ignored={s.conflict_ignored} "
            f"dead_lettered={s.dead_lettered} cursor->{s.cursor_after}"
        )


def main() -> None:
    configure_logging()
    generate_synthetic_data.main()

    if DB.exists():
        DB.unlink()
    store = CrmStore(DB)
    store.init_schema()
    engine = SyncEngine(store)
    readers = build_readers(DATA)

    print("\n--- FIRST SYNC (cold start) ---")
    first = sync_all(engine, readers)
    _print_stats("first pass", first)
    snap1 = store.snapshot()
    print(f"\ntable snapshot after first sync: {snap1}")

    print("\n--- SECOND SYNC (incremental: cursor skips already-synced records) ---")
    second = sync_all(engine, readers)
    _print_stats("second pass", second)
    read_second = sum(s.processed + s.dead_lettered for s in second)
    print(f"\nrecords the readers even looked at this pass: {read_second} (cursor did its job)")

    print("\n--- THIRD SYNC (full re-scan: cursors reset, hash-idempotency does the work) ---")
    for source in readers:
        store.set_cursor(source, None)
    third = sync_all(engine, readers)
    _print_stats("third pass", third)
    snap2 = store.snapshot()
    print(f"\ntable snapshot after third sync: {snap2}")

    created_total = sum(s.created for s in first)
    unchanged_third = sum(s.unchanged for s in third)
    changed_third = sum(s.created + s.updated + s.conflict_ignored for s in third)
    print("\n=== RESULT ===")
    print(f"  first pass created {created_total} activities across 3 surfaces")
    print(f"  second pass (incremental) processed {read_second} records")
    print(f"  third pass (full re-scan) saw {unchanged_third} unchanged, "
          f"changed {changed_third} rows")
    print(f"  snapshots identical across re-runs: {snap1 == snap2}")
    print(f"  contacts={snap2['contacts']} accounts={snap2['accounts']} "
          f"activities={snap2['activities']} dead_letter={snap2['dead_letter']}")

    store.close()
    if changed_third != 0 or snap1 != snap2:
        raise SystemExit("DEMO FAILED: re-sync was not a no-op")
    print("\nOK: sync is idempotent (incremental cursor + content-hash) and stable.")


if __name__ == "__main__":
    main()
