# Rollout: stage 2, the harness — and what comes just before it

**Status:** current — planned 2026-11-15; steps 1–3 done, step 4 next.
**Designs it follows:** [`../harness/runner/DESIGN.md`](../harness/runner/DESIGN.md)
(built), and [`../harness/pipelines/DESIGN.md`](../harness/pipelines/DESIGN.md)
(written in step 2 of this plan, before any pipeline code; partly built
from step 3).
The previous plan, the harness pre-work, is in
[`archive/2026-11-11-harness-prework.md`](archive/2026-11-11-harness-prework.md).

## What this plan covers

The last closeout listed four candidates. This plan puts all four in
order, and folds two of them into stage 2 where they belong:

| Candidate | Where it lands |
|---|---|
| 1. Split the four large dashboard pages | **Step 1**, one session |
| 2. Stage 2, the harness | **Steps 2–7** |
| 3. A scheduled `embed` job | **Step 4**, as a pipeline step after the news fetch, not a job on its own timer |
| 4. Continuous aggregates | **Step 6**, because the harness is what refreshes them |
| 4. The portfolio posterior | **Not here.** It is model work and belongs with stage 4 |

Two designs written this session are **not** in this plan's critical
path. They are separate, smaller rollouts that can go after it (or
between steps, if needed sooner):

- [`../harness/models/DESIGN.md`](../harness/models/DESIGN.md) — one seam
  for chat models (Claude, OpenAI, open-source).
- [`../knowledge/budget-manager/DESIGN.md`](../knowledge/budget-manager/DESIGN.md) —
  credit pools for automatic and manual news fetches.

## Progress

| # | Step | Size | Depends on | Status |
|---|---|---|---|---|
| 1 | Split the four large dashboard pages; draw the ingest stage strip | S | — | done 2026-11-15 |
| 2 | Write the pipelines design (no code) | S | — | done 2026-11-16 — reviewed, five questions answered |
| 3 | Pipeline spec, loader and `candlegraph run` for one step | M | 2 | done 2026-11-17 |
| 4 | The hourly pipeline end to end: validate, analyse, signal, embed as jobs | M | 3 | todo |
| 5 | Events: run finished → next thing starts | M | 3 | todo |
| 6 | Traces (OpenTelemetry) and continuous aggregates | M | 4 | todo |
| 7 | Closeout | S | 1–6 | todo |

**Why this order.**

- Step 1 first because it is small, independent, and stage 2 will add a
  pipelines page. That page should copy the split pattern, not the long
  functions.
- Step 2 before any code because a pipeline spec is a public format.
  Once people write YAML against it, changing it costs every file.
- Step 3 proves one pipeline with one step runs through `run_job` and
  records a parent run. Everything after builds on that.
- Step 4 is the exit criterion's main clause.
- Step 5 and 6 are independent of each other; 6 needs 4's runs to have
  something worth tracing.

**One step per session**, except 1 and 2 may share a session (they touch
different files). Steps 3 and 4 both change the run model, so keep them
apart: a failing test then points at one change.

---

## Step 1 — Split the four large dashboard pages

**Goal.** `_news_page` (160 lines), `_agents_page` (142), `_strategy_page`
(157) and `_portfolio_page` (141) become small panel functions, like
`_quality_page` and `_simulation_page` already are. Nothing on screen
changes, except one addition: the Agents page draws the stage strip for
ingest runs too.

**Read first.** `candlegraph/ui/app.py` `_quality_page` (the pattern to
copy); `ui/viewmodels.py::stages_frame` and `build_agents_view`.

**Changes.**

- Each page becomes a short function calling 3–5 panel functions. Panel
  functions take a view object and draw; they compute nothing.
- Any arithmetic found in the script moves to `viewmodels.py` with a
  test (the existing rule).
- `build_agents_view` reads the newest `ingest_run.detail.stages` as well,
  and the page draws it with the same strip as the observer's.

**Watch for.** Streamlit widget `key`s must not change, or saved
selections in an open tab reset. Buttons that spend or write stay behind
their confirmation.

**Done when.** No page function in `app.py` is over 60 lines; the
`AppTest` suite passes unchanged except for new tests of the ingest strip;
`make check` green.

**Outcome.** Done 2026-11-15.

- The four named pages are now 8–23 lines each, over panel functions:
  `_portfolio_*` (controls, metrics, allocation, frontier), `_strategy_*`
  (controls, metrics, charts, tables, ledger), `_agents_*` (button,
  metrics, news strip, findings, charts, history) and `_news_*` (metrics,
  runway, fetch panel, charts, tables).
- **Four more pages were over the 60-line criterion** than the goal
  named — `_risk_page` (98), `_data_page` (86), `_compare_page` (81) and
  `_forecast_page` (64). They were split too, so the criterion as written
  is met rather than quietly narrowed. Every page function is now ≤ 59
  lines; the longest remaining functions are panels (`_token_price_chart`,
  `_execution_panel`, `_estimator_panel`), which this step did not claim.
- **The ingest stage strip is drawn.** `AgentsView.ingest_stages` reads
  the newest ingest run that recorded stages — not simply the newest row,
  which is often a run still in progress whose stages are written when it
  closes. `_stage_strip` is one function used for both ledgers.
- Two small fixes found while splitting: the coverage chart was duplicated
  between the Agents and Data pages and is now `_coverage_chart`; and the
  stage detail column used its own formatter, so ingest's nested
  `by_symbol` printed as a raw Python dict. It now uses `describe()`, the
  same formatter `--progress` prints with.
- One bug caught by the tests while splitting: `_strategy_config` draws
  widgets, and the first split called it twice in one run, which
  Streamlit rejects as duplicate element ids. It is built once and passed
  down, with a comment saying why.
- Widget keys were not touched, so a tab open across the change keeps its
  selections.
- Verified on a real stack: `make up` healthy, the UI serving (HTTP 200),
  and the ingest strip showing the four real stages with readable detail.
  `make check` green (1694 passed, 11 e2e skips).

---

## Step 2 — Write the pipelines design

**Goal.** `docs/harness/pipelines/DESIGN.md`, status `proposed`. No code.

**It must decide:**

- **The spec format.** YAML, parsed into pydantic models like
  `settings.py` and `tokens.py`. A pipeline is a name, a trigger
  (schedule or event), and an ordered list of steps; each step names a
  registered job and its params.
- **Validation before running.** Unknown job, bad param, a step that
  writes `market_data` from the wrong role: all rejected at load time,
  with the line number.
- **How a pipeline run owns its steps.** One parent row in the generic
  `run` table; each step is a child run with `parent_run_id`.
- **What happens when a step fails.** Stop, skip, or continue — per step,
  default stop. A `partial` step continues by default.
- **Where pipelines run.** The `agents` process (it already hosts
  non-ingest jobs), with the worker still owning ingest. Decide whether a
  pipeline can call `ingest` or only react to it.
- **LangGraph or a plain loop.** The roadmap says pipelines are LangGraph
  graphs. The design should say what that buys over a for-loop for a
  linear pipeline, and keep the for-loop if the answer is "nothing yet".

**Done when.** The design is written, linked from `docs/README.md`, and
reviewed by you before step 3 starts.

**Outcome.** Written 2026-11-15: [`../harness/pipelines/DESIGN.md`](../harness/pipelines/DESIGN.md),
status `proposed`. What it decided, against the list above:

- **Spec:** YAML → pydantic, the whole file rejected with a line number
  on any error; `candlegraph run --check` in CI.
- **Validation:** unknown keys and jobs, params checked against each
  job's declared param model, no `market_data` writer, no non-idempotent
  job, no trigger loops.
- **Ownership:** a parent row in `run`, each step attempt a child row.
- **Failure:** `failed` stops by default, `partial` continues; retries
  only for transient errors, with jittered backoff; each attempt its own row.
- **Where:** pipelines **react to** ingest and never run it. Local: the
  executor lives in the `agents` process (no compose change). Production:
  2–3 stateless executor replicas claiming from a Postgres queue.
- **LangGraph or loop:** a plain loop; the roadmap's line is updated.

Added beyond the list, because you asked for fault tolerance and a
production view: a durable queue with `SKIP LOCKED`, leases and resume
after a crash, a session advisory lock per pipeline, a transactional
outbox for events, circuit breakers, hard timeouts in production, a
local-vs-production section, SLOs and metrics, and the model picker (paid
providers selectable only when their key is in the environment).
Streaming platforms (Redpanda, NATS JetStream) are evaluated with the
signals that would justify adding one; none is proposed now.

Reviewed 2026-11-16 and all five questions answered in §16 of the
design: a 15-minute SLO after ingest; `explain` runs by default but only
on stored news, with search left to a person or the daily job; the model
choice is **per step** (role as the default behind it), with the context
memory it needs designed in `../harness/models/DESIGN.md`; a single VM is
the production target; and `run` history is kept one month, via a new
`prune` job.

Step 2 also triggered a whole-repository review before any pipeline code:
[`../audit/2026-11-16-system-review/FINDINGS.md`](../audit/2026-11-16-system-review/FINDINGS.md).
Seven bugs were fixed in that pass; the open items that affect this rollout
are no authentication and no backups (both need your decision), and the
`prune` job answer 5 now requires.

---

## Step 3 — Spec, loader, and `candlegraph run`

**Goal.** `candlegraph run pipelines/example.yaml` runs a one-step
pipeline through `run_job` and records a parent run and a child run.

**Changes.**

- `candlegraph/harness/pipelines.py`: the pydantic spec, the loader, and
  `run_pipeline(spec, trigger)`.
- `pipelines/` directory at the repo root with one example spec.
- CLI `run` and `make run ARGS=...`; `candlegraph run --check FILE`
  validates without running.
- `run_log` shows the pipeline row with its children.

**Watch for.** `run_pipeline` must never raise either (same rule as
`run_job`). A child run must carry the parent's trigger.

**Done when.** The example runs; `runs --job` for the pipeline shows its
children; a bad spec is refused with a readable message and a line
number; `make check` green.

**Outcome.** Done 2026-11-17.

- `candlegraph/harness/pipelines.py` holds the pydantic spec, the loader
  and the executor. `pipelines/example.yaml` is one `observer` step;
  `candlegraph run` takes a path or a name (`PIPELINES_DIR`), `--check`
  validates and runs nothing, `make run` executes it in the stack and
  `make check-pipelines` is the CI gate. No migration: the pipeline's
  name, version, as-of time and step records go in `run.detail`.
- **A pipeline run is an ordinary run.** A spec is turned into a `Job` and
  handed to `run_job`, so the parent row, the `run=` log prefix, the stage
  fan-out and the never-raises rule all come from code that already
  existed. Each step is another `run_job` call carrying `parent_run_id`
  and the *parent's* trigger. The only new vocabulary is `Every.NEVER`,
  for a job built from a file rather than registered.
- **Each step is also a stage**, so a pipeline run draws with the stage
  strip step 1 built, and step 4's page is panels over helpers that
  exist. `StageStatus` gained `partial`: a step that half-worked must not
  display as `OK`.
- **Every rule in §5.2 is refused with a file and a line.** `yaml.compose`
  keeps the marks `safe_load` throws away, so the line map costs one
  extra parse and no new dependency. All problems are reported at once —
  one `--check` is enough to see every mistake.
- **`Job` gained `idempotent` and `params`, both failing closed.** A job
  is a step only once a run-twice test proves it, and
  `IDEMPOTENCE_PROOFS` in `tests/unit/harness/test_jobs.py` fails if a
  job is marked without one. `observer` is declared and proved;
  `fetch_news` is idempotent in fact and waits for the step that needs it
  (step 4); `ingest` is deliberately left undeclared, because a pipeline
  reacts to it and never runs it.
- **Two rules the design named, narrowed on purpose.** The loop check
  refuses an event trigger naming *this* pipeline; a loop through a second
  pipeline needs every spec in hand, which is the dispatcher's job in step
  5. Retries and timeouts are parsed and carried but not applied — step 4
  owns error classification and backoff.
- **Scope changed against design §15**, which put the durable queue,
  leases and resume in this step: they moved to step 5, where the event
  dispatcher is their first real producer. A claim/lease/reaper with only
  a foreground `candlegraph run` behind it would be ~400 lines of
  concurrency with nothing to prove it against, and this plan's own rule
  is to keep steps 3 and 4 apart. §15 of the design is updated to say so.
- Three small traps found while building it: pydantic drops a step that
  fails validation and then also reports the list as empty, burying the
  real mistake — "no steps" is a semantic check instead; a model-level
  validator's error has no field location, so durations and `model_role`
  validate on the field or the line is wrong; and the observer's child row
  is written only when the configured sinks include `ledger`, which a test
  now pins, or a pipeline run would show a parent with no children.
- The Dockerfile gained one `COPY pipelines ./pipelines` — the one
  deliberate exception to this plan's "compose and the Dockerfile change
  only in a step that says so", recorded here. Specs are reviewed
  definitions that ship with the image, like the migrations beside them.
- **`runs --job <pipeline>` showed only the parent**, which fails this
  step's criterion as written. `_run_log` now fetches each page's child
  runs in one extra query and prints them indented under their parent,
  without double-listing a child that is already on the page. `--limit`
  still counts the runs a person asked for: a pipeline's steps are detail
  of a row, not more rows.
- Verified on a real stack as well as in CI: `make check` green (1778
  passed, 11 e2e skips, integration ran), `make run
  ARGS='pipelines/example.yaml'` succeeded against a live stack, and
  `make runs ARGS='--job example'` showed the pipeline with its observer
  step underneath it.

---

## Step 4 — The hourly pipeline end to end

**Goal.** `pipelines/hourly.yaml`: ingest finished → validate → analyse
→ signal; a daily variant adds fetch news → embed. This is the stage's
exit criterion.

**Changes.**

- New registered jobs, each a thin body over code that already exists:
  - `validate` — the quality report; `failed` if an active token is
    missing, `partial` if coverage is under the threshold.
  - `analyse` — rolling statistics for the active tokens (nothing stored;
    the run records what it computed).
  - `signal` — `signals --store` for each registered strategy.
  - `embed` — `embed_pending`, bounded per run. This closes the backlog
    gap flagged in the pre-work.
- Each job declares `writes` truthfully (`signal` writes `signals`, a new
  write surface; nothing but `ingest` writes `market_data`).

**Watch for.** `signal` must read only causal data
(`load_symbol_series`); a test runs the pipeline on data with a gap and
checks no filled hour reached a signal. A failed `validate` stops the
pipeline before `signal` by default.

**Done when.** One `candlegraph run pipelines/hourly.yaml` runs all
steps as one parent run with child runs; `candlegraph jobs` lists the
new jobs; `make check` green.

**Outcome.** Not started.

---

## Step 5 — Events

**Goal.** A pipeline can start when another run finishes ("when ingest
succeeds, run hourly"), instead of on its own clock.

**Changes.**

- An event hook (`RunHook.finished` → publish `{job, outcome, run_id}`).
- First version in-process. Second version Postgres `LISTEN/NOTIFY`, so
  the worker's ingest can wake a pipeline in the agents process. No new
  service.
- Trigger value `event` on runs started this way, with `parent_run_id`
  pointing at the run that caused it.

**Watch for.** Loops (a pipeline triggered by its own step). Refuse at
load time. A missed notification (process down) must not lose work: on
start-up, a pipeline checks whether it has run since the last matching
event.

**Done when.** With the stack up, an ingest run causes exactly one hourly
pipeline run, visible in `run_log` with trigger `event`; `make check`
green.

**Outcome.** Not started.

---

## Step 6 — Traces and continuous aggregates

**Goal.** Every run and stage is a trace span; daily and weekly candle
rollups are maintained by the database and refreshed by the harness.

**Changes.**

- A tracing hook using `opentelemetry-api` (already in `uv.lock`), behind
  an optional extra and an exporter setting; off by default.
- Continuous aggregates over `token_hours_data` (daily, weekly), and a
  `refresh_aggregates` job. Measure the Market dashboard's long windows
  before and after; if nothing was slow, record that and keep them
  anyway only if the measurement says so.
- `docs/storage/timescaledb/DESIGN.md` updated; the aggregates are views,
  not tables, but the placement document gets a line.

**Done when.** A pipeline run appears as one trace with child spans in a
local collector; the aggregate refresh is a recorded run; `make check`
green.

**Outcome.** Not started.

---

## Step 7 — Closeout

`AGENTS.md` (layout, commands, any new invariant — likely "a pipeline
spec is validated before it runs"), `README.md`, the roadmap's stage 2
marked delivered with its exit criterion checked, skills (`run-stack`,
a new `add-pipeline` skill), archive this file.

**Done when.** `release-check` passes against a real stack; every Status
line under `docs/` is true.

**Outcome.** Not started.

---

## Rules for every step

- `make check` green before a step is done; integration skips are not
  green. Schema changes round-trip on tables with rows.
- Compose, the Dockerfile and `entrypoint.sh` change only in a step that
  says so (none of these do except possibly step 6's collector, which
  should be a separate opt-in compose file).
- No new dependency without saying why in the step's Outcome.
- Invariants 1–27 stand. If a step seems to need breaking one, stop and
  write down why first.
- Update the progress table at the end of every session.
