"""Alembic environment.

The connection URL comes from application settings, never from ``alembic.ini``,
so migrations run against whatever database the process is configured for —
including the throwaway one the integration tests spin up.
"""

from logging.config import fileConfig

from alembic import context
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import engine_from_config, pool, text
from sqlalchemy.engine import Connection

from candlegraph.settings import get_settings
from candlegraph.storage.preflight import (
    explain_missing_preload,
    explain_unknown_revision,
    timescaledb_is_preloaded,
    unknown_revisions,
)

config = context.config

if config.config_file_name is not None:
    # Keep existing loggers alive: migrations also run in-process from the
    # integration tests, which would otherwise lose their log capture.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.sqlalchemy_url.replace("%", "%%"))

SCHEMA = settings.db_schema

# No SQLAlchemy models yet; tables are defined in the revision scripts.
target_metadata = None


def run_migrations_offline() -> None:
    """Emit SQL for the configured URL without connecting."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema=SCHEMA,
        include_schemas=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def preflight(connection: Connection) -> None:
    """Refuse to migrate a database this checkout cannot migrate, and say why.

    Both checks exist because of an outage where neither condition produced a
    message naming its cause: ``CREATE EXTENSION timescaledb`` on a server
    that does not preload it is a backend ``FATAL`` that drops the connection,
    and a version table stamped with a revision this tree lacks surfaces only
    as Alembic's ``Can't locate revision``. The rules are pure functions in
    ``candlegraph.storage.preflight``; this only reads the inputs.
    """
    setting = str(connection.execute(text("SHOW shared_preload_libraries")).scalar() or "")
    if not timescaledb_is_preloaded(setting):
        raise RuntimeError(explain_missing_preload(setting))

    known = {script.revision for script in ScriptDirectory.from_config(config).walk_revisions()}
    current = MigrationContext.configure(
        connection, opts={"version_table_schema": SCHEMA}
    ).get_current_heads()
    if unknown := unknown_revisions(current, known):
        raise RuntimeError(explain_unknown_revision(unknown, known))


def run_migrations_online() -> None:
    """Connect and run migrations, creating the schema first if needed."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # The app owns its schema: do not depend on an initdb script having run.
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
        connection.commit()

        # Preflight only reads, but its statements open a transaction on this
        # connection, and Alembic leaves an already-open transaction to its
        # owner. Close it so begin_transaction() below owns, and commits, its own.
        preflight(connection)
        connection.rollback()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=SCHEMA,
            include_schemas=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
