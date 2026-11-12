# Rollout: harness pre-work — one runner under the three loops

**Status:** current — opened 2026-11-11. The design it executes is
[`../harness/runner/DESIGN.md`](../harness/runner/DESIGN.md), Status
`proposed`; this file sequences it and records how each step actually
went. Decisions live in the design document; a step that forces a new
decision changes that document in the same change, Status line included.
**Why this and not stage 2 itself:** stage 2 (the harness) is large, and a
third of it already exists twice — a scheduler builder, a never-raise
tick, a process loop and a ledger each written once for ingestion and
once for the agents, with different vocabularies. Unifying them first is
the only candidate on the previous `ROLLOUT.md` that gets *harder* once
the harness lands on top; the other three (the four oversized dashboard
pages, the portfolio posterior, continuous aggregates) are unchanged by
this work and stay queued.
**Operating procedure per step:** the `add-migration` skill for step 2
and 3's schema changes, `run-stack` to verify against a real compose
stack, `release-check` before a step is called done.

## Progress

| # | Step | Size | Depends on | Status |
|---|---|---|---|---|
| 1 | One scheduler, one process loop, one `run_job` | S | — | todo |
| 2 | One shape of a run: two-phase agent rows, `trigger`, `outcome`, `parent_run_id`, the `run_log` view, stages in ingest | M | 1 | todo |
| 3 | The third job proves the registry: `fetch_news` scheduled, the generic `run` table | S | 1, 2 | todo — **cut line**; stage 2 absorbs it if dropped |
| 4 | Closeout: invariant 27, docs, skills, README; archive this file | S | 1–3 | todo |

**Why this order.** Step 1 moves control flow and changes no schema;
step 2 changes a schema and moves ownership of a ledger row and changes
no control flow. Keeping them apart means a failing test in either
session points at one kind of change. Step 3 exists to *prove* steps 1
and 2 rather than to deliver news on a schedule: a job that is neither
ingestion nor an agent, registered with no new scheduler code and no new
ledger code, is the only evidence that the abstraction is one and not
two-and-a-half. Step 4 is the paperwork, and it is a step because the
definition of done in `AGENTS.md` is a checklist, not a feeling.

**Combining steps in one session.** 3 and 4 may share a session. **Do
not combine 1 and 2**, for the bisection reason above, and do not start
2 in a session that has less than its full budget: it is the step with a
migration, and a migration id is permanent the moment it has been applied
to any volume (`docs/incidents/2026-10-28-stack-would-not-start/`).

**Sizing warning for step 2.** It is the largest step and the one most
likely to overrun, because "one shape of a run" is three deliverables
that look like one: the schema, the view and the `runs --job` command;
the ingest stages; and moving the `agent_run` row from the `ledger` sink
into the harness open/close. If the session runs long, **land the first
two and leave the third**: keep `LedgerSink` as the single-phase writer
of `agent_run`, record in the Outcome that agent rows are not yet
two-phase, and finish the move at the start of step 3's session. The
design document names this fallback and why it is the worse resting
place (a hung observer stays invisible).

**What is deliberately not in this rollout.** OpenTelemetry, YAML
pipeline specs, an event broker, continuous aggregates, a rename of any
compose service or interval setting, `fill`/`embed` as scheduled jobs,
and the four oversized dashboard pages. The first four are stage 2; the
pages should be split *next*, before stage 2 adds a pipelines dashboard,
so the new page copies the good pattern; the jobs wait for a demonstrated
need, which the registry turns into a twenty-line addition when it
comes.

The exit criterion for the whole rollout is in the design document and
every clause has to hold at once.

---

## Step 1 — One scheduler, one process loop, one `run_job`

**Goal.** After this step there is one `BackgroundScheduler` builder, one
process loop and one function through which every job runs, and nothing
observable about the stack has changed: the same three processes, the
same compose file, the same intervals, the same log lines, the same
ledger rows.

**Read first.** `docs/harness/runner/DESIGN.md` — "The decision", "The
vocabulary" (`Job`, `JobContext`, `RunOutcome`) and "`run_job`: the
lifecycle". `candlegraph/ingestion/worker.py` and
`candlegraph/agents/scheduler.py` side by side: they are the two copies
being folded. `candlegraph/api/app.py::lifespan` for the deliberate
`shutdown(wait=False)`. Invariants 1 and 7.

**Touches.** New `candlegraph/harness/` with `jobs.py` (the `Job`
dataclass, `JOBS` with `ingest`, `observer`, `janitor`, `select(role |
names)`), `runs.py` (`Run`, `Outcome`, `RunOutcome`, `run_job`),
`hooks.py` (`RunHook`, `LogHook`, a composite that isolates failures,
and ledger hooks that *wrap the existing writers*: `ingestion.ledger
.start_run`/`finish_run` for ingest; a no-op for agents until step 2
because `LedgerSink` still writes their row), `scheduler.py`
(`build_scheduler(settings, jobs, job_override=…)`), `process.py`
(`run_process(settings, jobs, stop, shutdown_wait=True)`).
`ingestion/worker.py` and `agents/scheduler.py` are deleted and their
tests ported to `tests/unit/harness/` — port, not rewrite: every
assertion they make today (job ids, interval, `next_run_time` set,
`max_instances=1`, `coalesce=True`, injected job, spy-scheduler
lifecycle, database closed, "nothing to schedule" is a warning) is still
true and still required. `api/app.py` imports the harness builder with
the `ingest` selection and `shutdown_wait=False`. `cli.py`: `worker` and
`agents` call `run_process` with their role (plus an optional `--jobs`);
`ingest`, `backfill`, `observe` and `janitor` call `run_job` with
`manual`/`backfill` triggers; a new `jobs` subcommand lists the registry
and gets a `make jobs` target (the Makefile-coverage test will demand
it). `ui/viewmodels.py::run_observer_now` and the `observe` MCP tool
call `run_job` with triggers `ui` and `mcp`; the value is carried in
memory this step and stored from step 2. `.env.example` is unchanged.

**Watch for.**

- **Invariant 1.** `harness/jobs.py` must import `run_observer`,
  `run_janitor` and `run_ingest` lazily inside the job bodies, as both
  ticks do today. A registry that imports the observer at module load
  pulls LangGraph into `candlegraph serve`.
- **The two shutdowns differ on purpose.** The worker waits so an
  in-flight upsert batch finishes; the API does not, so uvicorn's
  shutdown is never held by a backfill. Both are tested today; both stay
  reachable through `run_process`'s argument, with a comment saying why.
- **`JobContext.params` carries what the ticks passed positionally**:
  `lookback_days` for a backfill, `open_pr`/`checks`/`shell` for the
  janitor, `sinks` for either agent. Do not widen the `Job.run`
  signature to accommodate one job; put it in `params`.
- **`run_job` never raises, and the CLI still exits non-zero.**
  `Outcome.FAILED` → exit 1 for `ingest`; the agents keep `--strict`'s
  rule (exit 1 at WARNING or worse, or on error).
- **`test_every_analytics_command_has_a_make_target`** excludes services
  by name; `worker` and `agents` stay excluded, `jobs` is interactive
  and needs its target.
- **Do not touch compose or the Dockerfile.** If a change here seems to
  need one, stop and re-read the two 2026-10-29 post-mortems first.

**Done when.** `grep -rn BackgroundScheduler candlegraph/` finds one
file; `make up` comes up identically and `test_compose_stack.py` is
untouched; `candlegraph jobs` prints three rows with schedule setting,
role and write surface; the ported tests pass; `make check` is green
with no new skips; a stack run shows the same ledger rows and log lines
as before the step.

**Outcome.** _Not started._

---

## Step 2 — One shape of a run

**Goal.** An ingest run and an agent run become the same kind of record
with the same two status axes, readable together; an agent run becomes
visible while it is running; every run says why it ran; every run has
stage records.

**Read first.** `docs/harness/runner/DESIGN.md` — "`Run`, and the two
axes of status", "Where the agent ledger row moves", "Stages everywhere".
`docs/storage/table-placement/DESIGN.md` (the ledgers are plain and this
adds columns and a view, no table). The `add-migration` skill, every
rule. `candlegraph/agents/stages.py` and `observer/graph.py::publish_node`
for how stage records reach the ledger today. Invariant 11.

**Touches.** One migration: `agent_run` gains `trigger TEXT NULL`,
`outcome TEXT NOT NULL DEFAULT 'succeeded'` (back-filled: rows with
`status = 'error'` get `failed`), `parent_run_id UUID NULL`, and
`finished_at` becomes nullable; `ingest_run` gains `parent_run_id UUID
NULL`; `CREATE VIEW run_log` exactly as the design document gives it.
The downgrade drops the view and the columns and restores `finished_at
NOT NULL` with `COALESCE(finished_at, started_at)`. `storage/statements.py`:
`open_agent_run`, `close_agent_run`, `get_run_log`. `agents/ledger.py`
becomes the open/close pair the harness hook calls; `agents/reporting.py`'s
`LedgerSink` becomes a name that is accepted and logged once as
redundant; `observer/graph.py` and `janitor/graph.py` take the `Run`
through their deps and stop inserting in `publish`. `agents/stages.py`
moves to `harness/stages.py` with the old path re-exporting;
`ingestion/runner.py::ingest_once` is expressed as four `staged` steps
(`token_metadata`, `token_hours`, `pool_discovery`, `pool_hours`) with
`hours_by_symbol` as the detail of the second, and `ingest --progress`
prints them. `cli.py`: `runs --job NAME` and `runs --all` read the view;
bare `runs` keeps today's ingest output. `mcp/payloads.py`: the
agent-run payload carries `trigger` and `outcome`. `utils/logging_utils.py`:
the `run=` filter. `.env.example`: the `ledger` comment. `tests/integration/test_migrations.py`
or `test_agents.py`: the populated round-trip and the interrupted-run
test.

**Watch for.**

- **Pre-migration rows do not get a guessed trigger.** `trigger` is
  `NULL` for every row written before this step. Invariant 11 is about
  market data, but the reason behind it — a fabricated value is
  indistinguishable from a real one later — applies to a ledger just as
  well.
- **The view pins a column list.** Step 3's migration will have to
  `DROP VIEW` and recreate it with a third branch. Say so in a comment
  on the view's DDL, so the next migration author is not surprised.
- **`status` keeps its meaning on both tables.** Readers of
  `agent_run.status` (the dashboard, `make agent-runs`, three MCP tools,
  the e2e suite) must see exactly what they see today. `outcome` is the
  new column; `status` is not reinterpreted.
- **The interrupted-run test is the point of the step.** Drive the
  observer graph with a `FakeDatabase` whose `collect_quality` raises
  something `run_observer`'s own handler does not catch is *not* the
  case — `run_observer` catches everything and publishes an error report,
  so the row closes as `failed`. The `running` row survives only an
  interruption *of the process*; prove it in the integration tier by
  opening a run, not closing it, and asserting `run_log` shows it, and
  prove in the unit tier that `open` precedes the first node and `close`
  follows the last.
- **`AgentReport.error` maps to `outcome = failed` and `verdict = NULL`.**
  Not to `verdict = error`: that is the conflation the step exists to
  remove.
- **Round-trip the migration on populated tables**, both of them, with
  at least one `error`-status agent row and one `running` ingest row, so
  the back-fill and the `COALESCE` are exercised.
- **Cut line** (see the sizing warning above): the migration, the view,
  `runs --job`, the stages and the log filter land; the ownership move
  may wait for step 3's session. If it does, say so in the Outcome and
  leave `LedgerSink` writing single-phase rows with `outcome` derived
  from `report.error`.

**Done when.** `make runs ARGS='--job observer'` lists observer runs
with `trigger` and `outcome`; an observer run started from the UI button
shows `trigger = ui`; `candlegraph ingest --progress` prints four stage
lines in the same format as `observe --progress`, and the Agents
dashboard's stage strip renders for an ingest run; a run opened and
never closed is visible in `run_log` as `running`; the migration
round-trips on populated tables; `make check` is green with no new
skips.

**Outcome.** _Not started._

---

## Step 3 — The third job, and the generic `run` table

**Goal.** Register a job that is neither ingestion nor an agent — the
daily news fetch — using nothing but the registry, and give jobs without
a ledger of their own the generic `run` table the design reserves for
them. Disabled by default; no stack spends a credit because of this
step.

**Read first.** `docs/knowledge/tavily/DESIGN.md` — the `(asset, UTC
day)` cache and the budget guard are what make a scheduled fetch safe to
restart; `docs/knowledge/sources/DESIGN.md` §"Adoption order" (the RSS
source is next and will be the second such job);
`docs/storage/table-placement/DESIGN.md` — the `run` table's placement
is recorded there as plain, *before* this migration is written.

**Touches.** `harness/jobs.py`: `fetch_news` with role `agents`, writes
`{knowledge, ledger}`, schedule from `FETCH_NEWS_INTERVAL` (default `0`).
`knowledge/news.py::run_news_fetch` returns a `RunOutcome` whose counts
are `searched`, `cached`, `refused`, `errored`, `documents_stored`, and
whose outcome is `partial` on a per-token error, `failed` on a rate-limit
halt, `succeeded` otherwise — a refusal is the guard working and is
never a failure. One migration: `run` (plain; `run_id`, `job`, `trigger`,
`outcome`, `started_at`, `finished_at`, `duration_seconds`, `counts
JSONB`, `errors JSONB`, `detail JSONB`, `parent_run_id`), the view
dropped and recreated with its third branch, `test_the_ledger_and_state
_tables_are_not_hypertables` extended. `cli.py knowledge fetch-news`
runs through `run_job`. `settings.py` and `.env.example`: the new
interval with a comment that names the budget. `README.md` commands
table. Compose: nothing.

**Watch for.**

- **First firing is immediate, and the container restarts.** Every
  restart of the `agents` service would run the fetch at once; the
  `(asset, day)` cache makes the second run of a day free, so this is
  bounded by design, but say so in the setting's comment rather than
  relying on a reader knowing it.
- **`Every(86400)` is enough.** The cache key is the UTC day, so the
  time of day does not matter; do not add a `Daily(at=…)` schedule type
  for a job that does not need it.
- **`writes` must not claim `market_data`**, and the registry test must
  fail if it ever does.
- **`fill` is not registered here**, deliberately: its cache is
  recomputed on demand and nothing has shown it needs a clock. Record
  the omission in the Outcome so the next reader does not take it for
  an oversight.

**Done when.** `candlegraph jobs` lists four jobs; a stack with
`FETCH_NEWS_INTERVAL=86400` on the `agents` service produces one
`run_log` row per UTC day with counts; a budget refusal is a `succeeded`
run with `refused` in its counts; `make fetch-news` still works by hand
and records a `manual` run; the budget guard's tests are unchanged;
`make check` is green with no new skips.

**Outcome.** _Not started._

---

## Step 4 — Closeout

**Goal.** Make every document, invariant and skill tell the truth about
what steps 1–3 built, then archive this plan.

**Touches.** `AGENTS.md`: invariant 27 ("Every run is a `Run`", worded as
in the design document), the `harness/` entry in the layout, `make jobs`
and `make runs ARGS=` in the commands table, the "In progress" paragraph,
`run_log` in the data model as a view. `README.md`: "How it works" (one
runner, three processes), the commands table, the Agents section's
ledger sentence. `docs/README.md`: this design's Status to `implemented`
and the rollout row. `docs/harness/runner/DESIGN.md`: Status
`implemented` with **what was not built named** — in particular whether
the agent rows are two-phase, and whether step 3 landed.
`docs/roadmap/DESIGN.md` §2: the pre-work paragraph updated. Skills:
`add-agent` step 4 (register in `harness/jobs.py`, not
`agents/scheduler.py`), `run-agents` (`make runs ARGS='--job observer'`,
the `ledger` sink note), `run-stack` (what `make jobs` shows). Then this
file moves to `docs/rollout/archive/2026-11-11-harness-prework.md`
and a new `ROLLOUT.md` says what is current: nothing, with the four
oversized pages recommended as the next small item and stage 2 proper as
the next stage, opened by writing `docs/harness/pipelines/DESIGN.md`
before any code.

**Done when.** The `release-check` skill runs clean against a real
stack; every Status line under `docs/` is true; `git grep
agents/scheduler.py` finds nothing outside the archive.

**Outcome.** _Not started._

---

## Standing rules for every step

- `make check` green before a step is called done; integration skips are
  not green (check for them). Where a step changes a schema, the
  round-trip test runs on *populated* tables.
- **Compose, the Dockerfile and `entrypoint.sh` are not touched** in
  this rollout. The two 2026-10-29 post-mortems are the reason; a step
  that seems to need a compose change has misunderstood the design.
- **No new dependency.** APScheduler, LangGraph and psycopg2 are what
  there is; `opentelemetry-api` stays unused until stage 2.
- **No setting is renamed.** The registry maps jobs to the names that
  exist.
- `agent_run.status` and `ingest_run.status` keep their current meanings
  for every reader; new meaning goes in new columns.
- The job bodies — `ingest_once`, the observer graph, the janitor graph —
  are not the subject of this work. A step that finds itself editing
  `assess.py` or `runner.py`'s cursor logic has drifted.
- Several tests exist to pin a past bug and say so in their docstring. A
  ported test keeps its docstring.
- Update the progress table above at the end of each session, including
  "partial" with a note, so the next session starts from the truth.
