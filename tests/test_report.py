"""The report artifact: it renders the derived numbers, and it renders them the same way twice."""

from __future__ import annotations

from datetime import date

from integration_sync.crm_store import CrmStore
from integration_sync.escalation import evaluate, tickets_by_milestone
from integration_sync.milestones import STATUS_DONE, STATUS_IN_PROGRESS, build_plan
from integration_sync.report import render

AS_OF = date(2026, 4, 20)
OWNER = "dana.rivers@marnovek-health.example"


def build(late: bool) -> CrmStore:
    store = CrmStore(":memory:")
    store.init_schema()
    store.upsert_milestone(
        milestone_key="M1", name="SYNTHETIC mapping signed off", owner_email=OWNER,
        planned_start="2026-04-01", planned_end="2026-04-15",
        actual_end="2026-04-25" if late else "2026-04-14", status=STATUS_DONE,
    )
    store.upsert_milestone(
        milestone_key="M2", name="SYNTHETIC interface live", owner_email=OWNER,
        planned_start="2026-04-15", planned_end="2026-05-10",
        status=STATUS_IN_PROGRESS, depends_on=("M1",),
    )
    return store


def render_for(store) -> str:
    plan = build_plan(store, AS_OF)
    grouped = tickets_by_milestone(store)
    return render(plan, evaluate(plan, grouped, as_of=AS_OF), as_of=AS_OF)


def test_healthy_plan_reports_no_escalations():
    store = build(late=False)
    text = render_for(store)
    assert "ESCALATIONS: none" in text
    assert "on track" in text
    store.close()


def test_slipped_plan_reports_the_projection_and_the_reason():
    store = build(late=True)
    text = render_for(store)
    assert "+10d late" in text          # M1 finished ten days past plan
    assert "2026-05-20" in text         # M2 projected: 05-10 plus the inherited ten days
    assert "dependency_slip" in text
    assert "depends on: M1" in text
    store.close()


def test_the_report_is_deterministic():
    first = render_for(build(late=True))
    second = render_for(build(late=True))
    assert first == second
