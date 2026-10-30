"""Domain model (spec section 3). Pydantic v2, unknown fields rejected, frozen, URN-addressed.

Model output from Nemotron is validated against these same types: a Plan can only contain
actions from the closed vocabulary below (spec section 7), and nothing else.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from auspex.core.urns import Urn


class StrictModel(BaseModel):
    """Base for every domain type: unknown fields rejected, immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:\-]+$")]
FleetName = Identifier
RobotName = Identifier
WaypointName = Identifier
ShortText = Annotated[str, Field(max_length=500)]
LongText = Annotated[str, Field(max_length=2000)]

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_control(text: str) -> str:
    """Remove control characters from untrusted text (newlines and tabs kept)."""
    return _CONTROL.sub("", text)


# --------------------------------------------------------------------------- time


class TimeWindow(StrictModel):
    """Half-open interval [start, end), timezone-aware."""

    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("window end must be after start")
        return self

    def overlaps(self, other: TimeWindow) -> bool:
        return self.start < other.end and other.start < self.end

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


# --------------------------------------------------------------------------- 3.1 Job


class JobKind(StrEnum):
    ROBOT_TASK = "robot_task"
    HUMAN_TRANSPORT = "human_transport"
    CLINICAL_EVENT = "clinical_event"


class PrecedenceClass(StrEnum):
    """Vocabulary only. The ORDER comes from hospital policy (policy/precedence.yaml)."""

    CODE = "code"
    STAT = "stat"
    CRITICAL = "critical"
    URGENT = "urgent"
    ROUTINE = "routine"
    RETURN = "return"


class JobStatus(StrEnum):
    REQUESTED = "requested"
    ASSIGNED = "assigned"
    RESPONDING = "responding"
    PICKED_UP = "picked_up"
    DELAYED = "delayed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


INFERABLE_JOB_FIELDS: frozenset[str] = frozenset(
    {"destination", "destination_type", "transport_mode", "bed_type", "payload", "due_at"}
)
"""Fields Nemotron may fill when the source left them missing or ambiguous. Never precedence."""


class Job(StrictModel):
    urn: Urn
    kind: JobKind
    source: Identifier
    external_id: Identifier
    precedence: PrecedenceClass
    origin: Urn
    destination: Urn | None = None
    destination_text: ShortText | None = None
    destination_type: Identifier | None = None
    payload: Identifier | None = None
    transport_mode: Identifier | None = None
    bed_type: Identifier | None = None
    due_at: AwareDatetime | None = None
    requester_urn: Urn | None = None
    beneficiary_urn: Urn | None = None
    receiver_urn: Urn | None = None
    status: JobStatus
    status_at: AwareDatetime
    rmf_task_id: Identifier | None = None
    notes: LongText | None = None
    inferred_fields: list[str] = Field(default_factory=list)
    # How sure the interpretation behind inferred_fields is (0..1); None when nothing inferred.
    inference_confidence: Confidence | None = None

    @model_validator(mode="after")
    def _inferred_fields_allowed(self) -> Self:
        bad = sorted(set(self.inferred_fields) - INFERABLE_JOB_FIELDS)
        if bad:
            raise ValueError(f"fields that may not be inferred: {bad}")
        return self


# --------------------------------------------------------------------------- 3.3 Claims


class ClaimBasis(StrEnum):
    RMF_PATH = "rmf_path"
    RMF_ESTIMATE = "rmf_estimate"
    ROUTE_TABLE = "route_table"
    DECLARED = "declared"
    OBSERVED = "observed"
    INFERRED = "inferred"
    ETA_MODEL = "eta_model"  # Auspex's ETA model on the fleet's nav graph (RMF publishes no path)


class Claim(StrictModel):
    claim_id: Identifier
    job_urn: Urn
    actor_urn: Urn
    segment_urn: Urn
    window: TimeWindow
    confidence: Confidence
    basis: ClaimBasis


# --------------------------------------------------------------------------- 3.4 Conflict


class ConflictKind(StrEnum):
    OVERLAP = "overlap"  # overlapping claims on an exclusive segment
    REQUIRED_CLEAR = "required_clear"  # claim on a segment a higher job needs clear


class Conflict(StrictModel):
    conflict_id: Identifier
    kind: ConflictKind
    segment_urn: Urn
    claim_ids: list[Identifier] = Field(min_length=2)
    job_urns: list[Urn] = Field(min_length=2)
    window: TimeWindow
    detected_at: AwareDatetime


# --------------------------------------------------------------------------- 7 Actions


class _Action(StrictModel):
    window: TimeWindow | None = None  # time-bounded actions: the adapter records the inverse


class CloseLanes(_Action):
    action: Literal["close_lanes"] = "close_lanes"
    fleet_name: FleetName
    segment_urn: Urn  # lane indices come from the site model, never from the model output


class OpenLanes(_Action):
    action: Literal["open_lanes"] = "open_lanes"
    fleet_name: FleetName
    segment_urn: Urn


class ReplanRobot(_Action):
    action: Literal["replan_robot"] = "replan_robot"
    fleet_name: FleetName
    robot_name: RobotName


class SubmitTask(_Action):
    action: Literal["submit_task"] = "submit_task"
    job_urn: Urn
    pickup_place: WaypointName | None = None
    dropoff_place: WaypointName
    payload: Identifier | None = None
    fleet_name: FleetName | None = None
    earliest_start: AwareDatetime | None = None
    replaces_task_id: Identifier | None = None


class CancelTask(_Action):
    action: Literal["cancel_task"] = "cancel_task"
    task_id: Identifier


class InterruptTask(_Action):
    action: Literal["interrupt_task"] = "interrupt_task"
    task_id: Identifier


class ResumeTask(_Action):
    action: Literal["resume_task"] = "resume_task"
    task_id: Identifier


class SetPriority(_Action):
    action: Literal["set_priority"] = "set_priority"
    task_id: Identifier
    high: bool  # Open-RMF priority is binary


class SetEarliestStart(_Action):
    action: Literal["set_earliest_start"] = "set_earliest_start"
    task_id: Identifier
    earliest_start: AwareDatetime


class RestrictFleets(_Action):
    action: Literal["restrict_fleets"] = "restrict_fleets"
    job_urn: Urn
    fleet_names: list[FleetName] = Field(min_length=1)


class HoldAtWaypoint(_Action):
    action: Literal["hold_at_waypoint"] = "hold_at_waypoint"
    fleet_name: FleetName
    robot_name: RobotName
    waypoint: WaypointName


class ClaimMutex(_Action):
    action: Literal["claim_mutex"] = "claim_mutex"
    fleet_name: FleetName
    robot_name: RobotName
    group: Identifier


class ReleaseMutex(_Action):
    action: Literal["release_mutex"] = "release_mutex"
    fleet_name: FleetName
    robot_name: RobotName
    group: Identifier


class Notify(_Action):
    action: Literal["notify"] = "notify"
    recipient_urn: Urn
    job_urn: Urn | None = None
    message: ShortText
    new_eta: AwareDatetime | None = None


class AdviseStaff(_Action):
    """A suggestion to people, never an order. Limited by policy (humans-not-ordered)."""

    action: Literal["advise_staff"] = "advise_staff"
    recipient_urn: Urn
    job_urn: Urn
    message: ShortText


Action = Annotated[
    CloseLanes
    | OpenLanes
    | ReplanRobot
    | SubmitTask
    | CancelTask
    | InterruptTask
    | ResumeTask
    | SetPriority
    | SetEarliestStart
    | RestrictFleets
    | HoldAtWaypoint
    | ClaimMutex
    | ReleaseMutex
    | Notify
    | AdviseStaff,
    Field(discriminator="action"),
]

ACTION_NAMES: frozenset[str] = frozenset(
    {
        "close_lanes",
        "open_lanes",
        "replan_robot",
        "submit_task",
        "cancel_task",
        "interrupt_task",
        "resume_task",
        "set_priority",
        "set_earliest_start",
        "restrict_fleets",
        "hold_at_waypoint",
        "claim_mutex",
        "release_mutex",
        "notify",
        "advise_staff",
    }
)


# --------------------------------------------------------------------------- 10 Post-conditions


class TaskStateIs(StrictModel):
    kind: Literal["task_state"] = "task_state"
    task_id: Identifier
    states: list[Identifier] = Field(min_length=1)  # Open-RMF task status values
    by: AwareDatetime


class SegmentClearFor(StrictModel):
    kind: Literal["segment_clear_for"] = "segment_clear_for"
    segment_urn: Urn
    fleet_name: FleetName
    window: TimeWindow


class JobEtaBefore(StrictModel):
    kind: Literal["job_eta_before"] = "job_eta_before"
    job_urn: Urn
    due_at: AwareDatetime


class RobotHeldAt(StrictModel):
    kind: Literal["robot_held_at"] = "robot_held_at"
    fleet_name: FleetName
    robot_name: RobotName
    waypoint: WaypointName


class LanesReopened(StrictModel):
    kind: Literal["lanes_reopened"] = "lanes_reopened"
    segment_urn: Urn
    fleet_name: FleetName


PostCondition = Annotated[
    TaskStateIs | SegmentClearFor | JobEtaBefore | RobotHeldAt | LanesReopened,
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------- 3.4 Option, Plan


class JobEffect(StrictModel):
    job_urn: Urn
    eta: AwareDatetime | None
    lateness_s: float = Field(ge=0.0)  # 0 when on time or no deadline
    delay_to_humans_s: float = Field(ge=0.0)
    interventions: int = Field(ge=0)


class OptionTemplate(StrEnum):
    REROUTE = "reroute"
    ALTERNATIVE_RESOURCE = "alternative_resource"
    WAIT = "wait"
    HANDOVER = "handover"
    RESEQUENCE = "resequence"
    ADVISE_STAFF = "advise_staff"
    DO_NOTHING = "do_nothing"
    COMPOSITE = "composite"  # proposed by the agent; re-scored by the evaluator


class Option(StrictModel):
    option_id: Identifier
    template: OptionTemplate
    actions: list[Action] = Field(max_length=20)
    effects: list[JobEffect]


class Producer(StrEnum):
    FAST_LANE = "fast_lane"
    SOLVER = "solver"
    AGENT = "agent"
    OPERATOR = "operator"  # written by a person in the console; may stand alone
    # A fast-lane plan may also stand alone: it can answer a facility event (a lift out of
    # service), which is neither a conflict nor an incident.


class Plan(StrictModel):
    plan_id: Identifier
    conflict_id: Identifier | None = None
    incident_id: Identifier | None = None
    producer: Producer
    actions: list[Action] = Field(max_length=20)
    expected: list[PostCondition] = Field(default_factory=list, max_length=40)
    predicted: list[JobEffect] = Field(default_factory=list)
    rationale: LongText = ""
    evidence_refs: list[Sha256Hex] = Field(default_factory=list, max_length=20)
    risk: float | None = Field(default=None, ge=0.0, le=1.0)  # always recomputed by the gate

    # A facility event (a lift fault) answered before any conflict or incident exists.
    event_id: Identifier | None = None

    @model_validator(mode="after")
    def _one_subject(self) -> Self:
        subjects = [x for x in (self.conflict_id, self.incident_id, self.event_id) if x]
        if len(subjects) > 1:
            raise ValueError("a plan addresses exactly one conflict, incident or event")
        if not subjects and self.producer not in (Producer.OPERATOR, Producer.FAST_LANE):
            raise ValueError("a plan addresses exactly one conflict, incident or event")
        return self


# --------------------------------------------------------------------------- 9 Gate decision


class Verdict(StrEnum):
    ALLOW = "ALLOW"
    REVIEW = "REVIEW"
    BLOCK = "BLOCK"


class ApprovalRoute(StrEnum):
    AUTOMATIC = "automatic"
    PERSON = "person"


class Decision(StrictModel):
    plan_id: Identifier
    verdict: Verdict
    rule_ids: list[Identifier]
    risk: float = Field(ge=0.0, le=1.0)
    approval_route: ApprovalRoute


# --------------------------------------------------------------------------- 3.5 Incident


class IncidentTrigger(StrEnum):
    BLOCKED_STATUS = "blocked_status"
    NO_PROGRESS = "no_progress"
    ETA_SLIP = "eta_slip"


class IncidentStatus(StrEnum):
    OPEN = "open"
    CONTAINED = "contained"
    RESOLVED = "resolved"
    CLOSED = "closed"


class PossibleCause(StrictModel):
    description: ShortText
    evidence_refs: list[Sha256Hex] = Field(default_factory=list)
    confirmed: Literal[False] = False  # causes are possible, never asserted


class IncidentReport(StrictModel):
    summary: LongText
    causes: list[PossibleCause] = Field(max_length=10)
    generated_at: AwareDatetime


class Stakes(StrictModel):
    due_at: AwareDatetime | None
    slack_s: float | None
    payload: Identifier | None
    precedence: PrecedenceClass


class Incident(StrictModel):
    incident_id: Identifier
    robot_urn: Urn
    rmf_task_id: Identifier
    job_urn: Urn | None
    segment_urn: Urn | None
    detected_at: AwareDatetime
    trigger: IncidentTrigger
    stakes: Stakes
    status: IncidentStatus = IncidentStatus.OPEN
    plan_id: Identifier | None = None
    outcome: ShortText | None = None
    report: IncidentReport | None = None


# --------------------------------------------------------------------------- 3.6 Evidence


class EvidenceSource(StrEnum):
    TAVILY = "tavily"
    OPERATOR = "operator"
    SYSTEM = "system"


class VendorAdvisory(StrictModel):
    """Typed extraction of a vendor bulletin (spec section 12)."""

    fault_code: Identifier
    meaning: ShortText
    expected_recovery_s: int | None = Field(default=None, ge=0)
    requires_manual_reset: bool | None = None
    affected_versions: list[Identifier] = Field(default_factory=list)


class Evidence(StrictModel):
    source: EvidenceSource
    url: Annotated[str, Field(max_length=2000)] | None
    retrieved_at: AwareDatetime
    content_sha256: Sha256Hex
    summary: Annotated[str, Field(max_length=1000)]
    extracted: VendorAdvisory | None = None
    trust: Literal["untrusted"] = "untrusted"
