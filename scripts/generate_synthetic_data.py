"""Generate the synthetic fixtures the demos read: an ICS calendar, an mbox mailbox, and a
CRM JSON export.

Everything here is invented. Names are fictional, domains use the reserved ``.example`` /
``.invalid`` TLDs that can never resolve, and bodies are labeled SYNTHETIC. There is no
real person, company, or credential anywhere in this data.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from integration_sync.calendar_source import write_ics  # noqa: E402
from integration_sync.crm_import_source import write_crm_export  # noqa: E402
from integration_sync.email_source import write_mbox  # noqa: E402
from integration_sync.ticket_source import write_ticket_export  # noqa: E402

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
            "organizer_email": "dana.rivers@zeltrovan-health.example",
            "organizer_name": "Dana Rivers",
            "account_name": "Zeltrovan Health",
        },
        {
            "uid": "evt-1002@sketch",
            "summary": "SYNTHETIC data mapping review",
            "description": "SYNTHETIC demo event. Field mapping review.",
            "start": _t(26), "end": _t(27),
            "organizer_email": "priya.nadel@ozmirthex-clinic.example",
            "organizer_name": "Priya Nadel",
            "account_name": "Ozmirthex Clinic",
        },
        {
            "uid": "evt-1003@sketch",
            "summary": "SYNTHETIC go-live check-in",
            "description": "SYNTHETIC demo event. Cutover readiness.",
            "start": _t(50), "end": _t(51),
            "organizer_email": "dana.rivers@zeltrovan-health.example",
            "organizer_name": "Dana Rivers",
            "account_name": "Zeltrovan Health",
        },
        {
            "uid": "evt-1004@sketch",
            "summary": "SYNTHETIC office hours",
            "description": "SYNTHETIC demo event. Open questions.",
            "start": _t(74), "end": _t(75),
            "organizer_email": "sam.okafor@drenvalik-labs.example",
            "organizer_name": "Sam Okafor",
            "account_name": "Drenvalik Labs",
        },
    ]


def emails() -> list[dict]:
    return [
        {
            "message_id": "<msg-2001@sketch>",
            "from_email": "dana.rivers@zeltrovan-health.example",
            "from_name": "Dana Rivers",
            "to": "solutions@vendor.invalid",
            "subject": "SYNTHETIC Re: kickoff follow-up",
            "date": _t(2),
            "body": "SYNTHETIC demo email. Thanks for the walkthrough.",
            "account_name": "Zeltrovan Health",
        },
        {
            "message_id": "<msg-2002@sketch>",
            "from_email": "priya.nadel@ozmirthex-clinic.example",
            "from_name": "Priya Nadel",
            "to": "solutions@vendor.invalid",
            "subject": "SYNTHETIC field questions",
            "date": _t(28),
            "body": "SYNTHETIC demo email. A few mapping questions attached.",
            "account_name": "Ozmirthex Clinic",
        },
        {
            "message_id": "<msg-2003@sketch>",
            "from_email": "sam.okafor@drenvalik-labs.example",
            "from_name": "Sam Okafor",
            "to": "solutions@vendor.invalid",
            "subject": "SYNTHETIC scheduling",
            "date": _t(60),
            "body": "SYNTHETIC demo email. Proposing a time for office hours.",
            "account_name": "Drenvalik Labs",
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
            "contact_email": "dana.rivers@zeltrovan-health.example",
            "contact_name": "Dana Rivers",
            "account_name": "Zeltrovan Health",
            "account_domain": "zeltrovan-health.example",
        },
        {
            "record_id": "note-3002",
            "kind": "task",
            "occurred_at": _t(30).isoformat(),
            "subject": "SYNTHETIC send mapping doc",
            "body": "SYNTHETIC demo task. Follow up with mapping doc.",
            "contact_email": "priya.nadel@ozmirthex-clinic.example",
            "contact_name": "Priya Nadel",
            "account_name": "Ozmirthex Clinic",
            "account_domain": "ozmirthex-clinic.example",
        },
    ]


def tickets() -> list[dict]:
    """Jira-shaped issues for the synthetic implementation project.

    Served in the ORIGINAL field shape. The demo's endpoint rewrites later pages into the
    drifted shape at read time, which is how a mid-stream schema change is reproduced
    without keeping two copies of the fixture.
    """

    def issue(key, summary, status, email, due, updated, milestone):
        return {
            "key": key,
            "fields": {
                "summary": summary,
                "status": status,
                "assignee": {"emailAddress": email, "displayName": email.split("@")[0]},
                "duedate": due,
                "updated": updated,
                "customfield_milestone": milestone,
                "account": "Zeltrovan Health",
            },
        }

    dana = "dana.rivers@zeltrovan-health.example"
    priya = "priya.nadel@ozmirthex-clinic.example"
    sam = "sam.okafor@drenvalik-labs.example"
    return [
        issue("OPS-401", "SYNTHETIC map member eligibility fields", "In Progress",
              dana, "2026-04-14", "2026-04-08T09:00:00+00:00", "M2"),
        issue("OPS-402", "SYNTHETIC confirm test environment credentials", "Done",
              priya, "2026-04-10", "2026-04-09T09:00:00+00:00", "M2"),
        issue("OPS-403", "SYNTHETIC draft the reconciliation checklist", "Open",
              sam, "2026-04-30", "2026-04-10T09:00:00+00:00", "M3"),
        issue("OPS-404", "SYNTHETIC schedule the readiness walkthrough", "Open",
              dana, "2026-05-15", "2026-04-11T09:00:00+00:00", "M3"),
        issue("OPS-405", "SYNTHETIC return the signed credentialing pack", "Blocked",
              priya, "2026-04-08", "2026-04-12T09:00:00+00:00", "M4"),
        issue("OPS-406", "SYNTHETIC refresh the training environment", "Open",
              sam, "2026-04-24", "2026-04-13T09:00:00+00:00", "M5"),
    ]


def milestones() -> list[dict]:
    """The synthetic project plan the tickets hang off.

    M1 finished four days late. Everything downstream of it inherits that, which is what
    the report and the escalation rules are for.
    """
    return [
        {
            "milestone_key": "M1", "name": "SYNTHETIC data mapping signed off",
            "account_name": "Zeltrovan Health",
            "owner_email": "dana.rivers@zeltrovan-health.example",
            "planned_start": "2026-04-01", "planned_end": "2026-04-15",
            "actual_end": "2026-04-19", "status": "done", "depends_on": [],
        },
        {
            "milestone_key": "M2", "name": "SYNTHETIC interface live in the test environment",
            "account_name": "Zeltrovan Health",
            "owner_email": "dana.rivers@zeltrovan-health.example",
            "planned_start": "2026-04-15", "planned_end": "2026-05-10",
            "actual_end": None, "status": "in_progress", "depends_on": ["M1"],
        },
        {
            "milestone_key": "M3", "name": "SYNTHETIC first synthetic claims batch reconciled",
            "account_name": "Zeltrovan Health",
            "owner_email": "sam.okafor@drenvalik-labs.example",
            "planned_start": "2026-05-10", "planned_end": "2026-05-22",
            "actual_end": None, "status": "planned", "depends_on": ["M2"],
        },
        {
            "milestone_key": "M4", "name": "SYNTHETIC credentialing pack returned",
            "account_name": "Zeltrovan Health",
            "owner_email": "priya.nadel@ozmirthex-clinic.example",
            "planned_start": "2026-04-01", "planned_end": "2026-04-12",
            "actual_end": None, "status": "in_progress", "depends_on": [],
        },
        {
            "milestone_key": "M5", "name": "SYNTHETIC training environment refreshed",
            "account_name": "Zeltrovan Health", "owner_email": "",
            "planned_start": "2026-04-18", "planned_end": "2026-04-25",
            "actual_end": None, "status": "planned", "depends_on": [],
        },
    ]


def main() -> None:
    DATA.mkdir(exist_ok=True)
    write_ics(DATA / "calendar.ics", calendar_events())
    write_mbox(DATA / "mailbox.mbox", emails())
    write_crm_export(DATA / "crm_export.json", crm_notes())
    write_ticket_export(str(DATA / "tickets.json"), tickets())
    (DATA / "milestones.json").write_text(json.dumps(milestones(), indent=2), encoding="utf-8")
    # Printed as <repo>/data rather than the absolute path. This output is captured into
    # demo_transcript.txt, which is committed, and an absolute path there publishes the
    # directory layout of whatever machine happened to produce it.
    print(f"wrote synthetic fixtures to <repo>/{DATA.name}/ :")
    print(f"  calendar.ics   ({len(calendar_events())} events)")
    print(f"  mailbox.mbox   ({len(emails())} messages)")
    print(f"  crm_export.json ({len(crm_notes())} records)")
    print(f"  tickets.json   ({len(tickets())} issues)")
    print(f"  milestones.json ({len(milestones())} milestones)")


if __name__ == "__main__":
    main()
