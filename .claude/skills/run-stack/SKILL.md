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

`make up` fails if any container never reaches healthy. Health probes run
every three seconds while a container is starting and every three minutes
once it is healthy, so `make up` is still quick and the steady-state log is
not a probe every ten seconds. A healthy `GET /status` is not logged at all;
if you need to see the probes, set `LOG_HEALTH_PROBES=true`. The services
come up in order: `postgres` (TimescaleDB, not plain PostgreSQL), then `candlegraph`
(the API, which applies the migrations), then `worker` (which ingests), `ui`,
and `agents`. The worker waits for the API to be *healthy* so two containers
never race on the same DDL.

## Verify

```sh
curl -s localhost:8000/status | python3 -m json.tool
```

Expect `"status": "ok"`, `"database": "up"`, and the configured token symbols.

See what the processes are running and on which interval:

```sh
make jobs
```

Eleven rows. Five are scheduled: `ingest` (role `worker`,
`DATA_POLL_INTERVAL`), `observer` and `janitor` (role `agents`; the
janitor is `0`, disabled, because the container has no git checkout),
`fetch_news` (`FETCH_NEWS_INTERVAL`, `0` by default because it spends
search credits) and `dispatch` (`EVENT_POLL_INTERVAL`, every minute).
The other six — `validate`, `analyse`, `signal`, `explain`,
`refresh_aggregates`, `embed` — say **pipeline step only**: they run when
a pipeline orders them, never on a clock. A job shown as disabled is not
an error, and neither is a step-only one.

After a minute, `make runs ARGS='--all'` should show an `ingest` and an
`observer` row, both with trigger `scheduled`. Within a minute of the
first successful ingest, the dispatcher starts the hourly pipeline:

```sh
make runs ARGS='--job hourly'   # one run, trigger `event`, its steps indented under it
make queue                      # its queue row, `done`
```

On a brand-new volume the first `hourly` run may **fail at `validate`**
because the safety-net schedule fired before any data existed. That is
expected and documented; the next ingest starts a real run. Exactly one
`dispatch` row per tick that did something is right — a quiet tick
writes no row at all (invariant 27's one exception).

To see the same runs as traces, start the stack with the collector
overlay instead: `make otel-up`, then `make otel-logs`.

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

## Stop, and start over

```sh
make down    # also deletes the volume, so the next `make up` backfills fresh
make reset   # down -v, delete this project's image, up again — the fix for a bad volume
```

`make up` prints the API container's last log lines when the stack does not
become healthy, and names `make reset` if the message is about a migration
or a volume. Do not reach for `docker system prune`: it deletes every other
project's state to fix one volume. Note what `docker compose up -d --build
--force-recreate` does **not** do: it recreates containers, never volumes,
so a volume at an unknown revision fails the same way afterwards.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Port already allocated | Another worktree's stack is up. Set `POSTGRES_HOST_PORT` / `APP_HOST_PORT` in `.env`, or `make down` in the other checkout. |
| `auth error: malformed API key` in the logs | `GRAPH_API_KEY` is empty or wrong in `.env`. Get one at <https://thegraph.com/studio/apikeys/>. |
| `/status` returns 503 | Postgres is unreachable. `docker compose ps` and `docker compose logs postgres`. The 503 *is* in `make logs` even though healthy probes are not. |
| `make logs` shows no `GET /status` lines | Expected. Healthy probes are filtered; see `docs/incidents/2026-10-29-health-probe-cadence/`. |
| A container shows `unhealthy` long after start | Steady-state probes are three minutes apart with three retries, so "unhealthy" means ~9 minutes of failures, not a blip. `docker inspect <container> --format '{{json .State.Health}}'` has the probe history. |
| App healthy but no rows | Normal for the first ~20s. After that, read `make logs-worker` — per-token errors are logged with the symbol, and `make runs` shows the run status. |
| `"data_fresh": false` in /status | No successful run in the last three poll intervals. `make runs` shows why. |
| Worker never starts | It waits for the API healthcheck. Fix the API first. |
| API container `Exited (1)` and not restarting | By design since `restart: on-failure:5`: its entrypoint failed five times in a row, which means the fault is deterministic (almost always the volume). `make up` already printed the log; `make reset` is the remedy. |
| `--force-recreate` "did nothing" | It recreates containers, not volumes, and before 10-29 could run another worktree's image because every checkout built `candlegraph:latest`. Images are now `candlegraph:<project>`; for a volume problem use `make reset`. |
| Migration errors on start | A volume from an older schema. `make down` (which drops it) then `make up`. |
| `timescaledb extension is installed on this server but not preloaded` | The migration preflight. The volume was created by plain `postgres:17`, so `initdb` never ran under the TimescaleDB image. Compose forces the preload, so on compose this means the server is not the compose one; elsewhere add `shared_preload_libraries = 'timescaledb'` and restart. `make down` then `make up` recreates the volume (it also carries a Debian collation version the Alpine image cannot verify). |
| `could not access file "timescaledb"`, or postgres dies on `CREATE EXTENSION` with `must be preloaded` | The same condition reached without the preflight (an image built before it). `make down` then `make up`. |
| `the database is stamped at revision <hash>, which does not exist in this checkout` (or Alembic's `Can't locate revision identified by <hash>`) | Two causes, and the message lists both. **(1)** Another worktree whose directory shares this one's basename is using the same Compose project, so you are looking at its database. `make down` would delete their data — run yours isolated: `COMPOSE_PROJECT_NAME=mine POSTGRES_HOST_PORT=5433 APP_HOST_PORT=8001 UI_HOST_PORT=8502 make up`. **(2)** A draft migration was applied to this database and renamed or rewritten before it was committed (check `git log --all -S <hash>`: no hit means it never existed in git). No committed revision can reach that schema; `make down` then `make up`. Never change a revision id after it has been applied anywhere. |

Never add `name:` to `docker-compose.yaml`: the project name is left to default
to the directory so several worktrees can run at once.

## Reading the stack's state as tools

`make mcp` serves the same reads this skill describes — `status`,
`recent_ingest_runs`, `data_quality_report`, `latest_agent_report` — over
MCP on stdio, pointed at the stack's database on `localhost`. It needs
`uv sync --extra mcp` once. Nothing in the listing writes market data, and
the one tool that can spend a search credit (`explain_signal`) does so
only when asked. It is a convenience over the commands above, not a
replacement: when something is actually broken, `make up`'s own output and
`make logs` say more than any tool result.
