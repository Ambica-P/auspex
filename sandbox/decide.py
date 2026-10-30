"""The sandbox's decision for an edited job 4471 (spec 14.6), with the core's own code:

1. the edited fields become a transport-system record, normalised by the core's intake;
2. Nemotron Nano reads the free text against the site's zones (same prompt, schema and
   site-model validation as the core: auspex.agent.flows.interpret_job);
3. the forecaster turns both jobs into claims at 10:28 on the recorded S1 snapshot (Robot A's
   position and task as recorded; its route recomputed on the captured nav graphs);
4. the conflict detector, the fast-lane playbook, the evaluator and the gate decide as the
   core's decision engine does for a robot-vs-person conflict.

Nothing is actuated. The result is shaped like the ops console's decision context, so the
replay site renders it with the same code (render.js)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator

from auspex.agent.agent import Agent, AgentFailed, Trace
from auspex.agent.flows import interpret_job
from auspex.core.logging import get_logger
from auspex.core.models import (
    Claim,
    Conflict,
    Decision,
    Job,
    JobKind,
    Plan,
    StrictModel,
    Verdict,
    strip_control,
)
from auspex.decide import fast_lane
from auspex.decide.engine import solver_plan
from auspex.decide.evaluator import OptionBuilder, Scored, Situation, rank_options
from auspex.decide.playbooks import Playbooks, load_playbooks
from auspex.execute.executor import make_gate_inputs
from auspex.forecast.eta import EtaModel
from auspex.forecast.forecaster import detect_conflicts, forecast, in_horizon
from auspex.ingest.hospital import (
    DeliveryRecord,
    StatusUpdate,
    TransportRecord,
    apply_status,
    normalize,
)
from auspex.picture.jobs import with_interpretation
from auspex.picture.ros_state import StateView
from auspex.policy.gate import evaluate
from auspex.policy.loader import Policy, load_policy
from auspex.site.model import DEFAULT_SITE_DIR, Site, build_site
from auspex.site.navgraph import NavGraph, load_captures

log = get_logger(__name__)

SNAPSHOT = Path(__file__).resolve().parent / "snapshots" / "s1.json"
HORIZON_S = 900.0
LABEL = "Decision only. No robot moves; the simulation is not running."


class S1Input(StrictModel):
    """The editable fields of transport job 4471. Lengths are re-checked against
    SANDBOX_MAX_INPUT_CHARS by the app."""

    destination_text: str = Field(min_length=1, max_length=500)
    notes: str = Field(default="", max_length=500)
    transport_mode: Literal["bed", "wheelchair", "stretcher", "walking"] = "bed"
    bed_type: Literal["standard", "bariatric", "crib"] | None = "bariatric"
    priority: Literal["Stat", "High", "Normal", "Low"] = "Stat"
    status_time: str = Field(default="10:28", pattern=r"^10:(2[7-9]|3[0-5])$")

    @field_validator("destination_text", "notes")
    @classmethod
    def _clean(cls, v: str) -> str:
        return strip_control(v).strip()


@dataclass
class World:
    site: Site
    graphs: dict[str, NavGraph]
    policy: Policy
    playbooks: Playbooks
    eta: EtaModel
    builder: OptionBuilder
    snapshot: dict[str, Any]


def load_world(snapshot_path: Path = SNAPSHOT) -> World:
    if not snapshot_path.is_file():
        raise FileNotFoundError(
            f"no snapshot at {snapshot_path}: run python -m sandbox.snapshot (scripts/run-sandbox)"
        )
    graphs = load_captures(DEFAULT_SITE_DIR / "nav_graphs")
    site = build_site(DEFAULT_SITE_DIR, graphs)
    eta = EtaModel(site, graphs)
    return World(
        site=site,
        graphs=graphs,
        policy=load_policy(),
        playbooks=load_playbooks(),
        eta=eta,
        builder=OptionBuilder(site=site, eta=eta, pad_s=30.0),
        snapshot=json.loads(snapshot_path.read_text("utf-8")),
    )


def _at(snap: dict[str, Any], clock: str) -> datetime:
    parts = [int(p) for p in clock.split(":")]
    while len(parts) < 3:
        parts.append(0)
    return datetime.fromisoformat(snap["date"]).replace(
        hour=parts[0], minute=parts[1], second=parts[2], tzinfo=UTC
    )


def _record_times(snap: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    out = dict(record)
    for key in ("needed_by", "status_at"):
        if isinstance(out.get(key), str) and ":" in out[key] and "T" not in out[key]:
            out[key] = _at(snap, out[key]).isoformat()
    return out


def _jobs(world: World, inp: S1Input) -> tuple[Job, dict[str, Any], Job]:
    snap = world.snapshot
    raw = {
        **snap["transport"]["record"],
        "destination_text": inp.destination_text,
        "notes": inp.notes or None,
        "transport_mode": inp.transport_mode,
        "bed_type": inp.bed_type,
        "priority": inp.priority,
        "status": snap["transport"]["status"],
        "status_at": inp.status_time,
    }
    raw = _record_times(snap, raw)
    transport = normalize(TransportRecord.model_validate(raw), world.site, world.policy)
    rj = snap["robot_job"]
    delivery = normalize(
        DeliveryRecord.model_validate(_record_times(snap, rj["record"])), world.site, world.policy
    )
    update = StatusUpdate(
        status=rj["status"], status_at=_at(snap, rj["status_at"]), rmf_task_id=rj["task_id"]
    )
    moved = apply_status(delivery, update)
    return transport, raw, moved or delivery


def _situation(world: World, jobs: list[Job], now: datetime) -> Situation:
    snap = world.snapshot
    task = snap["robot_job"]["task_id"]
    visited = {task: bool(snap["robot_job"]["visited_origin"])}
    closed = {f: set(v) for f, v in snap.get("closed", {}).items()}
    claims, paths = forecast(
        site=world.site,
        eta=world.eta,
        jobs=jobs,
        fleets=snap["fleets"],
        closed=closed,
        rtf={},
        visited=visited,
        now=now,
    )
    state = StateView(
        fleets=snap["fleets"],
        closed=closed,
        lifts=snap.get("lifts", {}),
        doors={},
        rtf={},
        lift_requests={},
    )
    return Situation(
        now=now,
        jobs={j.urn: j for j in jobs},
        claims=in_horizon(claims, now, HORIZON_S),
        paths=paths,
        state=state,
        visited=visited,
        owned=frozenset({task}),
    )


def _decide(
    world: World, s: Situation, c: Conflict
) -> tuple[Plan | None, Decision | None, list[Scored], str]:
    """As the decision engine does for one robot-vs-person conflict (deterministic part)."""
    jobs = [s.jobs[u] for u in c.job_urns if u in s.jobs]
    robots = [j for j in jobs if j.kind is JobKind.ROBOT_TASK]
    people = [j for j in jobs if j.kind is not JobKind.ROBOT_TASK]
    if len(robots) != 1 or len(people) != 1:
        return None, None, [], "not a robot-versus-person conflict; nothing to change"
    robot_job, human_job = robots[0], people[0]
    claims = [x for x in s.claims if x.claim_id in set(c.claim_ids)]
    inputs = make_gate_inputs(
        s.owned, world.site, world.graphs, s.state, list(s.jobs.values()), claims
    )
    ranked = rank_options(
        world.builder.for_conflict(s, robot_job, human_job, c.segment_urn),
        s.jobs,
        world.policy,
        30.0,
    )
    for pb in world.playbooks.for_trigger("conflict"):
        plan = fast_lane.clear_for_code_stat(pb, s, world.site, world.eta, c, robot_job, human_job)
        if plan is not None:
            return (
                plan,
                evaluate(plan, world.site, world.policy, inputs),
                ranked,
                (f"playbook {pb.id} applies (the person's job is {human_job.precedence.value})"),
            )
    for sc in ranked:
        probe = solver_plan(c, sc)
        d = evaluate(probe, world.site, world.policy, inputs)
        if d.verdict is Verdict.ALLOW:
            if sc.option.template.value == "do_nothing":
                return None, None, ranked, "the evaluator's best option is to do nothing"
            return probe, d, ranked, "no playbook applies; the evaluator's best allowed option"
    return None, None, ranked, "no option passes the gate without a person"


def _claim_view(c: Claim) -> dict[str, Any]:
    return {
        "segment_urn": c.segment_urn,
        "start": c.window.start.isoformat(),
        "end": c.window.end.isoformat(),
        "basis": c.basis.value,
        "confidence": c.confidence,
    }


async def decide(world: World, agent: Agent, inp: S1Input) -> dict[str, Any]:
    snap = world.snapshot
    now = _at(snap, snap["now"])
    transport, raw, robot_job = _jobs(world, inp)
    trace = Trace()
    interp: dict[str, Any] = {"inferred": False, "rationale": "", "route_constraints": []}
    try:
        answer = await interpret_job(agent, world.site, transport, trace)
    except AgentFailed as exc:
        answer = None
        interp["rationale"] = f"Nemotron's answer failed validation twice: {exc.message}"
    if answer is not None and answer.destination_zone is not None:
        transport = with_interpretation(transport, answer.destination_zone, answer.confidence)
        interp = {
            "inferred": True,
            "destination": answer.destination_zone,
            "confidence": answer.confidence,
            "route_constraints": answer.route_constraints,
            "rationale": answer.rationale,
        }
    elif not interp["rationale"]:
        interp["rationale"] = "Nemotron was not sure enough to infer a destination."
    t0 = time.monotonic()
    s = _situation(world, [transport, robot_job], now)
    conflicts = [
        c
        for c in detect_conflicts(
            claims=s.claims, jobs=s.jobs, site=world.site, policy=world.policy, now=now
        )
        if transport.urn in c.job_urns
    ]
    plan: Plan | None = None
    decision: Decision | None = None
    ranked: list[Scored] = []
    note = "no conflict with Robot A: nothing to decide"
    if conflicts:
        plan, decision, ranked, note = _decide(world, s, conflicts[0])
    deterministic_ms = round((time.monotonic() - t0) * 1000, 1)
    llm = [st for st in trace.steps if st["kind"] == "llm"]
    path = s.paths.get(robot_job.urn)
    result: dict[str, Any] = {
        "label": LABEL,
        "now": now.isoformat(),
        "clock_start": snap["clock_start"],
        "interpretation": interp,
        "transport_claims": [_claim_view(c) for c in s.claims if c.job_urn == transport.urn],
        "robot_route_lifts": path.lifts() if path is not None else [],
        "conflicts": [
            {
                "segment_urn": c.segment_urn,
                "job_urns": c.job_urns,
                "start": c.window.start.isoformat(),
                "end": c.window.end.isoformat(),
            }
            for c in conflicts
        ],
        "verdict": decision.verdict.value if decision else None,
        "rule_ids": decision.rule_ids if decision else [],
        "note": note,
        "models": [
            {
                "model_id": st["model_id"],
                "latency_ms": st["latency_ms"],
                "tokens": int(st["prompt_tokens"]) + int(st["completion_tokens"]),
                "prompt_tokens": int(st["prompt_tokens"]),
                "completion_tokens": int(st["completion_tokens"]),
                "outcome": st["outcome"],
            }
            for st in llm
        ],
        "deterministic_ms": deterministic_ms,
        "detail": {
            "plan": {
                "plan_id": plan.plan_id if plan else "none",
                "status": "decision_only",
                "producer": plan.producer.value if plan else "solver",
                "plan": plan.model_dump(mode="json")
                if plan
                else {"actions": [], "rationale": note},
                "decision": decision.model_dump(mode="json") if decision else {},
                "approval": None,
                "verification": None,
                "commands": [],
            },
            "decision_latency_ms": deterministic_ms,
            "jobs": [
                {
                    "job": transport.model_dump(mode="json"),
                    "raw": raw,
                    "interpretation": interp,
                },
                {"job": robot_job.model_dump(mode="json"), "raw": {}, "interpretation": None},
            ],
            "decisions": [
                {"options": [x.as_dict() for x in ranked], "note": note, "trace": trace.steps}
            ],
            "evidence": [],
        },
    }
    log.info(
        "sandbox.decided",
        inferred=interp["inferred"],
        conflicts=len(conflicts),
        verdict=result["verdict"],
        rules=result["rule_ids"],
        deterministic_ms=deterministic_ms,
    )
    return result


def validate_input(data: dict[str, Any], max_chars: int) -> S1Input:
    inp = S1Input.model_validate(data)
    for name in ("destination_text", "notes"):
        if len(getattr(inp, name)) > max_chars:
            raise ValidationError.from_exception_data(
                "S1Input",
                [
                    {
                        "type": "string_too_long",
                        "loc": (name,),
                        "input": "",
                        "ctx": {"max_length": max_chars},
                    }
                ],
            )
    return inp
