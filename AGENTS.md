# AGENTS.md

Vendor-neutral working agreement for this repository. Humans, CI, and coding
agents all follow this file. `CLAUDE.md` imports it and adds Claude-specific
notes; it does not restate anything here.

## Purpose

candlegraph ingests hourly Uniswap v3 token market data from The Graph into
PostgreSQL/TimescaleDB and serves OHLC candles over GraphQL. On top of it sit
a deterministic analytics layer and an agent layer (LangGraph graphs that
observe the system and post reports). "Roadmap" below says what is delivered
and what is next; `docs/` says why each piece is shaped the way it is.

## Commands

`Makefile` is the single source of truth. Never document a command that is not
a target, and never let CI run something that is not one either.

| Task | Command |
|---|---|
| Install the toolchain | `make install` |
| Format and auto-fix | `make fmt` |
| Lint / types | `make lint` / `make typecheck` |
| Fast offline tests | `make test-unit` |
| Tests needing Postgres | `make test-integration` |
| Everything CI runs | `make check` |
| Coverage report | `make cov` |
| Run the stack | `make up`, then `make logs` / `make logs-worker`, `make down` |
| Start over from a bad volume | `make reset` (drops the volume and this project's image, then `make up`) |
| One ingest pass | `make ingest` |
| Backfill 90 days | `make backfill days=90` |
| Data-quality report | `make report` |
| Recent ingest runs | `make runs` |
| Sync assets and aliases from `tokens.yaml` | `make sync-assets` (in the stack) or `candlegraph knowledge sync-assets` |
| Stack plus the embedding model server | `make up-knowledge` (first start pulls `bge-m3`), `make logs-ollama` |
| Embed chunks / re-encode after a model change | `make embed` / `make reembed` (or `candlegraph knowledge embed\|reembed`) |
| Rolling mean / volatility | `candlegraph stats WETH --window 24` |
| Pairwise comparison | `candlegraph compare WETH WBTC` |
| Signals | `candlegraph signals WETH --strategy bollinger [--store]` |
| Backtest | `candlegraph backtest WETH --strategy all` |
| Portfolio | `candlegraph portfolio --risk-free-rate 0.04 --objective max_sharpe` |
| ARIMA forecast | `candlegraph forecast WETH --auto-order` |
| Risk report | `candlegraph risk WETH --benchmark WBTC --confidence 0.99` |
| Streamlit UI | `make ui` (local) or the `ui` compose service |
| Run observer, once | `make observe` (in the stack) or `candlegraph observe [--strict]` |
| CI janitor, once | `make janitor ARGS="--open-pr --strict"` |
| Agent reports | `make agent-runs` / `make logs-agents` |
| Agents on a schedule | the `agents` compose service (`candlegraph agents`); the janitor's schedule is `.github/workflows/janitor.yml` |
| End-to-end against the stack | `make e2e` |
| New migration | `make revision m="description"` |
| Apply migrations | `make migrate` |
| Regenerate the GraphQL SDL | `make sdl` |

Dependencies are managed by **uv**, not Poetry or pip. `pyproject.toml` plus
`uv.lock` are the contract; add dependencies with `uv add` and commit the lock.

## Layout

```
candlegraph/
  settings.py          Typed configuration (pydantic-settings). Cross-cutting.
  tokens.py            Loads and validates config/tokens.yaml into a TokenRegistry.
  cli.py               Every command; see the table above.
  storage/
    database.py        Connection pool and query helpers. Connects lazily.
    statements.py      Every SQL statement, built per schema.
    preflight.py       Pure rules alembic/env.py checks before migrating: preload, known revision.
  ingestion/
    subgraph.py        Synchronous GraphQL client for The Graph gateway.
    transform.py       Subgraph rows -> database rows. Pure.
    runner.py          One ingest run: cursors in, upserted rows out.
    ledger.py          The ingest_run table: who ran, when, and how it went.
    quality.py         Gap detection and the data-quality report.
    worker.py          The standalone ingest process.
  analytics/           Pure numerics. No I/O except prices.py, which only reads.
    rolling.py         Rolling mean, volatility, z-score, Bollinger, drawdown.
    pairwise.py        Two-token comparison: correlation, beta, relative return. Aligned bars only.
    portfolio.py       Mean-variance optimisation and the efficient frontier.
    arima.py           ARIMA fitting, order selection, forecasting.
    risk.py            VaR, CVaR, systematic/idiosyncratic split, the risk report. Pure.
    rates.py           RateProvider seam: nominal and real risk-free rate; constant today, Tavily planned.
    prices.py          Stored rows -> aligned pandas frames.
  strategies/
    base.py            Signal, StrategyResult, and the registry.
    bollinger.py       Mean reversion with hysteresis.
    crossover.py       Moving-average trend following.
    arima_signal.py    Walk-forward ARIMA signals.
    backtest.py        Vectorised backtester. Owns the execution lag.
    store.py           Upserting signals into the signal table.
  api/
    app.py             FastAPI app factory and health check.
    schema.py          Strawberry GraphQL types and resolvers.
    formatting.py      Database rows -> chart payload. Pure.
  ui/
    viewmodels.py      Everything the UI computes. Plain Python, fully tested.
    app.py             The Streamlit script: widgets and charts only.
  knowledge/           Stage 1, growing with docs/rollout/ROLLOUT.md. So far: schema, assets, documents, embeddings.
    statements.py      Every SQL statement for the knowledge tables, built per schema.
    assets.py          tokens.yaml aliases -> asset rows. Pure transform + one upsert.
    documents.py       URL normalisation -> url_hash, sentence-boundary chunking (pure), document/chunk upserts.
    embed.py           Embedder Protocol; Ollama (bge-m3, default), OpenAI, and fake providers; resumable embed loop.
  agents/              LangGraph graphs. Readers and reporters; never writers of market data.
    base.py            Severity, Finding, AgentReport: the vocabulary every graph speaks.
    shell.py           Subprocess runner behind a Protocol, plus secret redaction.
    narrator.py        Findings -> summary paragraph. Template by default; Claude when configured.
    reporting.py       Markdown rendering and the sinks: log, ledger, file, webhook, github.
    ledger.py          The agent_run table.
    scheduler.py       APScheduler wiring and the `candlegraph agents` process.
    observer/          Stale data, gaps on liquid tokens, metric drift. assess.py is pure.
    janitor/           Run the checks, apply mechanical fixes, re-check, draft a PR.
  utils/
    logging_utils.py   Logger setup.
alembic/versions/    Migrations. The schema name comes from settings.
config/tokens.yaml   The tokens this deployment tracks, with their knowledge-layer aliases.
docs/<area>/<topic>/DESIGN.md   Design documents. Why, not how; `docs/README.md` indexes them.
  roadmap/           Delivered stages, next stages, and each exit criterion.
  storage/           TimescaleDB adoption, which tables are hypertables, and how pgvector coexists.
  analytics/         The statistics and models with their equations; risk management and the rate seam.
  rollout/ROLLOUT.md The one current rollout plan: ordered steps and a progress table.
  agents/            The agent framework, the observer, and the janitor.
  knowledge/         Stage 1: schema, vector store, embeddings (implemented); Tavily, MCP, sources (proposed).
  incidents/<date>-<slug>/POSTMORTEM.md   What went down, why, and what changed so it cannot again.
tests/unit/          Offline. No database, no network. Mirrors the package layout.
tests/integration/   Throwaway TimescaleDB (testcontainers, or TEST_DATABASE_URL).
tests/e2e/           Against a running stack; skipped unless CANDLEGRAPH_E2E_URL is set.
schema.graphql       Generated. `make sdl`; a test fails if it is stale.
```

## Data model

| Table | Grain | Natural key | Storage |
|---|---|---|---|
| `token` | one row per tracked token | `id` | plain |
| `token_hours_data` | one row per token per hour | `(token_id, period_start_unix)` | **hypertable** |
| `pool` | one row per tracked Uniswap v3 pool | `id` | plain |
| `pool_hours_data` | one row per pool per hour | `(pool_id, period_start_unix)` | **hypertable** |
| `ingest_run` | one row per ingest run | `run_id` | plain |
| `signal` | one row per strategy, symbol and bar | `(strategy, symbol, period_start_unix)` | **hypertable** |
| `agent_run` | one row per agent graph run | `run_id` | plain |
| `asset` | one row per knowledge entity (token addresses today) | `id` | plain |
| `source` | one row per document source (`tavily`, `rss:<feed>`, `manual`) | `id` | plain |
| `document` | one row per retrieved document | `url_hash` (unique; sha256 of the normalised URL) | plain |
| `chunk` | one embedded piece of a document; `embedding halfvec(1024)` + `embedding_model`, one active model | `(document_id, ordinal)` | plain — the ANN index must be global |
| `mention` | a chunk naming an asset | `(chunk_id, asset_id, method)` | plain |
| `edge` | a derived, time-bounded relation between assets | `(from_id, to_id, relation, valid_from)` | plain |
| `explanation` | one explained signal | `(strategy, symbol, period_start_unix)` | plain for now |
| `source_call` | one metered-source call, refusals included | `id` | plain |

The database is **TimescaleDB** (`timescale/timescaledb-ha:pg17.11-ts2.31.0`, pinned
in compose, in CI, and in the testcontainer), not plain PostgreSQL. The `-ha`
image also ships pgvector (installed by the knowledge migration, which the
stage-1 tables use for `chunk.embedding halfvec(1024)`) and pgvectorscale
(available, not installed). Vectors come from the `ollama` compose service
(`bge-m3`, profile `knowledge`); the column width is fixed at 1024 and a
model change is `make reembed`, never a migration. The three
series tables are chunked monthly on `period_start_unix`; the state and
ledger tables are deliberately ordinary tables. See invariant 24 and
`docs/storage/table-placement/DESIGN.md`.

Pool data is not optional detail: the subgraph carries **no transaction count
on the token entity at all**, so `tx_count`, per-pair volume, and pool TVL are
only reachable through `pool_hours_data`.

Enrichment columns are nullable on purpose. `NULL` means "not collected" —
rows written before the phase-2 migration genuinely have no value — and that
must stay distinguishable from a real `0`, which would skew any later average.

## Invariants

Each of these has a test. Breaking one should break the build, not production.

1. **Importing a module connects to nothing.** The pool is built on first use.
   This is what keeps `make test-unit` runnable with no database anywhere.
2. **Unit tests make no network calls.** `tests/unit/conftest.py` turns a real
   outbound request into a failure. Inject an `httpx.MockTransport`.
3. **Writes raise on failure.** Never log-and-return-zero in the write path; a
   silent partial write is a data-loss bug.
4. **Hourly rows upsert on `(token_id, period_start_unix)`.** That is the
   natural key: one row per token per hour. Re-ingesting is idempotent.
   **Ingestion never deletes.** Stored history is the input to the analytics
   and backtesting phases; `LOOKBACK_DAYS` only bounds the first fetch for a
   token with no rows yet.
5. **Candle times are bucket boundaries**, computed from the interval — never
   `MIN(timestamp)` of the rows present, which shifts when an hour is missing.
6. **Buckets with no data produce no candle.** Gaps are absent, not zero-filled.
7. **The API starts even when the subgraph is down.** Ingestion runs on the
   background scheduler, including the first backfill. Never block startup on it.
8. **A paging loop that cannot advance raises.** Never retry forever in place.
9. **Timestamps are UTC end to end.** The pool pins `timezone=UTC`; the column
   is `timestamptz`.
10. **Configuration is typed.** Read settings through `get_settings()`; never
    call `os.getenv` outside `settings.py`.
11. **A missing field becomes `NULL`, never `0`.** See "Data model" above.
12. **Every run is recorded in `ingest_run`**, and a ledger failure never
    takes the run down with it.
13. **A backfill rewinds.** An explicit `--days` window reaches back past the
    stored history; a normal poll only ever walks forwards.
14. **Pools are discovered one token at a time.** Batching the whole token
    list into one `first: N` query lets the most liquid token take every slot.
15. **A backtest lags execution by one bar.** A position decided on bar *t*
    earns bar *t+1*'s return. Multiplying an unshifted signal by same-bar
    returns manufactures a profitable backtest silently.
16. **Rolling windows are complete or absent.** `min_periods=window`, and
    standard deviations use `ddof=1`.
17. **A strategy never looks ahead.** The walk-forward ARIMA refits on
    `prices[: i + 1]`; appending future bars must not change past signals.
18. **Analytics are pure.** Only `analytics/prices.py` touches the database,
    and only to read. The UI's arithmetic lives in `ui/viewmodels.py`, never
    in the Streamlit script.
19. **A missing hour is not a fault.** Uniswap emits no row for an hour with
    no swaps, so quality is judged by coverage against
    `MIN_EXPECTED_COVERAGE`, never by the presence of a gap.
20. **No secrets in the repo.** `GRAPH_API_KEY` lives in `.env`, which is
    gitignored. `.env.example` documents it with an empty value.
21. **Agents never write market data.** Their whole write surface is the
    `agent_run` table and the configured report sinks. A graph that fails
    still posts a report with status `error`; a sink that fails is logged
    and skipped, never raised. Output that reaches a report is redacted
    first.
22. **Findings are computed; prose is optional.** An agent's status and exit
    code come from its findings alone. The optional model narrator sees the
    findings and nothing else, and cannot add, remove, or re-grade one.
23. **The janitor fixes only what is mechanical.** `fmt-check` and `lint`
    map to `make fmt`; everything else is reported. It never applies a fix
    to a tree with uncommitted changes, only stages tracked files, and
    refuses to commit a path that looks like a secret.
24. **Series tables are hypertables; state and ledger tables are not.**
    `token_hours_data`, `pool_hours_data` and `signal` are chunked on
    `period_start_unix`. `token`, `pool`, `ingest_run` and `agent_run` are
    plain — the first two have no time coordinate, and `ingest_run` is
    updated by `run_id`, which on a time-partitioned table would search
    every chunk. A hypertable requires every unique index to contain the
    partitioning column, which is why those three primary keys are
    `(id, period_start_unix)`. Both halves are tested;
    `docs/storage/table-placement/DESIGN.md` has the rule for a new table.
25. **Health probes are cheap and quiet after start-up.** Every compose
    `healthcheck` polls fast only inside `start_period` (`start_interval`)
    and every three minutes after; `/status` costs one database round trip;
    a successful probe is not written to the access log, a failing one is.
    Nothing consumes health after start-up, so a probe every few seconds
    was work for no one and buried the real log lines. See
    `docs/incidents/2026-10-29-health-probe-cadence/`.
26. **The stack starts the same way from any state.** One service builds
    the image, tagged `candlegraph:<project>`; one container applies
    migrations (`RUN_MIGRATIONS`) and the rest wait for it to be healthy;
    app containers restart `on-failure` a bounded number of times so a
    deterministic failure stays visible instead of looping; the Postgres
    probe uses TCP so initdb's temporary server cannot pass it. The remedy
    for a bad volume is `make reset`, never "clean Docker". See
    `docs/incidents/2026-10-29-compose-startup-robustness/`.

## Definition of done

A change is not finished until all of these hold:

- [ ] `make check` passes.
- [ ] New behaviour has tests in the right tier (prefer the fastest that can
      actually prove it).
- [ ] Schema changes ship with a migration that has a working `downgrade`.
- [ ] New or changed configuration appears in `.env.example` with a comment.
- [ ] `make sdl` has been run if the GraphQL schema changed.
- [ ] This file is updated if a command, invariant, or layout entry changed.
- [ ] `README.md` is updated if user-facing behaviour changed.
- [ ] The matching document in `docs/` is updated if a design decision
      changed, and its **Status** line still tells the truth.

## Conventions

- Each package is a section of the work: `storage`, `ingestion`, `api`.
  Put new modules in the section that owns them, and mirror that under
  `tests/unit/`. Pure transforms live beside the section that uses them.
- Test names state the behaviour (`test_a_gap_produces_no_candle_rather_than_an_empty_one`),
  not the function under test.
- Comments explain *why*, especially where the obvious implementation is wrong.
  Several invariants above exist because the obvious version was the bug.
- Docstrings use Google style with Args/Returns/Raises where it adds something.
- Public functions are typed; `mypy --strict` covers the package.

## Roadmap

`docs/roadmap/DESIGN.md` is the authoritative version, with the reasoning,
the diagram, and the design decisions each stage turns on. This is the
summary. Every stage carries an exit criterion because "done" is otherwise
unfalsifiable.

### Delivered

| Stage | What it added | Exit criterion, met |
|---|---|---|
| Stabilisation | A clean clone that builds, lints, type-checks and comes up under compose; `Makefile` as the one source of commands. | `make check && make up` |
| Data model and ingestion | Hourly token and pool data, idempotent upserts, the `ingest_run` ledger, the data-quality report. | `make backfill days=90 && make report` |
| Deterministic analytics | Rolling statistics, mean-variance portfolios, Bollinger/crossover/ARIMA signals, the `signal` table, a costed and lagged backtester, the Streamlit UI. | `candlegraph signals WETH --store && candlegraph backtest WETH --strategy all` |
| Agent layer | The run observer and the CI janitor as LangGraph graphs, the `agent_run` ledger, report sinks, the `agents` service and the janitor workflow. | `make observe && make agent-runs` |
| Time-series storage | The three series tables as TimescaleDB hypertables; state and ledger tables deliberately plain. | `make test-integration` (`test_timescale.py`) |

### Next

0. **Descriptive statistics, deeper.** Pairwise comparison between any two
   tracked tokens (*delivered*: `candlegraph compare WETH WBTC`, computed
   over aligned bars only — hours both tokens traded). Still to land: an
   `active` flag in `config/tokens.yaml` trimming the test run to five
   active tokens (WBTC, WETH, BNB, plus GNO and SHIB chosen for their
   gapped history), and the recorded gap-filling policy — linear
   interpolation for display only, a causal estimator for anything a
   strategy consumes, nothing ever written to the database. The tradeoff
   is in `docs/roadmap/DESIGN.md`.
   *Exit:* `candlegraph compare WETH WBTC` prints correlation, beta and
   relative performance over aligned bars (met); the stack ingests exactly
   the five active tokens; the fill policy exists behind `FillPolicy` with
   a no-look-ahead test.
1. **Knowledge graph and signaller.** Entities for assets, pools, sources
   and documents in Postgres with pgvector; Tavily for web search; news and
   text augmentation attached to the prices already stored. Paper trading
   only. *Designed* in `docs/knowledge/` (five documents: graph, vector
   store, embeddings, Tavily, MCP) and sequenced in
   `docs/rollout/ROLLOUT.md`, the step-by-step plan the build follows;
   read both and the `extend-knowledge-graph` skill before writing code.
   The stage opens with a pinned image switch to `timescaledb-ha` for
   pgvector, which is a volume rebuild.
   *Exit:* `candlegraph signals WETH --explain` prints a rationale citing a
   retrieved document with a URL and a timestamp, stored with the signal.
2. **Harness.** YAML pipeline specs, an event bus, OpenTelemetry metrics,
   and the continuous aggregates deferred from the TimescaleDB migration.
   *Exit:* one `candlegraph run pipelines/hourly.yaml` executes ingest →
   validate → analyse → signal as one orchestrated, traced run.
3. **Traditional markets.** NASDAQ and NYSE equities alongside crypto: a
   second ingestion adapter and an `instrument` supertype above `token`.
   Note that invariant 19 inverts here — a missing hour inside a trading
   session *is* a fault — so coverage becomes calendar-aware.
   *Exit:* `candlegraph report` shows an NYSE instrument at full coverage
   across a week containing a market holiday, without flagging it.
4. **Models and strategies.** Momentum (time-series and cross-sectional),
   GARCH and other models, and walk-forward evaluation as a command rather
   than a habit. Invariants 15–17 do not relax.
   *Exit:* `candlegraph backtest WETH --strategy all` includes momentum, and
   a walk-forward report covers every registered strategy, net of costs.
5. **Market-watch agents.** An RSS and feed observer writing into the
   knowledge graph, and an alerting agent with deduplication and cooldowns.
   *Exit:* a market event produces exactly one cited alert, and an identical
   event inside the cooldown produces none.

Prefer adding columns and a migration over reshaping the hourly tables; the
natural keys are settled. A table added from here gets its placement
decision recorded in `docs/storage/table-placement/DESIGN.md` first.
