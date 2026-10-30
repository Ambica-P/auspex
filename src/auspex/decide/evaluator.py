"""Option templates and the evaluator (spec 6.1, 6.2). Deterministic; no I/O.

For a conflict between a robot delivery and a person's transport on one segment, the
templates build candidate options, each with typed actions and the predicted effect on both
jobs:
- do_nothing: whoever reaches the segment first holds it; the other waits;
- reroute / alternative_resource: close the segment's lanes for the robot's fleet over the
  person's window, replan; the robot's new route is the ETA model's path without them;
- wait: hold the robot (interrupt its task, owned only) until the person's window has passed;
- handover: cancel the robot's task and resubmit the job to the fleet; predicted with the
  fastest idle robot of that fleet (Open-RMF's auction picks the robot; assumption logged).
A notification to the requester is added when the delivery is predicted late.

Objective, lexicographic by policy precedence (spec 6.2): missed deadlines in the highest
affected class; then lateness weighted by class; then delay caused to people; then
interventions. Lateness and delay are compared in TIE_TOLERANCE_S buckets (ties).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from auspex.core.models import (
    Action,
    Claim,
    CloseLanes,
    InterruptTask,
    Job,
    JobEffect,
    JobKind,
    Notify,
    Option,
    OptionTemplate,
    ReplanRobot,
    SubmitTask,
    TimeWindow,
)
from auspex.forecast.eta import EtaModel, RobotPath
from auspex.forecast.forecaster import job_route
from auspex.picture.ros_state import StateView
from auspex.policy.loader import Policy
from auspex.site.model import Site

ENDED = ("completed", "cancelled")


@dataclass(frozen=True)
class Situation:
    """What the decision engine knows at one moment (a snapshot; never mutated)."""

    now: datetime
    jobs: dict[str, Job]
    claims: list[Claim]
    paths: Mapping[str, RobotPath]
    state: StateView
    visited: Mapping[str, bool]  # rmf task id -> robot has been to the job's pickup
    owned: frozenset[str]
    # Lanes Auspex has queued for closing (commands not yet carried out): decisions taken
    # in the same moment must already route around them.
    planned_closed: Mapping[str, frozenset[int]] = field(default_factory=dict)
    held: frozenset[str] = frozenset()  # tasks Auspex holds (interrupted, not yet resumed)

    def closed(self, fleet: str) -> set[int]:
        return set(self.state.closed.get(fleet, set())) | set(self.planned_closed.get(fleet, ()))

    def robot_for(self, job: Job) -> tuple[str, dict[str, Any]] | None:
        if not job.rmf_task_id:
            return None
        for fleet, s in self.state.fleets.items():
            for r in s.get("robots", []):
                if r.get("task_id") == job.rmf_task_id:
                    return fleet, dict(r)
        return None

    def human_window(self, job_urns: set[str], segment: str) -> TimeWindow | None:
        mine = [c for c in self.claims if c.job_urn in job_urns and c.segment_urn == segment]
        if not mine:
            return None
        return TimeWindow(
            start=min(c.window.start for c in mine), end=max(c.window.end for c in mine)
        )


@dataclass(frozen=True)
class Scored:
    option: Option
    key: tuple[int, int, int, int]
    meets_highest: bool
    detail: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "option_id": self.option.option_id,
            "template": self.option.template.value,
            "actions": [a.model_dump(mode="json") for a in self.option.actions],
            "effects": [e.model_dump(mode="json") for e in self.option.effects],
            "score": list(self.key),
            "meets_highest": self.meets_highest,
            **self.detail,
        }


def segment_visit(path: RobotPath, segment: str) -> tuple[datetime, datetime] | None:
    legs = [leg for leg in path.legs if segment in leg.segments]
    if not legs:
        return None
    return legs[0].enter, legs[-1].exit


def lateness(job: Job, eta: datetime | None) -> float:
    if job.due_at is None or eta is None:
        return 0.0
    return max(0.0, (eta - job.due_at).total_seconds())


def contention(path: RobotPath, segment: str, window: TimeWindow) -> tuple[float, float]:
    """(robot wait s, delay caused to the person s) if the robot keeps this route."""
    visit = segment_visit(path, segment)
    if visit is None:
        return 0.0, 0.0
    enter, leave = visit
    if leave <= window.start or enter >= window.end:
        return 0.0, 0.0
    if enter <= window.start:  # robot first: the person waits for it to clear
        return 0.0, (leave - window.start).total_seconds()
    return (window.end - enter).total_seconds(), 0.0  # person first: the robot waits


@dataclass
class OptionBuilder:
    site: Site
    eta: EtaModel
    pad_s: float

    def for_conflict(
        self, s: Situation, robot_job: Job, human_job: Job, segment: str
    ) -> list[Option]:
        found = s.robot_for(robot_job)
        path = s.paths.get(robot_job.urn)
        window = s.human_window({human_job.urn}, segment)
        if found is None or path is None or window is None:
            return []
        fleet, robot = found
        task = str(robot_job.rmf_task_id)
        closed = s.closed(fleet)
        rtf = s.state.rtf.get(fleet, 1.0)
        visited = s.visited.get(task, False)
        until = window.end + timedelta(seconds=self.pad_s)
        options: list[Option] = []

        def effects(eta: datetime, human_delay: float, n: int) -> list[JobEffect]:
            return [
                JobEffect(
                    job_urn=robot_job.urn,
                    eta=eta,
                    lateness_s=lateness(robot_job, eta),
                    delay_to_humans_s=0.0,
                    interventions=n,
                ),
                JobEffect(
                    job_urn=human_job.urn,
                    eta=None,
                    lateness_s=0.0,
                    delay_to_humans_s=max(0.0, human_delay),
                    interventions=0,
                ),
            ]

        def add(template: OptionTemplate, actions: list[Action], fx: list[JobEffect]) -> None:
            late = fx[0].lateness_s > 0
            if late and robot_job.requester_urn:
                eta = fx[0].eta
                actions = [
                    *actions,
                    Notify(
                        recipient_urn=robot_job.requester_urn,
                        job_urn=robot_job.urn,
                        message="Your delivery is delayed by a lift or corridor conflict.",
                        new_eta=eta,
                    ),
                ]
            options.append(
                Option(
                    option_id=f"{template.value}-{len(options) + 1}",
                    template=template,
                    actions=actions,
                    effects=fx,
                )
            )

        # do nothing
        robot_wait, human_delay = contention(path, segment, window)
        add(
            OptionTemplate.DO_NOTHING,
            [],
            effects(path.eta + timedelta(seconds=robot_wait), human_delay, 0),
        )
        # reroute / alternative resource
        lanes = set(self.site.segments[segment].rmf_lanes.get(fleet, []))
        if lanes:
            alt = job_route(
                site=self.site,
                eta=self.eta,
                fleet=fleet,
                robot=robot,
                job=robot_job,
                visited_origin=visited,
                closed=closed | lanes,
                now=s.now,
                rtf=rtf,
            )
            if alt is not None:
                other_lift = [x for x in alt.lifts() if x != segment]
                template = (
                    OptionTemplate.ALTERNATIVE_RESOURCE
                    if segment.startswith("segment/lift_") and other_lift
                    else OptionTemplate.REROUTE
                )
                add(
                    template,
                    [
                        CloseLanes(
                            fleet_name=fleet,
                            segment_urn=segment,
                            window=TimeWindow(start=s.now, end=until),
                        ),
                        ReplanRobot(fleet_name=fleet, robot_name=str(robot["name"])),
                    ],
                    effects(alt.eta, 0.0, 2),
                )
        # wait (hold the robot until the person's window has passed)
        if task in s.owned and until > s.now:
            held_eta = until + timedelta(seconds=path.total_s)
            add(
                OptionTemplate.WAIT,
                [InterruptTask(task_id=task, window=TimeWindow(start=s.now, end=until))],
                effects(held_eta, 0.0, 1),
            )
        # handover to the fastest idle robot of the fleet (payload not yet collected)
        if task in s.owned and not visited:
            best: RobotPath | None = None
            for r in s.state.fleets.get(fleet, {}).get("robots", []):
                if r.get("name") == robot["name"] or r.get("task_id"):
                    continue
                p = job_route(
                    site=self.site,
                    eta=self.eta,
                    fleet=fleet,
                    robot=r,
                    job=robot_job,
                    visited_origin=False,
                    closed=closed,
                    now=s.now,
                    rtf=rtf,
                )
                if p is not None and (best is None or p.eta < best.eta):
                    best = p
            places = self._places(fleet, robot_job)
            if best is not None and places is not None:
                wait2, delay2 = contention(best, segment, window)
                add(
                    OptionTemplate.HANDOVER,
                    [
                        SubmitTask(
                            job_urn=robot_job.urn,
                            pickup_place=places[0],
                            dropoff_place=places[1],
                            payload=robot_job.payload,
                            fleet_name=fleet,
                            replaces_task_id=task,
                        )
                    ],
                    effects(best.eta + timedelta(seconds=wait2), delay2, 2),
                )
        return options

    # ------------------------------------------------------------------ handover helpers

    def zone_waypoint(self, fleet: str, zone: str | None) -> str | None:
        if zone is None or zone not in self.site.zones:
            return None
        for name in self.site.zone(zone).waypoints.get(fleet, []):
            if self.eta.waypoint_vertex(fleet, name) is not None:
                return name
        return None

    def supply_zones(self, job: Job, fleet: str) -> list[str]:
        """Zones stocking the job's payload that the fleet can reach (its own origin first)."""
        if not job.payload:
            return []
        out = [
            z.urn
            for z in self.site.zones.values()
            if job.payload in z.supplies and self.zone_waypoint(fleet, z.urn) is not None
        ]
        return sorted(out, key=lambda u: u != job.origin)

    def handover(
        self, s: Situation, robot_job: Job, fleet: str, from_zone: str, exclude: str
    ) -> tuple[RobotPath, SubmitTask] | None:
        """Fastest idle robot of the fleet collecting the payload at `from_zone`."""
        task = str(robot_job.rmf_task_id)
        pickup = self.zone_waypoint(fleet, from_zone)
        drop = self.zone_waypoint(fleet, robot_job.destination)
        if pickup is None or drop is None:
            return None
        job = robot_job.model_copy(update={"origin": from_zone})
        best: RobotPath | None = None
        for r in s.state.fleets.get(fleet, {}).get("robots", []):
            if r.get("name") == exclude or r.get("task_id"):
                continue
            p = job_route(
                site=self.site,
                eta=self.eta,
                fleet=fleet,
                robot=r,
                job=job,
                visited_origin=False,
                closed=s.closed(fleet),
                now=s.now,
                rtf=s.state.rtf.get(fleet, 1.0),
            )
            if p is not None and (best is None or p.eta < best.eta):
                best = p
        if best is None:
            return None
        action = SubmitTask(
            job_urn=robot_job.urn,
            pickup_place=pickup,
            dropoff_place=drop,
            payload=robot_job.payload,
            fleet_name=fleet,
            replaces_task_id=task,
        )
        return best, action

    # ------------------------------------------------------------------ outage (S2)

    def for_outage(
        self, s: Situation, robot_job: Job, segment: str, outage_s: float | None
    ) -> list[Option]:
        """Options for a robot whose route uses a segment that is out of service for
        `outage_s` (None: unknown, treated as longer than the job's deadline allows)."""
        found = s.robot_for(robot_job)
        path = s.paths.get(robot_job.urn)
        if found is None or path is None:
            return []
        fleet, robot = found
        task = str(robot_job.rmf_task_id)
        options: list[Option] = []

        def fx(eta: datetime | None, n: int) -> list[JobEffect]:
            return [
                JobEffect(
                    job_urn=robot_job.urn,
                    eta=eta,
                    lateness_s=lateness(robot_job, eta) if eta else 86_400.0,
                    delay_to_humans_s=0.0,
                    interventions=n,
                )
            ]

        def add(template: OptionTemplate, actions: list[Action], effects: list[JobEffect]) -> None:
            options.append(
                Option(
                    option_id=f"{template.value}-{len(options) + 1}",
                    template=template,
                    actions=actions,
                    effects=effects,
                )
            )

        visit = segment_visit(path, segment)
        if visit is not None:
            back = None if outage_s is None else s.now + timedelta(seconds=outage_s)
            eta = None if back is None else path.eta + max(back - visit[0], timedelta())
            add(OptionTemplate.WAIT, [], fx(eta, 0))
        lanes = set(self.site.segments[segment].rmf_lanes.get(fleet, []))
        if lanes:
            alt = job_route(
                site=self.site,
                eta=self.eta,
                fleet=fleet,
                robot=robot,
                job=robot_job,
                visited_origin=s.visited.get(task, False),
                closed=s.closed(fleet) | lanes,
                now=s.now,
                rtf=s.state.rtf.get(fleet, 1.0),
            )
            if alt is not None:
                add(
                    OptionTemplate.ALTERNATIVE_RESOURCE,
                    [
                        CloseLanes(fleet_name=fleet, segment_urn=segment),
                        ReplanRobot(fleet_name=fleet, robot_name=str(robot["name"])),
                    ],
                    fx(alt.eta, 2),
                )
        if task in s.owned:
            for zone in self.supply_zones(robot_job, fleet):
                if zone == robot_job.origin and s.visited.get(task, False):
                    continue  # the payload is already on the robot
                h = self.handover(s, robot_job, fleet, zone, exclude=str(robot["name"]))
                if h is not None:
                    add(OptionTemplate.HANDOVER, [h[1]], fx(h[0].eta, 2))
        return options

    def _places(self, fleet: str, job: Job) -> tuple[str | None, str] | None:
        def wp(zone: str | None) -> str | None:
            if zone is None or zone not in self.site.zones:
                return None
            for name in self.site.zone(zone).waypoints.get(fleet, []):
                if self.eta.waypoint_vertex(fleet, name) is not None:
                    return name
            return None

        drop = wp(job.destination)
        if drop is None:
            return None
        return wp(job.origin), drop


# --------------------------------------------------------------------------- objective


def score(option: Option, jobs: Mapping[str, Job], policy: Policy, tie_s: float) -> Scored:
    effects = [e for e in option.effects if e.job_urn in jobs]
    ranks = {e.job_urn: policy.rank(jobs[e.job_urn]) for e in effects}
    top = min(ranks.values(), default=0)
    n = len(policy.precedence)
    missed_top = sum(1 for e in effects if ranks[e.job_urn] == top and e.lateness_s > 0)
    weighted = sum(e.lateness_s * (n - ranks[e.job_urn]) for e in effects)
    people = sum(
        e.delay_to_humans_s for e in effects if jobs[e.job_urn].kind is not JobKind.ROBOT_TASK
    )
    interventions = sum(e.interventions for e in effects)
    tie = max(tie_s, 1.0)
    key = (missed_top, round(weighted / tie), round(people / tie), interventions)
    return Scored(
        option=option,
        key=key,
        meets_highest=missed_top == 0,
        detail={
            "weighted_lateness_s": round(weighted, 1),
            "delay_to_people_s": round(people, 1),
        },
    )


def rank_options(
    options: list[Option], jobs: Mapping[str, Job], policy: Policy, tie_s: float
) -> list[Scored]:
    """Best first; ties keep template order (do_nothing, reroute, wait, handover)."""
    return sorted((score(o, jobs, policy, tie_s) for o in options), key=lambda x: x.key)
