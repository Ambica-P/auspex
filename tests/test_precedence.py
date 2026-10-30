"""Spec 18.4 test 3: priority mapping and precedence order the S1-S3 jobs correctly."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from auspex.core.errors import UnmappedPriority
from auspex.core.models import Job, JobKind, JobStatus
from auspex.policy.loader import Policy, load_policy

T = datetime(2026, 10, 26, 10, 28, tzinfo=UTC)


@pytest.fixture(scope="module")
def policy() -> Policy:
    return load_policy()


def _job(
    policy: Policy,
    ident: str,
    source: str,
    priority: str,
    *,
    kind: JobKind = JobKind.HUMAN_TRANSPORT,
    destination_type: str | None = None,
    payload: str | None = None,
) -> Job:
    return Job(
        urn=f"job/{ident}",
        kind=kind,
        source=source,
        external_id=ident,
        precedence=policy.map_priority(source, priority, destination_type),
        origin="zone/ward-2B",
        destination_type=destination_type,
        payload=payload,
        status=JobStatus.RESPONDING,
        status_at=T,
    )


def test_s1_stat_bariatric_transport_outranks_oxygen(policy: Policy) -> None:
    # Job 4471: Stat, destination "CT 2" not yet interpreted (destination_type unknown).
    transport = _job(policy, "4471", "transport_system", "Stat", payload="patient")
    oxygen = _job(
        policy,
        "o2-bed12",
        "delivery_requests",
        "Critical",
        kind=JobKind.ROBOT_TASK,
        payload="oxygen",
    )
    assert transport.precedence.value == "stat"
    assert oxygen.precedence.value == "critical"
    assert policy.order([oxygen, transport]) == [transport, oxygen]


def test_s3_ed_stat_first_then_imaging_stat_then_oxygen(policy: Policy) -> None:
    ed = _job(policy, "5001", "transport_system", "STAT", destination_type="emergency_department")
    imaging = _job(policy, "5002", "transport_system", "stat", destination_type="imaging")
    oxygen = _job(
        policy,
        "o2-bed12",
        "delivery_requests",
        "Critical",
        kind=JobKind.ROBOT_TASK,
        payload="oxygen",
    )
    other_critical = _job(
        policy,
        "lab-7",
        "delivery_requests",
        "Critical",
        kind=JobKind.ROBOT_TASK,
        payload="specimen",
    )
    routine = _job(policy, "linen-3", "delivery_requests", "Normal", kind=JobKind.ROBOT_TASK)
    ordered = policy.order([routine, other_critical, oxygen, imaging, ed])
    assert [j.external_id for j in ordered] == ["5001", "5002", "o2-bed12", "lab-7", "linen-3"]


def test_same_class_stat_without_ed_is_a_tie(policy: Policy) -> None:
    a = _job(policy, "6001", "transport_system", "Stat", destination_type="imaging")
    b = _job(policy, "6002", "transport_system", "Stat", destination_type="imaging")
    assert policy.rank(a) == policy.rank(b)  # a tie: ordering them is a person's call


def test_code_outranks_everything_and_unknown_priority_is_refused(policy: Policy) -> None:
    code = _job(policy, "cb-1", "transport_system", "Code", kind=JobKind.CLINICAL_EVENT)
    ed = _job(policy, "5001", "transport_system", "Stat", destination_type="emergency_department")
    assert policy.order([ed, code]) == [code, ed]
    with pytest.raises(UnmappedPriority):
        policy.map_priority("transport_system", "ASAP")
    with pytest.raises(UnmappedPriority):  # Critical maps only for delivery requests
        policy.map_priority("transport_system", "Critical")
