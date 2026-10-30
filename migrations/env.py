"""Alembic environment. Runs as the migration role; the app role name is passed to migrations.

Target database:
- default: MIGRATION_DATABASE_URL, app role from DATABASE_URL;
- `alembic -x target=test upgrade head`: TEST_MIGRATION_DATABASE_URL / TEST_DATABASE_URL.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from auspex.core.config import db_url_parts, get_settings
from auspex.core.errors import ConfigError

config = context.config


def _target() -> tuple[str, str]:
    url = config.attributes.get("url")
    role = config.attributes.get("app_role")
    if url and role:
        return str(url), str(role)
    settings = get_settings()
    target = context.get_x_argument(as_dictionary=True).get("target", "main")
    if target == "test":
        if settings.test_migration_database_url is None or settings.test_database_url is None:
            raise ConfigError("TEST_DATABASE_URL and TEST_MIGRATION_DATABASE_URL are not set")
        mig, app = settings.test_migration_database_url, settings.test_database_url
    else:
        mig, app = settings.migration_database_url, settings.database_url
    return mig.get_secret_value(), db_url_parts(app.get_secret_value())[0]


def run_migrations_online() -> None:
    url, app_role = _target()
    engine = create_engine(url, poolclass=pool.NullPool, connect_args={"connect_timeout": 5})
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=None,
            transaction_per_migration=True,
        )
        context.config.attributes["app_role"] = app_role
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise ConfigError("offline migrations are not supported")
run_migrations_online()
