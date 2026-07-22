"""Calendar surface: read and write real ICS (RFC 5545) files via the ``icalendar`` library.

A calendar event maps into the unified activity model as a ``meeting`` activity. The
natural key is the event UID, which is exactly what iCalendar guarantees is stable across
updates to the same event, so it is the correct idempotency key.

The reader emits ``RawRecord``s with a uniform payload shape (see ``normalize.py``). It is
deliberately tolerant: it does NOT validate here, so that malformed events survive as raw
records and get dead-lettered by the engine rather than crashing the read.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

from icalendar import Calendar, Event

from .models import SOURCE_CALENDAR, RawRecord


def _domain_of(email: str) -> str:
    return email.split("@", 1)[1].strip().lower() if "@" in email else ""


def write_ics(path: str | Path, events: list[dict]) -> None:
    """Write a list of event dicts to a real ICS file.

    Each event dict may carry: uid, summary, description, location, start (datetime),
    end (datetime), organizer_email, organizer_name, account_name. Missing optional fields
    are simply omitted. This function does not enforce validity on purpose; the failure
    demo writes intentionally broken events through it.
    """
    cal = Calendar()
    cal.add("prodid", "-//integration-sync-sketch//synthetic//EN")
    cal.add("version", "2.0")
    for ev in events:
        comp = Event()
        if ev.get("uid") is not None:
            comp.add("uid", ev["uid"])
        if ev.get("summary") is not None:
            comp.add("summary", ev["summary"])
        if ev.get("description") is not None:
            comp.add("description", ev["description"])
        if ev.get("location") is not None:
            comp.add("location", ev["location"])
        if ev.get("start") is not None:
            comp.add("dtstart", ev["start"])
        if ev.get("end") is not None:
            comp.add("dtend", ev["end"])
        if ev.get("organizer_email"):
            organizer = f"mailto:{ev['organizer_email']}"
            comp.add("organizer", organizer, parameters={"CN": ev.get("organizer_name", "")})
        if ev.get("account_name") is not None:
            # A non-standard X-property carrying the account label for this demo.
            comp.add("x-account-name", ev["account_name"])
        cal.add_component(comp)
    Path(path).write_bytes(cal.to_ical())


def _to_iso(value: object) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC).isoformat()
    return None


def _organizer_email(comp: Event) -> str:
    organizer = comp.get("organizer")
    if organizer is None:
        return ""
    text = str(organizer)
    return text[len("mailto:"):].strip() if text.lower().startswith("mailto:") else text.strip()


def _organizer_name(comp: Event) -> str:
    organizer = comp.get("organizer")
    if organizer is None:
        return ""
    params = getattr(organizer, "params", {})
    return str(params.get("CN", "")).strip()


def read_ics(path: str | Path, since_iso: str | None = None) -> Iterator[RawRecord]:
    """Parse an ICS file into RawRecords, optionally filtered to events after a cursor.

    ``since_iso`` is the incremental high-water mark: only events whose start time is
    strictly greater are emitted. Events without a parseable start are always emitted, so
    the engine (not the reader) decides they are poison.
    """
    raw = Path(path).read_bytes()
    cal = Calendar.from_ical(raw)
    for comp in cal.walk("vevent"):
        uid_val = comp.get("uid")
        natural_key = None if uid_val is None else str(uid_val)

        dtstart = comp.get("dtstart")
        occurred_iso = _to_iso(dtstart.dt) if dtstart is not None else None

        if since_iso is not None and occurred_iso is not None and occurred_iso <= since_iso:
            continue

        email = _organizer_email(comp)
        payload = {
            "kind": "meeting",
            "occurred_at": occurred_iso,
            "subject": str(comp.get("summary", "")),
            "body": str(comp.get("description", "")),
            "contact_email": email,
            "contact_name": _organizer_name(comp),
            "account_name": str(comp.get("x-account-name", "")) or _domain_of(email),
            "account_domain": _domain_of(email),
        }
        yield RawRecord(source=SOURCE_CALENDAR, natural_key=natural_key, payload=payload)
