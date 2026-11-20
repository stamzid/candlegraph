---
name: run-pipelines
description: Operate candlegraph's pipelines — run one, watch the queue and the dispatcher, read a run and its steps, and work out why a pipeline did not run. Use when asked about a pipeline, the work queue, a dispatcher, a run that is missing or stuck, the Pipelines dashboard, or the prune job.
---

# Run and watch pipelines

A **pipeline** is a YAML file naming registered jobs in order. It is an
ordinary run with its steps as child runs, and it **never starts itself** —
something inserts a row in `run_queue` and the dispatcher, in the `agents`
service, claims it (invariant 28). To add or change one, use
`add-pipeline`; this skill is about running and reading them.

Three ship: `hourly` (validate → refresh_aggregates → analyse → signal →
explain), `daily` (fetch news → embed — **spends search credits**), and
`example` (one observer step).

## Look first

```sh
make queue                      # what a trigger asked for, and what became of it
make runs ARGS='--job hourly'   # the pipeline with its steps indented under it
make runs ARGS='--all'          # every job together, newest first
make check-pipelines            # validate every committed spec, running nothing
```

The **Pipelines dashboard** shows the same thing without a terminal: each
run with a strip of its steps, the queue with every row's dedup key, the
specs as the loader read them, and a **Run now** button per spec.

## Run one

```sh
make run ARGS='hourly'                  # by name (PIPELINES_DIR) or by path
make run ARGS='pipelines/hourly.yaml'
```

This runs in the foreground and takes the pipeline's advisory lock, so the
dispatcher will not run the same pipeline beside it. From the UI, **Run
now** instead *queues* a row with `trigger=ui`; the dispatcher performs it
within one `EVENT_POLL_INTERVAL` (60 s), and the page re-reads itself until
that run finishes. Two clicks in the same minute are one run — the dedup
key is `ui:<minute>`.

**`daily` spends money.** One Tavily credit per active token per UTC day,
cached per `(token, day)`. Its UI button states the cost first; from the
CLI there is no confirmation, so mean it.

## Why a pipeline did not run

Work down this list; each step is one command.

| What you see | What it means |
|---|---|
| **No queue row at all** | No trigger matched. `hourly` watches ingest (`once_per: hour`), so twelve polls in an hour produce one run; its 2 h schedule is only a safety net. Check `make runs` for a successful ingest |
| **Row stays `queued`** | Nothing is claiming it. Is the `agents` service up (`make ps`, `make logs-agents`)? Is `EVENT_POLL_INTERVAL=0`? The dashboard warns after three intervals |
| **Row `claimed`, no run** | A dispatcher holds the lease (`QUEUE_LEASE_SECONDS`, 900 s). If its process died, the lease expires and another tick reclaims it |
| **Row `dead`** | It ran out of claims (`QUEUE_MAX_CLAIMS`). Read `last_error` in `make queue`. Dead rows need a person and are never pruned, so they stay until someone acts (backlog `A7`) |
| **Run `failed` at `validate`** | The gate did its job: a token with no rows, or an impossible row. On a cold start, before the first ingest, this is expected |
| **Run `partial`** | It finished, but a step did not do everything. Coverage below the threshold is `partial`, not failure — a thin token legitimately has gaps (invariant 19) |
| **A spec is missing from the page / never runs** | The loader refused it. `make check-pipelines` prints the file and line |
| **No `dispatch` runs in `run_log`** | Normal. `dispatch` is a *quiet* job: it records a tick only when it did something, and always when it failed |

## Traces

```sh
make otel-up      # the stack plus a collector (opt-in overlay; a plain `make up` runs none)
make otel-logs    # the spans, as the collector received them
```

One trace per run, one span per step, one event per stage. Without the SDK
the instrumentation is still compiled in and costs nothing.

## Retries, deadlines, and what is not retried

- Only a step that **raised** is retried, and only when
  `harness/errors.py::classify` calls the error transient. A step that
  *returned* `failed` graded itself and is not re-run.
- Every attempt is its own child run, so retries are visible in `run_log`.
- The pipeline deadline is enforced **between** steps; per-step timeouts
  are cooperative (the bodies check between tokens). `observer` and
  `fetch_news` do not check, and run on.

## History, and what deletes it

The daily `prune` job deletes `run` rows older than `RUN_RETENTION_DAYS`
(30), a **whole family at a time**, and keeps any family with a member
still `running`. `ingest_run` and `agent_run` are never pruned. A `prune`
row saying `deleted 0` is the normal day. There is no `prune` command: to
force one, restart the `agents` container (its scheduler fires each job
once at start-up).

## Rules worth not breaking

- **A pipeline reacts to ingest and never runs it.** The loader refuses any
  step whose job writes `market_data`; one process writes market data.
- **Only a job proved idempotent may be a step** — the flag fails closed,
  and a run-twice test has to exist for it.
- **The UI asks, it never executes.** No Streamlit code may call
  `run_pipeline` or take the advisory lock.
- A spec is refused **whole**, with the line, before any step runs
  (invariant 29).

## Known gaps

Before concluding something is broken, check
[`docs/backlog/DESIGN.md`](../../../docs/backlog/DESIGN.md) — `A1`–`A10`
cover pipelines, including the queued run that cannot be withdrawn (`A3`)
and the stuck `running` family nothing closes (`A5`), and its "Accepted
behaviours" table lists what looks wrong and is not.
