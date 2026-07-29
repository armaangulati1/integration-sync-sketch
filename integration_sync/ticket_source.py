"""Ticketing surface: a synthetic, Jira-shaped issue endpoint and a client that survives it.

This is the fourth integration surface, and it exists to study the two failure modes the
first three do not have, because a file on disk has neither:

Rate limiting
    A real ticketing API answers a client that asks too fast with a 429 and a ``Retry-After``
    header. ``SyntheticTicketApi`` enforces a genuine token-bucket quota against an injected
    clock, so the limiter is a real limiter: the bucket only drains as time advances. The
    client (``fetch_pages``) honors the server's ``Retry-After`` when it is offered and falls
    back to its own exponential schedule when it is not.

Schema drift
    A real source changes underneath a running connector: a field is renamed, a scalar
    becomes an object, an unrecognized field appears, a field the connector depends on
    disappears. ``resolve_fields`` handles the first three without losing data and refuses
    the fourth loudly. Every resolution is recorded as a ``DriftNote`` so the change is
    observable rather than silently absorbed.

The distinction that makes drift handling worth anything:

    A renamed field carrying an unchanged value is a SCHEMA change, not a CONTENT change.

So drift notes are deliberately excluded from the content hash (see
``CanonicalRecord.drift``). A source that renames ``duedate`` to ``due_date`` overnight must
produce a re-sync of zero updates, not a full-table rewrite of every ticket. That property
is asserted directly in ``tests/test_schema_drift.py``.

Everything here is in-memory and offline. There is no Jira, no account, no network call.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from .errors import RateLimitError
from .models import SOURCE_TICKETS, RawRecord
from .retry import backoff_delays
from .timeutil import to_utc_iso

# Drift kinds, recorded on the record and written to the audit log by the projector.
DRIFT_ALIAS = "alias_used"
DRIFT_COERCED = "type_coerced"
DRIFT_UNKNOWN = "unknown_field"
DRIFT_MISSING = "missing_required"

# Ticket statuses the escalation layer treats as finished work.
CLOSED_STATUSES = frozenset({"done", "closed", "resolved", "cancelled"})


@dataclass(frozen=True)
class DriftNote:
    """One observation about the shape of an incoming payload."""

    kind: str
    field: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "field": self.field, "detail": self.detail}


# -- the synthetic endpoint ---------------------------------------------------


class _Bucket:
    """A token bucket over an injected clock.

    Requests are stamped with the clock's current value. A request is admitted only if
    fewer than ``limit`` stamps fall inside the trailing ``window``. When it is refused, the
    wait it reports is the real time until the oldest stamp ages out, so a client that
    sleeps exactly that long is admitted on its next try and one that sleeps less is not.
    """

    def __init__(self, *, limit: int, window: float, clock: Callable[[], float]) -> None:
        self.limit = limit
        self.window = window
        self.clock = clock
        self.stamps: list[float] = []

    def take(self) -> float | None:
        """Admit a request (returns None) or refuse it (returns seconds to wait)."""
        now = self.clock()
        self.stamps = [t for t in self.stamps if now - t < self.window]
        if len(self.stamps) < self.limit:
            self.stamps.append(now)
            return None
        oldest = min(self.stamps)
        return max(0.0, self.window - (now - oldest))


class SyntheticTicketApi:
    """An offline stand-in for a paginated, rate-limited, drifting ticket endpoint.

    ``issues`` is a list of Jira-shaped dicts. ``drift_from_page`` selects the page index at
    which ``drift_fn`` starts rewriting the payload shape, which is how a test plants a
    mid-stream schema change: the first page arrives in the old shape and the client has
    already committed to it before the new shape shows up.
    """

    def __init__(
        self,
        issues: list[dict[str, Any]],
        *,
        page_size: int = 2,
        limit: int | None = None,
        window: float = 60.0,
        clock: Callable[[], float] | None = None,
        advertise_retry_after: bool = True,
        drift_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        drift_from_page: int = 0,
    ) -> None:
        self.issues = issues
        self.page_size = page_size
        self.advertise_retry_after = advertise_retry_after
        self.drift_fn = drift_fn
        self.drift_from_page = drift_from_page
        self.clock = clock or (lambda: 0.0)
        self.bucket = (
            None if limit is None else _Bucket(limit=limit, window=window, clock=self.clock)
        )
        # Observability, asserted by the tests.
        self.calls = 0
        self.refusals = 0

    def search(self, *, start_at: int = 0, updated_since: str | None = None) -> dict[str, Any]:
        """One page of issues, newest-last, filtered by the incremental cursor."""
        if self.bucket is not None:
            wait = self.bucket.take()
            if wait is not None:
                self.refusals += 1
                raise RateLimitError(
                    f"429 quota exceeded on start_at={start_at}",
                    retry_after=wait if self.advertise_retry_after else None,
                )
        self.calls += 1

        matching = [
            issue
            for issue in self.issues
            if updated_since is None or _updated_of(issue) > updated_since
        ]
        page_index = start_at // self.page_size if self.page_size else 0
        page = matching[start_at : start_at + self.page_size]
        if self.drift_fn is not None and page_index >= self.drift_from_page:
            page = [self.drift_fn(dict(issue)) for issue in page]
        return {
            "startAt": start_at,
            "maxResults": self.page_size,
            "total": len(matching),
            "issues": page,
        }


def _updated_of(issue: dict[str, Any]) -> str:
    """Best-effort read of an issue's update timestamp across known field spellings."""
    fields = issue.get("fields") or {}
    for name in ("updated", "updated_at", "updatedAt"):
        value = fields.get(name)
        if value:
            return str(to_utc_iso(str(value)) or value)
    return ""


# -- the client ---------------------------------------------------------------


def fetch_pages(
    api: SyntheticTicketApi,
    *,
    updated_since: str | None = None,
    sleep: Callable[[float], None],
    attempts: int = 4,
    base: float = 0.5,
    factor: float = 2.0,
    max_delay: float | None = 30.0,
    on_throttle: Callable[[int, float, RateLimitError], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Page through the endpoint, backing off on the 429-equivalent.

    Each page gets its own retry budget. ``Retry-After`` wins when the server offers one,
    because the server knows when its own window rolls; the local exponential schedule is
    the fallback for a server that refuses without saying when to return. Exhausting the
    budget re-raises, so a genuinely wedged quota surfaces instead of returning short.
    """
    start_at = 0
    while True:
        page = _fetch_one(
            api,
            start_at=start_at,
            updated_since=updated_since,
            sleep=sleep,
            attempts=attempts,
            base=base,
            factor=factor,
            max_delay=max_delay,
            on_throttle=on_throttle,
        )
        yield page
        start_at += max(1, int(page.get("maxResults") or 1))
        if start_at >= int(page.get("total") or 0):
            return


def _fetch_one(
    api: SyntheticTicketApi,
    *,
    start_at: int,
    updated_since: str | None,
    sleep: Callable[[float], None],
    attempts: int,
    base: float,
    factor: float,
    max_delay: float | None,
    on_throttle: Callable[[int, float, RateLimitError], None] | None,
) -> dict[str, Any]:
    schedule = backoff_delays(attempts=attempts, base=base, factor=factor, max_delay=max_delay)
    last: RateLimitError | None = None
    for attempt in range(attempts):
        try:
            return api.search(start_at=start_at, updated_since=updated_since)
        except RateLimitError as exc:
            last = exc
            if attempt >= attempts - 1:
                break
            delay = exc.retry_after if exc.retry_after is not None else schedule[attempt]
            if on_throttle is not None:
                on_throttle(attempt + 1, delay, exc)
            sleep(delay)
    assert last is not None
    raise last


# -- schema-drift-tolerant field resolution -----------------------------------


@dataclass(frozen=True)
class FieldSpec:
    """How one canonical field may appear in the wild.

    ``aliases`` are the spellings seen or anticipated, tried in order. ``coerce`` flattens a
    value that may arrive as either a scalar or an object (Jira's ``status`` and ``assignee``
    both do this). ``required`` marks a field the connector cannot proceed without.

    ``object_expected`` records the shape the contract was written against, so the connector
    can tell "this is the normal shape" from "this type changed on me". Without it, a field
    that is SUPPOSED to arrive as an object (an assignee account) would be reported as drift
    on every single record, and a drift signal that fires constantly is not a signal.
    """

    canonical: str
    aliases: tuple[str, ...]
    required: bool = False
    coerce: Callable[[Any], str] | None = None
    object_expected: bool = False


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _named(value: Any) -> str:
    """Flatten ``"Open"`` and ``{"name": "Open"}`` to the same string."""
    if isinstance(value, dict):
        for key in ("name", "value", "displayName", "id"):
            if value.get(key):
                return _text(value[key])
        return ""
    return _text(value)


def _email(value: Any) -> str:
    """Flatten an assignee that may be a bare address or an account object."""
    if isinstance(value, dict):
        for key in ("emailAddress", "email", "email_address", "name"):
            if value.get(key):
                return _text(value[key])
        return ""
    return _text(value)


# The declared contract between this connector and the ticket source. Every alias here is a
# spelling the connector is willing to accept; anything outside it is reported as drift.
TICKET_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("summary", ("summary", "title"), required=True),
    FieldSpec("status", ("status", "state"), required=True, coerce=_named),
    FieldSpec(
        "assignee_email",
        ("assignee", "assignee_email", "assigneeAccount"),
        coerce=_email,
        object_expected=True,
    ),
    FieldSpec("due_date", ("duedate", "due_date", "dueDate")),
    FieldSpec("updated", ("updated", "updated_at", "updatedAt"), required=True),
    FieldSpec(
        "milestone_key",
        ("customfield_milestone", "milestone", "milestone_key"),
    ),
    FieldSpec("account_name", ("account", "account_name", "organization")),
)

# Issue-level identity. Getting this wrong is the expensive drift: if the key is read from a
# renamed field and falls back to empty, every ticket re-lands under a new identity and the
# idempotency ledger silently duplicates the whole board.
KEY_ALIASES: tuple[str, ...] = ("key", "issueKey", "issue_key", "id")


class MissingRequiredField(Exception):
    """A field the connector declared required did not arrive under any known spelling."""

    def __init__(self, field: str) -> None:
        super().__init__(f"required field {field!r} missing under every known alias")
        self.field = field


def resolve_fields(
    raw: dict[str, Any], specs: tuple[FieldSpec, ...] = TICKET_FIELDS
) -> tuple[dict[str, str], dict[str, Any], list[DriftNote]]:
    """Resolve a raw field bag against the declared contract.

    Returns ``(values, unmapped, drift)``:

    - ``values``   canonical name -> resolved string
    - ``unmapped`` fields the contract has no spec for, kept rather than dropped
    - ``drift``    one note per alias hit, coercion, or unrecognized field

    Raises ``MissingRequiredField`` when a required field is absent under every alias, which
    the reader turns into a poison record. That is deliberate: a connector that quietly
    writes an empty string for a field it depends on corrupts the target store, and the
    corruption is discovered much later than the dead-letter row would have been.
    """
    values: dict[str, str] = {}
    drift: list[DriftNote] = []
    consumed: set[str] = set()

    for spec in specs:
        found_under: str | None = None
        raw_value: Any = None
        for alias in spec.aliases:
            if alias in raw:
                found_under = alias
                raw_value = raw[alias]
                break
        if found_under is None:
            if spec.required:
                raise MissingRequiredField(spec.canonical)
            values[spec.canonical] = ""
            continue

        consumed.add(found_under)
        if found_under != spec.aliases[0]:
            drift.append(
                DriftNote(
                    DRIFT_ALIAS,
                    spec.canonical,
                    f"arrived as {found_under!r}, expected {spec.aliases[0]!r}",
                )
            )
        # Drift is a change from the DECLARED shape, not the presence of a nested object.
        if isinstance(raw_value, dict) != spec.object_expected:
            arrived = "object" if isinstance(raw_value, dict) else "scalar"
            declared = "object" if spec.object_expected else "scalar"
            drift.append(
                DriftNote(
                    DRIFT_COERCED,
                    spec.canonical,
                    f"arrived as {arrived}, contract declares {declared}",
                )
            )
        values[spec.canonical] = (spec.coerce or _text)(raw_value)

    unmapped = {k: v for k, v in raw.items() if k not in consumed}
    for name in sorted(unmapped):
        drift.append(
            DriftNote(DRIFT_UNKNOWN, name, "field not in the declared contract, kept unmapped")
        )
    return values, unmapped, drift


def resolve_key(issue: dict[str, Any]) -> tuple[str, list[DriftNote]]:
    """Resolve the issue's identity across key spellings."""
    for alias in KEY_ALIASES:
        if issue.get(alias):
            note = (
                []
                if alias == KEY_ALIASES[0]
                else [
                    DriftNote(
                        DRIFT_ALIAS,
                        "key",
                        f"arrived as {alias!r}, expected {KEY_ALIASES[0]!r}",
                    )
                ]
            )
            return _text(issue[alias]), note
    return "", [DriftNote(DRIFT_MISSING, "key", "no issue key under any known alias")]


# -- the reader ---------------------------------------------------------------


def read_tickets(
    api: SyntheticTicketApi,
    since_iso: str | None = None,
    *,
    sleep: Callable[[float], None] = lambda _d: None,
    attempts: int = 4,
    base: float = 0.5,
    on_throttle: Callable[[int, float, RateLimitError], None] | None = None,
) -> Iterator[RawRecord]:
    """Yield RawRecords for every ticket updated after the cursor.

    As with the other readers, nothing is validated here. A ticket missing a required field
    is emitted with a ``missing_required`` drift note and no timestamp, so the engine's
    normal poison path dead-letters it rather than this reader deciding policy.
    """
    for page in fetch_pages(
        api,
        updated_since=since_iso,
        sleep=sleep,
        attempts=attempts,
        base=base,
        on_throttle=on_throttle,
    ):
        for issue in page.get("issues") or []:
            yield _to_raw(issue)


def _to_raw(issue: dict[str, Any]) -> RawRecord:
    key, key_drift = resolve_key(issue)
    fields = issue.get("fields") or {}
    try:
        values, unmapped, drift = resolve_fields(fields)
    except MissingRequiredField as exc:
        # Emit an addressable but deliberately unusable record: no occurred_at, so
        # normalization dead-letters it with the drift note attached as the explanation.
        return RawRecord(
            source=SOURCE_TICKETS,
            natural_key=key or None,
            payload={
                "kind": "ticket",
                "occurred_at": "",
                "subject": "",
                "contact_email": "",
                "drift": [
                    note.as_dict()
                    for note in [*key_drift, DriftNote(DRIFT_MISSING, exc.field, str(exc))]
                ],
            },
        )

    drift = [*key_drift, *drift]
    assignee = values.get("assignee_email", "")
    account = values.get("account_name", "") or (
        assignee.split("@", 1)[1] if "@" in assignee else ""
    )
    occurred = to_utc_iso(values.get("updated", "")) or values.get("updated", "")
    status = values.get("status", "")
    return RawRecord(
        source=SOURCE_TICKETS,
        natural_key=key or None,
        payload={
            "kind": "ticket",
            "occurred_at": occurred,
            "subject": values.get("summary", ""),
            "body": f"SYNTHETIC ticket {key} status={status}",
            "contact_email": assignee,
            "contact_name": assignee.split("@", 1)[0].replace(".", " ").title() if assignee else "",
            "account_name": account,
            "account_domain": account,
            "extra": {
                "ticket_key": key,
                "status": status,
                "is_open": "0" if status.strip().lower() in CLOSED_STATUSES else "1",
                "due_date": values.get("due_date", ""),
                "milestone_key": values.get("milestone_key", ""),
                "unmapped_json": json.dumps(unmapped, sort_keys=True, default=str),
            },
            "drift": [note.as_dict() for note in drift],
        },
    )


def ticket_projector(store, record) -> None:
    """Write the ticket-shaped projection of a synced ticket record.

    Handed to ``SyncEngine(projectors={SOURCE_TICKETS: ticket_projector})``, it runs inside
    the record's transaction, so the ticket row and the sync bookkeeping commit together.
    """
    extra = record.extra
    store.upsert_ticket(
        ticket_key=extra.get("ticket_key") or record.natural_key,
        summary=record.subject,
        status=extra.get("status", ""),
        is_open=extra.get("is_open", "1") == "1",
        assignee_email=record.contact_email,
        due_date=extra.get("due_date", ""),
        milestone_key=extra.get("milestone_key", ""),
        updated_at=record.occurred_at.isoformat(),
        unmapped_json=extra.get("unmapped_json", "{}"),
    )


def write_ticket_export(path: str, issues: list[dict[str, Any]]) -> None:
    """Write Jira-shaped issues to a JSON fixture the demo loads."""
    from pathlib import Path

    Path(path).write_text(json.dumps(issues, indent=2), encoding="utf-8")


def load_ticket_export(path: str) -> list[dict[str, Any]]:
    from pathlib import Path

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []
