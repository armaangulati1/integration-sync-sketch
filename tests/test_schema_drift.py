"""Schema drift: the source changes shape underneath a running connector.

Each test PLANTS a specific drift into the synthetic endpoint mid-stream and asserts the
connector recovered from that exact drift, rather than asserting against a stub that was
told what to return. The four planted shapes are the ones that actually happen:

    1. a field is renamed              (``duedate`` -> ``due_date``)
    2. a scalar becomes an object      (``status: "Open"`` -> ``status: {"name": "Open"}``)
    3. an unrecognized field appears   (``request_type``)
    4. a required field disappears     (``summary`` removed)

The two load-bearing assertions are the last two tests in the drift-versus-content section:
a rename with unchanged values must NOT look like an update, and a renamed issue KEY must
not duplicate the board. The second is the expensive one, because a connector that reads
identity from a field that quietly moved will happily write every ticket twice and its
idempotency ledger will agree with it.

Every one of these is paired with a control that CAN fail, so a passing suite is evidence
rather than decoration.
"""

from __future__ import annotations

import json

from integration_sync.crm_store import CrmStore
from integration_sync.models import SOURCE_TICKETS
from integration_sync.sync_engine import SyncEngine
from integration_sync.ticket_source import (
    DRIFT_ALIAS,
    DRIFT_COERCED,
    DRIFT_UNKNOWN,
    SyntheticTicketApi,
    read_tickets,
    resolve_fields,
    ticket_projector,
)

CLEAN = [
    {
        "key": "OPS-201",
        "fields": {
            "summary": "SYNTHETIC interface mapping",
            "status": "Open",
            "assignee": {"emailAddress": "dana.rivers@northgate-health.example"},
            "duedate": "2026-04-10",
            "updated": "2026-04-01T09:00:00+00:00",
            "customfield_milestone": "M2",
        },
    },
    {
        "key": "OPS-202",
        "fields": {
            "summary": "SYNTHETIC credential handoff",
            "status": "Done",
            "assignee": {"emailAddress": "priya.nadel@riverbend-clinic.example"},
            "duedate": "2026-04-12",
            "updated": "2026-04-02T09:00:00+00:00",
            "customfield_milestone": "M2",
        },
    },
]


# -- the planted drifts -------------------------------------------------------


def rename_and_retype(issue: dict) -> dict:
    """Rename the due date and promote status from a string to an object.

    Deliberately changes SHAPE only. No value differs, which is what makes it the right
    input for the "a rename is not a content change" assertion below.
    """
    fields = dict(issue["fields"])
    fields["due_date"] = fields.pop("duedate")
    fields["status"] = {"name": fields["status"]}
    return {**issue, "fields": fields}


def add_unknown_field(issue: dict) -> dict:
    """A field the contract has never heard of shows up."""
    fields = dict(issue["fields"])
    fields["request_type"] = "onboarding"
    return {**issue, "fields": fields}


def drift_everything(issue: dict) -> dict:
    return add_unknown_field(rename_and_retype(issue))


def rename_key(issue: dict) -> dict:
    """Move the issue identity to a different field name."""
    out = dict(issue)
    out["issueKey"] = out.pop("key")
    return out


def drop_summary(issue: dict) -> dict:
    """Remove a field the connector declared required."""
    fields = dict(issue["fields"])
    fields.pop("summary")
    return {**issue, "fields": fields}


def bump_status(issue: dict) -> dict:
    """A genuine CONTENT change, used as the control for the rename tests.

    The update timestamp moves too, because that is what a ticketing system does when a
    ticket changes, and the engine's last-write-wins policy reads that timestamp.
    """
    fields = dict(issue["fields"])
    fields["status"] = "In Review"
    fields["updated"] = "2026-04-05T09:00:00+00:00"
    return {**issue, "fields": fields}


def sync(store: CrmStore, issues, drift_fn=None, *, page_size: int = 10, drift_from_page=0):
    api = SyntheticTicketApi(
        [dict(i) for i in issues],
        page_size=page_size,
        drift_fn=drift_fn,
        drift_from_page=drift_from_page,
    )
    engine = SyncEngine(
        store, base_delay=0.0, sleep=lambda _d: None,
        projectors={SOURCE_TICKETS: ticket_projector},
    )
    return engine.sync_source(SOURCE_TICKETS, lambda since: read_tickets(api, since))


# -- resolution of each planted shape -----------------------------------------


def test_renamed_field_resolves_through_its_alias():
    fields = rename_and_retype(CLEAN[0])["fields"]
    values, _unmapped, drift = resolve_fields(fields)
    assert values["due_date"] == "2026-04-10"  # the value survived the rename
    assert any(n.kind == DRIFT_ALIAS and n.field == "due_date" for n in drift)


def test_scalar_that_became_an_object_is_flattened():
    fields = rename_and_retype(CLEAN[0])["fields"]
    values, _unmapped, drift = resolve_fields(fields)
    assert values["status"] == "Open"  # not the repr of a dict
    assert any(n.kind == DRIFT_COERCED and n.field == "status" for n in drift)


def test_unrecognized_field_is_kept_not_dropped():
    fields = add_unknown_field(CLEAN[0])["fields"]
    _values, unmapped, drift = resolve_fields(fields)
    assert unmapped["request_type"] == "onboarding"
    assert any(n.kind == DRIFT_UNKNOWN and n.field == "request_type" for n in drift)


def test_clean_payload_reports_no_drift():
    # The control for all three above: an undrifted payload must produce an EMPTY note list,
    # otherwise the assertions above would pass on any input at all.
    _values, unmapped, drift = resolve_fields(CLEAN[0]["fields"])
    assert drift == []
    assert unmapped == {}


def test_unmapped_field_reaches_the_store():
    store = CrmStore(":memory:")
    store.init_schema()
    sync(store, CLEAN, drift_everything)
    row = store.get_ticket("OPS-201")
    assert json.loads(row["unmapped_json"])["request_type"] == "onboarding"
    store.close()


def test_missing_required_field_is_dead_lettered_not_written_blank():
    store = CrmStore(":memory:")
    store.init_schema()
    stats = sync(store, CLEAN, drop_summary)
    assert stats.dead_lettered == 2
    assert stats.created == 0
    # The point of dead-lettering rather than defaulting: no half-populated ticket rows.
    assert store.count("tickets") == 0
    queued = store.list_dead_letter()
    assert {r["natural_key"] for r in queued} == {"OPS-201", "OPS-202"}
    store.close()


# -- drift versus content -----------------------------------------------------


def test_pure_rename_is_not_a_content_change():
    store = CrmStore(":memory:")
    store.init_schema()
    first = sync(store, CLEAN)
    assert first.created == 2

    # The source renames a field and retypes another. No VALUE changed.
    store.set_cursor(SOURCE_TICKETS, None)  # full re-scan, as run_demo does
    second = sync(store, CLEAN, rename_and_retype)

    assert second.unchanged == 2, "a rename must not masquerade as an update"
    assert second.updated == 0
    store.close()


def test_a_real_content_change_still_updates():
    # The control for the test above. If this one did not update, "unchanged" would be
    # proving nothing except that the second pass was inert.
    store = CrmStore(":memory:")
    store.init_schema()
    sync(store, CLEAN)
    store.set_cursor(SOURCE_TICKETS, None)
    second = sync(store, CLEAN, bump_status)

    assert second.updated == 2
    assert second.unchanged == 0
    assert store.get_ticket("OPS-201")["status"] == "In Review"
    store.close()


def test_renamed_issue_key_does_not_duplicate_the_board():
    store = CrmStore(":memory:")
    store.init_schema()
    sync(store, CLEAN)
    assert store.count("tickets") == 2

    store.set_cursor(SOURCE_TICKETS, None)
    second = sync(store, CLEAN, rename_key)

    assert store.count("tickets") == 2, "identity drift silently duplicated the board"
    assert store.count("sync_records") == 2
    assert second.created == 0
    store.close()


def test_drift_from_page_two_still_lands_after_page_one_committed():
    # The realistic sequence: page 1 arrives in the old shape and is already written before
    # page 2 shows up in the new one. Both pages must end up correct.
    store = CrmStore(":memory:")
    store.init_schema()
    stats = sync(store, CLEAN, drift_everything, page_size=1, drift_from_page=1)
    assert stats.created == 2
    assert store.get_ticket("OPS-201")["due_date"] == "2026-04-10"  # undrifted page
    assert store.get_ticket("OPS-202")["due_date"] == "2026-04-12"  # drifted page
    store.close()


# -- observability ------------------------------------------------------------


def test_drift_is_written_to_the_audit_trail():
    store = CrmStore(":memory:")
    store.init_schema()
    sync(store, CLEAN, drift_everything)
    actions = [r["action"] for r in store.audit_for(SOURCE_TICKETS, "OPS-201")]
    assert "schema_drift" in actions
    details = [r["detail"] for r in store.audit_for(SOURCE_TICKETS, "OPS-201")]
    assert any("due_date" in d for d in details)


def test_drift_notes_are_recorded_once_not_once_per_run():
    # Drift must be observable without turning every re-sync into a growing audit table.
    store = CrmStore(":memory:")
    store.init_schema()
    sync(store, CLEAN, drift_everything)
    before = store.snapshot()

    store.set_cursor(SOURCE_TICKETS, None)
    sync(store, CLEAN, drift_everything)
    assert store.snapshot() == before, "a re-run over drifted data must still be a no-op"
    store.close()
