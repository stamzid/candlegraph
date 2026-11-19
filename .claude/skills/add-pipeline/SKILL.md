---
name: add-pipeline
description: Add a candlegraph pipeline spec (pipelines/*.yaml), or make a job usable as a pipeline step — registry entry, param model, idempotence proof, triggers — without breaking the loader's rules. Use when asked to add a pipeline, chain jobs together, run something after ingest, add a step, or put a job on an event or schedule.
---

# Add a pipeline, or a step

Read `docs/harness/pipelines/DESIGN.md` §5 (the spec and its rules) and
§6.2 (the contract every step meets) first. `pipelines/hourly.yaml` is the
reference spec; its comments explain each choice. Invariants 21, 27, 28 and
29 are the ones a pipeline can break.

## Before anything: does it need to be a pipeline?

A pipeline orders **registered jobs that already exist**. If the work is
one job on a timer, it is a `Job` with an `Every("<NAME>_INTERVAL")`
schedule (the `add-agent` skill covers the pattern), not a pipeline. If
the work needs a new job, do part B first.

A pipeline **never ingests**. It reacts to ingest with an event trigger.
The loader refuses any step whose job writes `market_data`.

## A. A new spec from existing steps

1. Write `pipelines/<name>.yaml`. The name must not be a registered job's
   name. Minimal shape:

   ```yaml
   name: <name>
   description: One sentence a person reads in `make runs`.
   version: 1
   triggers:                       # omit for "only when a person asks"
     - event: { job: ingest, outcome: [succeeded, partial], once_per: hour }
     - schedule: { every: 2h }     # the safety net, not a second trigger
   deadline: 15m                   # must be >= the sum of step timeouts
   defaults:
     retry: { attempts: 3, backoff: 30s, max_backoff: 2m }
     timeout: 3m
   steps:
     - id: validate
       job: validate
       on_failure: stop            # default; `continue` or `skip_rest` to change
     - id: signal
       job: signal
       params: { strategies: all }
       on_partial: continue        # default
   ```

2. Choose `once_per` deliberately. Ingest succeeds every five minutes;
   `run` would start twelve pipelines an hour. `hour` is almost always
   right for work per bar; `day` for daily work.
3. Decide what a failure does, per step. `validate`-like gates use
   `stop`; prose and other optional output uses `continue` (invariant 22:
   prose is optional).
4. A spec that spends money (`fetch_news`) says so in its header comment,
   and does not get a trigger without the user's say-so — see
   `pipelines/daily.yaml` and the budget-manager design.
5. Validate, then run it in the stack:

   ```sh
   make check-pipelines                   # every committed spec; also catches a loop between two specs
   make up
   make run ARGS='pipelines/<name>.yaml'  # or: make run ARGS='<name>'
   make runs ARGS='--job <name>'          # the pipeline, its steps indented under it
   make queue                             # if a trigger was meant to start it and did not
   ```

   `test_every_committed_spec_is_valid` picks the new file up on its own.

## B. Make a job usable as a step

A job is a step only once it is **proven** safe to re-run; every flag
fails closed.

1. **The body** goes in `candlegraph/harness/steps.py` if it is an adapter
   over code that exists — no arithmetic, no SQL of its own (invariant
   18: the numbers stay in `analytics/`, the SQL in `storage/statements.py`).
   Loop over tokens with `_within_deadline(ctx, tokens)` so the timeout is
   honoured between tokens, and return a `RunOutcome` with counts. Grade
   yourself: `partial` for a step that half-worked, `failed` when nothing
   usable happened. **Return** `failed` for a decided failure; **raise**
   only for something another attempt might fix — only a raised error is
   retried, and `harness/errors.py::classify` decides if it is transient.
2. **The params model** goes in `candlegraph/harness/params.py` (no domain
   imports there; the registry imports it). Reuse `SymbolsMixin` /
   `StrategiesMixin`. Every field has a default and a bound. Do not add a
   param that turns spending on — `explain` has no `search` param on
   purpose.
3. **The registry entry** in `candlegraph/harness/jobs.py`:
   `schedule=NEVER` for a step-only job, a truthful `writes` (never
   `market_data`), `ledger=run_ledger`, `params=<Model>`,
   `idempotent=True`.
4. **The proof**: a test named `test_running_<job>_twice_writes_the_same_state`
   (unit with `FakeDatabase` if it can prove it; integration if the claim
   is about real SQL, as `refresh_aggregates`'s is), and an entry in
   `IDEMPOTENCE_PROOFS` in `tests/unit/harness/test_jobs.py`. The registry
   test fails if either is missing.
5. If it reads prices for a strategy, it reads through
   `load_symbol_series` only — a test should show no filled hour reaches
   it (see the `signal` step's tests).

## Check

```sh
make jobs              # the new job shows "pipeline step only"
make test-unit
make check             # with the integration tier running, not skipped
```

## Finish

- `docs/harness/pipelines/DESIGN.md` if a rule or a default changed; its
  Status line if something listed as not built now is.
- `AGENTS.md`: the layout line for `harness/jobs.py` names the step jobs;
  the commands table if a target was added.
- `README.md` "How it works" lists the shipped pipelines and whether each
  spends money.
- `docs/overview/DESIGN.md` if the hour's life or who-writes-what changed.
- Then the `release-check` skill.
