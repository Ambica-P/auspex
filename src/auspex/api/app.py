"""Core REST API (spec 11) and process lifecycle.

Intake endpoints validate a source record fully (schema, codes, priority, status) before
queueing it, so a sender gets its error immediately; the consumer applies it. Every route
needs the admin bearer token except GET /health (liveness only, no details).
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import Engine, text

from auspex.agent.agent import Agent
from auspex.agent.flows import AgentFlows
from auspex.agent.llm import make_llm
from auspex.core.config import Settings, get_settings
from auspex.core.db import make_engine
from auspex.core.errors import AuspexError
from auspex.core.logging import configure_logging, get_logger
from auspex.core.models import Plan
from auspex.core.urns import make_urn
from auspex.core.version import CODE_FINGERPRINT
from auspex.decide.engine import CONFIGS, Config, DecisionEngine
from auspex.decide.evaluator import OptionBuilder
from auspex.decide.playbooks import load_playbooks
from auspex.evidence.service import BudgetExceeded, EvidenceService, SearchBudget, make_search
from auspex.execute.executor import ExecutionRefused, Executor, gate_inputs
from auspex.forecast.engine import PictureLoop, incidents_from_db
from auspex.forecast.eta import EtaModel
from auspex.ingest.consumer import Consumer
from auspex.ingest.events import content_event_id, insert_event
from auspex.ingest.facility import LiftStatus
from auspex.ingest.hospital import SourceRecord, StatusUpdate, map_status, normalize
from auspex.ledger.store import verify_chain
from auspex.monitor.progress import ProgressMonitor
from auspex.picture.jobs import get_job, list_jobs
from auspex.picture.ros_state import LIFT_MODE_CODES, RosState
from auspex.policy.approval import ApprovalError, Approvals
from auspex.policy.loader import Policy, load_policy
from auspex.site.model import DEFAULT_SITE_DIR, Site, build_site
from auspex.site.navgraph import NavGraph, load_captures

log = get_logger(__name__)
bearer = HTTPBearer(auto_error=False)


@dataclass
class Core:
    settings: Settings
    engine: Engine
    site: Site
    policy: Policy
    ros: RosState
    consumer: Consumer
    picture: PictureLoop
    executor: Executor
    approvals: Approvals
    graphs: dict[str, NavGraph]
    decide: DecisionEngine
    evidence: EvidenceService


def build_core(settings: Settings) -> Core:
    graphs = load_captures(DEFAULT_SITE_DIR / "nav_graphs")
    site = build_site(DEFAULT_SITE_DIR, graphs)
    policy = load_policy()
    engine = make_engine(settings.database_url.get_secret_value())
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    ros = RosState(graphs)
    consumer = Consumer(
        engine=engine,
        app_url=settings.database_url.get_secret_value(),
        site=site,
        policy=policy,
        ros=ros,
        batch_size=settings.event_batch_size,
        ros_retention_s=settings.ros_event_retention_s,
    )
    eta = EtaModel(site, graphs)
    monitor = ProgressMonitor(
        site=site,
        eta=eta,
        policy=policy,
        stall_threshold_s=settings.stall_threshold_s,
        eta_slip_tolerance_s=settings.eta_slip_tolerance_s,
    )
    picture = PictureLoop(
        engine=engine,
        site=site,
        policy=policy,
        ros=ros,
        eta=eta,
        monitor=monitor,
        horizon_s=settings.horizon_s,
        tick_s=settings.picture_tick_ms / 1000,
    )
    executor = Executor(
        engine=engine,
        site=site,
        policy=policy,
        graphs=graphs,
        ros=ros,
        paths=lambda: picture.snapshot().paths,
    )
    approvals = Approvals(
        engine=engine,
        site=site,
        policy=policy,
        executor=executor,
        timeout_s=settings.approval_timeout_s,
    )
    evidence = EvidenceService(
        engine=engine,
        search=make_search(settings),
        provider=settings.search_provider,
        vendor_site_url=str(settings.vendor_site_url) if settings.vendor_site_url else None,
        ttl_s=settings.tavily_cache_ttl_s,
    )
    decide = DecisionEngine(
        engine=engine,
        site=site,
        policy=policy,
        graphs=graphs,
        eta=eta,
        ros=ros,
        monitor=monitor,
        picture=picture.snapshot,
        approvals=approvals,
        playbooks=load_playbooks(),
        config=settings.auspex_config,
        fast_lane_budget_ms=settings.fast_lane_budget_ms,
        tie_tolerance_s=settings.tie_tolerance_s,
        flows=AgentFlows(
            agent=Agent(make_llm(settings), debug=settings.llm_debug),
            engine=engine,
            site=site,
            policy=policy,
            builder=OptionBuilder(site=site, eta=eta, pad_s=30.0),
            evidence=evidence,
            tie_s=settings.tie_tolerance_s,
        ),
        agent_budget_s=settings.agent_budget_s,
    )
    picture.on_change = decide.on_picture
    consumer.on_lift = decide.on_lift
    consumer.on_job = decide.on_job
    return Core(
        settings,
        engine,
        site,
        policy,
        ros,
        consumer,
        picture,
        executor,
        approvals,
        graphs,
        decide,
        evidence,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    core = build_core(settings)
    app.state.core = core
    core.consumer.start()
    core.picture.start()
    core.executor.start()
    core.decide.start()
    stop_approvals = _start_approval_timer(core)
    log.info("core.started", url=settings.core_url)
    try:
        yield
    finally:
        stop_approvals()
        core.decide.stop()
        core.executor.stop()
        core.picture.stop()
        core.consumer.stop()
        core.engine.dispose()
        log.info("core.stopped")


def _start_approval_timer(core: Core) -> Callable[[], None]:
    """Expire plans nobody approved in time (APPROVAL_TIMEOUT_S)."""
    stop = threading.Event()

    def run() -> None:
        while not stop.wait(5.0):
            try:
                core.approvals.tick()
            except Exception:
                log.exception("approvals.tick_failed")

    threading.Thread(target=run, name="approval-timer", daemon=True).start()
    return stop.set


def get_core(request: Request) -> Core:
    core: Core = request.app.state.core
    return core


def require_admin(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    core = get_core(request)
    expected = core.settings.admin_token.get_secret_value()
    if creds is None or not secrets.compare_digest(creds.credentials, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid bearer token")


CoreDep = Annotated[Core, Depends(get_core)]
admin = [Depends(require_admin)]
app = FastAPI(title="Auspex core", version="0.2.1", lifespan=lifespan)


def _reject(exc: AuspexError | ValidationError) -> HTTPException:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        detail = f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}"
    else:
        detail = str(exc)
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/health", dependencies=admin)
def health_detail(core: CoreDep) -> dict[str, Any]:
    pic = core.picture.snapshot()
    with core.engine.connect() as conn:
        pending = conn.execute(
            text("SELECT count(*) FROM events WHERE processed_at IS NULL")
        ).scalar_one()
        failed = conn.execute(
            text("SELECT count(*) FROM events WHERE error IS NOT NULL")
        ).scalar_one()
        jobs = conn.execute(text("SELECT count(*) FROM jobs")).scalar_one()
    return {
        "status": "ok",
        "config": core.decide.config,
        "llm_provider": core.settings.llm_provider.value,
        "search_provider": core.settings.search_provider.value,
        "llm_debug": core.settings.llm_debug,
        "code": CODE_FINGERPRINT,
        "models": {"triage": core.settings.model_triage, "reason": core.settings.model_reason}
        if core.settings.llm_provider.value != "fake"
        else {"triage": "fake-deterministic", "reason": "fake-deterministic"},
        "events": {"pending": pending, "failed": failed},
        "jobs": jobs,
        "consumer": core.consumer.stats.as_dict(),
        "ros": core.ros.snapshot(),
        "picture": {
            "at": pic.at.isoformat() if pic.at else None,
            "tick_ms": pic.tick_ms,
            "claims": len(pic.claims),
            "open_conflicts": len(pic.conflicts),
            "open_incidents": len(pic.incidents),
        },
    }


@app.post("/api/v1/jobs", status_code=status.HTTP_202_ACCEPTED, dependencies=admin)
def post_job(record: SourceRecord, core: CoreDep) -> dict[str, Any]:
    try:
        job = normalize(record, core.site, core.policy)  # full validation before queueing
    except (AuspexError, ValidationError) as exc:
        log.warning("intake.rejected", request_id=record.request_id, error=str(exc)[:300])
        raise _reject(exc) from None
    payload = {"record": record.model_dump(mode="json")}
    event_id = content_event_id("hospital", "record", payload)
    with core.engine.begin() as conn:
        inserted = insert_event(conn, event_id, "hospital", "record", payload)
    log.info("intake.queued", urn=job.urn, event_id=event_id, duplicate=not inserted)
    return {"event_id": event_id, "job_urn": job.urn, "duplicate": not inserted}


@app.post("/api/v1/jobs/{job_id}/status", status_code=status.HTTP_202_ACCEPTED, dependencies=admin)
def post_status(job_id: str, update: StatusUpdate, core: CoreDep) -> dict[str, Any]:
    try:
        urn = make_urn("job", job_id)
    except AuspexError as exc:
        raise _reject(exc) from None
    with core.engine.connect() as conn:
        found = get_job(conn, urn)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown job {urn}")
    try:
        map_status(found[0].source, update.status)
    except AuspexError as exc:
        raise _reject(exc) from None
    payload = {"job_urn": urn, "update": update.model_dump(mode="json")}
    event_id = content_event_id("hospital", "status", payload)
    with core.engine.begin() as conn:
        inserted = insert_event(conn, event_id, "hospital", "status", payload)
    log.info("intake.status_queued", urn=urn, status=update.status, duplicate=not inserted)
    return {"event_id": event_id, "job_urn": urn, "duplicate": not inserted}


@app.post(
    "/api/v1/facility/lifts/{lift_name}",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=admin,
)
def post_lift_status(lift_name: str, update: LiftStatus, core: CoreDep) -> dict[str, Any]:
    if f"segment/{lift_name}" not in core.site.segments:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown lift {lift_name}")
    payload = {"lift_name": lift_name, "status": update.model_dump(mode="json")}
    event_id = content_event_id("facility", "lift_status", payload)
    with core.engine.begin() as conn:
        inserted = insert_event(conn, event_id, "facility", "lift_status", payload)
    log.info("intake.lift_status_queued", lift=lift_name, mode=update.mode, duplicate=not inserted)
    return {"event_id": event_id, "duplicate": not inserted}


@app.get("/api/v1/jobs", dependencies=admin)
def get_jobs(core: CoreDep, limit: int = 200) -> list[dict[str, Any]]:
    with core.engine.connect() as conn:
        rows = list_jobs(conn, min(max(limit, 1), 1000))
    return [{"job": j.model_dump(mode="json"), "raw": raw} for j, raw in rows]


@app.get("/api/v1/jobs/{job_id}", dependencies=admin)
def get_one_job(job_id: str, core: CoreDep) -> dict[str, Any]:
    with core.engine.connect() as conn:
        found = get_job(conn, make_urn("job", job_id))
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown job job/{job_id}")
    return {"job": found[0].model_dump(mode="json"), "raw": found[1]}


@app.get("/api/v1/ledger/verify", dependencies=admin)
def ledger_verify(core: CoreDep) -> dict[str, Any]:
    try:
        count = verify_chain(core.engine)
    except AuspexError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "entries": count}


@app.get("/api/v1/timeline", dependencies=admin)
def get_timeline(core: CoreDep) -> dict[str, Any]:
    pic = core.picture.snapshot()
    by_segment: dict[str, list[dict[str, Any]]] = {}
    for c in sorted(pic.claims, key=lambda c: c.window.start):
        by_segment.setdefault(c.segment_urn, []).append(c.model_dump(mode="json"))
    return {
        "at": pic.at.isoformat() if pic.at else None,
        "horizon_s": core.settings.horizon_s,
        "real_time_factor": pic.rtf,
        "segments": by_segment,
        "robot_routes": {
            job: {
                "fleet": p.fleet,
                "eta": p.eta.isoformat(),
                "total_s": round(p.total_s, 1),
                "lifts": p.lifts(),
            }
            for job, p in pic.paths.items()
        },
    }


@app.get("/api/v1/conflicts", dependencies=admin)
def get_conflicts(core: CoreDep) -> list[dict[str, Any]]:
    return [c.model_dump(mode="json") for c in core.picture.snapshot().conflicts]


@app.get("/api/v1/incidents", dependencies=admin)
def get_incidents(core: CoreDep, limit: int = 50) -> dict[str, Any]:
    pic = core.picture.snapshot()
    with core.engine.connect() as conn:
        recent = incidents_from_db(conn, min(max(limit, 1), 500))
    return {
        "open": [{**i.model_dump(mode="json"), "context": ctx} for i, ctx in pic.incidents],
        "recent": recent,
    }


@app.get("/api/v1/incidents/{incident_id}", dependencies=admin)
def get_incident(incident_id: str, core: CoreDep) -> dict[str, Any]:
    with core.engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM incidents WHERE incident_id = :id"), {"id": incident_id}
        ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown incident {incident_id}")
    return dict(row.data)


# --------------------------------------------------------------------------- plans (M7)


class ApproveBody(BaseModel):
    approver: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._@\- ]+$")


class RejectBody(ApproveBody):
    reason: str = Field(default="", max_length=300)


def _plan_row(conn: Any, plan_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            "SELECT plan_id, status, producer, subject, plan, decision, approval, verification,"
            " created_at, updated_at FROM plans WHERE plan_id = :id"
        ),
        {"id": plan_id},
    ).first()
    if row is None:
        return None
    commands = conn.execute(
        text(
            "SELECT command_id, action, side, status, seq, inverse_of, not_before, attempts,"
            " payload, result FROM commands WHERE plan_id = :id ORDER BY seq"
        ),
        {"id": plan_id},
    ).all()
    out = dict(row._mapping)
    out["created_at"] = row.created_at.isoformat()
    out["updated_at"] = row.updated_at.isoformat()
    out["commands"] = [
        {
            **dict(c._mapping),
            "not_before": c.not_before.isoformat() if c.not_before else None,
        }
        for c in commands
    ]
    return out


@app.post("/api/v1/plans", status_code=status.HTTP_201_CREATED, dependencies=admin)
def post_plan(plan: Plan, core: CoreDep) -> dict[str, Any]:
    """Submit a plan (an operator's, or a producer's from M8). It goes through the gate."""
    with core.engine.connect() as conn:
        inputs = gate_inputs(conn, core.policy, core.site, core.graphs, core.ros.state())
    try:
        decision = core.approvals.submit(plan, inputs)
    except ApprovalError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except ExecutionRefused as exc:
        raise _reject(exc) from None
    with core.engine.connect() as conn:
        row = _plan_row(conn, plan.plan_id)
    return {"decision": decision.model_dump(mode="json"), "status": (row or {}).get("status")}


@app.get("/api/v1/plans", dependencies=admin)
def get_plans(core: CoreDep, limit: int = 50) -> list[dict[str, Any]]:
    with core.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT plan_id, status, producer, subject, decision, created_at, updated_at"
                " FROM plans ORDER BY created_at DESC LIMIT :n"
            ),
            {"n": min(max(limit, 1), 500)},
        ).all()
    return [
        {
            **dict(r._mapping),
            "created_at": r.created_at.isoformat(),
            "updated_at": r.updated_at.isoformat(),
        }
        for r in rows
    ]


@app.get("/api/v1/plans/{plan_id}", dependencies=admin)
def get_plan(plan_id: str, core: CoreDep) -> dict[str, Any]:
    with core.engine.connect() as conn:
        row = _plan_row(conn, plan_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown plan {plan_id}")
    return row


@app.post("/api/v1/plans/{plan_id}/approve", dependencies=admin)
def approve_plan(plan_id: str, body: ApproveBody, core: CoreDep) -> dict[str, Any]:
    try:
        core.approvals.approve(plan_id, body.approver)
    except ApprovalError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except ExecutionRefused as exc:
        raise _reject(exc) from None
    return get_plan(plan_id, core)


@app.post("/api/v1/plans/{plan_id}/reject", dependencies=admin)
def reject_plan(plan_id: str, body: RejectBody, core: CoreDep) -> dict[str, Any]:
    try:
        core.approvals.reject(plan_id, body.approver, body.reason)
    except ApprovalError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return get_plan(plan_id, core)


@app.get("/api/v1/notifications", dependencies=admin)
def get_notifications(core: CoreDep) -> list[dict[str, Any]]:
    return list(core.executor.notifications)[::-1]


@app.get("/api/v1/ledger", dependencies=admin)
def get_ledger(core: CoreDep, limit: int = 100, kind: str | None = None) -> list[dict[str, Any]]:
    with core.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT seq, kind, recorded_at, payload, entry_hash FROM ledger"
                " WHERE (CAST(:kind AS text) IS NULL OR kind = :kind)"
                " ORDER BY seq DESC LIMIT :n"
            ),
            {"kind": kind, "n": min(max(limit, 1), 1000)},
        ).all()
    return [
        {
            "seq": r.seq,
            "kind": r.kind,
            "recorded_at": r.recorded_at.isoformat(),
            "payload": r.payload,
            "entry_hash": r.entry_hash,
        }
        for r in rows
    ]


@app.get("/api/v1/ledger/export", dependencies=admin)
def export_ledger(core: CoreDep, after_seq: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
    """Entries after `after_seq`, oldest first, with the stored canonical body and both
    hashes, so a reader can recompute sha256(prev_hash || body) without this server."""
    with core.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT seq, entry_id, kind, recorded_at, body, prev_hash, entry_hash FROM ledger"
                " WHERE seq > :after ORDER BY seq LIMIT :n"
            ),
            {"after": max(after_seq, 0), "n": min(max(limit, 1), 5000)},
        ).all()
    return [
        {
            "seq": r.seq,
            "entry_id": str(r.entry_id),
            "kind": r.kind,
            "recorded_at": r.recorded_at.isoformat(),
            "body": r.body,
            "prev_hash": r.prev_hash,
            "entry_hash": r.entry_hash,
        }
        for r in rows
    ]


# --------------------------------------------------------------------------- configuration (M8)


class ConfigBody(BaseModel):
    config: Config
    by: str = Field(default="api", min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._@\- ]+$")


@app.get("/api/v1/config", dependencies=admin)
def get_config(core: CoreDep) -> dict[str, Any]:
    return {"config": core.decide.config, "configs": list(CONFIGS)}


@app.put("/api/v1/config", dependencies=admin)
def put_config(body: ConfigBody, core: CoreDep) -> dict[str, Any]:
    """A: observe only; B: deterministic; C: B + Nemotron (M10). Recorded in the ledger."""
    if body.config != core.decide.config:
        core.decide.set_config(body.config, body.by)
    return {"config": core.decide.config}


# --------------------------------------------------------------------------- evidence (M9)


class AdvisoryBody(BaseModel):
    vendor_urn: str = Field(min_length=1, max_length=100, pattern=r"^vendor/[a-z0-9\-]+$")
    product: str = Field(min_length=1, max_length=60, pattern=r"^[A-Za-z0-9._\-]+$")
    fault_code: str = Field(min_length=1, max_length=30, pattern=r"^[A-Za-z0-9._\-]+$")


@app.post("/api/v1/evidence/advisories", dependencies=admin)
async def post_advisories(body: AdvisoryBody, core: CoreDep) -> dict[str, Any]:
    """Look up a vendor's bulletins for a fault code (one search; cached by query)."""
    try:
        result = await core.evidence.vendor_advisories(
            body.vendor_urn, body.product, body.fault_code, SearchBudget(max_searches=1)
        )
    except BudgetExceeded as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from None
    except AuspexError as exc:
        raise _reject(exc) from None
    return {
        "query": result.query,
        "provider": result.provider,
        "cached": result.cached,
        "evidence": [e.model_dump(mode="json") for e in result.evidence],
    }


@app.get("/api/v1/evidence/{content_sha256}", dependencies=admin)
def get_evidence(content_sha256: str, core: CoreDep) -> dict[str, Any]:
    """Stored evidence by content hash, with its latest extraction (every extraction is in the
    ledger). `content` is the untrusted source text, returned as data."""
    with core.engine.connect() as conn:
        row = conn.execute(
            text("SELECT data, content, query FROM evidence WHERE content_sha256 = :h"),
            {"h": content_sha256},
        ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown evidence {content_sha256}")
    return {"evidence": dict(row.data), "query": row.query, "content": row.content}


# --------------------------------------------------------------------------- ops console (M11)

UI_DIR = DEFAULT_SITE_DIR.parent / "ui"
app.mount("/ui", StaticFiles(directory=UI_DIR, html=True, check_dir=False), name="ui")


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/ui/")


@app.get("/api/v1/map", dependencies=admin)
def get_map(core: CoreDep) -> dict[str, Any]:
    """Navigation graphs (static), robots, closed lanes and lift modes (live), for the map."""
    state = core.ros.state()
    fleets: dict[str, Any] = {}
    for fleet, g in core.graphs.items():
        fleets[fleet] = {
            "vertices": [
                {
                    "i": v.index,
                    "x": round(v.x, 2),
                    "y": round(v.y, 2),
                    "level": v.level,
                    "name": v.name,
                }
                for v in g.vertices
            ],
            "lanes": [[lane.v1, lane.v2, lane.index] for lane in g.lanes],
            "closed": sorted(state.closed.get(fleet, set())),
        }
    robots = [
        {
            "fleet": fleet,
            "name": r.get("name"),
            "level": r.get("level"),
            "x": r.get("x"),
            "y": r.get("y"),
            "task_id": r.get("task_id") or None,
        }
        for fleet, s in state.fleets.items()
        for r in s.get("robots", [])
    ]
    lifts = {
        name: {
            "mode": {v: k for k, v in LIFT_MODE_CODES.items()}.get(
                int(lift.get("current_mode", 0)), "UNKNOWN"
            ),
            "fault_code": lift.get("fault_code"),
            "floor": lift.get("current_floor"),
            "session": lift.get("session_id") or None,
        }
        for name, lift in state.lifts.items()
    }
    segments = {
        urn: {"kind": seg.kind, "name": seg.name, "lanes": seg.rmf_lanes}
        for urn, seg in core.site.segments.items()
        if seg.kind in ("lift", "corridor")
    }
    return {"fleets": fleets, "robots": robots, "lifts": lifts, "segments": segments}


@app.get("/api/v1/plans/{plan_id}/context", dependencies=admin)
def get_plan_context(plan_id: str, core: CoreDep) -> dict[str, Any]:
    """Everything the decision panel shows for one plan: the plan, its conflict, the jobs as
    received (raw) and as interpreted, the options with evaluator scores, the agent's
    reasoning trace (tool calls and results, model calls) and the evidence it cited."""
    with core.engine.connect() as conn:
        row = _plan_row(conn, plan_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown plan {plan_id}")
        plan = dict(row["plan"])
        subject = row["subject"]
        conflict = None
        if plan.get("conflict_id"):
            c = conn.execute(
                text("SELECT data FROM conflicts WHERE conflict_id = :id"),
                {"id": plan["conflict_id"]},
            ).first()
            conflict = dict(c.data) if c else None
        decisions = [
            {"seq": r.seq, "recorded_at": r.recorded_at.isoformat(), **dict(r.payload)}
            for r in conn.execute(
                # Conflict ids repeat when the same jobs meet again (a scenario run again), so
                # only the decision records made just before this plan belong to it.
                text(
                    "SELECT seq, recorded_at, payload FROM ledger WHERE kind = 'options' AND"
                    " (payload->>'plan_id' = :p OR ((payload->>'conflict_id' = :s"
                    "  OR payload->>'subject' = :s) AND recorded_at BETWEEN"
                    "  CAST(:at AS timestamptz) - interval '3 minutes'"
                    "  AND CAST(:at AS timestamptz) + interval '5 seconds')) ORDER BY seq"
                ),
                {"s": subject or "", "p": plan_id, "at": row["created_at"]},
            ).all()
        ]
        urns = sorted(
            {e["job_urn"] for e in plan.get("predicted", [])}
            | set((conflict or {}).get("job_urns", []))
            | {a["job_urn"] for a in plan.get("actions", []) if a.get("job_urn")}
        )
        jobs = []
        for urn in urns:
            found = get_job(conn, urn)
            if found is None:
                continue
            # The interpretation shown is the one behind the job's current inferred fields
            # (none when this job, reopened, has not been interpreted again).
            interp = (
                conn.execute(
                    text(
                        "SELECT payload FROM ledger WHERE kind = 'interpretation'"
                        " AND payload->>'urn' = :u ORDER BY seq DESC LIMIT 1"
                    ),
                    {"u": urn},
                ).first()
                if found[0].inferred_fields
                else None
            )
            jobs.append(
                {
                    "job": found[0].model_dump(mode="json"),
                    "raw": found[1],
                    "interpretation": dict(interp.payload) if interp else None,
                }
            )
        refs = list(plan.get("evidence_refs", []))
        for d in decisions:
            for step in d.get("trace") or []:
                if step.get("tool") == "search_vendor_advisories":
                    refs += [e["ref"] for e in (step.get("result") or {}).get("evidence", [])]
        evidence = [
            {"content_sha256": r.content_sha256, **dict(r.data)}
            for r in conn.execute(
                text("SELECT content_sha256, data FROM evidence WHERE content_sha256 = ANY(:h)"),
                {"h": sorted(set(refs))},
            ).all()
        ]
    return {
        "plan": row,
        "decision_latency_ms": core.decide.latency_ms.get(plan_id),
        "conflict": conflict,
        "jobs": jobs,
        "decisions": decisions,
        "evidence": evidence,
    }
