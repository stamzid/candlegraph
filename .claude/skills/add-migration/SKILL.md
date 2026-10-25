---
name: add-migration
description: Create, apply, and verify an Alembic migration for candlegraph. Use when asked to change the database schema — add a column, table, index, or constraint.
---

# Add a migration

## 1. Create the revision

```sh
make revision m="add volume columns to token_hours_data"
```

This writes `alembic/versions/<rev>_<slug>.py` with the `down_revision`
already chained. There are no SQLAlchemy models yet, so `--autogenerate` has
nothing to diff against; write the operations by hand.

## 2. Write it

Follow the existing revisions:

```python
from candlegraph.settings import get_settings

SCHEMA = get_settings().db_schema  # never hardcode "candlegraph"


def upgrade() -> None:
    op.add_column("token_hours_data", sa.Column("volume_usd", sa.Numeric()), schema=SCHEMA)


def downgrade() -> None:
    op.drop_column("token_hours_data", "volume_usd", schema=SCHEMA)
```

Rules:

- Always pass `schema=SCHEMA`. A missing `schema=` silently targets `public`.
- **Know whether you are touching a hypertable.** `token_hours_data`,
  `pool_hours_data` and `signal` are chunked on `period_start_unix`;
  `token`, `pool`, `ingest_run` and `agent_run` are plain. Adding or
  dropping a column works the same on both. What differs:
  - **Any unique index or primary key on a hypertable must contain
    `period_start_unix`.** `op.create_unique_constraint("uix_x", "signal",
    ["symbol"])` is rejected outright; it has to be
    `["symbol", "period_start_unix"]`.
  - A plain index is fine and is created on every chunk automatically.
  - Do not add a compression or retention policy. Invariant 4 forbids the
    second outright; `docs/storage/timescaledb/DESIGN.md` says why the first
    waits for a measured problem.
- Write a real `downgrade`. It is tested by `make test-integration`, which
  applies the chain to a fresh database.
- If you add a constraint, **de-duplicate existing rows first** in the same
  revision — see `8c1d47f2b3a0`, which does exactly that before tightening the
  natural key.
- New columns on a populated table must be nullable, or have a server default.

**Adding a new table?** Decide its placement *before* writing the migration
and record it in `docs/storage/table-placement/DESIGN.md`. It becomes a
hypertable only if all four hold: a row is one measurement at an instant,
writes land on their own time coordinate (and any `UPDATE` carries it),
reads filter by time window, and nothing ever deletes from it. Miss one and
it stays an ordinary table — `ingest_run` is the worked example of a table
that looks like a series and is not.
`test_the_ledger_and_state_tables_are_not_hypertables` enforces the split,
so a new table means updating that test too.

## 3. Apply and verify

```sh
make up                       # the entrypoint runs `alembic upgrade head`
make test-integration         # applies the chain to a throwaway TimescaleDB
```

The throwaway database is `timescale/timescaledb:2.31.0-pg17`, pinned in
`tests/integration/conftest.py` to match compose and CI. A migration that
passes against plain PostgreSQL proves nothing about hypertables.

Check the result directly:

```sh
docker compose exec -T postgres psql -U candlegraph -d candlegraph -c '\d+ candlegraph.token_hours_data'
```

On a hypertable, `\d+` shows the parent. To see the chunks and confirm the
partitioning actually happened:

```sh
docker compose exec -T postgres psql -U candlegraph -d candlegraph -c \
  "SELECT hypertable_name, count(*) FROM timescaledb_information.chunks GROUP BY 1;"
```

Confirm the downgrade works before merging:

```sh
make migrate                                    # to head
uv run alembic downgrade -1 && make migrate     # round-trip
```

Round-trip it against a database **with rows in it**, not an empty one. The
TimescaleDB revision is the cautionary tale: un-chunking a table means
copying it into a new one, and the ways that goes wrong — losing a
constraint name, leaving the `id` sequence behind the highest row — are all
invisible when the table is empty. `test_the_timescale_migration_round_trips`
is the pattern to copy.

## 4. Follow through

Changing the schema almost always means changing `candlegraph/dba.py` — the
statements there name their columns explicitly, including the `VALUES`
templates used for batch upserts. Update:

- the statement and its `_TEMPLATE` in `dba.py`,
- `prepare_hour_rows` in `helpers.py` if the write path gains a field,
- `tests/integration/test_database.py`, which asserts the live schema.
