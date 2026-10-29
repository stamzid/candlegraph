# TimescaleDB: adoption, migration, and what we deliberately did not turn on

**Status:** implemented — 2026-10-25, preflight added 2026-10-29
**Area:** `docker-compose.yaml`, `.github/workflows/ci.yml`,
`tests/integration/conftest.py`, `alembic/versions/`, `candlegraph/storage/statements.py`
**Companion:** [`../table-placement/DESIGN.md`](../table-placement/DESIGN.md) —
which tables are hypertables and why; this document is the *how*.

## What was there before

candlegraph stored hourly market data — time-series data by any definition —
in plain PostgreSQL 17. No code referenced `timescaledb`, `time_bucket`,
hypertables, continuous aggregates or compression. The one time-series-shaped
statement was `chart_query`, which bucketed with
`FLOOR(period_start_unix / interval) * interval` and re-aggregated on every
request.

That was a defensible first choice: at ten tokens and one row per hour,
PostgreSQL answers everything from a B-tree without noticing. Nothing was
slow. The reason to move was not today's query latency.

## Why we moved now

Three forces, in order of how soon they bite:

1. **The data only grows.** Invariant 4 says ingestion never deletes.
   `pool_hours_data` at `POOLS_PER_TOKEN=3` is the largest table within a
   year, and the index on an ever-growing table is the thing that eventually
   stops fitting in cache.
2. **The next phases read windows, not points.** The run observer already
   compares a 24-hour window against a 7-day baseline for every token on a
   15-minute schedule. The harness wants pre-aggregated candles rather than
   a `GROUP BY` per GraphQL request.
3. **Migrating is cheapest now.** The project is not deployed; the only data
   anywhere is test and local-development data. Converting a populated
   hypertable later is strictly more work than converting an empty one now,
   and the conversion changes primary keys — not something to do under load.

TimescaleDB is an extension, not a different database. Every invariant,
statement and test in the repo keeps working, which is what made this a
contained change rather than a rewrite.

## What was implemented

```mermaid
flowchart TD
  subgraph img["1 · Image, pinned in all three places"]
    I1["docker-compose.yaml"]
    I2[".github/workflows/ci.yml"]
    I3["tests/integration/conftest.py"]
    I1 & I2 & I3 --> IT["timescale/timescaledb:2.31.0-pg17"]
  end

  subgraph mig["2 · One Alembic revision, e4b9c7a05f13"]
    M1["CREATE EXTENSION timescaledb"]
    M2["PK -> (id, period_start_unix)<br/>on the three series tables"]
    M3["create_hypertable(migrate_data => true)<br/>monthly chunks"]
    M4["unix_now() + set_integer_now_func<br/>so future policies can run"]
    M1 --> M2 --> M3 --> M4
  end

  subgraph sql["3 · Statements"]
    S1["chart_query: FLOOR arithmetic -> time_bucket"]
    S2["missing_hours: left as generate_series"]
  end

  IT --> M1
  M4 --> S1

  classDef done fill:#e8f5e9,stroke:#2e7d32
  class IT,M1,M2,M3,M4,S1 done
```

### 1. The image

`postgres:17` becomes **`timescale/timescaledb:2.31.0-pg17`** in compose, in
the CI service container, and in the testcontainer. All three are pinned to
an exact version, for the reason the `Dockerfile` already records in a
comment: an unpinned image is how this build broke before. `latest-pg17`
would have been a rolling tag.

The image is PostgreSQL 17 with an init script that, **on `initdb` only**,
writes `shared_preload_libraries = 'timescaledb'` into `postgresql.conf` and
creates the extension in the default database. An existing `pgdata` volume
does **not** inherit any of that: the server reuses the data directory as it
finds it, the preload stays empty, and `CREATE EXTENSION timescaledb` is a
backend `FATAL` rather than an error. That is how the stack went down on
2026-10-28 (see
[`docs/incidents/2026-10-28-stack-would-not-start/`](../../incidents/2026-10-28-stack-would-not-start/POSTMORTEM.md)).
Two things now make the preload a property of the service rather than of the
volume's history: compose starts the server with
`-c shared_preload_libraries=timescaledb`, and `alembic/env.py` runs a
preflight that refuses to migrate a server that is not preloading it, with a
message naming the cause. The image is also Alpine where `postgres:17` was
Debian, so a volume carried across records a glibc collation version the
musl server cannot honour; that is one more reason an old volume is rebuilt,
not migrated.

The extension is now **required**, not optional. The migration raises a
clear error if it is missing *or not preloaded* rather than silently leaving
plain tables, because a schema that differs between a developer's database
and CI's is worse than a failure that says what to do. "Available" and
"preloaded" are checked separately: `pg_available_extensions` lists the
library the moment it is on disk, which is exactly the state of an old volume
under the new image.

### 2. The migration

One revision, `e4b9c7a05f13`. For each of `token_hours_data`,
`pool_hours_data` and `signal`:

- Replace the primary key `(id)` with `(id, period_start_unix)`. A
  hypertable requires every unique index to contain the partitioning column.
  The natural-key unique constraints already do, so they are untouched and
  idempotent upserts (invariant 4) survive unchanged.
- `create_hypertable(…, 'period_start_unix', chunk_time_interval => 2592000,
  migrate_data => true)`.

Then a `unix_now()` function and `set_integer_now_func` for each table.

Four decisions inside that:

- **Partition on `period_start_unix`, not `timestamp`.** Every statement
  filters on `period_start_unix`; partitioning on the column the planner
  actually sees is what produces chunk exclusion. The two columns are
  redundant by construction, so nothing is lost. The cost is that an integer
  time dimension needs an explicit "now" function before any policy can run,
  which is what `set_integer_now_func` provides.
- **Monthly chunks** (`2592000` seconds). At one row per entity-hour this
  keeps the chunk count in the low hundreds for years, and keeps a typical
  analytics window to one or two chunks.
- **`migrate_data => true`**, so the revision works on a populated database
  rather than only a fresh one. Verified against a real local volume.
- **It all runs in one transaction.** Verified on 2.31: the primary-key
  swap, `create_hypertable` with `migrate_data`, and `set_integer_now_func`
  commit together, so a failure leaves the schema untouched rather than half
  converted.

### The downgrade

Converting a hypertable back is not something the extension offers, so the
`downgrade` rebuilds each table:

```sql
CREATE TABLE t__plain (LIKE t EXCLUDING ALL);   -- columns, types, NOT NULL
INSERT INTO t__plain SELECT * FROM t;
DROP TABLE t;                                    -- takes the hypertable and its chunks
ALTER TABLE t__plain RENAME TO t;
-- recreate the sequence the dropped table owned, then the named constraints
```

`EXCLUDING ALL` is deliberate: `LIKE` always copies column names, types and
`NOT NULL`, and excluding everything else means the primary key, the natural
key, the check constraints and the indexes are recreated **by their original
names** rather than inheriting generated ones. The `id` sequence is dropped
with the old table and recreated with `setval` at the current maximum, so the
round trip preserves both the rows and the next id.
`test_the_timescale_migration_round_trips` proves it on a populated database.

### 3. Statements

| Statement | Before | After | Why |
|---|---|---|---|
| `chart_query` | `FLOOR(period_start_unix::numeric / interval) * interval` | `time_bucket(interval, period_start_unix)` | Identical semantics — verified `bool_and(...)` equal across a real series — but it is the form a continuous aggregate can be defined on, and it says what it means. Invariant 5 (bucket labels are boundaries) is unaffected: both compute the boundary from the interval, never from `MIN(timestamp)`. |
| `missing_hours` | `generate_series` left-joined against the table | unchanged | `time_bucket_gapfill` is the TimescaleDB idiom, but it requires explicit bounds in the `WHERE` and exists to *emit* placeholder rows. Gap detection wants a set difference, which `generate_series` states more plainly. Invariant 6 also means nothing outside the quality report may gap-fill, so adopting the function here would invite exactly the wrong reuse. |

Nothing else changed. The upsert statements, the cursor queries and the
quality statements run unmodified against hypertables, which was the point of
choosing an extension over a different database.

## What we deliberately did not turn on

- **Compression.** Chunks older than `LOOKBACK_DAYS` are read-mostly, but
  `backfill` can rewrite any hour at any time (invariant 13). Writing into a
  compressed chunk works but decompresses first, and the upsert path is the
  one place a surprise is unacceptable. Not until there is a measured storage
  problem.
- **Retention policies.** Never. Invariant 4 says ingestion never deletes,
  and a retention policy is a scheduled `DELETE`.
- **Continuous aggregates.** The payoff — daily and weekly candles as an
  incrementally refreshed view instead of a per-request `GROUP BY` — belongs
  with the harness, which is the phase that has a consumer for them.
  `chart_query` was moved to `time_bucket` now precisely so that defining one
  later is a new object rather than a rewrite.

## Test plan, and what it proves

| Test | Proves |
|---|---|
| `test_the_series_tables_are_hypertables` | the three tables are chunked, partitioned on `period_start_unix` |
| `test_the_ledger_and_state_tables_are_not_hypertables` | the four plain tables stayed plain — the placement decision is enforced, not just documented |
| `test_a_hypertable_still_upserts_on_the_natural_key` | invariant 4 across chunk boundaries |
| `test_rows_spread_across_chunks_by_time` | partitioning actually partitions |
| `test_the_timescale_migration_round_trips` | `downgrade` then `upgrade` on a populated database, rows and sequence intact |
| `test_a_server_that_does_not_preload_timescaledb_is_refused_before_any_ddl` | the preflight stops a server with the library on disk but not preloaded, before the first revision runs |
| `test_a_version_table_stamped_with_an_unknown_revision_is_refused_with_a_cause` | a database at a revision this checkout lacks is refused with both causes named |
| `test_compose_forces_the_timescale_preload_on_every_volume` (unit) | the service, not the volume's history, decides the preload |
| the existing `test_database.py` suite, unchanged | candles, bucket boundaries, gaps, idempotency and "never deletes" all behave identically on hypertables |

The last row is the important one: the migration is correct to the extent
that the tests written before it still pass without modification.

## Rejected alternatives

- **pg_partman on plain PostgreSQL.** Chunking without the extension, but
  none of the time-series functions, and more to operate than one preloaded
  extension.
- **A separate time-series store (ClickHouse, InfluxDB).** Two databases to
  keep consistent for a dataset that fits in memory, when the ledger, the
  signals and the planned knowledge-graph entities all want to be joined to
  the prices in one place.
- **Making everything a hypertable.** See the companion document. Four of
  seven tables are better off plain, and `ingest_run` would actively regress.
- **Deferring to the harness phase.** The original proposal suggested this.
  It was overtaken by the observation that the project has no deployment and
  no production data, which makes now the cheapest moment this migration will
  ever have.
