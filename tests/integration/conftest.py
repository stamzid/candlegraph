"""A throwaway PostgreSQL for the integration tier.

Two ways to get one, in order of preference:

* ``TEST_DATABASE_URL`` points at a disposable database (how CI does it, using
  a service container).
* Docker is available, and testcontainers starts one for the session.

If neither is possible the tier skips rather than fails, so ``pytest`` on a
machine without Docker still runs the 160-odd offline tests.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from candlegraph.settings import reset_settings_cache
from candlegraph.storage import DatabaseManager, set_db

SCHEMA = "candlegraph"


def _libpq_url(url: str) -> str:
    """Strip the SQLAlchemy driver suffix; psycopg2 wants a plain libpq URL."""
    return url.replace("postgresql+psycopg2://", "postgresql://")


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """A disposable database for the whole session."""
    if existing := os.environ.get("TEST_DATABASE_URL"):
        yield _libpq_url(existing)
        return

    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # pragma: no cover - dev dependency is always present
        pytest.skip("testcontainers is not installed and TEST_DATABASE_URL is unset")

    try:
        container = PostgresContainer("postgres:17", driver=None)
        container.start()
    except Exception as exc:  # pragma: no cover - depends on the host
        pytest.skip(f"no Docker for a throwaway Postgres and TEST_DATABASE_URL is unset: {exc}")

    try:
        yield _libpq_url(container.get_connection_url())
    finally:
        container.stop()


@contextmanager
def _env(**values: str) -> Iterator[None]:
    """Temporarily set environment variables, restoring the previous values."""
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    reset_settings_cache()
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_settings_cache()


@pytest.fixture(scope="session")
def migrated_dsn(postgres_dsn: str) -> str:
    """The same database with ``alembic upgrade head`` applied once.

    ``alembic/env.py`` reads the URL from settings, so the environment is set
    for the duration of the upgrade only. Leaving it set would leak into every
    later test in the session, since this fixture tears down last.
    """
    from alembic import command
    from alembic.config import Config

    with _env(DATABASE_URL=postgres_dsn, DB_SCHEMA=SCHEMA):
        command.upgrade(Config("alembic.ini"), "head")
    return postgres_dsn


@pytest.fixture
def db(migrated_dsn: str) -> Iterator[DatabaseManager]:
    """An empty, migrated database wired in as the process-wide manager."""
    manager = DatabaseManager(dsn=migrated_dsn, schema=SCHEMA, min_connections=1, max_connections=4)
    manager.execute(f'TRUNCATE "{SCHEMA}".token_hours_data, "{SCHEMA}".token')
    set_db(manager)
    try:
        yield manager
    finally:
        set_db(None)
        manager.close()
