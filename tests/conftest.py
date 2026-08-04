"""Shared test fixtures and helpers.

Kept small on purpose: an in-memory CRM store, a helper to build raw calendar-shaped
records, and an in-memory reader that honors the incremental cursor exactly like the file
readers do (yielding only records strictly newer than the cursor).
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from integration_sync.crm_store import CrmStore
from integration_sync.models import SOURCE_CALENDAR, RawRecord


@pytest.fixture()
def store() -> CrmStore:
    s = CrmStore(":memory:")
    s.init_schema()
    yield s
    s.close()


def make_raw(
    natural_key: str | None,
    *,
    occurred_at: str,
    subject: str = "SYNTHETIC subject",
    body: str = "SYNTHETIC body",
    email: str = "dana.rivers@zeltrovan-health.example",
    name: str = "Dana Rivers",
    account: str = "Zeltrovan Health",
    source: str = SOURCE_CALENDAR,
    kind: str = "meeting",
) -> RawRecord:
    domain = email.split("@", 1)[1] if "@" in email else ""
    return RawRecord(
        source=source,
        natural_key=natural_key,
        payload={
            "kind": kind,
            "occurred_at": occurred_at,
            "subject": subject,
            "body": body,
            "contact_email": email,
            "contact_name": name,
            "account_name": account,
            "account_domain": domain,
        },
    )


def mem_reader(records: list[RawRecord]):
    """Return a Reader over an in-memory list that respects the incremental cursor.

    Mirrors the file readers: a record whose occurred_at is <= the cursor is skipped.
    Records with no/blank occurred_at are always yielded (the engine dead-letters them).
    """

    def _reader(since: str | None) -> Iterable[RawRecord]:
        out = []
        for r in records:
            occurred = r.payload.get("occurred_at")
            if since is not None and occurred and str(occurred) <= since:
                continue
            out.append(r)
        return out

    return _reader
