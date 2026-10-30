"""Decision engine (spec 6): turns conflicts, incidents and facility events into plans.

Configurations (spec 15), switchable at run time (PUT /api/v1/config):
- A: Auspex observes only. No plans.
- B: deterministic: fast-lane playbooks, then the evaluator's best gate-passing option
  (producer `solver`). Fields only Nemotron could interpret stay unknown.
- C: B plus the Nemotron agent: interprets free-text jobs (S1); on a lift fault with a
  robot delivery at stake, holds the fast lane back for that fleet and lets the agent weigh
  waiting, rerouting and handover with vendor evidence (S2); when no option gets every
  deadline met, asks for composite options (S3); writes incident reports. If the agent fails
  or runs out of time, the deterministic B response is applied instead (fail closed).

Triggers arrive on a queue (from the picture loop and the event consumer) and are handled
in one thread, in order. The fast lane is budgeted (FAST_LANE_BUDGET_MS from the trigger to
the plan's submission); an overrun is logged as an error. For every conflict involving a
robot, the evaluator's scored options are recorded in the ledger (OPTIONS), whoever acts.
Conflicts between two people are recorded too: Auspex has no lever over people and never
orders them; a clinical tie goes to a person. The agent runs on its own event loop thread,
each decision within AGENT_BUDGET_S.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import threading
import time
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, TypeVar

from sqlalchemy import Engine, text

from auspex.agent.agent import AgentFailed, Trace
from auspex.agent.flows import AgentFlows
from auspex.agent.llm import LLMUnavailable
from auspex.core.logging import get_logger
from auspex.core.models import (
    Conflict,
    Incident,
    Job,
    JobKind,
    JobStatus,
    Plan,
    Producer,
    Verdict,
)
from auspex.decide import fast_lane
from auspex.decide.evaluator import OptionBuilder, Scored, Situation, rank_options
from auspex.decide.playbooks import Playbooks
from auspex.execute.executor import (
    gate_inputs,
    held_tasks,
    owned_tasks,
    standing_closures,
)
from auspex.forecast.engine import Picture, load_active_jobs
from auspex.forecast.eta import EtaModel
from auspex.ledger.store import LedgerKind, append
from auspex.monitor.progress import ProgressMonitor
from auspex.picture.jobs import apply_interpretation, get_job
from auspex.picture.ros_state import RosState
from auspex.policy.approval import ApprovalError, Approvals
from auspex.policy.gate import evaluate
from auspex.policy.loader import Policy
from auspex.site.model import Site
from auspex.site.navgraph import NavGraph

log = get_logger(__name__)

Config = Literal["A", "B", "C"]
CONFIGS: tuple[Config, ...] = ("A", "B", "C")


@dataclass(frozen=True)
class Trigger:
    kind: Literal["conflict", "incident", "resolved", "lift", "job"]
    at: float  # time.monotonic() when it happened
    conflict: Conflict | None = None
    incident: Incident | None = None
    lift: str | None = None
    mode: str | None = None
    fault: str | None = None
    job_urn: str | None = None


T = TypeVar("T")


class AgentRunner:
    """One asyncio loop in its own thread for every agent call (the LLM client keeps its
    connections on it); callers wait with a budget."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, name="agent-loop", daemon=True).start()

    def run(self, coro: Coroutine[Any, Any, T], budget_s: float) -> T:
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return fut.result(timeout=budget_s)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise AgentFailed("agent budget exceeded", budget_s=budget_s) from None


def solver_plan(c: Conflict, best: Scored) -> Plan:
    """The evaluator's option as a plan (producer solver)."""
    o = best.option
    return Plan(
        plan_id=f"sv-{c.conflict_id}-{datetime.now(UTC):%H%M%S}",
        conflict_id=c.conflict_id,
        producer=Producer.SOLVER,
        actions=o.actions,
        predicted=o.effects,
        rationale=f"Evaluator: option {o.option_id} ({o.template.value}) scores best "
        f"{list(best.key)} (missed top-class deadlines, weighted lateness, delay to "
        f"people, interventions); meets every top-class deadline: {best.meets_highest}.",
    )


class DecisionEngine:
    def __init__(
        self,
        *,
        engine: Engine,
        site: Site,
        policy: Policy,
        graphs: Mapping[str, NavGraph],
        eta: EtaModel,
        ros: RosState,
        monitor: ProgressMonitor,
        picture: Callable[[], Picture],
        approvals: Approvals,
        playbooks: Playbooks,
        config: Config,
        fast_lane_budget_ms: int,
        tie_tolerance_s: float,
        flows: AgentFlows | None = None,
        agent_budget_s: float = 30.0,
    ) -> None:
        self.engine = engine
        self.site = site
        self.policy = policy
        self.graphs = graphs
        self.eta = eta
        self.ros = ros
        self.monitor = monitor
        self.picture = picture
        self.approvals = approvals
        self.playbooks = playbooks
        self.config: Config = config
        self.budget_ms = fast_lane_budget_ms
        self.tie_s = tie_tolerance_s
        self.builder = OptionBuilder(site=site, eta=eta, pad_s=self._pad())
        self.flows = flows
        self.agent_budget_s = agent_budget_s
        self.runner = AgentRunner() if flows is not None else None
        self._queue: queue.Queue[Trigger] = queue.Queue(maxsize=1000)
        # What has been decided on, so a trigger repeats no work. Keys tied to jobs are
        # forgotten when those jobs end: a job id or conflict id seen again later (the same
        # transport record re-sent, a scenario run again) is a new case.
        self._handled: set[str] = set()
        self._handled_jobs: dict[str, set[str]] = {}
        self._fallbacks: dict[str, Plan] = {}
        # Decision latency per plan (trigger to submission), for the benchmark (spec 15).
        self.latency_ms: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="decision-engine", daemon=True)
        approvals.on_expired = self._expired

    def _pad(self) -> float:
        pbs = self.playbooks.for_trigger("conflict")
        return pbs[0].pad_s if pbs else 30.0

    # ------------------------------------------------------------------ inputs

    def set_config(self, config: Config, by: str) -> None:
        previous, self.config = self.config, config
        # A new configuration decides afresh: nothing decided under the old one is skipped.
        self._handled.clear()
        self._handled_jobs.clear()
        with self.engine.begin() as conn:
            append(
                conn,
                LedgerKind.SYSTEM,
                {"event": "config", "from": previous, "to": config, "by": by},
            )
        log.warning("decide.config", previous=previous, config=config, by=by)

    def on_picture(
        self, opened: list[Conflict], incidents: list[Incident], resolved: list[Incident]
    ) -> None:
        now = time.monotonic()
        for c in opened:
            self._put(Trigger("conflict", now, conflict=c))
        for i in incidents:
            self._put(Trigger("incident", now, incident=i))
        for i in resolved:
            self._put(Trigger("resolved", now, incident=i))

    def on_job(self, urn: str) -> None:
        self._put(Trigger("job", time.monotonic(), job_urn=urn))

    def on_lift(self, lift: str, mode: str, fault: str | None) -> None:
        self._put(Trigger("lift", time.monotonic(), lift=lift, mode=mode, fault=fault))

    def _put(self, t: Trigger) -> None:
        try:
            self._queue.put_nowait(t)
        except queue.Full:
            log.error("decide.queue_full", kind=t.kind)

    # ------------------------------------------------------------------ loop

    def start(self) -> None:
        self._thread.start()
        log.info("decide.started", config=self.config, budget_ms=self.budget_ms)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                t = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.handle(t)
            except Exception:  # keep deciding; the traceback is in the log
                log.exception("decide.failed", kind=t.kind)

    def _mark(self, key: str, job_urns: list[str] | set[str]) -> None:
        self._handled.add(key)
        self._handled_jobs[key] = set(job_urns)

    def _forget_if_ended(self, urn: str) -> None:
        with self.engine.connect() as conn:
            found = get_job(conn, urn)
        if found is None or found[0].status not in (JobStatus.COMPLETED, JobStatus.CANCELLED):
            return
        keys = [k for k, urns in self._handled_jobs.items() if urn in urns]
        for k in keys:
            self._handled.discard(k)
            del self._handled_jobs[k]
        if keys:
            log.info("decide.job_ended_forget", job=urn, keys=len(keys))

    def handle(self, t: Trigger) -> None:
        if t.kind == "job" and t.job_urn:
            self._forget_if_ended(t.job_urn)  # in every configuration
        if t.kind == "lift" and t.mode == "AGV":
            self._reopen(t)  # undo Auspex's own closure in every configuration
            return
        if self.config == "A":
            log.info("decide.observe_only", kind=t.kind)
            return
        if t.kind == "lift" and t.lift and t.mode:
            self._on_lift(t)
        elif t.kind == "conflict" and t.conflict is not None:
            self._on_conflict(t, t.conflict)
        elif t.kind == "incident" and t.incident is not None:
            self._on_incident(t, t.incident)
        elif t.kind == "job" and t.job_urn and self._agent_on():
            self._on_job(t.job_urn)
        elif t.kind == "resolved" and t.incident is not None and self._agent_on():
            self._on_resolved(t.incident)

    def _agent_on(self) -> bool:
        return self.config == "C" and self.flows is not None and self.runner is not None

    def _agent(self, coro: Coroutine[Any, Any, T], what: str) -> T | None:
        """Run an agent flow within the budget; None (and a log) when it fails closed."""
        assert self.runner is not None
        try:
            return self.runner.run(coro, self.agent_budget_s)
        except (AgentFailed, LLMUnavailable) as exc:
            log.error("agent.failed_closed", what=what, error=str(exc)[:300])
        except Exception:
            log.exception("agent.crashed", what=what)
        return None

    # ------------------------------------------------------------------ situation

    def situation(self) -> Situation:
        with self.engine.connect() as conn:
            jobs = load_active_jobs(conn)
            owned = owned_tasks(conn, self.policy)
            held = held_tasks(conn)
            rows = conn.execute(
                text(
                    "SELECT payload FROM commands WHERE action = 'close_lanes'"
                    " AND status IN ('pending', 'running')"
                    " AND (not_before IS NULL OR not_before <= now())"
                )
            ).all()
        planned: dict[str, set[int]] = {}
        for r in rows:
            p = dict(r.payload)
            planned.setdefault(str(p["fleet"]), set()).update(int(i) for i in p["lanes"])
        pic = self.picture()
        return Situation(
            now=datetime.now(UTC),
            jobs={j.urn: j for j in jobs},
            claims=list(pic.claims),
            paths=dict(pic.paths),
            state=self.ros.state(),
            visited=self.monitor.visited(),
            owned=owned,
            planned_closed={f: frozenset(v) for f, v in planned.items()},
            held=held,
        )

    # ------------------------------------------------------------------ handlers

    def _on_lift(self, t: Trigger) -> None:
        lift, mode = str(t.lift), str(t.mode)
        s = self.situation()
        at_stake = self._deliveries_at_stake(s, f"segment/{lift}") if self._agent_on() else []
        deferred = sorted({fleet for _, fleet in at_stake})
        for pb in self.playbooks.for_trigger("lift_unavailable"):
            plan = fast_lane.lift_unavailable(
                pb, s, self.site, lift, mode, t.fault, skip_fleets=set(deferred)
            )
            if plan is None:
                log.info("fast_lane.not_applicable", playbook=pb.id, lift=lift)
            else:
                self._submit(plan, s, t)
            if deferred:
                log.info(
                    "fast_lane.deferred_to_agent",
                    lift=lift,
                    fleets=deferred,
                    jobs=[j.urn for j, _ in at_stake],
                )
                self._outage_by_agent(t, s, pb, lift, mode, at_stake, deferred)

    def _deliveries_at_stake(self, s: Situation, segment: str) -> list[tuple[Job, str]]:
        out: list[tuple[Job, str]] = []
        for urn, path in s.paths.items():
            job = s.jobs.get(urn)
            found = s.robot_for(job) if job else None
            if job is None or found is None or segment not in path.lifts():
                continue
            if job.precedence in self.policy.stakes.incident_classes:
                out.append((job, found[0]))
        return out

    def _outage_by_agent(
        self,
        t: Trigger,
        s: Situation,
        pb: Any,
        lift: str,
        mode: str,
        at_stake: list[tuple[Job, str]],
        fleets: list[str],
    ) -> None:
        assert self.flows is not None
        safe = fast_lane.lift_unavailable(
            pb, s, self.site, lift, mode, t.fault, only_fleets=set(fleets)
        )
        event = f"fault-{lift}-{datetime.now(UTC):%H%M%S}"
        for job, _ in at_stake:
            trace = Trace()
            plan = self._agent(
                self.flows.outage(s, job, lift, mode, t.fault, event, trace), "outage"
            )
            self._record_trace(event, trace, plan)
            if plan is None:
                if safe is not None:
                    log.warning("agent.fallback_plan", plan=safe.plan_id, why="agent failed")
                    self._submit(safe, s, t)
                return
            if self._submit(plan, s, t) is Verdict.BLOCK and safe is not None:
                self._submit(safe, s, t)
            elif safe is not None:
                self.register_fallback(plan.plan_id, safe)

    def _reopen(self, t: Trigger) -> None:
        lift = str(t.lift)
        with self.engine.connect() as conn:
            fleets = standing_closures(conn, f"segment/{lift}")
        if not fleets:
            return
        s = self.situation()
        for pb in self.playbooks.for_trigger("lift_unavailable"):
            if pb.reopen_when == "lift_available":
                self._submit(fast_lane.lift_available(pb, s, self.site, lift, fleets), s, t)

    def _on_conflict(self, t: Trigger, c: Conflict) -> None:
        if c.conflict_id in self._handled:
            return
        self._mark(c.conflict_id, c.job_urns)
        s = self.situation()
        jobs = [s.jobs[u] for u in c.job_urns if u in s.jobs]
        robots = [j for j in jobs if j.kind is JobKind.ROBOT_TASK]
        people = [j for j in jobs if j.kind is not JobKind.ROBOT_TASK]
        if len(robots) != 1 or len(people) != 1:
            self._record_options(c, [], s, note=self._people_note(people))
            if not robots and self._agent_on() and self._clinical_tie(people):
                self._people_tie_by_agent(t, s, c)
            return
        robot_job, human_job = robots[0], people[0]
        acted = False
        fast_late: bool | None = None  # the fast lane's own forecast for the delivery
        for pb in self.playbooks.for_trigger("conflict"):
            plan = fast_lane.clear_for_code_stat(
                pb, s, self.site, self.eta, c, robot_job, human_job
            )
            if plan is not None:
                verdict = self._submit(plan, s, t, claims_for=c)
                acted = verdict is Verdict.ALLOW
                if verdict is not Verdict.BLOCK:
                    fast_late = plan.predicted[0].lateness_s > 0
                break
        ranked = rank_options(
            self.builder.for_conflict(s, robot_job, human_job, c.segment_urn),
            s.jobs,
            self.policy,
            self.tie_s,
        )
        best = self._best_allowed(ranked, c, s)
        self._record_options(c, ranked, s, best=best, acted_by_fast_lane=acted)
        if fast_late is not None:
            late = fast_late  # the fast lane answered (applied or with a person)
        else:
            usable = self._best_not_blocked(ranked, c, s)
            late = usable is None or usable.option.effects[0].lateness_s > 0
        if (
            self._agent_on()
            and late
            and robot_job.due_at is not None
            and self._tradeoff_by_agent(t, s, c, robot_job, ranked, best)
        ):
            return
        if acted or best is None:
            if not acted and best is None:
                log.warning("decide.no_option", conflict=c.conflict_id)
            return
        if best.option.template.value == "do_nothing":
            log.info("decide.do_nothing", conflict=c.conflict_id, score=list(best.key))
            return
        self._submit(self._solver_plan(c, best), s, t, claims_for=c)

    def _tradeoff_by_agent(
        self,
        t: Trigger,
        s: Situation,
        c: Conflict,
        robot_job: Job,
        ranked: list[Scored],
        best: Scored | None,
    ) -> bool:
        assert self.flows is not None
        key = f"tradeoff:{robot_job.urn}"
        if key in self._handled:
            return True  # the agent already proposed for this delivery; the plan covers it
        self._mark(key, {robot_job.urn})
        trace = Trace()
        s2 = self.situation()  # after the fast lane: the robot may now be held
        plan = self._agent(self.flows.tradeoff(s2, c, robot_job, ranked, trace), "tradeoff")
        self._record_trace(c.conflict_id, trace, plan)
        if plan is None:
            return False
        verdict = self._submit(plan, s2, t, claims_for=c)
        if verdict is not Verdict.BLOCK and best is not None:
            self.register_fallback(plan.plan_id, self._solver_plan(c, best))
        return verdict is not Verdict.BLOCK

    # ------------------------------------------------------------------ agent: S1, reports

    def _on_job(self, urn: str) -> None:
        assert self.flows is not None
        s = self.situation()
        job = s.jobs.get(urn)
        if (
            job is None
            or job.kind is not JobKind.HUMAN_TRANSPORT
            or job.destination is not None
            or not job.destination_text
            or job.status in (JobStatus.COMPLETED, JobStatus.CANCELLED)
            or urn in self._handled
        ):
            return
        self._mark(urn, {urn})
        trace = Trace()
        t0 = time.monotonic()
        answer = self._agent(self.flows.interpret(job, trace), "interpret")
        record = {
            "trace": trace.steps,
            "latency_ms": round((time.monotonic() - t0) * 1000),
            "destination_text": job.destination_text,
        }
        if answer is None or answer.destination_zone is None:
            with self.engine.begin() as conn:
                append(conn, LedgerKind.INTERPRETATION, {"urn": urn, "inferred": None, **record})
            return
        with self.engine.begin() as conn:
            apply_interpretation(
                conn,
                urn,
                answer.destination_zone,
                answer.confidence,
                {
                    "inferred": {"destination": answer.destination_zone},
                    "confidence": answer.confidence,
                    "route_constraints": answer.route_constraints,
                    "rationale": answer.rationale,
                    **record,
                },
            )

    def _on_resolved(self, inc: Incident) -> None:
        assert self.flows is not None
        key = f"report:{inc.incident_id}"
        if key in self._handled:
            return
        self._handled.add(key)
        trace = Trace()
        report = self._agent(self.flows.report(inc, trace), "report")
        if report is None:
            return
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE incidents SET data = jsonb_set(data, '{report}', CAST(:r AS jsonb)),"
                    " updated_at = now() WHERE incident_id = :id"
                ),
                {"id": inc.incident_id, "r": report.model_dump_json()},
            )
            append(
                conn,
                LedgerKind.INCIDENT_REPORT,
                {"incident_id": inc.incident_id, "report": report, "trace": trace.steps},
            )
        log.info("agent.incident_report", incident=inc.incident_id, causes=len(report.causes))

    def _record_trace(self, subject: str, trace: Trace, plan: Plan | None) -> None:
        with self.engine.begin() as conn:
            append(
                conn,
                LedgerKind.OPTIONS,
                {
                    "subject": subject,
                    "config": self.config,
                    "agent": True,
                    "models": trace.models(),
                    "plan_id": plan.plan_id if plan else None,
                    "trace": trace.steps,
                },
            )

    def _on_incident(self, t: Trigger, inc: Incident) -> None:
        key = f"{inc.incident_id}:{inc.status.value}"
        if key in self._handled or inc.status.value != "open":
            return
        self._handled.add(key)
        s = self.situation()
        for pb in self.playbooks.for_trigger("incident"):
            plan = fast_lane.notify_stalled(pb, s, inc)
            if plan is not None:
                self._submit(plan, s, t)

    # ------------------------------------------------------------------ plans

    def _solver_plan(self, c: Conflict, best: Scored) -> Plan:
        return solver_plan(c, best)

    def _best_allowed(self, ranked: list[Scored], c: Conflict, s: Situation) -> Scored | None:
        with self.engine.connect() as conn:
            inputs = gate_inputs(
                conn,
                self.policy,
                self.site,
                self.graphs,
                s.state,
                list(s.jobs.values()),
                self._claims(c, s),
            )
        for sc in ranked:
            probe = self._solver_plan(c, sc)
            d = evaluate(probe, self.site, self.policy, inputs)
            if d.verdict is Verdict.ALLOW:
                return sc
        return None

    def _best_not_blocked(self, ranked: list[Scored], c: Conflict, s: Situation) -> Scored | None:
        with self.engine.connect() as conn:
            inputs = gate_inputs(
                conn,
                self.policy,
                self.site,
                self.graphs,
                s.state,
                list(s.jobs.values()),
                self._claims(c, s),
            )
        for sc in ranked:
            if evaluate(self._solver_plan(c, sc), self.site, self.policy, inputs).verdict is not (
                Verdict.BLOCK
            ):
                return sc
        return None

    def _clinical_tie(self, people: list[Job]) -> bool:
        classes = {p.precedence for p in people}
        return (
            len(people) >= 2
            and len(classes) == 1
            and next(iter(classes)).value
            in (
                "code",
                "stat",
                "critical",
            )
        )

    def _people_tie_by_agent(self, t: Trigger, s: Situation, c: Conflict) -> None:
        assert self.flows is not None
        trace = Trace()
        plan = self._agent(self.flows.tradeoff(s, c, None, [], trace), "people_tie")
        self._record_trace(c.conflict_id, trace, plan)
        if plan is not None:
            self._submit(plan, s, t, claims_for=c)

    def _claims(self, c: Conflict, s: Situation) -> list[Any]:
        return [x for x in s.claims if x.claim_id in set(c.claim_ids)]

    def _submit(
        self, plan: Plan, s: Situation, t: Trigger, claims_for: Conflict | None = None
    ) -> Verdict:
        with self.engine.connect() as conn:
            inputs = gate_inputs(
                conn,
                self.policy,
                self.site,
                self.graphs,
                s.state,
                list(s.jobs.values()),
                self._claims(claims_for, s) if claims_for else [],
            )
        try:
            decision = self.approvals.submit(plan, inputs)
        except ApprovalError as exc:
            log.error("decide.submit_failed", plan=plan.plan_id, error=str(exc))
            return Verdict.BLOCK
        ms = (time.monotonic() - t.at) * 1000
        self.latency_ms[plan.plan_id] = round(ms, 1)
        if plan.producer is Producer.FAST_LANE:
            log_fn = log.info if ms <= self.budget_ms else log.error
            log_fn(
                "fast_lane.decided",
                plan=plan.plan_id,
                latency_ms=round(ms, 1),
                budget_ms=self.budget_ms,
                within_budget=ms <= self.budget_ms,
                verdict=decision.verdict.value,
            )
        else:
            log.info(
                f"{plan.producer.value}.decided",
                plan=plan.plan_id,
                latency_ms=round(ms, 1),
                verdict=decision.verdict.value,
            )
        return decision.verdict

    def _record_options(
        self,
        c: Conflict,
        ranked: list[Scored],
        s: Situation,
        *,
        best: Scored | None = None,
        acted_by_fast_lane: bool = False,
        note: str | None = None,
    ) -> None:
        record = {
            "conflict_id": c.conflict_id,
            "segment_urn": c.segment_urn,
            "config": self.config,
            "options": [x.as_dict() for x in ranked],
            "best_allowed": best.option.option_id if best else None,
            "meets_highest": any(x.meets_highest for x in ranked) if ranked else None,
            "fast_lane_acted": acted_by_fast_lane,
            "note": note,
            "at": s.now,
        }
        with self.engine.begin() as conn:
            append(conn, LedgerKind.OPTIONS, record)
        log.info(
            "decide.options",
            conflict=c.conflict_id,
            options=[(x.option.option_id, list(x.key)) for x in ranked],
            best=record["best_allowed"],
            meets_highest=record["meets_highest"],
            note=note,
        )

    def _people_note(self, people: list[Job]) -> str:
        if len(people) < 2:
            return "no robot job with a forecast route in this conflict; nothing to change"
        classes = {p.precedence for p in people}
        tie = len(classes) == 1 and next(iter(classes)).value in ("code", "stat", "critical")
        order = [p.external_id for p in sorted(people, key=self.policy.rank)]
        return f"two people compete; Auspex does not order people. Policy precedence: {order}" + (
            "; same clinical class: a person decides (clinical-tie-needs-person)" if tie else ""
        )

    # ------------------------------------------------------------------ approval timeout

    def register_fallback(self, plan_id: str, fallback: Plan) -> None:
        self._fallbacks[plan_id] = fallback

    def _expired(self, plan_id: str) -> None:
        fallback = self._fallbacks.pop(plan_id, None)
        if fallback is None:
            log.warning("decide.declined_no_fallback", plan=plan_id)
            return
        log.warning("decide.fallback", declined=plan_id, fallback=fallback.plan_id)
        self._submit(fallback, self.situation(), Trigger("conflict", time.monotonic()))
