"""Forecaster and conflict detector (spec 3.3, 3.4, 5.1 steps 3-4). Deterministic.

Claims (who needs which segment, when):
- human transports: the fastest usable route from the route table (lift attributes and
  conditions applied) from origin to destination zone. The porter starts the route
  `status_lead_s[status]` after the status time (picked_up: at the status time); statuses not
  listed get no forecast. Windows are widened by `pad_s`; confidence is
  `status_confidence[status]`. A destination Nemotron inferred makes the claims `inferred`.
  No destination, no claim: a free-text destination stays invisible until interpreted;
- robot jobs (the robot's task_id links it): the ETA model's route from the robot's position
  through the pickup (until the robot has been there) to the dropoff, with the fleet's closed
  lanes removed (basis `eta_model`; Open-RMF publishes no planned path over ROS);
- every robot: an `observed` claim on the segments where it stands now.

Conflicts, per segment, between claims of different jobs overlapping in time:
- `overlap` on an exclusive segment (a lift);
- `required_clear` on a shared segment when the higher job's class is in policy
  `require_clear_classes` and the other job ranks lower; dropped when the same two jobs
  already have an overlap conflict (resolving that encounter covers its lobbies too).
Robot-robot pairs are left to Open-RMF's own traffic negotiation and lift sessions.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from auspex.core.canonical import sha256_hex
from auspex.core.models import (
    Claim,
    ClaimBasis,
    Conflict,
    ConflictKind,
    Job,
    JobKind,
    JobStatus,
    PrecedenceClass,
    TimeWindow,
)
from auspex.core.urns import make_urn
from auspex.forecast.eta import EtaModel, RobotPath
from auspex.policy.loader import Policy
from auspex.site.model import Site

OBSERVED_HOLD_S = 30.0  # an observed robot is assumed to stay this long (refreshed each tick)
OBSERVED_NEAR_M = 2.0  # only claim where the robot stands if it is this close to a vertex
PATH_CONFIDENCE_NOW = 0.85
PATH_CONFIDENCE_FLOOR = 0.5
PATH_CONFIDENCE_DECAY_S = 600.0  # confidence falls to the floor this far ahead
INACTIVE = {JobStatus.COMPLETED, JobStatus.CANCELLED}
ROBOT_PAD_S = 20.0
ROBOT_PAD_FRACTION = 0.15
STALE_AFTER_S = 600.0  # a transport this far past its planned route end claims nothing


def robot_urn(fleet: str, robot: str) -> str:
    return make_urn("robot", f"{fleet}-{robot}")


def robot_job_urn(fleet: str, robot: str) -> str:
    """Stand-in job for a robot with no linked hospital job (ranked as routine)."""
    return make_urn("job", f"robot-{fleet}-{robot}")


def is_robot(actor_urn: str) -> bool:
    return actor_urn.startswith("robot/")


def _claim_id(job: str, segment: str, basis: ClaimBasis, start: datetime) -> str:
    return "cl-" + sha256_hex(f"{job}|{segment}|{basis.value}|{start.isoformat()}".encode())[:16]


def _merge(
    pieces: Iterable[tuple[str, datetime, datetime]],
) -> list[tuple[str, datetime, datetime]]:
    """Consecutive pieces on the same segment become one window."""
    out: list[tuple[str, datetime, datetime]] = []
    for seg, a, b in pieces:
        if out and out[-1][0] == seg and a <= out[-1][2]:
            out[-1] = (seg, out[-1][1], max(b, out[-1][2]))
        else:
            out.append((seg, a, b))
    return out


# --------------------------------------------------------------------------- humans


def human_claims(
    job: Job, site: Site, now: datetime, unavailable: frozenset[str] = frozenset()
) -> list[Claim]:
    if job.kind is not JobKind.HUMAN_TRANSPORT or job.status in INACTIVE:
        return []
    if job.destination is None:
        return []
    if job.status is not JobStatus.PICKED_UP and job.status.value not in site.status_lead_s:
        return []
    usable = [
        c
        for c in site.candidate_routes(
            job.origin, job.destination, job.transport_mode, job.bed_type
        )
        if c.usable and not any(leg.segment in unavailable for leg in c.route.legs)
    ]
    if not usable:
        return []
    route = usable[0].route
    factor = site.mode_factor(job.transport_mode)
    if job.status is JobStatus.PICKED_UP:
        planned = job.status_at  # moving now; legs already walked fall out below
    else:
        planned = job.status_at + timedelta(seconds=site.status_lead_s[job.status.value])
    # A porter running late starts "now"; but a job whose whole route should have ended
    # STALE_AFTER_S ago is stale (its completion was never reported): no claims.
    planned_end = planned + timedelta(seconds=route.total_s * factor)
    if (now - planned_end).total_seconds() > STALE_AFTER_S:
        return []
    start = max(planned, now) if job.status is not JobStatus.PICKED_UP else planned
    pad = timedelta(seconds=site.pad_s)
    pieces = []
    t = start
    for leg in route.legs:
        dt = timedelta(seconds=leg.duration_s * factor)
        if leg.segment is not None:
            pieces.append((leg.segment, t - pad, t + dt + pad))
        t += dt
    inferred = "destination" in job.inferred_fields
    basis = ClaimBasis.INFERRED if inferred else ClaimBasis.ROUTE_TABLE
    confidence = site.status_confidence.get(job.status.value, 0.5)
    if inferred and job.inference_confidence is not None:
        confidence = round(confidence * job.inference_confidence, 3)
    actor = make_urn("porter", f"{job.source}-{job.external_id}")
    return [
        Claim(
            claim_id=_claim_id(job.urn, seg, basis, a),
            job_urn=job.urn,
            actor_urn=actor,
            segment_urn=seg,
            window=TimeWindow(start=a, end=b),
            confidence=confidence,
            basis=basis,
        )
        for seg, a, b in _merge(pieces)
        if b > now
    ]


# --------------------------------------------------------------------------- robots


def path_claims(job_urn: str, actor: str, path: RobotPath, now: datetime) -> list[Claim]:
    """Claims along the robot's forecast route. The ETA model is a point estimate (live S1:
    a robot reached its pickup ~50 s later than forecast), so each window is widened by
    ROBOT_PAD_S plus ROBOT_PAD_FRACTION of how far ahead it is; never back before now."""
    pieces = [(s, leg.enter, leg.exit) for leg in path.legs for s in leg.segments]
    pieces.sort(key=lambda p: p[1])
    out = []
    for seg, a0, b0 in _merge(pieces):
        ahead = max(0.0, (a0 - now).total_seconds())
        pad = timedelta(seconds=ROBOT_PAD_S + ROBOT_PAD_FRACTION * ahead)
        a, b = max(now, a0 - pad), b0 + pad
        confidence = max(
            PATH_CONFIDENCE_FLOOR,
            PATH_CONFIDENCE_NOW
            - (PATH_CONFIDENCE_NOW - PATH_CONFIDENCE_FLOOR)
            * min(1.0, ahead / PATH_CONFIDENCE_DECAY_S),
        )
        out.append(
            Claim(
                claim_id=_claim_id(job_urn, seg, ClaimBasis.ETA_MODEL, a0),
                job_urn=job_urn,
                actor_urn=actor,
                segment_urn=seg,
                window=TimeWindow(start=a, end=b if b > a else a + timedelta(milliseconds=1)),
                confidence=round(confidence, 3),
                basis=ClaimBasis.ETA_MODEL,
            )
        )
    return out


def robot_segments_at(
    site: Site, eta: EtaModel, fleet: str, level: str, x: float, y: float
) -> list[str]:
    """Segments whose lanes touch the vertex the robot stands at (within OBSERVED_NEAR_M)."""
    v = eta.nearest_vertex(fleet, level, x, y)
    if v is None or eta.distance_to(fleet, v, level, x, y) > OBSERVED_NEAR_M:
        return []
    out: list[str] = []
    for lane in eta.graphs[fleet].lanes:
        if v in (lane.v1, lane.v2):
            for s in site.segments_for_lane(fleet, lane.index):
                if s not in out:
                    out.append(s)
    return out


def zone_vertex(site: Site, eta: EtaModel, fleet: str, zone: str | None) -> int | None:
    """The zone's best waypoint in this fleet's graph."""
    if zone is None or zone not in site.zones:
        return None
    for wp in site.zone(zone).waypoints.get(fleet, []):
        v = eta.waypoint_vertex(fleet, wp)
        if v is not None:
            return v
    return None


def job_stops(
    site: Site, eta: EtaModel, fleet: str, job: Job, visited_origin: bool
) -> list[int] | None:
    """Remaining stops for a robot job: pickup (unless visited or not in this graph), dropoff."""
    goal = zone_vertex(site, eta, fleet, job.destination)
    if goal is None:
        return None
    pickup = None if visited_origin else zone_vertex(site, eta, fleet, job.origin)
    return [pickup, goal] if pickup is not None and pickup != goal else [goal]


def job_route(
    *,
    site: Site,
    eta: EtaModel,
    fleet: str,
    robot: dict[str, Any],
    job: Job,
    visited_origin: bool,
    closed: set[int],
    now: datetime,
    rtf: float,
) -> RobotPath | None:
    stops = job_stops(site, eta, fleet, job, visited_origin)
    start = eta.nearest_vertex(fleet, robot["level"], robot["x"], robot["y"])
    if stops is None or start is None:
        return None
    return eta.route(fleet, start, stops, closed, now, rtf)


def forecast(
    *,
    site: Site,
    eta: EtaModel,
    jobs: list[Job],
    fleets: dict[str, dict[str, Any]],
    closed: dict[str, set[int]],
    rtf: dict[str, float],
    visited: dict[str, bool],
    now: datetime,
    unavailable: frozenset[str] = frozenset(),
) -> tuple[list[Claim], dict[str, RobotPath]]:
    """All current and predicted claims, and each linked robot's route (keyed by job urn).

    `visited` maps an rmf task id to whether its robot has reached the job's pickup;
    `unavailable` are segments (faulted lifts) porters will not use. Robots are forecast as
    Open-RMF will route them: it knows nothing of a fault until lanes are closed."""
    claims: list[Claim] = []
    paths: dict[str, RobotPath] = {}
    by_task = {j.rmf_task_id: j for j in jobs if j.rmf_task_id and j.status not in INACTIVE}
    for job in jobs:
        claims.extend(human_claims(job, site, now, unavailable))
    for fleet, state in fleets.items():
        if fleet not in eta.graphs:
            continue
        for r in state.get("robots", []):
            actor = robot_urn(fleet, r["name"])
            task = r.get("task_id") or ""
            linked = by_task.get(task)
            job_urn = linked.urn if linked is not None else robot_job_urn(fleet, r["name"])
            for seg in robot_segments_at(site, eta, fleet, r["level"], r["x"], r["y"]):
                claims.append(
                    Claim(
                        claim_id=_claim_id(job_urn, seg, ClaimBasis.OBSERVED, now),
                        job_urn=job_urn,
                        actor_urn=actor,
                        segment_urn=seg,
                        window=TimeWindow(start=now, end=now + timedelta(seconds=OBSERVED_HOLD_S)),
                        confidence=1.0,
                        basis=ClaimBasis.OBSERVED,
                    )
                )
            if linked is None or linked.kind is not JobKind.ROBOT_TASK:
                continue
            path = job_route(
                site=site,
                eta=eta,
                fleet=fleet,
                robot=r,
                job=linked,
                visited_origin=visited.get(task, False),
                closed=closed.get(fleet, set()),
                now=now,
                rtf=rtf.get(fleet, 1.0),
            )
            if path is None:
                continue
            paths[linked.urn] = path
            claims.extend(path_claims(linked.urn, actor, path, now))
    return claims, paths


def in_horizon(claims: list[Claim], now: datetime, horizon_s: float) -> list[Claim]:
    end = now + timedelta(seconds=horizon_s)
    return [c for c in claims if c.window.end > now and c.window.start < end]


# --------------------------------------------------------------------------- conflicts


def detect_conflicts(
    *,
    claims: list[Claim],
    jobs: dict[str, Job],
    site: Site,
    policy: Policy,
    now: datetime,
) -> list[Conflict]:
    routine = policy.rank(_Rankable(PrecedenceClass.ROUTINE))

    def rank(urn: str) -> int:
        job = jobs.get(urn)
        return policy.rank(job) if job is not None else routine

    def klass(urn: str) -> PrecedenceClass:
        job = jobs.get(urn)
        return job.precedence if job is not None else PrecedenceClass.ROUTINE

    by_seg: dict[str, list[Claim]] = {}
    for c in claims:
        by_seg.setdefault(c.segment_urn, []).append(c)
    found: dict[str, Conflict] = {}
    for seg_urn, seg_claims in by_seg.items():
        seg = site.segments.get(seg_urn)
        if seg is None:
            continue
        seg_claims.sort(key=lambda c: c.window.start)
        for i, a in enumerate(seg_claims):
            for b in seg_claims[i + 1 :]:
                if b.window.start >= a.window.end:
                    break
                if a.job_urn == b.job_urn or a.actor_urn == b.actor_urn:
                    continue
                if is_robot(a.actor_urn) and is_robot(b.actor_urn):
                    continue
                if seg.exclusive:
                    kind = ConflictKind.OVERLAP
                else:
                    hi, lo = (a, b) if rank(a.job_urn) <= rank(b.job_urn) else (b, a)
                    if klass(hi.job_urn) not in policy.require_clear_classes:
                        continue
                    if rank(lo.job_urn) <= rank(hi.job_urn):
                        continue
                    kind = ConflictKind.REQUIRED_CLEAR
                pair = sorted([a.job_urn, b.job_urn])
                cid = "cf-" + sha256_hex(f"{seg_urn}|{pair[0]}|{pair[1]}".encode())[:16]
                window = TimeWindow(
                    start=max(a.window.start, b.window.start),
                    end=min(a.window.end, b.window.end),
                )
                claim_ids = {a.claim_id, b.claim_id}
                prev = found.get(cid)
                if prev is not None:
                    window = TimeWindow(
                        start=min(prev.window.start, window.start),
                        end=max(prev.window.end, window.end),
                    )
                    claim_ids |= set(prev.claim_ids)
                found[cid] = Conflict(
                    conflict_id=cid,
                    kind=kind,
                    segment_urn=seg_urn,
                    claim_ids=sorted(claim_ids),
                    job_urns=pair,
                    window=window,
                    detected_at=now,
                )
    overlaps = [c for c in found.values() if c.kind is ConflictKind.OVERLAP]
    out = [
        c
        for c in found.values()
        if c.kind is ConflictKind.OVERLAP or not any(o.job_urns == c.job_urns for o in overlaps)
    ]
    return sorted(out, key=lambda c: c.window.start)


class _Rankable:
    def __init__(self, cls: PrecedenceClass) -> None:
        self.precedence = cls
        self.destination_type: str | None = None
        self.payload: str | None = None
