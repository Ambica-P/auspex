"""Policy gate (spec 9): every plan passes here before anything can act. Deterministic, no I/O.

Returns Decision{verdict, rule_ids, risk, approval_route}; risk is recomputed here, never
taken from the plan.

Build rules (what makes a plan executable at all), always BLOCK:
- `unsupported-action`: actions this build cannot carry out (hold_at_waypoint, set_priority,
  set_earliest_start, restrict_fleets, claim/release_mutex);
- `owned-tasks-only`: interrupt, resume or cancel (or replace) a task Auspex does not own;
- `known-target`: unknown segment, fleet without lanes on it, unknown robot, or a task place
  that is not a waypoint in the chosen fleet's graph.

Hospital rules (policy/rules/*.yaml), each with its on_fail:
- plan_effect `delays_job` + job_class: a predicted effect delays a job of those classes
  (lateness, or delay caused to a person); delay among two or more jobs of one class that
  the plan orders is left to `orders_same_class_jobs` (a person decides);
- plan_effect `orders_same_class_jobs` + job_class: the plan's effects touch two or more jobs
  of one of those classes and delay at least one of them (it orders them);
- action + `job_class_at_or_below`: the action's job must rank at or below that class;
- action + payload + `fleet_certified_for`: the task's fleet must hold the certification
  (a task without a fleet could go to any fleet, so every fleet must hold it);
- action + `max_closed_segments`: segments closed after the plan, at most this many;
- claim_basis + `min_confidence`: the plan's conflict rests on such claims with less
  confidence.

Route: BLOCK stops. REVIEW goes to a person. ALLOW is automatic for fast-lane and solver
plans that do not touch staff; agent plans that change a CRITICAL job's ETA, anything that
advises staff, and operator plans go to a person (spec 9 table).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from auspex.core.logging import get_logger
from auspex.core.models import (
    AdviseStaff,
    ApprovalRoute,
    CancelTask,
    Claim,
    CloseLanes,
    Decision,
    InterruptTask,
    JobEffect,
    OpenLanes,
    Plan,
    PrecedenceClass,
    Producer,
    ReplanRobot,
    RestrictFleets,
    ResumeTask,
    SubmitTask,
    Verdict,
)
from auspex.policy.loader import PlanEffect, Policy, Rule
from auspex.site.model import Site

log = get_logger(__name__)

SUPPORTED = frozenset(
    {
        "close_lanes",
        "open_lanes",
        "replan_robot",
        "submit_task",
        "cancel_task",
        "interrupt_task",
        "resume_task",
        "notify",
        "advise_staff",
    }
)
RISK_BY_ACTION = {
    "close_lanes": 0.15,
    "open_lanes": 0.05,
    "replan_robot": 0.05,
    "submit_task": 0.1,
    "cancel_task": 0.25,
    "interrupt_task": 0.2,
    "resume_task": 0.05,
    "notify": 0.0,
    "advise_staff": 0.1,
}
DELAY_TOLERANCE_S = 1.0  # effects are estimates; below this a job is not delayed


@dataclass(frozen=True)
class JobFacts:
    precedence: PrecedenceClass
    payload: str | None = None
    destination_type: str | None = None


@dataclass(frozen=True)
class GateInputs:
    owned_tasks: frozenset[str]
    robots: dict[str, frozenset[str]]  # fleet -> robot names seen in /fleet_states
    waypoints: dict[str, frozenset[str]] = field(default_factory=dict)  # fleet -> names
    closed_segments: frozenset[str] = field(default_factory=frozenset)  # closed now
    jobs: dict[str, JobFacts] = field(default_factory=dict)  # active jobs by urn
    claims: tuple[Claim, ...] = ()  # the claims behind the plan's conflict


def _delayed(e: JobEffect) -> bool:
    return e.lateness_s > DELAY_TOLERANCE_S or e.delay_to_humans_s > DELAY_TOLERANCE_S


def _build_rules(plan: Plan, site: Site, inputs: GateInputs) -> tuple[list[str], set[str]]:
    blocks: list[str] = []
    closing: set[str] = set(inputs.closed_segments)
    for action in plan.actions:
        if action.action not in SUPPORTED:
            blocks.append("unsupported-action")
            continue
        if (
            isinstance(action, (CancelTask, InterruptTask, ResumeTask))
            and action.task_id not in inputs.owned_tasks
        ):
            blocks.append("owned-tasks-only")
        if isinstance(action, (CloseLanes, OpenLanes)):
            seg = site.segments.get(action.segment_urn)
            if seg is None or not seg.rmf_lanes.get(action.fleet_name):
                blocks.append("known-target")
            elif isinstance(action, CloseLanes):
                closing.add(action.segment_urn)
            else:
                closing.discard(action.segment_urn)
        if isinstance(action, SubmitTask):
            if action.replaces_task_id and action.replaces_task_id not in inputs.owned_tasks:
                blocks.append("owned-tasks-only")
            places = [p for p in (action.pickup_place, action.dropoff_place) if p]
            fleets = [action.fleet_name] if action.fleet_name else list(inputs.waypoints)
            if not any(set(places) <= inputs.waypoints.get(f, frozenset()) for f in fleets):
                blocks.append("known-target")
        if isinstance(action, ReplanRobot) and action.robot_name not in inputs.robots.get(
            action.fleet_name, frozenset()
        ):
            blocks.append("known-target")
    return blocks, closing


def _rule_fails(
    rule: Rule, plan: Plan, site: Site, policy: Policy, inputs: GateInputs, closing: set[str]
) -> bool:
    scope, req = rule.applies_to, rule.require
    classes = set(scope.job_class or [])

    def klass(urn: str) -> PrecedenceClass | None:
        facts = inputs.jobs.get(urn)
        return facts.precedence if facts else None

    if scope.plan_effect is PlanEffect.DELAYS_JOB:
        # Delay that comes from ordering two or more jobs of one class among themselves is
        # judged by the ordering rule (a person decides), not as a delay Auspex causes.
        per_class: dict[PrecedenceClass | None, int] = {}
        for e in plan.predicted:
            per_class[klass(e.job_urn)] = per_class.get(klass(e.job_urn), 0) + 1
        return any(
            _delayed(e) and klass(e.job_urn) in classes and per_class[klass(e.job_urn)] < 2
            for e in plan.predicted
        )
    if scope.plan_effect is PlanEffect.ORDERS_SAME_CLASS_JOBS:
        by_class: dict[PrecedenceClass, list[JobEffect]] = {}
        for e in plan.predicted:
            k = klass(e.job_urn)
            if k in classes and k is not None:
                by_class.setdefault(k, []).append(e)
        return any(len(es) >= 2 and any(_delayed(e) for e in es) for es in by_class.values())
    if scope.claim_basis is not None:
        if req is None or req.min_confidence is None:
            return False
        return any(
            c.basis is scope.claim_basis and c.confidence < req.min_confidence
            for c in inputs.claims
        )
    actions = [a for a in plan.actions if a.action in set(scope.action or [])]
    if not actions or req is None:
        return False
    if req.max_closed_segments is not None:
        return len(closing) > req.max_closed_segments
    if req.job_class_at_or_below is not None:
        floor = policy.class_rank(req.job_class_at_or_below)
        for a in actions:
            urn = getattr(a, "job_urn", None)
            k = klass(str(urn)) if urn else None
            if k is None or policy.class_rank(k) < floor:
                return True
        return False
    if req.fleet_certified_for is not None:
        cert = req.fleet_certified_for
        certified = {f for f, spec in site.fleets.items() if cert in spec.certified_for}
        for a in actions:
            if isinstance(a, SubmitTask):
                facts = inputs.jobs.get(a.job_urn)
                if scope.payload and (facts is None or facts.payload != scope.payload):
                    continue
                fleets = {a.fleet_name} if a.fleet_name else set(site.fleets)
            elif isinstance(a, RestrictFleets):
                facts = inputs.jobs.get(a.job_urn)
                if scope.payload and (facts is None or facts.payload != scope.payload):
                    continue
                fleets = set(a.fleet_names)
            else:
                continue
            if not fleets <= certified:
                return True
        return False
    return False


def _route(plan: Plan, inputs: GateInputs) -> ApprovalRoute:
    if any(isinstance(a, AdviseStaff) for a in plan.actions):
        return ApprovalRoute.PERSON
    if plan.producer in (Producer.FAST_LANE, Producer.SOLVER):
        return ApprovalRoute.AUTOMATIC
    if plan.producer is Producer.AGENT:
        touches_critical = any(
            (f := inputs.jobs.get(e.job_urn)) is not None
            and f.precedence is PrecedenceClass.CRITICAL
            for e in plan.predicted
        )
        return ApprovalRoute.PERSON if touches_critical else ApprovalRoute.AUTOMATIC
    return ApprovalRoute.PERSON  # operator plans: a person confirms


def evaluate(plan: Plan, site: Site, policy: Policy, inputs: GateInputs) -> Decision:
    blocks, closing = _build_rules(plan, site, inputs)
    reviews: list[str] = []
    for rule in policy.rules:
        if _rule_fails(rule, plan, site, policy, inputs, closing):
            (blocks if rule.on_fail is Verdict.BLOCK else reviews).append(rule.id)
    risk = min(1.0, sum(RISK_BY_ACTION.get(a.action, 0.3) for a in plan.actions))
    if blocks:
        verdict, rules, route = Verdict.BLOCK, sorted(set(blocks)), ApprovalRoute.PERSON
    elif reviews:
        verdict, rules, route = Verdict.REVIEW, sorted(set(reviews)), ApprovalRoute.PERSON
    else:
        verdict, rules, route = Verdict.ALLOW, [], _route(plan, inputs)
    decision = Decision(
        plan_id=plan.plan_id,
        verdict=verdict,
        rule_ids=rules,
        risk=round(risk, 3),
        approval_route=route,
    )
    log.info(
        "gate.decision",
        plan=plan.plan_id,
        producer=plan.producer.value,
        verdict=verdict.value,
        rules=rules,
        route=route.value,
        risk=decision.risk,
        actions=[a.action for a in plan.actions],
    )
    return decision
