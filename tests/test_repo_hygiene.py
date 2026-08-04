"""Guard: no absolute home-directory path may appear in a committed text file.

Why this exists. ``demo_transcript.txt`` is a committed evidence artifact produced by
running the demos, and the demos print paths. An absolute path captured into it publishes
the directory layout of whatever machine produced it, including the names of parent
directories that were never meant to be public. That redaction was originally done by hand,
and anything done by hand comes undone the next time the artifact is regenerated.

Design note, mirroring the company-name guard beside it: the forbidden prefixes are
assembled from fragments so the literal strings never appear in this file. The guard
therefore does not have to exempt itself from its own scan, which is the failure mode where
a guard quietly stops guarding.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

EXCLUDED_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", "data"}
SCANNED_SUFFIXES = {".py", ".md", ".txt", ".toml", ".yml", ".yaml", ".json", ".cfg", ".ini", ".sh"}

# Assembled, never written literally. See the design note above.
HOME_PREFIXES = ("/" + "Users" + "/", "/" + "home" + "/", "C:" + "\\" + "Users" + "\\")


def _iter_files():
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        if path.suffix in SCANNED_SUFFIXES:
            yield path


def test_the_guard_can_actually_fire():
    # A check that cannot return a positive is not evidence.
    pattern = re.compile("|".join(re.escape(p) for p in HOME_PREFIXES))
    assert pattern.search(HOME_PREFIXES[0] + "someone/secret-project")
    assert not pattern.search("data/demo.db")


def test_no_absolute_home_paths_in_committed_files():
    pattern = re.compile("|".join(re.escape(p) for p in HOME_PREFIXES))
    violations: list[str] = []
    for path in _iter_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                violations.append(f"{path.relative_to(REPO_ROOT)}:{line_number}")
    assert not violations, (
        "absolute home-directory paths found in committed files:\n" + "\n".join(violations)
    )
