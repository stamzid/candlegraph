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

`make up` fails if either container never reaches healthy. The app waits for
Postgres's healthcheck, then applies migrations, then serves.

## Verify

```sh
curl -s localhost:8000/status | python3 -m json.tool
```

Expect `"status": "ok"`, `"database": "up"`, and the configured token symbols.

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
| App healthy but no rows | Normal for the first ~20s. After that, read `make logs` — per-token errors are logged with the symbol. |
| Migration errors on start | A volume from an older schema. `make down` (which drops it) then `make up`. |

Never add `name:` to `docker-compose.yaml`: the project name is left to default
to the directory so several worktrees can run at once.
