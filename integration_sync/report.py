"""The report artifact: milestones, slip, and escalations as plain text.

Deliberately text, deliberately deterministic, and deliberately narrow. The value of this
layer is that the same synced state always renders the same page, so the report can be
committed, diffed between runs, and asserted on in a test.
"""

from __future__ import annotations

from datetime import date

from .escalation import Escalation
from .milestones import MilestoneView, plan_in_schedule_order

WIDTH = 84


def _rule(char: str = "-") -> str:
    return char * WIDTH


def _fmt_date(value: date | None) -> str:
    return value.isoformat() if value is not None else "-"


def _slip_label(milestone: MilestoneView) -> str:
    if milestone.is_done:
        if milestone.slip_days > 0:
            return f"+{milestone.slip_days}d late"
        if milestone.slip_days < 0:
            return f"{milestone.slip_days}d early"
        return "on time"
    if milestone.slip_days > 0:
        return f"+{milestone.slip_days}d projected"
    return "on track"


def render(
    plan: dict[str, MilestoneView],
    escalations: list[Escalation],
    *,
    as_of: date,
    ticket_counts: dict[str, tuple[int, int]] | None = None,
) -> str:
    """Render the milestone and escalation report for ``as_of``."""
    counts = ticket_counts or {}
    lines: list[str] = []
    lines.append(_rule("="))
    lines.append(f"PROJECT MILESTONE REPORT (synthetic data)   as of {as_of.isoformat()}")
    lines.append(_rule("="))
    lines.append("")
    lines.append(
        f"{'MILESTONE':<10} {'PLANNED':<11} {'ACTUAL':<11} {'PROJECTED':<11} "
        f"{'SLIP':<15} {'OPEN TIX':<9} STATUS"
    )
    lines.append(_rule())

    for milestone in plan_in_schedule_order(plan):
        open_count, total_count = counts.get(milestone.key, (0, 0))
        lines.append(
            f"{milestone.key:<10} "
            f"{_fmt_date(milestone.planned_end):<11} "
            f"{_fmt_date(milestone.actual_end):<11} "
            f"{_fmt_date(milestone.projected_end):<11} "
            f"{_slip_label(milestone):<15} "
            f"{f'{open_count}/{total_count}':<9} "
            f"{milestone.status}"
        )
        lines.append(f"           {milestone.name}")
        if milestone.depends_on:
            lines.append(f"           depends on: {', '.join(milestone.depends_on)}")

    lines.append("")
    lines.append(_rule("="))
    if not escalations:
        lines.append("ESCALATIONS: none. Every open milestone is on track as of this date.")
        lines.append(_rule("="))
        return "\n".join(lines) + "\n"

    lines.append(f"ESCALATIONS: {len(escalations)}")
    lines.append(_rule("="))
    for esc in escalations:
        subject = f" [{esc.subject_key}]" if esc.subject_key else ""
        lines.append(f"  {esc.severity.upper():<7} {esc.milestone_key}{subject}  {esc.rule_id}")
        lines.append(f"          {esc.detail}")
    lines.append(_rule("="))
    return "\n".join(lines) + "\n"
