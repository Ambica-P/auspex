"""A fleet adapter's navigation graph, as published on /nav_graphs and captured to JSON.

Lane index = position in the graph's edge list (every edge is one-directional; a graph with a
bidirectional edge is refused rather than guessed). Indices are per fleet. Crossings come
from the event parameters Open-RMF puts on each edge (rmf_traffic_ros2 convert_Graph):

- doors:            entry_door_open_name, exit_door_close_name
- lift entry/exit:  entry_lift_end_lift_name (session begin), entry_lift_door_open_lift_name,
                    exit_lift_end_lift_name (session end)
- lift car moves:   entry_lift_move_lift_name (between floors, inside the car)

Closing a lift closes its entry/exit lanes only: closing a car-move lane would strand a robot
already inside the car.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from auspex.core.errors import SiteError
from auspex.core.models import StrictModel

ParamValue = str | int | float | bool

DOOR_PARAMS = ("entry_door_open_name", "exit_door_close_name")
LIFT_PARAMS = (
    "entry_lift_end_lift_name",
    "entry_lift_door_open_lift_name",
    "exit_lift_end_lift_name",
)
LIFT_MOVE_PARAMS = ("entry_lift_move_lift_name",)
UNIDIRECTIONAL = 1


class NavVertex(StrictModel):
    index: int = Field(ge=0)
    name: str = ""
    x: float
    y: float
    params: dict[str, ParamValue] = Field(default_factory=dict)

    @property
    def level(self) -> str:
        return str(self.params.get("map_name", ""))

    @property
    def is_holding_point(self) -> bool:
        return bool(self.params.get("is_holding_point", False))


class NavLane(StrictModel):
    index: int = Field(ge=0)
    v1: int = Field(ge=0)
    v2: int = Field(ge=0)
    edge_type: int = UNIDIRECTIONAL
    params: dict[str, ParamValue] = Field(default_factory=dict)

    def crossing(self) -> tuple[Literal["door", "lift", "lift_move"], str] | None:
        for kind, names in (
            ("door", DOOR_PARAMS),
            ("lift", LIFT_PARAMS),
            ("lift_move", LIFT_MOVE_PARAMS),
        ):
            for name in names:
                value = self.params.get(name)
                if isinstance(value, str) and value:
                    return kind, value  # type: ignore[return-value]
        return None


class NavGraph(StrictModel):
    fleet: str = Field(min_length=1)
    captured_at: str
    source: str
    vertices: list[NavVertex]
    lanes: list[NavLane]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        for i, v in enumerate(self.vertices):
            if v.index != i:
                raise ValueError(f"vertex {i} has index {v.index}")
        n = len(self.vertices)
        for i, lane in enumerate(self.lanes):
            if lane.index != i:
                raise ValueError(f"lane {i} has index {lane.index}")
            if lane.edge_type != UNIDIRECTIONAL:
                raise ValueError(f"lane {i} is not one-directional: lane index != edge index")
            if lane.v1 >= n or lane.v2 >= n:
                raise ValueError(f"lane {i} references a missing vertex")
        return self

    def vertex(self, index: int) -> NavVertex:
        return self.vertices[index]

    def lane_level(self, lane: NavLane) -> str:
        return self.vertices[lane.v1].level

    def waypoint(self, name: str) -> NavVertex | None:
        for v in self.vertices:
            if v.name == name:
                return v
        return None

    def crossing_lanes(self, kind: str, name: str) -> list[int]:
        return [
            lane.index
            for lane in self.lanes
            if (c := lane.crossing()) is not None and c == (kind, name)
        ]

    def crossings(self) -> dict[tuple[str, str], list[int]]:
        out: dict[tuple[str, str], list[int]] = {}
        for lane in self.lanes:
            c = lane.crossing()
            if c is not None:
                out.setdefault(c, []).append(lane.index)
        return out

    def lanes_in_area(
        self, level: str, xs: tuple[float, float], ys: tuple[float, float]
    ) -> list[int]:
        """Plain lanes (no door or lift event) with both endpoints inside the box on `level`."""

        def inside(v: NavVertex) -> bool:
            return v.level == level and xs[0] <= v.x <= xs[1] and ys[0] <= v.y <= ys[1]

        return [
            lane.index
            for lane in self.lanes
            if lane.crossing() is None
            and inside(self.vertices[lane.v1])
            and inside(self.vertices[lane.v2])
        ]

    def nearest_vertex(self, level: str, x: float, y: float) -> NavVertex | None:
        candidates = [v for v in self.vertices if v.level == level]
        if not candidates:
            return None
        return min(candidates, key=lambda v: math.hypot(v.x - x, v.y - y))


def load_capture(path: Path) -> NavGraph:
    try:
        return NavGraph.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise SiteError("cannot read nav graph capture", path=str(path), detail=str(exc)) from None
    except ValidationError as exc:
        first = exc.errors()[0]
        raise SiteError(
            "invalid nav graph capture",
            path=str(path),
            detail=f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}",
        ) from None


def load_captures(directory: Path) -> dict[str, NavGraph]:
    files = sorted(directory.glob("*.json"))
    if not files:
        raise SiteError(
            "no nav graph captures; run scripts/capture-navgraphs with the sim up",
            path=str(directory),
        )
    graphs: dict[str, NavGraph] = {}
    for f in files:
        g = load_capture(f)
        graphs[g.fleet] = g
    return graphs
