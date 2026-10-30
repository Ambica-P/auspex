"""Progress monitor and incident detector (spec 5.2). Deterministic; time is passed in.

Per robot with a task, each tick:
- progress: moving more than PROGRESS_M resets the stall clock (and resolves an open incident);
  reaching the job's pickup (within VISIT_M) is recorded with the forecast's predicted time
  (forecast vs actual, for calibrating the ETA model);
- explained wait (not a stall): charging or docking; the robot's own lift request (its adapter
  re-publishes it every second while waiting) or lift session, while that lift is in AGV mode
  or HUMAN mode (people using it); robot mode WAITING (traffic). Every explained wait is
  capped at WAITING_CAP x the stall threshold, after which it counts as a stall;
- NOT explained: requesting, or standing at the landing of, a lift that is OFFLINE, FIRE,
  EMERGENCY or UNKNOWN (the fault is recorded as context for the agent and the report);
- triggers for a robot standing still: `blocked_status` after BLOCKED_GRACE_S while requesting
  (or at the landing of) a faulted lift; `no_progress` after the stall threshold while not
  explained. For a robot that is moving: `eta_slip` when its ETA (recomputed from the current
  position) passes the expected finish + tolerance. A stall incident resolves when the robot
  moves, and the expected finish is then reset to the new ETA (the stall already accounts for
  its delay); a slip incident resolves when its ETA is back within half the tolerance.
  Open-RMF's task status `blocked` is not visible over ROS (it travels to the API server,
  whose token is unavailable), so it is not a trigger here.

Stakes decide what happens: a job in policy `incident_classes`, or with slack below
`min_slack_s`, opens an incident; anything else (including robots with no linked job) is
logged once per stall and waits.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from auspex.core.logging import get_logger
from auspex.core.models import (
    Incident,
    IncidentStatus,
    IncidentTrigger,
    Job,
    PrecedenceClass,
    Stakes,
)
from auspex.forecast.eta import LIFT_ENTER, EtaModel, RobotPath
from auspex.forecast.forecaster import job_route, robot_segments_at, robot_urn, zone_vertex
from auspex.policy.loader import Policy
from auspex.site.model import Site

log = get_logger(__name__)

PROGRESS_M = 0.5
VISIT_M = 1.0
WAITING_CAP = 3.0
LIFT_NEAR_M = 4.0
BLOCKED_GRACE_S = 10.0
LIFT_REQUEST_FRESH_S = 10.0  # requests arrive every 5 s from the bridge while active
MODE_CHARGING, MODE_WAITING, MODE_DOCKING = 1, 4, 7
LIFT_HUMAN, LIFT_AGV = 1, 2
LIFT_MODES = {0: "UNKNOWN", 1: "HUMAN", 2: "AGV", 3: "FIRE", 4: "OFFLINE", 5: "EMERGENCY"}
LIFT_FAULT = {0, 3, 4, 5}
STALL_TRIGGERS = {IncidentTrigger.NO_PROGRESS, IncidentTrigger.BLOCKED_STATUS}


@dataclass
class Track:
    fleet: str
    robot: str
    task_id: str
    started_at: datetime
    pos: tuple[str, float, float]
    progress_at: datetime
    expected_finish: datetime | None
    incident: Incident | None = None
    routine_logged: bool = False
    context: dict[str, Any] = field(default_factory=dict)
    visited_origin: bool = False
    predicted_pickup: datetime | None = None
    lift_held: str | None = None


@dataclass
class MonitorEvent:
    kind: Literal["opened", "resolved"]
    incident: Incident


class ProgressMonitor:
    def __init__(
        self,
        *,
        site: Site,
        eta: EtaModel,
        policy: Policy,
        stall_threshold_s: float,
        eta_slip_tolerance_s: float,
    ) -> None:
        self.site = site
        self.eta = eta
        self.policy = policy
        self.stall_s = stall_threshold_s
        self.slip_s = eta_slip_tolerance_s
        self.tracks: dict[str, Track] = {}
        self.calibration: list[dict[str, Any]] = []  # forecast vs actual, drained by the loop
        self.landings: dict[tuple[str, str], list[tuple[str, float, float]]] = {}
        for fleet, g in eta.graphs.items():
            for lane in g.lanes:
                lift = lane.params.get(LIFT_ENTER)
                if isinstance(lift, str):
                    v = g.vertex(lane.v1)
                    self.landings.setdefault((fleet, v.level), []).append((lift, v.x, v.y))

    # ------------------------------------------------------------------ helpers

    def _near_lift(self, fleet: str, level: str, x: float, y: float) -> str | None:
        for lift, lx, ly in self.landings.get((fleet, level), []):
            if math.hypot(lx - x, ly - y) <= LIFT_NEAR_M:
                return lift
        return None

    def explain(
        self,
        fleet: str,
        r: dict[str, Any],
        lifts: dict[str, dict[str, Any]],
        lift_requests: dict[str, dict[str, Any]],
        stalled_s: float,
        now: datetime,
    ) -> tuple[str | None, dict[str, Any]]:
        """(explanation or None, context). Context records a lift fault even if unexplained."""
        capped = stalled_s <= WAITING_CAP * self.stall_s
        lift_name: str | None = None
        request = lift_requests.get(f"{fleet}/{r['name']}")
        if request is not None:
            age = (now - request["received_at"]).total_seconds()
            if age <= LIFT_REQUEST_FRESH_S:
                lift_name = str(request["lift_name"])
        if lift_name is None:
            for name, lift in lifts.items():
                if f"{fleet}/{r['name']}" == str(lift.get("session_id", "")):
                    lift_name = name
        if lift_name is None:
            lift_name = self._near_lift(fleet, r["level"], r["x"], r["y"])
            near_only = True
        else:
            near_only = False
        context: dict[str, Any] = {}
        if lift_name is not None and lift_name in lifts:
            mode = int(lifts[lift_name].get("current_mode", 0))
            lift = lifts[lift_name]
            context = {"lift": lift_name, "lift_mode": LIFT_MODES.get(mode, str(mode))}
            if lift.get("fault_code"):
                context["fault_code"] = lift["fault_code"]
            holder = str(lift.get("session_id") or "")
            if holder and holder != f"{fleet}/{r['name']}":
                context["lift_held_by"] = holder
            if mode in LIFT_FAULT:
                return None, context
            if not near_only and capped:
                if mode == LIFT_AGV:
                    return f"waiting for or riding {lift_name}", context
                if mode == LIFT_HUMAN:
                    return f"{lift_name} in human use", context
        if r.get("mode") in (MODE_CHARGING, MODE_DOCKING):
            return "charging or docking", context
        if r.get("mode") == MODE_WAITING and capped:
            return "waiting (traffic negotiation)", context
        return None, context

    def _route(
        self,
        fleet: str,
        r: dict[str, Any],
        job: Job | None,
        visited_origin: bool,
        closed: set[int],
        now: datetime,
        rtf: float,
    ) -> RobotPath | None:
        if job is None:
            return None
        return job_route(
            site=self.site,
            eta=self.eta,
            fleet=fleet,
            robot=r,
            job=job,
            visited_origin=visited_origin,
            closed=closed,
            now=now,
            rtf=rtf,
        )

    def visited(self) -> dict[str, bool]:
        """rmf task id -> whether its robot has reached the job's pickup."""
        return {t.task_id: t.visited_origin for t in self.tracks.values()}

    def _check_pickup(
        self, track: Track, r: dict[str, Any], job: Job | None, now: datetime
    ) -> None:
        if track.visited_origin or job is None:
            return
        pickup = zone_vertex(self.site, self.eta, track.fleet, job.origin)
        if pickup is None:
            track.visited_origin = True  # no pickup in this fleet's graph: dropoff only
            return
        if self.eta.distance_to(track.fleet, pickup, r["level"], r["x"], r["y"]) > VISIT_M:
            return
        track.visited_origin = True
        error_s = (
            round((now - track.predicted_pickup).total_seconds(), 1)
            if track.predicted_pickup is not None
            else None
        )
        record = {
            "robot": f"{track.fleet}/{track.robot}",
            "task": track.task_id,
            "job": job.urn,
            "milestone": "pickup",
            "predicted": track.predicted_pickup,
            "actual": now,
            "error_s": error_s,
            "tracked_from": track.started_at,
        }
        self.calibration.append(record)
        log.info("monitor.pickup_reached", robot=record["robot"], job=job.urn, error_s=error_s)

    def _pickup_eta(
        self,
        fleet: str,
        r: dict[str, Any],
        job: Job,
        closed: set[int],
        now: datetime,
        rtf: float,
    ) -> datetime | None:
        pickup = zone_vertex(self.site, self.eta, fleet, job.origin)
        start = self.eta.nearest_vertex(fleet, r["level"], r["x"], r["y"])
        if pickup is None or start is None:
            return None
        path = self.eta.path(fleet, start, pickup, closed, now, rtf)
        return path.eta if path is not None else None

    def _stakes(self, job: Job | None, eta_s: float | None, now: datetime) -> Stakes:
        if job is None:
            return Stakes(
                due_at=None, slack_s=None, payload=None, precedence=PrecedenceClass.ROUTINE
            )
        slack = None
        if job.due_at is not None and eta_s is not None:
            slack = (job.due_at - (now + timedelta(seconds=eta_s))).total_seconds()
        return Stakes(
            due_at=job.due_at, slack_s=slack, payload=job.payload, precedence=job.precedence
        )

    def _acts(self, stakes: Stakes) -> bool:
        s = self.policy.stakes
        if stakes.precedence in s.incident_classes:
            return True
        return stakes.slack_s is not None and stakes.slack_s < s.min_slack_s

    # ------------------------------------------------------------------ tick

    def tick(
        self,
        *,
        now: datetime,
        fleets: dict[str, dict[str, Any]],
        lifts: dict[str, dict[str, Any]],
        lift_requests: dict[str, dict[str, Any]],
        closed: dict[str, set[int]],
        jobs_by_task: dict[str, Job],
        rtf: dict[str, float],
    ) -> list[MonitorEvent]:
        events: list[MonitorEvent] = []
        seen: set[str] = set()
        for fleet, state in fleets.items():
            if fleet not in self.eta.graphs:
                continue
            for r in state.get("robots", []):
                key = f"{fleet}/{r['name']}"
                task = r.get("task_id") or ""
                pos = (str(r["level"]), float(r["x"]), float(r["y"]))
                track = self.tracks.get(key)
                if not task:
                    if track is not None:
                        events.extend(self._close(track, now, "task ended"))
                        del self.tracks[key]
                    continue
                seen.add(key)
                job = jobs_by_task.get(task)
                fleet_closed = closed.get(fleet, set())
                fleet_rtf = rtf.get(fleet, 1.0)
                if track is None or track.task_id != task:
                    if track is not None:
                        events.extend(self._close(track, now, "task changed"))
                    track = Track(
                        fleet=fleet,
                        robot=r["name"],
                        task_id=task,
                        started_at=now,
                        pos=pos,
                        progress_at=now,
                        expected_finish=None,
                    )
                    self._check_pickup(track, r, job, now)
                    route = self._route(
                        fleet, r, job, track.visited_origin, fleet_closed, now, fleet_rtf
                    )
                    if route is not None:
                        track.expected_finish = route.eta
                        if not track.visited_origin and job is not None:
                            track.predicted_pickup = self._pickup_eta(
                                fleet, r, job, fleet_closed, now, fleet_rtf
                            )
                    self.tracks[key] = track
                    log.info(
                        "monitor.task_tracked",
                        robot=key,
                        task=task,
                        job=job.urn if job else None,
                        expected_finish=track.expected_finish.isoformat()
                        if track.expected_finish
                        else None,
                    )
                    continue
                # Being granted a lift (its session now this robot's) is progress too: the
                # wait before it was explained, and the ride itself barely moves the robot.
                holds_lift = next((n for n, s in lifts.items() if s.get("session_id") == key), None)
                granted = holds_lift is not None and holds_lift != track.lift_held
                track.lift_held = holds_lift
                moved = (
                    granted
                    or pos[0] != track.pos[0]
                    or math.hypot(pos[1] - track.pos[1], pos[2] - track.pos[2]) > PROGRESS_M
                )
                rebase = False
                if moved:
                    track.pos = pos
                    track.progress_at = now
                    self._check_pickup(track, r, job, now)
                    if track.incident is not None and track.incident.trigger in STALL_TRIGGERS:
                        events.extend(self._close(track, now, "progress resumed"))
                        track.routine_logged = False
                        rebase = True
                stalled_s = (now - track.progress_at).total_seconds()
                route = self._route(
                    fleet, r, job, track.visited_origin, fleet_closed, now, fleet_rtf
                )
                eta_s = route.total_s if route is not None else None
                if rebase and route is not None:
                    # The stall incident just closed already accounts for its delay; measure
                    # any further slip from here, not from the plan before the stall.
                    track.expected_finish = route.eta
                slip_s = (
                    (now + timedelta(seconds=eta_s) - track.expected_finish).total_seconds()
                    if track.expected_finish is not None and eta_s is not None
                    else None
                )
                if track.incident is not None:
                    if track.incident.trigger is not IncidentTrigger.ETA_SLIP:
                        continue
                    # A slip incident ends when the ETA is back within half the tolerance;
                    # a stall found while it is open replaces it (below).
                    if slip_s is not None and slip_s <= self.slip_s / 2:
                        events.extend(self._close(track, now, "back on schedule"))
                        track.routine_logged = False
                        continue
                    if moved:
                        continue
                trigger: IncidentTrigger | None = None
                context: dict[str, Any] = {}
                if moved:
                    # Moving but late: judged by the ETA only.
                    if slip_s is not None and slip_s > self.slip_s:
                        trigger = IncidentTrigger.ETA_SLIP
                else:
                    # Standing still: judged by the stall rules only.
                    explanation, context = self.explain(
                        fleet, r, lifts, lift_requests, stalled_s, now
                    )
                    if explanation is not None:
                        continue
                    lift_fault = context.get("lift_mode") in {LIFT_MODES[m] for m in LIFT_FAULT}
                    if lift_fault:
                        if stalled_s >= BLOCKED_GRACE_S:
                            trigger = IncidentTrigger.BLOCKED_STATUS
                    elif stalled_s > self.stall_s:
                        trigger = IncidentTrigger.NO_PROGRESS
                if trigger is None:
                    continue
                if track.incident is not None:
                    if trigger is IncidentTrigger.ETA_SLIP:
                        continue
                    events.extend(self._close(track, now, f"superseded by {trigger.value}"))
                stakes = self._stakes(job, eta_s, now)
                segs = robot_segments_at(self.site, self.eta, fleet, *pos)
                segs.sort(
                    key=lambda s: {"corridor": 0, "lift": 1, "door": 2}.get(
                        self.site.segment(s).kind, 3
                    )
                )
                if not self._acts(stakes):
                    if not track.routine_logged:
                        log.info(
                            "monitor.stall_logged",
                            robot=key,
                            task=task,
                            trigger=trigger.value,
                            stalled_s=round(stalled_s),
                            precedence=stakes.precedence.value,
                            slack_s=stakes.slack_s,
                            segment=segs[0] if segs else None,
                            **context,
                        )
                        track.routine_logged = True
                    continue
                incident = Incident(
                    incident_id=f"inc-{fleet}-{r['name']}-{int(now.timestamp())}",
                    robot_urn=robot_urn(fleet, r["name"]),
                    rmf_task_id=task,
                    job_urn=job.urn if job else None,
                    segment_urn=segs[0] if segs else None,
                    detected_at=now,
                    trigger=trigger,
                    stakes=stakes,
                )
                track.incident = incident
                track.context = context
                events.append(MonitorEvent("opened", incident))
                log.warning(
                    "monitor.incident_opened",
                    incident=incident.incident_id,
                    robot=key,
                    task=task,
                    job=incident.job_urn,
                    trigger=trigger.value,
                    stalled_s=round(stalled_s),
                    slack_s=stakes.slack_s,
                    segment=incident.segment_urn,
                    **context,
                )
        for key in [k for k in self.tracks if k not in seen]:
            events.extend(self._close(self.tracks.pop(key), now, "robot no longer reported"))
        return events

    def _close(self, track: Track, now: datetime, outcome: str) -> list[MonitorEvent]:
        if track.incident is None:
            return []
        resolved = track.incident.model_copy(
            update={"status": IncidentStatus.RESOLVED, "outcome": outcome}
        )
        track.incident = None
        log.info("monitor.incident_resolved", incident=resolved.incident_id, outcome=outcome)
        return [MonitorEvent("resolved", resolved)]

    def open_incidents(self) -> list[tuple[Incident, dict[str, Any]]]:
        return [(t.incident, t.context) for t in self.tracks.values() if t.incident is not None]
