"""Risk identification and escalation: a deterministic rule layer over the plan.

Five rules run against the milestone plan and the synced ticket board. Each one answers a
question a delivery lead would otherwise answer by reading two systems on a Monday morning,
and each one is a pure function of state plus an ``as_of`` date. There is no model here and
no scoring heuristic: the same inputs produce the same escalations, every time, and every
escalation names the exact evidence that produced it.

    R1 milestone_overdue          open, and its planned date has passed
    R2 blocking_ticket_overdue    an OPEN ticket on this milestone is past its own due date
    R3 dependency_slip            a direct predecessor slipped past the tolerated threshold
    R4 projected_late             not late yet, but already projected to land late
    R5 unowned_milestone          coming up soon with nobody's name on it

Two design choices worth defending:

**R4 is the one that earns its keep.** R1 tells you something you could see on a calendar.
R4 fires while the milestone's own date is still in the future, because a predecessor
already ran long, which is the only point at which the news is still actionable.

**Severity is assigned by rule, not computed.** A tunable risk score would be harder to
argue with and no more accurate on a synthetic board of this size. A rule that fires with
its evidence attached can be checked by the person it pages.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .milestones import MilestoneView, parse_date

RULE_MILESTONE_OVERDUE = "milestone_overdue"
RULE_BLOCKING_TICKET_OVERDUE = "blocking_ticket_overdue"
RULE_DEPENDENCY_SLIP = "dependency_slip"
RULE_PROJECTED_LATE = "projected_late"
RULE_UNOWNED_MILESTONE = "unowned_milestone"

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"


@dataclass(frozen=True)
class RuleConfig:
    """Thresholds, in one place, so the rules are tunable without being edited.

    ``dependency_slip_threshold_days`` is a tolerance, not a trigger point: a predecessor
    that slipped by exactly the threshold has not exceeded it and does not escalate. That
    boundary is asserted in the tests, because off-by-one on an alerting threshold is how
    an escalation layer loses its audience.
    """

    dependency_slip_threshold_days: int = 3
    unowned_within_days: int = 7


@dataclass(frozen=True)
class Escalation:
    """One flagged risk, with the evidence that produced it."""

    rule_id: str
    severity: str
    milestone_key: str
    subject_key: str
    detail: str

    def sort_key(self) -> tuple:
        rank = {SEVERITY_HIGH: 0, SEVERITY_MEDIUM: 1, SEVERITY_LOW: 2}
        return (rank.get(self.severity, 9), self.milestone_key, self.rule_id, self.subject_key)


def evaluate(
    plan: dict[str, MilestoneView],
    tickets_by_milestone: dict[str, list],
    *,
    as_of: date,
    config: RuleConfig | None = None,
) -> list[Escalation]:
    """Run every rule over every milestone. Returns escalations in a stable order."""
    cfg = config or RuleConfig()
    found: list[Escalation] = []

    for key in sorted(plan):
        milestone = plan[key]
        if milestone.is_done:
            # A finished milestone cannot be at risk. It may have finished late, which is a
            # reporting fact, not something to page anyone about.
            continue

        if milestone.is_overdue:
            found.append(
                Escalation(
                    RULE_MILESTONE_OVERDUE, SEVERITY_HIGH, key, "",
                    f"planned {milestone.planned_end.isoformat()}, still open on "
                    f"{as_of.isoformat()} ({milestone.slip_days}d late)",
                )
            )

        for ticket in tickets_by_milestone.get(key, []):
            if not _is_open(ticket):
                continue
            due = parse_date(_field(ticket, "due_date"))
            if due is not None and due < as_of:
                found.append(
                    Escalation(
                        RULE_BLOCKING_TICKET_OVERDUE, SEVERITY_HIGH, key,
                        _field(ticket, "ticket_key"),
                        f"ticket {_field(ticket, 'ticket_key')} is {_field(ticket, 'status')} "
                        f"and was due {due.isoformat()} ({(as_of - due).days}d ago)",
                    )
                )

        for parent_key in milestone.depends_on:
            parent = plan[parent_key]
            if parent.slip_days > cfg.dependency_slip_threshold_days:
                found.append(
                    Escalation(
                        RULE_DEPENDENCY_SLIP, SEVERITY_HIGH, key, parent_key,
                        f"depends on {parent_key}, which has slipped {parent.slip_days}d "
                        f"(tolerance {cfg.dependency_slip_threshold_days}d)",
                    )
                )

        if not milestone.is_overdue and milestone.projected_end > milestone.planned_end:
            found.append(
                Escalation(
                    RULE_PROJECTED_LATE, SEVERITY_MEDIUM, key, "",
                    f"planned {milestone.planned_end.isoformat()}, projected "
                    f"{milestone.projected_end.isoformat()} "
                    f"({milestone.inherited_slip_days}d inherited from upstream)",
                )
            )

        days_out = (milestone.planned_end - as_of).days
        if not milestone.owner_email and 0 <= days_out <= cfg.unowned_within_days:
            found.append(
                Escalation(
                    RULE_UNOWNED_MILESTONE, SEVERITY_LOW, key, "",
                    f"due in {days_out}d with no owner assigned",
                )
            )

    return sorted(found, key=Escalation.sort_key)


def _field(ticket, name: str):
    """Read a field from either a sqlite3.Row or a plain dict."""
    try:
        return ticket[name]
    except (IndexError, KeyError):
        return ""


def _is_open(ticket) -> bool:
    return bool(int(_field(ticket, "is_open") or 0))


def tickets_by_milestone(store) -> dict[str, list]:
    """Group the synced ticket board by the milestone each ticket points at."""
    grouped: dict[str, list] = {}
    for row in store.all_tickets():
        key = row["milestone_key"]
        if key:
            grouped.setdefault(key, []).append(row)
    return grouped


def record(store, escalations: list[Escalation], *, as_of: date) -> int:
    """Persist escalations. Returns how many were NEW.

    Re-running the rules for the same date records nothing further, so a scheduled run does
    not re-page on a risk that was already raised.
    """
    written = 0
    with store.transaction():
        for esc in escalations:
            if store.add_escalation(
                as_of=as_of.isoformat(),
                milestone_key=esc.milestone_key,
                rule_id=esc.rule_id,
                severity=esc.severity,
                subject_key=esc.subject_key,
                detail=esc.detail,
            ):
                written += 1
    return written
