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
- Write a real `downgrade`. It is tested by `make test-integration`, which
  applies the chain to a fresh database.
- If you add a constraint, **de-duplicate existing rows first** in the same
  revision — see `8c1d47f2b3a0`, which does exactly that before tightening the
  natural key.
- New columns on a populated table must be nullable, or have a server default.

## 3. Apply and verify

```sh
make up                       # the entrypoint runs `alembic upgrade head`
make test-integration         # applies the chain to a throwaway database
```

Check the result directly:

```sh
docker compose exec -T postgres psql -U candlegraph -d candlegraph -c '\d+ candlegraph.token_hours_data'
```

Confirm the downgrade works before merging:

```sh
make migrate                                    # to head
uv run alembic downgrade -1 && make migrate     # round-trip
```

## 4. Follow through

Changing the schema almost always means changing `candlegraph/dba.py` — the
statements there name their columns explicitly, including the `VALUES`
templates used for batch upserts. Update:

- the statement and its `_TEMPLATE` in `dba.py`,
- `prepare_hour_rows` in `helpers.py` if the write path gains a field,
- `tests/integration/test_database.py`, which asserts the live schema.
