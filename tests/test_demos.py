"""End-to-end smoke tests: the two demo scripts run clean and self-assert.

Both demos end with an internal assertion/`SystemExit` if their invariants break, so a
zero exit code is itself a meaningful check. We also assert on marker strings so a silent
change in behavior is caught.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / ".venv" / "bin" / "python"
PYTHON = str(PY) if PY.exists() else sys.executable


def _run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, str(REPO / "scripts" / script)],
        capture_output=True, text=True, cwd=str(REPO), timeout=120,
    )


def test_happy_path_demo_runs_and_is_idempotent():
    result = _run("run_demo.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "snapshots identical across re-runs: True" in result.stdout
    assert "OK: sync is idempotent" in result.stdout


def test_failure_demo_runs_and_handles_every_mode():
    result = _run("failure_demo.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK: every failure mode handled as designed." in result.stdout
    assert "conflict_ignored" in result.stdout
    assert "transient_exhausted" in result.stdout


def test_milestone_demo_runs_throttled_drifted_and_self_asserts():
    result = _run("milestone_demo.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK: throttled read, drift absorbed" in result.stdout
    # The two integration hazards actually fired during the run.
    assert "1 throttled" in result.stdout
    assert "alias_used: due_date" in result.stdout
    # And the rule layer produced its output.
    assert "milestone_overdue" in result.stdout
    assert "blocking_ticket_overdue" in result.stdout
    assert "projected_late" in result.stdout
