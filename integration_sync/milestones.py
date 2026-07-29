"""Project milestones: planned versus actual dates, dependencies, and derived slip.

A milestone is a dated deliverable with a planned window, an optional actual completion
date, and zero or more predecessors. The interesting part is not storing that; it is
answering "given what we know today, when does this actually land, and which upstream item
is the reason?" without a human maintaining a second spreadsheet of the answer.

The slip model, stated plainly so it can be argued with:

    done milestone       finish = actual_end
                         slip   = finish - planned_end        (can be negative: early)

    open milestone       inherited = max(0, largest slip among its direct predecessors)
                         finish    = max(planned_end + inherited, as_of)
                         slip      = finish - planned_end      (never negative while open)

Two properties fall out of that, and both are the point:

1. An open milestone whose date has passed slips by exactly as much as it is late, because
   it cannot finish in the past. It does not need a human to mark it late.
2. An open milestone whose own date is still in the FUTURE can already carry slip, inherited
   from a predecessor that ran long. That is the early warning: the item is not late yet and
   is already known to be landing late.

Slip propagates along the dependency edges in topological order, so a delay at the front of
a chain shows up at the end of it. A cycle in the graph is an error, not a default, because
silently picking an order would make the numbers unexplainable.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date, timedelta

STATUS_DONE = "done"
STATUS_IN_PROGRESS = "in_progress"
STATUS_PLANNED = "planned"


class DependencyCycle(ValueError):
    """The milestone graph contains a cycle, so no ordering of the slip math exists."""


class UnknownDependency(ValueError):
    """A milestone depends on a key that does not exist. A typo, not a schedule."""


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(str(value).strip()[:10])


@dataclass(frozen=True)
class MilestoneView:
    """A milestone with its derived schedule position at a given ``as_of`` date."""

    key: str
    name: str
    account_name: str
    owner_email: str
    planned_start: date | None
    planned_end: date
    actual_end: date | None
    status: str
    depends_on: tuple[str, ...]
    inherited_slip_days: int
    projected_end: date
    slip_days: int
    as_of: date

    @property
    def is_done(self) -> bool:
        return self.status == STATUS_DONE and self.actual_end is not None

    @property
    def is_overdue(self) -> bool:
        """Late on its own account: still open and its planned date has already passed."""
        return not self.is_done and self.planned_end < self.as_of

    @property
    def is_at_risk(self) -> bool:
        return self.slip_days > 0


def topological_order(keys: list[str], deps: dict[str, tuple[str, ...]]) -> list[str]:
    """Kahn's algorithm. Raises ``DependencyCycle`` if the graph does not resolve.

    Ties are broken by sorted key so the order, and therefore every number derived from it,
    is identical on every run.
    """
    known = set(keys)
    for key, parents in deps.items():
        for parent in parents:
            if parent not in known:
                raise UnknownDependency(f"{key!r} depends on unknown milestone {parent!r}")

    indegree = {key: len(deps.get(key, ())) for key in keys}
    children: dict[str, list[str]] = {key: [] for key in keys}
    for key, parents in deps.items():
        for parent in parents:
            children[parent].append(key)

    ready = deque(sorted(k for k, d in indegree.items() if d == 0))
    order: list[str] = []
    while ready:
        key = ready.popleft()
        order.append(key)
        newly_ready = []
        for child in children[key]:
            indegree[child] -= 1
            if indegree[child] == 0:
                newly_ready.append(child)
        for child in sorted(newly_ready):
            ready.append(child)

    if len(order) != len(keys):
        stuck = sorted(set(keys) - set(order))
        raise DependencyCycle(f"cycle among milestones {stuck}")
    return order


def build_plan(store, as_of: date) -> dict[str, MilestoneView]:
    """Compute every milestone's derived schedule position as of ``as_of``."""
    rows = {row["milestone_key"]: row for row in store.all_milestones()}
    deps = {key: tuple(store.deps_of(key)) for key in rows}
    order = topological_order(list(rows), deps)

    views: dict[str, MilestoneView] = {}
    for key in order:
        row = rows[key]
        planned_end = parse_date(row["planned_end"])
        if planned_end is None:
            raise ValueError(f"milestone {key!r} has no planned_end")
        actual_end = parse_date(row["actual_end"])
        status = row["status"]
        parents = deps.get(key, ())

        inherited = max((max(0, views[p].slip_days) for p in parents), default=0)

        if status == STATUS_DONE and actual_end is not None:
            projected_end = actual_end
        else:
            # An open milestone cannot finish in the past, and cannot finish before its
            # predecessors do, so its projection is the later of those two floors.
            projected_end = max(planned_end + timedelta(days=inherited), as_of)

        views[key] = MilestoneView(
            key=key,
            name=row["name"],
            account_name=row["account_name"],
            owner_email=row["owner_email"],
            planned_start=parse_date(row["planned_start"]),
            planned_end=planned_end,
            actual_end=actual_end,
            status=status,
            depends_on=parents,
            inherited_slip_days=inherited,
            projected_end=projected_end,
            slip_days=(projected_end - planned_end).days,
            as_of=as_of,
        )
    return views


def load_milestones(store, path: str) -> int:
    """Load a milestone plan from a JSON fixture into the store. Returns the row count."""
    import json
    from pathlib import Path

    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    with store.transaction():
        for row in rows:
            store.upsert_milestone(
                milestone_key=row["milestone_key"],
                name=row.get("name", ""),
                account_name=row.get("account_name", ""),
                owner_email=row.get("owner_email", ""),
                planned_start=row.get("planned_start", ""),
                planned_end=row.get("planned_end", ""),
                actual_end=row.get("actual_end"),
                status=row.get("status", STATUS_PLANNED),
                depends_on=tuple(row.get("depends_on", ())),
            )
    return len(rows)


def plan_in_schedule_order(plan: dict[str, MilestoneView]) -> list[MilestoneView]:
    """Milestones sorted for reporting: by planned date, then key."""
    return sorted(plan.values(), key=lambda m: (m.planned_end, m.key))
