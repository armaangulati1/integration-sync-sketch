"""One place to normalize timestamps to UTC ISO-8601.

Cursors and conflict tie-breaks compare timestamps as strings, so every timestamp that
reaches the engine must be in one canonical format (UTC, ISO-8601 with a ``+00:00``
offset). Naive datetimes are assumed to be UTC, which is stated here rather than left
implicit.
"""

from __future__ import annotations

from datetime import UTC, date, datetime


def to_utc_iso(value: object) -> str | None:
    """Return a canonical UTC ISO-8601 string for a datetime/date/parseable string.

    Returns ``None`` when the value cannot be interpreted, so callers can treat that as a
    poison signal rather than guessing.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()
