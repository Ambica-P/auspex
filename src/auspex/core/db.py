"""Database access: engine factory and the idempotent role/database bootstrap.

Roles:
- the migration role (MIGRATION_DATABASE_URL): superuser of the container; creates the
  database and the app role, runs Alembic, owns the tables;
- the app role (DATABASE_URL): what every Auspex process uses. It may INSERT and SELECT
  the ledger but never UPDATE or DELETE it (grants in migration 0001, plus a trigger).

`python -m auspex.core.db bootstrap` works on a fresh volume and on an existing one, so the
password in .env is always applied to the app role.
"""

from __future__ import annotations

import sys
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
from sqlalchemy import Engine, create_engine

from auspex.core.config import db_url_parts, get_settings
from auspex.core.errors import ConfigError, DatabaseError
from auspex.core.logging import configure_logging, get_logger

log = get_logger(__name__)

CONNECT_TIMEOUT_S = 5


def libpq_url(url: str) -> str:
    """SQLAlchemy URL (postgresql+psycopg://) → libpq URL (postgresql://)."""
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def with_database(url: str, database: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))


def make_engine(url: str) -> Engine:
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_S},
    )


def ensure_database_and_role(migration_url: str, app_role: str, app_password: str) -> None:
    """Create the target database if missing; create or update the app role. Idempotent."""
    _, database = db_url_parts(migration_url)
    admin_url = libpq_url(with_database(migration_url, "postgres"))
    try:
        with psycopg.connect(admin_url, autocommit=True, connect_timeout=CONNECT_TIMEOUT_S) as c:
            exists = c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,))
            if exists.fetchone() is None:
                c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
                log.info("db.database_created", database=database)
            role = c.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (app_role,))
            verb = "ALTER" if role.fetchone() is not None else "CREATE"
            c.execute(
                sql.SQL("{} ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                    sql.SQL(verb), sql.Identifier(app_role), sql.Literal(app_password)
                )
            )
            c.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database), sql.Identifier(app_role)
                )
            )
            log.info("db.app_role_ready", role=app_role, action=verb.lower(), database=database)
    except psycopg.OperationalError as exc:
        raise DatabaseError(
            "cannot connect with MIGRATION_DATABASE_URL; is Postgres up, and does "
            "POSTGRES_PASSWORD match the existing volume?",
            database=database,
            detail=str(exc).splitlines()[0] if str(exc) else "",
        ) from None


def main(argv: list[str]) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    target = argv[1] if len(argv) > 1 else "main"
    if argv[:1] != ["bootstrap"] or target not in ("main", "test"):
        print("usage: python -m auspex.core.db bootstrap [main|test]", file=sys.stderr)
        return 2
    if target == "main":
        mig, app = settings.migration_database_url, settings.database_url
        password = settings.auspex_app_db_password
    else:
        if settings.test_migration_database_url is None or settings.test_database_url is None:
            raise ConfigError("TEST_DATABASE_URL and TEST_MIGRATION_DATABASE_URL are not set")
        mig, app = settings.test_migration_database_url, settings.test_database_url
        password = settings.auspex_app_db_password
    app_role, _ = db_url_parts(app.get_secret_value())
    ensure_database_and_role(mig.get_secret_value(), app_role, password.get_secret_value())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (ConfigError, DatabaseError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        sys.exit(1)
