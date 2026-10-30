"""Spec 18.4 test 1: no code path reaches the adapter without the gate's ALLOW and approval.

- The executor refuses a plan without an Authorization minted by the approval router for
  exactly that plan (missing, forged, for another plan, or for a changed plan).
- Statically: only the approval router calls `execute_authorized` or makes an Authorization,
  and only the executor writes commands (the queue the ROS bridge carries out).
- Against the test database: a BLOCKED plan and a plan awaiting review queue no commands; a
  rejected plan never does; an approved plan does, each command recorded in the ledger.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import Engine, text

from auspex.core.errors import SiteError
from auspex.core.models import Plan, PrecedenceClass
from auspex.execute.executor import ExecutionRefused, Executor
from auspex.ledger.store import verify_chain
from auspex.picture.ros_state import RosState
from auspex.policy.approval import Approvals, Authorization, _mint
from auspex.policy.gate import GateInputs, JobFacts, evaluate
from auspex.policy.loader import load_policy
from auspex.site.model import DEFAULT_SITE_DIR, build_site
from auspex.site.navgraph import load_captures

SRC = Path(__file__).resolve().parents[1] / "src" / "auspex"


def _plan(plan_id: str, actions: list[dict[str, Any]]) -> Plan:
    return Plan.model_validate(
        {"plan_id": plan_id, "producer": "operator", "actions": actions, "rationale": "test"}
    )


def _close_lift_25(plan_id: str) -> Plan:
    now = datetime.now(UTC)
    return _plan(
        plan_id,
        [
            {
                "action": "close_lanes",
                "fleet_name": "deliveryRobot",
                "segment_urn": "segment/lift_25",
                "window": {
                    "start": now.isoformat(),
                    "end": (now + timedelta(seconds=60)).isoformat(),
                },
            },
            {
                "action": "notify",
                "recipient_urn": "staff/porter-desk",
                "message": "lift_25 reserved for a bariatric transport",
            },
        ],
    )


def _offline_executor() -> Executor:
    # Refusal happens before anything is touched: no database, site or ROS state needed.
    return Executor(
        engine=cast(Engine, None),
        site=cast(Any, None),
        policy=load_policy(),
        graphs={},
        ros=RosState({}),
        paths=dict,
    )


# --------------------------------------------------------------------------- authorization


def test_executor_refuses_without_valid_authorization() -> None:
    executor = _offline_executor()
    plan = _close_lift_25("plan-t1-a")
    other = _close_lift_25("plan-t1-b")
    good = _mint(plan, "person:test")
    forged = dataclasses.replace(good, key=object())
    for_other = _mint(other, "person:test")
    changed = plan.model_copy(update={"rationale": "changed after approval"})
    for auth in (None, forged, for_other):
        with pytest.raises(ExecutionRefused):
            executor.execute_authorized(plan, cast(Authorization, auth))
    with pytest.raises(ExecutionRefused):
        executor.execute_authorized(changed, good)


# --------------------------------------------------------------------------- static paths


def _calls(tree: ast.AST, name: str) -> int:
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Attribute) and f.attr == name) or (
                isinstance(f, ast.Name) and f.id == name
            ):
                n += 1
    return n


def test_only_the_approval_router_reaches_the_executor() -> None:
    offenders: dict[str, list[str]] = {
        "execute_authorized": [],
        "Authorization": [],
        "_mint": [],
        "INSERT INTO commands": [],
    }
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for name in ("execute_authorized", "Authorization", "_mint"):
            if _calls(tree, name) and rel != "policy/approval.py":
                offenders[name].append(rel)
        if "INSERT INTO commands" in source and rel != "execute/executor.py":
            offenders["INSERT INTO commands"].append(rel)
    assert offenders == {k: [] for k in offenders}


# --------------------------------------------------------------------------- end to end (DB)


def _commands(engine: Engine, plan_id: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM commands WHERE plan_id = :p"), {"p": plan_id}
            ).scalar_one()
        )


def _status(engine: Engine, plan_id: str) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT status FROM plans WHERE plan_id = :p"), {"p": plan_id}
            ).scalar_one()
        )


def test_gate_and_approval_guard_the_command_queue(test_db: Any) -> None:
    engine: Engine = test_db.app
    try:
        graphs = load_captures(DEFAULT_SITE_DIR / "nav_graphs")
    except SiteError as exc:
        pytest.skip(f"needs the site's nav graph captures: {exc}")
    site = build_site(DEFAULT_SITE_DIR, graphs)
    policy = load_policy()
    executor = Executor(
        engine=engine,
        site=site,
        policy=policy,
        graphs=graphs,
        ros=RosState(graphs),
        paths=dict,
    )
    approvals = Approvals(engine=engine, site=site, policy=policy, executor=executor, timeout_s=60)
    inputs = GateInputs(owned_tasks=frozenset({"owned-task-1"}), robots={})
    suffix = datetime.now(UTC).strftime("%H%M%S%f")

    # BLOCK: cancelling a task Auspex does not own.
    blocked = _plan(f"plan-blk-{suffix}", [{"action": "cancel_task", "task_id": "someone-else"}])
    decision = approvals.submit(blocked, inputs)
    assert decision.verdict.value == "BLOCK"
    assert "owned-tasks-only" in decision.rule_ids
    assert _status(engine, blocked.plan_id) == "blocked"
    assert _commands(engine, blocked.plan_id) == 0

    # BLOCK: an action this build cannot carry out.
    hold = _plan(
        f"plan-hold-{suffix}",
        [
            {
                "action": "hold_at_waypoint",
                "fleet_name": "deliveryRobot",
                "robot_name": "deliveryRobot_1",
                "waypoint": "L1_sub_waiting_area_1",
            }
        ],
    )
    assert approvals.submit(hold, inputs).verdict.value == "BLOCK"
    assert _commands(engine, hold.plan_id) == 0

    # An operator's plan the gate allows still waits for a person; rejected: never queued.
    rejected = _close_lift_25(f"plan-rej-{suffix}")
    d = approvals.submit(rejected, inputs)
    assert (d.verdict.value, d.approval_route.value) == ("ALLOW", "person")
    assert _status(engine, rejected.plan_id) == "awaiting_approval"
    approvals.reject(rejected.plan_id, "tester", "not now")
    assert _status(engine, rejected.plan_id) == "rejected"
    assert _commands(engine, rejected.plan_id) == 0

    # Waiting for a person, then approved: queued (close, its inverse, notify), all ledgered.
    approved = _close_lift_25(f"plan-ok-{suffix}")
    approvals.submit(approved, inputs)
    assert _commands(engine, approved.plan_id) == 0
    approvals.approve(approved.plan_id, "tester")
    assert _status(engine, approved.plan_id) == "executing"
    assert _commands(engine, approved.plan_id) == 3
    with engine.connect() as conn:
        ledgered = conn.execute(
            text("SELECT count(*) FROM ledger WHERE kind = 'command' AND payload->>'plan_id' = :p"),
            {"p": approved.plan_id},
        ).scalar_one()
    assert ledgered == 3
    verify_chain(engine)


# --------------------------------------------------------------------------- hospital rules


def test_automatic_plans_that_delay_a_stat_transport_are_blocked() -> None:
    """A solver plan would run without a person; the gate must stop it if it delays a STAT
    transport (never-delay-code-or-stat), and send a same-class ordering to a person."""
    policy = load_policy()
    stat = JobFacts(precedence=PrecedenceClass.STAT)
    inputs = GateInputs(
        owned_tasks=frozenset(),
        robots={},
        jobs={"job/t-1": stat, "job/t-2": stat, "job/o2": JobFacts(PrecedenceClass.CRITICAL)},
    )

    def solver(plan_id: str, effects: list[dict[str, Any]]) -> Plan:
        return Plan.model_validate(
            {
                "plan_id": plan_id,
                "conflict_id": "cf-test",
                "producer": "solver",
                "actions": [{"action": "notify", "recipient_urn": "staff/alex", "message": "test"}],
                "predicted": effects,
            }
        )

    def effect(urn: str, delay: float) -> dict[str, Any]:
        return {
            "job_urn": urn,
            "eta": None,
            "lateness_s": 0.0,
            "delay_to_humans_s": delay,
            "interventions": 0,
        }

    site = cast(Any, None)  # no lane or task actions: the site is not consulted
    d = evaluate(solver("p-delay", [effect("job/t-1", 90.0)]), site, policy, inputs)
    assert d.verdict.value == "BLOCK" and "never-delay-code-or-stat" in d.rule_ids
    ok = evaluate(solver("p-fine", [effect("job/o2", 0.0)]), site, policy, inputs)
    assert (ok.verdict.value, ok.approval_route.value) == ("ALLOW", "automatic")


def test_the_agent_cannot_reach_the_executor() -> None:
    """The agent package imports nothing that can act: no executor, approval or gate."""
    forbidden = ("auspex.execute", "auspex.policy.approval", "auspex.api")
    offenders = []
    for path in (SRC / "agent").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            offenders += [f"{path.name}: {n}" for n in names if n.startswith(forbidden)]
    assert offenders == []
