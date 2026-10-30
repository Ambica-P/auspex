"""M7 live check (scripts/check-actuation): plans through gate, approval, commands, verification.

Needs the sim, the bridge and the core running, robots idle. Uses the deliveryRobot fleet:
1. close lift_25 for 40 s + replan a robot + notify: REVIEW, nothing queued before approval;
   after approval the lanes read back closed, the plan verifies, and the inverse reopens them;
2. a rejected plan queues nothing;
3. submit a task (Auspex owns it), wait until a robot executes it;
4. interrupt it for 15 s: resumed automatically with the interrupt's token;
5. interrupt it again (120 s) and cancel it while held: the executor resumes first, then
   cancels; the scheduled resume is dropped; the task ends;
6. cancelling a task Auspex does not own is BLOCKED;
7. every step is in the ledger and the chain verifies.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from auspex.core.config import get_settings
from auspex.core.errors import ConfigError

FLEET = "deliveryRobot"
PICKUP, DROPOFF = "L2_north_counter", "L1_left_treatment_1"
_results: list[bool] = []


def check(ok: bool, msg: str) -> bool:
    _results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'} {msg}", flush=True)
    return ok


def info(msg: str) -> None:
    print(f"INFO {msg}", flush=True)


def wait_for(pred: Callable[[], bool], timeout: float, period: float = 1.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(period)
    return pred()


def window(start_in_s: float, length_s: float) -> dict[str, str]:
    start = datetime.now(UTC) + timedelta(seconds=start_in_s)
    return {"start": start.isoformat(), "end": (start + timedelta(seconds=length_s)).isoformat()}


class Core:
    def __init__(self, client: httpx.Client, tag: str) -> None:
        self.c = client
        self.tag = tag
        self.plan_ids: list[str] = []

    def health(self) -> dict[str, Any]:
        r = self.c.get("/api/v1/health")
        r.raise_for_status()
        return dict(r.json())

    def submit(
        self, name: str, actions: list[dict[str, Any]], expected: list[dict[str, Any]] | None = None
    ) -> tuple[str, dict[str, Any]]:
        pid = f"check-{self.tag}-{name}"
        body = {
            "plan_id": pid,
            "producer": "operator",
            "actions": actions,
            "expected": expected or [],
            "rationale": f"scripts/check-actuation step {name}",
        }
        r = self.c.post("/api/v1/plans", json=body)
        if r.status_code != 201:
            print(f"FAIL submit {pid}: HTTP {r.status_code} {r.text[:300]}")
            sys.exit(1)
        self.plan_ids.append(pid)
        out = dict(r.json())
        d = out["decision"]
        info(f"plan {pid}: {d['verdict']} {d['rule_ids']} risk {d['risk']} -> {out['status']}")
        return pid, out

    def plan(self, pid: str) -> dict[str, Any]:
        r = self.c.get(f"/api/v1/plans/{pid}")
        r.raise_for_status()
        return dict(r.json())

    def approve(self, pid: str) -> None:
        r = self.c.post(f"/api/v1/plans/{pid}/approve", json={"approver": "check-actuation"})
        if r.status_code != 200:
            print(f"FAIL approve {pid}: HTTP {r.status_code} {r.text[:300]}")
            sys.exit(1)

    def reject(self, pid: str) -> None:
        r = self.c.post(
            f"/api/v1/plans/{pid}/reject", json={"approver": "check-actuation", "reason": "test"}
        )
        r.raise_for_status()

    def finish(self, pid: str, timeout: float) -> dict[str, Any]:
        done: dict[str, Any] = {}

        def finished() -> bool:
            done.update(self.plan(pid))
            return done.get("status") in ("completed", "failed")

        wait_for(finished, timeout)
        for c in done.get("commands", []):
            res = c.get("result") or {}
            brief = {k: res[k] for k in ("readback", "lane_attempts", "token", "error") if k in res}
            info(f"  {c['command_id']} {c['action']}: {c['status']} {brief}")
        v = done.get("verification") or {}
        for chk in v.get("checks", []):
            info(f"  post-condition {chk['condition']}: {chk['outcome']} ({chk['detail']})")
        if v.get("reason"):
            info(f"  failure: {v['reason']}")
        return done

    def command(self, pid: str, suffix: str) -> dict[str, Any]:
        for c in self.plan(pid)["commands"]:
            if str(c["command_id"]).endswith(suffix):
                return dict(c)
        return {}


def robot_with(core: Core, task: str) -> str | None:
    for robot, t in core.health()["ros"].get("robot_tasks", {}).items():
        if t == task:
            return str(robot)
    return None


def main() -> int:
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"FAIL configuration: {exc}")
        return 1
    url = f"http://{settings.core_host}:{settings.core_port}"
    headers = {"Authorization": f"Bearer {settings.admin_token.get_secret_value()}"}
    tag = datetime.now(UTC).strftime("%H%M%S")
    with httpx.Client(base_url=url, headers=headers, timeout=10.0) as client:
        try:
            core = Core(client, tag)
            health = core.health()
        except httpx.HTTPError as exc:
            print(f"FAIL core not reachable at {url}: {exc} (scripts/run-core?)")
            return 1
        robots = health["ros"]["fleets"].get(FLEET, [])
        if not check(bool(robots), f"core sees {FLEET} robots {robots} (bridge running?)"):
            return 1
        robot = robots[0]
        lane_steps(core, client, robot)
        task_steps(core, tag)
        return finish(core, client)


def lane_steps(core: Core, client: httpx.Client, robot: str) -> None:
    # 1. timed closure + replan + notify
    pid, out = core.submit(
        "close",
        [
            {
                "action": "close_lanes",
                "fleet_name": FLEET,
                "segment_urn": "segment/lift_25",
                "window": window(0, 40),
            },
            {"action": "replan_robot", "fleet_name": FLEET, "robot_name": robot},
            {
                "action": "notify",
                "recipient_urn": "staff/porter-desk",
                "message": "lift_25 kept clear of robots for 40 s (check-actuation)",
            },
        ],
    )
    check(
        out["decision"]["approval_route"] == "person" and out["status"] == "awaiting_approval",
        "operator's closure plan waits for a person",
    )
    check(not core.plan(pid)["commands"], "nothing queued before approval")
    core.approve(pid)
    p = core.finish(pid, 60)
    check(p["status"] == "completed", f"closure plan executed and verified ({p['status']})")
    close = core.command(pid, "-c01")
    lanes = set((close.get("payload") or {}).get("lanes", []))
    readback = set((close.get("result") or {}).get("readback", []))
    check(
        bool(lanes) and lanes <= readback,
        f"lift_25 lanes {sorted(lanes)} read back closed on /lane_states",
    )
    notes = client.get("/api/v1/notifications").json()
    check(any(n.get("plan_id") == pid for n in notes), "porter desk notified")
    info("waiting for the closure window to end (≈40 s) ...")
    inverse_ok = wait_for(
        lambda: core.command(pid, "-c01-inv").get("status") in ("succeeded", "failed"), 75
    )
    inv = core.command(pid, "-c01-inv")
    check(inverse_ok and inv.get("status") == "succeeded", "inverse reopened lift_25 on time")
    closed_now = set(core.health()["ros"]["closed_lanes"].get(FLEET, []))
    check(not (lanes & closed_now), f"core sees lift_25 open again (closed now {closed_now})")

    # 2. rejected plan
    pid, _ = core.submit(
        "reject",
        [{"action": "close_lanes", "fleet_name": FLEET, "segment_urn": "segment/lift_1"}],
    )
    core.reject(pid)
    p = core.plan(pid)
    check(p["status"] == "rejected" and not p["commands"], "rejected plan queued nothing")


def task_steps(core: Core, tag: str) -> None:
    # 3. submit an owned task
    pid, _ = core.submit(
        "submit",
        [
            {
                "action": "submit_task",
                "job_urn": f"job/check-actuation-{tag}",
                "pickup_place": PICKUP,
                "dropoff_place": DROPOFF,
                "fleet_name": FLEET,
            }
        ],
    )
    core.approve(pid)
    p = core.finish(pid, 30)
    res = core.command(pid, "-c01").get("result") or {}
    task = ((res.get("state") or {}).get("booking") or {}).get("id")
    if not check(p["status"] == "completed" and bool(task), f"task submitted ({task})"):
        return
    info(f"waiting for a {FLEET} robot to start {task} ...")
    if not check(
        wait_for(lambda: robot_with(core, str(task)) is not None, 120, 2.0),
        f"{robot_with(core, str(task))} executes {task}",
    ):
        return

    # 4. interrupt for 15 s, resumed automatically with the token
    now = datetime.now(UTC)
    pid, _ = core.submit(
        "hold15",
        [{"action": "interrupt_task", "task_id": task, "window": window(0, 15)}],
        [
            {
                "kind": "task_state",
                "task_id": task,
                "states": ["underway"],
                "by": (now + timedelta(seconds=10)).isoformat(),
            }
        ],
    )
    core.approve(pid)
    p = core.finish(pid, 40)
    token = (core.command(pid, "-c01").get("result") or {}).get("token")
    check(p["status"] == "completed" and token is not None, f"robot held (token {token!r})")
    wait_for(lambda: core.command(pid, "-c01-inv").get("status") in ("succeeded", "failed"), 45)
    inv = core.command(pid, "-c01-inv")
    sent = ((inv.get("payload") or {}).get("request") or {}).get("for_task")
    check(
        inv.get("status") == "succeeded" and sent == task,
        "resumed automatically at the window's end, with the interrupt's token",
    )

    # 5. hold for 120 s, cancel while held: resume first, then cancel
    pid_hold, _ = core.submit(
        "hold120", [{"action": "interrupt_task", "task_id": task, "window": window(0, 120)}]
    )
    core.approve(pid_hold)
    p = core.finish(pid_hold, 40)
    check(p["status"] == "completed", "robot held again (120 s window)")
    pid, _ = core.submit(
        "cancel",
        [{"action": "cancel_task", "task_id": task}],
        [
            {
                "kind": "task_state",
                "task_id": task,
                "states": ["canceled"],
                "by": (datetime.now(UTC) + timedelta(seconds=40)).isoformat(),
            }
        ],
    )
    core.approve(pid)
    p = core.finish(pid, 70)
    actions = [c["action"] for c in p["commands"]]
    check(actions == ["resume_task", "cancel_task"], f"resume before cancel ({actions})")
    check(p["status"] == "completed", f"task cancelled and no longer running ({p['status']})")
    check(
        core.command(pid_hold, "-c01-inv").get("status") == "cancelled",
        "the scheduled resume was dropped",
    )

    # 6. not ours
    pid, out = core.submit(
        "notours", [{"action": "cancel_task", "task_id": "patrol.dispatch-not-ours"}]
    )
    check(
        out["decision"]["verdict"] == "BLOCK"
        and "owned-tasks-only" in out["decision"]["rule_ids"]
        and not core.plan(pid)["commands"],
        "cancelling a task Auspex does not own is BLOCKED",
    )
    return


def finish(core: Core, client: httpx.Client) -> int:
    v = client.get("/api/v1/ledger/verify").json()
    check(bool(v.get("ok")), f"ledger chain verifies ({v})")
    entries = client.get("/api/v1/ledger", params={"limit": 1000}).json()
    mine = set(core.plan_ids)
    kinds: dict[str, int] = {}
    for e in entries:
        p = e.get("payload") or {}
        plan_id = p.get("plan_id") or (p.get("plan") or {}).get("plan_id")
        plan_id = plan_id or (p.get("decision") or {}).get("plan_id")
        if plan_id in mine:
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    info(f"ledger entries for this check: {kinds}")
    check(
        all(
            kinds.get(k, 0) > 0
            for k in (
                "plan",
                "gate_decision",
                "approval",
                "command",
                "command_result",
                "verification",
            )
        ),
        "plan, gate decision, approval, commands, results and verifications are in the ledger",
    )
    failed = _results.count(False)
    print(
        f"{'PASS' if not failed else 'FAIL'} check-actuation: {len(_results) - failed} passed,"
        f" {failed} failed"
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
