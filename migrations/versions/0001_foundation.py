"""Foundation schema: jobs, segments, claims, conflicts, incidents, commands, events, ledger.

Revision ID: 0001
Revises:
Create Date: 2026-10-26
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

OPERATIONAL_TABLES = ("jobs", "segments", "claims", "conflicts", "incidents", "commands", "events")


def _app_role() -> str:
    role = str(op.get_context().config.attributes["app_role"])  # type: ignore[union-attr]
    if not _ROLE_RE.fullmatch(role):
        raise ValueError(f"app role name must match {_ROLE_RE.pattern}")
    return role


def upgrade() -> None:
    role = _app_role()

    op.execute("""
    CREATE TABLE jobs (
        urn            text PRIMARY KEY,
        source         text NOT NULL,
        external_id    text NOT NULL,
        kind           text NOT NULL,
        precedence     text NOT NULL,
        status         text NOT NULL,
        status_at      timestamptz NOT NULL,
        due_at         timestamptz,
        rmf_task_id    text,
        data           jsonb NOT NULL,
        created_at     timestamptz NOT NULL DEFAULT now(),
        updated_at     timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT jobs_source_external_id_key UNIQUE (source, external_id)
    );
    CREATE INDEX jobs_status_idx ON jobs (status);
    CREATE INDEX jobs_rmf_task_id_idx ON jobs (rmf_task_id) WHERE rmf_task_id IS NOT NULL;

    CREATE TABLE segments (
        urn            text PRIMARY KEY,
        kind           text NOT NULL CHECK (kind IN ('corridor', 'lift', 'door', 'room')),
        level          text,
        exclusive      boolean NOT NULL,
        data           jsonb NOT NULL,
        updated_at     timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE claims (
        claim_id       text PRIMARY KEY,
        job_urn        text NOT NULL,
        actor_urn      text NOT NULL,
        segment_urn    text NOT NULL,
        window_start   timestamptz NOT NULL,
        window_end     timestamptz NOT NULL,
        confidence     double precision NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
        basis          text NOT NULL,
        active         boolean NOT NULL DEFAULT true,
        created_at     timestamptz NOT NULL DEFAULT now(),
        CHECK (window_end > window_start)
    );
    CREATE INDEX claims_segment_window_idx ON claims (segment_urn, window_start, window_end)
        WHERE active;
    CREATE INDEX claims_job_idx ON claims (job_urn);

    CREATE TABLE conflicts (
        conflict_id    text PRIMARY KEY,
        segment_urn    text NOT NULL,
        status         text NOT NULL DEFAULT 'open',
        detected_at    timestamptz NOT NULL,
        data           jsonb NOT NULL,
        updated_at     timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX conflicts_status_idx ON conflicts (status);

    CREATE TABLE incidents (
        incident_id    text PRIMARY KEY,
        robot_urn      text NOT NULL,
        rmf_task_id    text NOT NULL,
        job_urn        text,
        segment_urn    text,
        trigger        text NOT NULL,
        status         text NOT NULL DEFAULT 'open',
        detected_at    timestamptz NOT NULL,
        data           jsonb NOT NULL,
        updated_at     timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX incidents_status_idx ON incidents (status);

    CREATE TABLE commands (
        command_id     text PRIMARY KEY,
        plan_id        text NOT NULL,
        action         text NOT NULL,
        side           text NOT NULL CHECK (side IN ('api', 'ros')),
        status         text NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')),
        payload        jsonb NOT NULL,
        result         jsonb,
        attempts       integer NOT NULL DEFAULT 0,
        not_before     timestamptz,
        created_at     timestamptz NOT NULL DEFAULT now(),
        updated_at     timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX commands_pending_idx ON commands (side, created_at) WHERE status = 'pending';

    CREATE TABLE events (
        event_id       text PRIMARY KEY,
        source         text NOT NULL,
        kind           text NOT NULL,
        payload        jsonb NOT NULL,
        received_at    timestamptz NOT NULL DEFAULT now(),
        processed_at   timestamptz,
        error          text
    );
    CREATE INDEX events_pending_idx ON events (received_at) WHERE processed_at IS NULL;

    CREATE FUNCTION events_notify() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        PERFORM pg_notify('auspex_events', NEW.event_id);
        RETURN NEW;
    END $$;
    CREATE TRIGGER events_notify_trg AFTER INSERT ON events
        FOR EACH ROW EXECUTE FUNCTION events_notify();

    CREATE FUNCTION commands_notify() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        PERFORM pg_notify('auspex_commands', NEW.command_id);
        RETURN NEW;
    END $$;
    CREATE TRIGGER commands_notify_trg AFTER INSERT ON commands
        FOR EACH ROW EXECUTE FUNCTION commands_notify();

    CREATE TABLE ledger (
        seq            bigint PRIMARY KEY CHECK (seq >= 1),
        entry_id       uuid NOT NULL UNIQUE,
        kind           text NOT NULL,
        recorded_at    timestamptz NOT NULL,
        payload        jsonb NOT NULL,
        body           text NOT NULL,
        prev_hash      char(64) NOT NULL,
        entry_hash     char(64) NOT NULL UNIQUE
    );
    CREATE INDEX ledger_kind_idx ON ledger (kind);

    CREATE FUNCTION ledger_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'ledger is append-only (% refused)', TG_OP
            USING ERRCODE = 'insufficient_privilege';
    END $$;
    CREATE TRIGGER ledger_no_update_delete BEFORE UPDATE OR DELETE ON ledger
        FOR EACH ROW EXECUTE FUNCTION ledger_append_only();
    CREATE TRIGGER ledger_no_truncate BEFORE TRUNCATE ON ledger
        FOR EACH STATEMENT EXECUTE FUNCTION ledger_append_only();
    """)

    tables = ", ".join(OPERATIONAL_TABLES)
    op.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
    op.execute(f'GRANT SELECT, INSERT, UPDATE ON {tables} TO "{role}"')
    # The event queue and command queue are worked in place; nothing is deleted by the app.
    op.execute(f'GRANT SELECT, INSERT ON ledger TO "{role}"')
    op.execute(f'REVOKE UPDATE, DELETE, TRUNCATE ON ledger FROM "{role}"')


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS ledger, events, commands, incidents, conflicts, claims, segments, jobs;
    DROP FUNCTION IF EXISTS ledger_append_only(), events_notify(), commands_notify();
    """)
