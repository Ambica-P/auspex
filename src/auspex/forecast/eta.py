"""ETA model (spec 6.2): shortest-time robot paths over a fleet's navigation graph.

Lane cost = length / (the fleet's nominal speed x speed_factor), plus event time: a door costs
`door_s`; entering a lift costs the lift's `robot_wait_s` (car arrives); a car move between
floors costs `ride_s`; leaving the car costs `door_s`. Closed lanes are removed. All costs are
simulation seconds; dividing by the real-time factor turns them into wall seconds.

This is Auspex's own estimate on Open-RMF's graph with Open-RMF's closures applied; after an
action, Open-RMF's own estimate is read back and the error logged (M7).
"""

from __future__ import annotations

import heapq
import math
from datetime import datetime, timedelta

from auspex.core.models import StrictModel
from auspex.site.model import Site
from auspex.site.navgraph import NavGraph, NavLane

LIFT_ENTER = "entry_lift_end_lift_name"  # session begin (sic, Open-RMF's naming)
LIFT_EXIT = "entry_lift_door_open_lift_name"
LIFT_MOVE = "entry_lift_move_lift_name"
DOOR = "entry_door_open_name"


class PathLeg(StrictModel):
    lane: int
    segments: list[str]
    enter: datetime
    exit: datetime


class RobotPath(StrictModel):
    fleet: str
    start_vertex: int
    goal_vertex: int
    legs: list[PathLeg]
    eta: datetime
    total_s: float  # wall seconds

    def lifts(self) -> list[str]:
        seen: list[str] = []
        for leg in self.legs:
            for s in leg.segments:
                if s.startswith("segment/lift_") and "-lobby-" not in s and s not in seen:
                    seen.append(s)
        return seen


class EtaModel:
    def __init__(self, site: Site, graphs: dict[str, NavGraph]) -> None:
        self.site = site
        self.graphs = graphs
        self._out: dict[str, dict[int, list[NavLane]]] = {}
        for fleet, g in graphs.items():
            adj: dict[int, list[NavLane]] = {}
            for lane in g.lanes:
                adj.setdefault(lane.v1, []).append(lane)
            self._out[fleet] = adj

    def lane_cost_s(self, fleet: str, lane: NavLane) -> float:
        g = self.graphs[fleet]
        a, b = g.vertex(lane.v1), g.vertex(lane.v2)
        spec = self.site.fleets[fleet]
        speed = spec.nominal_speed_mps * spec.speed_factor
        cost = math.hypot(b.x - a.x, b.y - a.y) / speed
        p = lane.params
        if isinstance(p.get(LIFT_ENTER), str):
            cost += self._lift(str(p[LIFT_ENTER]), "robot_wait_s")
        if isinstance(p.get(LIFT_MOVE), str):
            cost += self._lift(str(p[LIFT_MOVE]), "ride_s")
        if isinstance(p.get(LIFT_EXIT), str) or isinstance(p.get(DOOR), str):
            cost += self.site.door_s
        return cost

    def _lift(self, lift: str, attr: str) -> float:
        seg = self.site.segments.get(f"segment/{lift}")
        value = getattr(seg, attr, None) if seg is not None else None
        return float(value) if value is not None else 0.0

    def nearest_vertex(self, fleet: str, level: str, x: float, y: float) -> int | None:
        v = self.graphs[fleet].nearest_vertex(level, x, y)
        return v.index if v is not None else None

    def distance_to(self, fleet: str, vertex: int, level: str, x: float, y: float) -> float:
        v = self.graphs[fleet].vertex(vertex)
        if v.level != level:
            return math.inf
        return math.hypot(v.x - x, v.y - y)

    def waypoint_vertex(self, fleet: str, name: str) -> int | None:
        v = self.graphs[fleet].waypoint(name)
        return v.index if v is not None else None

    def route(
        self,
        fleet: str,
        start: int,
        stops: list[int],
        closed: set[int],
        now: datetime,
        rtf: float = 1.0,
    ) -> RobotPath | None:
        """Fastest path through `stops` in order (e.g. pickup then dropoff); None if any is
        unreachable with the closed lanes removed."""
        if not stops:
            return None
        legs: list[PathLeg] = []
        at, t = start, now
        for stop in stops:
            part = self.path(fleet, at, stop, closed, t, rtf)
            if part is None:
                return None
            legs.extend(part.legs)
            at, t = stop, part.eta
        return RobotPath(
            fleet=fleet,
            start_vertex=start,
            goal_vertex=stops[-1],
            legs=legs,
            eta=t,
            total_s=(t - now).total_seconds(),
        )

    def path(
        self,
        fleet: str,
        start: int,
        goal: int,
        closed: set[int],
        now: datetime,
        rtf: float = 1.0,
    ) -> RobotPath | None:
        """Fastest path start -> goal avoiding closed lanes; None when unreachable."""
        if fleet not in self.graphs:
            return None
        if start == goal:
            return RobotPath(
                fleet=fleet, start_vertex=start, goal_vertex=goal, legs=[], eta=now, total_s=0.0
            )
        rtf = min(max(rtf, 0.05), 2.0)
        dist: dict[int, float] = {start: 0.0}
        prev: dict[int, NavLane] = {}
        heap: list[tuple[float, int]] = [(0.0, start)]
        while heap:
            d, v = heapq.heappop(heap)
            if v == goal:
                break
            if d > dist.get(v, math.inf):
                continue
            for lane in self._out[fleet].get(v, []):
                if lane.index in closed:
                    continue
                nd = d + self.lane_cost_s(fleet, lane)
                if nd < dist.get(lane.v2, math.inf):
                    dist[lane.v2] = nd
                    prev[lane.v2] = lane
                    heapq.heappush(heap, (nd, lane.v2))
        if goal not in dist:
            return None
        lanes: list[NavLane] = []
        v = goal
        while v != start:
            lane = prev[v]
            lanes.append(lane)
            v = lane.v1
        lanes.reverse()
        legs: list[PathLeg] = []
        t = now
        for lane in lanes:
            dt = timedelta(seconds=self.lane_cost_s(fleet, lane) / rtf)
            legs.append(
                PathLeg(
                    lane=lane.index,
                    segments=self.site.segments_for_lane(fleet, lane.index),
                    enter=t,
                    exit=t + dt,
                )
            )
            t += dt
        return RobotPath(
            fleet=fleet,
            start_vertex=start,
            goal_vertex=goal,
            legs=legs,
            eta=t,
            total_s=(t - now).total_seconds(),
        )
