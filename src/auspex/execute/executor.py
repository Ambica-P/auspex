"""Executor (spec 7, 10): an authorised plan becomes commands; results are ledgered and verified.

`execute_authorized` is the only way in, and it refuses anything but an `Authorization` minted
by the approval router for exactly this plan. It expands the plan's actions into rows of the
`commands` queue, each recorded in the ledger (COMMAND) in the same transaction:
- side `ros` (lanes, replans, Open-RMF task requests): carried out by the ROS bridge, which
  reads lane closures back from `/lane_states` and retries;
- side `core` (notify, advise_staff): carried out here; the people's notifications.
Commands of a plan run in order (`seq`); a command waits until the ones before it succeeded.
Time-bounded actions start at their window's start and get an inverse command at its end
(close -> open lanes; interrupt -> resume with the interrupt's token).

Every tick (1 s) the executor: runs due core commands; ledgers each finished command
(COMMAND_RESULT) and links tasks it submitted to their jobs; moves a plan whose commands all
succeeded to `verifying` and checks its post-conditions against live state (VERIFICATION);
on a failed command or post-condition it rolls back (inverses at once), marks the plan
failed and leaves it for a person (REVIEW).

Rules from live runs: never cancel an interrupted task (resume it first, with its token); a
resume must carry the interrupt's token. Auspex changes only tasks it owns (gate rule).
"""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import Connection, Engine, text

from auspex.core.canonical import canonical_json
from auspex.core.errors import AuspexError
from auspex.core.logging import get_logger
from auspex.core.models import (
    Action,
    AdviseStaff,
    CancelTask,
    Claim,
    CloseLanes,
    InterruptTask,
    Job,
    LanesReopened,
    Notify,
    OpenLanes,
    Plan,
    PostCondition,
    ReplanRobot,
    ResumeTask,
    SegmentClearFor,
    SubmitTask,
    TaskStateIs,
    TimeWindow,
)
from auspex.forecast.eta import RobotPath
from auspex.ledger.store import LedgerKind, append
from auspex.picture.jobs import link_task
from auspex.picture.ros_state import RosState, StateView
from auspex.policy.approval import Authorization
from auspex.policy.gate import GateInputs, JobFacts
from auspex.policy.loader import Policy
from auspex.site.model import Site
from auspex.site.navgraph import NavGraph
from auspex.verify.postconditions import Outcome, check

log = get_logger(__name__)

TICK_S = 1.0
VERIFY_TIMEOUT_S = 30.0
DEFAULT_CLOSURE_CHECK_S = 60.0
NOTIFICATIONS_KEPT = 200
CONDITION: TypeAdapter[PostCondition] = TypeAdapter(PostCondition)


class ExecutionRefused(AuspexError):
    code = "execution_refused"


@dataclass
class CommandSpec:
    action: str
    side: str
    payload: dict[str, Any]
    not_before: datetime | None = None
    inverse: CommandSpec | None = None


def _ms(t: datetime | None) -> int:
    return int(t.timestamp() * 1000) if t else 0


def _future(t: datetime | None, now: datetime) -> datetime | None:
    return t if t is not None and t > now else None


# --------------------------------------------------------------------------- ownership


def owned_tasks(conn: Connection, policy: Policy) -> frozenset[str]:
    """Tasks Auspex submitted, plus tasks of active jobs from sources that delegate to it."""
    submitted = conn.execute(
        text(
            "SELECT result->'state'->'booking'->>'id' AS task_id FROM commands"
            " WHERE action = 'submit_task' AND status = 'succeeded'"
        )
    ).all()
    delegated = conn.execute(
        text(
            "SELECT rmf_task_id AS task_id FROM jobs WHERE rmf_task_id IS NOT NULL"
            " AND source = ANY(:sources) AND status NOT IN ('completed', 'cancelled')"
        ),
        {"sources": list(policy.ownership.delegated_sources)},
    ).all()
    return frozenset(str(r.task_id) for r in [*submitted, *delegated] if r.task_id)


def gate_inputs(
    conn: Connection,
    policy: Policy,
    site: Site,
    graphs: Mapping[str, NavGraph],
    state: StateView,
    jobs: list[Job] | None = None,
    claims: list[Claim] | None = None,
) -> GateInputs:
    """What the gate needs to judge a plan: ownership, robots, waypoints, closures, the
    active jobs' classes and payloads, and the claims behind the plan's conflict."""
    if jobs is None:
        rows = conn.execute(
            text("SELECT data FROM jobs WHERE status NOT IN ('completed', 'cancelled')")
        ).all()
        jobs = [Job.model_validate(dict(r.data)["job"]) for r in rows]
    return make_gate_inputs(owned_tasks(conn, policy), site, graphs, state, jobs, claims)


def make_gate_inputs(
    owned: frozenset[str],
    site: Site,
    graphs: Mapping[str, NavGraph],
    state: StateView,
    jobs: list[Job],
    claims: list[Claim] | None = None,
) -> GateInputs:
    """Gate inputs from known facts (no database): the core's and the sandbox's."""
    closed = frozenset(
        seg.urn
        for seg in site.segments.values()
        for fleet, lanes in seg.rmf_lanes.items()
        if lanes and set(lanes) <= state.closed.get(fleet, set())
    )
    return GateInputs(
        owned_tasks=owned,
        robots={
            f: frozenset(str(r["name"]) for r in s.get("robots", []))
            for f, s in state.fleets.items()
        },
        waypoints={f: frozenset(v.name for v in g.vertices if v.name) for f, g in graphs.items()},
        closed_segments=closed,
        jobs={
            j.urn: JobFacts(
                precedence=j.precedence, payload=j.payload, destination_type=j.destination_type
            )
            for j in jobs
        },
        claims=tuple(claims or ()),
    )


def standing_closures(conn: Connection, segment: str) -> list[str]:
    """Fleets for which Auspex closed this segment without an end (no scheduled reopening)
    and has not reopened it since: what Auspex must reopen when the cause clears."""
    rows = conn.execute(
        text(
            "SELECT DISTINCT c.payload->>'fleet' AS fleet FROM commands c"
            " WHERE c.action = 'close_lanes' AND c.status = 'succeeded'"
            " AND c.inverse_of IS NULL AND c.payload->>'segment_urn' = :s"
            " AND NOT EXISTS (SELECT 1 FROM commands i WHERE i.inverse_of = c.command_id"
            "  AND i.status <> 'cancelled')"
            " AND NOT EXISTS (SELECT 1 FROM commands o WHERE o.action = 'open_lanes'"
            "  AND o.status = 'succeeded' AND o.payload->>'segment_urn' = :s"
            "  AND o.payload->>'fleet' = c.payload->>'fleet' AND o.updated_at > c.updated_at)"
        ),
        {"s": segment},
    ).all()
    return sorted(str(r.fleet) for r in rows)


def outstanding_tokens(conn: Connection, task_id: str) -> list[tuple[str, str]]:
    """(interrupt command id, token) of every interrupt of the task not yet released by a
    resume. Open-RMF resumes a task only when all its interruption tokens are released.
    A resume releases the interrupts it names (token_from) and the earlier interrupts whose
    tokens it carried; token values alone are not trusted to be unique."""
    interrupts = conn.execute(
        text(
            "SELECT command_id, result->>'token' AS token, updated_at FROM commands"
            " WHERE action = 'interrupt_task' AND status = 'succeeded'"
            " AND payload->>'task_id' = :t ORDER BY updated_at"
        ),
        {"t": task_id},
    ).all()
    if not interrupts:
        return []
    resumes = conn.execute(
        text(
            "SELECT payload->>'token_from' AS token_from,"
            " payload->'request'->'for_tokens' AS sent, result->'sent_tokens' AS filled,"
            " updated_at FROM commands WHERE action = 'resume_task' AND status = 'succeeded'"
            " AND payload->>'task_id' = :t"
        ),
        {"t": task_id},
    ).all()
    released: set[str] = set()
    for r in resumes:
        tokens = {str(x) for x in (r.sent or [])} | {str(x) for x in (r.filled or [])}
        for i in interrupts:
            if i.command_id == r.token_from or (
                i.updated_at < r.updated_at and str(i.token) in tokens
            ):
                released.add(str(i.command_id))
    return [
        (str(i.command_id), str(i.token))
        for i in interrupts
        if i.token is not None and str(i.command_id) not in released
    ]


def held_tasks(conn: Connection) -> frozenset[str]:
    """Tasks Auspex has interrupted (or is about to) and not yet resumed."""
    rows = conn.execute(
        text(
            "SELECT DISTINCT payload->>'task_id' AS task, status FROM commands"
            " WHERE action = 'interrupt_task' AND status IN ('pending', 'running', 'succeeded')"
        )
    ).all()
    out = {str(r.task) for r in rows if r.status in ("pending", "running")}
    for task in {str(r.task) for r in rows if r.status == "succeeded"} - out:
        if outstanding_tokens(conn, task):
            out.add(task)
    return frozenset(out)


# --------------------------------------------------------------------------- executor


class Executor:
    def __init__(
        self,
        *,
        engine: Engine,
        site: Site,
        policy: Policy,
        graphs: Mapping[str, NavGraph],
        ros: RosState,
        paths: Callable[[], Mapping[str, RobotPath]],
    ) -> None:
        self.engine = engine
        self.site = site
        self.policy = policy
        self.graphs = graphs
        self.ros = ros
        self.paths = paths
        self.notifications: deque[dict[str, Any]] = deque(maxlen=NOTIFICATIONS_KEPT)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="executor", daemon=True)

    # ------------------------------------------------------------------ entry point

    def execute_authorized(self, plan: Plan, auth: Authorization) -> list[str]:
        if not isinstance(auth, Authorization) or not auth.valid_for(plan):
            log.error("execute.refused", plan=plan.plan_id, reason="no valid authorization")
            raise ExecutionRefused("plan is not authorised", plan_id=plan.plan_id)
        try:
            with self.engine.begin() as conn:
                ids = self._enqueue(conn, plan)
        except ExecutionRefused as exc:
            self._fail_plan(plan.plan_id, f"could not expand: {exc.message}", rollback=False)
            raise
        log.info("execute.queued", plan=plan.plan_id, commands=ids, by=auth.approved_by)
        return ids

    def _enqueue(self, conn: Connection, plan: Plan) -> list[str]:
        now = datetime.now(UTC)
        ids: list[str] = []
        specs: list[CommandSpec] = []
        for action in plan.actions:
            specs += self._expand(conn, action, now)
        for seq, spec in enumerate(specs, start=1):
            cid = f"{plan.plan_id}-c{seq:02d}"
            self._insert(conn, plan.plan_id, cid, seq, spec, inverse_of=None)
            ids.append(cid)
            if spec.inverse is not None:
                inv = spec.inverse
                if inv.payload.get("token_from") == "@original":
                    inv.payload["token_from"] = cid
                self._insert(conn, plan.plan_id, f"{cid}-inv", 1000 + seq, inv, inverse_of=cid)
                ids.append(f"{cid}-inv")
        return ids

    def _insert(
        self,
        conn: Connection,
        plan_id: str,
        cid: str,
        seq: int,
        spec: CommandSpec,
        inverse_of: str | None,
    ) -> None:
        conn.execute(
            text(
                "INSERT INTO commands (command_id, plan_id, action, side, payload, not_before,"
                " seq, inverse_of) VALUES (:id, :plan, :action, :side, CAST(:payload AS jsonb),"
                " :nb, :seq, :inv)"
            ),
            {
                "id": cid,
                "plan": plan_id,
                "action": spec.action,
                "side": spec.side,
                "payload": json.dumps(spec.payload),
                "nb": spec.not_before,
                "seq": seq,
                "inv": inverse_of,
            },
        )
        append(
            conn,
            LedgerKind.COMMAND,
            {
                "command_id": cid,
                "plan_id": plan_id,
                "action": spec.action,
                "side": spec.side,
                "seq": seq,
                "payload": spec.payload,
                "not_before": spec.not_before,
                "inverse_of": inverse_of,
            },
        )

    def _expand(self, conn: Connection, action: Action, now: datetime) -> list[CommandSpec]:
        w: TimeWindow | None = action.window
        start = _future(w.start if w else None, now)
        if isinstance(action, (CloseLanes, OpenLanes)):
            lanes = self.site.segments[action.segment_urn].rmf_lanes[action.fleet_name]
            close = isinstance(action, CloseLanes)
            if close:
                self._supersede_reopen(conn, action.fleet_name, action.segment_urn, w)
            payload = {
                "fleet": action.fleet_name,
                "segment_urn": action.segment_urn,
                "lanes": sorted(lanes),
                "close": close,
            }
            inverse = (
                CommandSpec(
                    "open_lanes" if close else "close_lanes",
                    "ros",
                    {**payload, "close": not close},
                    not_before=w.end,
                )
                if w
                else None
            )
            return [CommandSpec(action.action, "ros", payload, start, inverse)]
        if isinstance(action, ReplanRobot):
            payload = {"fleet": action.fleet_name, "robot": action.robot_name}
            return [CommandSpec("replan_robot", "ros", payload, start)]
        if isinstance(action, SubmitTask):
            places = [p for p in (action.pickup_place, action.dropoff_place) if p]
            request: dict[str, Any] = {
                "category": "patrol",
                "description": {"places": places, "rounds": 1},
                "unix_millis_earliest_start_time": _ms(action.earliest_start),
                "unix_millis_request_time": _ms(now),
                "requester": self.policy.ownership.requester,
            }
            if action.fleet_name:
                request["fleet_name"] = action.fleet_name
            specs = [
                CommandSpec(
                    "submit_task",
                    "ros",
                    {
                        "job_urn": action.job_urn,
                        "request": {"type": "dispatch_task_request", "request": request},
                    },
                    start,
                )
            ]
            if action.replaces_task_id:
                specs += self._cancel_specs(conn, action.replaces_task_id, None)
            return specs
        if isinstance(action, CancelTask):
            return self._cancel_specs(conn, action.task_id, start)
        if isinstance(action, InterruptTask):
            t = action.task_id
            inverse = (
                CommandSpec(
                    "resume_task",
                    "ros",
                    {
                        "task_id": t,
                        "token_from": "@original",
                        "request": {"type": "resume_task_request", "for_task": t},
                    },
                    not_before=w.end,
                )
                if w
                else None
            )
            payload = {"task_id": t, "request": {"type": "interrupt_task_request", "task_id": t}}
            return [CommandSpec("interrupt_task", "ros", payload, start, inverse)]
        if isinstance(action, ResumeTask):
            spec = self._resume_spec(conn, action.task_id, start)
            if spec is None:
                raise ExecutionRefused("no interrupt to resume", task_id=action.task_id)
            return [spec]
        if isinstance(action, (Notify, AdviseStaff)):
            return [CommandSpec(action.action, "core", action.model_dump(mode="json"), start)]
        raise ExecutionRefused("action not supported by the executor", action=action.action)

    def _supersede_reopen(
        self, conn: Connection, fleet: str, segment: str, w: TimeWindow | None
    ) -> None:
        """A new closure of a segment cancels earlier scheduled reopenings that would end it
        too soon (all of them when the new closure has no end)."""
        rows = conn.execute(
            text(
                "UPDATE commands SET status = 'cancelled', updated_at = now(),"
                " result = CAST(:res AS jsonb)"
                " WHERE action = 'open_lanes' AND status = 'pending' AND inverse_of IS NOT NULL"
                " AND payload->>'fleet' = :f AND payload->>'segment_urn' = :s"
                " AND (CAST(:end AS timestamptz) IS NULL OR not_before < :end)"
                " RETURNING command_id"
            ),
            {
                "f": fleet,
                "s": segment,
                "end": w.end if w else None,
                "res": json.dumps({"superseded": "a later closure of the same segment"}),
            },
        ).all()
        if rows:
            log.info(
                "execute.reopen_superseded",
                fleet=fleet,
                segment=segment,
                commands=[r.command_id for r in rows],
            )

    def _resume_spec(
        self, conn: Connection, task_id: str, start: datetime | None
    ) -> CommandSpec | None:
        found = outstanding_tokens(conn, task_id)
        if not found:
            return None
        # A resume now supersedes any scheduled one (the interrupts' inverses).
        conn.execute(
            text(
                "UPDATE commands SET status = 'cancelled', updated_at = now()"
                " WHERE action = 'resume_task' AND status = 'pending'"
                " AND inverse_of = ANY(:ids)"
            ),
            {"ids": [cid for cid, _ in found]},
        )
        payload = {
            "task_id": task_id,
            "request": {
                "type": "resume_task_request",
                "for_task": task_id,
                "for_tokens": [token for _, token in found],
            },
        }
        return CommandSpec("resume_task", "ros", payload, start)

    def _cancel_specs(
        self, conn: Connection, task_id: str, start: datetime | None
    ) -> list[CommandSpec]:
        specs: list[CommandSpec] = []
        resume = self._resume_spec(conn, task_id, start)
        if resume is not None:  # never cancel an interrupted task: resume it first
            specs.append(resume)
        request = {"type": "cancel_task_request", "task_id": task_id}
        payload = {"task_id": task_id, "request": request}
        specs.append(CommandSpec("cancel_task", "ros", payload, None if specs else start))
        return specs

    # ------------------------------------------------------------------ loop

    def start(self) -> None:
        self._thread.start()
        log.info("executor.started", tick_s=TICK_S)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick(datetime.now(UTC))
            except Exception:  # keep the loop alive; the traceback is in the log
                log.exception("executor.tick_failed")
            self._stop.wait(TICK_S)

    def tick(self, now: datetime) -> None:
        self._run_core_commands()
        self._ledger_results()
        self._advance_plans(now)

    # ------------------------------------------------------------------ core commands

    def _run_core_commands(self) -> None:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT c.command_id, c.plan_id, c.action, c.payload FROM commands c"
                    " WHERE c.side = 'core' AND c.status = 'pending'"
                    " AND (c.not_before IS NULL OR c.not_before <= now())"
                    " AND NOT EXISTS (SELECT 1 FROM commands p WHERE p.plan_id = c.plan_id"
                    " AND p.inverse_of IS NULL AND p.seq < c.seq AND p.status <> 'succeeded')"
                    " ORDER BY c.seq FOR UPDATE SKIP LOCKED"
                )
            ).all()
            for r in rows:
                note = {
                    "at": datetime.now(UTC).isoformat(),
                    "plan_id": r.plan_id,
                    "kind": r.action,
                    **dict(r.payload),
                }
                self.notifications.append(note)
                conn.execute(
                    text(
                        "UPDATE commands SET status = 'succeeded', attempts = attempts + 1,"
                        " result = CAST(:res AS jsonb), updated_at = now()"
                        " WHERE command_id = :id"
                    ),
                    {"id": r.command_id, "res": json.dumps({"delivered_to": "notifications"})},
                )
                log.info(
                    "notify.sent",
                    plan=r.plan_id,
                    kind=r.action,
                    recipient=dict(r.payload).get("recipient_urn"),
                )

    # ------------------------------------------------------------------ results

    def _ledger_results(self) -> None:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT command_id, plan_id, action, status, payload, result, attempts"
                    " FROM commands WHERE status IN ('succeeded', 'failed', 'cancelled')"
                    " AND NOT ledgered ORDER BY updated_at FOR UPDATE SKIP LOCKED"
                )
            ).all()
            for r in rows:
                result = dict(r.result) if r.result else {}
                append(
                    conn,
                    LedgerKind.COMMAND_RESULT,
                    {
                        "command_id": r.command_id,
                        "plan_id": r.plan_id,
                        "action": r.action,
                        "status": r.status,
                        "attempts": int(r.attempts),
                        "result": result,
                    },
                )
                conn.execute(
                    text("UPDATE commands SET ledgered = true WHERE command_id = :id"),
                    {"id": r.command_id},
                )
                log_fn = log.info if r.status != "failed" else log.error
                log_fn(
                    "command.finished",
                    command=r.command_id,
                    action=r.action,
                    status=r.status,
                    attempts=int(r.attempts),
                    error=result.get("error"),
                )
                if r.action == "submit_task" and r.status == "succeeded":
                    booking = ((result.get("state") or {}).get("booking") or {}).get("id")
                    job_urn = dict(r.payload).get("job_urn")
                    if booking and job_urn:
                        link_task(conn, str(job_urn), str(booking), by=f"plan:{r.plan_id}")

    # ------------------------------------------------------------------ plans

    def _advance_plans(self, now: datetime) -> None:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT plan_id, status, plan, verification FROM plans"
                    " WHERE status IN ('executing', 'verifying')"
                )
            ).all()
        for r in rows:
            plan = Plan.model_validate(dict(r.plan))
            if r.status == "executing":
                self._check_commands(plan, now)
            else:
                self._verify(plan, dict(r.verification or {}), now)

    def _check_commands(self, plan: Plan, now: datetime) -> None:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT command_id, status, ledgered, result FROM commands"
                    " WHERE plan_id = :p AND inverse_of IS NULL"
                ),
                {"p": plan.plan_id},
            ).all()
        failed = [r for r in rows if r.status in ("failed", "cancelled")]
        if failed:
            first = failed[0]
            err = (dict(first.result or {})).get("error", first.status)
            self._fail_plan(plan.plan_id, f"command {first.command_id} {first.status}: {err}")
            return
        if all(r.status == "succeeded" and r.ledgered for r in rows):
            conditions = [*plan.expected, *self._implicit(plan, now)]
            deadline = max(
                [now + timedelta(seconds=VERIFY_TIMEOUT_S)]
                + [c.by for c in conditions if isinstance(c, TaskStateIs)]
            )
            verification = {
                "started_at": now.isoformat(),
                "deadline": deadline.isoformat(),
                "conditions": [json.loads(canonical_json(c)) for c in conditions],
            }
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE plans SET status = 'verifying', verification ="
                        " CAST(:v AS jsonb), updated_at = now() WHERE plan_id = :p"
                    ),
                    {"p": plan.plan_id, "v": json.dumps(verification)},
                )
            log.info("plan.verifying", plan=plan.plan_id, conditions=len(conditions))

    def _implicit(self, plan: Plan, now: datetime) -> list[PostCondition]:
        out: list[PostCondition] = []
        for a in plan.actions:
            if isinstance(a, CloseLanes):
                window = a.window or TimeWindow(
                    start=now, end=now + timedelta(seconds=DEFAULT_CLOSURE_CHECK_S)
                )
                out.append(
                    SegmentClearFor(
                        segment_urn=a.segment_urn, fleet_name=a.fleet_name, window=window
                    )
                )
            elif isinstance(a, OpenLanes):
                out.append(LanesReopened(segment_urn=a.segment_urn, fleet_name=a.fleet_name))
        return out

    def _verify(self, plan: Plan, verification: dict[str, Any], now: datetime) -> None:
        conditions = [CONDITION.validate_python(c) for c in verification.get("conditions", [])]
        state = self.ros.state()
        paths = self.paths()
        results = [
            (c, check(c, site=self.site, graphs=self.graphs, state=state, paths=paths, now=now))
            for c in conditions
        ]
        deadline = datetime.fromisoformat(str(verification["deadline"]))
        report = [
            {"condition": c.kind, "outcome": res.outcome.value, "detail": res.detail}
            for c, res in results
        ]
        if all(res.outcome is Outcome.PASS for _, res in results):
            record = {"plan_id": plan.plan_id, "passed": True, "checks": report, "at": now}
            with self.engine.begin() as conn:
                self._set_plan(conn, plan.plan_id, "completed", {**verification, **record})
                append(conn, LedgerKind.VERIFICATION, record)
            log.info("plan.verified", plan=plan.plan_id, checks=len(report))
            return
        if now >= deadline:
            failing = "; ".join(r["detail"] for r in report if r["outcome"] != "pass")
            self._fail_plan(plan.plan_id, f"post-conditions failed: {failing}", checks=report)

    # ------------------------------------------------------------------ failure

    def _fail_plan(
        self,
        plan_id: str,
        reason: str,
        *,
        rollback: bool = True,
        checks: list[dict[str, Any]] | None = None,
    ) -> None:
        inverses: list[str] = []
        with self.engine.begin() as conn:
            if rollback:
                inverses = self._rollback(conn, plan_id)
            record = {
                "plan_id": plan_id,
                "passed": False,
                "reason": reason[:500],
                "checks": checks or [],
                "rolled_back": inverses,
                "next": "review",
                "at": datetime.now(UTC),
            }
            self._set_plan(conn, plan_id, "failed", record)
            append(conn, LedgerKind.VERIFICATION, record)
        log.error("plan.failed", plan=plan_id, reason=reason[:300], rolled_back=inverses)

    def _rollback(self, conn: Connection, plan_id: str) -> list[str]:
        """Cancel the plan's pending steps; run inverses of the steps that took effect now."""
        conn.execute(
            text(
                "UPDATE commands SET status = 'cancelled', updated_at = now()"
                " WHERE plan_id = :p AND status = 'pending' AND inverse_of IS NULL"
            ),
            {"p": plan_id},
        )
        rows = conn.execute(
            text(
                "SELECT c.command_id, c.action, c.payload,"
                " (SELECT i.command_id FROM commands i WHERE i.inverse_of = c.command_id"
                "  AND i.status = 'pending') AS pending_inverse,"
                " EXISTS (SELECT 1 FROM commands i WHERE i.inverse_of = c.command_id)"
                "  AS has_inverse"
                " FROM commands c WHERE c.plan_id = :p AND c.inverse_of IS NULL"
                " AND c.status = 'succeeded' ORDER BY c.seq DESC"
            ),
            {"p": plan_id},
        ).all()
        done: list[str] = []
        next_seq = 2000
        for r in rows:
            if r.pending_inverse:
                conn.execute(
                    text(
                        "UPDATE commands SET not_before = now(), updated_at = now()"
                        " WHERE command_id = :id"
                    ),
                    {"id": r.pending_inverse},
                )
                done.append(str(r.pending_inverse))
                continue
            if r.has_inverse:
                continue
            payload = dict(r.payload)
            spec: CommandSpec | None = None
            if r.action in ("close_lanes", "open_lanes"):
                close = not bool(payload["close"])
                spec = CommandSpec(
                    "close_lanes" if close else "open_lanes", "ros", {**payload, "close": close}
                )
            elif r.action == "interrupt_task":
                t = str(payload["task_id"])
                spec = CommandSpec(
                    "resume_task",
                    "ros",
                    {
                        "task_id": t,
                        "token_from": r.command_id,
                        "request": {"type": "resume_task_request", "for_task": t},
                    },
                )
            if spec is not None:
                next_seq += 1
                cid = f"{r.command_id}-undo"
                self._insert(conn, plan_id, cid, next_seq, spec, inverse_of=str(r.command_id))
                done.append(cid)
        return done

    @staticmethod
    def _set_plan(
        conn: Connection, plan_id: str, status: str, verification: dict[str, Any]
    ) -> None:
        conn.execute(
            text(
                "UPDATE plans SET status = :s, verification = CAST(:v AS jsonb),"
                " updated_at = now() WHERE plan_id = :p"
            ),
            {"p": plan_id, "s": status, "v": json.dumps(verification, default=str)},
        )
