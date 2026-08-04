"""HubSpot CRM v3 connector: contacts and deals, over a transport seam.

This is the fifth integration surface, and the first one shaped against a **real, external,
documented vendor API** rather than an invented one. Nothing here talks to HubSpot by
default. What makes the claim honest is the two-rung ladder the module is built around:

Rung 1 (what runs offline, in CI, today)
    A ``RecordedTransport`` replays hand-authored cassettes in ``fixtures/hubspot/``, whose
    envelopes match the shapes HubSpot's public documentation specifies for
    ``GET /crm/v3/objects/{contacts,deals}``: a ``results`` array of
    ``{id, properties, createdAt, updatedAt, archived}`` and an optional
    ``paging.next.after`` continuation token. The full sync path runs against it.

Rung 2 (what runs only after a human creates an account)
    A ``LiveTransport`` points the SAME client at ``https://api.hubapi.com`` with a private
    app token. See ``RUNBOOK_HUBSPOT.md``. Until that has actually been run, the honest
    claim is rung 1, and the README says so.

The seam is the whole design. ``HubSpotClient`` never learns which transport it holds, so
the pagination, throttling, retry, and record-shaping code exercised by the tests is
byte-for-byte the code that runs against the live account. A mock that tested a different
code path would prove nothing about the live one.

What the connector handles, and why each one exists:

Auth
    A private app token is a bearer credential. It is read from the environment, never a
    file, never an argument default, and it is redacted from every repr and log line this
    module emits.

Cursor pagination
    HubSpot's list endpoints do not page by offset. They return an opaque ``after`` token
    under ``paging.next.after`` and omit ``paging`` entirely on the final page. A client
    that assumes an offset, or that loops until it sees an empty page, is wrong against
    this API. There is also a repeat-token guard, because a server that returns the token
    it was just given would otherwise page forever.

Rate limits
    HubSpot answers a client over quota with HTTP 429. The client honors ``Retry-After``
    when the response carries one and falls back to its own exponential schedule when it
    does not, which is the safe behavior either way. Exhausting the budget RAISES rather
    than returning the pages collected so far, because a connector that silently returns
    two of five pages hands its caller a CRM that looks complete.

Failure taxonomy
    401/403 raise immediately (a wrong credential is not a transient condition and retrying
    it just burns quota), 5xx are transient and retried, other 4xx raise as API errors.

Everything downstream is unchanged: records flow through the existing ``normalize``
validation boundary, the existing idempotency ledger, and the existing dead-letter queue.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import RateLimitError, SyncError, TransientError
from .models import SOURCE_HUBSPOT_CONTACTS, SOURCE_HUBSPOT_DEALS, RawRecord
from .retry import backoff_delays
from .timeutil import to_utc_iso

log = logging.getLogger("sync.hubspot")

DEFAULT_BASE_URL = "https://api.hubapi.com"
TOKEN_ENV_VAR = "HUBSPOT_PRIVATE_APP_TOKEN"
BASE_URL_ENV_VAR = "HUBSPOT_BASE_URL"

# HubSpot caps a page of CRM objects at 100.
MAX_PAGE_SIZE = 100

CONTACT_PROPERTIES: tuple[str, ...] = (
    "email",
    "firstname",
    "lastname",
    "company",
    "jobtitle",
    "lifecyclestage",
    "lastmodifieddate",
)

DEAL_PROPERTIES: tuple[str, ...] = (
    "dealname",
    "dealstage",
    "pipeline",
    "amount",
    "closedate",
    "hubspot_owner_id",
    "hs_lastmodifieddate",
)

# Deal stages that mean the deal is finished, either way. HubSpot's default pipeline uses
# these two internal ids; a custom pipeline can use anything, which is called out in the
# README limitations rather than guessed at here.
CLOSED_DEAL_STAGES = frozenset({"closedwon", "closedlost"})


class HubSpotError(SyncError):
    """Base class for connector-level failures that are not per-record data problems."""


class HubSpotAuthError(HubSpotError):
    """401 or 403. A bad or unscoped token, which no amount of retrying will fix."""


class HubSpotApiError(HubSpotError):
    """A non-2xx response that is neither an auth failure nor a throttle."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


class CassetteMiss(HubSpotError):
    """The recorded transport was asked for a request no cassette covers.

    This is deliberately loud. A mock that answered an unrecorded request with an empty page
    would let a broken pagination change pass its tests while quietly syncing nothing.
    """


@dataclass(frozen=True)
class HubSpotResponse:
    """One HTTP exchange, reduced to what the client actually reasons about."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        """Case-insensitive header lookup, because HTTP header casing is not guaranteed."""
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None


# A transport takes a path and query params and returns a response. That is the entire
# contract, and it is why the recorded and live transports are interchangeable.
Transport = Callable[[str, "Mapping[str, str]"], HubSpotResponse]


# -- the recorded (offline) transport -----------------------------------------


@dataclass
class RecordedExchange:
    """One recorded request/response pair from a cassette file."""

    path: str
    params: dict[str, str]
    status: int
    headers: dict[str, str]
    body: dict[str, Any]
    consumed: bool = False

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RecordedExchange:
        return cls(
            path=str(raw["path"]),
            params={str(k): str(v) for k, v in (raw.get("params") or {}).items()},
            status=int(raw.get("status", 200)),
            headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
            body=dict(raw.get("body") or {}),
        )


class RecordedTransport:
    """Replays a cassette of hand-authored, HubSpot-shaped exchanges.

    Matching is ordered and consuming: a request is answered by the FIRST unconsumed
    exchange whose path matches and whose recorded params are all present in the request.
    Ordered consumption is what lets one cassette script a 429 followed by the 200 for the
    same page, which is exactly the sequence the backoff path needs to be tested against.

    The subset check on params is a real assertion, not decoration: if the client stops
    sending ``properties``, or sends a different ``after`` token than the recording was
    written for, the cassette refuses the request instead of silently answering it.
    """

    def __init__(self, exchanges: Sequence[RecordedExchange], *, name: str = "cassette") -> None:
        self.exchanges = list(exchanges)
        self.name = name
        self.requests: list[tuple[str, dict[str, str]]] = []

    @classmethod
    def from_file(cls, path: str | Path) -> RecordedTransport:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        exchanges = [RecordedExchange.from_dict(item) for item in data["exchanges"]]
        return cls(exchanges, name=str(data.get("name") or Path(path).stem))

    @classmethod
    def from_dir(cls, directory: str | Path, *names: str) -> RecordedTransport:
        """Concatenate several cassettes, in the order named, into one transport."""
        base = Path(directory)
        exchanges: list[RecordedExchange] = []
        for name in names:
            data = json.loads((base / f"{name}.json").read_text(encoding="utf-8"))
            exchanges.extend(RecordedExchange.from_dict(item) for item in data["exchanges"])
        return cls(exchanges, name="+".join(names))

    def __call__(self, path: str, params: Mapping[str, str]) -> HubSpotResponse:
        self.requests.append((path, dict(params)))
        for exchange in self.exchanges:
            if exchange.consumed or exchange.path != path:
                continue
            if any(params.get(k) != v for k, v in exchange.params.items()):
                continue
            exchange.consumed = True
            return HubSpotResponse(
                status=exchange.status, headers=exchange.headers, body=exchange.body
            )
        raise CassetteMiss(
            f"cassette {self.name!r} has no unconsumed exchange for "
            f"{path} with params {dict(params)!r}"
        )

    @property
    def unconsumed(self) -> list[RecordedExchange]:
        """Recorded exchanges the run never asked for. A test can assert this is empty."""
        return [e for e in self.exchanges if not e.consumed]


# -- the live transport -------------------------------------------------------


class LiveTransport:
    """Talks to the real HubSpot API. Constructed only when a token is present.

    The token is read from the environment and is never accepted as a literal default, never
    written to a file by this code, and never included in a repr or a log line. The only
    place it appears is the ``Authorization`` header of an outbound request.
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
        opener: Callable[[urllib.request.Request, float], Any] | None = None,
    ) -> None:
        resolved = token if token is not None else os.environ.get(TOKEN_ENV_VAR, "")
        if not resolved.strip():
            raise HubSpotAuthError(
                f"no HubSpot token: set {TOKEN_ENV_VAR} (see RUNBOOK_HUBSPOT.md). "
                "Live mode is refused rather than silently falling back to fixtures."
            )
        self._token = resolved.strip()
        self.base_url = (base_url or os.environ.get(BASE_URL_ENV_VAR) or DEFAULT_BASE_URL).rstrip(
            "/"
        )
        self.timeout = timeout
        self._opener = opener or (lambda req, timeout: urllib.request.urlopen(req, timeout=timeout))

    def __repr__(self) -> str:
        # Redaction is not cosmetic: a repr lands in tracebacks, logs, and CI output.
        return f"LiveTransport(base_url={self.base_url!r}, token=<redacted>)"

    def build_request(self, path: str, params: Mapping[str, str]) -> urllib.request.Request:
        """Build the outbound request. Exposed so the header shape is testable offline."""
        query = urllib.parse.urlencode(dict(params))
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        return urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    def __call__(self, path: str, params: Mapping[str, str]) -> HubSpotResponse:
        request = self.build_request(path, params)
        try:
            with self._opener(request, self.timeout) as response:
                return HubSpotResponse(
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                    body=_json_or_empty(response.read()),
                )
        except urllib.error.HTTPError as exc:
            # A 429 or a 5xx arrives here, not on the success path. It is a real response
            # with real headers, so it is returned as one and classified by the client.
            return HubSpotResponse(
                status=int(exc.code),
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=_json_or_empty(exc.read()),
            )
        except urllib.error.URLError as exc:
            # A refused connection or a DNS failure is exactly the retryable class.
            raise TransientError(f"network failure talking to HubSpot: {exc.reason}") from exc


def _json_or_empty(payload: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# -- the client ---------------------------------------------------------------


class HubSpotClient:
    """Pages a HubSpot CRM object type, surviving throttles, over any transport."""

    def __init__(
        self,
        transport: Transport,
        *,
        sleep: Callable[[float], None],
        page_size: int = MAX_PAGE_SIZE,
        attempts: int = 4,
        base: float = 0.5,
        factor: float = 2.0,
        max_delay: float | None = 30.0,
        on_throttle: Callable[[int, float, RateLimitError], None] | None = None,
    ) -> None:
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be 1..{MAX_PAGE_SIZE}, got {page_size}")
        self.transport = transport
        self.sleep = sleep
        self.page_size = page_size
        self.attempts = attempts
        self.base = base
        self.factor = factor
        self.max_delay = max_delay
        self.on_throttle = on_throttle
        self.pages_fetched = 0
        self.throttles = 0

    def iter_pages(
        self, object_type: str, *, properties: Sequence[str], associations: Sequence[str] = ()
    ) -> Iterator[dict[str, Any]]:
        """Yield each page of ``object_type``, following ``paging.next.after`` to the end.

        The loop terminates on the ABSENCE of ``paging.next.after``, which is how this API
        signals the last page. It deliberately does not stop on an empty ``results`` array:
        a page can legitimately come back empty while a continuation token still points at
        more records, and stopping there would truncate the sync.
        """
        path = f"/crm/v3/objects/{object_type}"
        after: str | None = None
        seen_tokens: set[str] = set()
        while True:
            params: dict[str, str] = {
                "limit": str(self.page_size),
                "properties": ",".join(properties),
            }
            if associations:
                params["associations"] = ",".join(associations)
            if after is not None:
                params["after"] = after
            page = self._get_with_retry(path, params)
            self.pages_fetched += 1
            yield page

            after = _next_after(page)
            if after is None:
                return
            if after in seen_tokens:
                # A server that hands back the token it was just given would otherwise
                # page forever. Surfacing it beats an infinite loop in a nightly job.
                raise HubSpotApiError(
                    f"pagination token {after!r} repeated on {path}; refusing to loop",
                    status=200,
                )
            seen_tokens.add(after)

    def _get_with_retry(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        schedule = backoff_delays(
            attempts=self.attempts, base=self.base, factor=self.factor, max_delay=self.max_delay
        )
        last: RateLimitError | TransientError | None = None
        for attempt in range(self.attempts):
            try:
                return self._get_once(path, params)
            except (RateLimitError, TransientError) as exc:
                last = exc
                if attempt >= self.attempts - 1:
                    break
                retry_after = getattr(exc, "retry_after", None)
                delay = retry_after if retry_after is not None else schedule[attempt]
                self.throttles += 1
                if self.on_throttle is not None and isinstance(exc, RateLimitError):
                    self.on_throttle(attempt + 1, delay, exc)
                log.warning(
                    "hubspot %s backing off %.3fs after attempt %d: %s",
                    path, delay, attempt + 1, exc,
                )
                self.sleep(delay)
        assert last is not None
        raise last

    def _get_once(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        response = self.transport(path, params)
        status = response.status
        if 200 <= status < 300:
            return response.body
        if status == 429:
            raise RateLimitError(
                f"429 from HubSpot on {path}: {response.body.get('message', 'rate limited')}",
                retry_after=_retry_after_seconds(response),
            )
        if status in (401, 403):
            # Not retryable on purpose. Retrying a rejected credential cannot succeed, and
            # it spends quota that the rest of the sync needs.
            raise HubSpotAuthError(
                f"{status} from HubSpot on {path}: "
                f"{response.body.get('message', 'authentication failed')}. "
                f"Check the private app token and its CRM scopes."
            )
        if status >= 500:
            raise TransientError(f"{status} from HubSpot on {path}; retryable")
        raise HubSpotApiError(
            f"{status} from HubSpot on {path}: {response.body.get('message', 'request failed')}",
            status=status,
        )


def _retry_after_seconds(response: HubSpotResponse) -> float | None:
    """Read the server's own wait instruction, when it offers one.

    ``Retry-After`` is honored when present. It is NOT assumed to be present: a 429 without
    one falls through to the client's exponential schedule, which is why the fallback branch
    exists and is tested.
    """
    raw = response.header("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _next_after(page: Mapping[str, Any]) -> str | None:
    paging = page.get("paging")
    if not isinstance(paging, dict):
        return None
    nxt = paging.get("next")
    if not isinstance(nxt, dict):
        return None
    after = nxt.get("after")
    return str(after) if after not in (None, "") else None


# -- readers: HubSpot objects to RawRecords -----------------------------------


def read_hubspot_contacts(
    client: HubSpotClient, since_iso: str | None = None
) -> Iterator[RawRecord]:
    """Yield a RawRecord per HubSpot contact updated after the cursor.

    The cursor filter is applied CLIENT-SIDE, and that is a real limitation rather than an
    oversight. HubSpot's ``GET /crm/v3/objects/{type}`` list endpoint takes no
    last-modified filter, so an incremental run still pages the whole object set and the
    cursor saves writes rather than API calls. The production answer is the search endpoint
    with an ``hs_lastmodifieddate`` GTE filter, which is a POST with a different body shape
    and is not implemented here. The limitation is repeated in the README.

    Nothing is validated here. A contact with no email address is emitted as-is and the
    engine's existing poison path dead-letters it, so the reader never decides policy.
    """
    for page in client.iter_pages("contacts", properties=CONTACT_PROPERTIES):
        for obj in page.get("results") or []:
            record = _contact_to_raw(obj)
            if _after_cursor(record, since_iso):
                yield record


def read_hubspot_deals(
    client: HubSpotClient,
    since_iso: str | None = None,
    *,
    resolve_contact_email: Callable[[str], str] | None = None,
) -> Iterator[RawRecord]:
    """Yield a RawRecord per HubSpot deal updated after the cursor.

    ``resolve_contact_email`` turns an associated contact id into an email address. It
    exists because HubSpot returns associations as bare object ids, so a deal cannot be
    attached to a person without a second lookup. Real connectors resolve that against
    already-synced data, which is what the demo does: contacts sync first, and the resolver
    reads the local store.

    A deal whose associated contact has not landed yet resolves to no email and is
    dead-lettered by the normal poison path. That is the correct outcome and not a bug: it
    makes the ordering dependency between two object types visible instead of writing a
    deal attached to nobody.
    """
    for page in client.iter_pages("deals", properties=DEAL_PROPERTIES, associations=("contacts",)):
        for obj in page.get("results") or []:
            record = _deal_to_raw(obj, resolve_contact_email)
            if _after_cursor(record, since_iso):
                yield record


def _after_cursor(record: RawRecord, since_iso: str | None) -> bool:
    if since_iso is None:
        return True
    occurred = str(record.payload.get("occurred_at") or "")
    # A record with no parseable timestamp is never filtered out. It has to reach the engine
    # so it can be dead-lettered with an explanation, rather than vanishing at the reader.
    if not occurred:
        return True
    return occurred > since_iso


def _prop(obj: Mapping[str, Any], name: str) -> str:
    properties = obj.get("properties") or {}
    value = properties.get(name)
    return "" if value is None else str(value).strip()


def _updated_at(obj: Mapping[str, Any], *property_fallbacks: str) -> str:
    """Prefer the envelope's ``updatedAt``, fall back to the modified-date property."""
    for candidate in (obj.get("updatedAt"), *(_prop(obj, name) for name in property_fallbacks)):
        if candidate:
            return to_utc_iso(str(candidate)) or ""
    return ""


def _contact_to_raw(obj: Mapping[str, Any]) -> RawRecord:
    object_id = str(obj.get("id") or "").strip()
    email = _prop(obj, "email")
    first, last = _prop(obj, "firstname"), _prop(obj, "lastname")
    name = " ".join(part for part in (first, last) if part)
    company = _prop(obj, "company")
    domain = email.split("@", 1)[1] if "@" in email else ""
    return RawRecord(
        source=SOURCE_HUBSPOT_CONTACTS,
        natural_key=object_id or None,
        payload={
            "kind": "hubspot_contact",
            "occurred_at": _updated_at(obj, "lastmodifieddate"),
            "subject": f"Contact {name or email or object_id}",
            "body": f"SYNTHETIC HubSpot contact {object_id} "
                    f"lifecyclestage={_prop(obj, 'lifecyclestage') or 'unset'}",
            "contact_email": email,
            "contact_name": name,
            "account_name": company or domain,
            "account_domain": domain,
            "extra": {
                "hubspot_id": object_id,
                "lifecyclestage": _prop(obj, "lifecyclestage"),
                "jobtitle": _prop(obj, "jobtitle"),
                "archived": "1" if obj.get("archived") else "0",
            },
        },
    )


def _deal_to_raw(
    obj: Mapping[str, Any], resolve_contact_email: Callable[[str], str] | None
) -> RawRecord:
    object_id = str(obj.get("id") or "").strip()
    associated = _associated_ids(obj, "contacts")
    email = ""
    for contact_id in associated:
        if resolve_contact_email is None:
            break
        email = resolve_contact_email(contact_id)
        if email:
            break
    stage = _prop(obj, "dealstage")
    name = _prop(obj, "dealname")
    return RawRecord(
        source=SOURCE_HUBSPOT_DEALS,
        natural_key=object_id or None,
        payload={
            "kind": "hubspot_deal",
            "occurred_at": _updated_at(obj, "hs_lastmodifieddate"),
            "subject": f"Deal {name or object_id}",
            "body": f"SYNTHETIC HubSpot deal {object_id} stage={stage or 'unset'}",
            "contact_email": email,
            "contact_name": email.split("@", 1)[0].replace(".", " ").title() if email else "",
            "account_name": email.split("@", 1)[1] if "@" in email else "",
            "account_domain": email.split("@", 1)[1] if "@" in email else "",
            "extra": {
                "hubspot_id": object_id,
                "dealname": name,
                "dealstage": stage,
                "pipeline": _prop(obj, "pipeline"),
                "amount": _prop(obj, "amount"),
                "closedate": _prop(obj, "closedate"),
                "owner_id": _prop(obj, "hubspot_owner_id"),
                "is_open": "0" if stage.strip().lower() in CLOSED_DEAL_STAGES else "1",
                "associated_contact_ids": ",".join(associated),
            },
        },
    )


def _associated_ids(obj: Mapping[str, Any], association: str) -> list[str]:
    """Read ``associations.<type>.results[].id`` defensively.

    HubSpot only includes an association block when the request asked for it and the object
    actually has one, so every level of this is optional.
    """
    associations = obj.get("associations")
    if not isinstance(associations, dict):
        return []
    block = associations.get(association)
    if not isinstance(block, dict):
        return []
    results = block.get("results")
    if not isinstance(results, list):
        return []
    return [str(item.get("id")) for item in results if isinstance(item, dict) and item.get("id")]


# -- the projector ------------------------------------------------------------


def deal_projector(store, record) -> None:
    """Write the deal-shaped projection of a synced deal record.

    Handed to ``SyncEngine(projectors={SOURCE_HUBSPOT_DEALS: deal_projector})``, it runs
    inside the record's transaction, so the deal row commits with its activity and ledger
    rows or not at all.
    """
    extra = record.extra
    store.upsert_deal(
        deal_id=extra.get("hubspot_id") or record.natural_key,
        name=extra.get("dealname", ""),
        stage=extra.get("dealstage", ""),
        pipeline=extra.get("pipeline", ""),
        amount=extra.get("amount", ""),
        close_date=extra.get("closedate", ""),
        owner_email=extra.get("owner_id", ""),
        contact_email=record.contact_email,
        is_open=extra.get("is_open", "1") == "1",
        updated_at=record.occurred_at.isoformat(),
        unmapped_json=json.dumps(
            {"associated_contact_ids": extra.get("associated_contact_ids", "")}, sort_keys=True
        ),
    )


# -- wiring helpers -----------------------------------------------------------


def contact_email_resolver(store) -> Callable[[str], str]:
    """Resolve a HubSpot contact id to an email using contacts already in the store.

    This is the local-lookup half of the association story: the deal sync depends on the
    contact sync having run first, and this is where that dependency lives.
    """

    def resolve(contact_id: str) -> str:
        row = store.get_activity(SOURCE_HUBSPOT_CONTACTS, str(contact_id))
        if row is None:
            return ""
        contact = store.conn.execute(
            "SELECT email FROM contacts WHERE contact_id = ?", (row["contact_id"],)
        ).fetchone()
        return str(contact["email"]) if contact else ""

    return resolve


def _relative(path: str | Path) -> str:
    """Render a path relative to the working directory, falling back to its name."""
    candidate = Path(path)
    try:
        return str(candidate.relative_to(Path.cwd()))
    except ValueError:
        return candidate.name


def build_client(
    *,
    live: bool,
    fixtures_dir: str | Path,
    sleep: Callable[[float], None],
    cassettes: Sequence[str],
    page_size: int = MAX_PAGE_SIZE,
    on_throttle: Callable[[int, float, RateLimitError], None] | None = None,
) -> HubSpotClient:
    """Build a client over either transport. The client itself is identical either way."""
    transport: Transport
    if live:
        transport = LiveTransport()
        log.info("hubspot: LIVE mode against %s", transport.base_url)
    else:
        transport = RecordedTransport.from_dir(fixtures_dir, *cassettes)
        # Logged relative to the working directory on purpose. An absolute path in a
        # committed demo transcript publishes the layout of whatever machine produced it.
        log.info("hubspot: recorded mode from %s", _relative(fixtures_dir))
    return HubSpotClient(
        transport, sleep=sleep, page_size=page_size, on_throttle=on_throttle
    )
