"""Convenience wiring so the demo scripts stay short: build readers from the data files,
run all sources, and rebuild raw records from dead-letter payloads for reprocessing.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .calendar_source import read_ics
from .crm_import_source import read_crm_export
from .email_source import read_mbox
from .models import (
    SOURCE_CALENDAR,
    SOURCE_CRM_IMPORT,
    SOURCE_EMAIL,
    SOURCE_TICKETS,
    RawRecord,
)
from .sync_engine import Reader, SyncEngine, SyncStats


def build_readers(data_dir: str | Path) -> dict[str, Reader]:
    """Return a source -> reader map bound to the fixture files in ``data_dir``."""
    data = Path(data_dir)

    def calendar_reader(since: str | None) -> Iterable[RawRecord]:
        return read_ics(data / "calendar.ics", since)

    def email_reader(since: str | None) -> Iterable[RawRecord]:
        return read_mbox(data / "mailbox.mbox", since)

    def crm_reader(since: str | None) -> Iterable[RawRecord]:
        return read_crm_export(data / "crm_export.json", since)

    return {
        SOURCE_CALENDAR: calendar_reader,
        SOURCE_EMAIL: email_reader,
        SOURCE_CRM_IMPORT: crm_reader,
    }


def sync_all(engine: SyncEngine, readers: dict[str, Reader]) -> list[SyncStats]:
    """Run every source once, in a stable order, and return the per-source stats."""
    return [engine.sync_source(source, readers[source]) for source in sorted(readers)]


def dead_letter_rebuilders() -> dict[str, object]:
    """Rebuild a RawRecord from a stored dead-letter row, per source.

    The dead-letter row stores the source, the natural key, and the raw reader payload,
    which together are exactly what the reader emitted, so reprocessing is just re-wrapping
    them. A row that was dead-lettered for a *missing* key carries a synthetic key and will
    remain poison on reprocess until its payload is fixed, which is the intended behavior.
    """

    def from_calendar(natural_key: str, payload: dict) -> RawRecord:
        return RawRecord(source=SOURCE_CALENDAR, natural_key=natural_key, payload=payload)

    def from_email(natural_key: str, payload: dict) -> RawRecord:
        return RawRecord(source=SOURCE_EMAIL, natural_key=natural_key, payload=payload)

    def from_crm(natural_key: str, payload: dict) -> RawRecord:
        return RawRecord(source=SOURCE_CRM_IMPORT, natural_key=natural_key, payload=payload)

    def from_tickets(natural_key: str, payload: dict) -> RawRecord:
        # A ticket dead-lettered for a missing required field carries the drift note that
        # explains why. Re-wrapping it unchanged means it stays poison on reprocess until
        # the source starts sending the field again, which is the correct outcome.
        return RawRecord(source=SOURCE_TICKETS, natural_key=natural_key, payload=payload)

    return {
        SOURCE_CALENDAR: from_calendar,
        SOURCE_EMAIL: from_email,
        SOURCE_CRM_IMPORT: from_crm,
        SOURCE_TICKETS: from_tickets,
    }
