"""ROS bridge: Open-RMF topics -> Postgres event queue (spec 4). The only rclpy process.

Runs in its own venv (.venv-bridge: system Python 3.12 + system site packages for rclpy, plus
psycopg). Each topic is converted to a plain dict (convert.py) and inserted into `events`
with source "ros". Rates are bounded so the queue stays small:
- fleet_state: per fleet at most once per FLEET_PERIOD_S, and at once when any robot's task,
  mode or level changes;
- lift_state / door_state: on change of their meaningful fields, else a heartbeat every
  HEARTBEAT_S; lane_states, dispatch_states, nav_graph, ingestor_state: on change.
lift_request: on change, else every 5 s while the robot keeps requesting (a robot waiting
for a lift re-publishes every 1 s), so the core can tell an active request from a stale one.
Every STATS_PERIOD_S it logs and emits per-topic message counts (`bridge_stats`), which shows
at a glance which topics are quiet.

Commands from the core (lane closures, replans, task requests) are carried out by the
command runner thread (commands.py, M7).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from rmf_building_map_msgs.msg import Graph
from rmf_door_msgs.msg import DoorState
from rmf_fleet_msgs.msg import FleetState, LaneStates
from rmf_ingestor_msgs.msg import IngestorState
from rmf_lift_msgs.msg import LiftRequest, LiftState
from rmf_task_msgs.msg import DispatchStates

sys.path.insert(0, str(Path(__file__).resolve().parent))
import convert
from commands import CommandRunner, RosLevers

FLEET_PERIOD_S = 1.0
HEARTBEAT_S = 30.0
STATS_PERIOD_S = 30.0
QUEUE_MAX = 10_000
BATCH_MAX = 200

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s bridge %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("bridge")


def _digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _qos_any(depth: int) -> QoSProfile:
    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=depth,
        reliability=ReliabilityPolicy.BEST_EFFORT,
        durability=DurabilityPolicy.VOLATILE,
    )


def _qos(depth: int, latched: bool) -> QoSProfile:
    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=depth,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.TRANSIENT_LOCAL if latched else DurabilityPolicy.VOLATILE,
    )


class Writer(threading.Thread):
    """Batches events into Postgres; reconnects with backoff and keeps the pending batch."""

    def __init__(self, dsn: str, q: queue.Queue[tuple[str, str, dict[str, Any]]]) -> None:
        super().__init__(name="event-writer", daemon=True)
        self.dsn = dsn
        self.q = q
        self.stop = threading.Event()
        self.written: Counter[str] = Counter()

    def run(self) -> None:
        batch: list[tuple[str, str, dict[str, Any]]] = []
        backoff = 1.0
        while not self.stop.is_set() or batch or not self.q.empty():
            try:
                with psycopg.connect(self.dsn, connect_timeout=5) as conn:
                    log.info("db.connected")
                    backoff = 1.0
                    while not self.stop.is_set() or batch or not self.q.empty():
                        if not batch:
                            batch = self._take()
                        if not batch:
                            continue
                        with conn.transaction(), conn.cursor() as cur:
                            cur.executemany(
                                "INSERT INTO events (event_id, source, kind, payload)"
                                " VALUES (%s, 'ros', %s, %s::jsonb) ON CONFLICT DO NOTHING",
                                [(eid, kind, json.dumps(p)) for eid, kind, p in batch],
                            )
                        for _, kind, _ in batch:
                            self.written[kind] += 1
                        batch = []
            except (psycopg.OperationalError, OSError) as exc:
                log.error(
                    "db.unavailable error=%r retry_s=%.0f pending=%d",
                    str(exc).splitlines()[0] if str(exc) else exc,
                    backoff,
                    len(batch),
                )
                if self.stop.is_set():
                    log.error("db.giving_up_at_shutdown lost=%d", len(batch) + self.q.qsize())
                    return
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def _take(self) -> list[tuple[str, str, dict[str, Any]]]:
        try:
            first = self.q.get(timeout=0.2)
        except queue.Empty:
            return []
        out = [first]
        while len(out) < BATCH_MAX:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                break
        return out


class Bridge(Node):
    def __init__(self, q: queue.Queue[tuple[str, str, dict[str, Any]]]) -> None:
        super().__init__("auspex_ros_bridge")
        self.q = q
        self.received: Counter[str] = Counter()
        self.emitted: Counter[str] = Counter()
        self.dropped = 0
        self._last: dict[str, tuple[str, float]] = {}  # key -> (digest, monotonic time)
        self.levers = RosLevers(self)
        self._sub(FleetState, "/fleet_states", _qos(10, True), self._fleet)
        lane_states = self._simple("lane_states", convert.lane_states, "fleet")

        def on_lanes(msg: Any) -> None:
            self.levers.on_lane_states(msg.fleet_name, [int(i) for i in msg.closed_lanes])
            lane_states(msg)

        self._sub(LaneStates, "/lane_states", _qos(10, True), on_lanes)
        self._sub(Graph, "/nav_graphs", _qos(10, True), self._graph)
        self._sub(LiftState, "/lift_states", _qos(100, False), self._lift)
        # Fleet adapters publish lift requests to /adapter_lift_requests (every 1 s while
        # waiting); the lift supervisor forwards only the session it has granted to
        # /lift_requests. Listening here sees robots queued behind another session too.
        self._sub(LiftRequest, "/adapter_lift_requests", _qos_any(100), self._lift_request)
        self._sub(DoorState, "/door_states", _qos(100, False), self._door)
        self._sub(
            DispatchStates,
            "/dispatch_states",
            _qos(10, False),
            self._simple("dispatch_states", convert.dispatch_states, None),
        )
        self._sub(
            IngestorState,
            "/ingestor_states",
            _qos(10, False),
            self._simple("ingestor_state", convert.ingestor_state, "guid"),
        )
        self.create_timer(STATS_PERIOD_S, self._stats)

    def _sub(self, msg_type: Any, topic: str, qos: QoSProfile, cb: Callable[[Any], None]) -> None:
        def wrapped(msg: Any) -> None:
            self.received[topic] += 1
            try:
                cb(msg)
            except Exception:
                log.exception("convert.failed topic=%s", topic)

        self.create_subscription(msg_type, topic, wrapped, qos)

    def emit(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            self.q.put_nowait((f"ros:{kind}:{uuid.uuid4().hex}", kind, payload))
            self.emitted[kind] += 1
        except queue.Full:
            self.dropped += 1

    def _changed(self, key: str, payload: Any, heartbeat: float | None = None) -> bool:
        digest = _digest(payload)
        prev = self._last.get(key)
        now = time.monotonic()
        if (
            prev is None
            or prev[0] != digest
            or (heartbeat is not None and now - prev[1] >= heartbeat)
        ):
            self._last[key] = (digest, now)
            return True
        return False

    def _simple(
        self, kind: str, fn: Callable[[Any], dict[str, Any]], key_field: str | None
    ) -> Callable[[Any], None]:
        def cb(msg: Any) -> None:
            payload = fn(msg)
            key = f"{kind}/{payload[key_field] if key_field else ''}"
            if self._changed(key, payload):
                self.emit(kind, payload)

        return cb

    def _fleet(self, msg: Any) -> None:
        payload = convert.fleet_state(msg)
        essentials = [(r["name"], r["task_id"], r["mode"], r["level"]) for r in payload["robots"]]
        key = f"fleet_state/{payload['fleet']}"
        digest = _digest(essentials)
        prev = self._last.get(key)
        now = time.monotonic()
        if prev is None or prev[0] != digest or now - prev[1] >= FLEET_PERIOD_S:
            self._last[key] = (digest, now)
            self.emit("fleet_state", payload)

    def _lift(self, msg: Any) -> None:
        payload = convert.lift_state(msg)
        meaningful = {k: v for k, v in payload.items() if k != "lift_time"}
        if self._changed(f"lift_state/{payload['lift_name']}", meaningful, HEARTBEAT_S):
            self.emit("lift_state", payload)

    def _lift_request(self, msg: Any) -> None:
        payload = convert.lift_request(msg)
        key = f"lift_request/{payload['session_id']}"
        if self._changed(key, {k: v for k, v in payload.items() if k != "request_time"}, 5.0):
            self.emit("lift_request", payload)

    def _door(self, msg: Any) -> None:
        payload = convert.door_state(msg)
        if self._changed(f"door_state/{payload['door_name']}", payload["mode"], HEARTBEAT_S):
            self.emit("door_state", payload)

    def _graph(self, msg: Any) -> None:
        payload = convert.graph_to_dict(msg, datetime.now(UTC).isoformat().replace("+00:00", "Z"))
        comparable = {k: v for k, v in payload.items() if k != "captured_at"}
        if self._changed(f"nav_graph/{payload['fleet']}", comparable):
            self.emit("nav_graph", payload)

    def _stats(self) -> None:
        counts = dict(sorted(self.received.items()))
        quiet = sorted(
            t
            for t in (
                "/fleet_states",
                "/lane_states",
                "/nav_graphs",
                "/lift_states",
                "/adapter_lift_requests",
                "/door_states",
                "/dispatch_states",
                "/ingestor_states",
            )
            if self.received.get(t, 0) == 0
        )
        log.info(
            "stats received=%s emitted=%s dropped=%d queue=%d quiet=%s",
            counts,
            dict(self.emitted),
            self.dropped,
            self.q.qsize(),
            quiet,
        )
        self.emit(
            "bridge_stats",
            {
                "counts": counts,
                "emitted": dict(self.emitted),
                "dropped": self.dropped,
                "quiet": quiet,
            },
        )


def main() -> int:
    dsn = os.environ.get("DATABASE_URL", "").replace("postgresql+psycopg://", "postgresql://", 1)
    if not dsn.startswith("postgresql://"):
        print("FAIL DATABASE_URL is not set (scripts/run-bridge passes it from .env)")
        return 1
    q: queue.Queue[tuple[str, str, dict[str, Any]]] = queue.Queue(maxsize=QUEUE_MAX)
    writer = Writer(dsn, q)
    writer.start()
    rclpy.init()
    node = Bridge(q)
    runner = CommandRunner(dsn, node.levers)
    runner.start()
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    log.info("started topics=8 fleet_period_s=%.1f heartbeat_s=%.0f", FLEET_PERIOD_S, HEARTBEAT_S)
    try:
        while not stop.is_set() and rclpy.ok():
            executor.spin_once(timeout_sec=0.2)
    finally:
        runner.stop.set()
        runner.join(timeout=15.0)
        executor.shutdown(timeout_sec=2.0)
        node.destroy_node()
        rclpy.try_shutdown()
        writer.stop.set()
        writer.join(timeout=5.0)
        log.info(
            "stopped written=%s dropped=%d commands=%d",
            dict(writer.written),
            node.dropped,
            runner.done,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
