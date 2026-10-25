---
name: backfill-and-verify
description: Backfill historical candlegraph data over a longer window and verify what landed with the data-quality report. Use when asked to backfill, load history, fill gaps, or check data coverage and quality.
---

# Backfill and verify

## 1. Run it

```sh
make up                     # the stack must be healthy first
make backfill days=90       # ~5 minutes for ten tokens
```

`backfill` differs from a normal poll in one way that matters: it **rewinds**.
A poll resumes from the newest stored hour and only walks forwards, so on a
database that already holds recent data a poll can never fetch older history.
An explicit `--days` window reaches back past what is stored.

It is safe to re-run. Writes upsert on the natural key, so a second identical
backfill reports the same row counts and changes nothing.

## 2. Verify

```sh
make report
```

Read it in this order:

1. **`MISSING (no rows at all)`** — a configured token ingested nothing.
   Always a real problem: check the address against the subgraph
   (`.claude/skills/add-token`), and check `make runs` for an error.
2. **`IMPOSSIBLE ROWS`** — `high < low`, or open/close outside the bar. Always
   a real problem, in our transform or upstream.
3. **`BELOW n% COVERAGE`** — needs judgement, see below.
4. **field completeness** — if a column is well under 100%, the subgraph query
   probably stopped asking for that field.

```sh
make runs                   # status, counts, duration per run
```

Statuses are `succeeded`, `partial` (some targets failed, some data written),
`failed`, and `running`.

## 3. Interpreting coverage

**Low coverage is often correct.** Uniswap v3 emits no `tokenHourData` for an
hour with no swaps, so a thin token is genuinely sparse — GNO sits near 47%
and that is the real market, not a bug. Before treating low coverage as a
fault, check whether the token is simply illiquid:

```sh
docker compose exec -T postgres psql -U candlegraph -d candlegraph -c \
  "SELECT symbol, AVG(volume_usd)::numeric(18,2) AS avg_hourly_volume_usd
   FROM candlegraph.token_hours_data GROUP BY symbol ORDER BY 2;"
```

A token with a tiny average hourly volume and scattered one- or two-hour gaps
is behaving normally. Tune `MIN_EXPECTED_COVERAGE` rather than chasing it.

What *is* suspicious: a long contiguous gap (many hours) on a liquid token, or
coverage that drops between runs. Those point at a failed run — check
`make runs` and the worker log.

## 4. Gate it in CI

```sh
docker compose exec -T worker python -m candlegraph report --strict
```

`--strict` exits non-zero when a token is missing, coverage is below the
threshold, or impossible rows exist.
