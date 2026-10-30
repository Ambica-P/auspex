"""Milestone 2 lever checks, run against the live clinic simulation (scripts/check-levers).

Each subcommand prints evidence lines and ends with PASS, FAIL or INCONCLUSIVE lines. It uses
only system Python and rclpy (plus the RMF API server for `api`, and with `--via api`);
nothing here is imported by the core.

  api                               auth requirement and task routes of the RMF API server
  places    --fleet F               holding points in F's navigation graph, by level
  lanes     --fleet F --crossing C  close C's lanes for F, read back, reopen, read back
  route     --fleet F --place P [--close C --when before|moving]
                                    dispatch a task restricted to F; optionally close C (and
                                    call the Auspex replan service); report lifts/doors used
  interrupt --fleet F --place P     interrupt a moving task, check the robot holds, resume
  ready     [--fleets A,B]          every expected fleet reports robots, each with its
                                    Auspex replan service (catches a hung or dead adapter)

Task requests (dispatch, interrupt, resume, cancel) go over ROS by default: the same
rmf_api_msgs JSON on /task_api_requests, answered on /task_api_responses (the path
rmf_demos_tasks uses; no token). `--via api` sends them to the RMF API server instead.
Over ROS, a task's progress is read from /dispatch_states (assignment) and the robot's
`task_id` in /fleet_states (running, then gone); completed and failed look the same there.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from rmf_building_map_msgs.msg import Graph
from rmf_door_msgs.msg import DoorRequest
from rmf_fleet_msgs.msg import ClosedLanes, FleetState, LaneRequest, LaneStates
from rmf_lift_msgs.msg import LiftRequest
from rmf_task_msgs.msg import ApiRequest, ApiResponse, DispatchStates
from std_srvs.srv import Trigger

API_BASE = os.environ.get("RMF_API_BASE", "http://127.0.0.1:8000").rstrip("/")
API_TOKEN = os.environ.get("RMF_API_TOKEN", "")
HTTP_TIMEOUT_S = 10.0
ROS_REQUEST_TIMEOUT_S = 10.0
CROSSING_PARAMS = (
    "entry_door_open_name",
    "exit_door_close_name",
    "entry_lift_door_open_lift_name",
    "exit_lift_end_lift_name",
    "entry_lift_end_lift_name",
)
DISPATCH_STATUS = {
    0: "uninitialized",
    1: "queued",
    2: "selected",
    3: "dispatched",
    4: "failed_to_assign",
    5: "canceled_in_flight",
}
API_RESPONDING = 2  # rmf_task_msgs/ApiResponse.TYPE_RESPONDING
TERMINAL = {
    "completed",
    "failed",
    "canceled",
    "killed",
    "error",
    "skipped",
    "failed_to_assign",
    "canceled_in_flight",
    "finished",
}

_t0 = time.monotonic()


def say(msg: str) -> None:
    print(f"[{time.monotonic() - _t0:7.1f}s] {msg}", flush=True)


def verdict(kind: str, msg: str) -> None:
    print(f"{kind} {msg}", flush=True)


# --------------------------------------------------------------------------- HTTP


def http(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API_BASE}{path}", data=data, method=method)  # noqa: S310
    req.add_header("Content-Type", "application/json")
    if API_TOKEN:
        req.add_header("Authorization", f"Bearer {API_TOKEN}")
    attempts = 3 if method == "GET" else 1
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, raw[:500]
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == attempts - 1:
                sys.exit(f"FAIL cannot reach RMF API server at {API_BASE}: {exc}")
            time.sleep(1.0)
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------- ROS


@dataclass
class Seen:
    graphs: dict[str, Any] = field(default_factory=dict)
    lane_states: dict[str, list[int]] = field(default_factory=dict)
    closed_lanes: dict[str, list[int]] = field(default_factory=dict)
    positions: dict[str, tuple[str, float, float, int]] = field(default_factory=dict)
    robot_task: dict[str, str] = field(default_factory=dict)
    dispatch: dict[str, tuple[str, str, str, list[str]]] = field(default_factory=dict)
    api_responses: dict[str, Any] = field(default_factory=dict)
    lift_requests: list[tuple[float, str, str, str]] = field(default_factory=list)
    door_requests: list[tuple[float, str, str, int]] = field(default_factory=list)


def _qos(depth: int, reliable: bool, latched: bool) -> QoSProfile:
    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=depth,
        reliability=ReliabilityPolicy.RELIABLE if reliable else ReliabilityPolicy.BEST_EFFORT,
        durability=DurabilityPolicy.TRANSIENT_LOCAL if latched else DurabilityPolicy.VOLATILE,
    )


class Watch(Node):
    def __init__(self) -> None:
        super().__init__("auspex_check_levers")
        self.seen = Seen()
        self.lock = threading.Lock()
        any_pub = _qos(100, reliable=False, latched=False)  # matches any publisher
        self.create_subscription(Graph, "/nav_graphs", self._graph, _qos(10, True, True))
        self.create_subscription(
            LaneStates, "/lane_states", self._lane_states, _qos(10, True, True)
        )
        self.create_subscription(ClosedLanes, "/closed_lanes", self._closed, _qos(1, True, True))
        self.create_subscription(FleetState, "/fleet_states", self._fleet, any_pub)
        self.create_subscription(LiftRequest, "/lift_requests", self._lift, any_pub)
        self.create_subscription(DoorRequest, "/door_requests", self._door, any_pub)
        self.create_subscription(DispatchStates, "/dispatch_states", self._dispatch, any_pub)
        # Same QoS as rmf_demos_tasks: reliable, transient local.
        self.create_subscription(
            ApiResponse, "/task_api_responses", self._api_response, _qos(10, True, True)
        )
        self.task_pub = self.create_publisher(ApiRequest, "/task_api_requests", _qos(1, True, True))
        self.lane_pub = self.create_publisher(
            LaneRequest, "/lane_closure_requests", _qos(10, True, False)
        )

    def _graph(self, msg: Any) -> None:
        with self.lock:
            self.seen.graphs[msg.name] = msg

    def _lane_states(self, msg: Any) -> None:
        with self.lock:
            self.seen.lane_states[msg.fleet_name] = [int(i) for i in msg.closed_lanes]

    def _closed(self, msg: Any) -> None:
        with self.lock:
            self.seen.closed_lanes[msg.fleet_name] = [int(i) for i in msg.closed_lanes]

    def _fleet(self, msg: Any) -> None:
        with self.lock:
            for r in msg.robots:
                key = f"{msg.name}/{r.name}"
                loc = r.location
                self.seen.positions[key] = (loc.level_name, loc.x, loc.y, int(r.mode.mode))
                self.seen.robot_task[key] = r.task_id

    def _dispatch(self, msg: Any) -> None:
        with self.lock:
            for d in list(msg.active) + list(msg.finished):
                self.seen.dispatch[d.task_id] = (
                    DISPATCH_STATUS.get(int(d.status), str(d.status)),
                    d.assignment.fleet_name,
                    d.assignment.expected_robot_name,
                    list(d.errors),
                )

    def _api_response(self, msg: Any) -> None:
        if msg.type != API_RESPONDING:
            return
        try:
            body = json.loads(msg.json_msg)
        except json.JSONDecodeError:
            body = msg.json_msg
        with self.lock:
            self.seen.api_responses.setdefault(msg.request_id, body)

    def _lift(self, msg: Any) -> None:
        with self.lock:
            self.seen.lift_requests.append(
                (time.monotonic(), msg.lift_name, msg.session_id, msg.destination_floor)
            )

    def _door(self, msg: Any) -> None:
        with self.lock:
            self.seen.door_requests.append(
                (time.monotonic(), msg.door_name, msg.requester_id, int(msg.requested_mode.value))
            )


class Ros:
    """rclpy lifecycle: spin in a background thread, shut down cleanly (no exit segfault)."""

    def __init__(self) -> None:
        rclpy.init()
        self.node = Watch()
        self.executor = MultiThreadedExecutor()
        self.executor.add_node(self.node)
        self.thread = threading.Thread(target=self.executor.spin, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.executor.shutdown(timeout_sec=2.0)
        self.thread.join(timeout=2.0)
        self.node.destroy_node()
        rclpy.try_shutdown()


def wait_for(pred: Any, timeout: float, period: float = 0.2) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(period)
    return False


# --------------------------------------------------------------------------- task requests


class TaskApi:
    """Task requests over ROS (default) or the RMF API server. Same JSON either way."""

    def __init__(self, node: Watch, via: str) -> None:
        self.node = node
        self.via = via
        self.robot_had: dict[str, bool] = {}

    def request(self, payload: dict[str, Any], route: str) -> tuple[bool, Any]:
        if self.via == "api":
            status, resp = http("POST", route, payload)
            ok = status == 200 and isinstance(resp, dict) and bool(resp.get("success"))
            say(f"{payload['type']} via API -> HTTP {status}: {json.dumps(resp)[:400]}")
            return ok, resp
        rid = f"auspex_check_{uuid.uuid4().hex[:12]}"
        msg = ApiRequest()
        msg.request_id = rid
        msg.json_msg = json.dumps(payload)
        self.node.task_pub.publish(msg)

        def answered() -> bool:
            with self.node.lock:
                return rid in self.node.seen.api_responses

        if not wait_for(answered, ROS_REQUEST_TIMEOUT_S):
            say(f"{payload['type']} via ROS: no response on /task_api_responses for {rid}")
            return False, None
        with self.node.lock:
            resp = self.node.seen.api_responses[rid]
        ok = isinstance(resp, dict) and bool(resp.get("success"))
        say(f"{payload['type']} via ROS ({rid}): {json.dumps(resp)[:400]}")
        return ok, resp

    def dispatch(self, fleet: str, place: str) -> str:
        payload = {
            "type": "dispatch_task_request",
            "request": {
                "category": "patrol",
                "description": {"places": [place], "rounds": 1},
                "unix_millis_earliest_start_time": 0,
                "unix_millis_request_time": int(time.time() * 1000),
                "requester": "auspex_check_levers",
                "fleet_name": fleet,
            },
        }
        ok, resp = self.request(payload, "/tasks/dispatch_task")
        task_id = ((resp or {}).get("state") or {}).get("booking", {}).get("id") if ok else None
        if not task_id:
            sys.exit("FAIL dispatch was not accepted (see the response above)")
        say(f"task {task_id} dispatched to fleet {fleet}, place {place}")
        return str(task_id)

    def state(self, task_id: str) -> dict[str, Any]:
        """{status, fleet, robot, errors}. Over ROS: dispatch state, then the robot's task_id."""
        if self.via == "api":
            status, resp = http("GET", f"/tasks/{task_id}/state")
            if status != 200 or not isinstance(resp, dict):
                return {"status": None, "fleet": None, "robot": None, "errors": []}
            assigned = resp.get("assigned_to") or {}
            return {
                "status": resp.get("status"),
                "fleet": assigned.get("group"),
                "robot": assigned.get("name"),
                "errors": [],
            }
        with self.node.lock:
            d = self.node.seen.dispatch.get(task_id)
            robot_tasks = dict(self.node.seen.robot_task)
        if d is None:
            return {"status": None, "fleet": None, "robot": None, "errors": []}
        status, fleet, robot, errors = d
        if status == "dispatched" and robot:
            key = f"{fleet}/{robot}"
            if robot_tasks.get(key) == task_id:
                self.robot_had[task_id] = True
                status = "underway"
            elif self.robot_had.get(task_id):
                status = "finished"
        return {"status": status, "fleet": fleet or None, "robot": robot or None, "errors": errors}


# --------------------------------------------------------------------------- helpers


def graph_for(node: Watch, fleet: str) -> Any:
    if not wait_for(lambda: fleet in node.seen.graphs, 10.0):
        sys.exit(
            f"FAIL no /nav_graphs message for fleet {fleet} within 10 s: is its fleet adapter "
            "up? (grep 'process has died\\|Successfully added' in the sim log)"
        )
    return node.seen.graphs[fleet]


def param_value(p: Any) -> Any:
    # rmf_building_map_msgs/Param: type STRING=1 INT=2 DOUBLE=3 BOOL=4
    return {1: p.value_string, 2: p.value_int, 3: p.value_float, 4: p.value_bool}.get(p.type)


def crossing_lanes(graph: Any, crossing: str) -> list[int]:
    if any(e.edge_type != 1 for e in graph.edges):
        sys.exit("FAIL graph has a bidirectional edge; lane index != edge index (see doc)")
    lanes = []
    for idx, edge in enumerate(graph.edges):
        for p in edge.params:
            if p.name in CROSSING_PARAMS and param_value(p) == crossing:
                lanes.append(idx)
                break
    return lanes


def place_level(graph: Any, place: str) -> str | None:
    for v in graph.vertices:
        if v.name == place:
            for p in v.params:
                if p.name == "map_name":
                    return str(param_value(p))
    return None


def is_lift(graph: Any, crossing: str) -> bool:
    return any(
        "lift" in p.name and param_value(p) == crossing for e in graph.edges for p in e.params
    )


def lane_request(node: Watch, fleet: str, close: list[int], open_: list[int]) -> None:
    wait_for(lambda: node.lane_pub.get_subscription_count() > 0, 5.0)
    say(
        f"publish LaneRequest fleet={fleet} close={close} open={open_} "
        f"(subscribers={node.lane_pub.get_subscription_count()})"
    )
    msg = LaneRequest()
    msg.fleet_name = fleet
    msg.close_lanes = close
    msg.open_lanes = open_
    for _ in range(3):  # subscriber is best-effort depth 1: send a few times
        node.lane_pub.publish(msg)
        time.sleep(0.3)


def readback(node: Watch, fleet: str, lanes: list[int], closed: bool) -> bool:
    def ok() -> bool:
        with node.lock:
            state = set(node.seen.lane_states.get(fleet, []))
        return set(lanes) <= state if closed else not (set(lanes) & state)

    good = wait_for(ok, 10.0)
    with node.lock:
        ls = sorted(node.seen.lane_states.get(fleet, []))
        cl = sorted(node.seen.closed_lanes.get(fleet, []))
    say(f"readback /lane_states closed_lanes={ls}  /closed_lanes={cl}")
    return good


def position(node: Watch, key: str) -> tuple[str, float, float, int] | None:
    with node.lock:
        return node.seen.positions.get(key)


def moved_since(node: Watch, key: str, start: tuple[str, float, float, int], dist: float) -> bool:
    cur = position(node, key)
    if cur is None:
        return False
    return cur[0] != start[0] or math.hypot(cur[1] - start[1], cur[2] - start[2]) > dist


def call_replan(node: Watch, fleet: str, robot: str) -> bool:
    name = f"/auspex/replan/{fleet}/{robot}"
    client = node.create_client(Trigger, name)
    if not client.wait_for_service(timeout_sec=5.0):
        say(f"replan service {name} not available (is the sim running via scripts/run-sim?)")
        return False
    future = client.call_async(Trigger.Request())
    if not wait_for(future.done, 5.0):
        say(f"replan service {name} did not answer within 5 s")
        return False
    res = future.result()
    say(f"replan service {name}: success={res.success} message={res.message!r}")
    return bool(res.success)


# --------------------------------------------------------------------------- subcommands


def cmd_api(_ros: Ros | None, _args: argparse.Namespace) -> int:
    status, body = http("GET", "/user")
    say(f"GET /user -> HTTP {status}: {json.dumps(body)[:300]}")
    if status == 200:
        verdict("PASS", "API server accepts these credentials")
    else:
        verdict("FAIL", f"API server answered {status}; task checks default to --via ros")
    status, schema = http("GET", "/openapi.json")
    if status == 200 and isinstance(schema, dict):
        paths = sorted(p for p in schema.get("paths", {}) if p.startswith("/tasks"))
        say("task routes: " + ", ".join(paths))
    return 0


def cmd_places(ros: Ros, args: argparse.Namespace) -> int:
    graph = graph_for(ros.node, args.fleet)
    by_level: dict[str, list[str]] = {}
    for v in graph.vertices:
        if not v.name:
            continue
        props = {p.name: param_value(p) for p in v.params}
        if props.get("is_holding_point"):
            by_level.setdefault(str(props.get("map_name", "?")), []).append(v.name)
    say(f"fleet {args.fleet}: {len(graph.vertices)} vertices, {len(graph.edges)} lanes")
    for level, names in sorted(by_level.items()):
        print(f"holding points on {level}: " + ", ".join(sorted(names)))
    return 0


def cmd_lanes(ros: Ros, args: argparse.Namespace) -> int:
    node = ros.node
    lanes = crossing_lanes(graph_for(node, args.fleet), args.crossing)
    if not lanes:
        verdict("FAIL", f"no lanes in {args.fleet}'s graph cross {args.crossing}")
        return 1
    say(f"{args.crossing} lanes for {args.fleet} (edge index): {lanes}")
    lane_request(node, args.fleet, lanes, [])
    closed_ok = readback(node, args.fleet, lanes, closed=True)
    lane_request(node, args.fleet, [], lanes)
    open_ok = readback(node, args.fleet, lanes, closed=False)
    verdict("PASS" if closed_ok else "FAIL", f"close {args.crossing} lanes {lanes} read back")
    verdict("PASS" if open_ok else "FAIL", f"reopen {args.crossing} lanes {lanes} read back")
    return 0 if closed_ok and open_ok else 1


def cmd_route(ros: Ros, args: argparse.Namespace) -> int:
    node = ros.node
    api = TaskApi(node, args.via)
    lanes: list[int] = []
    if args.close:
        lanes = crossing_lanes(graph_for(node, args.fleet), args.close)
        if not lanes:
            verdict("FAIL", f"no lanes in {args.fleet}'s graph cross {args.close}")
            return 1
        say(f"{args.close} lanes for {args.fleet}: {lanes}")
    graph = graph_for(node, args.fleet)
    dest_level = place_level(graph, args.place)
    lift_closed = bool(lanes) and is_lift(graph, args.close)
    closed_at: float | None = None
    replan_ok: bool | None = None
    robot = group = None
    last: dict[str, Any] = {"status": None, "errors": []}
    dispatched_at = time.monotonic()
    start_pos: tuple[str, float, float, int] | None = None
    try:
        if lanes and args.when == "before":
            lane_request(node, args.fleet, lanes, [])
            if not readback(node, args.fleet, lanes, closed=True):
                verdict("FAIL", "closure did not read back; not dispatching")
                return 1
            closed_at = time.monotonic()
        task_id = api.dispatch(args.fleet, args.place)
        dispatched_at = time.monotonic()
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            st = api.state(task_id)
            if st["status"] != last["status"]:
                say(f"task status {last['status']} -> {st['status']}")
            last = st
            if robot is None and st["robot"]:
                robot, group = st["robot"], st["fleet"]
                start_pos = position(node, f"{args.fleet}/{robot}")
                say(f"assigned to {group}/{robot}; start position {start_pos}")
            if (
                lanes
                and args.when == "moving"
                and closed_at is None
                and robot
                and start_pos
                and moved_since(node, f"{args.fleet}/{robot}", start_pos, 0.5)
            ):
                say(f"{robot} is moving: closing {args.close} mid-task")
                lane_request(node, args.fleet, lanes, [])
                readback(node, args.fleet, lanes, closed=True)
                closed_at = time.monotonic()
                replan_ok = call_replan(node, args.fleet, robot)
            if st["status"] in TERMINAL:
                break
            time.sleep(1.0)
    finally:
        if lanes:
            lane_request(node, args.fleet, [], lanes)
            readback(node, args.fleet, lanes, closed=False)

    with node.lock:
        lifts = [r for r in node.seen.lift_requests if robot and robot in r[2]]
        doors = [r for r in node.seen.door_requests if robot and robot in r[2]]
    used_lifts: list[str] = []
    for t, lift, session, floor in lifts:
        if lift not in used_lifts:
            used_lifts.append(lift)
            say(f"first lift request: {lift} to {floor} by {session} at +{t - dispatched_at:.1f}s")
    used_doors = list(dict.fromkeys(d[1] for d in doors))
    say(f"lifts requested: {used_lifts or 'none'}; doors requested: {used_doors or 'none'}")
    if last["errors"]:
        say(f"dispatch errors: {last['errors']}")

    ok = True
    if robot is None:
        verdict("FAIL", f"task was never assigned (last status {last['status']})")
        return 1
    if group == args.fleet:
        verdict("PASS", f"task restricted to {args.fleet} was assigned to {group}/{robot}")
    else:
        verdict("FAIL", f"task restricted to {args.fleet} went to {group}/{robot}")
        ok = False
    if last["status"] == "completed":
        verdict("PASS", "task completed")
    elif last["status"] == "finished":
        verdict("PASS", "task finished (left the robot; over ROS, completed and failed look alike)")
    else:
        verdict("FAIL", f"task ended as {last['status']} (timeout {args.timeout}s)")
        ok = False
    same_level = start_pos is not None and dest_level is not None and start_pos[0] == dest_level
    if lanes:
        if lift_closed and same_level:
            verdict(
                "INCONCLUSIVE",
                f"{robot} started on {dest_level}, the destination's level: the task needs no "
                f"lift, so closing {args.close} proves nothing (pick a place on the other level)",
            )
            ok = False
        elif closed_at is None:
            verdict("INCONCLUSIVE", f"{robot} never moved, so {args.close} was not closed")
            ok = False
        else:
            after = [r for r in lifts + doors if r[0] >= closed_at + 1.0 and r[1] == args.close]
            if after:
                verdict("FAIL", f"{robot} requested {args.close} after its lanes were closed")
                ok = False
            else:
                verdict("PASS", f"{robot} did not request {args.close} after closure")
        if replan_ok is not None:
            verdict("PASS" if replan_ok else "FAIL", "Auspex replan service accepted the call")
            ok = ok and replan_ok
    return 0 if ok else 1


def cmd_interrupt(ros: Ros, args: argparse.Namespace) -> int:
    node = ros.node
    api = TaskApi(node, args.via)
    task_id = api.dispatch(args.fleet, args.place)
    if not wait_for(lambda: bool(api.state(task_id)["robot"]), 60.0, 1.0):
        verdict("FAIL", f"task was not assigned within 60 s ({api.state(task_id)})")
        return 1
    robot = api.state(task_id)["robot"]
    key = f"{args.fleet}/{robot}"
    start_pos = position(node, key)
    if start_pos is None or not wait_for(lambda: moved_since(node, key, start_pos, 0.5), 90.0):
        verdict("FAIL", f"{robot} did not start moving within 90 s")
        return 1
    accepted, resp = api.request(
        {"type": "interrupt_task_request", "task_id": task_id}, "/tasks/interrupt_task"
    )
    token = resp.get("token") if isinstance(resp, dict) else None
    time.sleep(5.0)  # let it come to rest
    held_pos = position(node, key)
    held = held_pos is not None and not wait_for(
        lambda: moved_since(node, key, held_pos, 0.3), 15.0
    )
    say(f"after interrupt: status={api.state(task_id)['status']} held={held}")
    body: dict[str, Any] = {"type": "resume_task_request", "for_task": task_id}
    if token:
        body["for_tokens"] = [token]
    resumed_ok, _ = api.request(body, "/tasks/resume_task")
    pos = position(node, key)
    resumed = pos is not None and wait_for(lambda: moved_since(node, key, pos, 0.5), 60.0)
    say(f"after resume: status={api.state(task_id)['status']} moving={resumed}")
    verdict("PASS" if accepted else "FAIL", f"interrupt request accepted (token {token})")
    verdict("PASS" if held else "FAIL", f"{robot} stayed put for 15 s while interrupted")
    verdict("PASS" if resumed_ok else "FAIL", "resume request accepted")
    verdict("PASS" if resumed else "FAIL", f"{robot} moved again after resume")
    api.request({"type": "cancel_task_request", "task_id": task_id}, "/tasks/cancel_task")
    say(f"cancel requested for check task {task_id}")
    return 0 if accepted and held and resumed_ok and resumed else 1


def cmd_ready(ros: Ros, args: argparse.Namespace) -> int:
    node = ros.node
    expected = [f for f in args.fleets.split(",") if f]
    first_seen: dict[str, float] = {}

    def status() -> dict[str, tuple[list[str], list[str]]]:
        services = {name for name, _ in node.get_service_names_and_types()}
        with node.lock:
            keys = list(node.seen.positions)
        out: dict[str, tuple[list[str], list[str]]] = {}
        for fleet in expected:
            robots = sorted(k.split("/", 1)[1] for k in keys if k.startswith(f"{fleet}/"))
            if robots and fleet not in first_seen:
                first_seen[fleet] = time.monotonic()
                say(f"{fleet}: robots {robots}")
            missing = [r for r in robots if f"/auspex/replan/{fleet}/{r}" not in services]
            out[fleet] = (robots, missing)
        return out

    def all_ready() -> bool:
        return all(robots and not missing for robots, missing in status().values())

    wait_for(all_ready, args.timeout, 1.0)
    ok = True
    for fleet, (robots, missing) in status().items():
        if not robots:
            verdict(
                "FAIL",
                f"{fleet}: no robots in /fleet_states after {args.timeout:.0f} s "
                "(adapter hung or dead: grep 'process has died\\|Successfully added')",
            )
            ok = False
        elif missing:
            verdict("FAIL", f"{fleet}: no replan service for {missing}")
            ok = False
        else:
            verdict("PASS", f"{fleet}: {len(robots)} robots, all with replan services")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("api")
    sp = sub.add_parser("places")
    sp.add_argument("--fleet", required=True)
    sl = sub.add_parser("lanes")
    sl.add_argument("--fleet", required=True)
    sl.add_argument("--crossing", required=True)
    sr = sub.add_parser("route")
    sr.add_argument("--fleet", required=True)
    sr.add_argument("--place", required=True)
    sr.add_argument("--close")
    sr.add_argument("--when", choices=["before", "moving"], default="before")
    sr.add_argument("--timeout", type=float, default=300.0)
    sr.add_argument("--via", choices=["ros", "api"], default="ros")
    si = sub.add_parser("interrupt")
    si.add_argument("--fleet", required=True)
    si.add_argument("--place", required=True)
    si.add_argument("--via", choices=["ros", "api"], default="ros")
    sy = sub.add_parser("ready")
    sy.add_argument("--fleets", default="deliveryRobot,tinyRobot")
    sy.add_argument("--timeout", type=float, default=120.0)
    args = p.parse_args()
    if args.cmd == "api":
        return cmd_api(None, args)
    handler = {
        "places": cmd_places,
        "lanes": cmd_lanes,
        "route": cmd_route,
        "interrupt": cmd_interrupt,
        "ready": cmd_ready,
    }[args.cmd]
    ros = Ros()
    try:
        return handler(ros, args)
    finally:
        ros.close()


if __name__ == "__main__":
    sys.exit(main())
