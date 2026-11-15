# Rollout: stage 2, the harness — and what comes just before it

**Status:** current — planned 2026-11-15, not started.
**Designs it follows:** [`../harness/runner/DESIGN.md`](../harness/runner/DESIGN.md)
(built), and `../harness/pipelines/DESIGN.md` (written in step 2 of this
plan, before any pipeline code).
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
| 1 | Split the four large dashboard pages; draw the ingest stage strip | S | — | todo |
| 2 | Write the pipelines design (no code) | S | — | todo |
| 3 | Pipeline spec, loader and `candlegraph run` for one step | M | 2 | todo |
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

**Outcome.** Not started.

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

**Outcome.** Not started.

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

**Outcome.** Not started.

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
