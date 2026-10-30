"""Command runner (spec 7): carries out the core's `ros` commands against Open-RMF.

A thread inside the bridge. It wakes on NOTIFY auspex_commands (and every second for
commands whose start time has come), claims one runnable command at a time
(`FOR UPDATE SKIP LOCKED`, status running), carries it out and writes the result:
- close_lanes / open_lanes: LaneRequest on /lane_closure_requests for the fleet (RELIABLE,
  sent 3 times: the adapter's subscriber is best-effort depth 1), then read back from
  /lane_states for up to 10 s; up to 3 attempts. /closed_lanes is advisory and not used;
- replan_robot: the rmf_adapter_ext service /auspex/replan/<fleet>/<robot> (std_srvs/Trigger);
- submit_task, cancel_task, interrupt_task, resume_task: an ApiRequest on /task_api_requests
  with request_id = command id, answered on /task_api_responses (type 2). One attempt: a
  dispatch is not idempotent. A resume scheduled as an interrupt's inverse takes the token
  from that interrupt's result.
Runnable: a plan's steps in order (every earlier step succeeded); an inverse once its original
succeeded. On start-up, commands left running by a dead bridge are marked failed (the core
then rolls the plan back), never retried blindly.
The core ledgers every result and verifies post-conditions; this runner never decides.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import psycopg
from psycopg.rows import dict_row
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from rmf_fleet_msgs.msg import LaneRequest
from rmf_task_msgs.msg import ApiRequest, ApiResponse
from std_srvs.srv import Trigger

log = logging.getLogger("bridge.commands")

CHANNEL = "auspex_commands"
POLL_S = 1.0
LANE_SENDS = 3
LANE_READBACK_S = 10.0
LANE_ATTEMPTS = 3
TASK_RESPONSE_S = 10.0
REPLAN_TIMEOUT_S = 5.0
API_RESPONDING = 2

CLAIM_SQL = """
UPDATE commands SET status = 'running', attempts = attempts + 1, updated_at = now()
WHERE command_id = (
    SELECT c.command_id FROM commands c
    WHERE c.side = 'ros' AND c.status = 'pending'
      AND (c.not_before IS NULL OR c.not_before <= now())
      AND (
        (c.inverse_of IS NULL AND NOT EXISTS (
            SELECT 1 FROM commands p WHERE p.plan_id = c.plan_id AND p.inverse_of IS NULL
              AND p.seq < c.seq AND p.status <> 'succeeded'))
        OR (c.inverse_of IS NOT NULL AND EXISTS (
            SELECT 1 FROM commands o WHERE o.command_id = c.inverse_of
              AND o.status = 'succeeded'))
      )
    ORDER BY c.not_before NULLS FIRST, c.created_at, c.seq
    LIMIT 1 FOR UPDATE SKIP LOCKED
)
RETURNING command_id, plan_id, action, payload, attempts
"""


class CommandFailed(Exception):
    pass


def _qos(depth: int, reliable: bool, latched: bool) -> QoSProfile:
    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=depth,
        reliability=ReliabilityPolicy.RELIABLE if reliable else ReliabilityPolicy.BEST_EFFORT,
        durability=DurabilityPolicy.TRANSIENT_LOCAL if latched else DurabilityPolicy.VOLATILE,
    )


class RosLevers:
    """The ROS endpoints the runner uses; callbacks run in the bridge's executor thread."""

    def __init__(self, node: Node) -> None:
        self.node = node
        self.lock = threading.Lock()
        self.closed: dict[str, set[int]] = {}
        self.responses: dict[str, Any] = {}
        self.lane_pub = node.create_publisher(
            LaneRequest, "/lane_closure_requests", _qos(10, True, False)
        )
        # Same QoS as rmf_demos_tasks (confirmed M2).
        self.task_pub = node.create_publisher(ApiRequest, "/task_api_requests", _qos(1, True, True))
        node.create_subscription(
            ApiResponse, "/task_api_responses", self._response, _qos(10, True, True)
        )
        self.clients: dict[str, Any] = {}

    def on_lane_states(self, fleet: str, closed: list[int]) -> None:
        with self.lock:
            self.closed[fleet] = set(closed)

    def _response(self, msg: Any) -> None:
        if msg.type != API_RESPONDING:
            return
        try:
            body: Any = json.loads(msg.json_msg)
        except json.JSONDecodeError:
            body = {"raw": msg.json_msg}
        with self.lock:
            self.responses.setdefault(msg.request_id, body)

    def closed_lanes(self, fleet: str) -> set[int]:
        with self.lock:
            return set(self.closed.get(fleet, set()))

    def response(self, request_id: str) -> Any:
        with self.lock:
            return self.responses.get(request_id)

    def replan_client(self, fleet: str, robot: str) -> Any:
        name = f"/auspex/replan/{fleet}/{robot}"
        if name not in self.clients:
            self.clients[name] = self.node.create_client(Trigger, name)
        return self.clients[name]


def wait_for(pred: Callable[[], bool], timeout: float, period: float = 0.1) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(period)
    return pred()


class CommandRunner(threading.Thread):
    def __init__(self, dsn: str, levers: RosLevers) -> None:
        super().__init__(name="command-runner", daemon=True)
        self.dsn = dsn
        self.levers = levers
        self.stop = threading.Event()
        self.done = 0

    # ------------------------------------------------------------------ loop

    def run(self) -> None:
        backoff = 1.0
        recovered = False
        while not self.stop.is_set():
            try:
                with (
                    psycopg.connect(self.dsn, autocommit=True, connect_timeout=5) as listen,
                    psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row) as db,
                ):
                    listen.execute(f"LISTEN {CHANNEL}")
                    if not recovered:
                        self._recover(db)
                        recovered = True
                    backoff = 1.0
                    log.info("commands.listening channel=%s", CHANNEL)
                    while not self.stop.is_set():
                        while not self.stop.is_set() and self._run_one(db):
                            pass
                        for _ in listen.notifies(timeout=POLL_S, stop_after=1):
                            pass
            except (psycopg.OperationalError, OSError) as exc:
                log.error("commands.db_unavailable error=%r retry_s=%.0f", str(exc)[:200], backoff)
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def _recover(self, db: psycopg.Connection[Any]) -> None:
        rows = db.execute(
            "UPDATE commands SET status = 'failed', result = %s::jsonb, updated_at = now()"
            " WHERE side = 'ros' AND status = 'running' RETURNING command_id",
            (json.dumps({"error": "bridge restarted while the command was running"}),),
        ).fetchall()
        if rows:
            log.error("commands.recovered_as_failed ids=%s", [r["command_id"] for r in rows])

    def _run_one(self, db: psycopg.Connection[Any]) -> bool:
        row = db.execute(CLAIM_SQL).fetchone()
        if row is None:
            return False
        cid, action, payload = row["command_id"], row["action"], dict(row["payload"])
        log.info("command.start id=%s action=%s attempt=%d", cid, action, row["attempts"])
        t0 = time.monotonic()
        try:
            result = self._execute(db, cid, action, payload)
            status = "succeeded"
        except CommandFailed as exc:
            result, status = {"error": str(exc)}, "failed"
        except Exception as exc:  # a bug: fail the command, keep the runner alive
            log.exception("command.crashed id=%s", cid)
            result, status = {"error": f"{type(exc).__name__}: {exc}"}, "failed"
        result["elapsed_s"] = round(time.monotonic() - t0, 2)
        db.execute(
            "UPDATE commands SET status = %s, result = %s::jsonb, updated_at = now()"
            " WHERE command_id = %s",
            (status, json.dumps(result), cid),
        )
        self.done += 1
        log_fn = log.info if status == "succeeded" else log.error
        out = json.dumps(result)[:300]
        log_fn("command.%s id=%s action=%s result=%s", status, cid, action, out)
        return True

    # ------------------------------------------------------------------ actions

    def _execute(
        self, db: psycopg.Connection[Any], cid: str, action: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if action in ("close_lanes", "open_lanes"):
            fleet, lanes = str(payload["fleet"]), [int(i) for i in payload["lanes"]]
            return self._lanes(fleet, lanes, bool(payload["close"]))
        if action == "replan_robot":
            return self._replan(str(payload["fleet"]), str(payload["robot"]))
        if action in ("submit_task", "cancel_task", "interrupt_task", "resume_task"):
            request = dict(payload["request"])
            if action == "resume_task" and "for_tokens" not in request:
                request["for_tokens"] = [self._token(db, str(payload.get("token_from", "")))]
            result = self._task_api(cid, request)
            if action == "resume_task":
                result["sent_tokens"] = request["for_tokens"]  # which interruptions it released
            return result
        raise CommandFailed(f"unknown ros action {action}")

    def _lanes(self, fleet: str, lanes: list[int], close: bool) -> dict[str, Any]:
        want = set(lanes)

        def applied() -> bool:
            now = self.levers.closed_lanes(fleet)
            return want <= now if close else not (want & now)

        for attempt in range(1, LANE_ATTEMPTS + 1):
            msg = LaneRequest()
            msg.fleet_name = fleet
            msg.close_lanes = lanes if close else []
            msg.open_lanes = [] if close else lanes
            for _ in range(LANE_SENDS):
                self.levers.lane_pub.publish(msg)
                time.sleep(0.3)
            if wait_for(applied, LANE_READBACK_S):
                return {
                    "readback": sorted(self.levers.closed_lanes(fleet)),
                    "lane_attempts": attempt,
                    "subscribers": self.levers.lane_pub.get_subscription_count(),
                }
            log.warning(
                "lanes.not_applied fleet=%s close=%s lanes=%s attempt=%d",
                fleet,
                close,
                lanes,
                attempt,
            )
            time.sleep(float(attempt))
        raise CommandFailed(
            f"/lane_states did not show lanes {lanes} {'closed' if close else 'open'} for {fleet}"
            f" after {LANE_ATTEMPTS} attempts (now {sorted(self.levers.closed_lanes(fleet))})"
        )

    def _replan(self, fleet: str, robot: str) -> dict[str, Any]:
        client = self.levers.replan_client(fleet, robot)
        if not client.wait_for_service(timeout_sec=REPLAN_TIMEOUT_S):
            raise CommandFailed(f"replan service for {fleet}/{robot} not available")
        future = client.call_async(Trigger.Request())
        if not wait_for(future.done, REPLAN_TIMEOUT_S):
            raise CommandFailed(f"replan service for {fleet}/{robot} did not answer")
        res = future.result()
        if not res.success:
            raise CommandFailed(f"replan refused: {res.message}")
        return {"success": True, "message": res.message}

    def _task_api(self, cid: str, request: dict[str, Any]) -> dict[str, Any]:
        msg = ApiRequest()
        msg.request_id = cid
        msg.json_msg = json.dumps(request)
        self.levers.task_pub.publish(msg)
        if not wait_for(lambda: self.levers.response(cid) is not None, TASK_RESPONSE_S):
            raise CommandFailed(f"no response on /task_api_responses within {TASK_RESPONSE_S} s")
        resp = self.levers.response(cid)
        if not isinstance(resp, dict) or not resp.get("success"):
            raise CommandFailed(f"{request['type']} refused: {json.dumps(resp)[:300]}")
        return dict(resp)

    def _token(self, db: psycopg.Connection[Any], interrupt_id: str) -> str:
        row = db.execute(
            "SELECT result->>'token' AS token FROM commands WHERE command_id = %s"
            " AND status = 'succeeded'",
            (interrupt_id,),
        ).fetchone()
        if row is None or row["token"] is None:
            raise CommandFailed(f"no token from interrupt {interrupt_id}")
        return str(row["token"])
