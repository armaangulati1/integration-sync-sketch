"""Milestone schedule math: slip, dependency propagation, and the graph itself.

Every case here plants one specific schedule condition and asserts the derived number, so a
regression in the slip model shows up as a wrong day count rather than a vague failure.
"""

from __future__ import annotations

from datetime import date

import pytest

from integration_sync.crm_store import CrmStore
from integration_sync.milestones import (
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    DependencyCycle,
    UnknownDependency,
    build_plan,
    topological_order,
)


def store_with(*milestones: dict) -> CrmStore:
    store = CrmStore(":memory:")
    store.init_schema()
    for m in milestones:
        store.upsert_milestone(**m)
    return store


def ms(key, planned_end, **kw) -> dict:
    base = {
        "milestone_key": key,
        "name": f"SYNTHETIC {key}",
        "account_name": "Zeltrovan Health",
        "owner_email": "dana.rivers@zeltrovan-health.example",
        "planned_start": "2026-04-01",
        "planned_end": planned_end,
        "status": STATUS_IN_PROGRESS,
    }
    base.update(kw)
    return base


# -- the graph ----------------------------------------------------------------


def test_topological_order_is_deterministic():
    deps = {"C": ("A", "B"), "B": ("A",)}
    assert topological_order(["A", "B", "C"], deps) == ["A", "B", "C"]
    # Same graph, different input order, same answer.
    assert topological_order(["C", "B", "A"], deps) == ["A", "B", "C"]


def test_cycle_is_an_error_not_a_guess():
    with pytest.raises(DependencyCycle):
        topological_order(["A", "B"], {"A": ("B",), "B": ("A",)})


def test_dependency_on_a_nonexistent_milestone_is_an_error():
    with pytest.raises(UnknownDependency):
        topological_order(["A"], {"A": ("GHOST",)})


# -- slip on a single milestone -----------------------------------------------


def test_completed_early_reports_negative_slip():
    store = store_with(
        ms("M1", "2026-04-10", status=STATUS_DONE, actual_end="2026-04-07")
    )
    plan = build_plan(store, date(2026, 4, 20))
    assert plan["M1"].slip_days == -3
    assert plan["M1"].is_done
    store.close()


def test_completed_late_reports_the_actual_overrun():
    store = store_with(
        ms("M1", "2026-04-10", status=STATUS_DONE, actual_end="2026-04-15")
    )
    plan = build_plan(store, date(2026, 4, 20))
    assert plan["M1"].slip_days == 5
    store.close()


def test_open_and_still_in_the_future_is_on_track():
    store = store_with(ms("M1", "2026-04-30"))
    plan = build_plan(store, date(2026, 4, 20))
    assert plan["M1"].slip_days == 0
    assert not plan["M1"].is_overdue
    store.close()


def test_open_and_past_due_slips_by_exactly_how_late_it_is():
    # It cannot finish in the past, so today becomes the floor on its projection.
    store = store_with(ms("M1", "2026-04-10"))
    plan = build_plan(store, date(2026, 4, 20))
    assert plan["M1"].slip_days == 10
    assert plan["M1"].projected_end == date(2026, 4, 20)
    assert plan["M1"].is_overdue
    store.close()


# -- propagation --------------------------------------------------------------


def test_slip_propagates_from_a_predecessor():
    store = store_with(
        ms("M1", "2026-04-10", status=STATUS_DONE, actual_end="2026-04-16"),
        ms("M2", "2026-04-30", depends_on=("M1",)),
    )
    plan = build_plan(store, date(2026, 4, 20))
    assert plan["M1"].slip_days == 6
    assert plan["M2"].inherited_slip_days == 6
    assert plan["M2"].projected_end == date(2026, 5, 6)
    assert plan["M2"].slip_days == 6
    # The early warning: M2's own date has not arrived, and it is already landing late.
    assert not plan["M2"].is_overdue
    assert plan["M2"].is_at_risk
    store.close()


def test_an_early_predecessor_does_not_pull_a_successor_forward():
    # Finishing early creates slack, not a new commitment. Negative slip must not propagate.
    store = store_with(
        ms("M1", "2026-04-10", status=STATUS_DONE, actual_end="2026-04-05"),
        ms("M2", "2026-04-30", depends_on=("M1",)),
    )
    plan = build_plan(store, date(2026, 4, 20))
    assert plan["M1"].slip_days == -5
    assert plan["M2"].inherited_slip_days == 0
    assert plan["M2"].projected_end == date(2026, 4, 30)
    store.close()


def test_slip_propagates_down_a_chain():
    store = store_with(
        ms("M1", "2026-04-10", status=STATUS_DONE, actual_end="2026-04-18"),
        ms("M2", "2026-04-20", depends_on=("M1",)),
        ms("M3", "2026-05-30", depends_on=("M2",)),
    )
    plan = build_plan(store, date(2026, 4, 15))
    assert plan["M1"].slip_days == 8
    assert plan["M2"].slip_days == 8  # planned 04-20 + 8d inherited
    assert plan["M3"].slip_days == 8  # and on down the chain
    assert plan["M3"].projected_end == date(2026, 6, 7)
    store.close()


def test_the_worst_predecessor_sets_the_projection():
    store = store_with(
        ms("M1", "2026-04-10", status=STATUS_DONE, actual_end="2026-04-12"),
        ms("M2", "2026-04-10", status=STATUS_DONE, actual_end="2026-04-19"),
        ms("M3", "2026-04-30", depends_on=("M1", "M2")),
    )
    plan = build_plan(store, date(2026, 4, 20))
    assert plan["M3"].inherited_slip_days == 9  # the M2 branch, not the M1 branch
    store.close()


def test_overdue_beats_inherited_when_it_is_larger():
    # An open milestone that is BOTH past due and downstream of a slip takes the larger floor.
    store = store_with(
        ms("M1", "2026-04-01", status=STATUS_DONE, actual_end="2026-04-03"),
        ms("M2", "2026-04-05", depends_on=("M1",)),
    )
    plan = build_plan(store, date(2026, 4, 25))
    assert plan["M2"].inherited_slip_days == 2
    assert plan["M2"].projected_end == date(2026, 4, 25)  # today, not 04-07
    assert plan["M2"].slip_days == 20
    store.close()
