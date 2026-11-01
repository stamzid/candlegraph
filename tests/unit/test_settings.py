"""Settings are typed, not stringly typed, and the gateway URL is built correctly."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from candlegraph.settings import (
    DEFAULT_SUBGRAPH_ID,
    REPO_ROOT,
    Settings,
    get_settings,
    reset_settings_cache,
)
from tests.conftest import make_settings


def test_defaults_are_the_documented_ones() -> None:
    settings = Settings(_env_file=None)

    assert settings.subgraph_id == DEFAULT_SUBGRAPH_ID
    assert settings.lookback_days == 7
    assert settings.data_poll_interval == 300
    assert settings.ingest_enabled is True
    assert settings.graph_api_key is None
    assert not hasattr(settings, "retain_history"), "history pruning was removed"


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [("1", 1), ("30", 30), ("365", 365)],
)
def test_numeric_env_values_are_coerced_to_int(
    monkeypatch: pytest.MonkeyPatch, env_value: str, expected: int
) -> None:
    """The old settings module handed callers the raw string, breaking timedelta."""
    monkeypatch.setenv("LOOKBACK_DAYS", env_value)
    monkeypatch.setenv("DATA_POLL_INTERVAL", env_value)

    settings = Settings(_env_file=None)

    assert settings.lookback_days == expected
    assert settings.data_poll_interval == expected
    assert isinstance(settings.lookback_days, int)
    assert isinstance(settings.data_poll_interval, int)


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [("true", True), ("false", False), ("1", True), ("0", False), ("yes", True), ("no", False)],
)
def test_boolean_env_values_are_coerced(
    monkeypatch: pytest.MonkeyPatch, env_value: str, expected: bool
) -> None:
    monkeypatch.setenv("INGEST_ENABLED", env_value)

    settings = Settings(_env_file=None)

    assert settings.ingest_enabled is expected


def test_env_names_are_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("lookback_days", "3")
    assert Settings(_env_file=None).lookback_days == 3


@pytest.mark.parametrize("field", ["lookback_days", "data_poll_interval", "subgraph_max_tries"])
def test_out_of_range_values_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: 0})


def test_pool_bounds_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="MAX_DB_CONNECTIONS"):
        Settings(_env_file=None, min_db_connections=10, max_db_connections=2)


def test_subgraph_url_uses_the_gateway_with_the_subgraph_id() -> None:
    settings = make_settings(subgraph_id="abc123")

    assert settings.subgraph_url == "https://gateway.thegraph.com/api/subgraphs/id/abc123"


def test_transport_url_overrides_the_gateway() -> None:
    settings = make_settings(transport_url="http://localhost:9000/graphql")

    assert settings.subgraph_url == "http://localhost:9000/graphql"


def test_api_key_becomes_a_bearer_header() -> None:
    assert make_settings(graph_api_key="s3cret").subgraph_headers == {"Authorization": "Bearer s3cret"}


def test_no_api_key_means_no_auth_header() -> None:
    assert make_settings(graph_api_key=None).subgraph_headers == {}


def test_api_key_is_not_leaked_by_repr() -> None:
    """A SecretStr keeps the key out of logs and crash dumps."""
    settings = make_settings(graph_api_key="super-secret-key")

    assert "super-secret-key" not in repr(settings)
    assert settings.graph_api_key is not None
    assert settings.graph_api_key.get_secret_value() == "super-secret-key"


def test_dsn_is_assembled_from_the_db_parts() -> None:
    settings = make_settings(db_host="db.example", db_port=6543, db_user="u", db_pass="p", db_name="candles")

    assert settings.dsn == "postgresql://u:p@db.example:6543/candles"


def test_database_url_overrides_the_db_parts() -> None:
    settings = make_settings(database_url="postgresql://x:y@other:5432/z", db_host="ignored")

    assert settings.dsn == "postgresql://x:y@other:5432/z"


def test_relative_tokens_file_resolves_against_the_repo_root() -> None:
    settings = make_settings(tokens_file="config/tokens.yaml")

    assert settings.tokens_path == REPO_ROOT / "config" / "tokens.yaml"
    assert settings.tokens_path.is_file()


def test_absolute_tokens_file_is_left_alone(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.yaml"
    assert make_settings(tokens_file=str(target)).tokens_path == target


def test_unknown_env_vars_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose and CI inject plenty of unrelated variables."""
    monkeypatch.setenv("SOME_UNRELATED_THING", "x")
    assert Settings(_env_file=None).lookback_days == 7


def test_get_settings_is_cached_until_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOKBACK_DAYS", "11")
    reset_settings_cache()
    assert get_settings().lookback_days == 11
    assert get_settings() is get_settings()

    monkeypatch.setenv("LOOKBACK_DAYS", "12")
    assert get_settings().lookback_days == 11

    reset_settings_cache()
    assert get_settings().lookback_days == 12


def test_risk_defaults_are_the_documented_ones() -> None:
    settings = Settings(_env_file=None)

    assert settings.risk_free_rate_source == "constant"
    assert settings.inflation_rate == 0.0
    assert settings.risk_confidence == 0.95
    assert settings.risk_horizon_bars == 24
    assert settings.risk_var_method == "historical"
    assert settings.risk_benchmark == "WETH"


@pytest.mark.parametrize(
    ("field", "value"), [("risk_confidence", 0.5), ("risk_confidence", 1.0), ("risk_horizon_bars", 0)]
)
def test_out_of_range_risk_values_are_rejected(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_embedding_defaults_follow_the_design_decision() -> None:
    """docs/knowledge/embeddings/DESIGN.md: local bge-m3 via Ollama, 1024 fixed."""
    settings = Settings(_env_file=None)

    assert settings.embedding_provider == "ollama"
    assert settings.embedding_model == "bge-m3"
    assert settings.embedding_dimensions == 1024
    assert settings.embedding_truncate is False
    assert settings.ollama_url == "http://ollama:11434"
    assert settings.openai_api_key is None


def test_tavily_defaults_follow_the_design_document() -> None:
    """docs/knowledge/tavily/DESIGN.md: scarce credits, smoothed by a daily cap."""
    settings = Settings(_env_file=None)

    assert settings.tavily_api_key is None
    assert settings.tavily_monthly_credits == 1000
    assert settings.tavily_daily_credits == 40
    assert settings.tavily_reserve_credits == 50
    assert settings.tavily_search_depth == "basic"
    assert settings.tavily_max_results == 8
    assert settings.tavily_max_tries == 3
    assert settings.knowledge_search_window_hours == 24


def test_retrieval_defaults_name_the_built_backend() -> None:
    settings = Settings(_env_file=None)

    assert settings.retrieval_backend == "postgres"
    assert settings.retrieval_top_k == 8
    assert settings.retrieval_candidates == 40
