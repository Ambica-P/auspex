"""Ledger store on Postgres. Appends are serialized with a transaction-scoped advisory lock.

The app role can only INSERT and SELECT the `ledger` table; UPDATE, DELETE and TRUNCATE are
refused by grants and by a trigger (migration 0001).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Connection, Engine, text

from auspex.core.canonical import canonical_json
from auspex.core.errors import LedgerError
from auspex.core.logging import get_logger
from auspex.ledger.chain import GENESIS, LedgerEntry, compute_hash, make_body, verify_entries

log = get_logger(__name__)

_LOCK_KEY = 0x41555350  # "AUSP": serializes appends across processes


class LedgerKind(StrEnum):
    JOB_RECEIVED = "job_received"
    JOB_STATUS = "job_status"
    INTERPRETATION = "interpretation"
    CONFLICT = "conflict"
    INCIDENT = "incident"
    OPTIONS = "options"
    EVIDENCE = "evidence"
    PLAN = "plan"
    GATE_DECISION = "gate_decision"
    APPROVAL = "approval"
    COMMAND = "command"
    COMMAND_RESULT = "command_result"
    VERIFICATION = "verification"
    FORECAST_VS_ACTUAL = "forecast_vs_actual"
    INCIDENT_REPORT = "incident_report"
    FACILITY_STATUS = "facility_status"
    SYSTEM = "system"


def append(conn: Connection, kind: LedgerKind, payload: Mapping[str, Any]) -> LedgerEntry:
    """Append one entry inside the caller's transaction. Commit happens with the caller's."""
    conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
    last = conn.execute(
        text("SELECT seq, entry_hash FROM ledger ORDER BY seq DESC LIMIT 1")
    ).one_or_none()
    seq = 1 if last is None else int(last.seq) + 1
    prev_hash = GENESIS if last is None else str(last.entry_hash)
    entry_id = str(uuid.uuid4())
    recorded_at = datetime.now(UTC)
    body = make_body(seq, entry_id, kind.value, recorded_at, payload)
    entry_hash = compute_hash(prev_hash, body)
    conn.execute(
        text(
            "INSERT INTO ledger (seq, entry_id, kind, recorded_at, payload, body, prev_hash,"
            " entry_hash) VALUES (:seq, :entry_id, :kind, :recorded_at,"
            " CAST(:payload AS jsonb), :body, :prev_hash, :entry_hash)"
        ),
        {
            "seq": seq,
            "entry_id": entry_id,
            "kind": kind.value,
            "recorded_at": recorded_at,
            "payload": canonical_json(dict(payload)).decode("utf-8"),
            "body": body,
            "prev_hash": prev_hash,
            "entry_hash": entry_hash,
        },
    )
    log.info("ledger.appended", seq=seq, kind=kind.value, entry_hash=entry_hash[:12])
    return LedgerEntry(
        seq=seq,
        entry_id=entry_id,
        kind=kind.value,
        recorded_at=recorded_at,
        payload=json.loads(canonical_json(dict(payload))),
        body=body,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
    )


def iter_entries(conn: Connection, batch: int = 1000) -> Iterator[LedgerEntry]:
    after = 0
    while True:
        rows = conn.execute(
            text(
                "SELECT seq, entry_id, kind, recorded_at, payload, body, prev_hash, entry_hash"
                " FROM ledger WHERE seq > :after ORDER BY seq LIMIT :n"
            ),
            {"after": after, "n": batch},
        ).all()
        if not rows:
            return
        for r in rows:
            yield LedgerEntry(
                seq=int(r.seq),
                entry_id=str(r.entry_id),
                kind=str(r.kind),
                recorded_at=r.recorded_at,
                payload=dict(r.payload),
                body=str(r.body),
                prev_hash=str(r.prev_hash),
                entry_hash=str(r.entry_hash),
            )
        after = int(rows[-1].seq)


def verify_chain(engine: Engine) -> int:
    """Verify the whole ledger; return the entry count or raise LedgerChainBroken."""
    with engine.connect() as conn:
        try:
            count = verify_entries(iter_entries(conn))
        except LedgerError as exc:
            log.error("ledger.verify_failed", error=str(exc))
            raise
    log.info("ledger.verified", entries=count)
    return count
