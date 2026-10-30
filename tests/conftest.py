"""Shared fixtures. Database fixtures skip (with the reason) when TEST_* URLs are not set."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import Engine

from auspex.core.config import db_url_parts, get_settings
from auspex.core.db import (
    ensure_database_and_role,
    libpq_url,
    make_engine,
    with_database,
)
from auspex.core.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[1]


class DbHandles:
    def __init__(self, migration_url: str, app_url: str) -> None:
        self.migration_url = migration_url
        self.app_url = app_url
        self.admin: Engine = make_engine(migration_url)
        self.app: Engine = make_engine(app_url)


@pytest.fixture(scope="session")
def test_db() -> Iterator[DbHandles]:
    try:
        settings = get_settings()
    except ConfigError as exc:
        pytest.skip(f"configuration invalid: {exc}")
    if settings.test_migration_database_url is None or settings.test_database_url is None:
        pytest.skip("TEST_DATABASE_URL / TEST_MIGRATION_DATABASE_URL not set in .env")
    mig = settings.test_migration_database_url.get_secret_value()
    app = settings.test_database_url.get_secret_value()
    app_role, database = db_url_parts(app)
    if database == db_url_parts(settings.database_url.get_secret_value())[1]:
        pytest.fail("TEST_DATABASE_URL must name a different database from DATABASE_URL")

    # Fresh database every session.
    try:
        with psycopg.connect(
            libpq_url(with_database(mig, "postgres")), autocommit=True, connect_timeout=5
        ) as c:
            c.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable (scripts/db-up?): {str(exc).splitlines()[0]}")
    ensure_database_and_role(mig, app_role, settings.auspex_app_db_password.get_secret_value())

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.attributes["url"] = mig
    cfg.attributes["app_role"] = app_role
    command.upgrade(cfg, "head")

    db = DbHandles(mig, app)
    yield db
    db.app.dispose()
    db.admin.dispose()
