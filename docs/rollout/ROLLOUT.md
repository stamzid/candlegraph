# Rollout: harness pre-work — one runner for all scheduled work

**Status:** current — opened 2026-11-11.
**Design:** [`../harness/runner/DESIGN.md`](../harness/runner/DESIGN.md).
This file lists the steps, their order and how each one went. Decisions
belong in the design document; if a step changes a decision, it updates
that document in the same change.

## Why this work comes first

Three processes run work on a timer: the ingest worker, the agents
process, and the API (when `INGEST_ENABLED=true`). Before this rollout,
the code for scheduling, running and recording that work was written
twice, once for ingestion and once for the agents, with different names
and different ledger columns.

Stage 2 (the harness: pipelines, events, tracing) needs to run all of
these jobs in one way. Merging the two copies now is cheaper than
building stage 2 on top of both. The other candidates for the next
piece of work (splitting four large dashboard pages, the portfolio
posterior, continuous aggregates) do not get harder if they wait, so they
stay queued.

**Procedures:** the `add-migration` skill for schema changes (steps 2
and 3), `run-stack` to check a real stack, `release-check` before a step
is called done.

## Progress

| # | Step | Size | Depends on | Status |
|---|---|---|---|---|
| 1 | One scheduler, one process loop, one `run_job` | S | — | done 2026-11-12 |
| 2 | One shape of a run: ledger rows opened before and closed after every run, `trigger`, `outcome`, `parent_run_id`, the `run_log` view, stages for ingest | M | 1 | done 2026-11-13 — see Outcome |
| 3 | A third job (`fetch_news`) and the generic `run` table | S | 1, 2 | todo — optional; stage 2 can absorb it |
| 4 | Closeout: invariant 27, docs, skills, README; archive this file | S | 1–3 | todo |

**Why this order.** Step 1 changes control flow and no schema. Step 2
changes the schema and who writes the ledger rows, but not control flow.
Keeping them apart means a failing test points at one kind of change.
Step 3 tests the design: if a job that is neither ingestion nor an agent
can be added without new scheduler or ledger code, the design works.
Step 4 is the paperwork required by the definition of done in
`AGENTS.md`.

**Do not combine steps 1 and 2 in one session.** Steps 3 and 4 may share
one. Do not start step 2 without time to finish it: it adds a migration,
and a migration id is permanent once any database has applied it (see
`docs/incidents/2026-10-28-stack-would-not-start/`).

**Not in this rollout:** OpenTelemetry, YAML pipeline specs, an event
broker, continuous aggregates, renaming any compose service or interval
setting, scheduling `fill` or `embed`, and the four large dashboard
pages. The first four belong to stage 2. The dashboard pages should be
split before stage 2 adds a pipelines page.

---

## Step 1 — One scheduler, one process loop, one `run_job`

**Goal.** One scheduler builder, one process loop and one function that
runs every job. Nothing visible changes: same processes, same compose
file, same intervals, same log lines, same ledger rows.

**Read first.** The design document's sections "The decision", "The
vocabulary" and "`run_job`". The old `ingestion/worker.py` and
`agents/scheduler.py`. `api/app.py::lifespan`. Invariants 1 and 7.

**Changes.**

- New package `candlegraph/harness/`: `jobs.py` (the `Job` registry
  `JOBS` with `ingest`, `observer`, `janitor`, and `select`), `runs.py`
  (`Run`, `Outcome`, `RunOutcome`, `run_job`), `hooks.py` (`RunHook`,
  `LogHook`, `CompositeHook`), `scheduler.py` (`build_scheduler`),
  `process.py` (`run_process`).
- Delete `ingestion/worker.py` and `agents/scheduler.py`; move their
  tests to `tests/unit/harness/` and keep every assertion.
- `api/app.py` uses the harness builder with only the `ingest` job.
- CLI: `worker` and `agents` call `run_process` and accept `--jobs`;
  `ingest`, `backfill`, `observe` and `janitor` call `run_job`; new
  `jobs` command and `make jobs` target.
- The UI's "Run observer now" button and the `observe` MCP tool call
  `run_job` with triggers `ui` and `mcp`.

**Watch for.**

- Import job bodies inside the job functions, not at module load, or
  `candlegraph serve` would load LangGraph (invariant 1).
- The worker waits for running jobs on shutdown so an upsert batch
  finishes; the API does not, so uvicorn can stop quickly. Keep both.
- Job-specific inputs go in `JobContext.params`, not in the `Job.run`
  signature.
- `run_job` never raises, but the CLI still exits non-zero on failure.
- Do not touch compose or the Dockerfile.

**Done when.** `grep -rn BackgroundScheduler candlegraph/` finds one
file; `make up` behaves as before; `candlegraph jobs` prints three rows;
the moved tests pass; `make check` is green with no new skips.

**Outcome.** Done 2026-11-12. The harness package exists as described;
the two old modules are deleted and their tests moved. `make check`
passed (the only skips were the e2e tests), and a stack run showed the
same start-up lines and ledger rows as before.

Two things were left for step 2:

1. The `ingest_run` row was still written by `ingestion.runner.run_ingest`,
   not by a hook. `run_ingest` was given the harness's `run_id` so the
   two agreed.
2. The stage callback (`on_stage`) was only passed to a job when the
   caller asked for `--progress`, because a CLI test required it.

---

## Step 2 — One shape of a run

**Goal.** Ingest runs and agent runs are recorded the same way:

- the ledger row is written when the run starts and updated when it
  ends, so a run that hangs or is killed is visible as `running`;
- every row says why it ran (`trigger`) and whether it completed
  (`outcome`), separately from what an agent found (`status`);
- both tables can be read together through one view, `run_log`;
- ingest runs record stages, like agent runs already do.

**Read first.** The design document's sections "`Run` and the two kinds
of status", "Who writes the ledger rows" and "Stages for every job".
`docs/storage/table-placement/DESIGN.md` (this step adds columns and a
view, not a table). The `add-migration` skill. Invariant 11.

**Changes.**

- **Migration.** `agent_run` gets `trigger TEXT NULL`, `outcome TEXT NOT
  NULL DEFAULT 'succeeded'` (existing rows with `status = 'error'` get
  `failed`) and `parent_run_id UUID NULL`; `finished_at` and `status`
  become nullable, because a row now exists while the run is in
  progress. `ingest_run` gets `parent_run_id UUID NULL`. New view
  `run_log`. The downgrade drops the view and the columns, closes any
  unfinished agent row as `error` (`finished_at = started_at`), and
  restores `NOT NULL`.
- **Ledger hooks.** `harness/hooks.py` gets `IngestLedgerHook` and
  `AgentLedgerHook`. They open the row in `started` and close it in
  `finished`. `run_ingest` and the `ledger` report sink no longer write
  rows themselves.
- **Ingest stages.** `ingest_once` records four stages:
  `token_metadata`, `token_hours`, `pool_discovery`, `pool_hours`.
  `candlegraph ingest --progress` prints them.
- **Stages always reach the hooks.** `run_job` always passes a stage
  callback to the job (step 1's second leftover).
- **Readers.** `candlegraph runs --job NAME` and `runs --all` read
  `run_log`; plain `runs` still prints ingest runs. The agent-run MCP
  payload includes `trigger` and `outcome`. Existing readers of
  `agent_run` only see finished rows, so they see what they saw before.
- **Logging.** Every log line written during a run ends with
  `run=<first 8 characters of run_id>`.

**Watch for.**

- Rows written before this migration get `trigger = NULL`. Do not guess
  a value.
- The view lists its columns explicitly. Step 3 will have to drop and
  recreate it; the migration says so in a comment.
- `agent_run.status` and `ingest_run.status` keep their meaning for every
  reader. New meaning goes in new columns.
- A crashed agent graph is `outcome = failed` with no verdict, not a
  verdict of `error`. (`status` stays `error` on that row, as before.)
- Test the migration round trip on tables that have rows, including an
  `error` agent row and a `running` ingest row.

**Done when.** `make runs ARGS='--job observer'` lists observer runs with
`trigger` and `outcome`; an observer run started from the UI shows
`trigger = ui`; `candlegraph ingest --progress` prints four stage lines
in the same format as `observe --progress`; a run opened and never closed
shows in `run_log` as `running`; the migration round-trips on populated
tables; `make check` is green with no new skips.

**Outcome.** Done 2026-11-13, including both items left over from step 1.

- Migration `be8aa030e200`: the columns and the view as planned.
  `agent_run.status` also became nullable (it is `NULL` while a run is in
  progress). The downgrade closes unfinished agent rows as `error` rather
  than deleting them.
- `IngestLedgerHook` and `AgentLedgerHook` write every ledger row.
  `run_ingest` is now a thin wrapper around `run_job` that re-raises when
  the run could not start, so its existing callers and tests behave as
  before. `LedgerSink.post` does nothing.
- **One change from the design's first draft:** an agent row is written
  only when the run's sinks include `ledger` (from `--sinks`, or
  `AGENT_REPORT_SINKS` when none are passed). The janitor in GitHub
  Actions has no database and runs with `log,file`; this keeps it from
  trying to connect. The design document records the reason.
- `close_agent_run` is an upsert, so a failed open still ends with a row.
- `StageRecord` moved to `harness/stages.py` (old path re-exports it), with
  a new `timed_stage` helper. `ingest_once` records four stages, and
  `ingest`/`backfill --progress` print them.
- `runs --job NAME` and `runs --all` read `run_log`; `make runs` accepts
  `ARGS`. The `agent_runs` MCP tool returns `trigger` and `outcome`.
  `get_recent_agent_runs` returns finished rows only.
- Log lines written during a run end with `run=<8 chars>`.
- The observer job also accepts `registry` and `now` in its params (used
  by the integration test, which now runs the observer through `run_job`).
- Tests: new `tests/unit/harness/test_ledger_hooks.py`; stage tests in
  `test_runner.py`; CLI tests for `runs --job/--all` and `--progress`;
  integration tests for `run_log`, a never-closed run, and the migration
  round trip on populated ledgers. Tests that expected the graphs or the
  sink to insert `agent_run` were changed to expect the harness's open and
  close instead.
- `make check`: 1667 passed, 11 skipped (the e2e tests, which pass against
  a running stack: 11/11). On a real stack: `runs --job observer` shows
  `trigger` and `outcome`, a UI-started run shows `trigger = ui`, a row
  from before the migration shows trigger `-`, and worker log lines carry
  the same `run=` id as the ledger row.

**Not done in this step:** the Agents dashboard does not yet show an
ingest run's stage strip (the data is in `ingest_run.detail.stages`; the
page only reads `agent_run`). That is UI work for step 4 or the dashboard
split.

---

## Step 3 — A third job and the generic `run` table

**Goal.** Register the daily news fetch as a job using only the
registry, and add a generic `run` table for jobs that have no ledger of
their own. Disabled by default, so no stack spends search credits
because of this step.

**Read first.** `docs/knowledge/tavily/DESIGN.md` (the per-asset, per-day
cache and the budget guard make a scheduled fetch safe to repeat);
`docs/knowledge/sources/DESIGN.md` §"Adoption order";
`docs/storage/table-placement/DESIGN.md` (record the `run` table as a
plain table before writing the migration).

**Changes.**

- `harness/jobs.py`: a `fetch_news` job, role `agents`, writes
  `{knowledge, ledger}`, interval `FETCH_NEWS_INTERVAL` (default `0`).
- `knowledge/news.py::run_news_fetch` returns a `RunOutcome` with counts
  `searched`, `cached`, `refused`, `errored`, `documents_stored`.
  Outcome: `partial` if a token failed, `failed` if rate-limited,
  otherwise `succeeded`. A budget refusal is the guard working, not a
  failure.
- Migration: the `run` table (plain), and `run_log` recreated with a
  third branch; extend `test_the_ledger_and_state_tables_are_not_hypertables`.
- `candlegraph knowledge fetch-news` runs through `run_job`.
- `settings.py` and `.env.example`: the new interval, with a comment
  about the budget. README commands table.

**Watch for.**

- The first firing is immediate and the container may restart, so a
  restart runs the fetch again. The daily cache makes the second run
  free; say so in the setting's comment.
- `Every(86400)` is enough; do not add a time-of-day schedule.
- `writes` must not include `market_data`; the registry test enforces it.
- `fill` is not scheduled here on purpose. Say so in the Outcome.

**Done when.** `candlegraph jobs` lists four jobs; with
`FETCH_NEWS_INTERVAL=86400` the stack writes one `run_log` row per UTC
day; a budget refusal is a `succeeded` run with `refused` counted;
`make fetch-news` still works and records a `manual` run; `make check`
is green.

**Outcome.** Not started.

---

## Step 4 — Closeout

**Goal.** Make every document, invariant and skill match what steps 1–3
built, then archive this file.

**Changes.**

- `AGENTS.md`: invariant 27 (wording in the design document), the
  harness layout entry, `make jobs` and `make runs ARGS=` in the
  commands table, the "In progress" paragraph, `run_log` in the data
  model.
- `README.md`: "How it works", the commands table, the Agents section.
- `docs/README.md` and `docs/harness/runner/DESIGN.md`: Status
  `implemented`, naming anything that was not built.
- `docs/roadmap/DESIGN.md` §2: the pre-work paragraph.
- Skills: `add-agent`, `run-agents`, `run-stack`.
- Move this file to `docs/rollout/archive/2026-11-11-harness-prework.md`
  and write a new `ROLLOUT.md` saying there is no current rollout. Next
  recommended: split the four large dashboard pages, then stage 2,
  starting with `docs/harness/pipelines/DESIGN.md`.

**Done when.** `release-check` passes against a real stack; every Status
line under `docs/` is true; `git grep agents/scheduler.py` finds nothing
outside the archive.

**Outcome.** Not started.

---

## Rules for every step

- `make check` green before a step is done. Integration-test skips do
  not count as green. Schema changes are round-tripped on tables with
  rows.
- Do not change compose, the Dockerfile or `entrypoint.sh`. See the two
  2026-10-29 post-mortems.
- No new dependencies.
- No settings are renamed.
- `agent_run.status` and `ingest_run.status` keep their current meaning.
- The job bodies (`ingest_once`'s cursor logic, the observer and janitor
  graphs, `assess.py`) are not what this work is about. If a step is
  editing them, it has drifted.
- A test that pins a past bug keeps its docstring when it is moved.
- Update the progress table at the end of every session, including
  "partial" with a note.
