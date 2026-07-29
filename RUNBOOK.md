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
py scripts/generate_synthetic_data.py   # writes data/calendar.ics, data/mailbox.mbox,
                                        # data/crm_export.json, data/tickets.json, data/milestones.json
py scripts/run_demo.py                   # cold sync, then two re-runs proving idempotency
py scripts/milestone_demo.py             # throttled + drifting ticket sync, plan, escalations, report
```

A healthy run ends with `OK: sync is idempotent (...) and stable.` and a table snapshot
that is identical across re-runs. The milestone demo ends with `OK: throttled read, drift
absorbed, plan projected, escalations raised.` and writes `data/milestone_report.txt`.

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

## When the ticket board comes back short

Symptom: the sync reports fewer tickets than the board has, or `read_tickets` raises
`RateLimitError`.

That exception is deliberate. `fetch_pages` re-raises a throttle it could not wait out
rather than returning the pages it did get, because a short page set looks exactly like a
complete board to the caller and would silently mark live tickets as absent.

1. **Check how hard it was throttled.** The endpoint tracks it:

   ```python
   print(api.calls, "served,", api.refusals, "refused")
   ```

2. **Give it a bigger budget before you give it a bigger delay.** `read_tickets(...,
   attempts=N)` controls how many times a single page may be retried. The quota window is
   the source's, not yours; the only fix on your side is being willing to wait it out.

3. **If the source advertises `Retry-After`, do not override it.** The client already
   prefers the server's number over its own schedule. A local schedule that is shorter than
   the window burns the whole retry budget without the bucket ever draining, which is
   exactly the failure `test_client_falls_back_to_exponential_when_no_retry_after_is_offered`
   walks through.

4. **Re-running after a throttle is safe.** The cursor only advances over records actually
   handled, and the sync is idempotent, so a partial run followed by a re-run converges.

---

## When the ticket source changes shape

Symptom: `schema_drift` rows appear in `audit_log`, or tickets start landing in
`dead_letter` with a `missing_required` explanation.

```sql
SELECT natural_key, detail FROM audit_log WHERE action = 'schema_drift' ORDER BY audit_id;
```

Read the `kind` at the front of each detail line:

- `alias_used` -> a field arrived under a different name. **Already handled.** The value was
  resolved through the alias list and nothing was lost. No action needed, though it is worth
  moving the new spelling to the front of that field's `aliases` tuple in
  `ticket_source.TICKET_FIELDS` once the source has clearly settled on it.
- `type_coerced` -> a scalar became an object or the reverse. **Already handled.** If the new
  shape is now permanent, flip that field's `object_expected` so the note stops firing on
  every record; a signal that fires constantly is not a signal.
- `unknown_field` -> a field nobody declared. It is preserved in `tickets.unmapped_json`, not
  dropped. If it matters, add a `FieldSpec` for it and a column; until then it is retrievable:

  ```sql
  SELECT ticket_key, unmapped_json FROM tickets WHERE unmapped_json != '{}';
  ```

- `missing_required` -> **this one needs you.** A field the connector declared required
  arrived under no known spelling, so the ticket was dead-lettered rather than written with
  a blank. Find the new spelling in the source, add it to that field's `aliases`, and
  reprocess the queue. Nothing was lost in the meantime.

A rename alone never rewrites your rows: drift notes are excluded from the content hash, so
a re-sync after a pure rename reports `unchanged`, not `updated`. If you see a wave of
`updated` after a schema change, the values changed too, not just the names.

---

## When an escalation looks wrong

1. **Check the date it was computed for.** Escalations are keyed by `as_of`. A rule fires
   against a specific day, and re-running that same day is a no-op by design.

   ```sql
   SELECT as_of, milestone_key, rule_id, subject_key, detail
   FROM escalations ORDER BY as_of DESC, milestone_key;
   ```

2. **Read the evidence in `detail`.** Every escalation names what produced it: the ticket
   key, the predecessor milestone, or the day count. If the evidence is right and the
   conclusion is wrong, the threshold is wrong, not the rule.

3. **Tune thresholds in one place**, not by editing rules:

   ```python
   from integration_sync.escalation import RuleConfig, evaluate
   evaluate(plan, grouped, as_of=today, config=RuleConfig(dependency_slip_threshold_days=5))
   ```

   Note the boundary: the threshold is a **tolerance**. A predecessor that slipped exactly
   the threshold does not escalate; it has to exceed it.

4. **A milestone shows slip you did not expect?** It is almost always inherited. Compare
   `inherited_slip_days` against `slip_days` on the `MilestoneView`: if they match, the delay
   is upstream and the `dependency_slip` escalation names which predecessor.

5. **`DependencyCycle` on `build_plan`?** Two milestones depend on each other, so no
   ordering exists and no slip number would be explainable. Fix the plan; the code will not
   guess an order.

---

## Reference: tuning the engine

`SyncEngine(store, attempts=3, base_delay=0.05, factor=2.0, max_delay=2.0, jitter=False,
sleep=time.sleep, fault=None)`

- `attempts` / `base_delay` / `factor` / `max_delay` -> the retry budget and backoff shape.
- `jitter=True` -> full jitter on the delays (recommended under real contention).
- `sleep` -> injectable so tests and demos do not actually block.
- `fault` -> the transient-failure hook; `None` in real use, a fault injector in the demo.
