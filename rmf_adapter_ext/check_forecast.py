"""M5 live check (scripts/check-forecast): forecast, conflict and stall detection on the sim.

Needs the sim (scripts/run-sim), the core (scripts/run-core) and the bridge (scripts/run-bridge).
Steps (S1's mechanics, with the transport's destination coded because Nemotron arrives in M10):
1. post an oxygen delivery request (L2 oxygen store -> bed 12, Critical) to the core;
2. dispatch the matching task to deliveryRobot over ROS and link it to the job (status update);
3. the core's forecast must route the robot through lift_25 (pickup on L2, then down to L1);
4. post a Stat bariatric bed transport Ward 2B -> Imaging, timed so the porter's lift_25 claim
   overlaps the robot's return trip; the core must detect the conflict BEFORE the robot gets
   to the lift;
5. interrupt the robot (an unannounced stall): the core must open an incident; resume with
   the interrupt's token: the incident must resolve;
6. cancel the task and both jobs: the conflict must clear.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_levers import Ros, TaskApi, moved_since, position, say, verdict, wait_for

CORE = os.environ.get("CORE_URL", "http://127.0.0.1:8100").rstrip("/")
TOKEN = os.environ.get("ADMIN_TOKEN", "")
FLEET = "deliveryRobot"
PICKUP_WP, DROPOFF_WP = "L2_sub_waiting_area_1", "L1_left_treatment_1"
LIFT = "segment/lift_25"
PORTER_LIFT_OFFSET_S = 60 + 35 + 25 - 30  # status lead + legs before the lift - pad (routes.yaml)
PORTER_LIFT_LEN_S = 240 + 60  # lift leg + 2 x pad
STALL_WAIT_S = 45 + 40  # STALL_THRESHOLD_S default + tick and bridge latency

_ok: list[bool] = []


def check(ok: bool, msg: str) -> bool:
    _ok.append(ok)
    verdict("PASS" if ok else "FAIL", msg)
    return ok


def core(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    req = urllib.request.Request(  # noqa: S310
        f"{CORE}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        sys.exit(f"FAIL core {method} {path} -> HTTP {exc.code}: {exc.read().decode()[:300]}")
    except (urllib.error.URLError, TimeoutError) as exc:
        sys.exit(f"FAIL core not reachable at {CORE} ({exc}); is scripts/run-core running?")


def iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat()


def lift_windows(job_urn: str) -> list[tuple[datetime, datetime]]:
    tl = core("GET", "/api/v1/timeline")
    out = []
    for c in tl["segments"].get(LIFT, []):
        if c["job_urn"] == job_urn and c["basis"] == "eta_model":
            out.append(
                (
                    datetime.fromisoformat(c["window"]["start"]),
                    datetime.fromisoformat(c["window"]["end"]),
                )
            )
    return sorted(out)


def conflict_between(a: str, b: str) -> dict[str, Any] | None:
    for c in core("GET", "/api/v1/conflicts"):
        if sorted(c["job_urns"]) == sorted([a, b]) and c["segment_urn"] == LIFT:
            return dict(c)
    return None


def open_incident(task: str) -> dict[str, Any] | None:
    for i in core("GET", "/api/v1/incidents")["open"]:
        if i["rmf_task_id"] == task:
            return dict(i)
    return None


def main() -> int:
    if not TOKEN:
        print("FAIL ADMIN_TOKEN not set (scripts/check-forecast passes it from .env)")
        return 1
    health = core("GET", "/api/v1/health")
    robots = health["ros"]["fleets"].get(FLEET, [])
    if not robots:
        print(f"FAIL the core sees no {FLEET} robots; run scripts/check-ingest first")
        return 1
    rtf = health["ros"].get("real_time_factor")
    say(f"core up; {FLEET} robots {robots}; real-time factor {rtf}")
    tag = datetime.now(UTC).strftime("%H%M%S")
    o2_id, tr_id = f"O2-CHK-{tag}", f"4471-CHK-{tag}"
    o2_urn = f"job/delivery_requests-{o2_id}"
    tr_urn = f"job/transport_system-{tr_id}"
    now = datetime.now(UTC)
    core(
        "POST",
        "/api/v1/jobs",
        {
            "system": "delivery_requests",
            "request_id": o2_id,
            "priority": "Critical",
            "item": "oxygen",
            "pickup_code": "L2-O2",
            "dropoff_code": "L1-W1A-12",
            "needed_by": iso(now + timedelta(minutes=15)),
            "requested_by": "staff-alex",
            "receiver": "rohan-arm-1",
            "status": "Submitted",
            "status_at": iso(now),
        },
    )
    say(f"posted oxygen request {o2_urn}")

    ros = Ros()
    task = ""
    pending_token: str | None = None  # interrupted and not yet resumed
    try:
        api = TaskApi(ros.node, "ros")
        payload = {
            "type": "dispatch_task_request",
            "request": {
                "category": "patrol",
                "description": {"places": [PICKUP_WP, DROPOFF_WP], "rounds": 1},
                "unix_millis_earliest_start_time": 0,
                "unix_millis_request_time": int(time.time() * 1000),
                "requester": "auspex_check_forecast",
                "fleet_name": FLEET,
            },
        }
        ok, resp = api.request(payload, "/tasks/dispatch_task")
        task = ((resp or {}).get("state") or {}).get("booking", {}).get("id", "") if ok else ""
        if not check(bool(task), f"oxygen task dispatched over ROS ({task or resp})"):
            return 1
        core(
            "POST",
            f"/api/v1/jobs/delivery_requests-{o2_id}/status",
            {"status": "Assigned", "status_at": iso(datetime.now(UTC)), "rmf_task_id": task},
        )
        if not wait_for(lambda: bool(api.state(task)["robot"]), 60.0, 1.0):
            check(False, f"task assigned within 60 s ({api.state(task)})")
            return 1
        robot = api.state(task)["robot"]
        key = f"{FLEET}/{robot}"
        say(f"task {task} assigned to {key}; start {position(ros.node, key)}")
        # The robot may first finish another task (e.g. its automatic return to the charger
        # after an earlier run was cancelled); the core forecasts a task once it executes.
        if not wait_for(lambda: api.state(task)["status"] == "underway", 180.0, 1.0):
            check(False, f"{robot} starts the task within 180 s ({api.state(task)})")
            return 1
        say(f"{robot} is executing the task; position {position(ros.node, key)}")

        # 3. forecast route
        def routed() -> bool:
            return o2_urn in core("GET", "/api/v1/timeline")["robot_routes"]

        if not check(wait_for(routed, 20.0, 1.0), "core forecasts a route for the linked robot"):
            return 1
        route = core("GET", "/api/v1/timeline")["robot_routes"][o2_urn]
        say(
            f"forecast route: lifts {route['lifts']}, ETA {route['eta']} ({route['total_s']:.0f} s)"
        )
        check(LIFT in route["lifts"], f"forecast route uses lift_25 ({route['lifts']})")
        windows = lift_windows(o2_urn)
        say("robot lift_25 claims: " + ", ".join(f"{a:%H:%M:%S}-{b:%H:%M:%S}" for a, b in windows))
        if not windows:
            check(False, "robot has lift_25 claims")
            return 1

        # 4. transport timed to overlap the robot's last lift_25 pass
        start, end = windows[-1]
        latest_ok = end - timedelta(seconds=PORTER_LIFT_OFFSET_S + 20)
        earliest_ok = start - timedelta(seconds=PORTER_LIFT_OFFSET_S + PORTER_LIFT_LEN_S - 20)
        now = datetime.now(UTC)
        if now < earliest_ok:
            say(f"waiting {(earliest_ok - now).total_seconds():.0f} s so the porter overlaps")
            time.sleep((earliest_ok - now).total_seconds())
        status_at = min(datetime.now(UTC), latest_ok)
        core(
            "POST",
            "/api/v1/jobs",
            {
                "system": "transport_system",
                "request_id": tr_id,
                "priority": "Stat",
                "origin_code": "L2-W2B",
                "destination_code": "L1-IMG",
                "destination_text": "CT 2",
                "transport_mode": "bed",
                "bed_type": "bariatric",
                "patient_ref": "P-CHK",
                "notes": "on portable vent, needs large car",
                "status": "Responding",
                "status_at": iso(status_at),
            },
        )
        posted = datetime.now(UTC)
        say(f"posted Stat bariatric transport {tr_urn} (responding at {status_at:%H:%M:%S})")
        if check(
            wait_for(lambda: conflict_between(o2_urn, tr_urn) is not None, 20.0, 1.0),
            "core detects the lift_25 conflict between the transport and the oxygen robot",
        ):
            c = conflict_between(o2_urn, tr_urn) or {}
            lead = (windows[-1][0] - datetime.now(UTC)).total_seconds()
            say(
                f"conflict {c.get('conflict_id')} {c.get('kind')} window "
                f"{c['window']['start'][11:19]}-{c['window']['end'][11:19]}; detected "
                f"{(datetime.now(UTC) - posted).total_seconds():.0f} s after the transport"
            )
            check(lead > 0, f"detected before the robot reaches the lift ({lead:.0f} s ahead)")

        # 5. unannounced stall
        here = position(ros.node, key)
        if here is None or not wait_for(lambda: moved_since(ros.node, key, here, 1.0), 60.0):
            check(False, f"{robot} moving before the stall test")
        else:
            ok, resp = api.request(
                {"type": "interrupt_task_request", "task_id": task}, "/tasks/interrupt_task"
            )
            token = resp.get("token") if isinstance(resp, dict) else None
            pending_token = str(token) if token is not None else None
            check(ok and token is not None, f"robot interrupted (token {token})")
            stalled_at = datetime.now(UTC)
            if check(
                wait_for(lambda: open_incident(task) is not None, STALL_WAIT_S, 2.0),
                f"core opens an incident within {STALL_WAIT_S} s",
            ):
                inc = open_incident(task) or {}
                say(
                    f"incident {inc.get('incident_id')} trigger={inc.get('trigger')} after "
                    f"{(datetime.now(UTC) - stalled_at).total_seconds():.0f} s; stakes "
                    f"{inc.get('stakes')}; context {inc.get('context')}"
                )
                check(inc.get("trigger") == "no_progress", "trigger is no_progress")
            # The adapter reads for_tokens unconditionally; without it, it never answers.
            ok, _ = api.request(
                {"type": "resume_task_request", "for_task": task, "for_tokens": [str(token)]},
                "/tasks/resume_task",
            )
            check(ok, "resume accepted")
            if ok:
                pending_token = None
            check(
                wait_for(lambda: open_incident(task) is None, 40.0, 2.0),
                "incident resolves when the robot moves again",
            )
    finally:
        if task and pending_token is not None:
            # Cancelling a task that is still interrupted left the robot stuck (2026-10-27):
            # always resume first.
            TaskApi(ros.node, "ros").request(
                {"type": "resume_task_request", "for_task": task, "for_tokens": [pending_token]},
                "/tasks/resume_task",
            )
        if task:
            TaskApi(ros.node, "ros").request(
                {"type": "cancel_task_request", "task_id": task}, "/tasks/cancel_task"
            )
        ros.close()
        now_iso = iso(datetime.now(UTC))
        core(
            "POST",
            f"/api/v1/jobs/delivery_requests-{o2_id}/status",
            {"status": "Cancelled", "status_at": now_iso},
        )
        if any(j["job"]["urn"] == tr_urn for j in core("GET", "/api/v1/jobs")):
            core(
                "POST",
                f"/api/v1/jobs/transport_system-{tr_id}/status",
                {"status": "Cancelled", "status_at": now_iso},
            )
        say("cancelled the task and both jobs")
    check(
        wait_for(lambda: conflict_between(o2_urn, tr_urn) is None, 20.0, 1.0),
        "conflict clears once the jobs are cancelled",
    )
    ok = all(_ok)
    print("PASS forecast checks" if ok else "FAIL forecast checks")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
