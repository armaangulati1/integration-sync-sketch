# Verification record: `hubspot-dashboard`

What this branch claims, and the checks that back each claim. Every command below was run
against this tree and its output is pasted verbatim rather than summarized. Where a check
can only report an absence, a control that CAN return a positive is run beside it, because a
check that cannot fail is not evidence.

- Code range: `2c78ce1..f15e8d1` (`main` to the last code commit), 11 commits.
- All commands run at `f15e8d1`, on 2026-08-03 (America/Los_Angeles, UTC-7), which is that
  commit's own date. This file is committed on top of `f15e8d1` and adds no code, which is
  why the ranges quoted throughout end there.
- macOS. Python 3.14.6, ruff 0.15.22, streamlit 1.60.0, Node v20.20.2, mermaid-cli 11.16.0.

This repository is developed against a local `.venv`, so commands are shown as
`.venv/bin/...`. CI runs the same suite on Python 3.11 and 3.12.

---

## 1. Test suite

```
$ .venv/bin/python -m pytest
........................................................................ [ 42%]
........................................................................ [ 84%]
...........................                                              [100%]
171 passed in 2.90s
pytest exit: 0
```

171 passed, 0 failed, 0 skipped.

Zero skips is worth reading carefully rather than skimming. Three of the four dashboard
tests skip when streamlit is absent, and they did not skip here because the `dashboard`
extra is installed locally, as it is in CI. On a base install those three skip by design and
the compile check still runs.

Per-file collected counts, which the README quotes:

```
$ .venv/bin/python -m pytest --collect-only -qq
tests/test_backoff.py: 9
tests/test_conflict.py: 4
tests/test_dashboard.py: 4
tests/test_deadletter.py: 5
tests/test_demos.py: 4
tests/test_escalation.py: 18
tests/test_hubspot_source.py: 34
tests/test_idempotency.py: 3
tests/test_incremental.py: 4
tests/test_milestones.py: 12
tests/test_no_company_names.py: 2
tests/test_normalize.py: 9
tests/test_ops_metrics.py: 33
tests/test_rate_limit.py: 8
tests/test_repo_hygiene.py: 2
tests/test_report.py: 3
tests/test_schema_drift.py: 12
tests/test_sources.py: 5
```

Sum: 171. `test_ops_metrics.py` defines 26 functions and collects 33 because the age-band
boundary test is parametrized over 8 cases. Both numbers appear in the README and both were
re-derived here rather than carried forward.

## 2. Lint

```
$ .venv/bin/ruff check .
All checks passed!
ruff exit: 0
```

## 3. Nothing that should be ignored is tracked

```
$ git ls-files | grep -E "\.venv|__pycache__|vendor_blocklist.local|^data/"
grep exit: 1 (1 = no matches = pass)
```

Empty, which is the pass condition. An empty result from a broken command looks identical to
an empty result from a clean tree, so two controls run beside it. First, the same machinery
against a pattern that IS present:

```
$ git ls-files | grep -cE "^tests/"
21
```

Second, confirmation that the three ignored paths genuinely exist on disk, so `.gitignore`
is doing the work rather than the paths simply being absent:

```
.venv: on disk=yes tracked=0
data: on disk=yes tracked=0
tests/vendor_blocklist.local.txt: on disk=yes tracked=0
```

## 4. No credential is anywhere in the branch

```
$ git diff 2c78ce1..HEAD -- . ':(exclude)BRANCH_EVIDENCE.md' \
    | grep -nE "sk-[A-Za-z0-9]{8}|pat-na1-|$HOME_PREFIXES|token=|secret|password|BEGIN [A-Z ]*PRIVATE KEY" \
    | grep -v SYNTHETIC
1937:+        return f"LiveTransport(base_url={self.base_url!r}, token=<redacted>)"
4624:+    assert pattern.search(HOME_PREFIXES[0] + "someone/secret-project")
```

Two hits, both benign, and both are the safety machinery itself:

1. The `token=<redacted>` line is the `LiveTransport.__repr__` that keeps a real token out
   of tracebacks and CI logs. It is asserted by `test_the_token_is_redacted_from_the_repr`.
2. The `secret-project` line is the repo-hygiene guard's own can-it-fire control. The home
   prefix it uses is assembled at runtime, so no literal absolute path appears in the file.

A broader sweep for token shapes over the same range:

```
$ git diff 2c78ce1..HEAD -- . ':(exclude)BRANCH_EVIDENCE.md' \
    | grep -nE "\bpat-[A-Za-z0-9]|sk-[A-Za-z0-9]|ghp_|AKIA[0-9A-Z]{16}"
3761:+    monkeypatch.setenv(TOKEN_ENV_VAR, "pat-na1-SYNTHETIC-TOKEN")
3763:+    assert request.get_header("Authorization") == "Bearer pat-na1-SYNTHETIC-TOKEN"
3780:+    monkeypatch.setenv(TOKEN_ENV_VAR, "pat-na1-SYNTHETIC-TOKEN")
3782:+    assert "pat-na1-SYNTHETIC-TOKEN" not in text
3787:+    monkeypatch.setenv(TOKEN_ENV_VAR, "pat-na1-SYNTHETIC-TOKEN")
3794:+    monkeypatch.setenv(TOKEN_ENV_VAR, "pat-na1-SYNTHETIC-TOKEN")
```

Every hit is the literal `SYNTHETIC` placeholder used to test the auth header and the repr
redaction. The only other `pat-` occurrences in the range are the RUNBOOK's documentation
ellipses (`pat-...`).

Two things about how that command is written, both deliberate:

`$HOME_PREFIXES` stands in for the three absolute home-directory prefixes that were really in
the command: the two unix ones and the Windows one, the same set `tests/test_repo_hygiene.py`
assembles at runtime. Writing them literally here makes the repo-hygiene guard fail on this
very file, which is the guard doing exactly its job on a document whose entire purpose is to
assert the tree is clean. The document gives way, not the guard.

This file is excluded from its own scan by pathspec. It quotes the patterns and the
`SYNTHETIC` placeholder verbatim, so including it would only report this document quoting
itself, and the pasted output would change every time the output was pasted. Excluding it is
also what makes the output above reproducible at `HEAD`. The file is not thereby unchecked.
It gets its own scan, against a narrower pattern that matches credential shapes rather than
the words "token" or "secret":

```
$ grep -cE "sk-[A-Za-z0-9]{16}|ghp_[A-Za-z0-9]{20}|AKIA[0-9A-Z]{16}|pat-na1-[0-9a-f]{8}|BEGIN [A-Z ]*PRIVATE KEY" BRANCH_EVIDENCE.md
3
```

Three hits, and all three are accounted for: they are the two deliberately-fake literals in
the control immediately below, plus the `printf` line that produces them. A fourth hit would
be a finding. Those three lines are also the control, since a pattern that matches the fake
`AKIA` and `ghp_` literals is demonstrably capable of matching a real one:

```
$ printf 'AKIA0123456789ABCDEF\nghp_abcdefghijklmnopqrst\n' | grep -nE "<the same pattern>"
1:AKIA0123456789ABCDEF
2:ghp_abcdefghijklmnopqrst
grep exit: 0 (0 = the pattern fires)
```

The two literals are invented filler in the correct shapes, not redacted real credentials.

## 5. The four demos still run

CI runs all four after the suite. Each self-asserts rather than merely exiting zero, so a
zero exit means the assertions held, not just that Python reached the end of the file.

```
run_demo.py        exit=0  OK: sync is idempotent (incremental cursor + content-hash) and stable.
failure_demo.py    exit=0  OK: every failure mode handled as designed.
milestone_demo.py  exit=0  OK: throttled read, drift absorbed, plan projected, escalations raised.
hubspot_demo.py    exit=0  OK: HubSpot contacts and deals synced, throttle survived, re-run changed nothing.
```

## 6. The README Mermaid diagram renders, and renders the right thing

Verified by an actual render of the block extracted verbatim from README.md, then by
grepping the rendered SVG. Reading the syntax and calling it valid would not be evidence.

```
$ npx -y @mermaid-js/mermaid-cli@11 -i diagram.mmd -o diagram.svg
Generating single mermaid chart
mmdc exit: 0
svg bytes: 59318
```

The render is itself the check, because mermaid-cli fails closed. A deliberately malformed
control diagram was rendered to prove it:

```
$ npx -y @mermaid-js/mermaid-cli@11 -i broken.mmd -o broken.svg
mmdc exit on BROKEN input: 1
broken.svg written: no
Error: Parse error on line 4:
```

Nonzero exit, no file written. So the exit 0 and the 59,318 bytes above mean the diagram
parsed.

What the SVG actually contains:

```
node labels rendered: 38
expected labels checked: 37 (the last one is a control that must be missing)
missing: ['DELIBERATELY-ABSENT-CONTROL-LABEL']
anchor edge L_DB_DB_tables_0 present: True
error node markers -- class="error-icon": 0, "Syntax error" text: 0
(the string error-icon also appears 1x total, because mermaid always emits a .error-icon CSS rule in its stylesheet)
<br /> line breaks in labels: 26, literal backslash-n left over: 0
```

Read line by line: all 36 real nodes are present (five sources, the transport seam with its
recorded and live branches, five readers, `normalize`, the engine, its four outcomes, the
CRM SQLite node and the nine tables inside its subgraph, the milestone and escalation layer,
`ops_metrics`, and the dashboard). The 37th expected label is a string that is deliberately
not in the diagram, and it is reported missing, which proves the presence test can return a
negative rather than passing everything.

The error-node line is the one that would be easy to get wrong. A naive
`grep -c "error-icon"` returns 1 on a perfectly good diagram, because mermaid always writes
a `.error-icon` CSS rule into the SVG's embedded stylesheet. The precise markers of a real
error node, `class="error-icon"` and the rendered text `Syntax error`, are both 0.

The `\n` line breaks inside node labels resolve to 26 real `<br />` elements rather than to
literal backslash-n, which was the one syntax risk worth checking.

The `DB_tables` subgraph previously had no edge to anything, so it floated at the edge of the
layout instead of sitting beside the `CRM SQLite` node it describes. It is now anchored with
a single `DB --> DB_tables` edge, which is the `L_DB_DB_tables_0` edge id confirmed above.

## 7. Fixture account names, checked against the real world

Every account name in this repository is invented. The point of inventing them is that
nothing here can be mistaken for a real customer or a real engagement, and that only holds
if the invented name does not happen to belong to somebody.

**The instrument was validated before it was trusted.** The first endpoint used here
answered "no results" to every query put to it, including queries that genuinely have
results, because it had degraded into a challenge page while still returning HTTP 200. Its
answers were discarded. The endpoint below was validated first by searching for a company
known to exist, and only used once that search returned the right company:

| Query | Engine | Result |
|---|---|---|
| `Vantree Systems EDI` (positive control) | `lite.duckduckgo.com/lite/` | **Vantree Systems Inc**, EDI and API automation connecting trading partners to SAP, Dynamics, Acumatica, Sage, NetSuite. Domain `vantree.com`. Control PASSES: the engine returns real companies. |

Three names were then searched, adopted, and are in the tree today:

| Name in the tree | Query | Engine | Result |
|---|---|---|---|
| **Zeltrovan** Health | `Zeltrovan` | `lite.duckduckgo.com/lite/` | NO RESULTS. Engine offered near-misses (`Rovan Zelt` a music artist, `Zetron` a communications company, `Zeltrons` a Star Wars species), so it searched and found nothing of the name. |
| **Drenvalik** Labs | `Drenvalik` | `lite.duckduckgo.com/lite/` | NO RESULTS. Near-misses returned (`Drunvalo Melchizedek`, `DynaLik`, `Disney Dreamlight Valley`). |
| **Ozmirthex** Clinic | `Ozmirthex` | `lite.duckduckgo.com/lite/` | NO RESULTS. Only unrelated pages (a bar in Yuma, payroll software). |

Single-token searches are used rather than the full two-word names on purpose. If the token
returns nothing at all, no phrase containing it can return anything, so the single-token
search is the stronger of the two checks.

### Six names that were rejected, and why

This section is the reason the record exists. Six candidate names were discarded, and the
last three were discarded *after* an earlier check had cleared them.

| Rejected name | Query | Result that killed it |
|---|---|---|
| Lakeside Labs | `Lakeside Labs` | Lakeside Labs GmbH, a real research organization in Klagenfurt, Austria, plus a US software consultancy of the same name. |
| Northgate Health | `Northgate Health` | Several real facilities: Northgate Health and Rehabilitation Center (Bessemer AL, San Antonio TX), Northgate Health Care Facility (North Tonawanda NY), Northgate Health Centre (Oxford UK). |
| Riverbend Clinic | `Riverbend Clinic` | Skagit Regional Clinics - Riverbend, Mount Vernon WA. |
| Vantree Health | `Vantree Systems` | **Vantree Systems Inc** (`vantree.com`), an EDI and API automation vendor. The worst-placed collision on the branch: healthcare EDI is exactly the work this repository models, so the name read like a real customer. |
| Quillhaven Labs | `Quillhaven company` | `quillhavengoods.com` (leather journals and writing accessories), Quill Haven Card Co. (`quillhavencardcompany.com`), and a novel series' fictional town of the same name. |
| Ambervale Clinic | `Ambervale company` | **AMBERVALE LIMITED**, UK Companies House number 10734193; **Ambervale Capital** (`ambervale.co.uk`), which acquires and manages care homes; AmberVale Capital, an investment firm; Ambervale Homeowners Association, Santa Ana CA. A care-home operator is a healthcare business, which makes this the second-worst collision found. |

Corroborated independently for Ambervale: `en.wikipedia.org` full-text search for `Ambervale`
reports 4 results, including Ambervale as a housing development in Tallaght, Dublin.

### Why the names changed shape

The first six candidates were built the way company names are really built, by recombining
ordinary words: Lakeside, Northgate, Riverbend, Vantree, Quillhaven, Ambervale. That is
precisely why they kept colliding. Real companies are named by recombining ordinary words,
so a plausible-sounding invented name is likely to have been invented already by somebody
else, and a word-composite can also collide by containment with a longer real name.

The three names now in the tree are built from invented morphemes with no dictionary word as
a component. That does not make collision literally impossible, and this record does not
claim it does. What it does is remove the mechanism that produced all six earlier
collisions, and reduce the remaining check to a single exact-string search, which is the
search that was run and pasted above.

### Email domains

Every synthetic email domain in the tracked tree uses a TLD reserved by RFC 2606, so none of
them can resolve regardless of the display name:

```
$ git ls-files | xargs grep -ohE "@[A-Za-z0-9._-]+\.[a-z]{2,}" | sort | uniq -c | sort -rn
  28 @zeltrovan-health.example
  12 @drenvalik-labs.example
   9 @ozmirthex-clinic.example
   4 @vendor.invalid
   2 @y.example
   1 @pytest.mark.parametrize
   1 @pytest.fixture
```

Four domains on `.example` and one on `.invalid`; RFC 2606 reserves both. The last two rows
are pytest decorators caught by an email-shaped regex, not addresses.

### The regenerated transcript

`demo_transcript.txt` was regenerated by re-running the suite and all four demos, not
text-substituted, because one of its values is a content hash. Its diff against the previous
version contains only run timestamps, the pytest duration, the two renamed deal lines, and
the synthetic key of the keyless dead-letter record, which moved from
`calendar:no-key:3a7c966ba6e5d5a3` to `calendar:no-key:941a7ff98593e3b0`. That last change
is correct rather than alarming, and it is the reason a text substitution would have been
wrong: the key is a hash over the record's payload, and that payload's account name changed.

## 8. What this record does not cover

- Everything here was run on one machine against one Python version. CI is the check that
  the suite passes on 3.11 and 3.12; this record does not duplicate it.
- The HubSpot connector's live path is not exercised here. Only the recorded path runs in
  CI and in this record. Live mode is documented in RUNBOOK_HUBSPOT.md and requires a human
  to create a sandbox and export a token.
- The name searches establish that no organization of these names was findable on a
  general web search on the date above. They are not trademark clearance.
