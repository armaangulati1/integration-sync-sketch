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

# The source surfaces this demo syncs FROM.
SOURCE_CALENDAR = "calendar"
SOURCE_EMAIL = "email"
SOURCE_CRM_IMPORT = "crm_import"
SOURCE_TICKETS = "tickets"
# The HubSpot surfaces are two distinct sources, not one, because they have separate
# cursors: contacts and deals move at different rates, and a deal sync that fails must not
# rewind the contact watermark. Two sources is also what makes the ordering dependency
# between them explicit (a deal is only linkable once its contact has landed).
SOURCE_HUBSPOT_CONTACTS = "hubspot_contacts"
SOURCE_HUBSPOT_DEALS = "hubspot_deals"

VALID_SOURCES = frozenset(
    {
        SOURCE_CALENDAR,
        SOURCE_EMAIL,
        SOURCE_CRM_IMPORT,
        SOURCE_TICKETS,
        SOURCE_HUBSPOT_CONTACTS,
        SOURCE_HUBSPOT_DEALS,
    }
)


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
    # Source-specific fields that have no column on the shared activity shape. A ticket puts
    # its status, due date, and milestone link here. These ARE part of the content hash: a
    # ticket whose status changes is a genuine content change and must re-sync.
    extra: dict[str, Any] = field(default_factory=dict)
    # Observations about the SHAPE of the incoming payload (a field arrived under an alias,
    # a type was coerced, an unrecognized field appeared). Deliberately NOT part of the
    # content hash: when a source renames a field but the value is identical, that is a
    # schema change, not a content change, and it must not masquerade as an update.
    drift: tuple[dict[str, str], ...] = ()

    def hash_fields(self) -> dict[str, Any]:
        """The subset of fields whose change should count as a content change."""
        fields: dict[str, Any] = {
            "kind": self.kind,
            "occurred_at": self.occurred_at.isoformat(),
            "subject": self.subject,
            "body": self.body,
            "contact_email": self.contact_email,
            "contact_name": self.contact_name,
            "account_name": self.account_name,
            "account_domain": self.account_domain,
        }
        # Namespaced so an extra key can never collide with, or silently shadow, a core one.
        for key in sorted(self.extra):
            fields[f"extra.{key}"] = self.extra[key]
        return fields
