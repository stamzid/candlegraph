# candlegraph

A digital asset market data service. candlegraph ingests hourly Uniswap v3 token prices and metadata from the Uniswap subgraph, stores them in Postgres, and serves OHLC candles through a GraphQL API for charts and analytics.

**Stack:** Python 3.11, FastAPI, Strawberry GraphQL, PostgreSQL, SQLAlchemy/Alembic, APScheduler, Poetry, Docker.

> **Status:** the default `TRANSPORT_URL` points to The Graph's legacy hosted service. Moving to The Graph gateway (API key required) is the first item on the roadmap.

## How it works

1. On startup, the ingester backfills the last `LOOKBACK_DAYS` of `tokenHourDatas` and token metadata for each supported token.
2. A scheduler keeps polling every `DATA_POLL_INTERVAL` seconds.
3. The API aggregates hourly rows into candles of any size (`timeUnitInHours`) at query time.

Supported tokens live in `candlegraph/tokens.py` (currently WBTC, SHIB, GNO).

## Quick start

```sh
docker compose up -d
```

Or run locally against the Postgres container:

```sh
docker compose up -d postgres
poetry install
poetry run alembic upgrade head
poetry run python -m candlegraph.app
```

Locally, set `DB_HOST=localhost` and change the host in `alembic.ini`'s `sqlalchemy.url` to `localhost`; both default to the Docker hostname.

The API is at `http://localhost:8000/graphql` (GraphiQL in the browser), with a health check at `/status`.

## Configuration

Copy `.env.example` and adjust. The DB credentials in the repo (`candlegraph`/`candlegraph`) are throwaway defaults for the local Docker Postgres only.

| Variable | Default | Purpose |
|---|---|---|
| `TRANSPORT_URL` | legacy hosted subgraph | Uniswap v3 subgraph endpoint |
| `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASS`, `DB_NAME`, `DB_SCHEMA` | local Docker values | Postgres connection |
| `MINIMUM_DB_CONNECTIONS` / `MAXIMUM_DB_CONNECTIONS` | 16 / 1024 | Connection pool size |
| `LOOBACK_DAYS` | 7 | Days of history to ingest and keep (name as currently read by `settings.py`) |
| `DATA_POLL_INTERVAL` | 300 | Seconds between polls |
| `PERSISTANCE_MODE` | unset | Unset deletes data older than the lookback window; any value keeps it |

## API

```graphql
query {
  getChartData(tokenSymbol: "WBTC", timeUnitInHours: 2) {
    tokenMetadata { id name symbol totalSupply volumeUsd decimals }
    candles { time priceType value }
  }
}
```

`candles` is a list of five series (`open`, `close`, `high`, `low`, `priceUSD`), each sorted by time:

```json
"candles": [
  [{ "time": "2024-01-01T00:00:00", "priceType": "open",  "value": 50000 }, ...],
  [{ "time": "2024-01-01T00:00:00", "priceType": "close", "value": 50500 }, ...],
  ...
]
```

If there's no data for the token, metadata fields come back empty and each series holds a single zero-valued point.

## Schema migrations

```sh
poetry run alembic revision -m "describe change"   # new migration
poetry run alembic upgrade head                    # apply
poetry run alembic downgrade -1                    # roll back one
```

## Tests

```sh
poetry run python -m unittest tests.test_helpers   # unit tests, no DB or network
docker compose up -d && poetry run pytest tests/test_endpoint.py   # end-to-end, needs the stack running
```

## Design notes

- **Postgres:** structured time series, fast aggregate queries, and Alembic for schema history.
- **FastAPI + Strawberry:** async ASGI server with strongly typed GraphQL; Strawberry is the GraphQL library FastAPI's docs point to.
- **Query-time aggregation:** hourly rows are stored once and bucketed on read, so any candle size works without extra tables.
- **Poetry + Docker:** reproducible installs locally and a cached multi-stage image for deployment.

## Roadmap

- Move to The Graph gateway, fix compose and settings, offline test suite with CI
- Correct per-candle OHLC, gap handling, reliable backfill and incremental ingestion
- More tokens, pools and chains; analytics (returns, volatility, VWAP, drawdown)
- Dashboard, alerts, caching and metrics, AWS deployment with Terraform
- Knowledge graph linking price moves to public news (RSS, Substack)
- Read-only research agents that explain price moves using the app's own data

## License

MIT
