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

    # --- Analytics ---------------------------------------------------------
    # Annual risk-free rate used for Sharpe ratios and mean-variance
    # optimisation. 0.04 is 4%.
    risk_free_rate: float = Field(default=0.0, ge=-1.0, le=1.0)
    # Width of one analytics bar, for annualising rates.
    analytics_interval_hours: int = Field(default=1, ge=1)
    # Trailing window analytics commands default to.
    analytics_lookback_days: int = Field(default=90, ge=1)
    # Rolling mean / volatility lookback, in bars.
    rolling_window: int = Field(default=24, ge=2)
    # Per-side trading cost assumptions used by the backtester, in basis
    # points. Uniswap v3 fee tiers run 5-100bps; 30 is the common one.
    backtest_fee_bps: float = Field(default=30.0, ge=0.0)
    backtest_slippage_bps: float = Field(default=5.0, ge=0.0)

    # --- Risk --------------------------------------------------------------
    # Where the risk-free rate comes from. `constant` is RISK_FREE_RATE above;
    # a Tavily-backed source (current bill yield and CPI) is planned for the
    # knowledge layer and plugs in here. See docs/analytics/risk/DESIGN.md.
    risk_free_rate_source: str = "constant"
    # Annual inflation rate, for the *real* risk-free rate reported beside the
    # nominal one. Sharpe ratios keep using the nominal rate by convention.
    inflation_rate: float = Field(default=0.0, gt=-1.0, le=1.0)
    # Value-at-Risk confidence level and horizon (in bars) for `candlegraph
    # risk` and the portfolio report. 0.95 over 24 hourly bars is a one-day
    # 95% VaR.
    risk_confidence: float = Field(default=0.95, gt=0.5, lt=1.0)
    risk_horizon_bars: int = Field(default=24, ge=1)
    # VaR estimator: historical | parametric | cornish_fisher.
    risk_var_method: str = "historical"
    # Benchmark for the systematic/idiosyncratic split; empty disables it.
    risk_benchmark: str | None = "WETH"

    # --- Knowledge: embeddings ----------------------------------------------
    # Which provider turns text into vectors: ollama (local, the default),
    # openai (hosted), or fake (deterministic, for development and tests).
    # One active model per deployment; changing it is a re-embed.
    # See docs/knowledge/embeddings/DESIGN.md.
    embedding_provider: str = "ollama"
    embedding_model: str = "bge-m3"
    # Fixed by the chunk.embedding column type (halfvec(1024)); a migration
    # to change. The provider must produce exactly this many dimensions.
    embedding_dimensions: int = Field(default=1024, ge=1)
    # Allow a Matryoshka model's longer vector to be truncated to the column
    # width. Meaningless for other models, so off by default.
    embedding_truncate: bool = False
    embedding_batch_size: int = Field(default=32, ge=1)
    embedding_timeout_seconds: float = Field(default=120.0, gt=0)
    # The `ollama` compose service (profile `knowledge`).
    ollama_url: str = "http://ollama:11434"
    openai_api_key: SecretStr | None = None

    # --- Knowledge: Tavily and the news budget -------------------------------
    # The only knowledge-layer secret; .env only, never logged (invariant 20).
    tavily_api_key: SecretStr | None = None
    # Credits, not requests. The guard refuses before the provider would;
    # refusals are source_call rows. See docs/knowledge/tavily/DESIGN.md.
    tavily_monthly_credits: int = Field(default=1000, ge=1)
    tavily_daily_credits: int = Field(default=40, ge=1)
    # Held back for an operator's manual search; the pipeline never spends it.
    tavily_reserve_credits: int = Field(default=50, ge=0)
    # basic costs 1 credit, advanced 2; our query is narrow, basic is the default.
    tavily_search_depth: str = "basic"
    tavily_max_results: int = Field(default=8, ge=1, le=20)
    tavily_timeout_seconds: float = Field(default=30.0, gt=0)
    tavily_max_tries: int = Field(default=3, ge=1)
    # How far before the bar a news search looks.
    knowledge_search_window_hours: int = Field(default=24, ge=1)

    # --- Knowledge: retrieval -------------------------------------------------
    # Which engine answers "what was said about this asset before this bar".
    # `postgres` is the hybrid pgvector + tsvector retrieval in knowledge/retrieve.py.
    # `ragflow` is evaluated and not built — docs/knowledge/ragflow/DESIGN.md.
    retrieval_backend: str = "postgres"
    # Fused chunks an explanation gets, and candidates each leg contributes.
    retrieval_top_k: int = Field(default=8, ge=1)
    retrieval_candidates: int = Field(default=40, ge=1)

    # --- Agents ------------------------------------------------------------
    # Where agent reports go: a comma-separated subset of
    # log, ledger, file, webhook, github.
    agent_report_sinks: str = "log,ledger"
    # Directory for the `file` sink, relative to the repo root unless absolute.
    agent_report_dir: Path = Path("reports")
    # Slack-compatible endpoint for the `webhook` sink.
    agent_webhook_url: str | None = None
    # owner/repo for the `github` sink; defaults to the checkout's origin.
    agent_github_repo: str | None = None
    # Optional model for narrating reports. Unset = deterministic template.
    agent_llm_model: str | None = None
    anthropic_api_key: SecretStr | None = None

    # --- Run observer ------------------------------------------------------
    # Seconds between observer runs in `candlegraph agents`; 0 disables.
    observer_interval: int = Field(default=900, ge=0)
    # Poll intervals without a successful ingest before the data is stale.
    observer_stale_intervals: int = Field(default=3, ge=1)
    # Consecutive failed runs that is critical.
    observer_failure_streak: int = Field(default=3, ge=1)
    observer_baseline_days: int = Field(default=7, ge=1)
    observer_drift_window_hours: int = Field(default=24, ge=1)
    # A window with fewer bars than this is "not enough data", never drift.
    observer_min_bars: int = Field(default=12, ge=2)
    observer_volume_drift_ratio: float = Field(default=4.0, gt=1.0)
    observer_volatility_drift_ratio: float = Field(default=3.0, gt=1.0)
    observer_run_drift_ratio: float = Field(default=3.0, gt=1.0)
    # A contiguous gap this long on a *liquid* token is suspicious; on a thin
    # token it is the market (invariant 19).
    observer_suspicious_gap_hours: int = Field(default=6, ge=1)
    # Baseline mean hourly volume_usd above which a token counts as liquid.
    observer_liquid_volume_usd: float = Field(default=100_000.0, ge=0.0)

    # --- CI janitor --------------------------------------------------------
    # Make targets to run, in order. Only targets: the Makefile is the single
    # source of truth for commands.
    janitor_checks: str = "fmt-check,lint,typecheck,test-unit"
    # Push a branch and open a draft PR when a fix changed tracked files.
    janitor_open_pr: bool = False
    janitor_base_branch: str = "main"
    janitor_timeout_seconds: float = Field(default=900.0, gt=0)
    # Tail of command output kept in the report.
    janitor_output_lines: int = Field(default=40, ge=1)
    # Seconds between janitor runs in `candlegraph agents`; 0 disables. The
    # compose container has no git checkout, so the schedule there is 0 and
    # the real schedule is the GitHub Actions cron.
    janitor_interval: int = Field(default=0, ge=0)
    # Checkout the janitor operates on. Defaults to this repository.
    janitor_repo_root: Path | None = None

    # --- UI ----------------------------------------------------------------
    ui_host: str = "0.0.0.0"
    ui_port: int = 8501

    # --- Server ------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    # Whether a *successful* GET /status is written to the access log. The
    # compose health probe is the only steady-state caller, and one line per
    # probe buried every real request. Failures are always logged.
    log_health_probes: bool = False

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

    @property
    def report_sinks(self) -> tuple[str, ...]:
        """The configured agent report sinks, parsed and de-duplicated in order."""
        return _csv(self.agent_report_sinks)

    @property
    def janitor_check_targets(self) -> tuple[str, ...]:
        """The make targets the janitor runs, in order."""
        return _csv(self.janitor_checks)

    @property
    def janitor_root(self) -> Path:
        """The checkout the janitor runs in: ``JANITOR_REPO_ROOT`` or this repo."""
        return self.janitor_repo_root or REPO_ROOT

    @property
    def report_dir(self) -> Path:
        """``agent_report_dir`` resolved against the repo root when relative."""
        if self.agent_report_dir.is_absolute():
            return self.agent_report_dir
        return REPO_ROOT / self.agent_report_dir


def _csv(text: str) -> tuple[str, ...]:
    """Split a comma-separated setting, dropping blanks and repeats."""
    seen: list[str] = []
    for part in text.split(","):
        name = part.strip()
        if name and name not in seen:
            seen.append(name)
    return tuple(seen)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, parsed on first use."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings. Used by tests that patch the environment."""
    get_settings.cache_clear()
