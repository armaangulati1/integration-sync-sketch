# RUNBOOK

Operating the sync engine: how to run it, and what to do when something goes wrong. This is
a demo, but the runbook is written the way a real one would be, because knowing the
operational moves is half the point of the exercise.

All commands assume the repo root and the project virtualenv:

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
alias py=.venv/bin/python
```

---

## Normal operation

Generate the synthetic fixtures and run a full sync:

```bash
py scripts/generate_synthetic_data.py   # writes data/calendar.ics, data/mailbox.mbox, data/crm_export.json
py scripts/run_demo.py                   # cold sync, then two re-runs proving idempotency
```

A healthy run ends with `OK: sync is idempotent (...) and stable.` and a table snapshot
that is identical across re-runs.

The state lives in one SQLite file (`data/demo.db` for the demo). Everything below is a
query or update against that database. Open it with:

```bash
sqlite3 data/demo.db
```

---

## What "healthy" looks like

- `sync_state` has one row per source, and its `cursor` is at or near the newest source
  timestamp.
- `dead_letter` has zero rows with `resolved = 0`, or a known, triaged set.
- `sync_records` count equals `activities` count (one ledger row per synced activity).
- `audit_log` grows by one row per create/update/conflict, and does **not** grow on a
  no-op re-run.

Quick check:

```sql
SELECT source, cursor FROM sync_state;
SELECT category, COUNT(*) FROM dead_letter WHERE resolved = 0 GROUP BY category;
SELECT (SELECT COUNT(*) FROM sync_records) AS ledger,
       (SELECT COUNT(*) FROM activities)   AS activities;
```

---

## Reprocessing the dead-letter queue

A record lands in `dead_letter` for one of two reasons, recorded in `category`:

- `poison` / `poison_no_key` -> the record itself is invalid (bad timestamp, missing
  contact, no natural key). **Retrying alone will not help; the payload must be fixed first.**
- `transient_exhausted` -> the record was valid but its write kept failing (a flaky
  dependency). **Once the dependency is healthy, reprocessing usually clears it.**

Inspect the queue:

```sql
SELECT dl_id, source, natural_key, category, failure_count, error
FROM dead_letter WHERE resolved = 0 ORDER BY dl_id;
```

Drain it in code (this is what `failure_demo.py` does after "fixing" the dependency):

```python
from integration_sync.crm_store import CrmStore
from integration_sync.sync_engine import SyncEngine
from integration_sync.pipeline import dead_letter_rebuilders

store = CrmStore("data/demo.db"); store.init_schema()
engine = SyncEngine(store)
resolved = engine.reprocess_dead_letter(dead_letter_rebuilders())
print("resolved", resolved)
```

Behavior you can rely on:

- A row that now succeeds is marked `resolved = 1` and its activity is applied.
- A row that fails again stays queued with `failure_count` incremented, so you can see
  which records are stubborn.
- A `poison_no_key` row is **never** auto-recovered: without a source id there is no safe
  identity, so it stays queued until a human supplies the key (edit the source and re-sync,
  or delete the row if it is junk).

To retire a row you have decided is junk rather than fixing it:

```sql
UPDATE dead_letter SET resolved = 1 WHERE dl_id = <id>;
```

---

## Resetting a cursor safely

The cursor in `sync_state.cursor` is the incremental high-water mark. Resetting it forces
the next run to re-read older records. **This is safe** precisely because the sync is
idempotent: re-reading already-synced records produces `unchanged` no-ops, not duplicates.

Re-scan one source from the beginning:

```sql
UPDATE sync_state SET cursor = NULL WHERE source = 'calendar';
```

Re-scan from a specific point (for example, to pick up an edit you know predates the
cursor):

```sql
UPDATE sync_state SET cursor = '2026-03-03T00:00:00+00:00' WHERE source = 'email';
```

Then run the sync again. Expect a burst of `unchanged` (and any genuinely changed records
as `updated`), but **no new duplicate activities** and **no snapshot drift** for unchanged
data.

What you must NOT do: delete rows from `sync_records` to "force a resync". That erases the
idempotency ledger, so the engine can no longer tell a re-read from a new record. Reset the
**cursor** instead; leave the ledger intact.

---

## When a sync stalls or looks wrong

Work down this list in order.

1. **Is the cursor stuck ahead of the data?** If `sync_state.cursor` is newer than every
   source record (for example after a clock skew or a bad manual edit), the reader filters
   everything out and the run does nothing. Check `SELECT source, cursor FROM sync_state;`
   against the newest source timestamp; reset the cursor (above) if it overshot.

2. **Are records piling up in the dead-letter queue?** `SELECT category, COUNT(*) FROM
   dead_letter WHERE resolved = 0 GROUP BY category;`
   - Mostly `transient_exhausted` -> the downstream dependency is unhealthy. Fix it, then
     reprocess. Consider raising `attempts` / `base_delay` on the engine if the dependency
     is merely slow.
   - Mostly `poison` -> a source is emitting malformed data. Read the `error` column; it
     names the exact validation that failed (bad `occurred_at`, invalid `contact_email`,
     missing key). Fix at the source.

3. **Did a run half-apply?** It should not: each record is one transaction. If you suspect
   it, verify the invariant `COUNT(sync_records) == COUNT(activities)`. A mismatch is a
   real bug, not expected operation.

4. **Two records at the exact same timestamp getting missed?** This is the known watermark
   boundary limitation. Symptom: a record exists in the source with `occurred_at` equal to
   the cursor and never syncs. Workaround: reset the cursor to just before that timestamp
   and re-run (idempotency makes this cheap and safe). A production fix pairs the timestamp
   with a tiebreaker id or reads with a small overlap window.

5. **Audit trail says `conflict_ignored` for something you expected to update?** That means
   the incoming record's `occurred_at` was older than what is already stored, so
   last-write-wins kept the newer stored copy. Check the timestamps:
   `SELECT * FROM audit_log WHERE natural_key = '<key>' ORDER BY audit_id;`
   If the incoming record really should win, its source timestamp is wrong; fix it at the
   source rather than overriding the policy.

---

## Reference: tuning the engine

`SyncEngine(store, attempts=3, base_delay=0.05, factor=2.0, max_delay=2.0, jitter=False,
sleep=time.sleep, fault=None)`

- `attempts` / `base_delay` / `factor` / `max_delay` -> the retry budget and backoff shape.
- `jitter=True` -> full jitter on the delays (recommended under real contention).
- `sleep` -> injectable so tests and demos do not actually block.
- `fault` -> the transient-failure hook; `None` in real use, a fault injector in the demo.
