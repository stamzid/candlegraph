---
name: run-stack
description: Build and run the candlegraph docker compose stack, verify it is healthy and ingesting, inspect logs, and tear it down. Use when asked to run, start, restart, debug, or stop the local stack.
---

# Run the stack

## Prerequisites

`.env` must exist with a real `GRAPH_API_KEY` or ingestion returns auth errors
(the API still starts and serves — that is by design).

```sh
test -f .env || cp .env.example .env
```

## Start

```sh
make up      # build, start, and block until both containers are healthy
```

`make up` fails if any container never reaches healthy. The services come up
in order: `postgres` (TimescaleDB, not plain PostgreSQL), then `candlegraph`
(the API, which applies the migrations), then `worker` (which ingests), `ui`,
and `agents`. The worker waits for the API to be *healthy* so two containers
never race on the same DDL.

## Verify

```sh
curl -s localhost:8000/status | python3 -m json.tool
```

Expect `"status": "ok"`, `"database": "up"`, and the configured token symbols.

Confirm the storage layer came up as intended — three hypertables, and the
ledger and state tables left plain:

```sh
docker compose exec -T postgres psql -U candlegraph -d candlegraph -c \
  "SELECT hypertable_name FROM timescaledb_information.hypertables ORDER BY 1;"
```

Ingestion starts a moment after the server; give it ~20s, then:

```sh
make logs                      # expect "Ingested N hourly rows for <SYMBOL>"
docker compose exec -T postgres psql -U candlegraph -d candlegraph -c \
  "SELECT symbol, COUNT(*), to_timestamp(MAX(period_start_unix)) AS newest
   FROM candlegraph.token_hours_data GROUP BY symbol ORDER BY symbol;"
```

Query candles:

```sh
curl -s -X POST localhost:8000/graphql -H 'content-type: application/json' \
  --data '{"query":"{ getChartData(tokenSymbol:\"WBTC\", timeUnitInHours:24){ candles{ time priceType value } } }"}'
```

GraphiQL is at <http://localhost:8000/graphql> in a browser.

Full end-to-end assertions, once rows exist:

```sh
make e2e
```

## Stop

```sh
make down    # also deletes the volume, so the next `make up` backfills fresh
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Port already allocated | Another worktree's stack is up. Set `POSTGRES_HOST_PORT` / `APP_HOST_PORT` in `.env`, or `make down` in the other checkout. |
| `auth error: malformed API key` in the logs | `GRAPH_API_KEY` is empty or wrong in `.env`. Get one at <https://thegraph.com/studio/apikeys/>. |
| `/status` returns 503 | Postgres is unreachable. `docker compose ps` and `docker compose logs postgres`. |
| App healthy but no rows | Normal for the first ~20s. After that, read `make logs-worker` — per-token errors are logged with the symbol, and `make runs` shows the run status. |
| `"data_fresh": false` in /status | No successful run in the last three poll intervals. `make runs` shows why. |
| Worker never starts | It waits for the API healthcheck. Fix the API first. |
| Migration errors on start | A volume from an older schema. `make down` (which drops it) then `make up`. |
| `timescaledb extension is installed on this server but not preloaded` | The migration preflight. The volume was created by plain `postgres:17`, so `initdb` never ran under the TimescaleDB image. Compose forces the preload, so on compose this means the server is not the compose one; elsewhere add `shared_preload_libraries = 'timescaledb'` and restart. `make down` then `make up` recreates the volume (it also carries a Debian collation version the Alpine image cannot verify). |
| `could not access file "timescaledb"`, or postgres dies on `CREATE EXTENSION` with `must be preloaded` | The same condition reached without the preflight (an image built before it). `make down` then `make up`. |
| `the database is stamped at revision <hash>, which does not exist in this checkout` (or Alembic's `Can't locate revision identified by <hash>`) | Two causes, and the message lists both. **(1)** Another worktree whose directory shares this one's basename is using the same Compose project, so you are looking at its database. `make down` would delete their data — run yours isolated: `COMPOSE_PROJECT_NAME=mine POSTGRES_HOST_PORT=5433 APP_HOST_PORT=8001 UI_HOST_PORT=8502 make up`. **(2)** A draft migration was applied to this database and renamed or rewritten before it was committed (check `git log --all -S <hash>`: no hit means it never existed in git). No committed revision can reach that schema; `make down` then `make up`. Never change a revision id after it has been applied anywhere. |

Never add `name:` to `docker-compose.yaml`: the project name is left to default
to the directory so several worktrees can run at once.
