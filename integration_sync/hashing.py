"""Content hashing for change detection.

The content hash is the heart of idempotency. Two records with the same natural key are
considered *unchanged* if and only if their content hashes match. The hash is computed
over a canonical, order-independent serialization of the semantically meaningful fields,
so cosmetic differences (dict ordering, trailing whitespace) do not spuriously look like
changes, and any real content change does.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def content_hash(fields: dict[str, Any]) -> str:
    """Return a stable SHA-256 hex digest over the meaningful fields of a record.

    Keys are sorted and values are normalized to strings with surrounding whitespace
    stripped, so the digest depends only on content, never on serialization order.
    """
    normalized = {
        str(key): ("" if value is None else str(value).strip())
        for key, value in fields.items()
    }
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
