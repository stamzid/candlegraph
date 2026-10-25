"""Typed application configuration.

Every value is read from the environment (or a ``.env`` file) exactly once and
coerced to a real Python type, so callers never have to guess whether they hold
an ``int`` or the string ``"300"``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Uniswap v3 mainnet on The Graph's decentralised network.
DEFAULT_SUBGRAPH_ID = "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV"
GATEWAY_BASE_URL = "https://gateway.thegraph.com/api/subgraphs/id"

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime configuration for the ingester and the API."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Data source -------------------------------------------------------
    graph_api_key: SecretStr | None = None
    subgraph_id: str = DEFAULT_SUBGRAPH_ID
    # Explicit endpoint; overrides the gateway URL when set.
    transport_url: str | None = None
    subgraph_page_size: int = Field(default=100, ge=1, le=1000)
    subgraph_max_pages: int = Field(default=1000, ge=1)
    subgraph_timeout_seconds: float = Field(default=30.0, gt=0)
    subgraph_max_tries: int = Field(default=5, ge=1)

    # --- Database ----------------------------------------------------------
    db_host: str = "postgres"
    db_port: int = 5432
    db_user: str = "candlegraph"
    db_pass: SecretStr = SecretStr("candlegraph")
    db_name: str = "candlegraph"
    db_schema: str = "candlegraph"
    # Full URL overrides the DB_* values when set.
    database_url: str | None = None
    min_db_connections: int = Field(default=2, ge=1)
    max_db_connections: int = Field(default=10, ge=1)

    # --- Ingestion ---------------------------------------------------------
    tokens_file: Path = Path("config/tokens.yaml")
    # How far back a first-time backfill reaches. Stored history is never
    # deleted, so this only bounds the initial fetch for an unseen token.
    lookback_days: int = Field(default=7, ge=1)
    data_poll_interval: int = Field(default=300, ge=1)
    # False serves the API only. The worker process ignores this.
    ingest_enabled: bool = True
    # Pool-level data is where volume per pair, fees, and tx counts come from;
    # the token-level entity carries no transaction count at all.
    ingest_pools: bool = True
    # Top pools by TVL to track per configured token, counted on each side of
    # the pair. 0 disables discovery and tracks only already-stored pools.
    pools_per_token: int = Field(default=3, ge=0, le=50)

    # --- Data quality ------------------------------------------------------
    # An hour with no Uniswap activity legitimately has no row, so a token is
    # only flagged when coverage falls below this fraction of the window.
    min_expected_coverage: float = Field(default=0.80, ge=0.0, le=1.0)

    # --- Server ------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _check_pool_bounds(self) -> Settings:
        if self.max_db_connections < self.min_db_connections:
            raise ValueError("MAX_DB_CONNECTIONS must be >= MIN_DB_CONNECTIONS")
        return self

    @property
    def subgraph_url(self) -> str:
        """The endpoint the subgraph client posts to."""
        if self.transport_url:
            return self.transport_url
        return f"{GATEWAY_BASE_URL}/{self.subgraph_id}"

    @property
    def subgraph_headers(self) -> dict[str, str]:
        """Auth headers for the gateway; empty when no key is configured."""
        if self.graph_api_key is None:
            return {}
        return {"Authorization": f"Bearer {self.graph_api_key.get_secret_value()}"}

    @property
    def dsn(self) -> str:
        """libpq/SQLAlchemy connection URL."""
        if self.database_url:
            return self.database_url
        password = self.db_pass.get_secret_value()
        return f"postgresql://{self.db_user}:{password}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def sqlalchemy_url(self) -> str:
        """``dsn`` with the driver pinned to psycopg2.

        SQLAlchemy 2.1 resolves a bare ``postgresql://`` to psycopg 3, which is
        not a dependency here, so Alembic must be told which driver to use.
        """
        dsn = self.dsn
        if dsn.startswith("postgresql+"):
            return dsn
        return dsn.replace("postgresql://", "postgresql+psycopg2://", 1)

    @property
    def tokens_path(self) -> Path:
        """``tokens_file`` resolved against the repo root when it is relative."""
        if self.tokens_file.is_absolute():
            return self.tokens_file
        return REPO_ROOT / self.tokens_file


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, parsed on first use."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings. Used by tests that patch the environment."""
    get_settings.cache_clear()
