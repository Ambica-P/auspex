"""Post-conditions (spec 10), checked against live state: an action is done only when they pass.

Each check returns PASS, FAIL or WAIT (not decidable yet, e.g. a closure window not started).
State comes from the bridge's events (RosState) and the picture (forecast ETAs).

Task state over ROS (confirmed M2/M5): `underway` while a robot's `task_id` is the task (also
while interrupted); `queued` while the dispatcher holds it; otherwise it has ended, and
completed, failed and cancelled cannot be told apart there, so any of them counts as `ended`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from auspex.core.models import (
    JobEtaBefore,
    LanesReopened,
    PostCondition,
    RobotHeldAt,
    SegmentClearFor,
    TaskStateIs,
)
from auspex.forecast.eta import RobotPath
from auspex.picture.ros_state import StateView
from auspex.site.model import Site
from auspex.site.navgraph import NavGraph

HELD_RADIUS_M = 1.0
ENDED_STATES = frozenset({"completed", "failed", "canceled", "killed", "skipped", "ended"})
QUEUED_DISPATCH = frozenset({1, 2})  # QUEUED, SELECTED


class Outcome(StrEnum):
    PASS = "pass"  # noqa: S105 (an outcome, not a password)
    FAIL = "fail"
    WAIT = "wait"


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    detail: str


def task_state(state: StateView, task_id: str) -> str:
    for fleet in state.fleets.values():
        for r in fleet.get("robots", []):
            if r.get("task_id") == task_id:
                return "underway"
    for d in state.dispatch.get("active", []):
        if d.get("task_id") == task_id and int(d.get("status", 0)) in QUEUED_DISPATCH:
            return "queued"
    return "ended"


def _robot(state: StateView, fleet: str, robot: str) -> dict[str, object] | None:
    for r in state.fleets.get(fleet, {}).get("robots", []):
        if r.get("name") == robot:
            return dict(r)
    return None


def check(
    cond: PostCondition,
    *,
    site: Site,
    graphs: Mapping[str, NavGraph],
    state: StateView,
    paths: Mapping[str, RobotPath],
    now: datetime,
) -> Result:
    if isinstance(cond, SegmentClearFor):
        lanes = set(site.segments[cond.segment_urn].rmf_lanes.get(cond.fleet_name, []))
        closed = state.closed.get(cond.fleet_name, set())
        if now < cond.window.start:
            return Result(Outcome.WAIT, "window not started")
        if lanes and lanes <= closed:
            return Result(Outcome.PASS, f"lanes {sorted(lanes)} closed for {cond.fleet_name}")
        return Result(Outcome.FAIL, f"lanes {sorted(lanes - closed)} still open")
    if isinstance(cond, LanesReopened):
        lanes = set(site.segments[cond.segment_urn].rmf_lanes.get(cond.fleet_name, []))
        still = lanes & state.closed.get(cond.fleet_name, set())
        if not still:
            return Result(Outcome.PASS, f"lanes {sorted(lanes)} open for {cond.fleet_name}")
        return Result(Outcome.FAIL, f"lanes {sorted(still)} still closed")
    if isinstance(cond, TaskStateIs):
        observed = task_state(state, cond.task_id)
        wanted = set(cond.states)
        ok = observed in wanted or (observed == "ended" and bool(wanted & ENDED_STATES))
        if ok:
            return Result(Outcome.PASS, f"task {cond.task_id} is {observed}")
        if now < cond.by:
            return Result(Outcome.WAIT, f"task {cond.task_id} is {observed}")
        return Result(Outcome.FAIL, f"task {cond.task_id} is {observed}, wanted {sorted(wanted)}")
    if isinstance(cond, RobotHeldAt):
        r = _robot(state, cond.fleet_name, cond.robot_name)
        graph = graphs.get(cond.fleet_name)
        wp = graph.waypoint(cond.waypoint) if graph else None
        if r is None or wp is None:
            return Result(Outcome.FAIL, "robot or waypoint unknown")
        dist = math.hypot(float(str(r["x"])) - wp.x, float(str(r["y"])) - wp.y)
        if r.get("level") == wp.level and dist <= HELD_RADIUS_M:
            return Result(Outcome.PASS, f"{cond.robot_name} at {cond.waypoint} ({dist:.1f} m)")
        return Result(Outcome.FAIL, f"{cond.robot_name} {dist:.1f} m from {cond.waypoint}")
    if isinstance(cond, JobEtaBefore):
        path = paths.get(cond.job_urn)
        if path is None:
            return Result(Outcome.WAIT, "no forecast for the job yet")
        if path.eta <= cond.due_at:
            return Result(Outcome.PASS, f"ETA {path.eta.isoformat(timespec='seconds')}")
        return Result(Outcome.FAIL, f"ETA {path.eta.isoformat(timespec='seconds')} after due")
    raise AssertionError(f"unhandled post-condition {cond!r}")
