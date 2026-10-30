"""Evidence (spec 3.6, 12): stored bulletins by content hash, and the search cache.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-27
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
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
    CREATE TABLE evidence (
        content_sha256 text PRIMARY KEY CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
        source         text NOT NULL,
        url            text,
        retrieved_at   timestamptz NOT NULL,
        query          text NOT NULL,
        data           jsonb NOT NULL,
        content        text NOT NULL
    );
    CREATE TABLE search_cache (
        query_hash     text PRIMARY KEY,
        provider       text NOT NULL,
        query          jsonb NOT NULL,
        hits           jsonb NOT NULL,
        fetched_at     timestamptz NOT NULL DEFAULT now()
    );
    """)
    role = _app_role()
    op.execute(f'GRANT SELECT, INSERT, UPDATE ON evidence TO "{role}"')
    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON search_cache TO "{role}"')


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS search_cache; DROP TABLE IF EXISTS evidence;")
