"""Agent flows (spec 6.3): what the agent sees, which tools run, what becomes a Plan.

The orchestrator runs the read-only tools (spec 6.3 list) and records each call with its
result in the trace; Nemotron reasons over the results and answers in a typed schema. Every
proposal is re-scored by the deterministic evaluator; the plan then goes to the gate like
any other. The agent cannot reach the executor.

- interpret (Nano): free-text destination -> zone, as an inferred field with confidence (S1);
- outage (Super, with Nano extraction of vendor bulletins found by Tavily): how long a lift
  fault lasts, then which evaluated option to take: wait, reroute, hand over (S2);
- tradeoff (Super): when no template option meets every top-class deadline, composite
  options: hand the payload over from another store, suggest an order to staff (S3);
- report (Nano): possible causes of a stall, each tied to ledger evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text

from auspex.agent.agent import Agent, AgentFailed, Trace
from auspex.agent.schemas import Interpretation
from auspex.core.logging import get_logger
from auspex.core.models import (
    Action,
    Conflict,
    Evidence,
    Incident,
    IncidentReport,
    Job,
    JobEffect,
    JobKind,
    Notify,
    Option,
    OptionTemplate,
    Plan,
    PossibleCause,
    Producer,
    TimeWindow,
    VendorAdvisory,
)
from auspex.decide.evaluator import OptionBuilder, Scored, Situation, lateness, rank_options
from auspex.evidence.service import EvidenceService, SearchBudget
from auspex.policy.loader import Policy
from auspex.site.model import Site

log = get_logger(__name__)

TRANSPORT_DESK = "staff/transport-desk"
MIN_INTERPRETATION_CONFIDENCE = 0.3  # below this nothing is inferred (the gate wants 0.6)


def _job_view(j: Job) -> dict[str, Any]:
    return {
        "urn": j.urn,
        "kind": j.kind.value,
        "precedence": j.precedence.value,
        "status": j.status.value,
        "origin": j.origin,
        "destination": j.destination,
        "payload": j.payload,
        "transport_mode": j.transport_mode,
        "bed_type": j.bed_type,
        "due_at": j.due_at,
    }


def _person_view(j: Job, w: TimeWindow | None) -> dict[str, Any]:
    view = _job_view(j)
    if w is not None:
        view["needs_segment"] = [w.start, w.end]
    return view


def _options_view(ranked: list[Scored]) -> list[dict[str, Any]]:
    out = []
    for sc in ranked:
        robot = sc.option.effects[0]
        out.append(
            {
                "option_id": sc.option.option_id,
                "template": sc.option.template.value,
                "actions": [a.action for a in sc.option.actions],
                "robot_eta": robot.eta,
                "robot_lateness_s": round(robot.lateness_s),
                "delay_to_people_s": sc.detail.get("delay_to_people_s", 0.0),
                "score": list(sc.key),
                "meets_highest": sc.meets_highest,
            }
        )
    return out


async def interpret_job(agent: Agent, site: Site, job: Job, trace: Trace) -> Interpretation | None:
    """Nano reads a job's free-text destination and notes against the site's zones. None when
    it is unsure (nothing is inferred). Shared by the core and the decision sandbox."""
    zones = [
        {"urn": z.urn, "name": z.name, "aliases": z.aliases, "level": z.level, "kind": z.kind}
        for z in site.zones.values()
    ]
    trace.tool("get_job", {"urn": job.urn}, _job_view(job))
    trace.tool("get_site", {"zones": "all"}, {"zones": len(zones)})
    answer = await agent.interpret(
        job=_job_view(job),
        zones=zones,
        destination_text=job.destination_text or "",
        notes=job.notes or "",
        trace=trace,
    )
    if answer.destination_zone is None or answer.confidence < MIN_INTERPRETATION_CONFIDENCE:
        log.info("agent.interpret_unsure", urn=job.urn, confidence=answer.confidence)
        return None
    return answer


class AgentFlows:
    def __init__(
        self,
        *,
        agent: Agent,
        engine: Engine,
        site: Site,
        policy: Policy,
        builder: OptionBuilder,
        evidence: EvidenceService,
        tie_s: float,
    ) -> None:
        self.agent = agent
        self.engine = engine
        self.site = site
        self.policy = policy
        self.builder = builder
        self.evidence = evidence
        self.tie_s = tie_s

    # ------------------------------------------------------------------ S1

    async def interpret(self, job: Job, trace: Trace) -> Interpretation | None:
        return await interpret_job(self.agent, self.site, job, trace)

    # ------------------------------------------------------------------ S2

    async def _advisories(
        self, vendor: str, product: str, fault: str, budget: SearchBudget, trace: Trace
    ) -> list[tuple[Evidence, VendorAdvisory | None]]:
        result = await self.evidence.vendor_advisories(vendor, product, fault, budget)
        out: list[tuple[Evidence, VendorAdvisory | None]] = []
        for ev in result.evidence[:2]:
            page = self._content(ev.content_sha256)
            advisory: VendorAdvisory | None = ev.extracted
            if page:
                try:
                    advisory = await self.agent.extract(page=page, fault_code=fault, trace=trace)
                except AgentFailed:
                    log.warning("agent.extract_fallback", url=ev.url, using="bulletin parser")
            out.append((ev, advisory))
        trace.tool(
            "search_vendor_advisories",
            {"vendor_urn": vendor, "product": product, "query": result.query},
            {
                "provider": result.provider,
                "cached": result.cached,
                "evidence": [
                    {"ref": e.content_sha256, "url": e.url, "extracted": a} for e, a in out
                ],
            },
        )
        return out

    def _content(self, digest: str) -> str | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT content FROM evidence WHERE content_sha256 = :h"), {"h": digest}
            ).first()
        return str(row.content) if row else None

    def _history(self, segment: str) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT incident_id, trigger, detected_at FROM incidents"
                    " WHERE segment_urn = :s ORDER BY detected_at DESC LIMIT 5"
                ),
                {"s": segment},
            ).all()
        return [
            {"incident_id": r.incident_id, "trigger": r.trigger, "at": r.detected_at} for r in rows
        ]

    async def outage(
        self,
        s: Situation,
        robot_job: Job,
        lift: str,
        mode: str,
        fault: str | None,
        event_id: str,
        trace: Trace,
    ) -> Plan | None:
        seg_urn = f"segment/{lift}"
        seg = self.site.segment(seg_urn)
        trace.tool("get_job", {"urn": robot_job.urn}, _job_view(robot_job))
        trace.tool(
            "get_site",
            {"segment": seg_urn},
            {"vendor_urn": seg.vendor_urn, "product": seg.product, "mode": mode, "fault": fault},
        )
        found: list[tuple[Evidence, VendorAdvisory | None]] = []
        if fault and seg.vendor_urn and seg.product:
            found = await self._advisories(
                seg.vendor_urn, seg.product, fault, SearchBudget(max_searches=3), trace
            )
        history = self._history(seg_urn)
        trace.tool("get_incident_history", {"segment": seg_urn}, history)
        refs = {e.content_sha256 for e, _ in found}
        estimate = await self.agent.outage(
            context={
                "lift": lift,
                "mode": mode,
                "fault_code": fault,
                "evidence": [
                    {"ref": e.content_sha256, "url": e.url, "extracted": a} for e, a in found
                ],
                "history": history,
            },
            evidence_refs=refs,
            trace=trace,
        )
        if not estimate.needs_technician and any(a and a.requires_manual_reset for _, a in found):
            # A validated, typed field of the vendor's bulletin outranks the model's reading.
            log.info("agent.outage_technician_from_evidence", lift=lift, fault=fault)
            estimate = estimate.model_copy(update={"needs_technician": True})
        options = self.builder.for_outage(s, robot_job, seg_urn, estimate.expected_outage_s)
        ranked = rank_options(options, s.jobs, self.policy, self.tie_s)
        trace.tool(
            "evaluate_option",
            {"outage_s": estimate.expected_outage_s, "options": len(ranked)},
            _options_view(ranked),
        )
        if not ranked:
            return None
        choice = await self.agent.choose(
            context={
                "job": _job_view(robot_job),
                "lift": lift,
                "outage": estimate.model_dump(),
                "options": _options_view(ranked),
            },
            option_ids={sc.option.option_id for sc in ranked},
            evidence_refs=refs,
            trace=trace,
        )
        chosen = next(sc for sc in ranked if sc.option.option_id == choice.option_id)
        return self._plan(
            robot_job,
            chosen.option,
            rationale=f"Lift {lift} {mode} {fault or ''}: expected outage "
            f"{estimate.expected_outage_s} s ({estimate.rationale}) {choice.rationale}",
            refs=sorted(set(choice.evidence_refs) | set(estimate.evidence_refs)),
            event_id=event_id,
            trace=trace,
        )

    # ------------------------------------------------------------------ S3

    async def tradeoff(
        self,
        s: Situation,
        conflict: Conflict,
        robot_job: Job | None,
        ranked: list[Scored],
        trace: Trace,
    ) -> Plan | None:
        """With a robot job: composite options for the delivery (and the people sharing the
        segment). Without one (two people of one class on a lift): only the order to suggest
        to staff, which a person approves (clinical-tie-needs-person)."""
        found = s.robot_for(robot_job) if robot_job is not None else None
        if robot_job is not None and found is None:
            return None
        segment = conflict.segment_urn
        people = sorted(
            (
                j
                for j in s.jobs.values()
                if j.kind is JobKind.HUMAN_TRANSPORT
                and any(c.job_urn == j.urn and c.segment_urn == segment for c in s.claims)
            ),
            key=self.policy.rank,
        )
        policy_order = [p.urn for p in people]
        supply: list[dict[str, Any]] = []
        handovers: dict[str, tuple[Any, Any]] = {}
        zones = self.builder.supply_zones(robot_job, found[0]) if robot_job and found else []
        for zone in zones:
            assert robot_job is not None and found is not None
            fleet, robot = found
            h = self.builder.handover(s, robot_job, fleet, zone, exclude=str(robot["name"]))
            if h is None:
                continue
            handovers[zone] = h
            supply.append(
                {
                    "urn": zone,
                    "name": self.site.zone(zone).name,
                    "handover_eta": h[0].eta,
                    "lateness_s": round(lateness(robot_job, h[0].eta)),
                }
            )
        if robot_job is not None:
            trace.tool(
                "list_candidate_robots",
                {"job": robot_job.urn},
                {"supply_zones": supply},
            )
        windows = {p.urn: s.human_window({p.urn}, segment) for p in people}
        trace.tool(
            "get_timeline",
            {"segment": segment},
            {u: [w.start, w.end] if w else None for u, w in windows.items()},
        )
        recipients = sorted(
            {TRANSPORT_DESK}
            | {
                j.requester_urn
                for j in [*([robot_job] if robot_job else []), *people]
                if j.requester_urn
            }
        )
        answer = await self.agent.tradeoff(
            context={
                "segment": segment,
                "robot_job": _job_view(robot_job) if robot_job else None,
                "people": [_person_view(p, windows.get(p.urn)) for p in people],
                "policy_order": policy_order,
                "template_options": _options_view(ranked),
                "supply_zones": supply,
                "recipients": recipients,
            },
            untrusted={f"notes {p.urn}": p.notes for p in people if p.notes},
            supply_zones=set(handovers),
            people=set(policy_order),
            recipients=set(recipients),
            policy_order=policy_order,
            trace=trace,
        )
        composites: list[Option] = []
        for prop in answer.proposals:
            actions: list[Action] = []
            robot_eta = ranked[0].option.effects[0].eta if ranked else None
            n = 0
            if prop.handover_from_zone:
                path, submit = handovers[prop.handover_from_zone]
                actions.append(submit)
                robot_eta, n = path.eta, 2
            for spec in prop.notify:
                actions.append(Notify(recipient_urn=spec.recipient_urn, message=spec.message))
            effects = (
                [
                    JobEffect(
                        job_urn=robot_job.urn,
                        eta=robot_eta,
                        lateness_s=lateness(robot_job, robot_eta) if robot_eta else 0.0,
                        delay_to_humans_s=0.0,
                        interventions=n,
                    )
                ]
                if robot_job is not None
                else []
            )
            free_at: datetime | None = None
            for urn in prop.people_sequence:
                w = windows.get(urn)
                delay = 0.0
                if w is not None:
                    if free_at is not None and free_at > w.start:
                        delay = (free_at - w.start).total_seconds()
                    free_at = w.end + timedelta(seconds=delay)
                effects.append(
                    JobEffect(
                        job_urn=urn,
                        eta=None,
                        lateness_s=0.0,
                        delay_to_humans_s=delay,
                        interventions=0,
                    )
                )
            composites.append(
                Option(
                    option_id=prop.option_id,
                    template=OptionTemplate.COMPOSITE,
                    actions=actions,
                    effects=effects,
                )
            )
        rescored = rank_options(composites, s.jobs, self.policy, self.tie_s)
        trace.tool("evaluate_option", {"composites": len(rescored)}, _options_view(rescored))
        chosen = next(o for o in composites if o.option_id == answer.chosen)
        if not chosen.actions:
            return None  # nothing to do or say
        return self._plan(
            robot_job,
            chosen,
            rationale=answer.rationale,
            refs=[],
            conflict_id=conflict.conflict_id,
            trace=trace,
        )

    # ------------------------------------------------------------------ plans

    def _plan(
        self,
        robot_job: Job | None,
        option: Option,
        *,
        rationale: str,
        refs: list[str],
        trace: Trace,
        event_id: str | None = None,
        conflict_id: str | None = None,
    ) -> Plan:
        actions = list(option.actions)
        robot = option.effects[0] if option.effects else None
        notified = {a.recipient_urn for a in actions if isinstance(a, Notify)}
        if (
            robot_job is not None
            and robot is not None
            and robot_job.requester_urn
            and robot_job.requester_urn not in notified
        ):
            actions.append(
                Notify(
                    recipient_urn=robot_job.requester_urn,
                    job_urn=robot_job.urn,
                    message=f"Your {robot_job.payload or 'delivery'}: "
                    + ("on its way" if robot.lateness_s <= 0 else "delayed")
                    + (f", new ETA {robot.eta:%H:%M}" if robot.eta else "")
                    + ".",
                    new_eta=robot.eta,
                )
            )
        now = datetime.now(UTC)
        subject = conflict_id or event_id or "x"
        return Plan(
            plan_id=f"ag-{subject}-{now:%H%M%S}"[:128],
            conflict_id=conflict_id,
            event_id=event_id,
            producer=Producer.AGENT,
            actions=actions,
            predicted=option.effects,
            rationale=(f"[{', '.join(trace.models())}] {rationale}")[:2000],
            evidence_refs=refs[:20],
        )

    # ------------------------------------------------------------------ reports

    async def report(self, inc: Incident, trace: Trace) -> IncidentReport:
        since = inc.detected_at - timedelta(minutes=15)
        segment = inc.segment_urn or ""
        lift = segment.removeprefix("segment/").split("-lobby-")[0] if segment else ""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT kind, payload, entry_hash, recorded_at FROM ledger"
                    " WHERE recorded_at >= :since AND kind IN ('facility_status', 'conflict',"
                    " 'incident', 'job_status', 'plan') ORDER BY seq DESC LIMIT 300"
                ),
                {"since": since},
            ).all()
        evidence: list[dict[str, Any]] = []
        for r in rows:
            p = dict(r.payload)
            what = ""
            if r.kind == "facility_status" and (not lift or p.get("lift_name") == lift):
                code = p.get("fault_code") or ""
                what = f"lift {p.get('lift_name')} reported {p.get('mode')} {code}"
            elif r.kind == "conflict" and p.get("event") == "detected":
                c = p.get("conflict") or {}
                if not segment or str(c.get("segment_urn", "")).startswith(f"segment/{lift}"):
                    what = f"conflict on {c.get('segment_urn')} between {c.get('job_urns')}"
            elif r.kind == "incident" and (p.get("incident") or {}).get("incident_id") != (
                inc.incident_id
            ):
                i = p.get("incident") or {}
                if i.get("segment_urn") == inc.segment_urn and inc.segment_urn:
                    what = f"earlier incident {i.get('trigger')} at {i.get('segment_urn')}"
            if what:
                evidence.append(
                    {"ref": r.entry_hash, "kind": r.kind, "at": r.recorded_at, "what": what}
                )
        evidence = evidence[:15]
        trace.tool("get_incident_history", {"incident": inc.incident_id}, evidence)
        draft = await self.agent.report(
            context={
                "incident": inc.model_dump(mode="json", exclude={"report"}),
                "robot": inc.robot_urn,
                "evidence": evidence,
            },
            refs={e["ref"] for e in evidence},
            trace=trace,
        )
        return IncidentReport(
            summary=draft.summary,
            causes=[
                PossibleCause(description=c.description, evidence_refs=c.evidence_refs)
                for c in draft.causes
            ],
            generated_at=datetime.now(UTC),
        )
