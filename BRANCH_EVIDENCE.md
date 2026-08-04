# Branch evidence: `hubspot-dashboard`

Mechanical verification of this branch, run at `875c5a3` (the commit before this file was
added). Every command below was run against that tree and its output is pasted verbatim
rather than summarized. Where a check can only report absence, a control that CAN return a
positive is run beside it, because a check that cannot fail is not evidence.

- Range under review: `2c78ce1..875c5a3` (`main..HEAD`), 8 commits.
- Run on 2026-08-04, macOS, Python 3.14.6, ruff 0.15.22, streamlit 1.60.0.

Note on the runner: this repository is developed against a local `.venv`, so commands are
shown as `.venv/bin/...`. CI runs the same checks on Python 3.11 and 3.12.

---

## 1. Test suite

```
$ .venv/bin/python -m pytest
........................................................................ [ 42%]
........................................................................ [ 84%]
...........................                                              [100%]
171 passed in 3.01s
```

171 passed, 0 failed, 0 skipped. The count moved from 169 because this branch's review
added two tests: one asserting the funnel splits on the engine's own dead-letter category
constants, and one wedged-job control beside the cursor-staleness test.

Zero skips is worth reading carefully. Three of the four dashboard tests skip when
streamlit is absent, and they did not skip here because the `dashboard` extra is installed
locally, as it is in CI. On a base install those three skip by design and the compile check
still runs.

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
boundary test is parametrized over 8 cases. Both numbers are stated in the README and both
were re-derived here rather than carried forward.

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

Empty, which is the pass condition. Two controls, because an empty result from a broken
command looks identical to an empty result from a clean tree:

```
$ git ls-files | grep -cE "^tests/"
21
```

The pattern machinery can return a positive on this tree, so the empty result above is a
finding rather than a silent failure.

```
$ for d in .venv data tests/vendor_blocklist.local.txt; do ...; done
.venv exists on disk; tracked=0
data exists on disk; tracked=0
tests/vendor_blocklist.local.txt exists on disk; tracked=0
```

All three genuinely exist locally and none is tracked, so `.gitignore` is doing the work
rather than the paths simply being absent.

## 4. Secret scan over the shipping range

```
$ git diff main..HEAD | grep -nE "sk-[A-Za-z0-9]{8}|pat-na1-|$HOME_PREFIXES|token=|secret|password|BEGIN [A-Z ]*PRIVATE KEY" | grep -v SYNTHETIC
1929:+        return f"LiveTransport(base_url={self.base_url!r}, token=<redacted>)"
4588:+    assert pattern.search(HOME_PREFIXES[0] + "someone/secret-project")
```

`$HOME_PREFIXES` stands in for the three absolute home-directory prefixes that were really
in the command: the two unix ones and the Windows one, the same set
`tests/test_repo_hygiene.py` assembles. They are written that way here for a reason worth
recording. The first draft of this file pasted them literally, and the repo-hygiene guard
immediately failed on this file. That is the guard doing exactly its job, on a document
whose entire purpose is to assert the tree is clean, so the document was changed rather than
the guard.

Two hits, both benign, and both are the safety machinery itself:

1. The `token=<redacted>` line is the `LiveTransport.__repr__` that keeps a real token out
   of tracebacks and CI logs. It is asserted by
   `test_the_token_is_redacted_from_the_repr`.
2. The `secret-project` line is the repo-hygiene guard's own can-it-fire control. The home
   prefix it uses is assembled at runtime, so no literal absolute path appears in the file.

A broader token-shape sweep:

```
$ git diff main..HEAD | grep -nE "\bpat-[A-Za-z0-9]|sk-[A-Za-z0-9]|ghp_|AKIA[0-9A-Z]{16}"
3743:+    monkeypatch.setenv(TOKEN_ENV_VAR, "pat-na1-SYNTHETIC-TOKEN")
3745:+    assert request.get_header("Authorization") == "Bearer pat-na1-SYNTHETIC-TOKEN"
3762:+    monkeypatch.setenv(TOKEN_ENV_VAR, "pat-na1-SYNTHETIC-TOKEN")
3764:+    assert "pat-na1-SYNTHETIC-TOKEN" not in text
3769:+    monkeypatch.setenv(TOKEN_ENV_VAR, "pat-na1-SYNTHETIC-TOKEN")
3776:+    monkeypatch.setenv(TOKEN_ENV_VAR, "pat-na1-SYNTHETIC-TOKEN")
```

Every hit is the literal `SYNTHETIC` placeholder used to test the auth header and the repr
redaction. The only other `pat-` occurrences in the range are the RUNBOOK's documentation
ellipses (`pat-...`). No credential of any kind is in the range.

## 5. The four demos still run

CI runs all four after the suite, and each self-asserts rather than merely exiting zero.

```
run_demo.py OK
failure_demo.py OK
milestone_demo.py OK
hubspot_demo.py OK
```

## 6. The README Mermaid diagram renders

Verified by an actual render, not by reading the syntax:

```
$ npx -y @mermaid-js/mermaid-cli@11 -i diagram.mmd -o diagram.svg
Generating single mermaid chart
(exit 0, 59998-byte SVG)
```

mermaid-cli 11.16.0, against the block extracted verbatim from README.md. The rendered
output was inspected, not just produced: all five source nodes, the transport seam with its
recorded and live branches, the four engine outcomes, the CRM SQLite table list, the
milestone and escalation layer, and the `ops_metrics` to dashboard edge are all present, and
there is no Mermaid error node. The `\n` line breaks inside node labels resolve to real
`<br />` elements in the SVG rather than to literal backslash-n, which was the one syntax
risk worth checking.

One cosmetic observation, deliberately NOT changed because it is outside the review's
scope: the `DB_tables` subgraph is not connected by an edge to anything, so it floats at the
edge of the layout rather than sitting beside the `CRM SQLite` node it describes. The
diagram is valid and complete; it would simply read better anchored. Worth a decision before
this branch is ever pushed.

## 7. Fixture account names, checked against the real world

The three synthetic account names on this branch all collided with real organizations. Each
was searched, and each replacement was searched too:

| Old name | What it collides with | New name | Search result for the new name |
|---|---|---|---|
| Lakeside Labs | Lakeside Labs, a real research organization in Klagenfurt, Austria, plus a US software consultancy of the same name | Quillhaven Labs | no results |
| Northgate Health | several real facilities, including Northgate Health and Rehabilitation Center (Bessemer AL and San Antonio TX), Northgate Health Care Facility (North Tonawanda NY), and Northgate Health Centre (Oxford UK) | Vantree Health | no results |
| Riverbend Clinic | Skagit Regional Clinics - Riverbend, Mount Vernon WA | Ambervale Clinic | no results |

All email domains keep the `.example` TLD reserved by RFC 2606, so the addresses cannot
resolve regardless of the display name.

`demo_transcript.txt` was regenerated after the rename. Its diff against the previous
version contains only four classes of change: run timestamps, the pytest count (169 to 171),
the two renamed deal lines, and the content hash of the keyless dead-letter record. That
last one is correct rather than alarming: the hash covers the record payload, and the
payload's account name changed.
