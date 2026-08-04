"""The three source surfaces: real ICS, mbox, and CRM-JSON round-trip through their readers."""

from __future__ import annotations

from datetime import UTC, datetime

from integration_sync.calendar_source import read_ics, write_ics
from integration_sync.crm_import_source import read_crm_export, write_crm_export
from integration_sync.email_source import read_mbox, write_mbox
from integration_sync.models import SOURCE_CALENDAR, SOURCE_CRM_IMPORT, SOURCE_EMAIL


def test_ics_write_then_read(tmp_path):
    path = tmp_path / "cal.ics"
    write_ics(path, [
        {
            "uid": "evt-a@sketch", "summary": "SYNTHETIC a",
            "start": datetime(2026, 3, 2, 9, tzinfo=UTC),
            "end": datetime(2026, 3, 2, 10, tzinfo=UTC),
            "organizer_email": "dana.rivers@marnovek-health.example",
            "organizer_name": "Dana Rivers", "account_name": "Marnovek Health",
        },
    ])
    # The file is real ICS.
    assert path.read_text().startswith("BEGIN:VCALENDAR")

    records = list(read_ics(path))
    assert len(records) == 1
    r = records[0]
    assert r.source == SOURCE_CALENDAR
    assert r.natural_key == "evt-a@sketch"
    assert r.payload["contact_email"] == "dana.rivers@marnovek-health.example"
    assert r.payload["occurred_at"] == "2026-03-02T09:00:00+00:00"
    assert r.payload["account_name"] == "Marnovek Health"


def test_ics_reader_honors_cursor(tmp_path):
    path = tmp_path / "cal.ics"
    write_ics(path, [
        {"uid": "e1@sketch", "summary": "SYNTHETIC 1",
         "start": datetime(2026, 3, 2, 9, tzinfo=UTC),
         "organizer_email": "a@marnovek-health.example"},
        {"uid": "e2@sketch", "summary": "SYNTHETIC 2",
         "start": datetime(2026, 3, 5, 9, tzinfo=UTC),
         "organizer_email": "a@marnovek-health.example"},
    ])
    keys = [r.natural_key for r in read_ics(path, "2026-03-03T00:00:00+00:00")]
    assert keys == ["e2@sketch"]


def test_mbox_write_then_read(tmp_path):
    path = tmp_path / "mail.mbox"
    write_mbox(path, [
        {
            "message_id": "<m1@sketch>", "from_email": "sam.okafor@quillhaven-labs.example",
            "from_name": "Sam Okafor", "to": "solutions@vendor.invalid",
            "subject": "SYNTHETIC hi", "date": datetime(2026, 3, 2, 9, tzinfo=UTC),
            "body": "SYNTHETIC body", "account_name": "Quillhaven Labs",
        },
    ])
    records = list(read_mbox(path))
    assert len(records) == 1
    r = records[0]
    assert r.source == SOURCE_EMAIL
    assert r.natural_key == "<m1@sketch>"
    assert r.payload["contact_email"] == "sam.okafor@quillhaven-labs.example"
    assert r.payload["contact_name"] == "Sam Okafor"
    assert "SYNTHETIC body" in r.payload["body"]


def test_crm_export_write_then_read(tmp_path):
    path = tmp_path / "crm.json"
    write_crm_export(path, [
        {
            "record_id": "note-1", "kind": "note",
            "occurred_at": "2026-03-02T09:00:00+00:00",
            "subject": "SYNTHETIC note", "body": "SYNTHETIC",
            "contact_email": "priya.nadel@ambervale-clinic.example",
            "contact_name": "Priya Nadel", "account_name": "Ambervale Clinic",
            "account_domain": "ambervale-clinic.example",
        },
    ])
    records = list(read_crm_export(path))
    assert len(records) == 1
    r = records[0]
    assert r.source == SOURCE_CRM_IMPORT
    assert r.natural_key == "note-1"
    assert r.payload["kind"] == "note"


def test_crm_export_non_list_is_empty(tmp_path):
    path = tmp_path / "crm.json"
    path.write_text('{"not": "a list"}')
    assert list(read_crm_export(path)) == []
