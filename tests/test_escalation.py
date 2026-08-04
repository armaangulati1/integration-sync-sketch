"""Escalation rules: each one fires on a planted condition and stays quiet otherwise.

The pattern is the one the `ops-sql-forensics` sibling uses: start from a HEALTHY board that
must produce zero escalations, then plant exactly one at-risk condition per test and assert
that exactly the expected rule fires. The healthy-board test is what makes the rest mean
something. A rule layer that fires on everything is indistinguishable from one that works,
until the first week somebody ignores it.
"""

from __future__ import annotations

from datetime import date

from integration_sync.crm_store import CrmStore
from integration_sync.escalation import (
    RULE_BLOCKING_TICKET_OVERDUE,
    RULE_DEPENDENCY_SLIP,
    RULE_MILESTONE_OVERDUE,
    RULE_PROJECTED_LATE,
    RULE_UNOWNED_MILESTONE,
    RuleConfig,
    evaluate,
    record,
    tickets_by_milestone,
)
from integration_sync.milestones import STATUS_DONE, STATUS_IN_PROGRESS, build_plan

AS_OF = date(2026, 4, 20)
OWNER = "dana.rivers@marnovek-health.example"


def healthy_store() -> CrmStore:
    """A board with nothing wrong with it. Every rule must stay silent on this one."""
    store = CrmStore(":memory:")
    store.init_schema()
    store.upsert_milestone(
        milestone_key="M1", name="SYNTHETIC data mapping signed off",
        account_name="Marnovek Health", owner_email=OWNER,
        planned_start="2026-04-01", planned_end="2026-04-15",
        actual_end="2026-04-14", status=STATUS_DONE,
    )
    store.upsert_milestone(
        milestone_key="M2", name="SYNTHETIC interface live in the test environment",
        account_name="Marnovek Health", owner_email=OWNER,
        planned_start="2026-04-15", planned_end="2026-05-10",
        status=STATUS_IN_PROGRESS, depends_on=("M1",),
    )
    add_ticket(store, "OPS-301", milestone="M2", due="2026-05-01", is_open=True)
    add_ticket(store, "OPS-302", milestone="M2", due="2026-04-05", is_open=False)
    return store


def add_ticket(store, key, *, milestone, due, is_open, status=None):
    store.upsert_ticket(
        ticket_key=key,
        summary=f"SYNTHETIC {key}",
        status=status or ("Open" if is_open else "Done"),
        is_open=is_open,
        assignee_email=OWNER,
        due_date=due,
        milestone_key=milestone,
        updated_at="2026-04-01T09:00:00+00:00",
    )


def run(store, *, as_of=AS_OF, config=None):
    plan = build_plan(store, as_of)
    return evaluate(plan, tickets_by_milestone(store), as_of=as_of, config=config)


def rule_ids(escalations):
    return sorted({e.rule_id for e in escalations})


# -- the quiet case -----------------------------------------------------------


def test_a_healthy_board_produces_no_escalations():
    store = healthy_store()
    assert run(store) == []
    store.close()


def test_a_completed_but_late_milestone_does_not_escalate():
    # Finishing late is a reporting fact. There is nothing left to escalate about it.
    store = healthy_store()
    store.upsert_milestone(
        milestone_key="M1", name="SYNTHETIC data mapping signed off",
        owner_email=OWNER, planned_start="2026-04-01", planned_end="2026-04-15",
        actual_end="2026-04-17", status=STATUS_DONE,
    )
    assert not any(e.milestone_key == "M1" for e in run(store))
    store.close()


# -- R1 milestone overdue -----------------------------------------------------


def test_planted_overdue_milestone_escalates():
    store = healthy_store()
    store.upsert_milestone(
        milestone_key="M2", name="SYNTHETIC interface live in the test environment",
        owner_email=OWNER, planned_start="2026-04-15", planned_end="2026-04-12",
        status=STATUS_IN_PROGRESS, depends_on=("M1",),
    )
    found = run(store)
    assert RULE_MILESTONE_OVERDUE in rule_ids(found)
    overdue = [e for e in found if e.rule_id == RULE_MILESTONE_OVERDUE]
    assert overdue[0].milestone_key == "M2"
    assert "8d late" in overdue[0].detail
    store.close()


def test_a_milestone_due_today_is_not_yet_overdue():
    # Boundary: due today is on time, not late.
    store = healthy_store()
    store.upsert_milestone(
        milestone_key="M2", name="SYNTHETIC interface live",
        owner_email=OWNER, planned_end=AS_OF.isoformat(),
        status=STATUS_IN_PROGRESS, depends_on=("M1",),
    )
    assert RULE_MILESTONE_OVERDUE not in rule_ids(run(store))
    store.close()


# -- R2 blocking ticket overdue -----------------------------------------------


def test_planted_overdue_open_ticket_escalates_its_milestone():
    store = healthy_store()
    add_ticket(store, "OPS-303", milestone="M2", due="2026-04-11", is_open=True)
    found = run(store)
    blocking = [e for e in found if e.rule_id == RULE_BLOCKING_TICKET_OVERDUE]
    assert len(blocking) == 1
    assert blocking[0].subject_key == "OPS-303"
    assert blocking[0].milestone_key == "M2"
    assert "9d ago" in blocking[0].detail
    store.close()


def test_a_closed_overdue_ticket_stays_quiet():
    # The control for the rule above. The healthy board already contains a CLOSED ticket
    # whose due date has passed (OPS-302); it must never have fired.
    store = healthy_store()
    assert RULE_BLOCKING_TICKET_OVERDUE not in rule_ids(run(store))
    store.close()


def test_two_overdue_tickets_produce_two_distinct_escalations():
    store = healthy_store()
    add_ticket(store, "OPS-303", milestone="M2", due="2026-04-11", is_open=True)
    add_ticket(store, "OPS-304", milestone="M2", due="2026-04-02", is_open=True)
    blocking = [e for e in run(store) if e.rule_id == RULE_BLOCKING_TICKET_OVERDUE]
    assert {e.subject_key for e in blocking} == {"OPS-303", "OPS-304"}
    store.close()


def test_a_ticket_pointing_at_no_milestone_is_ignored():
    store = healthy_store()
    add_ticket(store, "OPS-305", milestone="", due="2026-04-01", is_open=True)
    assert RULE_BLOCKING_TICKET_OVERDUE not in rule_ids(run(store))
    store.close()


# -- R3 dependency slip -------------------------------------------------------


def test_planted_dependency_slip_past_the_threshold_escalates():
    store = healthy_store()
    store.upsert_milestone(
        milestone_key="M1", name="SYNTHETIC data mapping signed off",
        owner_email=OWNER, planned_end="2026-04-15",
        actual_end="2026-04-19", status=STATUS_DONE,  # 4d slip against a 3d tolerance
    )
    found = [e for e in run(store) if e.rule_id == RULE_DEPENDENCY_SLIP]
    assert len(found) == 1
    assert found[0].milestone_key == "M2"
    assert found[0].subject_key == "M1"
    store.close()


def test_a_dependency_exactly_at_the_threshold_stays_quiet():
    # Boundary control: the tolerance is a tolerance, not a trigger.
    store = healthy_store()
    store.upsert_milestone(
        milestone_key="M1", name="SYNTHETIC data mapping signed off",
        owner_email=OWNER, planned_end="2026-04-15",
        actual_end="2026-04-18", status=STATUS_DONE,  # exactly 3d
    )
    assert RULE_DEPENDENCY_SLIP not in rule_ids(run(store))
    store.close()


def test_the_threshold_is_configurable():
    store = healthy_store()
    store.upsert_milestone(
        milestone_key="M1", name="SYNTHETIC data mapping signed off",
        owner_email=OWNER, planned_end="2026-04-15",
        actual_end="2026-04-18", status=STATUS_DONE,
    )
    tightened = RuleConfig(dependency_slip_threshold_days=2)
    assert RULE_DEPENDENCY_SLIP in rule_ids(run(store, config=tightened))
    store.close()


# -- R4 projected late --------------------------------------------------------


def test_inherited_slip_escalates_before_the_milestone_is_late():
    # The rule that earns its keep: M2 is due 2026-05-10 and today is 2026-04-20, so it is
    # not late by any calendar reading, and it is already projected to miss.
    store = healthy_store()
    store.upsert_milestone(
        milestone_key="M1", name="SYNTHETIC data mapping signed off",
        owner_email=OWNER, planned_end="2026-04-15",
        actual_end="2026-04-19", status=STATUS_DONE,
    )
    found = run(store)
    projected = [e for e in found if e.rule_id == RULE_PROJECTED_LATE]
    assert len(projected) == 1
    assert projected[0].milestone_key == "M2"
    assert "2026-05-14" in projected[0].detail  # 05-10 planned, 4d inherited
    assert RULE_MILESTONE_OVERDUE not in rule_ids(found)  # it is genuinely not late yet
    store.close()


def test_an_on_track_successor_does_not_get_a_projection_warning():
    store = healthy_store()
    assert RULE_PROJECTED_LATE not in rule_ids(run(store))
    store.close()


# -- R5 unowned milestone -----------------------------------------------------


def test_planted_unowned_milestone_due_soon_escalates():
    store = healthy_store()
    store.upsert_milestone(
        milestone_key="M2", name="SYNTHETIC interface live",
        owner_email="", planned_end="2026-04-24",
        status=STATUS_IN_PROGRESS, depends_on=("M1",),
    )
    found = [e for e in run(store) if e.rule_id == RULE_UNOWNED_MILESTONE]
    assert len(found) == 1
    assert "due in 4d" in found[0].detail
    store.close()


def test_an_unowned_milestone_far_out_stays_quiet():
    store = healthy_store()
    store.upsert_milestone(
        milestone_key="M2", name="SYNTHETIC interface live",
        owner_email="", planned_end="2026-06-30",
        status=STATUS_IN_PROGRESS, depends_on=("M1",),
    )
    assert RULE_UNOWNED_MILESTONE not in rule_ids(run(store))
    store.close()


# -- persistence --------------------------------------------------------------


def test_escalations_persist_and_re_running_the_same_date_is_a_no_op():
    store = healthy_store()
    add_ticket(store, "OPS-303", milestone="M2", due="2026-04-11", is_open=True)
    found = run(store)
    assert record(store, found, as_of=AS_OF) == len(found)
    assert record(store, found, as_of=AS_OF) == 0  # already raised, do not page twice
    assert store.count("escalations") == len(found)
    store.close()


def test_the_same_risk_on_a_later_date_is_a_new_escalation():
    store = healthy_store()
    add_ticket(store, "OPS-303", milestone="M2", due="2026-04-11", is_open=True)
    record(store, run(store), as_of=AS_OF)
    later = date(2026, 4, 21)
    assert record(store, run(store, as_of=later), as_of=later) > 0
    assert len(store.list_escalations(as_of=later.isoformat())) > 0
    store.close()


def test_severity_ordering_puts_high_first():
    store = healthy_store()
    add_ticket(store, "OPS-303", milestone="M2", due="2026-04-11", is_open=True)
    store.upsert_milestone(
        milestone_key="M2", name="SYNTHETIC interface live",
        owner_email="", planned_end="2026-04-24",
        status=STATUS_IN_PROGRESS, depends_on=("M1",),
    )
    severities = [e.severity for e in run(store)]
    assert severities == sorted(severities, key=lambda s: {"high": 0, "medium": 1}.get(s, 2))
    store.close()
