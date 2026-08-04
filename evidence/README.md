# evidence/

Artifacts from runs that cannot be reproduced in CI, because they need an account a human
created.

Everything in the repository outside this directory runs offline and is reproducible with
`pytest` and the demo scripts. This directory is for the opposite case: a run against a real
HubSpot developer sandbox, where the only durable proof is a captured artifact.

## What belongs here

| File | Produced by | What it shows |
|---|---|---|
| `hubspot_live_run_<date>.txt` | `scripts/hubspot_demo.py --live`, automatically | Per-source counts, pages fetched, throttles waited out, and the store snapshot from a real run |
| `hubspot_private_app_scopes.png` | A human, from the HubSpot UI | That the private app was granted read scopes only |
| `hubspot_call_log.png` | A human, from the HubSpot UI | HubSpot's own log of the API calls it received, which is the one piece of evidence this repository does not author itself |
| `dashboard.png` | A human, from a browser | The ops dashboard rendered over the store the live run produced |

See [`RUNBOOK_HUBSPOT.md`](../RUNBOOK_HUBSPOT.md) for how to produce each one.

## Rules

- **No credentials.** Not the token, not a fragment of it. The automatic evidence file is
  written to contain counts only, and any screenshot showing a token must be redacted before
  it is committed.
- **No record contents.** Counts, timings, and shapes are evidence. Contact names and email
  addresses are not, and a sandbox is still somebody's data.
- **An empty directory is an honest directory.** If the live run has not happened, nothing
  here is missing and no claim about it is made anywhere. The README's HubSpot section says
  mock-verified until this directory says otherwise.
