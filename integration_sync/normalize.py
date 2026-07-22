"""Validation boundary: turn an untrusted ``RawRecord`` into a validated ``CanonicalRecord``.

This is the single place where "is this record usable?" is decided. Every reader funnels
through here. If a record cannot be made canonical it raises ``PoisonError`` (carrying the
natural key when one exists), and the engine dead-letters it. Keeping validation in one
module, rather than scattered across the three readers, is what makes the poison policy
uniform and testable.

Validation rules (a record is poison if any fail):
    - source is one of the known sources
    - natural_key is present and non-empty (without it, idempotency is impossible)
    - occurred_at parses as an ISO-8601 timestamp (needed for cursor + conflict tie-break)
    - contact_email is present and contains an '@' (needed to attach the activity)
"""

from __future__ import annotations

from datetime import datetime

from .errors import PoisonError
from .hashing import content_hash
from .models import VALID_SOURCES, CanonicalRecord, RawRecord
from .timeutil import to_utc_iso


def normalize(raw: RawRecord) -> CanonicalRecord:
    key = raw.natural_key

    if raw.source not in VALID_SOURCES:
        raise PoisonError(f"unknown source {raw.source!r}", natural_key=key)

    if key is None or not str(key).strip():
        raise PoisonError("missing natural key", natural_key=None)
    key = str(key).strip()

    payload = raw.payload
    occurred_raw = payload.get("occurred_at")
    if not occurred_raw:
        raise PoisonError("missing occurred_at timestamp", natural_key=key)
    occurred_iso = to_utc_iso(str(occurred_raw))
    if occurred_iso is None:
        raise PoisonError(f"unparseable occurred_at {occurred_raw!r}", natural_key=key)
    occurred_at = datetime.fromisoformat(occurred_iso)

    contact_email = str(payload.get("contact_email", "")).strip()
    if "@" not in contact_email:
        raise PoisonError(
            f"missing or invalid contact_email {contact_email!r}", natural_key=key
        )

    kind = str(payload.get("kind", "")).strip() or "activity"
    subject = str(payload.get("subject", "")).strip()
    body = str(payload.get("body", "")).strip()
    contact_name = str(payload.get("contact_name", "")).strip()
    account_name = str(payload.get("account_name", "")).strip() or contact_email.split("@", 1)[1]
    account_domain = str(payload.get("account_domain", "")).strip()

    record = CanonicalRecord(
        source=raw.source,
        natural_key=key,
        kind=kind,
        occurred_at=occurred_at,
        subject=subject,
        body=body,
        contact_email=contact_email,
        contact_name=contact_name,
        account_name=account_name,
        account_domain=account_domain,
        content_hash="",
    )
    # Fill the content hash now that the canonical fields are settled.
    digest = content_hash(record.hash_fields())
    return CanonicalRecord(**{**record.__dict__, "content_hash": digest})
