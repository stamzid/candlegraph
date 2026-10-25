"""Alembic environment.

The connection URL comes from application settings, never from ``alembic.ini``,
so migrations run against whatever database the process is configured for —
including the throwaway one the integration tests spin up.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from candlegraph.settings import get_settings

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
