"""Event consumer: LISTEN on the queue, process events in arrival order, mark each done last.

Each event is handled in its own transaction together with its effects (job rows, ledger
entries), so a crash never leaves an event half-applied. A handler failure rolls back the
effects and marks the event with its error (it is not retried forever); the error is logged
and counted on /health.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import psycopg
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Engine, text

from auspex.core.db import libpq_url
from auspex.core.errors import AuspexError
from auspex.core.logging import get_logger
from auspex.ingest.events import CHANNEL, Event, claim_batch, mark_done, prune_ros_events
from auspex.ingest.facility import LiftStatus
from auspex.ingest.hospital import (
    DeliveryRecord,
    SourceRecord,
    StatusUpdate,
    TransportRecord,
    apply_status,
    normalize,
)
from auspex.ledger.store import LedgerKind, append
from auspex.picture.jobs import get_job, update_status, upsert_job
from auspex.picture.ros_state import RosState
from auspex.policy.loader import Policy
from auspex.site.model import Site

log = get_logger(__name__)

RECORD_ADAPTER: TypeAdapter[TransportRecord | DeliveryRecord] = TypeAdapter(SourceRecord)
NOTIFY_WAIT_S = 2.0
PRUNE_EVERY_S = 600.0


@dataclass
class ConsumerStats:
    processed: Counter[str] = field(default_factory=Counter)
    errors: int = 0
    last_error: str | None = None
    last_event_at: datetime | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict[str, Any]:
        return {
            "processed": dict(self.processed),
            "errors": self.errors,
            "last_error": self.last_error,
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
            "started_at": self.started_at.isoformat(),
        }


class Consumer:
    def __init__(
        self,
        *,
        engine: Engine,
        app_url: str,
        site: Site,
        policy: Policy,
        ros: RosState,
        batch_size: int = 200,
        ros_retention_s: int = 3600,
    ) -> None:
        self.engine = engine
        self.app_url = libpq_url(app_url)
        self.site = site
        self.policy = policy
        self.ros = ros
        self.batch_size = batch_size
        self.ros_retention_s = ros_retention_s
        self.stats = ConsumerStats()
        # Called after a facility lift status is committed (decision engine fast lane).
        self.on_lift: Callable[[str, str, str | None], None] | None = None
        self._lift_updates: list[tuple[str, str, str | None]] = []
        # Called after a job record is committed (decision engine: interpretation in C).
        self.on_job: Callable[[str], None] | None = None
        self._job_updates: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="event-consumer", daemon=True)
        self._handlers: dict[tuple[str, str], Callable[[Any, Event], str]] = {
            ("hospital", "record"): self._on_record,
            ("hospital", "status"): self._on_status,
            ("facility", "lift_status"): self._on_lift_status,
        }

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._thread.start()
        log.info("consumer.started", channel=CHANNEL, batch=self.batch_size)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)
        log.info("consumer.stopped", processed=sum(self.stats.processed.values()))

    def _run(self) -> None:
        backoff = 1.0
        last_prune = 0.0
        while not self._stop.is_set():
            try:
                with psycopg.connect(self.app_url, autocommit=True, connect_timeout=5) as listen:
                    listen.execute(f"LISTEN {CHANNEL}")
                    backoff = 1.0
                    while not self._stop.is_set():
                        self.drain()
                        if time.monotonic() - last_prune > PRUNE_EVERY_S:
                            self._prune()
                            last_prune = time.monotonic()
                        for _ in listen.notifies(timeout=NOTIFY_WAIT_S, stop_after=1):
                            pass
            except (psycopg.OperationalError, OSError) as exc:
                log.error(
                    "consumer.db_unavailable", error=str(exc).splitlines()[0], retry_s=backoff
                )
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    # ------------------------------------------------------------------ processing

    def drain(self) -> int:
        total = 0
        while not self._stop.is_set():
            with self.engine.connect() as conn:
                batch = claim_batch(conn, self.batch_size)
                conn.rollback()
            if not batch:
                return total
            for event in batch:
                self._process(event)
            total += len(batch)
        return total

    def _process(self, event: Event) -> None:
        key = f"{event.source}/{event.kind}"
        try:
            with self.engine.begin() as conn:
                still_pending = conn.execute(
                    text(
                        "SELECT 1 FROM events WHERE event_id = :id AND processed_at IS NULL"
                        " FOR UPDATE SKIP LOCKED"
                    ),
                    {"id": event.event_id},
                ).first()
                if still_pending is None:
                    return
                if event.source == "ros":
                    self.ros.apply(event.kind, event.payload)
                    outcome = "applied"
                else:
                    handler = self._handlers.get((event.source, event.kind))
                    if handler is None:
                        raise AuspexError(
                            "no handler for event", source=event.source, kind=event.kind
                        )
                    outcome = handler(conn, event)
                mark_done(conn, event.event_id)
            self._notify_lifts()
            self.stats.processed[key] += 1
            self.stats.last_event_at = datetime.now(UTC)
            if event.source != "ros":
                log.info("event.processed", event_id=event.event_id, kind=key, outcome=outcome)
        except (AuspexError, ValidationError) as exc:
            self._fail(event, key, str(exc))
        except Exception as exc:  # a bug: record it on the event, keep consuming
            log.exception("event.handler_crashed", event_id=event.event_id, kind=key)
            self._fail(event, key, f"{type(exc).__name__}: {exc}")

    def _notify_lifts(self) -> None:
        jobs, self._job_updates = self._job_updates, []
        if self.on_job is not None:
            for urn in jobs:
                self.on_job(urn)
        updates, self._lift_updates = self._lift_updates, []
        if self.on_lift is None:
            return
        for lift, mode, fault in updates:
            self.on_lift(lift, mode, fault)

    def _fail(self, event: Event, key: str, error: str) -> None:
        self._lift_updates = []
        self._job_updates = []
        self.stats.errors += 1
        self.stats.last_error = f"{event.event_id}: {error}"[:500]
        log.error("event.failed", event_id=event.event_id, kind=key, error=error[:500])
        with self.engine.begin() as conn:
            mark_done(conn, event.event_id, error=error[:2000])

    def _prune(self) -> None:
        try:
            with self.engine.begin() as conn:
                n = prune_ros_events(conn, self.ros_retention_s)
            if n:
                log.info("events.pruned", source="ros", deleted=n)
        except Exception as exc:  # pruning is housekeeping; never stop consuming for it
            log.warning("events.prune_failed", error=str(exc).splitlines()[0])

    # ------------------------------------------------------------------ handlers

    def _on_record(self, conn: Any, event: Event) -> str:
        record = RECORD_ADAPTER.validate_python(event.payload["record"])
        job = normalize(record, self.site, self.policy)
        outcome = upsert_job(conn, job, record.model_dump(mode="json"))
        self._job_updates.append(job.urn)
        return outcome

    def _on_status(self, conn: Any, event: Event) -> str:
        urn = str(event.payload["job_urn"])
        update = StatusUpdate.model_validate(event.payload["update"])
        found = get_job(conn, urn)
        if found is None:
            raise AuspexError("status for an unknown job", urn=urn)
        old, raw = found
        new = apply_status(old, update)
        if new is None:
            log.info("job.stale_status_ignored", urn=urn, at=update.status_at.isoformat())
            return "stale"
        outcome = update_status(conn, old, new, raw)
        self._job_updates.append(urn)  # e.g. a job ending lets the engine forget its cases
        return outcome

    def _on_lift_status(self, conn: Any, event: Event) -> str:
        lift = str(event.payload["lift_name"])
        status = LiftStatus.model_validate(event.payload["status"])
        payload = {"lift_name": lift, **status.model_dump(mode="json")}
        append(conn, LedgerKind.FACILITY_STATUS, {"kind": "lift_status", **payload})
        self.ros.apply("facility_lift", payload)
        self._lift_updates.append((lift, status.mode, status.fault_code))
        log.warning(
            "facility.lift_status", lift=lift, mode=status.mode, fault_code=status.fault_code
        )
        return status.mode
