"""Canonical data models shared across the three source surfaces and the sync engine.

A ``RawRecord`` is what a source emits: a loosely-typed bag of strings plus whatever
natural key the source could recover. It may be malformed.

A ``CanonicalRecord`` is what normalization produces: a validated, typed record with a
non-empty natural key, a parsed timestamp, and a content hash. The engine only ever
writes ``CanonicalRecord``s; anything that cannot be normalized into one is dead-lettered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# The three source surfaces this demo syncs FROM.
SOURCE_CALENDAR = "calendar"
SOURCE_EMAIL = "email"
SOURCE_CRM_IMPORT = "crm_import"

VALID_SOURCES = frozenset({SOURCE_CALENDAR, SOURCE_EMAIL, SOURCE_CRM_IMPORT})


@dataclass(frozen=True)
class RawRecord:
    """The untrusted, pre-validation shape emitted by a source reader."""

    source: str
    natural_key: str | None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CanonicalRecord:
    """The validated shape the engine writes into the CRM target.

    ``occurred_at`` is the source timestamp used as both the incremental cursor value and
    the tie-breaker for the conflict policy. ``content_hash`` is computed over the
    meaningful fields (see ``hashing.content_hash``) and drives change detection.
    """

    source: str
    natural_key: str
    kind: str
    occurred_at: datetime
    subject: str
    body: str
    contact_email: str
    contact_name: str
    account_name: str
    account_domain: str
    content_hash: str

    def hash_fields(self) -> dict[str, Any]:
        """The subset of fields whose change should count as a content change."""
        return {
            "kind": self.kind,
            "occurred_at": self.occurred_at.isoformat(),
            "subject": self.subject,
            "body": self.body,
            "contact_email": self.contact_email,
            "contact_name": self.contact_name,
            "account_name": self.account_name,
            "account_domain": self.account_domain,
        }
