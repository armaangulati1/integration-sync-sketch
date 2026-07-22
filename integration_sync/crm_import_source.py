"""CRM-import surface: a JSON export of activities logged directly in another CRM-shaped
system, of the same schema this project's own SQLite store uses.

This is the third integration surface. It lets the demo show two things the other two
cannot alone: (1) syncing structured CRM activity records, and (2) cross-source conflicts,
where a note logged in another CRM and a calendar event describe the same interaction and
land on overlapping accounts.

The natural key is the export's own record id. As with the other readers, no validation
happens here; malformed rows survive as raw records for the engine to dead-letter.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from .models import SOURCE_CRM_IMPORT, RawRecord
from .timeutil import to_utc_iso


def _domain_of(email: str) -> str:
    return email.split("@", 1)[1].strip().lower() if "@" in email else ""


def write_crm_export(path: str | Path, records: list[dict]) -> None:
    """Write a list of CRM activity dicts to a JSON export file."""
    Path(path).write_text(json.dumps(records, indent=2), encoding="utf-8")


def read_crm_export(path: str | Path, since_iso: str | None = None) -> Iterator[RawRecord]:
    """Parse a CRM JSON export into RawRecords, optionally filtered by cursor.

    A top-level structure that is not a list, or rows that are not objects, are surfaced as
    best-effort raw records (or skipped when nothing addressable can be recovered) rather
    than raising, so one bad export does not abort the whole read.
    """
    text = Path(path).read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, list):
        return
    for row in data:
        if not isinstance(row, dict):
            continue
        record_id = row.get("record_id")
        natural_key = None if record_id is None else str(record_id)
        # Canonicalize to UTC ISO so the cursor comparison matches the engine's format.
        # An unparseable timestamp is left as-is here so normalization dead-letters it.
        occurred_iso = to_utc_iso(row.get("occurred_at")) or row.get("occurred_at")
        if since_iso is not None and occurred_iso is not None and str(occurred_iso) <= since_iso:
            continue
        email = str(row.get("contact_email", ""))
        payload = {
            "kind": str(row.get("kind", "note")),
            "occurred_at": occurred_iso,
            "subject": str(row.get("subject", "")),
            "body": str(row.get("body", "")),
            "contact_email": email,
            "contact_name": str(row.get("contact_name", "")),
            "account_name": str(row.get("account_name", "")) or _domain_of(email),
            "account_domain": str(row.get("account_domain", "")) or _domain_of(email),
        }
        yield RawRecord(source=SOURCE_CRM_IMPORT, natural_key=natural_key, payload=payload)
