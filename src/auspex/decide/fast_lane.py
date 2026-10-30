"""Fast lane (spec 6, Contain): playbook plans, deterministic, no LLM or Tavily, no I/O.

Each function turns one trigger into a Plan (or None when the playbook does not apply).
Predicted effects are filled where the ETA model can compute them, so the gate's hospital
rules see what the plan does to each job.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from auspex.core.models import (
    Action,
    CloseLanes,
    Conflict,
    Incident,
    InterruptTask,
    Job,
    JobEffect,
    JobKind,
    Notify,
    OpenLanes,
    Plan,
    Producer,
    ReplanRobot,
    TimeWindow,
)
from auspex.decide.evaluator import Situation, lateness, segment_visit
from auspex.decide.playbooks import Playbook
from auspex.forecast.eta import EtaModel
from auspex.forecast.forecaster import job_route, robot_segments_at
from auspex.site.model import Site


def _plan_id(playbook: Playbook, subject: str, now: datetime) -> str:
    return f"fl-{playbook.id}-{subject}-{now:%H%M%S}"[:128]


def lift_unavailable(
    pb: Playbook,
    s: Situation,
    site: Site,
    lift: str,
    mode: str,
    fault: str | None,
    *,
    skip_fleets: set[str] | None = None,
    only_fleets: set[str] | None = None,
) -> Plan | None:
    """Close the lift for every fleet (config C may hold back fleets whose deliveries the
    agent decides on: `skip_fleets`; `only_fleets` builds that safe default on its own)."""
    seg_urn = f"segment/{lift}"
    seg = site.segments.get(seg_urn)
    if seg is None:
        return None
    actions: list[Action] = [
        CloseLanes(fleet_name=fleet, segment_urn=seg_urn)
        for fleet, lanes in sorted(seg.rmf_lanes.items())
        if lanes
        and not set(lanes) <= s.closed(fleet)
        and fleet not in (skip_fleets or set())
        and (only_fleets is None or fleet in only_fleets)
    ]
    if not actions:
        return None
    affected: list[Job] = []
    for urn, path in s.paths.items():
        job = s.jobs.get(urn)
        if job is None or seg_urn not in path.lifts():
            continue
        found = s.robot_for(job)
        if found is None or found[0] in (skip_fleets or set()):
            continue
        if only_fleets is not None and found[0] not in only_fleets:
            continue
        affected.append(job)
        if pb.replan == "robots_routed_through":
            actions.append(ReplanRobot(fleet_name=found[0], robot_name=str(found[1]["name"])))
    if pb.notify == "requesters_of_affected_jobs":
        for job in affected:
            if job.requester_urn:
                actions.append(
                    Notify(
                        recipient_urn=job.requester_urn,
                        job_urn=job.urn,
                        message=f"{lift} is out of service ({mode} {fault or ''}); "
                        "your delivery is being rerouted.".replace(" )", ")"),
                    )
                )
    suffix = "-safe" if only_fleets is not None else ""
    return Plan(
        plan_id=_plan_id(pb, lift + suffix, s.now),
        producer=Producer.FAST_LANE,
        actions=actions,
        rationale=f"Playbook {pb.id}: {lift} reported {mode} {fault or ''} by the building's "
        "lift monitoring; robots kept out of it until it is back in service.",
    )


def lift_available(pb: Playbook, s: Situation, site: Site, lift: str, fleets: list[str]) -> Plan:
    return Plan(
        plan_id=_plan_id(pb, f"{lift}-reopen", s.now),
        producer=Producer.FAST_LANE,
        actions=[OpenLanes(fleet_name=f, segment_urn=f"segment/{lift}") for f in fleets],
        rationale=f"Playbook {pb.id}: {lift} back in service; lanes Auspex closed reopened.",
    )


def clear_for_code_stat(
    pb: Playbook,
    s: Situation,
    site: Site,
    eta: EtaModel,
    conflict: Conflict,
    robot_job: Job,
    human_job: Job,
) -> Plan | None:
    if human_job.precedence not in pb.human_classes:
        return None
    if robot_job.kind is not JobKind.ROBOT_TASK:
        return None
    found = s.robot_for(robot_job)
    path = s.paths.get(robot_job.urn)
    if found is None or path is None:
        return None
    fleet, robot = found
    seg = conflict.segment_urn
    lanes = set(site.segments[seg].rmf_lanes.get(fleet, []))
    if not lanes:
        return None
    here = robot_segments_at(site, eta, fleet, robot["level"], robot["x"], robot["y"])
    if seg in here:
        return None  # already at the segment: closing it now could trap the robot
    # Keep the segment clear for every CODE/STAT person due on it, not only this one.
    people = {
        j.urn
        for j in s.jobs.values()
        if j.kind is not JobKind.ROBOT_TASK and j.precedence in pb.human_classes
    }
    window = s.human_window(people, seg) or conflict.window
    until = window.end + timedelta(seconds=pb.pad_s)
    if until <= s.now:
        return None
    closure = TimeWindow(start=s.now, end=until)
    task = str(robot_job.rmf_task_id)
    alt = job_route(
        site=site,
        eta=eta,
        fleet=fleet,
        robot=robot,
        job=robot_job,
        visited_origin=s.visited.get(task, False),
        closed=s.closed(fleet) | lanes,
        now=s.now,
        rtf=s.state.rtf.get(fleet, 1.0),
    )
    actions: list[Action] = [CloseLanes(fleet_name=fleet, segment_urn=seg, window=closure)]
    if alt is not None:
        robot_eta = alt.eta
        actions.append(ReplanRobot(fleet_name=fleet, robot_name=str(robot["name"])))
        how = f"rerouted (ETA {robot_eta:%H:%M:%S} UTC)"
    elif pb.if_no_route == "hold" and task in s.owned:
        robot_eta = until + timedelta(seconds=path.total_s)
        if task not in s.held:  # one hold at a time: every interruption must be released
            actions.append(InterruptTask(task_id=task, window=closure))
        how = f"held until {until:%H:%M:%S} UTC (no other route)"
    else:
        visit = segment_visit(path, seg)
        robot_eta = path.eta + (
            timedelta(seconds=max(0.0, (until - visit[0]).total_seconds()))
            if visit
            else timedelta()
        )
        how = "waits for the segment to reopen (no other route; task not Auspex's to hold)"
    late = lateness(robot_job, robot_eta)
    if pb.notify and late > 0 and robot_job.requester_urn:
        actions.append(
            Notify(
                recipient_urn=robot_job.requester_urn,
                job_urn=robot_job.urn,
                message="Your delivery gives way to an urgent patient transport.",
                new_eta=robot_eta,
            )
        )
    return Plan(
        plan_id=_plan_id(pb, conflict.conflict_id, s.now),
        conflict_id=conflict.conflict_id,
        producer=Producer.FAST_LANE,
        actions=actions,
        predicted=[
            JobEffect(
                job_urn=robot_job.urn,
                eta=robot_eta,
                lateness_s=late,
                delay_to_humans_s=0.0,
                interventions=len(actions),
            ),
            JobEffect(
                job_urn=human_job.urn,
                eta=None,
                lateness_s=0.0,
                delay_to_humans_s=0.0,
                interventions=0,
            ),
        ],
        rationale=f"Playbook {pb.id}: {human_job.precedence.value.upper()} transport "
        f"{human_job.external_id} needs {seg} until {until:%H:%M:%S} UTC; robot "
        f"{robot['name']} {how}.",
    )


def notify_stalled(pb: Playbook, s: Situation, inc: Incident) -> Plan | None:
    job = s.jobs.get(inc.job_urn) if inc.job_urn else None
    if job is None or not job.requester_urn:
        return None
    path = s.paths.get(job.urn)
    return Plan(
        plan_id=_plan_id(pb, inc.incident_id, s.now),
        incident_id=inc.incident_id,
        producer=Producer.FAST_LANE,
        actions=[
            Notify(
                recipient_urn=job.requester_urn,
                job_urn=job.urn,
                message=f"Your delivery's robot has stopped ({inc.trigger.value}); "
                "Auspex is working on it.",
                new_eta=path.eta if path else None,
            )
        ],
        rationale=f"Playbook {pb.id}: {inc.trigger.value} on a {job.precedence.value} "
        "delivery; requester told.",
    )
