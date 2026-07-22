"""integration-sync-sketch.

A self-contained study of the patterns behind syncing external systems (calendar,
email, a CRM-shaped store) into a unified target: idempotency by natural key plus
content hash, incremental cursors, a documented conflict policy with an audit trail,
retry with exponential backoff on transient failures, and dead-lettering for poison
records.

All data in this package's demos is synthetic and self-authored. There are no network
calls, no real credentials, and no external services.
"""

__version__ = "0.1.0"
