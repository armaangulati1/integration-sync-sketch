"""Generate the synthetic fixtures the demos read: an ICS calendar, an mbox mailbox, and a
CRM JSON export.

Everything here is invented. Names are fictional, domains use the reserved ``.example`` /
``.invalid`` TLDs that can never resolve, and bodies are labeled SYNTHETIC. There is no
real person, company, or credential anywhere in this data.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from integration_sync.calendar_source import write_ics  # noqa: E402
from integration_sync.crm_import_source import write_crm_export  # noqa: E402
from integration_sync.email_source import write_mbox  # noqa: E402

DATA = REPO / "data"

# Fictional accounts and people. Domains are reserved non-resolvable TLDs.
BASE = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


def _t(hours: float) -> datetime:
    return BASE + timedelta(hours=hours)


def calendar_events() -> list[dict]:
    return [
        {
            "uid": "evt-1001@sketch",
            "summary": "SYNTHETIC kickoff sync",
            "description": "SYNTHETIC demo event. Onboarding walkthrough.",
            "location": "Video",
            "start": _t(0), "end": _t(1),
            "organizer_email": "dana.rivers@northgate-health.example",
            "organizer_name": "Dana Rivers",
            "account_name": "Northgate Health",
        },
        {
            "uid": "evt-1002@sketch",
            "summary": "SYNTHETIC data mapping review",
            "description": "SYNTHETIC demo event. Field mapping review.",
            "start": _t(26), "end": _t(27),
            "organizer_email": "priya.nadel@riverbend-clinic.example",
            "organizer_name": "Priya Nadel",
            "account_name": "Riverbend Clinic",
        },
        {
            "uid": "evt-1003@sketch",
            "summary": "SYNTHETIC go-live check-in",
            "description": "SYNTHETIC demo event. Cutover readiness.",
            "start": _t(50), "end": _t(51),
            "organizer_email": "dana.rivers@northgate-health.example",
            "organizer_name": "Dana Rivers",
            "account_name": "Northgate Health",
        },
        {
            "uid": "evt-1004@sketch",
            "summary": "SYNTHETIC office hours",
            "description": "SYNTHETIC demo event. Open questions.",
            "start": _t(74), "end": _t(75),
            "organizer_email": "sam.okafor@lakeside-labs.example",
            "organizer_name": "Sam Okafor",
            "account_name": "Lakeside Labs",
        },
    ]


def emails() -> list[dict]:
    return [
        {
            "message_id": "<msg-2001@sketch>",
            "from_email": "dana.rivers@northgate-health.example",
            "from_name": "Dana Rivers",
            "to": "solutions@vendor.invalid",
            "subject": "SYNTHETIC Re: kickoff follow-up",
            "date": _t(2),
            "body": "SYNTHETIC demo email. Thanks for the walkthrough.",
            "account_name": "Northgate Health",
        },
        {
            "message_id": "<msg-2002@sketch>",
            "from_email": "priya.nadel@riverbend-clinic.example",
            "from_name": "Priya Nadel",
            "to": "solutions@vendor.invalid",
            "subject": "SYNTHETIC field questions",
            "date": _t(28),
            "body": "SYNTHETIC demo email. A few mapping questions attached.",
            "account_name": "Riverbend Clinic",
        },
        {
            "message_id": "<msg-2003@sketch>",
            "from_email": "sam.okafor@lakeside-labs.example",
            "from_name": "Sam Okafor",
            "to": "solutions@vendor.invalid",
            "subject": "SYNTHETIC scheduling",
            "date": _t(60),
            "body": "SYNTHETIC demo email. Proposing a time for office hours.",
            "account_name": "Lakeside Labs",
        },
    ]


def crm_notes() -> list[dict]:
    return [
        {
            "record_id": "note-3001",
            "kind": "note",
            "occurred_at": _t(3).isoformat(),
            "subject": "SYNTHETIC call notes",
            "body": "SYNTHETIC demo note. Logged after kickoff.",
            "contact_email": "dana.rivers@northgate-health.example",
            "contact_name": "Dana Rivers",
            "account_name": "Northgate Health",
            "account_domain": "northgate-health.example",
        },
        {
            "record_id": "note-3002",
            "kind": "task",
            "occurred_at": _t(30).isoformat(),
            "subject": "SYNTHETIC send mapping doc",
            "body": "SYNTHETIC demo task. Follow up with mapping doc.",
            "contact_email": "priya.nadel@riverbend-clinic.example",
            "contact_name": "Priya Nadel",
            "account_name": "Riverbend Clinic",
            "account_domain": "riverbend-clinic.example",
        },
    ]


def main() -> None:
    DATA.mkdir(exist_ok=True)
    write_ics(DATA / "calendar.ics", calendar_events())
    write_mbox(DATA / "mailbox.mbox", emails())
    write_crm_export(DATA / "crm_export.json", crm_notes())
    print(f"wrote synthetic fixtures to {DATA}/ :")
    print(f"  calendar.ics   ({len(calendar_events())} events)")
    print(f"  mailbox.mbox   ({len(emails())} messages)")
    print(f"  crm_export.json ({len(crm_notes())} records)")


if __name__ == "__main__":
    main()
