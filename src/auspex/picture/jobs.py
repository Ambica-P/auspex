"""Job store (Postgres `jobs`), with every change recorded in the ledger in the same transaction.

Upserts are keyed by (source, external_id): a resent record with the same content changes
nothing; a newer status updates the job; an older one is ignored (records arrive out of
order from real feeds). The raw source record is kept beside the job for the decision panel.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Connection, text

from auspex.core.canonical import canonical_json
from auspex.core.logging import get_logger
from auspex.core.models import Job, JobStatus
from auspex.ledger.store import LedgerKind, append

log = get_logger(__name__)


def get_job(conn: Connection, urn: str) -> tuple[Job, dict[str, Any]] | None:
    row = conn.execute(text("SELECT data FROM jobs WHERE urn = :urn"), {"urn": urn}).first()
    if row is None:
        return None
    data = dict(row.data)
    return Job.model_validate(data["job"]), dict(data.get("raw") or {})


def list_jobs(conn: Connection, limit: int = 200) -> list[tuple[Job, dict[str, Any]]]:
    rows = conn.execute(
        text("SELECT data FROM jobs ORDER BY status_at DESC LIMIT :n"), {"n": limit}
    ).all()
    out = []
    for r in rows:
        data = dict(r.data)
        out.append((Job.model_validate(data["job"]), dict(data.get("raw") or {})))
    return out


def _write(conn: Connection, job: Job, raw: dict[str, Any]) -> None:
    data = json.loads(canonical_json({"job": job, "raw": raw}))
    conn.execute(
        text(
            "INSERT INTO jobs (urn, source, external_id, kind, precedence, status, status_at,"
            " due_at, rmf_task_id, data) VALUES (:urn, :source, :external_id, :kind,"
            " :precedence, :status, :status_at, :due_at, :rmf_task_id, CAST(:data AS jsonb))"
            " ON CONFLICT (urn) DO UPDATE SET kind = EXCLUDED.kind,"
            " precedence = EXCLUDED.precedence, status = EXCLUDED.status,"
            " status_at = EXCLUDED.status_at, due_at = EXCLUDED.due_at,"
            " rmf_task_id = EXCLUDED.rmf_task_id, data = EXCLUDED.data, updated_at = now()"
        ),
        {
            "urn": job.urn,
            "source": job.source,
            "external_id": job.external_id,
            "kind": job.kind.value,
            "precedence": job.precedence.value,
            "status": job.status.value,
            "status_at": job.status_at,
            "due_at": job.due_at,
            "rmf_task_id": job.rmf_task_id,
            "data": json.dumps(data),
        },
    )


_ENDED = (JobStatus.COMPLETED, JobStatus.CANCELLED)


def upsert_job(conn: Connection, job: Job, raw: dict[str, Any]) -> str:
    """Insert or update a job from a full source record. Returns what happened."""
    existing = get_job(conn, job.urn)
    if existing is None:
        _write(conn, job, raw)
        append(conn, LedgerKind.JOB_RECEIVED, {"job": job.model_dump(mode="json"), "raw": raw})
        log.info(
            "job.received",
            urn=job.urn,
            precedence=job.precedence.value,
            status=job.status.value,
            destination=job.destination,
        )
        return "received"
    old, _ = existing
    if job.status_at < old.status_at:
        # The record is older than the job's latest status (a status update overtook it): its
        # status is out of date, but its fields (deadline, destination, notes) are the source's
        # current ones. Keep the newer status, take the rest from the record.
        log.info(
            "job.late_record_merged",
            urn=job.urn,
            at=job.status_at.isoformat(),
            current_at=old.status_at.isoformat(),
        )
        job = job.model_copy(
            update={
                "status": old.status,
                "status_at": old.status_at,
                "rmf_task_id": job.rmf_task_id or old.rmf_task_id,
            }
        )
    # A record for a job that had ended (completed or cancelled) and is active again is a new
    # case: nothing inferred or linked for the old one carries over (it is interpreted afresh).
    reopened = old.status in _ENDED and job.status not in _ENDED
    if reopened:
        log.info("job.reopened", urn=job.urn, status_from=old.status.value)
    # A resent record that still leaves a field missing keeps what was inferred for it (same
    # free text) and the robot task already linked to the job.
    keep: dict[str, Any] = (
        {}
        if reopened
        else {f: getattr(old, f) for f in old.inferred_fields if getattr(job, f, None) is None}
    )
    if keep and job.destination_text == old.destination_text:
        job = job.model_copy(
            update={
                **keep,
                "inferred_fields": old.inferred_fields,
                "inference_confidence": old.inference_confidence,
            }
        )
    if job.rmf_task_id is None and old.rmf_task_id is not None and not reopened:
        job = job.model_copy(update={"rmf_task_id": old.rmf_task_id})
    if job == old:
        return "unchanged"
    _write(conn, job, raw)
    append(
        conn,
        LedgerKind.JOB_STATUS,
        {
            "urn": job.urn,
            "from": old.status.value,
            "to": job.status.value,
            "at": job.status_at,
            "job": job.model_dump(mode="json"),
        },
    )
    log.info("job.updated", urn=job.urn, status_from=old.status.value, status_to=job.status.value)
    return "updated"


def update_status(conn: Connection, old: Job, new: Job, raw: dict[str, Any]) -> str:
    if old.status in _ENDED and new.status not in _ENDED and new.inferred_fields:
        # an ended job made active again is a new case: nothing inferred for it carries over
        cleared = dict.fromkeys(new.inferred_fields)
        new = new.model_copy(
            update={**cleared, "inferred_fields": [], "inference_confidence": None}
        )
        log.info("job.reopened", urn=new.urn, status_from=old.status.value)
    if new == old:
        return "unchanged"
    _write(conn, new, raw)
    append(
        conn,
        LedgerKind.JOB_STATUS,
        {"urn": new.urn, "from": old.status.value, "to": new.status.value, "at": new.status_at},
    )
    log.info("job.status", urn=new.urn, status_from=old.status.value, status_to=new.status.value)
    return "updated"


def link_task(conn: Connection, urn: str, task_id: str, by: str) -> bool:
    """Record the Open-RMF task now doing this job (a task Auspex submitted). False if unknown."""
    found = get_job(conn, urn)
    if found is None:
        log.warning("job.link_unknown", urn=urn, task=task_id)
        return False
    old, raw = found
    if old.rmf_task_id == task_id:
        return True
    _write(conn, old.model_copy(update={"rmf_task_id": task_id}), raw)
    append(
        conn,
        LedgerKind.JOB_STATUS,
        {"urn": urn, "rmf_task_from": old.rmf_task_id, "rmf_task_to": task_id, "by": by},
    )
    log.info("job.task_linked", urn=urn, task=task_id, previous=old.rmf_task_id, by=by)
    return True


def with_interpretation(old: Job, destination: str, confidence: float) -> Job:
    """The job with Nemotron's inferred destination, listed in inferred_fields."""
    new = old.model_copy(
        update={
            "destination": destination,
            "inferred_fields": sorted({*old.inferred_fields, "destination"}),
            "inference_confidence": confidence,
        }
    )
    return Job.model_validate(new.model_dump())  # the inferred fields are still allowed ones


def apply_interpretation(
    conn: Connection, urn: str, destination: str, confidence: float, record: dict[str, Any]
) -> bool:
    """Record Nemotron's interpretation of a job: the destination it inferred, listed in
    inferred_fields with its confidence (never the precedence). Ledger INTERPRETATION."""
    found = get_job(conn, urn)
    if found is None:
        return False
    old, raw = found
    new = with_interpretation(old, destination, confidence)
    _write(conn, new, raw)
    append(conn, LedgerKind.INTERPRETATION, {"urn": urn, **record})
    log.info("job.interpreted", urn=urn, destination=destination, confidence=confidence)
    return True
