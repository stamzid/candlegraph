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
| Run the stack | `make up`, then `make logs`, `make down` |
| End-to-end against the stack | `make e2e` |
| New migration | `make revision m="description"` |
| Apply migrations | `make migrate` |
| Regenerate the GraphQL SDL | `make sdl` |

Dependencies are managed by **uv**, not Poetry or pip. `pyproject.toml` plus
`uv.lock` are the contract; add dependencies with `uv add` and commit the lock.

## Layout

```
candlegraph/
  settings.py        Typed configuration (pydantic-settings). Everything reads from here.
  tokens.py          Loads and validates config/tokens.yaml into a TokenRegistry.
  subgraph_client.py Synchronous GraphQL client for The Graph gateway.
  ingest.py          One ingest run: cursors in, upserted rows out.
  dba.py             Connection pool and every SQL statement.
  helpers.py         Pure transforms. No I/O, so most tests live against this.
  schema.py          Strawberry GraphQL types and resolvers.
  app.py             FastAPI app factory, health check, ingest scheduler.
  __main__.py        Console entrypoint.
alembic/versions/    Migrations. The schema name comes from settings.
config/tokens.yaml   The tokens this deployment tracks.
tests/unit/          Offline. No database, no network.
tests/integration/   Throwaway PostgreSQL (testcontainers, or TEST_DATABASE_URL).
tests/e2e/           Against a running stack; skipped unless CANDLEGRAPH_E2E_URL is set.
schema.graphql       Generated. `make sdl`; a test fails if it is stale.
```

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
11. **No secrets in the repo.** `GRAPH_API_KEY` lives in `.env`, which is
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

- Test names state the behaviour (`test_a_gap_produces_no_candle_rather_than_an_empty_one`),
  not the function under test.
- Comments explain *why*, especially where the obvious implementation is wrong.
  Several invariants above exist because the obvious version was the bug.
- Docstrings use Google style with Args/Returns/Raises where it adds something.
- Public functions are typed; `mypy --strict` covers the package.

## Roadmap

Phase 1 (stabilise) is complete. Next phases, each with an exit criterion:

2. **Data model and ingestion.** Volume, volumeUSD, TVL, fees, tx count, and
   pool-level data. Ingest worker split from the API, a run ledger, gap
   detection. *Exit:* idempotent 90-day backfill for ten tokens with a
   data-quality report.
3. **Deterministic analytics.** Moving-average crossover, Bollinger mean
   reversion, one time-series signal; MPT frontier and risk parity.
   *Exit:* a signal table and a backtest report reproducible from the CLI.
4. **Agent layer.** A CI janitor graph and a run-observer graph (LangGraph).
   *Exit:* both run on a schedule and post reports.
5. **Harness.** YAML pipeline specs, an event bus, OpenTelemetry metrics.
   *Exit:* ingest → validate → analyse → signal as one orchestrated pipeline.
6. **Knowledge graph and signaller.** Entities for assets, pools, authors, and
   on-chain events in Postgres with pgvector. Paper trading only.
   *Exit:* signals carry explanations citing graph context.

Phase 2 is where the schema grows. Prefer adding columns and a migration over
reshaping `token_hours_data`; the natural key is settled.
