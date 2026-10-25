"""Application wiring: health check, router mounting, and scheduler lifecycle.

The regression these guard against: the old entrypoint ran the initial backfill
synchronously before ``uvicorn.run``, so an unreachable subgraph meant the HTTP
server never started at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from candlegraph.app import INGEST_JOB_ID, build_scheduler, create_app, ingest_job
from candlegraph.dba import set_db
from candlegraph.settings import Settings
from tests.conftest import FakeDatabase, make_settings


class _NullScheduler:
    """Stands in for APScheduler so no ingest thread runs during a test."""

    def start(self) -> None:
        pass

    def shutdown(self, wait: bool = True) -> None:
        pass


@pytest.fixture
def db() -> FakeDatabase:
    database = FakeDatabase()
    set_db(database)
    return database


@pytest.fixture
def app_settings(tokens_file: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    return make_settings(tokens_file=str(tokens_file), ingest_enabled=False)


def client_for(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        yield client


# --- health check ----------------------------------------------------------


def test_status_is_ok_when_the_database_answers(app_settings: Settings, db: FakeDatabase) -> None:
    for client in client_for(app_settings):
        response = client.get("/status")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": "up",
        "ingest": "disabled",
        "tokens": ["WBTC", "SHIB"],
    }


def test_status_is_503_when_the_database_is_unreachable(app_settings: Settings, db: FakeDatabase) -> None:
    """A health check that passes while the database is down is worse than none."""
    db.ping = lambda: False  # type: ignore[method-assign]

    for client in client_for(app_settings):
        response = client.get("/status")

    assert response.status_code == 503
    assert response.json()["database"] == "down"


def test_status_reports_ingestion_as_enabled(
    tokens_file: Path, db: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("candlegraph.app.build_scheduler", lambda _s: _NullScheduler())
    settings = make_settings(tokens_file=str(tokens_file), ingest_enabled=True)

    with TestClient(create_app(settings)) as client:
        assert client.get("/status").json()["ingest"] == "enabled"


# --- routing ---------------------------------------------------------------


def test_graphql_is_mounted(app_settings: Settings, db: FakeDatabase) -> None:
    for client in client_for(app_settings):
        response = client.post("/graphql", json={"query": "{ supportedTokens }"})

    assert response.status_code == 200
    assert response.json() == {"data": {"supportedTokens": ["WBTC", "SHIB"]}}


def test_an_unknown_path_is_a_404(app_settings: Settings, db: FakeDatabase) -> None:
    for client in client_for(app_settings):
        response = client.get("/nope")

    assert response.status_code == 404


# --- scheduler -------------------------------------------------------------


def test_the_ingest_job_fires_immediately_then_on_the_interval() -> None:
    """The first backfill is the job's first run, not a blocking startup step."""
    scheduler = build_scheduler(make_settings(data_poll_interval=42))
    job = scheduler.get_job(INGEST_JOB_ID)

    assert job is not None
    assert job.trigger.interval.total_seconds() == 42
    assert job.next_run_time is not None
    assert job.max_instances == 1
    assert job.coalesce is True


def test_no_scheduler_runs_when_ingestion_is_disabled(
    app_settings: Settings, db: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[Any] = []
    monkeypatch.setattr("candlegraph.app.build_scheduler", lambda s: started.append(s))

    for client in client_for(app_settings):
        assert client.get("/status").status_code == 200

    assert started == []


def test_the_scheduler_starts_and_shuts_down_with_the_app(
    tokens_file: Path, db: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class SpyScheduler:
        def start(self) -> None:
            events.append("start")

        def shutdown(self, wait: bool = True) -> None:
            events.append(f"shutdown(wait={wait})")

    monkeypatch.setattr("candlegraph.app.build_scheduler", lambda _s: SpyScheduler())
    settings = make_settings(tokens_file=str(tokens_file), ingest_enabled=True)

    with TestClient(create_app(settings)) as client:
        client.get("/status")

    assert events == ["start", "shutdown(wait=False)"]
    assert db.closed is True


def test_the_api_serves_even_when_every_ingest_run_fails(
    tokens_file: Path, db: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))

    def always_fails(*_args: Any, **_kwargs: Any) -> None:
        raise ConnectionError("subgraph is gone")

    monkeypatch.setattr("candlegraph.app.run_ingest", always_fails)
    settings = make_settings(tokens_file=str(tokens_file), ingest_enabled=True, data_poll_interval=3600)

    with TestClient(create_app(settings)) as client:
        assert client.get("/status").status_code == 200
        assert client.post("/graphql", json={"query": "{ supportedTokens }"}).status_code == 200


# --- the job itself --------------------------------------------------------


def test_the_ingest_job_delegates_to_run_ingest(
    tokens_file: Path, db: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr("candlegraph.app.run_ingest", lambda *args: calls.append(args))
    settings = make_settings(tokens_file=str(tokens_file))

    ingest_job(settings)

    assert len(calls) == 1
    database, registry, passed_settings = calls[0]
    assert database is db
    assert registry.symbols == ("WBTC", "SHIB")
    assert passed_settings is settings


def test_the_ingest_job_never_raises(
    tokens_file: Path, db: FakeDatabase, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An exception escaping the job would stop APScheduler rescheduling it."""
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))

    def boom(*_args: Any) -> None:
        raise RuntimeError("subgraph down")

    monkeypatch.setattr("candlegraph.app.run_ingest", boom)

    with caplog.at_level("ERROR", logger="candlegraph"):
        ingest_job(make_settings(tokens_file=str(tokens_file)))

    assert "Ingest run failed" in caplog.text


def test_the_ingest_job_survives_a_broken_tokens_file(
    tmp_path: Path, db: FakeDatabase, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("tokens: []\n", encoding="utf-8")
    monkeypatch.setenv("TOKENS_FILE", str(broken))

    with caplog.at_level("ERROR", logger="candlegraph"):
        ingest_job(make_settings(tokens_file=str(broken)))

    assert "Ingest run failed" in caplog.text
