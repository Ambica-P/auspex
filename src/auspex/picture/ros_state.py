"""Latest Open-RMF state, kept in memory from the bridge's events (spec 4).

Keyed per entity, newest wins: fleets (robots with position, task and mode), lane closures
per fleet, lifts, doors, dispatch states, ingestors. Live navigation graphs are compared with
the captures the site model was built from: a mismatch means lane indices may have moved,
so it is logged as an error and reported by /health (rebuild with scripts/capture-navgraphs).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import ValidationError

from auspex.core.logging import get_logger
from auspex.site.navgraph import NavGraph

log = get_logger(__name__)

GraphStatus = Literal["match", "mismatch", "not_seen", "invalid"]
# rmf_lift_msgs/LiftState.current_mode
LIFT_MODE_CODES = {"UNKNOWN": 0, "HUMAN": 1, "AGV": 2, "FIRE": 3, "OFFLINE": 4, "EMERGENCY": 5}
LIFT_UNAVAILABLE = {0, 3, 4, 5}
RTF_MIN_WALL_S = 5.0  # measure the real-time factor over at least this much wall time


@dataclass(frozen=True)
class StateView:
    fleets: dict[str, dict[str, Any]]
    closed: dict[str, set[int]]
    lifts: dict[str, dict[str, Any]]
    doors: dict[str, dict[str, Any]]
    rtf: dict[str, float]  # sim seconds per wall second, per fleet (1.0 until measured)
    lift_requests: dict[str, dict[str, Any]]  # session "<fleet>/<robot>" -> latest request
    dispatch: dict[str, Any] = field(default_factory=dict)  # latest /dispatch_states


def graph_signature(g: NavGraph) -> tuple[Any, ...]:
    return (
        tuple((v.name, v.level, round(v.x, 3), round(v.y, 3)) for v in g.vertices),
        tuple((lane.v1, lane.v2, lane.crossing()) for lane in g.lanes),
    )


class RosState:
    def __init__(self, site_graphs: dict[str, NavGraph]) -> None:
        self._lock = threading.Lock()
        self.fleets: dict[str, dict[str, Any]] = {}
        self.lanes: dict[str, list[int]] = {}
        self.lifts: dict[str, dict[str, Any]] = {}
        self.doors: dict[str, dict[str, Any]] = {}
        self.ingestors: dict[str, dict[str, Any]] = {}
        self.dispatch: dict[str, Any] = {}
        self.bridge_stats: dict[str, Any] = {}
        self.updated: dict[str, datetime] = {}
        self._site_signatures = {f: graph_signature(g) for f, g in site_graphs.items()}
        self.graph_status: dict[str, GraphStatus] = dict.fromkeys(site_graphs, "not_seen")
        self.rtf: dict[str, float] = {}
        self.lift_requests: dict[str, dict[str, Any]] = {}
        # Lift status from the building's lift monitoring (facility feed): overrides the
        # mode Open-RMF reports and adds the fault code (LiftState has none).
        self.facility_lifts: dict[str, dict[str, Any]] = {}
        self._rtf_anchor: dict[str, tuple[float, float]] = {}  # fleet -> (wall, sim)

    def apply(self, kind: str, payload: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        with self._lock:
            if kind == "fleet_state":
                self.fleets[payload["fleet"]] = payload
                self._measure_rtf(payload)
            elif kind == "lane_states":
                self.lanes[payload["fleet"]] = list(payload["closed_lanes"])
            elif kind == "lift_state":
                self.lifts[payload["lift_name"]] = payload
            elif kind == "lift_request":
                session = str(payload["session_id"])
                if int(payload["request_type"]) == 0:  # end of session
                    self.lift_requests.pop(session, None)
                else:
                    self.lift_requests[session] = {**payload, "received_at": now}
            elif kind == "facility_lift":
                if payload["mode"] == "AGV":
                    self.facility_lifts.pop(payload["lift_name"], None)
                else:
                    self.facility_lifts[payload["lift_name"]] = payload
            elif kind == "door_state":
                self.doors[payload["door_name"]] = payload
            elif kind == "ingestor_state":
                self.ingestors[payload["guid"]] = payload
            elif kind == "dispatch_states":
                self.dispatch = payload
            elif kind == "bridge_stats":
                self.bridge_stats = payload
            elif kind == "nav_graph":
                self._check_graph(payload)
            else:
                log.warning("ros_state.unknown_kind", kind=kind)
                return
            self.updated[kind] = now

    def _measure_rtf(self, payload: dict[str, Any]) -> None:
        sims = [float(r.get("t", 0.0)) for r in payload.get("robots", []) if r.get("t")]
        if not sims:
            return
        fleet, wall, sim = payload["fleet"], time.monotonic(), max(sims)
        anchor = self._rtf_anchor.get(fleet)
        if anchor is None or sim < anchor[1]:
            self._rtf_anchor[fleet] = (wall, sim)
            return
        dw = wall - anchor[0]
        if dw >= RTF_MIN_WALL_S:
            rtf = (sim - anchor[1]) / dw
            if 0.01 < rtf < 10.0:
                prev = self.rtf.get(fleet)
                self.rtf[fleet] = rtf if prev is None else 0.7 * prev + 0.3 * rtf
            self._rtf_anchor[fleet] = (wall, sim)

    def state(self) -> StateView:
        with self._lock:
            return StateView(
                fleets={f: dict(s) for f, s in self.fleets.items()},
                closed={f: set(c) for f, c in self.lanes.items()},
                lifts=self._effective_lifts(),
                doors={n: dict(s) for n, s in self.doors.items()},
                rtf={f: self.rtf.get(f, 1.0) for f in self.fleets},
                lift_requests={k: dict(v) for k, v in self.lift_requests.items()},
                dispatch=dict(self.dispatch),
            )

    def _effective_lifts(self) -> dict[str, dict[str, Any]]:
        lifts = {n: dict(s) for n, s in self.lifts.items()}
        for name, f in self.facility_lifts.items():
            lift = lifts.setdefault(name, {"lift_name": name, "session_id": ""})
            lift["current_mode"] = LIFT_MODE_CODES[f["mode"]]
            lift["fault_code"] = f.get("fault_code")
            lift["status_source"] = "facility"
        return lifts

    def _check_graph(self, payload: dict[str, Any]) -> None:
        fleet = str(payload.get("fleet", ""))
        try:
            live = NavGraph.model_validate(payload)
        except ValidationError as exc:
            self.graph_status[fleet] = "invalid"
            log.error("site.graph_invalid", fleet=fleet, error=str(exc.errors()[0]["msg"]))
            return
        expected = self._site_signatures.get(fleet)
        status: GraphStatus = (
            "match" if expected is not None and graph_signature(live) == expected else "mismatch"
        )
        if self.graph_status.get(fleet) != status:
            log_fn = log.info if status == "match" else log.error
            log_fn(
                "site.graph_checked",
                fleet=fleet,
                status=status,
                lanes=len(live.lanes),
                hint=None if status == "match" else "run scripts/capture-navgraphs, then restart",
            )
        self.graph_status[fleet] = status

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "fleets": {
                    f: sorted(r["name"] for r in s.get("robots", []))
                    for f, s in self.fleets.items()
                },
                "robot_tasks": {
                    f"{f}/{r['name']}": r.get("task_id") or None
                    for f, s in self.fleets.items()
                    for r in s.get("robots", [])
                },
                "closed_lanes": dict(self.lanes),
                "lifts": {n: s.get("current_mode") for n, s in self.lifts.items()},
                "lift_faults": {
                    n: f"{f['mode']} {f.get('fault_code') or ''}".strip()
                    for n, f in self.facility_lifts.items()
                },
                "doors": {n: s.get("mode") for n, s in self.doors.items()},
                "ingestors": sorted(self.ingestors),
                "graphs": dict(self.graph_status),
                "real_time_factor": {f: round(v, 3) for f, v in self.rtf.items()},
                "lift_requests": {
                    k: f"{v['lift_name']} -> {v['destination_floor']}"
                    for k, v in self.lift_requests.items()
                },
                "bridge_topics": self.bridge_stats.get("counts", {}),
                "updated": {k: v.isoformat() for k, v in self.updated.items()},
            }
