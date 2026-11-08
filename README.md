# candlegraph

candlegraph turns raw on-chain trading activity into something you can reason about: it ingests
hourly Uniswap v3 prices from The Graph into TimescaleDB, serves them as OHLC candles over GraphQL,
and layers deterministic analytics — rolling statistics, mean-variance portfolios, trading signals,
costed backtests and tail-risk measures — on top of that stored history. LangGraph agents watch the
pipeline and report on it, a knowledge layer lets a signal cite the news that was already public
when it fired, and the whole surface is available to an assistant as MCP tools.

In more detail: a separate ingest worker discovers the deepest pools for each tracked token and
stores hourly OHLC, USD price, volume, TVL and fees per token, plus pool-level volume, fees,
liquidity and transaction counts, recording every run in a ledger and measuring coverage with a
built-in data-quality report. The analytics layer reads that history and never the network:
rolling mean and volatility, Bollinger mean reversion, moving-average crossover and walk-forward
ARIMA signals, Markowitz portfolios (tangency, minimum variance, risk parity) with an efficient
frontier, pairwise correlation and beta over aligned bars, a vectorised backtester that lags
execution by one bar and charges fees on every trade, and a risk report with Value at Risk,
expected shortfall and a systematic-versus-idiosyncratic split against a benchmark. The maths
behind each of those is written up, with equations, in
[`docs/analytics/models/`](docs/analytics/models/DESIGN.md) and
[`docs/analytics/risk/`](docs/analytics/risk/DESIGN.md). A **run observer** agent reads the
ledger on a schedule and reports stale data, gaps that matter and metric drift; a **CI janitor**
runs the repository's checks, applies the mechanical fixes and drafts a pull request. Both post
reports; neither writes market data. Everything is reachable from one CLI, a Streamlit UI, and the
GraphQL API, and the whole stack runs locally under Docker with no paid service required.

**Stack:** Python 3.12, FastAPI, Strawberry GraphQL, PostgreSQL 17 + TimescaleDB + pgvector,
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
| `RISK_FREE_RATE` | 0.0 | Annual rate as a decimal fraction (0.04 = 4%) for Sharpe ratios and the tangency portfolio. The UI takes percent and converts. |
| `ANALYTICS_LOOKBACK_DAYS` | 90 | Default window for the analytics commands. |
| `ROLLING_WINDOW` | 24 | Default rolling lookback, in bars. |
| `BACKTEST_FEE_BPS` / `BACKTEST_SLIPPAGE_BPS` | 30 / 5 | Per-side cost assumptions. |
| `RISK_CONFIDENCE` / `RISK_HORIZON_BARS` / `RISK_VAR_METHOD` | 0.95 / 24 / `historical` | Value-at-Risk confidence, horizon in bars, and estimator (`historical`, `parametric`, `cornish_fisher`). |
| `RISK_BENCHMARK` | `WETH` | Market proxy for the systematic/idiosyncratic split; empty disables it. |
| `EMBEDDING_PROVIDER` / `EMBEDDING_MODEL` | `ollama` / `bge-m3` | Local embeddings by default; `openai` (`text-embedding-3-large`, needs `OPENAI_API_KEY`) or `fake` for development. One active model; changing it is `make reembed`. |
| `OLLAMA_URL` | `http://ollama:11434` | The `ollama` compose service, part of `make up` (the first start pulls the model once). |
| `TAVILY_API_KEY` | unset | **Required for `fetch-news`.** Free tier ~1,000 credits/month; without it the news pipeline refuses and everything else still works. |
| `TAVILY_MONTHLY_CREDITS` / `TAVILY_DAILY_CREDITS` / `TAVILY_RESERVE_CREDITS` | 1000 / 40 / 50 | The budget guard's caps: the plan, a daily smoothing cap, and a reserve kept for manual searches. |
| `RETRIEVAL_BACKEND` / `RETRIEVAL_TOP_K` / `RETRIEVAL_CANDIDATES` | `postgres` / 8 / 40 | The hybrid pgvector + `tsvector` retrieval; `ragflow` is refused with a pointer to its evaluation. |
| `KNOWLEDGE_LLM_MODEL` | unset | Optional Claude model that writes the `--explain` rationale over the numbers and citations; unset is the deterministic template. Falls back to `AGENT_LLM_MODEL`. |
| `KNOWLEDGE_REFERENCE_SYMBOL` | `WBTC` | The tracked token an explained move is compared against, so the rationale can call it idiosyncratic or market-wide. |
| `RISK_FREE_RATE_SOURCE` / `INFLATION_RATE` | `constant` / 0.0 | Where the risk-free rate comes from (a Tavily source is planned), and the inflation rate for the real rate printed beside it. |
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

Signal explanations follow one rule: **reads are free, the mutation spends only when asked.**
`signalExplanation` and `signalExplanations` read the `explanation` table (no network, no model);
`explainSignal` runs the strategy, explains a bar and stores it, and calls the news source only
with `search: true` (at most one Tavily credit per token per UTC day, whoever asks).

```graphql
query {
  signalExplanation(tokenSymbol: "WETH", strategy: "bollinger") {
    bar positionLabel score rationale newsStatus
    citations { n url publishedAt legs }
  }
}

mutation {
  explainSignal(tokenSymbol: "WETH", strategy: "bollinger", search: false) {
    rationale rejectedCitations citations { url publishedAt }
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
and returns 503 when the database is unreachable. It costs one database round trip, and the
compose stack probes it every three seconds while starting and every three minutes afterwards.
A successful probe is not written to the access log (`LOG_HEALTH_PROBES=true` to change that);
a failing one always is.

## Commands

`python -m candlegraph <command>` (or `candlegraph <command>` once installed):

| Command | What it does |
|---|---|
| `serve` | Run the GraphQL API |
| `worker` | Run the ingest loop until stopped |
| `ingest` | One ingest pass, then exit |
| `backfill --days 90` | One pass over a longer window, rewinding past stored history |
| `report [--days N] [--strict] [--json]` | Data-quality report; `--strict` exits non-zero when dirty, `--json` prints it as data |
| `runs [--limit N]` | Recent rows from the ingest ledger |
| `strategies` | List the registered strategies |
| `stats SYMBOL [--window N]` | Rolling mean and annualised volatility |
| `compare SYMBOL_A SYMBOL_B [--window N]` | Pairwise comparison: correlation, beta, relative return |
| `signals SYMBOL [--strategy X] [--store] [--explain [--at ISO] [--reference S] [--no-search]]` | Generate signals, optionally persisting them; `--explain` prints one bar's numeric context and a rationale citing news published before it (URL and timestamp per citation), stored in `explanation`. Works with no LLM key and no Tavily key |
| `backtest SYMBOL [--strategy X\|all]` | Backtest, net of fees and slippage |
| `portfolio [--objective X] [--risk-free-rate R]` | Mean-variance allocation |
| `forecast SYMBOL [--auto-order]` | ARIMA point forecast and interval |
| `risk SYMBOL [--benchmark X] [--confidence C] [--horizon N] [--method M]` | VaR, CVaR, drawdown, and the systematic/idiosyncratic split |
| `fill [SYMBOLS…] [--method M] [--all-methods] [--days N] [--max-fill N] [--clear] [--status] [--evaluate]` | Recompute the gap-estimate cache for hours with no stored row, score the estimators against held-out real hours, or report what is cached. Writes `token_hours_fill` only |
| `ui` | Run the Streamlit UI |
| `observe [--strict] [--json] [--sinks X]` | Run the run-observer agent once and post its report |
| `janitor [--open-pr] [--strict] [--checks X]` | Run the CI janitor once: checks, mechanical fixes, draft PR |
| `agents` | Run the agents on their schedule until stopped |
| `agent-runs [--limit N] [--agent X] [--show]` | Recent reports from the agent ledger |
| `knowledge sync-assets` | Load `asset` rows (addresses, symbols, aliases) from `config/tokens.yaml` |
| `knowledge embed` / `knowledge reembed [--batch-size N] [--limit N]` | Embed chunks without a vector (or from another model) with the configured `EMBEDDING_MODEL`; resumable |
| `knowledge fetch-news [--symbol S]` | One budgeted Tavily news search per token per UTC day; cached days cost nothing, refusals are ledgered |
| `knowledge search QUERY [--symbol S] [--at ISO] [--mode display\|strategy] [--lexical-only]` | Hybrid retrieval over stored news as of a bar: dense + lexical legs fused by reciprocal rank, nothing published after the bar |
| `knowledge link-mentions` | Link stored chunks to the assets their aliases name (idempotent backfill) |
| `mcp [--transport stdio\|http]` | Serve the read-only tools over the Model Context Protocol (needs `uv sync --extra mcp`) |

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
(`timescale/timescaledb-ha:pg17.11-ts2.31.0`, pinned in compose, in CI, and in the
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

**Upgrading an existing database:** the image moved twice in October 2026 —
`postgres:17` → `timescale/timescaledb` (Alpine) → `timescale/timescaledb-ha`
(Ubuntu, with pgvector). A volume from either earlier image is **rebuilt, not
carried**: the data directory path differs (`/home/postgres/pgdata/data` on
`-ha`), the C library and therefore the collation version differ, and a
volume initialised under plain `postgres:17` has no TimescaleDB preload of
its own. Compose forces the preload on the server's command line and a
migration preflight refuses a server that is not preloading it, with a
message that says why; the remedy is `make reset` (drop the volume and this
project's image, start again).
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
candlegraph risk WETH --benchmark WBTC --confidence 0.99   # VaR, CVaR, systematic vs idiosyncratic
```

The theory behind every number here — returns and annualisation, rolling statistics, Bollinger,
ARIMA, correlation and beta, Sharpe and drawdown, Markowitz portfolios and the backtester — is
written out with equations in [`docs/analytics/models/DESIGN.md`](docs/analytics/models/DESIGN.md);
the risk measures and how to tune them are in
[`docs/analytics/risk/DESIGN.md`](docs/analytics/risk/DESIGN.md).

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

**Risk** (`candlegraph risk WETH`) reports annualised and downside volatility, maximum
drawdown, Value at Risk and expected shortfall (historical, parametric, or Cornish–Fisher, at a
configurable confidence and horizon), Sharpe, Sortino and Calmar, skew and kurtosis, and — against
a benchmark, WETH by default — beta, alpha, R² and the split of the token's variance into the
systematic part diversification cannot remove and the idiosyncratic part it can. The portfolio
report prints the VaR and CVaR of the solved allocation. The risk-free rate comes through one
seam (`RISK_FREE_RATE_SOURCE`), constant today and ready for a Tavily-backed source that pulls
live bill yields and inflation; the real rate is printed beside the nominal one.

**Backtests are costed and lagged.** A position decided on bar *t* earns bar *t+1*'s return, and
fees plus slippage are charged on every position change. Both matter: on hourly data a naive
strategy at 35bps per side pays more in costs than it makes in edge, and the reports say so
rather than hiding it.

## Gaps, and what may be done about them

Uniswap emits no row for an hour with no swaps, so a thin token's history has holes in it. What
may be drawn in their place depends entirely on **who is asking**, and that rule is code rather
than convention: every fill policy declares whether it is *causal*.

| Policy | Uses | Error bar | Causal | Who may consume it |
|---|---|---|---|---|
| `none` | nothing — the hole stays | — | yes | anyone; the honest default for aligned-bar statistics |
| `drop` | only hours every symbol has | — | yes | anyone |
| `ffill` | the last price before the gap | no | yes | anyone, including strategies |
| `interpolate` (`linear`) | a line between the bars **either side** | no | **no** | display only |
| `causal` | Kalman local-level forecast from before the gap | **yes**, widens as √hours | yes | anyone, including strategies |
| `bridge` | Brownian bridge pinned to both observed ends | **yes**, widest mid-gap | **no** | display only |

The two model-based estimators work in **log** space — a random walk in logs cannot produce a
negative price, and a 1% move costs the same at 3 as at 30,000. Both are deterministic: they
return the posterior *mean* rather than a sampled path, because a chart that changes on every
reload is not something an operator can reason about. What they add over a straight line is the
admission of uncertainty: `sigma_log` per filled hour, rendered as a ribbon that widens with the
gap. `causal`'s mean coincides with a forward fill; the band is the point. A local-linear-trend
variant was rejected — a twenty-hour extrapolated trend is a confident fabrication.

Anything that trades is protected at the seam, not by convention. `load_symbol_series` — the
loader every strategy, signal and backtest goes through — **refuses** a non-causal policy rather
than quietly downgrading it, and `apply_fill` returns a `synthetic` mask so a filled cell stays
distinguishable wherever it travels. A gap longer than the cap (24h) is left alone entirely, not
partly filled.

### The cache

Estimates are stored so a dashboard does not recompute them on every widget change. The three
estimators are **alternatives for the same missing hour, not layers**: all three can be cached
(`method` is part of the natural key) so they can be compared, and exactly one is selected at a
time by `FILL_METHOD`, `--method`, or the Quality dashboard's picker.

```sh
make fill                                  # the selected estimator, every token
make fill ARGS="--all-methods"             # all three, so they can be compared
make fill-status                           # what is cached, per token and estimator
make fill-evaluate                         # score them against hours we already know
candlegraph fill WETH --method bridge --max-fill 12
candlegraph fill --clear --all-methods
```

`--evaluate` is how "which estimator is best" gets a falsifiable answer: real hours are held
out, each estimator re-guesses them, and the error is reported. Expect `bridge` and `linear` to
beat `causal` on most tokens — they win *because* they read the bar after the gap, which is
exactly why they are display only. On a genuinely gappy token the ranking can invert.

Rows go in `token_hours_fill` — **never** in `token_hours_data`. Only missing hours are stored,
each with the length of the run it belongs to, so the data-quality report keeps measuring what
was actually ingested and coverage can still be falsified. It is the one table in the database
that is deleted from: an estimate for an hour that has since been ingested is wrong, not stale.
The reasoning is in [`docs/roadmap/DESIGN.md`](docs/roadmap/DESIGN.md) §0 and
[`docs/storage/table-placement/DESIGN.md`](docs/storage/table-placement/DESIGN.md).

## UI

```sh
make ui                 # locally, against the stack's database
```

A trader's terminal rather than a notebook: the sidebar picks one dashboard at a time, every
dashboard opens with a metric strip, and every chart carries a title that says what to look for.

- **Market** — the whole tape: relative strength (every token rebased to 100), a movers table
  with 24h/7d/30d changes, a return-correlation heatmap over aligned bars, and a volatility ladder.
- **Token** — hourly candlesticks with the Bollinger envelope, volume, rolling volatility,
  the z-score stretch gauge, the drawdown-from-peak curve, band width (the squeeze) and %B.
  Hours with no stored row are shaded on the candles and counted as a coverage figure, so a
  chart with holes in it cannot be mistaken for a continuous one; where the cache has an
  estimate it is drawn as a **dashed** bridge anchored on the real bars either side, under a
  shaded ±1 s.d. ribbon when the selected estimator has one.
- **Quality** — can this history be trusted, and where is it thin? A **gap map** (one lane per
  token: green where hours are stored, amber where they are missing), the distribution of gap
  lengths (constant drizzle is a quiet market; one long bar is an ingestion failure), field
  completeness (a blank field is `NULL` — *not collected* — never a real zero), per-token
  coverage and staleness, the longest gaps, any impossible rows, and the **gap estimators**:
  pick one of the three, see in a sentence whether a strategy could consume it, recompute one
  or all of them, and score them against hours whose real price is already known. It reads the same
  `quality.build_report` that `candlegraph report` and the `data_quality_report` MCP tool read,
  so the three cannot disagree. Read-only: nothing here fills a gap or writes a row.
- **Risk** — VaR and CVaR at a chosen confidence, horizon and estimator; the return histogram
  with the VaR cut; the three estimators side by side as a fat-tail check; and the
  systematic-vs-idiosyncratic split against a benchmark.
- **Compare** — the A/B price ratio, the rolling correlation regime, and the beta cloud with its
  fitted slope. It opens on WETH against WBTC — two assets that actually move, since pricing a
  token in a stablecoin is a dollar chart the Token dashboard already draws. Pearson and
  Spearman correlation sit side by side (a wide gap between them means
  a few extreme hours are driving the linear number), and each leg's own total return,
  volatility and Sharpe are tabled over the same aligned bars — the twelve numbers
  `candlegraph compare` prints, rather than only the joint five.
- **Portfolio** — allocation donut, the risk budget (each asset's share of portfolio volatility),
  the efficient frontier with the solved portfolio starred, and the allocation's own VaR/CVaR.
- **Strategy** — *does this rule beat holding the token, net of costs?* Opens with a **How this
  backtest works** explainer, because a backtest whose assumptions are off-screen is unreadable:
  the one-bar execution lag, the cost model, and why `excess` is the number to read before
  `total return`. Then equity vs buy-and-hold, a **gross-against-net** chart answering "did the
  signal fail or did the fees eat it", all thirteen metrics the CLI prints, a **comparison of
  every registered strategy** on the token (what `backtest --strategy all` shows), exposure,
  drawdown, and the trade ledger. Starting capital and execution lag are controls beside the
  result; setting the lag to 0 is loudly labelled as not real.
  The **look-ahead check** re-runs the same strategy at `execution_lag=0` and plots both curves.
  Note it does not always flatter: a trend rule gains from the bias, a mean-reversion rule is
  punished by it (it shorts the bar that just rose, then collects the rise). Both numbers are
  equally unreal — the lesson is the size of the gap, not its sign.
- **Forecast** — the ARIMA path with its confidence interval drawn at the horizon.
- **Strategy → Explain** — the `signals --explain` surface: what is stored for the selected token
  and strategy shows for free; **Explain latest signal** runs the pipeline, and a checkbox decides
  whether the news source may be called (at most one Tavily credit per token per UTC day, disabled
  without a key). The rationale may cite only documents published before the bar; anything the
  narrator invented is stripped and flagged on screen.
- **Agents** — the watchtower: a **Run observer now** button (one on-demand run, written to the
  agent ledger), the latest findings coloured by severity, ingest cadence, per-token coverage,
  the full run history, and a news-health strip (did the recent Tavily runs succeed, fail, or get
  refused by the budget).
- **News** — the metered Tavily pipeline in detail: credit runway for the month and the day,
  credit burn against the daily cap, call outcomes per day (ok / refused / rate-limited / error),
  the full `source_call` ledger, the freshest stored articles, and a **Fetch news** button behind
  a confirmation that states the cost before a credit is spent.
- **Data** — what is stored and how to load more: coverage per token, recent ingest runs, and a
  **backfill** control (7 days to 1 year) that asks for confirmation before it spends network and
  writes rows. The default first fetch is `LOOKBACK_DAYS` (7); backfills reach further and never
  delete.

**Your selections are remembered.** Pick WBTC against DAI on Compare, go and look at something
else, come back: it is as you left it. Streamlit discards a widget's state as soon as a rerun
does not render it, so each choice is mirrored into the session and the widget reopens on the
mirror. **↺ Reset to defaults** in the sidebar forgets them all; it leaves an action in flight
(a backfill awaiting confirmation) alone, because that is not a preference.

**Defaults a trader expects.** Every single-token page opens on WBTC, Compare opens on WETH
against WBTC, and the Portfolio opens on the majors with stablecoins left out and a 25% cap per
token (or the smallest cap the chosen tokens can satisfy). An annualised expected return that
falls below −100% is shown as −100% and flagged; a VaR or CVaR is never reported above 100%,
because a long position cannot lose more than everything.

**Units are explicit.** Rates are entered in **percent per year** (4.25 means 4.25%; the sidebar
also shows the real, inflation-adjusted rate beside the nominal one), and trading costs in
**basis points per side** (100 bps = 1%). The CLI and `.env` take the rate as a decimal fraction
(`0.04` = 4%) and say so in their help text.

The UI's arithmetic lives in `candlegraph/ui/viewmodels.py`, not in the Streamlit script, so it
is covered by ordinary unit tests; the script itself is smoke-tested page by page with
Streamlit's `AppTest`.

## Agents

Two LangGraph graphs, both deterministic, both only readers. Each is a `StateGraph` of plain
functions over a typed state; the design, with flowcharts, is in
[`docs/agents/`](docs/agents/).

**The run observer** (`candlegraph observe`) answers three questions in writing: is the data
fresh, is it complete, and does it still look like the same market? `--progress` (what
`make observe` passes) prints each stage — `collect_ledger`, `collect_quality`, `collect_metrics`,
`assess`, `publish` — to stderr as it finishes, with its duration and what it read; the same
records land in a **Stages** section of the report, in the ledger row, and on the Agents
dashboard, and a stage the database-down short-circuit skipped is listed as skipped.

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

## Tools over MCP

`candlegraph mcp` serves the same functions the CLI calls as **Model Context Protocol** tools, so
an assistant already open in your terminal can answer "what state is this thing in" from typed
results instead of a scraped log. It is an optional extra:

```sh
uv sync --extra mcp
make mcp                 # stdio, against the stack's database on localhost
```

To register it with Claude Code, point the client at the same command:

```sh
claude mcp add candlegraph -- uv run --extra mcp python -m candlegraph mcp
```

Give it `DB_HOST=localhost` and `OLLAMA_URL=http://localhost:11434` (with `DB_PORT` and the
Ollama port adjusted if you moved them) so it reaches the compose services from outside their
network, exactly as `make mcp` does. Both matter: the in-compose hostnames do not resolve on
the host, and an unreachable embedder leaves retrieval on the lexical leg alone — which it will
say, rather than failing.

Fourteen tools, all reading what already exists: `status`, `recent_ingest_runs`,
`data_quality_report`, `candles`, `stats`, `compare`, `signals`, `backtest`, `agent_runs`,
`latest_agent_report`, `observe`, `search_knowledge`, `explain_signal` and `knowledge_budget`.
The design documents and post-mortems are served as resources
(`candlegraph://docs/roadmap`, `candlegraph://incidents/<slug>`), and two prompts package the
reading order the skills describe.

**No tool writes market data.** The only writers in the listing are `observe`, which records one
`agent_run` row because that is what an agent run is, and `explain_signal`, which upserts an
`explanation` — and which spends a search credit only when called with `search: true`, under the
same budget guard as everything else, so at most one credit per token per UTC day. Both are
annotated so a client can see it before calling. Every result is redacted on the way out with the
same function the agent reports use. `--transport http` serves the same tools over streamable
HTTP for a client in another container; there is no compose service for it yet.

The reasoning, including why there is deliberately no MCP *client* for Tavily, is in
[`docs/knowledge/mcp/DESIGN.md`](docs/knowledge/mcp/DESIGN.md).

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

Six pieces of work are delivered and covered by the test suite:

- **Stabilisation** — a clean clone that builds, lints, type-checks and comes up.
- **Data model and ingestion** — hourly token and pool data, idempotent upserts,
  the ingest ledger, the data-quality report.
- **Deterministic analytics** — rolling statistics, mean-variance portfolios,
  three strategies, a costed and lagged backtester, the Streamlit UI.
- **Agent layer** — the run observer and the CI janitor, with their ledger and
  report sinks.
- **Time-series storage** — the series tables as TimescaleDB hypertables.
- **Knowledge graph and signaller** — the corpus on pgvector, local embeddings,
  Tavily under a credit budget, hybrid retrieval that never looks past the bar,
  `signals --explain` with its citation validator, and the read-only MCP server.

Next, in order:

0. **Descriptive statistics, deeper** — pairwise comparison between any two
   tokens (delivered: `candlegraph compare`), a five-token active universe
   for the test run (WBTC, WETH, BNB, and the gap-prone GNO and SHIB), and
   a recorded policy for filling ingestion gaps on thin tokens —
   interpolation for display only, a causal estimator for anything a
   strategy consumes, nothing written to the database.
1. ~~**Knowledge graph and signaller**~~ — *delivered 2026-11-06*. Assets,
   sources and documents in Postgres with pgvector, Tavily for web search,
   and news attached to prices, so a signal can cite *why*. Paper trading
   only. RAGFlow was evaluated as a second retrieval backend and declined
   for now (`docs/knowledge/ragflow/DESIGN.md`). See `docs/knowledge/` and
   the archived plan under `docs/rollout/archive/`.
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
