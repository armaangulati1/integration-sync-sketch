"""Guard: no real company name may be used as fixture data anywhere in the committed tree.

The invariant is about *synthetic data posing as real*. Accounts, contacts, deal names,
email domains and demo output must all be invented, so that nothing in this repository can
be mistaken for a real customer or a real engagement. Prose that names a real organization
factually is a different thing and is allowed: the verification record in
BRANCH_EVIDENCE.md names the real companies that the invented fixture names were checked
against, which is the whole point of that record. The blocklist is therefore a list of
names that must never be *used*, not a ban on the characters ever appearing.

Design (mirrors the externalized-blocklist pattern): the names to forbid are loaded from
two data files, a committed example file of invented placeholders and a gitignored local
file holding the real names. This test module contains NO names of its own, so it does not
exempt itself from the scan. The only paths excluded are the two list files themselves,
which legitimately contain the forbidden strings, and non-source directories.

Matching is whole-word and case-insensitive.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BLOCKLIST_LOCAL = REPO_ROOT / "tests" / "vendor_blocklist.local.txt"
BLOCKLIST_EXAMPLE = REPO_ROOT / "tests" / "vendor_blocklist.example.txt"

EXCLUDED_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
EXCLUDED_FILES = {BLOCKLIST_LOCAL.resolve(), BLOCKLIST_EXAMPLE.resolve()}
SCANNED_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".toml",
    ".yml",
    ".yaml",
    ".cfg",
    ".ini",
    ".sh",
    ".sql",
    ".json",
}
SCANNED_NAMES = {"LICENSE", ".gitignore", "Dockerfile"}


def _load_names(path: Path) -> list[str]:
    if not path.exists():
        return []
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line)
    return names


def _blocklist() -> list[str]:
    names = set(_load_names(BLOCKLIST_EXAMPLE)) | set(_load_names(BLOCKLIST_LOCAL))
    return sorted(names)


def _iter_files():
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        if path.resolve() in EXCLUDED_FILES:
            continue
        if path.suffix in SCANNED_SUFFIXES or path.name in SCANNED_NAMES:
            yield path


def test_blocklist_is_non_trivial():
    # If neither list loads, the guard would be scanning for nothing.
    assert _blocklist(), "no blocklist names loaded; the guard would be a no-op"


def test_no_company_names_in_tree():
    names = _blocklist()
    patterns = [(name, re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)) for name in names]
    violations: list[str] = []
    for path in _iter_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name, pattern in patterns:
            if pattern.search(text):
                violations.append(f"{path.relative_to(REPO_ROOT)} contains blocked name {name!r}")
    assert not violations, "blocked company names found:\n" + "\n".join(violations)
