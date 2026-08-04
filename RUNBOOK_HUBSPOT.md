# Runbook: taking the HubSpot connector live

The connector in `integration_sync/hubspot_source.py` runs offline against recorded
cassettes. This runbook is how a human turns that into a run against a real HubSpot
developer sandbox, and what may honestly be claimed afterward.

Read the claim ladder first, because the whole point of this file is moving from rung 1 to
rung 2, and nothing here is claimable until the run has actually happened.

| Rung | What is true | What may be said |
|---|---|---|
| **1. Now** | The full sync path runs against self-authored cassettes whose envelopes follow HubSpot's documented response shapes. Pagination, 429 backoff, idempotency, and dead-lettering are all exercised. | "HubSpot CRM v3 connector at demo scope, mock-verified against recorded response shapes." |
| **2. After this runbook** | The same client, readers, and engine ran against `api.hubapi.com` with a private app token, on a sandbox holding records a human created. | "Verified against a live HubSpot developer sandbox on `<date>`." |

Rung 2 does not replace rung 1's caveats. It is still demo scope, still synthetic records,
still a single-user sandbox, and still no production system.

---

## What only a human can do

An agent does not create accounts, does not accept terms of service, and does not handle
credentials. Steps 1 through 4 are yours. Step 5 onward is a command.

---

## 1. Create a free HubSpot developer test account

1. Go to `developers.hubspot.com` and create a developer account if you do not have one.
   It is free.
2. Inside it, create a **test account** (in the developer portal this has lived under
   "Test accounts", though HubSpot moves its navigation around, so search the portal if the
   label has changed). A test account is a full CRM instance with fake data, separate from
   any real portal.

A free standard HubSpot CRM account also supports private apps and works fine for this. The
developer test account is the recommendation only because nothing in it is ever real.

## 2. Create a private app and copy its token

In the test account:

1. Settings (the gear icon) then **Integrations** then **Private apps**.
2. **Create a private app**. Name it something obviously non-production, for example
   `integration-sync-sketch (read only)`.
3. On the **Scopes** tab, grant exactly these two, and nothing else:
   - `crm.objects.contacts.read`
   - `crm.objects.deals.read`
4. Create the app and copy the access token. It starts with `pat-`.

**Grant read scopes only.** This connector issues `GET` requests and nothing else. A write
scope it never uses is a credential that can do damage it has no reason to be able to do.

## 3. Put the token in the environment, never in a file this repo tracks

```bash
export HUBSPOT_PRIVATE_APP_TOKEN='pat-...'
```

If you prefer a file, `.env` is already gitignored, but nothing in this repo reads it
automatically, so you would still export it yourself:

```bash
echo "HUBSPOT_PRIVATE_APP_TOKEN='pat-...'" >> .env   # gitignored
set -a && source .env && set +a
```

Two safety properties are worth knowing, because they are tested rather than promised:

- Live mode **refuses to start** without a token instead of quietly falling back to the
  cassettes. A fixture run that reports itself as live would be the worst possible outcome
  here, so it is made impossible (`test_live_transport_refuses_to_start_without_a_token`).
- The token is redacted from the transport's `repr`, which is what lands in tracebacks and
  CI logs (`test_the_token_is_redacted_from_the_repr`).

## 4. Seed a handful of synthetic records

In the test account UI, create by hand:

- **3 to 5 contacts.** Give each an email at a `.example` domain, for example
  `dana.rivers@marnovek-health.example`. Fill in first name, last name, and company.
- **1 contact with no email address at all.** This is the interesting one. It exercises the
  dead-letter path against a real record rather than a fixture.
- **2 to 3 deals.** Set a name, an amount, and a stage. **Associate at least one deal with
  a contact** and deliberately **leave one deal unassociated**, which exercises the
  unresolvable-association path.

Use invented names and `.example` domains only. A sandbox is still somebody's data, and the
evidence file this produces is going into a public repository.

If you want to see pagination do real work, create more than 100 contacts by import.
Otherwise the live run fetches one page, which is correct behavior and proves less. This is
optional: the cassettes already prove multi-page behavior.

## 5. Run it

```bash
.venv/bin/python scripts/hubspot_demo.py --live
```

Expected output shape:

```
########## HUBSPOT CONNECTOR DEMO (LIVE) ##########
--- syncing HubSpot contacts ---
  created=4 unchanged=0 updated=0 dead_lettered=1 cursor->...
  client: 1 page(s) fetched, 0 throttle(s) waited out
--- syncing HubSpot deals ... ---
--- re-syncing the SAME contacts (idempotency check) ---
  created=0 unchanged=4 updated=0
```

The line that matters most is the last one. `created=0 updated=0` on the second pass against
a real account is the idempotency guarantee holding against real data, not against a fixture
that was written to agree with it.

The counts will not match the recorded run, because they describe your sandbox. That is why
`--live` skips the recorded-mode assertions and writes an evidence file instead.

## 6. Capture the evidence

The live run writes `evidence/hubspot_live_run_<date>.txt` automatically. It contains counts
and timings only, never record contents and never the token.

Capture two screenshots by hand into `evidence/`:

1. **`hubspot_private_app_scopes.png`**: the private app's Scopes tab, showing the two read
   scopes granted. Redact the token if any part of it is visible.
2. **`hubspot_call_log.png`**: the private app's **Logs** tab, which lists the API calls
   HubSpot itself received. This is the strongest evidence in the whole exercise, because it
   is HubSpot's record of the requests rather than this repo's own claim about them.

Optionally, a third:

3. **`dashboard.png`**: the dashboard rendered over the live store, captured with

   ```bash
   SYNC_DB=data/hubspot_demo.db .venv/bin/streamlit run dashboard/app.py
   ```

Before committing any screenshot, look at it. Crop or redact anything that shows the token,
a portal id you would rather not publish, or a real name.

Do not commit the live console transcript. `demo_transcript.txt` is a record of the RECORDED
run and nothing else. Live mode already withholds deal rows and dead-letter errors from the
console for this reason, but the transcript also carries the sandbox's own timings and
counts, and the committed evidence file is the place for those.

## 7. Update the claim

Once the run is done and the evidence is committed, and **only** then, change the README's
HubSpot scope section from the rung 1 wording to the rung 2 wording, and add the date. If
the run never happens, leave it exactly as it is. The rung 1 claim is a true and useful
claim on its own.

---

## Troubleshooting

**`HubSpotAuthError: no HubSpot token`**
The environment variable is not set in the shell you are running from. `export` does not
cross terminal windows.

**`401` from HubSpot**
The token is wrong, was rotated, or belongs to a different portal. Copy it again from the
private app page.

**`403` from HubSpot**
The token is valid but the app is missing a scope. Add `crm.objects.contacts.read` and
`crm.objects.deals.read` on the Scopes tab, then update the app. HubSpot issues a new token
when scopes change, so copy the new one.

**`429` from HubSpot**
The client already handles this: it waits out `Retry-After` when the response carries one
and uses its own exponential schedule when it does not. Seeing `throttle(s) waited out` in
the output above zero is the rate-limit path working, not failing. It is only a problem if
the run ends with an unhandled `RateLimitError`, which means the retry budget was exhausted.

**The run reports zero contacts**
The sandbox is empty, or every contact predates the cursor. The demo starts from a fresh
database each run, so the cursor should be empty. Check the records exist in the UI.

**A contact you expected is dead-lettered**
Check whether it has an email address. The validation boundary requires one, because
without it there is no key to attach the activity to. That is the intended behavior and is
exactly what the deliberately-broken contact in step 4 is there to show.

---

## Rotating or revoking the token afterward

When you are finished, delete the private app in the test account, or at minimum unset the
variable:

```bash
unset HUBSPOT_PRIVATE_APP_TOKEN
```

A read-only sandbox token is low risk, and it is still a credential. Do not leave it in a
shell history file you sync anywhere.
