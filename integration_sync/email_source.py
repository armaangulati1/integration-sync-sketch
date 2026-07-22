"""Email surface: a synthetic local mailbox in mbox format via the stdlib ``mailbox`` module.

This models the shape of an IMAP/mail export without any network or credentials. Each
message maps into the unified activity model as an ``email`` activity. The natural key is
the RFC 5322 ``Message-ID``, which is the stable, globally-unique identifier for a message
and therefore the correct idempotency key.

As with the calendar reader, this reader does not validate; malformed messages become raw
records that the engine dead-letters.
"""

from __future__ import annotations

import mailbox
from collections.abc import Iterator
from datetime import UTC
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path

from .models import SOURCE_EMAIL, RawRecord


def _domain_of(email: str) -> str:
    return email.split("@", 1)[1].strip().lower() if "@" in email else ""


def write_mbox(path: str | Path, messages: list[dict]) -> None:
    """Write a list of message dicts to a real mbox file.

    Each dict may carry: message_id, from_email, from_name, to, subject, date (datetime),
    body, account_name. Missing fields are omitted so the failure demo can craft broken
    messages (for example, no From header).
    """
    p = Path(path)
    if p.exists():
        p.unlink()
    box = mailbox.mbox(str(p))
    box.lock()
    try:
        for m in messages:
            msg = mailbox.mboxMessage()
            if m.get("message_id") is not None:
                msg["Message-ID"] = m["message_id"]
            if m.get("from_email") is not None:
                name = m.get("from_name", "")
                msg["From"] = f"{name} <{m['from_email']}>" if name else m["from_email"]
            if m.get("to") is not None:
                msg["To"] = m["to"]
            if m.get("subject") is not None:
                msg["Subject"] = m["subject"]
            if m.get("date") is not None:
                dt = m["date"]
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                msg["Date"] = format_datetime(dt)
            if m.get("account_name") is not None:
                msg["X-Account-Name"] = m["account_name"]
            msg.set_payload(m.get("body", ""))
            box.add(msg)
        box.flush()
    finally:
        box.unlock()
        box.close()


def _parse_from(raw_from: str) -> tuple[str, str]:
    """Return (email, display_name) from a From header, tolerant of odd formatting."""
    raw_from = raw_from.strip()
    if "<" in raw_from and ">" in raw_from:
        name = raw_from.split("<", 1)[0].strip().strip('"')
        email = raw_from.split("<", 1)[1].split(">", 1)[0].strip()
        return email, name
    return raw_from, ""


def read_mbox(path: str | Path, since_iso: str | None = None) -> Iterator[RawRecord]:
    """Parse an mbox file into RawRecords, optionally filtered to messages after a cursor."""
    box = mailbox.mbox(str(path))
    try:
        for msg in box:
            msg_id = msg.get("Message-ID")
            natural_key = None if msg_id is None else str(msg_id).strip()

            date_hdr = msg.get("Date")
            occurred_iso: str | None = None
            if date_hdr:
                try:
                    dt = parsedate_to_datetime(date_hdr)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    occurred_iso = dt.astimezone(UTC).isoformat()
                except (TypeError, ValueError):
                    occurred_iso = None

            if since_iso is not None and occurred_iso is not None and occurred_iso <= since_iso:
                continue

            from_email, from_name = _parse_from(msg.get("From", ""))
            payload = {
                "kind": "email",
                "occurred_at": occurred_iso,
                "subject": str(msg.get("Subject", "")),
                "body": _body_text(msg),
                "contact_email": from_email,
                "contact_name": from_name,
                "account_name": str(msg.get("X-Account-Name", "")) or _domain_of(from_email),
                "account_domain": _domain_of(from_email),
            }
            yield RawRecord(source=SOURCE_EMAIL, natural_key=natural_key, payload=payload)
    finally:
        box.close()


def _body_text(msg: mailbox.mboxMessage) -> str:
    payload = msg.get_payload()
    if isinstance(payload, list):
        parts = [str(p.get_payload()) for p in payload]
        return "\n".join(parts).strip()
    return str(payload).strip()
