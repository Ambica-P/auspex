"""The Postgres event queue (spec 4): every input to the core is an event row.

Writers (the API's intake, the ROS bridge) insert with a unique `event_id`; a duplicate is
ignored, so retries and replays are idempotent. A trigger sends NOTIFY on `auspex_events`;
the consumer processes events in arrival order and marks each done last.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

from auspex.core.canonical import canonical_json, sha256_hex
from auspex.core.models import StrictModel

CHANNEL = "auspex_events"


class Event(StrictModel):
    event_id: str
    source: str
    kind: str
    payload: dict[str, Any]
    received_at: datetime


def content_event_id(source: str, kind: str, payload: dict[str, Any]) -> str:
    """Same content -> same id, so an identical resend is a no-op."""
    digest = sha256_hex(canonical_json({"source": source, "kind": kind, "payload": payload}))
    return f"{source}:{kind}:{digest[:32]}"


def insert_event(
    conn: Connection, event_id: str, source: str, kind: str, payload: dict[str, Any]
) -> bool:
    """Insert one event; False when an event with this id already exists."""
    row = conn.execute(
        text(
            "INSERT INTO events (event_id, source, kind, payload)"
            " VALUES (:event_id, :source, :kind, CAST(:payload AS jsonb))"
            " ON CONFLICT (event_id) DO NOTHING RETURNING event_id"
        ),
        {
            "event_id": event_id,
            "source": source,
            "kind": kind,
            "payload": canonical_json(payload).decode("utf-8"),
        },
    ).first()
    return row is not None


def claim_batch(conn: Connection, limit: int) -> list[Event]:
    """Lock the oldest unprocessed events (inside the caller's transaction)."""
    rows = conn.execute(
        text(
            "SELECT event_id, source, kind, payload, received_at FROM events"
            " WHERE processed_at IS NULL ORDER BY received_at, event_id"
            " LIMIT :n FOR UPDATE SKIP LOCKED"
        ),
        {"n": limit},
    ).all()
    return [
        Event(
            event_id=r.event_id,
            source=r.source,
            kind=r.kind,
            payload=dict(r.payload),
            received_at=r.received_at,
        )
        for r in rows
    ]


def mark_done(conn: Connection, event_id: str, error: str | None = None) -> None:
    conn.execute(
        text("UPDATE events SET processed_at = now(), error = :error WHERE event_id = :id"),
        {"id": event_id, "error": error},
    )


def prune_ros_events(conn: Connection, older_than_s: int) -> int:
    """Delete processed ROS events older than the retention (they are high-rate state)."""
    result = conn.execute(
        text(
            "DELETE FROM events WHERE source = 'ros' AND processed_at IS NOT NULL"
            " AND processed_at < now() - make_interval(secs => :s)"
        ),
        {"s": older_than_s},
    )
    return int(result.rowcount or 0)
