"""Let the app role delete processed ROS events (high-rate state; retention in the consumer).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-26
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _app_role() -> str:
    role = str(op.get_context().config.attributes["app_role"])  # type: ignore[union-attr]
    if not _ROLE_RE.fullmatch(role):
        raise ValueError(f"app role name must match {_ROLE_RE.pattern}")
    return role


def upgrade() -> None:
    op.execute(f'GRANT DELETE ON events TO "{_app_role()}"')
    op.execute(
        "CREATE INDEX events_ros_processed_idx ON events (processed_at) WHERE source = 'ros'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS events_ros_processed_idx")
    op.execute(f'REVOKE DELETE ON events FROM "{_app_role()}"')
