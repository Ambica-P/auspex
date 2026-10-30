"""Hospital source records and their deterministic mapping to `Job` (spec 3.1, 5.1 step 1).

The record shapes follow real systems: a transport system (Epic Transport / TeleTracking
style: request id, priority, coded origin and destination plus free text, mode, bed type,
needed-by, timestamped status) and a delivery-request system for robot deliveries.

Intake is strictly deterministic:
- precedence comes from hospital policy (priority mapping), never inferred;
- locations resolve ONLY from the source's structured codes (site/zones.yaml `codes`).
  Free text (destination_text, notes) is stored untouched and untrusted; interpreting it is
  Nemotron's job (M10), validated against the site model;
- an origin without a known code is refused: the record cannot be placed at all;
- source status names map through fixed vocabularies; an unknown status is refused.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from auspex.core.errors import AuspexError
from auspex.core.models import (
    Identifier,
    Job,
    JobKind,
    JobStatus,
    LongText,
    StrictModel,
    strip_control,
)
from auspex.core.urns import make_urn
from auspex.policy.loader import Policy
from auspex.site.model import Site

TRANSPORT_SOURCE = "transport_system"
DELIVERY_SOURCE = "delivery_requests"


class IntakeError(AuspexError):
    """A source record cannot be mapped to a job. Reported to the sender (HTTP 422)."""

    code = "intake_error"


# Source status vocabularies (case-insensitive) -> JobStatus.
TRANSPORT_STATUS: dict[str, JobStatus] = {
    "requested": JobStatus.REQUESTED,
    "pending": JobStatus.REQUESTED,
    "assigned": JobStatus.ASSIGNED,
    "dispatched": JobStatus.ASSIGNED,
    "acknowledged": JobStatus.ASSIGNED,
    "responding": JobStatus.RESPONDING,
    "en route": JobStatus.RESPONDING,
    "picked up": JobStatus.PICKED_UP,
    "in transit": JobStatus.PICKED_UP,
    "delayed": JobStatus.DELAYED,
    "complete": JobStatus.COMPLETED,
    "completed": JobStatus.COMPLETED,
    "cancelled": JobStatus.CANCELLED,
    "canceled": JobStatus.CANCELLED,
}
DELIVERY_STATUS: dict[str, JobStatus] = {
    "submitted": JobStatus.REQUESTED,
    "queued": JobStatus.REQUESTED,
    "assigned": JobStatus.ASSIGNED,
    "underway": JobStatus.PICKED_UP,
    "in progress": JobStatus.PICKED_UP,
    "delayed": JobStatus.DELAYED,
    "delivered": JobStatus.COMPLETED,
    "completed": JobStatus.COMPLETED,
    "cancelled": JobStatus.CANCELLED,
    "canceled": JobStatus.CANCELLED,
}
VOCABULARY = {TRANSPORT_SOURCE: TRANSPORT_STATUS, DELIVERY_SOURCE: DELIVERY_STATUS}

Code = Annotated[str, Field(min_length=1, max_length=64)]
FreeText = Annotated[str, Field(max_length=500)]


class TransportRecord(StrictModel):
    """A transport-system request (porter job: patient, bed or equipment)."""

    system: Literal["transport_system"]
    request_id: Identifier
    priority: Annotated[str, Field(min_length=1, max_length=32)]
    origin_code: Code
    origin_text: FreeText | None = None
    destination_code: Code | None = None
    destination_text: FreeText | None = None
    destination_type: Identifier | None = None  # set by the source only when coded
    transport_mode: Identifier | None = None
    bed_type: Identifier | None = None
    needed_by: AwareDatetime | None = None
    patient_ref: Identifier | None = None  # pseudonymous
    requested_by: Identifier | None = None
    notes: LongText | None = None
    status: Annotated[str, Field(min_length=1, max_length=32)]
    status_at: AwareDatetime


class DeliveryRecord(StrictModel):
    """A delivery request for a robot (supplies, medication, oxygen)."""

    system: Literal["delivery_requests"]
    request_id: Identifier
    priority: Annotated[str, Field(min_length=1, max_length=32)]
    item: Identifier
    pickup_code: Code
    dropoff_code: Code | None = None
    dropoff_text: FreeText | None = None
    needed_by: AwareDatetime | None = None
    requested_by: Identifier | None = None
    patient_ref: Identifier | None = None
    receiver: Identifier | None = None  # device that takes the item (e.g. a bedside arm)
    rmf_task_id: Identifier | None = None
    notes: LongText | None = None
    status: Annotated[str, Field(min_length=1, max_length=32)]
    status_at: AwareDatetime


SourceRecord = Annotated[TransportRecord | DeliveryRecord, Field(discriminator="system")]


class StatusUpdate(StrictModel):
    """A status change for a known job, in the source's own vocabulary."""

    status: Annotated[str, Field(min_length=1, max_length=32)]
    status_at: AwareDatetime
    rmf_task_id: Identifier | None = None


def job_urn(source: str, external_id: str) -> str:
    return make_urn("job", f"{source}-{external_id}")


def map_status(source: str, status: str) -> JobStatus:
    vocab = VOCABULARY.get(source)
    mapped = vocab.get(status.strip().casefold()) if vocab else None
    if mapped is None:
        raise IntakeError("unknown source status", source=source, status=status)
    return mapped


def _zone(site: Site, code: str | None, what: str, request_id: str) -> str | None:
    if code is None:
        return None
    zone = site.resolve_code(code)
    if zone is None:
        raise IntakeError(f"unknown {what} code", code=code, request_id=request_id)
    return zone


def _clean(text: str | None) -> str | None:
    return strip_control(text) if text is not None else None


def _destination_type(site: Site, zone: str | None, given: str | None) -> str | None:
    """The source's own destination type wins; else the coded zone's type (zones.yaml)."""
    if given is not None or zone is None:
        return given
    return site.zone(zone).destination_type


def normalize(record: TransportRecord | DeliveryRecord, site: Site, policy: Policy) -> Job:
    if isinstance(record, TransportRecord):
        origin = _zone(site, record.origin_code, "origin", record.request_id)
        destination = _zone(site, record.destination_code, "destination", record.request_id)
        dest_type = _destination_type(site, destination, record.destination_type)
        assert origin is not None
        return Job(
            urn=job_urn(TRANSPORT_SOURCE, record.request_id),
            kind=JobKind.HUMAN_TRANSPORT,
            source=TRANSPORT_SOURCE,
            external_id=record.request_id,
            precedence=policy.map_priority(TRANSPORT_SOURCE, record.priority, dest_type),
            origin=origin,
            destination=destination,
            destination_text=_clean(record.destination_text),
            destination_type=dest_type,
            payload="patient" if record.patient_ref else None,
            transport_mode=record.transport_mode,
            bed_type=record.bed_type,
            due_at=record.needed_by,
            requester_urn=make_urn("person", record.requested_by) if record.requested_by else None,
            beneficiary_urn=make_urn("patient", record.patient_ref) if record.patient_ref else None,
            status=map_status(TRANSPORT_SOURCE, record.status),
            status_at=record.status_at,
            notes=_clean(record.notes),
        )
    origin = _zone(site, record.pickup_code, "pickup", record.request_id)
    destination = _zone(site, record.dropoff_code, "dropoff", record.request_id)
    assert origin is not None
    return Job(
        urn=job_urn(DELIVERY_SOURCE, record.request_id),
        kind=JobKind.ROBOT_TASK,
        source=DELIVERY_SOURCE,
        external_id=record.request_id,
        precedence=policy.map_priority(DELIVERY_SOURCE, record.priority, None),
        origin=origin,
        destination=destination,
        destination_text=_clean(record.dropoff_text),
        destination_type=_destination_type(site, destination, None),
        payload=record.item,
        due_at=record.needed_by,
        requester_urn=make_urn("person", record.requested_by) if record.requested_by else None,
        beneficiary_urn=make_urn("patient", record.patient_ref) if record.patient_ref else None,
        receiver_urn=make_urn("device", record.receiver) if record.receiver else None,
        status=map_status(DELIVERY_SOURCE, record.status),
        status_at=record.status_at,
        rmf_task_id=record.rmf_task_id,
        notes=_clean(record.notes),
    )


def apply_status(job: Job, update: StatusUpdate) -> Job | None:
    """The job with the new status, or None when the update is older than the job's status."""
    if update.status_at < job.status_at:
        return None
    changes: dict[str, object] = {
        "status": map_status(job.source, update.status),
        "status_at": update.status_at,
    }
    if update.rmf_task_id is not None:
        changes["rmf_task_id"] = update.rmf_task_id
    return Job.model_validate({**job.model_dump(), **changes})
