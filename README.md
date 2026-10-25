# candlegraph

A digital asset market data service. candlegraph ingests hourly Uniswap v3 token prices and
metadata from The Graph, stores them in PostgreSQL, and serves OHLC candles through a GraphQL API
for charts and analytics.

**Stack:** Python 3.12, FastAPI, Strawberry GraphQL, PostgreSQL 17, SQLAlchemy/Alembic,
APScheduler, uv, Docker.

## How it works

1. On startup the service applies migrations, then starts serving immediately.
2. A background scheduler runs the first backfill and re-polls every `DATA_POLL_INTERVAL`
   seconds. Each run resumes from the newest hour already stored, so restarts re-fetch nothing
   and writes are idempotent.
3. The API groups the stored hourly rows into candles of any width at query time.

Ingestion deliberately runs *alongside* the server rather than before it: if The Graph is
unreachable, the API still comes up and serves whatever data is already stored.

The tokens tracked are config, not code — see [`config/tokens.yaml`](config/tokens.yaml)
(currently WBTC, SHIB, GNO).

## Quick start

You need Docker and a free Graph API key from <https://thegraph.com/studio/apikeys/>.

```sh
cp .env.example .env
$EDITOR .env            # set GRAPH_API_KEY
make up                 # builds, starts, and waits until healthy
```

Then:

```sh
curl -s localhost:8000/status            # {"status":"ok","database":"up",...}
make logs                                # "Ingested 168 hourly rows for WBTC"
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
| `LOOKBACK_DAYS` | 7 | How far back a first-time backfill reaches. |
| `DATA_POLL_INTERVAL` | 300 | Seconds between ingest runs. |
| `RETAIN_HISTORY` | `true` | `false` deletes rows older than the lookback window after each run. |
| `INGEST_ENABLED` | `true` | `false` serves the API only. |
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

## Tests

| Tier | Needs | Command |
|---|---|---|
| `tests/unit` | nothing — no database, no network | `make test-unit` |
| `tests/integration` | Docker, or `TEST_DATABASE_URL` | `make test-integration` |
| `tests/e2e` | a running stack | `make up && make e2e` |

The integration tier starts a throwaway PostgreSQL with testcontainers, or uses
`TEST_DATABASE_URL` if set (which is how CI does it). Tiers that cannot run skip rather than fail,
so check the skip count before reading a green run as proof.

## Contributing

[`AGENTS.md`](AGENTS.md) has the working agreement: commands, layout, the invariants the test
suite pins down, and the definition of done. Read it before changing anything.
