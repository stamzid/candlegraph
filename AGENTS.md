# AGENTS.md

Vendor-neutral working agreement for this repository. Humans, CI, and coding
agents all follow this file. `CLAUDE.md` imports it and adds Claude-specific
notes; it does not restate anything here.

## Purpose

candlegraph ingests hourly Uniswap v3 token market data from The Graph into
PostgreSQL and serves OHLC candles over GraphQL. It is the data foundation for
a planned analytics, backtesting, and agent layer; see "Roadmap" below.

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
| One ingest pass | `make ingest` |
| Backfill 90 days | `make backfill days=90` |
| Data-quality report | `make report` |
| Recent ingest runs | `make runs` |
| Rolling mean / volatility | `candlegraph stats WETH --window 24` |
| Signals | `candlegraph signals WETH --strategy bollinger [--store]` |
| Backtest | `candlegraph backtest WETH --strategy all` |
| Portfolio | `candlegraph portfolio --risk-free-rate 0.04 --objective max_sharpe` |
| ARIMA forecast | `candlegraph forecast WETH --auto-order` |
| Streamlit UI | `make ui` (local) or the `ui` compose service |
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
  ingestion/
    subgraph.py        Synchronous GraphQL client for The Graph gateway.
    transform.py       Subgraph rows -> database rows. Pure.
    runner.py          One ingest run: cursors in, upserted rows out.
    ledger.py          The ingest_run table: who ran, when, and how it went.
    quality.py         Gap detection and the data-quality report.
    worker.py          The standalone ingest process.
  analytics/           Pure numerics. No I/O except prices.py, which only reads.
    rolling.py         Rolling mean, volatility, z-score, Bollinger, drawdown.
    portfolio.py       Mean-variance optimisation and the efficient frontier.
    arima.py           ARIMA fitting, order selection, forecasting.
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
  utils/
    logging_utils.py   Logger setup.
alembic/versions/    Migrations. The schema name comes from settings.
config/tokens.yaml   The tokens this deployment tracks.
tests/unit/          Offline. No database, no network. Mirrors the package layout.
tests/integration/   Throwaway PostgreSQL (testcontainers, or TEST_DATABASE_URL).
tests/e2e/           Against a running stack; skipped unless CANDLEGRAPH_E2E_URL is set.
schema.graphql       Generated. `make sdl`; a test fails if it is stale.
```

## Data model

| Table | Grain | Natural key |
|---|---|---|
| `token` | one row per tracked token | `id` |
| `token_hours_data` | one row per token per hour | `(token_id, period_start_unix)` |
| `pool` | one row per tracked Uniswap v3 pool | `id` |
| `pool_hours_data` | one row per pool per hour | `(pool_id, period_start_unix)` |
| `ingest_run` | one row per ingest run | `run_id` |
| `signal` | one row per strategy, symbol and bar | `(strategy, symbol, period_start_unix)` |

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

Phases 1 (stabilise), 2 (data model and ingestion) and 3 (deterministic
analytics) are complete. Phase 3 shipped rolling statistics, mean-variance
optimisation with a configurable risk-free rate, Bollinger mean reversion,
walk-forward ARIMA signals, the `signal` table, a vectorised backtester and
a Streamlit UI; its exit criterion is met by
`candlegraph signals WETH --store && candlegraph backtest WETH --strategy all`.

Next phases, each with an exit criterion:

4. **Agent layer.** A CI janitor graph and a run-observer graph (LangGraph).
   *Exit:* both run on a schedule and post reports.
5. **Harness.** YAML pipeline specs, an event bus, OpenTelemetry metrics.
   *Exit:* ingest → validate → analyse → signal as one orchestrated pipeline.
6. **Knowledge graph and signaller.** Entities for assets, pools, authors, and
   on-chain events in Postgres with pgvector. Paper trading only.
   *Exit:* signals carry explanations citing graph context.

Phase 2 is where the schema grows. Prefer adding columns and a migration over
reshaping `token_hours_data`; the natural key is settled.
