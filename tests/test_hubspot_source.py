"""The HubSpot connector: auth shape, cursor pagination, throttling, and the sync path.

Every test here runs offline against the recorded cassettes in ``fixtures/hubspot/``. The
point of the transport seam is that the client under test is the same object the live run
uses, so what these tests exercise is the live code path with a different socket underneath.

Each planted condition is paired with a control that can fail. The 429 tests assert the
client waited the server's number AND that a server which offers no number gets the local
exponential schedule instead. The idempotency test is paired with a first run that really
did write, so "nothing changed" is evidence rather than an inert second pass.
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from integration_sync.crm_store import CrmStore
from integration_sync.errors import RateLimitError, TransientError
from integration_sync.hubspot_source import (
    CONTACT_PROPERTIES,
    DEAL_PROPERTIES,
    TOKEN_ENV_VAR,
    CassetteMiss,
    HubSpotApiError,
    HubSpotAuthError,
    HubSpotClient,
    HubSpotResponse,
    LiveTransport,
    RecordedExchange,
    RecordedTransport,
    contact_email_resolver,
    deal_projector,
    read_hubspot_contacts,
    read_hubspot_deals,
)
from integration_sync.models import SOURCE_HUBSPOT_CONTACTS, SOURCE_HUBSPOT_DEALS
from integration_sync.sync_engine import SyncEngine

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "hubspot"


class FakeSleep:
    """Records what the client waited for. Nothing here advances real time."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


def contacts_client(sleep: FakeSleep, **kwargs) -> HubSpotClient:
    transport = RecordedTransport.from_dir(FIXTURES, "contacts")
    return HubSpotClient(transport, sleep=sleep, page_size=2, **kwargs)


def deals_client(sleep: FakeSleep, **kwargs) -> HubSpotClient:
    transport = RecordedTransport.from_dir(FIXTURES, "deals")
    return HubSpotClient(transport, sleep=sleep, page_size=2, **kwargs)


def scripted(*exchanges: dict) -> RecordedTransport:
    """Build a one-off transport from inline exchanges, for the error-taxonomy tests."""
    return RecordedTransport([RecordedExchange.from_dict(e) for e in exchanges], name="scripted")


def page(results: list[dict], after: str | None = None) -> dict:
    body: dict = {"results": results}
    if after is not None:
        body["paging"] = {"next": {"after": after, "link": f"https://x.example?after={after}"}}
    return body


# -- auth: the header shape, verified without a network call ------------------


def test_live_transport_sends_a_bearer_token(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV_VAR, "pat-na1-SYNTHETIC-TOKEN")
    request = LiveTransport().build_request("/crm/v3/objects/contacts", {"limit": "100"})
    assert request.get_header("Authorization") == "Bearer pat-na1-SYNTHETIC-TOKEN"
    assert request.get_header("Accept") == "application/json"
    assert request.get_full_url() == (
        "https://api.hubapi.com/crm/v3/objects/contacts?limit=100"
    )
    assert request.get_method() == "GET"


def test_live_transport_refuses_to_start_without_a_token(monkeypatch):
    # The failure mode this prevents: a live run silently falling back to fixtures and
    # reporting a successful sync of data that never came from the account.
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    with pytest.raises(HubSpotAuthError):
        LiveTransport()


def test_the_token_is_redacted_from_the_repr(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV_VAR, "pat-na1-SYNTHETIC-TOKEN")
    text = repr(LiveTransport())
    assert "pat-na1-SYNTHETIC-TOKEN" not in text
    assert "redacted" in text


def test_base_url_is_overridable_for_a_sandbox_or_a_proxy(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV_VAR, "pat-na1-SYNTHETIC-TOKEN")
    transport = LiveTransport(base_url="https://api.hubapi.example/")
    request = transport.build_request("/crm/v3/objects/deals", {})
    assert request.get_full_url() == "https://api.hubapi.example/crm/v3/objects/deals"


def test_a_network_failure_is_classified_transient(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV_VAR, "pat-na1-SYNTHETIC-TOKEN")

    def refuse(_request, _timeout):
        raise urllib.error.URLError("connection refused")

    transport = LiveTransport(opener=refuse)
    with pytest.raises(TransientError):
        transport("/crm/v3/objects/contacts", {})


# -- cursor pagination --------------------------------------------------------


def test_pagination_follows_the_after_token_to_the_last_page():
    sleep = FakeSleep()
    client = contacts_client(sleep)
    pages = list(client.iter_pages("contacts", properties=CONTACT_PROPERTIES))

    assert [len(p["results"]) for p in pages] == [2, 2, 1]
    assert client.pages_fetched == 3
    # The final page carries no paging block at all. That absence is the terminator.
    assert "paging" not in pages[-1]
    assert client.transport.unconsumed == []


def test_the_after_token_is_sent_back_verbatim_on_the_next_request():
    sleep = FakeSleep()
    client = contacts_client(sleep)
    list(client.iter_pages("contacts", properties=CONTACT_PROPERTIES))
    afters = [params.get("after") for _path, params in client.transport.requests]
    assert afters == [None, "702", "702", "704"]  # the repeated 702 is the retry after the 429


def test_pagination_does_not_stop_on_an_empty_page_that_still_has_a_token():
    # A page can be empty while more records remain. Stopping there truncates the sync.
    transport = scripted(
        {"path": "/crm/v3/objects/contacts", "params": {}, "body": page([], after="A")},
        {"path": "/crm/v3/objects/contacts", "params": {"after": "A"},
         "body": page([{"id": "1"}])},
    )
    client = HubSpotClient(transport, sleep=FakeSleep(), page_size=2)
    pages = list(client.iter_pages("contacts", properties=("email",)))
    assert [len(p["results"]) for p in pages] == [0, 1]


def test_a_repeated_pagination_token_raises_instead_of_looping_forever():
    transport = scripted(
        {"path": "/crm/v3/objects/contacts", "params": {}, "body": page([{"id": "1"}], after="A")},
        {"path": "/crm/v3/objects/contacts", "params": {"after": "A"},
         "body": page([{"id": "2"}], after="A")},
    )
    client = HubSpotClient(transport, sleep=FakeSleep(), page_size=2)
    with pytest.raises(HubSpotApiError, match="repeated"):
        list(client.iter_pages("contacts", properties=("email",)))


def test_the_deals_request_asks_for_its_properties_and_its_associations():
    # Associations are not returned unless the request asks for them, so a dropped param
    # here would silently detach every deal from its contact.
    client = deals_client(FakeSleep())
    list(client.iter_pages("deals", properties=DEAL_PROPERTIES, associations=("contacts",)))
    _path, params = client.transport.requests[0]
    assert params["associations"] == "contacts"
    assert params["properties"].split(",") == list(DEAL_PROPERTIES)


def test_page_size_is_bounded_by_the_documented_maximum():
    with pytest.raises(ValueError):
        HubSpotClient(scripted(), sleep=FakeSleep(), page_size=250)


# -- rate limiting ------------------------------------------------------------


def test_the_client_waits_the_retry_after_the_server_asked_for():
    sleep = FakeSleep()
    client = contacts_client(sleep)
    pages = list(client.iter_pages("contacts", properties=CONTACT_PROPERTIES))

    assert [len(p["results"]) for p in pages] == [2, 2, 1]  # it recovered and got everything
    assert sleep.slept == [10.0]  # the server's number, not the local 0.5s schedule
    assert client.throttles == 1


def test_a_429_without_a_retry_after_falls_back_to_the_local_schedule():
    # The control for the test above: strip the header and the local schedule must take over.
    body = {"status": "error", "errorType": "RATE_LIMIT", "message": "SYNTHETIC throttle"}
    transport = scripted(
        {"path": "/crm/v3/objects/contacts", "params": {}, "status": 429, "body": body},
        {"path": "/crm/v3/objects/contacts", "params": {}, "status": 429, "body": body},
        {"path": "/crm/v3/objects/contacts", "params": {}, "body": page([{"id": "1"}])},
    )
    sleep = FakeSleep()
    client = HubSpotClient(transport, sleep=sleep, page_size=2, base=0.5, factor=2.0)
    list(client.iter_pages("contacts", properties=("email",)))
    assert sleep.slept == [pytest.approx(0.5), pytest.approx(1.0)]


def test_an_unparseable_retry_after_is_ignored_rather_than_trusted():
    body = {"status": "error", "errorType": "RATE_LIMIT"}
    transport = scripted(
        {"path": "/crm/v3/objects/contacts", "params": {}, "status": 429,
         "headers": {"Retry-After": "soon"}, "body": body},
        {"path": "/crm/v3/objects/contacts", "params": {}, "body": page([{"id": "1"}])},
    )
    sleep = FakeSleep()
    client = HubSpotClient(transport, sleep=sleep, page_size=2, base=0.5)
    list(client.iter_pages("contacts", properties=("email",)))
    assert sleep.slept == [pytest.approx(0.5)]


def test_an_exhausted_retry_budget_raises_rather_than_returning_a_short_page_set():
    # A connector that returns two of five pages hands its caller a CRM that looks complete.
    body = {"status": "error", "errorType": "RATE_LIMIT"}
    transport = scripted(
        *[
            {"path": "/crm/v3/objects/contacts", "params": {}, "status": 429,
             "headers": {"Retry-After": "1"}, "body": body}
            for _ in range(3)
        ]
    )
    sleep = FakeSleep()
    client = HubSpotClient(transport, sleep=sleep, page_size=2, attempts=3)
    with pytest.raises(RateLimitError):
        list(client.iter_pages("contacts", properties=("email",)))
    assert sleep.slept == [1.0, 1.0]  # exactly attempts-1 waits, then it gave up loudly


def test_the_throttle_callback_sees_the_wait_and_the_error():
    seen: list[tuple[int, float]] = []
    sleep = FakeSleep()
    client = contacts_client(sleep, on_throttle=lambda a, d, e: seen.append((a, d)))
    list(client.iter_pages("contacts", properties=CONTACT_PROPERTIES))
    assert seen == [(1, 10.0)]


# -- the rest of the failure taxonomy ----------------------------------------


def test_a_401_raises_immediately_and_is_never_retried():
    # Retrying a rejected credential cannot succeed and spends quota the sync needs.
    transport = scripted(
        {"path": "/crm/v3/objects/contacts", "params": {}, "status": 401,
         "body": {"message": "Authentication credentials not found"}}
    )
    sleep = FakeSleep()
    client = HubSpotClient(transport, sleep=sleep, page_size=2)
    with pytest.raises(HubSpotAuthError):
        list(client.iter_pages("contacts", properties=("email",)))
    assert sleep.slept == []
    assert len(transport.requests) == 1


def test_a_500_is_transient_and_is_retried():
    transport = scripted(
        {"path": "/crm/v3/objects/contacts", "params": {}, "status": 500, "body": {}},
        {"path": "/crm/v3/objects/contacts", "params": {}, "body": page([{"id": "1"}])},
    )
    sleep = FakeSleep()
    client = HubSpotClient(transport, sleep=sleep, page_size=2, base=0.25)
    pages = list(client.iter_pages("contacts", properties=("email",)))
    assert len(pages) == 1
    assert sleep.slept == [pytest.approx(0.25)]


def test_a_400_raises_as_an_api_error_without_retrying():
    transport = scripted(
        {"path": "/crm/v3/objects/contacts", "params": {}, "status": 400,
         "body": {"message": "Invalid property"}}
    )
    client = HubSpotClient(transport, sleep=FakeSleep(), page_size=2)
    with pytest.raises(HubSpotApiError) as caught:
        list(client.iter_pages("contacts", properties=("email",)))
    assert caught.value.status == 400


def test_header_lookup_is_case_insensitive():
    response = HubSpotResponse(status=429, headers={"retry-after": "7"})
    assert response.header("Retry-After") == "7"


# -- the cassette is a real check, not a rubber stamp -------------------------


def test_an_unrecorded_request_misses_loudly():
    # A mock that answered anything with an empty page would let a broken pagination
    # change pass its tests while syncing nothing.
    transport = RecordedTransport.from_dir(FIXTURES, "contacts")
    with pytest.raises(CassetteMiss):
        transport("/crm/v3/objects/companies", {"limit": "2"})


def test_a_client_with_the_wrong_page_size_misses_the_cassette():
    client = HubSpotClient(RecordedTransport.from_dir(FIXTURES, "contacts"), sleep=FakeSleep())
    with pytest.raises(CassetteMiss):
        list(client.iter_pages("contacts", properties=CONTACT_PROPERTIES))


def test_dropping_the_properties_param_misses_the_cassette():
    client = contacts_client(FakeSleep())
    with pytest.raises(CassetteMiss):
        list(client.iter_pages("contacts", properties=()))


# -- record shaping -----------------------------------------------------------


def test_a_contact_becomes_an_addressable_record():
    records = list(read_hubspot_contacts(contacts_client(FakeSleep())))
    assert [r.natural_key for r in records] == ["701", "702", "703", "704", "705"]
    first = records[0].payload
    assert first["contact_email"] == "dana.rivers@zeltrovan-health.example"
    assert first["contact_name"] == "Dana Rivers"
    assert first["account_name"] == "Zeltrovan Health"
    assert first["occurred_at"] == "2026-04-02T09:15:00+00:00"
    assert first["extra"]["lifecyclestage"] == "customer"


def test_the_cursor_filters_records_the_list_endpoint_cannot_filter_server_side():
    client = contacts_client(FakeSleep())
    records = list(read_hubspot_contacts(client, "2026-04-04T08:30:00+00:00"))
    assert [r.natural_key for r in records] == ["704", "705"]
    # The honest cost of a client-side cursor: every page was still fetched.
    assert client.pages_fetched == 3


def test_a_deal_resolves_its_contact_through_the_association_block():
    emails = {
        "701": "dana.rivers@zeltrovan-health.example",
        "703": "sam.okafor@drenvalik-labs.example",
    }
    records = list(
        read_hubspot_deals(
            deals_client(FakeSleep()), resolve_contact_email=lambda i: emails.get(i, "")
        )
    )
    assert [r.natural_key for r in records] == ["8801", "8802", "8803"]
    assert records[0].payload["contact_email"] == "dana.rivers@zeltrovan-health.example"
    assert records[0].payload["extra"]["is_open"] == "1"
    assert records[1].payload["extra"]["is_open"] == "0"  # closedwon
    # 8803 points at a contact that does not exist, so it resolves to nothing.
    assert records[2].payload["contact_email"] == ""


def test_a_deal_amount_survives_as_an_exact_string():
    # Money through a binary float is how a total quietly stops reconciling.
    records = list(
        read_hubspot_deals(
            deals_client(FakeSleep()), resolve_contact_email=lambda _i: "x@y.example"
        )
    )
    assert records[1].payload["extra"]["amount"] == "12500.50"


def test_a_missing_association_block_is_survived_not_crashed_on():
    transport = scripted(
        {"path": "/crm/v3/objects/deals", "params": {},
         "body": page([{"id": "1", "properties": {"dealname": "SYNTHETIC"},
                        "updatedAt": "2026-04-01T00:00:00Z"}])}
    )
    client = HubSpotClient(transport, sleep=FakeSleep(), page_size=2)
    records = list(read_hubspot_deals(client, resolve_contact_email=lambda _i: "x@y.example"))
    assert records[0].payload["contact_email"] == ""


# -- end to end through the existing sync engine ------------------------------


def sync_contacts(store: CrmStore, engine: SyncEngine, cursor_advance: bool = True):
    def reader(since: str | None):
        return read_hubspot_contacts(contacts_client(FakeSleep()), since)

    return engine.sync_source(SOURCE_HUBSPOT_CONTACTS, reader, advance_cursor=cursor_advance)


def test_a_first_contact_sync_creates_every_valid_record_and_dead_letters_the_rest(store):
    engine = SyncEngine(store, sleep=lambda _d: None)
    stats = sync_contacts(store, engine)

    assert stats.created == 4
    assert stats.dead_lettered == 1  # contact 704 has no email address
    assert stats.dead_letter_keys == ["704"]
    assert store.count("activities") == 4
    rows = store.list_dead_letter()
    assert rows[0]["source"] == SOURCE_HUBSPOT_CONTACTS
    assert "contact_email" in rows[0]["error"]


def test_re_running_the_same_contact_sync_changes_nothing(store):
    engine = SyncEngine(store, sleep=lambda _d: None)
    first = sync_contacts(store, engine, cursor_advance=False)
    assert first.created == 4  # the control: the first pass really did write

    before = store.snapshot()
    second = sync_contacts(store, engine, cursor_advance=False)
    assert second.created == 0
    assert second.unchanged == 4
    assert store.snapshot() == before


def test_the_cursor_resumes_where_the_last_run_stopped(store):
    engine = SyncEngine(store, sleep=lambda _d: None)
    first = sync_contacts(store, engine)
    assert first.cursor_after == "2026-04-06T14:20:00+00:00"

    second = sync_contacts(store, engine)
    assert second.processed == 0  # nothing newer than the watermark
    assert second.dead_lettered == 0


def test_deals_sync_after_contacts_and_project_into_the_deals_table(store):
    engine = SyncEngine(
        store, sleep=lambda _d: None, projectors={SOURCE_HUBSPOT_DEALS: deal_projector}
    )
    sync_contacts(store, engine)

    resolver = contact_email_resolver(store)

    def deal_reader(since: str | None):
        return read_hubspot_deals(deals_client(FakeSleep()), since, resolve_contact_email=resolver)

    stats = engine.sync_source(SOURCE_HUBSPOT_DEALS, deal_reader)

    assert stats.created == 2
    # 8803's associated contact was never synced, so it has nobody to attach to.
    assert stats.dead_lettered == 1
    assert stats.dead_letter_keys == ["8803"]

    deal = store.get_deal("8801")
    assert deal["contact_email"] == "dana.rivers@zeltrovan-health.example"
    assert deal["amount"] == "48000.00"
    assert deal["is_open"] == 1
    assert store.get_deal("8802")["is_open"] == 0
    assert store.get_deal("8803") is None  # dead-lettered, so never projected


def test_a_deal_projection_cannot_outlive_a_rolled_back_transaction(store):
    # The projector runs inside the record's transaction. If the write fails, the deal row
    # must roll back with everything else rather than being left behind. The fault is scoped
    # to deals so the contacts really do land first; otherwise this would only be retesting
    # that an unresolvable association dead-letters.
    healthy = SyncEngine(store, sleep=lambda _d: None)
    sync_contacts(store, healthy)
    assert store.count("activities") == 4

    def fail_deals_only(record):
        if record.source == SOURCE_HUBSPOT_DEALS:
            raise TransientError("SYNTHETIC downstream failure")

    engine = SyncEngine(
        store,
        sleep=lambda _d: None,
        attempts=2,
        fault=fail_deals_only,
        projectors={SOURCE_HUBSPOT_DEALS: deal_projector},
    )
    resolver = contact_email_resolver(store)

    def deal_reader(since: str | None):
        return read_hubspot_deals(deals_client(FakeSleep()), since, resolve_contact_email=resolver)

    stats = engine.sync_source(SOURCE_HUBSPOT_DEALS, deal_reader)
    # Two resolvable deals exhausted their retry budget, and 8803 was poison from the start.
    assert stats.dead_lettered == 3
    assert stats.retries == 2
    assert store.count("deals") == 0  # no orphan projection survived the rollback
    assert store.count("activities") == 4  # still only the contacts


# -- the fixtures themselves --------------------------------------------------


def test_every_fixture_is_documented_and_shaped_like_the_documented_envelope():
    for name in ("contacts", "deals"):
        data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        assert data["notes"], f"{name} cassette carries no provenance note"
        assert any("SELF-AUTHORED" in line for line in data["notes"])
        for exchange in data["exchanges"]:
            if exchange.get("status", 200) != 200:
                continue
            body = exchange["body"]
            assert isinstance(body["results"], list)
            for obj in body["results"]:
                assert "id" in obj
