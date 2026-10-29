# candlegraph

A digital asset market data service. candlegraph ingests hourly Uniswap v3 token prices and
metadata from The Graph, stores them in PostgreSQL, and serves OHLC candles through a GraphQL API
for charts and analytics.

It stores hourly OHLC, USD price, volume, TVL and fees per token, plus pool-level data
(per-pair volume, fees, liquidity and transaction counts) for the deepest Uniswap v3 pools
holding each token. Every ingest run is recorded, and a built-in data-quality report measures
coverage and finds gaps.

On top of that sits a deterministic analytics layer: rolling mean and volatility, mean-variance
portfolio optimisation, Bollinger mean reversion, walk-forward ARIMA signals, and a vectorised
backtester — all usable from the CLI or a Streamlit UI.

Watching over it is an agent layer built on LangGraph: a **run observer** that reads the ingest
ledger on a schedule and reports stale data, gaps that matter, and metric drift, and a **CI
janitor** that runs the repository's checks, applies the mechanical fixes, and drafts a pull
request. Both post reports; neither writes market data.

**Stack:** Python 3.12, FastAPI, Strawberry GraphQL, PostgreSQL 17 + TimescaleDB,
SQLAlchemy/Alembic, APScheduler, LangGraph, uv, Docker.

## How it works

The stack is five processes: PostgreSQL, the **API**, a separate **ingest worker**, the
**Streamlit UI**, and the **agents**.

1. On startup the API applies migrations, then serves immediately.
2. The worker discovers the deepest pools for each tracked token, then polls token and pool
   hourly data every `DATA_POLL_INTERVAL` seconds. Each run resumes from the newest hour
   already stored, so restarts re-fetch nothing and writes are idempotent.
   **Stored data is never deleted** — it is the input to the planned analytics phases.
3. Every run is written to the `ingest_run` ledger, so freshness and failures are queryable.
4. The API groups the stored hourly rows into candles of any width at query time,
   with `time_bucket` over hypertables chunked by month.
5. The analytics layer reads that history and produces statistics, allocations and signals.
6. The agents service runs the run observer every `OBSERVER_INTERVAL` seconds. It reads the
   ledger, the quality report, and a trailing window of rows, and posts a report to the
   `agent_run` table and the log (or a webhook, a file, or a GitHub issue).

Ingestion runs in its own process so a long backfill cannot compete with query traffic, and
so an unreachable subgraph degrades the data rather than the service — the API comes up and
stays up regardless.

The tokens tracked are config, not code — see [`config/tokens.yaml`](config/tokens.yaml)
(ten mainnet tokens by default: WETH, WBTC, USDC, USDT, DAI, LINK, UNI, AAVE, SHIB, GNO).

## Quick start

You need Docker and a free Graph API key from <https://thegraph.com/studio/apikeys/>.

```sh
cp .env.example .env
$EDITOR .env            # set GRAPH_API_KEY
make up                 # builds, starts, and waits until healthy
```

Then:

```sh
curl -s localhost:8000/status            # {"status":"ok","database":"up","data_fresh":true,...}
make logs-worker                         # "Ingested 168 hourly rows for WBTC"
```

Backfill history and check what you got:

```sh
make backfill days=90                    # ~5 min for ten tokens
make report                              # coverage, gaps, field completeness
make runs                                # the ingest ledger
```

The UI is at <http://localhost:8501>.

See what the observer makes of it:

```sh
make observe                             # one observer run, report printed
make agent-runs                          # every report so far, newest first
make logs-agents                         # the scheduled runs as they happen
```

GraphiQL is at <http://localhost:8000/graphql>. `make down` stops the stack and drops its volume.

### Developing locally

```sh
make install            # uv sync, including dev tools
make test-unit          # fast, offline: no database, no network
make check              # format, lint, types, and the full suite
make help               # every target
```

To run the service outside Docker against the compose database:

```sh
docker compose up -d postgres
DB_HOST=localhost make migrate
DB_HOST=localhost uv run python -m candlegraph
```

## Configuration

Everything is read through typed settings ([`candlegraph/settings.py`](candlegraph/settings.py));
see [`.env.example`](.env.example) for the full list.

| Variable | Default | Purpose |
|---|---|---|
| `GRAPH_API_KEY` | — | **Required for ingestion.** Gateway API key. |
| `SUBGRAPH_ID` | Uniswap v3 mainnet | Subgraph to query. |
| `TRANSPORT_URL` | unset | Explicit endpoint; overrides the gateway URL. |
| `SUBGRAPH_PAGE_SIZE` / `SUBGRAPH_MAX_PAGES` | 100 / 1000 | Paging bounds. |
| `SUBGRAPH_TIMEOUT_SECONDS` / `SUBGRAPH_MAX_TRIES` | 30 / 5 | Request timeout and retry budget. |
| `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASS`, `DB_NAME`, `DB_SCHEMA` | local compose values | Postgres connection. |
| `DATABASE_URL` | unset | Full URL; overrides the `DB_*` values. |
| `MIN_DB_CONNECTIONS` / `MAX_DB_CONNECTIONS` | 2 / 10 | Pool size. |
| `TOKENS_FILE` | `config/tokens.yaml` | Which tokens to ingest. |
| `LOOKBACK_DAYS` | 7 | How far back a first-time backfill reaches for a token with no stored rows. |
| `INGEST_POOLS` | `true` | `false` skips pool-level data (and so transaction counts). |
| `POOLS_PER_TOKEN` | 3 | Deepest pools to track per token, per side of the pair. 0 freezes the set. |
| `MIN_EXPECTED_COVERAGE` | 0.80 | Coverage below which `candlegraph report` flags a token. |
| `RISK_FREE_RATE` | 0.0 | Annual rate for Sharpe ratios and the tangency portfolio. |
| `ANALYTICS_LOOKBACK_DAYS` | 90 | Default window for the analytics commands. |
| `ROLLING_WINDOW` | 24 | Default rolling lookback, in bars. |
| `BACKTEST_FEE_BPS` / `BACKTEST_SLIPPAGE_BPS` | 30 / 5 | Per-side cost assumptions. |
| `UI_HOST`, `UI_PORT` | `0.0.0.0`, 8501 | Streamlit UI. |
| `DATA_POLL_INTERVAL` | 300 | Seconds between ingest runs. |
| `AGENT_REPORT_SINKS` | `log,ledger` | Where agent reports go: any of `log`, `ledger`, `file`, `webhook`, `github`. |
| `AGENT_WEBHOOK_URL`, `AGENT_REPORT_DIR`, `AGENT_GITHUB_REPO` | unset | Targets for the `webhook`, `file`, and `github` sinks. |
| `AGENT_LLM_MODEL` / `ANTHROPIC_API_KEY` | unset | Optional: a Claude model writes the report's summary paragraph. Findings and status are computed either way. |
| `OBSERVER_INTERVAL` | 900 | Seconds between observer runs in the `agents` service; 0 disables. |
| `OBSERVER_STALE_INTERVALS` | 3 | Poll intervals without a successful ingest before data is stale. Twice that is critical. |
| `OBSERVER_DRIFT_WINDOW_HOURS` / `OBSERVER_BASELINE_DAYS` | 24 / 7 | The recent window and the baseline it is compared with. |
| `OBSERVER_VOLUME_DRIFT_RATIO` / `OBSERVER_VOLATILITY_DRIFT_RATIO` / `OBSERVER_RUN_DRIFT_RATIO` | 4 / 3 / 3 | Recent-to-baseline ratios that count as drift. |
| `OBSERVER_SUSPICIOUS_GAP_HOURS` / `OBSERVER_LIQUID_VOLUME_USD` | 6 / 100000 | A gap this long is a finding only on a token this liquid. |
| `JANITOR_CHECKS` | `fmt-check,lint,typecheck,test-unit` | Make targets the janitor runs. |
| `JANITOR_OPEN_PR` / `JANITOR_BASE_BRANCH` | `false` / `main` | Push a branch and draft a PR when a fix changed files. |
| `JANITOR_INTERVAL` | 0 | Seconds between janitor runs in `candlegraph agents`; the real schedule is a GitHub Actions cron. |
| `INGEST_ENABLED` | `true` | `false` serves the API only. The compose stack sets this `false` for the API and runs the `worker` service instead. |
| `HOST`, `PORT`, `LOG_LEVEL` | `0.0.0.0`, 8000, `INFO` | Server. |

The database credentials in `.env.example` are throwaway defaults for the local Docker Postgres.
Replace them anywhere else. `.env` is gitignored; never commit a real key.

Running two checkouts at once? Compose uses the directory as its project name, so the stacks are
already separate — just set `POSTGRES_HOST_PORT` and `APP_HOST_PORT` so the published ports differ.

## API

The committed schema is [`schema.graphql`](schema.graphql).

```graphql
query {
  getChartData(tokenSymbol: "WBTC", timeUnitInHours: 24) {
    tokenMetadata { id name symbol totalSupply volumeUsd decimals }
    candles { time priceType value }
  }
}
```

`candles` is a list of five series — `open`, `close`, `high`, `low`, `priceUSD` — in that order,
each sorted by time. `tokenSymbol` is case-insensitive. An unknown symbol returns empty metadata
and a single zero-valued point rather than an error.

Two details worth knowing:

- **Candle times are bucket boundaries**, aligned to the Unix epoch — so `timeUnitInHours: 24`
  buckets on midnight UTC regardless of which hours are present.
- **Gaps produce no candle.** An hour with no Uniswap v3 activity has no row, so a bucket with no
  rows is absent from the series rather than zero-filled.

`supportedTokens` returns the symbols this deployment tracks. `GET /status` is the health check
and returns 503 when the database is unreachable.

## Commands

`python -m candlegraph <command>` (or `candlegraph <command>` once installed):

| Command | What it does |
|---|---|
| `serve` | Run the GraphQL API |
| `worker` | Run the ingest loop until stopped |
| `ingest` | One ingest pass, then exit |
| `backfill --days 90` | One pass over a longer window, rewinding past stored history |
| `report [--days N] [--strict]` | Data-quality report; `--strict` exits non-zero when dirty |
| `runs [--limit N]` | Recent rows from the ingest ledger |
| `strategies` | List the registered strategies |
| `stats SYMBOL [--window N]` | Rolling mean and annualised volatility |
| `compare SYMBOL_A SYMBOL_B [--window N]` | Pairwise comparison: correlation, beta, relative return |
| `signals SYMBOL [--strategy X] [--store]` | Generate signals, optionally persisting them |
| `backtest SYMBOL [--strategy X\|all]` | Backtest, net of fees and slippage |
| `portfolio [--objective X] [--risk-free-rate R]` | Mean-variance allocation |
| `forecast SYMBOL [--auto-order]` | ARIMA point forecast and interval |
| `ui` | Run the Streamlit UI |
| `observe [--strict] [--json] [--sinks X]` | Run the run-observer agent once and post its report |
| `janitor [--open-pr] [--strict] [--checks X]` | Run the CI janitor once: checks, mechanical fixes, draft PR |
| `agents` | Run the agents on their schedule until stopped |
| `agent-runs [--limit N] [--agent X] [--show]` | Recent reports from the agent ledger |

## Data quality

`make report` prints coverage per token, the gaps it found, and how populated each enrichment
column is:

```
symbol        rows  coverage   gaps   longest  range
WBTC          2160   100.0%      0         -  2026-07-27 09:00 .. 2026-10-25 08:00
GNO           1026    47.5%    436       22h  2026-07-27 09:00 .. 2026-10-25 07:00
field completeness: volume=100%  volume_usd=100%  tvl_usd=100%  fees_usd=100%
BELOW 80% COVERAGE: GNO 47.5%
```

A gap is **not** automatically a fault. Uniswap v3 emits no `tokenHourData` for an hour with no
swaps, so a thin token is legitimately sparse — GNO above is real, not a bug. That is why the
report measures coverage against `MIN_EXPECTED_COVERAGE` rather than treating any gap as an
error. It separately flags rows that are impossible regardless of market conditions
(`high < low`, OHLC outside the bar).

## Storage

The database is **TimescaleDB**, not plain PostgreSQL — the same image everywhere
(`timescale/timescaledb:2.31.0-pg17`, pinned in compose, in CI, and in the
integration tests). The extension is required: the migrations create hypertables
and fail with an explanatory error without it.

Not every table is a hypertable, and the split is deliberate:

| Chunked on `period_start_unix` | Ordinary tables |
|---|---|
| `token_hours_data`, `pool_hours_data`, `signal` | `token`, `pool`, `ingest_run`, `agent_run` |
| one measurement per instant, read by time window, never deleted | current state with no time coordinate, or small operational ledgers |

`ingest_run` is the interesting exclusion: it *looks* like a series, but a run
is updated by `run_id` when it finishes, and on a time-partitioned table that
update would have to search every chunk.
[`docs/storage/table-placement/DESIGN.md`](docs/storage/table-placement/DESIGN.md)
has the measurements behind each decision and the rule for the next table.

Compression and retention policies are deliberately off — invariant 4 says
stored history is never deleted, and a retention policy is a scheduled `DELETE`.

**Upgrading an existing database:** a data directory created by plain
`postgres:17` has no `shared_preload_libraries=timescaledb` of its own. Compose
now passes that setting on the server's command line, so such a volume comes
up preloaded, and a migration preflight refuses to run against a server that
is not, with a message that says why. The old Debian-based volume still
carries a glibc collation version the Alpine image cannot honour, so the
recommended path is still `make down` (which drops the volume) then `make up`.
If the preflight instead reports the database is **stamped at a revision this
checkout does not have**, read the message: either another worktree shares
your Compose project, or a draft migration was applied here and renamed before
it was committed. The second case can only be rebuilt. The full account is in
[`docs/incidents/2026-10-28-stack-would-not-start/POSTMORTEM.md`](docs/incidents/2026-10-28-stack-would-not-start/POSTMORTEM.md).

## Analytics

All of it is deterministic — the same stored data and the same flags give the same numbers.

```sh
candlegraph stats WETH --window 168           # rolling mean + annualised volatility
candlegraph compare WETH WBTC --window 168    # correlation, beta, relative performance
candlegraph forecast WETH --auto-order        # ARIMA, order picked by AIC
candlegraph signals WETH --strategy bollinger --store
candlegraph backtest WETH --strategy all      # compare every strategy
candlegraph portfolio --risk-free-rate 0.04 --objective max_sharpe --max-weight 0.4
```

**Strategies.** `bollinger` (mean reversion, with separate entry and exit thresholds so a price
hovering at one level does not thrash), `crossover` (moving-average trend following), and
`arima` (walk-forward forecast; the model refits on history up to the current bar only).

**Portfolio.** `max_sharpe`, `min_volatility`, `target_return`, `risk_parity` and `equal_weight`,
with a configurable risk-free rate, weight bounds, shorting, and covariance shrinkage. The
efficient frontier is available from both the CLI and the UI.

**Pairwise comparison** (`candlegraph compare WETH WBTC`) reports Pearson and
Spearman return correlation, beta, total and relative return, volatilities,
Sharpe ratios, tracking error, and a rolling correlation — computed **only
over hours both tokens actually traded**. Nothing is forward-filled or
interpolated first: correlating a real price against a filled one measures
the fill policy, not the market, so a missing hour on either side simply
drops out and the report says how many bars survived.

**Backtests are costed and lagged.** A position decided on bar *t* earns bar *t+1*'s return, and
fees plus slippage are charged on every position change. Both matter: on hourly data a naive
strategy at 35bps per side pays more in costs than it makes in edge, and the reports say so
rather than hiding it.

## UI

```sh
make ui                 # locally, against the stack's database
```

Four tabs: **Prices & volatility** (rolling statistics and Bollinger bands), **Portfolio**
(weights and the efficient frontier), **Strategies** (signals, equity curve, drawdown), and
**Forecast** (ARIMA with its confidence interval). The risk-free rate and cost assumptions live
in the sidebar and feed every tab.

The UI's arithmetic lives in `candlegraph/ui/viewmodels.py`, not in the Streamlit script, so it
is covered by ordinary unit tests; the script itself is smoke-tested with Streamlit's `AppTest`.

## Agents

Two LangGraph graphs, both deterministic, both only readers. Each is a `StateGraph` of plain
functions over a typed state; the design, with flowcharts, is in
[`docs/agents/`](docs/agents/).

**The run observer** (`candlegraph observe`) answers three questions in writing: is the data
fresh, is it complete, and does it still look like the same market?

```
# observer — 2026-10-25 12:15 UTC — WARNING

observer finished WARNING: 6 findings (2 warning, 1 info, 3 ok). WETH: 7h gap
2026-10-24 13:00..2026-10-24 19:00 (7h) on a liquid token (mean hourly volume $4,812,330).
SHIB: median hourly volume x5.2 vs baseline ($18,402 → $95,110)

| severity | kind  | subject | message                                   |
|----------|-------|---------|-------------------------------------------|
| warning  | drift | SHIB    | median hourly volume x5.2 vs baseline ... |
| warning  | gap   | WETH    | 7h gap ... on a liquid token ...          |
| ok       | fresh | ingest  | last successful ingest was 4m ago         |
...
```

It applies the same judgement the data-quality section above asks of a human. Staleness is
measured against `DATA_POLL_INTERVAL`. **A gap is only a finding on a liquid token** — GNO going
quiet for a day is the market; WETH going quiet for six hours is a failed run. Drift is a ratio
of medians between the last 24 hours and the week before, because hourly crypto volume is
heavy-tailed and a z-score would be driven by one big hour. A window with too few bars is "not
enough data", never drift. Drift is a warning, not an alarm: it is news for a human to look at.

**The CI janitor** (`candlegraph janitor`, `make janitor`) runs the Makefile's checks, applies the
only fixes that are mechanical (`make fmt`, for a failing `fmt-check` or `lint`), re-runs the
checks to prove the fix, and — with `--open-pr` — commits the tracked changes on a
`janitor/<stamp>` branch and opens a **draft** PR. It never merges, never "fixes" a type error or
a test, and never touches a working tree that has uncommitted changes. Its schedule is
[`.github/workflows/janitor.yml`](.github/workflows/janitor.yml), daily.

**Reports** go to the sinks in `AGENT_REPORT_SINKS`: the log, the `agent_run` table (read with
`make agent-runs`), a Markdown file, a Slack-compatible webhook, or a GitHub issue (opened only at
warning or worse). A sink failing never fails the run. Set `AGENT_LLM_MODEL` and
`ANTHROPIC_API_KEY` (and `uv sync --extra llm`) to have a Claude model write the summary
paragraph; the findings, the status, and the exit code are computed and are the same either way.

Add a graph by following [`docs/agents/framework/DESIGN.md`](docs/agents/framework/DESIGN.md)
and the `add-agent` skill.

## Tests

| Tier | Needs | Command |
|---|---|---|
| `tests/unit` | nothing — no database, no network | `make test-unit` |
| `tests/integration` | Docker, or `TEST_DATABASE_URL` pointing at a TimescaleDB | `make test-integration` |
| `tests/e2e` | a running stack | `make up && make e2e` |

The integration tier starts a throwaway TimescaleDB with testcontainers, or uses
`TEST_DATABASE_URL` if set (which is how CI does it; it must be a TimescaleDB,
since the migration chain creates hypertables). Tiers that cannot run skip rather than fail,
so check the skip count before reading a green run as proof.

## Status and what comes next

Five pieces of work are delivered and covered by the test suite:

- **Stabilisation** — a clean clone that builds, lints, type-checks and comes up.
- **Data model and ingestion** — hourly token and pool data, idempotent upserts,
  the ingest ledger, the data-quality report.
- **Deterministic analytics** — rolling statistics, mean-variance portfolios,
  three strategies, a costed and lagged backtester, the Streamlit UI.
- **Agent layer** — the run observer and the CI janitor, with their ledger and
  report sinks.
- **Time-series storage** — the series tables as TimescaleDB hypertables.

Next, in order:

0. **Descriptive statistics, deeper** — pairwise comparison between any two
   tokens (delivered: `candlegraph compare`), a five-token active universe
   for the test run (WBTC, WETH, BNB, and the gap-prone GNO and SHIB), and
   a recorded policy for filling ingestion gaps on thin tokens —
   interpolation for display only, a causal estimator for anything a
   strategy consumes, nothing written to the database.
1. **Knowledge graph and signaller** — assets, pools, sources and documents in
   Postgres with pgvector, Tavily for web search, and news attached to prices,
   so a signal can cite *why*. Paper trading only.
2. **Harness** — YAML pipeline specs, an event bus, OpenTelemetry, and the
   continuous aggregates deferred from the TimescaleDB migration.
3. **Traditional markets** — NASDAQ and NYSE equities alongside crypto, which
   means an `instrument` supertype and calendar-aware coverage.
4. **Models and strategies** — momentum, GARCH and friends, with walk-forward
   evaluation as a first-class command.
5. **Market-watch agents** — an RSS feed observer and an alerting agent that
   deduplicates instead of repeating itself.

[`docs/roadmap/DESIGN.md`](docs/roadmap/DESIGN.md) has the reasoning, the
diagram, and each stage's exit criterion.

## Contributing

[`AGENTS.md`](AGENTS.md) has the working agreement: commands, layout, the invariants the test
suite pins down, and the definition of done. Read it before changing anything. The design
notes behind each decision live in [`docs/`](docs/), one directory per topic.
