"""Plans (spec 3.4, 9): a plan's status through gate, approval, commands and verification.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-27
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _app_role() -> str:
    role = str(op.get_context().config.attributes["app_role"])  # type: ignore[union-attr]
    if not _ROLE_RE.fullmatch(role):
        raise ValueError(f"app role name must match {_ROLE_RE.pattern}")
    return role


def upgrade() -> None:
    op.execute("""
    CREATE TABLE plans (
        plan_id        text PRIMARY KEY,
        status         text NOT NULL CHECK (status IN ('blocked', 'awaiting_approval',
                       'rejected', 'expired', 'executing', 'verifying', 'completed', 'failed')),
        producer       text NOT NULL,
        subject        text,
        plan           jsonb NOT NULL,
        decision       jsonb,
        approval       jsonb,
        verification   jsonb,
        created_at     timestamptz NOT NULL DEFAULT now(),
        updated_at     timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX plans_status_idx ON plans (status);
    ALTER TABLE commands DROP CONSTRAINT commands_side_check;
    ALTER TABLE commands ADD CONSTRAINT commands_side_check
        CHECK (side IN ('api', 'ros', 'core'));
    ALTER TABLE commands ADD COLUMN seq integer NOT NULL DEFAULT 0;
    ALTER TABLE commands ADD COLUMN inverse_of text;
    ALTER TABLE commands ADD COLUMN ledgered boolean NOT NULL DEFAULT false;
    CREATE INDEX commands_plan_idx ON commands (plan_id, seq);
    """)
    op.execute(f'GRANT SELECT, INSERT, UPDATE ON plans TO "{_app_role()}"')


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS plans;
    DROP INDEX IF EXISTS commands_plan_idx;
    ALTER TABLE commands DROP COLUMN IF EXISTS seq;
    ALTER TABLE commands DROP COLUMN IF EXISTS inverse_of;
    ALTER TABLE commands DROP COLUMN IF EXISTS ledgered;
    DELETE FROM commands WHERE side = 'core';
    ALTER TABLE commands DROP CONSTRAINT commands_side_check;
    ALTER TABLE commands ADD CONSTRAINT commands_side_check CHECK (side IN ('api', 'ros'));
    """)
