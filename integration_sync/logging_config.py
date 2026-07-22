"""Small logging helper so the demo scripts produce readable, structured output.

Kept deliberately dependency-free. The engine logs at INFO for the normal lifecycle
(created / unchanged / updated / conflict-ignored) and at WARNING for retries and
dead-lettering, so a demo transcript reads as an operations story.
"""

from __future__ import annotations

import logging
import sys


def configure_logging(level: int = logging.INFO) -> None:
    """Configure root logging once, idempotently, with a compact timestamped format."""
    root = logging.getLogger()
    if getattr(configure_logging, "_configured", False):
        root.setLevel(level)
        return
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    configure_logging._configured = True  # type: ignore[attr-defined]
