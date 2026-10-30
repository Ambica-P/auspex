"""Scenario controller (spec 8): drives the simulation for S1-S3 and records what happened.

Run with system Python and ROS sourced (scripts/run-scenario). Next to the hospital mock
(which replays the scenario's hospital records into the core on the same clock), it:
- sends each robot task directly to its robot (robot_task_request over ROS) and links it to
  its delivery job in the core (status update with the task id), as a delivery system would;
- makes porters occupy lifts: a lift session ("porter/<job>") requested through the lift
  supervisor like a fleet adapter does, so robots really queue behind it; the porter boards,
  rides, alights and ends the session;
- injects lift faults: a session ("fault/<lift>") that keeps the car from serving anyone, plus
  the fault on the facility feed (the lift monitoring system's status and fault code);
- simulates Rohan's bedside arm (IngestorState: idle, busy while taking a delivery);
- observes: robot arrivals, robot lift waits (request to grant), porter delays, deliveries;
  and from the core, the conflicts and incidents it raised;
- sets the core's configuration (A observe, B deterministic, C with Nemotron), records the
  plans the core made, scores the run against the scenario's ground truth for that
  configuration and writes bench/results/<config>/<scenario>-<time>.json.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from rmf_ingestor_msgs.msg import IngestorState
from rmf_lift_msgs.msg import LiftRequest, LiftState

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "rmf_adapter_ext"))
from check_levers import Ros, TaskApi, Watch, say, verdict, wait_for  # noqa: E402

sys.path.insert(0, str(REPO / "sim"))
from replay_recorder import Recorder, RecorderRefused  # noqa: E402

sys.path.insert(0, str(REPO / "src" / "auspex" / "core"))
from version import code_fingerprint  # noqa: E402  (standard library only)

CORE = os.environ.get("CORE_URL", "http://127.0.0.1:8100").rstrip("/")
TOKEN = os.environ.get("ADMIN_TOKEN", "")
TICK_S = 0.5
ARRIVE_M = 1.0
LIFT_REPUBLISH_S = 1.0
START_WAIT_S = 300.0  # robots may still be parking after an earlier run
REQUEST_HUMAN, REQUEST_AGV, REQUEST_END = 2, 1, 0
DOOR_OPEN, DOOR_CLOSED = 2, 0
INGESTOR_IDLE, INGESTOR_BUSY = 0, 1


def now() -> datetime:
    return datetime.now(UTC)


def clock_s(value: str) -> int:
    parts = [int(p) for p in str(value).split(":")]
    if len(parts) == 2:
        parts.append(0)
    if len(parts) != 3:
        raise ValueError(f"scenario time must be HH:MM or HH:MM:SS: {value!r}")
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


# --------------------------------------------------------------------------- core HTTP


def core(method: str, path: str, body: dict[str, Any] | None = None, ok404: bool = False) -> Any:
    req = urllib.request.Request(  # noqa: S310
        f"{CORE}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                return json.loads(resp.read().decode() or "null")
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and ok404:
                return None
            detail = exc.read()[:300]
            raise RuntimeError(f"core {method} {path} -> HTTP {exc.code}: {detail!r}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == 2:
                raise RuntimeError(f"core unreachable at {CORE}: {exc}") from None
            time.sleep(1.0)
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------- ROS node


class ControllerNode(Watch):
    def __init__(self) -> None:
        super().__init__()
        self.lift_states: dict[str, Any] = {}
        reliable_latched = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=100,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(
            LiftState,
            "/lift_states",
            self._on_lift_state,
            QoSProfile(depth=100, reliability=ReliabilityPolicy.RELIABLE),
        )
        # The lift supervisor listens here (transient local, reliable), like fleet adapters.
        self.lift_pub = self.create_publisher(
            LiftRequest, "/adapter_lift_requests", reliable_latched
        )
        self.ingestor_pub = self.create_publisher(
            IngestorState, "/ingestor_states", QoSProfile(depth=10)
        )
        self.robot_requests: dict[str, dict[str, Any]] = {}  # session -> first request info
        self.create_subscription(
            LiftRequest,
            "/adapter_lift_requests",
            self._on_adapter_request,
            QoSProfile(
                depth=100,
                reliability=ReliabilityPolicy.BEST_EFFORT,
                durability=DurabilityPolicy.VOLATILE,
            ),
        )

    def _on_lift_state(self, msg: Any) -> None:
        with self.lock:
            self.lift_states[msg.lift_name] = msg

    def _on_adapter_request(self, msg: Any) -> None:
        with self.lock:
            if msg.request_type == REQUEST_END:
                self.robot_requests.pop(msg.session_id, None)
            elif msg.session_id not in self.robot_requests:
                self.robot_requests[msg.session_id] = {"lift": msg.lift_name, "at": now()}

    def lift_request(self, lift: str, session: str, floor: str, kind: int, door: int) -> None:
        msg = LiftRequest()
        msg.lift_name = lift
        msg.session_id = session
        msg.request_type = kind
        msg.destination_floor = floor
        msg.door_state = door
        msg.request_time = self.get_clock().now().to_msg()
        self.lift_pub.publish(msg)

    def lift(self, name: str) -> Any:
        with self.lock:
            return self.lift_states.get(name)

    def ingestor(self, guid: str, mode: int) -> None:
        msg = IngestorState()
        msg.time = self.get_clock().now().to_msg()
        msg.guid = guid
        msg.mode = mode
        msg.seconds_remaining = 0.0
        self.ingestor_pub.publish(msg)


class ControllerRos(Ros):
    def __init__(self) -> None:  # same lifecycle as Ros, with the controller's node
        import rclpy  # noqa: PLC0415
        from rclpy.executors import MultiThreadedExecutor  # noqa: PLC0415

        rclpy.init()
        self.node = ControllerNode()
        self.executor = MultiThreadedExecutor()
        self.executor.add_node(self.node)
        self.thread = threading.Thread(target=self.executor.spin, daemon=True)
        self.thread.start()


# --------------------------------------------------------------------------- actors


@dataclass
class RobotTask:
    spec: dict[str, Any]
    at: datetime
    task_id: str = ""
    sent_at: datetime | None = None
    arrivals: dict[str, datetime] = field(default_factory=dict)
    delivered_at: datetime | None = None
    lift_waits: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    robot: str = ""  # fleet/robot doing it now (changes on a handover)
    places: list[str] = field(default_factory=list)  # places still to visit
    handovers: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.robot = f"{self.spec['fleet']}/{self.spec['robot']}"
        self.places = list(self.spec["places"])

    @property
    def job_path(self) -> str:
        return f"{self.spec['system']}-{self.spec['request_id']}"


@dataclass
class Porter:
    spec: dict[str, Any]
    at: datetime
    stage: str = "pending"  # pending, requesting, boarding, riding, alighting, done
    requested_at: datetime | None = None
    granted_at: datetime | None = None
    released_at: datetime | None = None
    stage_at: datetime | None = None
    last_pub: float = 0.0

    @property
    def session(self) -> str:
        return f"porter/{self.spec['job']}"


@dataclass
class Fault:
    spec: dict[str, Any]
    at: datetime
    until: datetime | None
    stage: str = "pending"  # pending, active, cleared
    floor: str = ""
    granted_at: datetime | None = None
    last_pub: float = 0.0

    @property
    def session(self) -> str:
        return f"fault/{self.spec['lift']}"


class Controller:
    def __init__(self, scenario: dict[str, Any], config: str, t0: datetime) -> None:
        self.sc = scenario
        self.config = config
        self.t0 = t0
        self.origin = clock_s(scenario["clock_start"])
        self.ros = ControllerRos()
        self.node: ControllerNode = self.ros.node  # type: ignore[assignment]
        self.api = TaskApi(self.node, "ros")
        self.tasks = [RobotTask(t, self.wall(t["at"])) for t in scenario.get("robot_tasks", [])]
        self.porters = [Porter(p, self.wall(p["request_at"])) for p in scenario.get("porters", [])]
        self.faults = [
            Fault(
                f,
                self.wall(f["at"]),
                self.wall(f["recover_at"]) if f.get("recover_at") else None,
            )
            for f in scenario.get("faults", [])
        ]
        self.end_at = self.wall(scenario["end_at"])
        self.conflicts: dict[str, dict[str, Any]] = {}
        self.incidents: dict[str, dict[str, Any]] = {}
        self.plans: dict[str, dict[str, Any]] = {}
        self.since = now()
        self.approve_after_s: float | None = None
        self.reject = False
        self.approved: set[str] = set()
        self.ingestor_busy_until: datetime | None = None
        self.notes: list[str] = []

    def wall(self, scenario_time: str) -> datetime:
        return self.t0 + timedelta(seconds=clock_s(scenario_time) - self.origin)

    def label(self, t: datetime | None) -> str | None:
        if t is None:
            return None
        s = self.origin + round((t - self.t0).total_seconds())
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"

    # ------------------------------------------------------------------ robot tasks

    def _send_task(self, task: RobotTask) -> None:
        s = task.spec
        payload = {
            "type": "robot_task_request",
            "robot": s["robot"],
            "fleet": s["fleet"],
            "request": {
                "category": "patrol",
                "description": {"places": s["places"], "rounds": 1},
                "unix_millis_earliest_start_time": 0,
                "unix_millis_request_time": int(time.time() * 1000),
                "requester": "auspex_scenario_controller",
            },
        }
        ok, resp = self.api.request(payload, "/tasks/robot_task")
        task.sent_at = now()
        booking = ((resp or {}).get("state") or {}).get("booking", {}) if ok else {}
        task.task_id = str(booking.get("id", ""))
        if not task.task_id:
            task.error = f"robot task refused: {resp}"
            say(f"FAIL {task.error}")
            return
        say(f"robot task {task.task_id} -> {s['fleet']}/{s['robot']} places {s['places']}")
        for _ in range(10):  # the record may still be in the core's queue
            done = core(
                "POST",
                f"/api/v1/jobs/{task.job_path}/status",
                {"status": "Assigned", "status_at": now().isoformat(), "rmf_task_id": task.task_id},
                ok404=True,
            )
            if done is not None:
                return
            time.sleep(0.5)
        task.error = f"job {task.job_path} not found in the core"
        say(f"FAIL {task.error}")

    def _observe_task(self, task: RobotTask) -> None:
        if not task.task_id or task.delivered_at is not None:
            return
        key = task.robot
        with self.node.lock:
            pos = self.node.seen.positions.get(key)
            graph = self.node.seen.graphs.get(key.split("/")[0])
            request = self.node.robot_requests.get(key)
        if pos is None or graph is None:
            return
        for place in task.places:
            if place in task.arrivals:
                continue
            v = next((v for v in graph.vertices if v.name == place), None)
            if v is None:
                continue
            level = next((str(p.value_string) for p in v.params if p.name == "map_name"), "")
            if pos[0] == level and math.hypot(pos[1] - v.x, pos[2] - v.y) <= ARRIVE_M:
                task.arrivals[place] = now()
                say(f"{key} reached {place}")
                if place == task.places[-1]:
                    task.delivered_at = now()
                    self._delivered(task)
            break  # places are visited in order
        if request is not None:
            lift_name = request["lift"]
            open_wait = next(
                (w for w in task.lift_waits if w["lift"] == lift_name and w["granted"] is None),
                None,
            )
            if open_wait is None and not any(
                w["requested"] == request["at"] for w in task.lift_waits
            ):
                task.lift_waits.append(
                    {"lift": lift_name, "requested": request["at"], "granted": None}
                )
            state = self.node.lift(lift_name)
            for w in task.lift_waits:
                if w["granted"] is None and state is not None and state.session_id == key:
                    w["granted"] = now()
                    wait = (w["granted"] - w["requested"]).total_seconds()
                    say(f"{key} got {lift_name} after waiting {wait:.0f} s")

    def _delivered(self, task: RobotTask) -> None:
        s = task.spec
        if s.get("receiver"):
            self.ingestor_busy_until = now() + timedelta(seconds=15)
        core(
            "POST",
            f"/api/v1/jobs/{task.job_path}/status",
            {"status": "Delivered", "status_at": now().isoformat()},
            ok404=True,
        )

    # ------------------------------------------------------------------ porters

    def _porter(self, p: Porter) -> None:
        s = p.spec
        t = time.monotonic()
        state = self.node.lift(s["lift"])
        holds = state is not None and state.session_id == p.session
        if p.stage == "pending" and now() >= p.at:
            p.stage, p.requested_at = "requesting", now()
            say(f"porter {s['job']} requests {s['lift']} at {s['from_floor']}")
        if p.stage == "requesting":
            if t - p.last_pub >= LIFT_REPUBLISH_S:
                self.node.lift_request(
                    s["lift"], p.session, s["from_floor"], REQUEST_HUMAN, DOOR_OPEN
                )
                p.last_pub = t
            if holds and state.current_floor == s["from_floor"] and state.door_state == DOOR_OPEN:
                p.stage, p.granted_at, p.stage_at = "boarding", now(), now()
                delay = (p.granted_at - p.requested_at).total_seconds() if p.requested_at else 0
                say(f"porter {s['job']} has {s['lift']} (waited {delay:.0f} s); boarding")
        elif p.stage == "boarding":
            if (now() - p.stage_at).total_seconds() >= s.get("board_s", 60):  # type: ignore[operator]
                p.stage, p.stage_at = "riding", now()
                p.last_pub = 0.0  # send the car to the destination floor at once
        elif p.stage == "riding":
            if t - p.last_pub >= LIFT_REPUBLISH_S:
                self.node.lift_request(
                    s["lift"], p.session, s["to_floor"], REQUEST_HUMAN, DOOR_OPEN
                )
                p.last_pub = t
            if (
                state is not None
                and state.current_floor == s["to_floor"]
                and state.door_state == DOOR_OPEN
            ):
                p.stage, p.stage_at = "alighting", now()
        elif p.stage == "alighting":
            if (now() - p.stage_at).total_seconds() >= s.get("alight_s", 60):  # type: ignore[operator]
                self._end_session(s["lift"], p.session, s["to_floor"])
                p.stage, p.released_at = "done", now()
                say(f"porter {s['job']} released {s['lift']}")
                core(
                    "POST",
                    f"/api/v1/jobs/{s['job']}/status",
                    {"status": "Completed", "status_at": now().isoformat()},
                    ok404=True,
                )

    def _end_session(self, lift: str, session: str, floor: str) -> None:
        for _ in range(3):
            self.node.lift_request(lift, session, floor, REQUEST_END, DOOR_CLOSED)
            time.sleep(0.1)

    # ------------------------------------------------------------------ faults

    def _fault(self, f: Fault) -> None:
        s = f.spec
        t = time.monotonic()
        state = self.node.lift(s["lift"])
        if f.stage == "pending" and now() >= f.at:
            f.stage = "active"
            f.floor = state.current_floor if state is not None else s.get("floor", "L1")
            core(
                "POST",
                f"/api/v1/facility/lifts/{s['lift']}",
                {
                    "mode": s.get("mode", "OFFLINE"),
                    "fault_code": s["code"],
                    "status_at": now().isoformat(),
                },
            )
            say(f"fault {s['lift']} {s.get('mode', 'OFFLINE')} {s['code']} reported")
        if f.stage == "active":
            if t - f.last_pub >= LIFT_REPUBLISH_S:
                self.node.lift_request(s["lift"], f.session, f.floor, REQUEST_AGV, DOOR_CLOSED)
                f.last_pub = t
            if f.granted_at is None and state is not None and state.session_id == f.session:
                f.granted_at = now()
                say(f"fault now holds {s['lift']} (car out of service)")
            if f.until is not None and now() >= f.until:
                self._clear_fault(f)

    def _clear_fault(self, f: Fault) -> None:
        if f.stage != "active":
            return
        self._end_session(f.spec["lift"], f.session, f.floor)
        core(
            "POST",
            f"/api/v1/facility/lifts/{f.spec['lift']}",
            {"mode": "AGV", "fault_code": None, "status_at": now().isoformat()},
        )
        f.stage = "cleared"
        say(f"fault {f.spec['lift']} {f.spec['code']} cleared")

    # ------------------------------------------------------------------ core findings

    def _poll_core(self) -> None:
        for c in core("GET", "/api/v1/conflicts"):
            if c["conflict_id"] not in self.conflicts:
                self.conflicts[c["conflict_id"]] = {**c, "seen_at": now().isoformat()}
                say(f"core: conflict {c['kind']} on {c['segment_urn']} {c['job_urns']}")
        for i in core("GET", "/api/v1/incidents")["open"]:
            if i["incident_id"] not in self.incidents:
                self.incidents[i["incident_id"]] = {**i, "seen_at": now().isoformat()}
                say(f"core: incident {i['trigger']} {i['robot_urn']} context {i.get('context')}")
        for t in self.tasks:
            self._follow_handover(t)
        self._approve_waiting()
        for p in core("GET", "/api/v1/plans?limit=30"):
            if p["plan_id"] in self.plans or datetime.fromisoformat(p["created_at"]) < self.since:
                continue
            detail = core("GET", f"/api/v1/plans/{p['plan_id']}")
            self.plans[p["plan_id"]] = detail
            d = detail.get("decision") or {}
            actions = [a["action"] for a in detail["plan"]["actions"]]
            say(
                f"core: plan {p['plan_id']} ({p['producer']}) {d.get('verdict')} "
                f"{d.get('rule_ids')} {actions} -> {detail['status']}"
            )

    def _follow_handover(self, task: RobotTask) -> None:
        """A plan may hand the job to another robot: follow the job's current task."""
        if not task.task_id or task.delivered_at is not None:
            return
        job = core("GET", f"/api/v1/jobs/{task.job_path}", ok404=True)
        current = ((job or {}).get("job") or {}).get("rmf_task_id")
        if not current or current == task.task_id:
            return
        with self.node.lock:
            carrier = next((k for k, v in self.node.seen.robot_task.items() if v == current), None)
        if carrier is None:
            return  # assigned but not started yet; look again next poll
        say(f"handover: {task.job_path} now task {current} on {carrier}")
        task.handovers.append(
            {"at": self.label(now()), "from": task.robot, "to": carrier, "task": current}
        )
        task.task_id, task.robot = current, carrier
        task.places = [task.spec["places"][-1]]  # the new robot collects elsewhere, same drop
        task.arrivals = {}

    def _approve_waiting(self) -> None:
        """The person on duty (simulated): approves (or, for the replay's reject branch,
        rejects) plans waiting for a person after a delay."""
        if self.approve_after_s is None:
            return
        verb = "reject" if self.reject else "approve"
        for p in core("GET", "/api/v1/plans?limit=30"):
            if p["status"] != "awaiting_approval" or p["plan_id"] in self.approved:
                continue
            if datetime.fromisoformat(p["created_at"]) < self.since:
                continue
            age = (now() - datetime.fromisoformat(p["created_at"])).total_seconds()
            if age < self.approve_after_s:
                continue
            self.approved.add(p["plan_id"])
            body: dict[str, Any] = {"approver": "charge-nurse-simulated-by-run-scenario"}
            if self.reject:
                body["reason"] = "rejected by the simulated person (replay reject branch)"
            try:
                core("POST", f"/api/v1/plans/{p['plan_id']}/{verb}", body)
                say(
                    f"person {'rejected' if self.reject else 'approved'} plan {p['plan_id']}"
                    f" {p['decision'].get('rule_ids')}"
                )
            except RuntimeError as exc:
                say(f"{verb} of {p['plan_id']} failed: {exc}")

    # ------------------------------------------------------------------ run

    def done(self) -> bool:
        return (
            all(t.delivered_at is not None or t.error for t in self.tasks)
            and all(p.stage == "done" for p in self.porters)
            and all(f.until is None or f.stage == "cleared" for f in self.faults)
        )

    def run(self) -> None:
        last_poll = 0.0
        last_ingestor = 0.0
        while now() < self.end_at and not self.done():
            for task in self.tasks:
                if task.sent_at is None and now() >= task.at:
                    self._send_task(task)
                self._observe_task(task)
            for p in self.porters:
                self._porter(p)
            for f in self.faults:
                self._fault(f)
            if time.monotonic() - last_ingestor >= 1.0:
                busy = self.ingestor_busy_until is not None and now() < self.ingestor_busy_until
                self.node.ingestor("rohan-arm-1", INGESTOR_BUSY if busy else INGESTOR_IDLE)
                last_ingestor = time.monotonic()
            if time.monotonic() - last_poll >= 2.0:
                try:
                    self._poll_core()
                except RuntimeError as exc:
                    say(f"core poll failed: {exc}")
                last_poll = time.monotonic()
            time.sleep(TICK_S)
        self._poll_core()

    def cleanup(self) -> None:
        for p in self.porters:
            if p.stage not in ("pending", "done"):
                self._end_session(p.spec["lift"], p.session, p.spec.get("to_floor", "L1"))
        for f in self.faults:
            self._clear_fault(f)
        for task in self.tasks:
            if task.task_id and task.delivered_at is None:
                self.api.request(
                    {"type": "cancel_task_request", "task_id": task.task_id}, "/tasks/cancel_task"
                )
                core(
                    "POST",
                    f"/api/v1/jobs/{task.job_path}/status",
                    {"status": "Cancelled", "status_at": now().isoformat()},
                    ok404=True,
                )
        self.ros.close()

    # ------------------------------------------------------------------ results

    def observations(self) -> dict[str, Any]:
        def due(task: RobotTask) -> datetime | None:
            return self.wall(task.spec["due"]) if task.spec.get("due") else None

        return {
            "robot_tasks": {
                t.spec["request_id"]: {
                    "robot": t.robot,
                    "handovers": t.handovers,
                    "task_id": t.task_id,
                    "error": t.error,
                    "sent": self.label(t.sent_at),
                    "arrivals": {k: self.label(v) for k, v in t.arrivals.items()},
                    "delivered": self.label(t.delivered_at),
                    "due": t.spec.get("due"),
                    "on_time": (
                        t.delivered_at is not None
                        and due(t) is not None
                        and t.delivered_at <= due(t)  # type: ignore[operator]
                    ),
                    "lift_waits": [
                        {
                            "lift": w["lift"],
                            "requested": self.label(w["requested"]),
                            "granted": self.label(w["granted"]),
                            "wait_s": round(
                                ((w["granted"] or now()) - w["requested"]).total_seconds()
                            ),
                        }
                        for w in t.lift_waits
                    ],
                }
                for t in self.tasks
            },
            "porters": {
                p.spec["job"]: {
                    "lift": p.spec["lift"],
                    "requested": self.label(p.requested_at),
                    "granted": self.label(p.granted_at),
                    "released": self.label(p.released_at),
                    "delay_s": round(((p.granted_at or now()) - p.requested_at).total_seconds())
                    if p.requested_at
                    else None,
                }
                for p in self.porters
            },
            "faults": {
                f.spec["lift"]: {
                    "code": f.spec["code"],
                    "reported": self.label(f.at) if f.stage != "pending" else None,
                    "held_from": self.label(f.granted_at),
                    "cleared": f.stage == "cleared",
                }
                for f in self.faults
            },
            "core_conflicts": list(self.conflicts.values()),
            "core_incidents": list(self.incidents.values()),
            "core_plans": [self._plan_summary(pid) for pid in self.plans],
            "core_reports": self._reports(),
            "core_providers": self._providers(),
            "core_jobs": self._jobs(),
            "core_interpretations": self._interpretations(),
        }

    def _interpretations(self) -> list[dict[str, Any]]:
        """Nemotron's readings of jobs during this run (model calls outside any plan)."""
        try:
            rows = core("GET", "/api/v1/ledger?kind=interpretation&limit=50")
        except RuntimeError:
            return []
        out = []
        for r in rows:
            if datetime.fromisoformat(r["recorded_at"]) < self.since:
                continue
            steps = [st for st in r["payload"].get("trace") or [] if st.get("kind") == "llm"]
            out.append(
                {
                    "urn": r["payload"].get("urn"),
                    "destination": (r["payload"].get("inferred") or {}).get("destination"),
                    "models": sorted({st["model_id"] for st in steps}),
                    "calls": len(steps),
                    "tokens": sum(
                        int(st["prompt_tokens"]) + int(st["completion_tokens"]) for st in steps
                    ),
                }
            )
        return out

    def _providers(self) -> dict[str, Any]:
        try:
            hlt = core("GET", "/api/v1/health")
        except RuntimeError:
            return {}
        return {k: hlt.get(k) for k in ("config", "llm_provider", "search_provider")}

    def _reports(self) -> list[dict[str, Any]]:
        try:
            recent = core("GET", "/api/v1/incidents?limit=50")["recent"]
        except RuntimeError:
            return []
        return [
            {"incident_id": i["incident_id"], "trigger": i["trigger"], "report": i["report"]}
            for i in recent
            if i.get("report") and datetime.fromisoformat(i["detected_at"]) >= self.since
        ]

    def _jobs(self) -> dict[str, Any]:
        try:
            rows = core("GET", "/api/v1/jobs?limit=200")
        except RuntimeError:
            return {}
        return {
            r["job"]["urn"]: {
                "destination": r["job"]["destination"],
                "inferred_fields": r["job"]["inferred_fields"],
                "inference_confidence": r["job"].get("inference_confidence"),
            }
            for r in rows
            if datetime.fromisoformat(r["job"]["status_at"]) >= self.since - timedelta(hours=1)
        }

    def _plan_summary(self, plan_id: str) -> dict[str, Any]:
        try:
            p = core("GET", f"/api/v1/plans/{plan_id}") or self.plans[plan_id]
        except RuntimeError:
            p = self.plans[plan_id]
        llm = {"models": [], "calls": 0, "tokens": 0, "llm_ms": 0, "repaired": 0, "invalid": 0}
        latency = None
        try:
            ctx = core("GET", f"/api/v1/plans/{plan_id}/context")
            latency = ctx.get("decision_latency_ms")
            for d in ctx.get("decisions", []):
                for st in d.get("trace") or []:
                    if st.get("kind") != "llm":
                        continue
                    llm["calls"] += 1
                    llm["tokens"] += int(st["prompt_tokens"]) + int(st["completion_tokens"])
                    llm["llm_ms"] += int(st["latency_ms"])
                    llm["repaired"] += st.get("outcome") == "repaired"
                    llm["invalid"] += st.get("outcome") == "invalid"
                    if st["model_id"] not in llm["models"]:
                        llm["models"].append(st["model_id"])
        except RuntimeError:
            pass
        return {
            "plan_id": plan_id,
            "producer": p["producer"],
            "decision_latency_ms": latency,
            "llm": llm,
            "status": p["status"],
            "verdict": (p.get("decision") or {}).get("verdict"),
            "rule_ids": (p.get("decision") or {}).get("rule_ids"),
            "actions": p["plan"]["actions"],
            "rationale": p["plan"].get("rationale"),
            "created_at": p.get("created_at"),
        }


# --------------------------------------------------------------------------- scoring


def score(checks: list[dict[str, Any]], obs: dict[str, Any]) -> list[tuple[bool, str]]:
    out: list[tuple[bool, str]] = []
    for c in checks:
        kind = c["kind"]
        if kind == "robot_waits_or_porter_delayed":
            task = obs["robot_tasks"].get(c["robot_job"], {})
            waits = [w["wait_s"] for w in task.get("lift_waits", []) if w["lift"] == c["lift"]]
            porter = obs["porters"].get(c["porter_job"], {})
            robot_wait = max(waits, default=0)
            porter_delay = porter.get("delay_s") or 0
            ok = robot_wait >= c["min_robot_wait_s"] or porter_delay >= c["min_porter_delay_s"]
            out.append(
                (
                    ok,
                    f"{c['lift']} contested: robot waited {robot_wait} s, porter "
                    f"delayed {porter_delay} s ({c.get('why', '')})",
                )
            )
        elif kind == "porter_delayed":
            delay = obs["porters"].get(c["job"], {}).get("delay_s") or 0
            out.append(
                (
                    delay >= c["min_s"] if c.get("min_s") is not None else True,
                    f"porter {c['job']} delayed {delay} s (>= {c['min_s']}; {c.get('why', '')})",
                )
            )
        elif kind == "delivered_on_time":
            task = obs["robot_tasks"].get(c["job"], {})
            on_time = bool(task.get("on_time"))
            out.append(
                (
                    on_time == c["expect"] if c["expect"] is not None else True,
                    f"{c['job']} delivered {task.get('delivered')} "
                    f"(due {task.get('due')}); on time = {on_time}, "
                    f"expected {c['expect']}",
                )
            )
        elif kind == "incident_opened":
            found = [
                i
                for i in obs["core_incidents"]
                if i["trigger"] == c["trigger"]
                and (not c.get("job") or (i.get("job_urn") or "").endswith(c["job"]))
            ]
            detail = found[0].get("context") if found else None
            out.append((bool(found), f"core opened a {c['trigger']} incident ({detail})"))
        elif kind == "conflict_detected":
            found = [
                x
                for x in obs["core_conflicts"]
                if x["segment_urn"] == c["segment"]
                and all(any(u.endswith(j) for u in x["job_urns"]) for j in c["jobs"])
            ]
            out.append(
                (
                    bool(found) == c["expect"],
                    f"conflict on {c['segment']} between "
                    f"{c['jobs']}: detected = {bool(found)}, "
                    f"expected {c['expect']}",
                )
            )
        elif kind == "plan_applied":
            wanted = set(c.get("actions_any") or [c["action"]])
            none_of = set(c.get("none_of") or [])
            found = [
                p
                for p in obs.get("core_plans", [])
                if p["status"] == "completed"
                and (not c.get("producer") or p["producer"] == c["producer"])
                and (not c.get("rule") or c["rule"] in (p.get("rule_ids") or []))
                and any(
                    a["action"] in wanted
                    and (not c.get("segment") or a.get("segment_urn") == c["segment"])
                    for a in p["actions"]
                )
                and not any(a["action"] in none_of for a in p["actions"])
            ]
            out.append(
                (
                    bool(found) == c["expect"],
                    f"plan with {sorted(wanted)} {c.get('segment', '')} "
                    f"{'without ' + str(sorted(none_of)) + ' ' if none_of else ''}"
                    f"{'rule ' + c['rule'] + ' ' if c.get('rule') else ''}"
                    f"({c.get('producer') or 'any producer'}) applied and verified: "
                    f"{[p['plan_id'] for p in found] or 'none'}, expected {c['expect']}"
                    f" ({c.get('why', '')})",
                )
            )
        elif kind == "interpreted":
            job = next(
                (v for k, v in obs.get("core_jobs", {}).items() if k.endswith(c["job"])), None
            )
            ok = (
                bool(job)
                and job["destination"] == c["destination"]
                and "destination" in (job["inferred_fields"] or [])
            )
            out.append(
                (
                    ok == c["expect"],
                    f"{c['job']} destination inferred as {job and job['destination']} "
                    f"(confidence {job and job['inference_confidence']}), expected "
                    f"{c['destination']}: {ok}",
                )
            )
        elif kind == "incident_report":
            reports = obs.get("core_reports", [])
            causes = sum(len(r["report"].get("causes", [])) for r in reports)
            out.append(
                (
                    bool(reports) == c["expect"] if c["expect"] is not None else True,
                    f"incident reports written: {len(reports)} ({causes} possible causes, "
                    f"none confirmed), expected {c['expect']}",
                )
            )
        elif kind == "porter_not_delayed":
            delay = obs["porters"].get(c["job"], {}).get("delay_s") or 0
            out.append(
                (
                    delay <= c["max_s"],
                    f"porter {c['job']} delayed {delay} s (<= {c['max_s']}; {c.get('why', '')})",
                )
            )
        else:
            out.append((False, f"unknown check kind {kind}"))
    return out


# --------------------------------------------------------------------------- main


def start_mock(path: Path, t0: datetime) -> subprocess.Popen[bytes]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [  # noqa: S607 - uv from PATH, as every script uses it
            "uv",
            "run",
            "--frozen",
            "python",
            "-m",
            "hospital_mock",
            "replay",
            str(path),
            "--t0",
            str(t0.timestamp()),
        ],
        cwd=REPO,
        env=env,
    )


def robots_ready(node: ControllerNode, starts: dict[str, str]) -> bool:
    with node.lock:
        tasks = dict(node.seen.robot_task)
        positions = dict(node.seen.positions)
        graphs = dict(node.seen.graphs)
    for key, waypoint in starts.items():
        fleet = key.split("/", 1)[0]
        if tasks.get(key) or key not in positions or fleet not in graphs:
            return False
        v = next((v for v in graphs[fleet].vertices if v.name == waypoint), None)
        if v is None:
            return False
        pos = positions[key]
        if math.hypot(pos[1] - v.x, pos[2] - v.y) > 2.0:
            return False
    return True


def main() -> int:  # noqa: PLR0915 (one linear script: set up, run, score, record)
    p = argparse.ArgumentParser()
    p.add_argument("scenario", type=Path)
    p.add_argument("--config", default="A", choices=["A", "B", "C"])
    p.add_argument(
        "--approve-after",
        type=float,
        default=5.0,
        help="seconds before the simulated person approves a plan waiting for a person "
        "(negative: never; approve in the console instead)",
    )
    p.add_argument(
        "--reject",
        action="store_true",
        help="the simulated person rejects instead of approving (replay reject branch)",
    )
    p.add_argument(
        "--record",
        action="store_true",
        help="record a replay of the run for the judge site (web/public/replays/)",
    )
    args = p.parse_args()
    if not TOKEN:
        print("FAIL ADMIN_TOKEN not set (scripts/run-scenario passes it from .env)")
        return 1
    scenario = yaml.safe_load(args.scenario.read_text(encoding="utf-8"))
    running = core("GET", "/api/v1/health").get("code")
    expected = code_fingerprint(REPO / "src" / "auspex")
    if running != expected:
        print(
            f"FAIL the core is running different code ({running}) from this repository "
            f"({expected}): restart scripts/run-core, then run again"
        )
        return 1
    # Start clean: jobs left active by earlier runs would still be forecast and conflict.
    stale = [
        j["job"]
        for j in core("GET", "/api/v1/jobs?limit=1000")
        if j["job"]["status"] not in ("completed", "cancelled")
    ]
    for job in stale:
        core(
            "POST",
            f"/api/v1/jobs/{job['urn'].split('/', 1)[1]}/status",
            {"status": "Cancelled", "status_at": now().isoformat()},
            ok404=True,
        )
    if stale:
        say(f"cancelled {len(stale)} jobs left active by earlier runs")
    core("PUT", "/api/v1/config", {"config": args.config, "by": "run-scenario"})
    say(f"core configuration set to {args.config}")
    mock = None
    t0 = now() + timedelta(seconds=8)
    ctl = Controller(scenario, args.config, t0)
    ctl.approve_after_s = args.approve_after if args.approve_after >= 0 else None
    ctl.reject = args.reject
    recorder: Recorder | None = None
    branch = "reject" if args.reject else ("no-answer" if ctl.approve_after_s is None else "")
    replay_name = "-".join(x for x in (scenario["id"], args.config, branch) if x)
    if args.record:
        try:
            recorder = Recorder(
                core,
                REPO / "web" / "public" / "replays" / f"{replay_name}.json",
                {
                    "scenario": scenario["id"],
                    "description": scenario.get("description", ""),
                    "config": args.config,
                    "branch": branch or "approve",
                    "clock_start": scenario["clock_start"],
                    "end_at": scenario["end_at"],
                },
            )
        except RecorderRefused as exc:
            print(f"FAIL {exc}")
            return 1
    try:
        starts = scenario.get("robot_starts", {})
        say(
            f"scenario {scenario['id']} (config {args.config}): waiting for robots idle at "
            f"their start positions {starts}"
        )
        if not wait_for(lambda: robots_ready(ctl.node, starts), START_WAIT_S, 2.0):
            print(
                "FAIL robots not idle at their start positions; are they still parking? "
                "Restart the sim if a robot is stuck (see the M5 notes)."
            )
            return 1
        ctl.t0 = now() + timedelta(seconds=5)
        ctl.tasks = [RobotTask(t, ctl.wall(t["at"])) for t in scenario.get("robot_tasks", [])]
        ctl.porters = [Porter(x, ctl.wall(x["request_at"])) for x in scenario.get("porters", [])]
        ctl.faults = [
            Fault(f, ctl.wall(f["at"]), ctl.wall(f["recover_at"]) if f.get("recover_at") else None)
            for f in scenario.get("faults", [])
        ]
        ctl.end_at = ctl.wall(scenario["end_at"])
        say(
            f"scenario clock {scenario['clock_start']} = {ctl.t0:%H:%M:%S} UTC; "
            f"ends by {scenario['end_at']}"
        )
        if recorder is not None:
            recorder.start(ctl.t0)
            say(f"recording the replay {replay_name}")
        mock = start_mock(args.scenario, ctl.t0)
        ctl.run()
    finally:
        ctl.cleanup()
        if mock is not None:
            mock.wait(timeout=60)
    obs = ctl.observations()
    checks = scenario.get("ground_truth", {}).get(f"baseline_{args.config.lower()}", [])
    results = score(checks, obs)
    for ok, msg in results:
        verdict("PASS" if ok else "FAIL", msg)
    # Approval-branch runs (reject, no answer) are replay material, not benchmark runs.
    out_dir = REPO / "bench" / "results" / (f"raw/{branch}" if branch else args.config)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{scenario['id']}-{ctl.t0:%Y%m%dT%H%M%SZ}.json"
    out.write_text(
        json.dumps(
            {
                "scenario": scenario["id"],
                "config": args.config,
                "started": ctl.t0.isoformat(),
                "clock_start": scenario["clock_start"],
                "observations": obs,
                "checks": [{"ok": ok, "check": msg} for ok, msg in results],
            },
            indent=1,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    ok = all(r[0] for r in results)
    if recorder is not None:
        say("finishing the replay (verification and incident report)")
        replay = recorder.finish([{"ok": ok_, "check": msg} for ok_, msg in results], now())
        say(f"replay written: {replay.relative_to(REPO)} ({replay.stat().st_size // 1024} KB)")
    print(f"results: {out}")
    print(f"{'PASS' if ok else 'FAIL'} scenario {scenario['id']} config {args.config}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
