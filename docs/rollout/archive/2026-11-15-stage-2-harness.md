# Rollout: stage 2, the harness — and what comes just before it

**Status:** archived — planned 2026-11-15, closed 2026-11-19. All seven
steps done; one gate of step 7 (the release check against a real stack)
was not run in the closing session and is carried by
[`../ROLLOUT.md`](../ROLLOUT.md) — see step 7's Outcome.
**Designs it follows:** [`../../harness/runner/DESIGN.md`](../../harness/runner/DESIGN.md)
(built), and [`../../harness/pipelines/DESIGN.md`](../../harness/pipelines/DESIGN.md)
(written in step 2, built in steps 3–6).
The previous plan, the harness pre-work, is in
[`2026-11-11-harness-prework.md`](2026-11-11-harness-prework.md).

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

- [`../harness/models/DESIGN.md`](../../harness/models/DESIGN.md) — one seam
  for chat models (Claude, OpenAI, open-source).
- [`../knowledge/budget-manager/DESIGN.md`](../../knowledge/budget-manager/DESIGN.md) —
  credit pools for automatic and manual news fetches.

## Progress

| # | Step | Size | Depends on | Status |
|---|---|---|---|---|
| 1 | Split the four large dashboard pages; draw the ingest stage strip | S | — | done 2026-11-15 |
| 2 | Write the pipelines design (no code) | S | — | done 2026-11-16 — reviewed, five questions answered |
| 3 | Pipeline spec, loader and `candlegraph run` for one step | M | 2 | done 2026-11-17 |
| 4 | The hourly pipeline end to end: validate, analyse, signal, embed as jobs | M | 3 | done 2026-11-17 |
| 5 | Events: run finished → next thing starts | M | 3 | done 2026-11-18 |
| 6 | Traces (OpenTelemetry) and continuous aggregates | M | 4 | done 2026-11-18 |
| 7 | Closeout | S | 1–6 | done 2026-11-19 — documents; the stack run of `release-check` carried forward |

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

**Outcome.** Written 2026-11-15: [`../harness/pipelines/DESIGN.md`](../../harness/pipelines/DESIGN.md),
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
[`../audit/2026-11-16-system-review/FINDINGS.md`](../../audit/2026-11-16-system-review/FINDINGS.md).
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

**Outcome.** Done 2026-11-17.

- **Five jobs, not four.** `validate`, `analyse`, `signal` and `embed` as
  planned, plus `explain` — it is a thin body over `explain_symbol` and
  §16 answer 2 had already settled how it must behave. Their bodies are
  in `candlegraph/harness/steps.py`, which says why they live together:
  each is an adapter with no arithmetic and no SQL of its own, and the
  alternative was a `JobContext` inside `analytics/`, which only reads
  the database and only through `prices.py` (invariant 18).
- **`pipelines/hourly.yaml`** — validate → analyse → signal → explain,
  under a 15-minute deadline matching the SLO. **`pipelines/daily.yaml`**
  — fetch news → embed, the one pipeline that spends money. Ingest is a
  step of neither: the loader refuses a job that writes market data.
- **Retries and timeouts are applied, not just parsed.** Step 3 carried
  them and ignored them, which made `timeout: 2m` a quiet lie.
  `harness/errors.py::classify` decides in one place whether another
  attempt could help; backoff is exponential with full jitter; every
  attempt is its own child run, so retries are visible in `run_log`
  rather than hidden in one row.
- **Only a step that *raised* is retried.** A job that *returned* `failed`
  has graded itself — `fetch_news` does this for a rate limit it already
  backed off from — and re-running it would overrule a decision the job
  was better placed to make. An unknown exception type is **permanent**:
  a wrongly-permanent error fails once, visibly, while a wrongly-transient
  one burns the deadline three times and then fails anyway.
- **The deadline is enforced between steps; per-step timeouts are
  cooperative.** The five new bodies check their deadline between tokens;
  `observer` and `fetch_news` do not yet, and run on. Python cannot safely
  kill a thread, and §7.5 puts the hard kill in production, so saying so
  beats pretending.
- **`Job.scheduled` splits the registry in two.** A pipeline step has no
  interval setting, and `select(role=…)` now returns only the scheduled
  jobs — otherwise the agents process would have been handed `analyse` on
  a timer, which is exactly what this stage exists to stop. `candlegraph
  jobs` prints "pipeline step only" rather than "disabled", because they
  are different things.
- **A real bug the integration test caught: the executor passed the spec's
  *raw* params.** The loader validated them for the message and threw the
  result away, so `strategies: all` reached the body as the three
  characters `a`, `l`, `l` — three unknown strategy names, and a step that
  failed for every token. `step_params()` now validates at execution too,
  which also covers a spec built in Python that never saw the loader. Two
  unit tests pin it.
- **The causal rule the step was asked to prove.** `signal` reads only
  through `load_symbol_series`, and the test that matters compares two
  runs over the same gap where the gap runs to the end of the shorter
  window — so its right endpoint exists only in the longer one. Under
  `ffill` both runs score the gap's bars identically; under
  `interpolate` they would not (verified: 141/141/141 against
  142/143/144). A second test asserts the fill cache is never read on the
  way to a signal at all.
- Two smaller decisions recorded in the code: strategy names are checked
  at **load** time (code, the same everywhere) and symbols at **run**
  time (deployment config, so a spec valid in CI must not fail in a
  deployment tracking a different set); and `explain` has no `search`
  parameter at all, so no pipeline can turn spending on.
- **Deferred from design §15's step 4, deliberately:** the per-step model
  picker, the `model_choice` table and the Pipelines dashboard page.
  That is a migration plus a UI page plus the resolution order from
  `../harness/models/DESIGN.md`, which is itself unbuilt — a separate
  seam, and none of it is in this stage's exit criterion. It wants its
  own small rollout after step 7.
- `make check` green (1854 passed, 11 e2e skips, integration ran), and
  verified on a real stack: `make run ARGS='pipelines/hourly.yaml'` runs
  four steps as one family in `run_log`.

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

**Outcome.** Done 2026-11-18.

- **`run_queue` and a dispatcher**, not an event hook. The rollout said
  `RunHook.finished` → publish. A hook turned out to be the wrong seam:
  it fires inside the *worker* process, and the pipeline runs in the
  *agents* process, so the hook would have had to write somewhere the
  other process reads — which is the queue. Going straight to the queue
  removes a hop and a failure mode, and the hook stays available for
  step 6's tracing, which genuinely is per-process.
- **No `run_event` outbox**, against design §7.7. The outbox pattern keeps
  an event and a state change consistent across two *stores*; here both
  are the same Postgres, and `run_log` already records every finished run
  with its outcome and time. A second row is one more thing that can
  disagree. The dispatcher reads `run_log` over a lookback window, and
  the queue's unique key makes re-reading free — so **the window is the
  catch-up**, which is also how the rollout's "a missed notification must
  not lose work" is satisfied, with no watermark to keep and therefore
  none to lose. §7.7 and the table-placement document both record it.
- **The dedup key is the bar hour.** `once_per: hour | day | run` on the
  trigger, defaulting to `hour`. This is the load-bearing decision of the
  step: ingest polls every five minutes, so `run` would have been twelve
  hourly pipelines an hour, each re-writing the same signals. Three
  ingests in one hour now produce one run and two counted duplicates.
- **`dispatch` is a "quiet" job**, which is a narrow amendment to
  invariant 27 and is written into it. A 60-second poll that recorded
  every tick would be 1,440 rows a day of "looked, nothing there" — it
  would bury every real run in `run_log` and give the prune job most of
  its work. A quiet job writes a row only when it did something, and
  **always** when it failed. It may only be used by a job that is
  read-mostly and sub-second, so the half-state two-phase rows exist to
  show has nothing to show.
- **The advisory lock is real and tested.** A manual `candlegraph run
  hourly` while the dispatcher is working does not produce two concurrent
  runs: the second finds the lock taken and the queue row goes back with
  `available_at` pushed out.
- **Two bugs the integration tier caught**, neither visible from a fake
  database:
  1. `enqueue` and `claim` used `fetch_all`, which **rolls back** — it is
     the read helper. An `INSERT … ON CONFLICT DO NOTHING RETURNING *`
     through it looks like it worked and leaves nothing behind, so the
     queue deduplicated nothing at all. `execute_returning` now exists for
     writes that return rows, and says in its docstring why it is not
     `fetch_all`.
  2. The schedule net was evaluated **before** the tick's events were
     queued, so it fired alongside every event it existed to cover and the
     first tick started every pipeline twice. Schedules are now evaluated
     after, and see the row the event just made.
- **Deferred, each with a reason in design §15:** step-level *resume*
  (a re-claimed row re-runs from the start, which is safe because every
  step is idempotent — resume saves work, it does not make anything
  correct), and the circuit breaker (§7.6 reacts to a signal nothing in
  this step can see; it goes with step 6's metrics).
- **A third bug the real stack caught, which no test could have.** The
  quiet ledger leaked: `events_seen` is a non-zero count, so for the whole
  hour after an ingest every tick looked like work and wrote a row — ten
  rows in fifteen minutes. A count of what a tick *looked at* is not work
  done, and only the job knows the difference, so the body now returns
  empty counts when it did nothing and the generic hook rule stands. After
  the fix, five ingest events over ten ticks produced exactly one dispatch
  row: `enqueued 1, duplicates 4, events_seen 5`.
- `make check` green (1917 passed, 11 e2e skips, integration ran), and
  verified end to end on a real stack: ingest finished, the dispatcher
  queued one `hourly` row per bar hour, and `run_log` showed the pipeline
  and all four steps with trigger `event` under the ingest that caused it.

**Known limitations**, recorded rather than discovered later:

| | |
|---|---|
| An event older than `EVENT_LOOKBACK_MINUTES` is never acted on | By design; the `schedule` trigger is the net, which is why `hourly.yaml` carries one |
| A broken spec stops **its** pipeline silently | The dispatcher skips it and records the error in the tick's run rather than dying, because the other pipelines still need to run. `make check-pipelines` in CI is what stops it being merged |
| A pipeline slower than `QUEUE_LEASE_SECONDS` (900s) would be reclaimed while still running | The advisory lock stops the second run starting, and the row returns to the queue — so it is wasteful, not wrong. Raise the lease before adding a long step |
| `parent_run_id` means "caused by" for an event-triggered run and "owned by" for a step | The two share one column; `trigger` is what distinguishes them, and `event` is only ever written by this path |
| The dispatcher runs at most one pipeline per tick | Keeps a tick bounded by one pipeline. Two pipelines due at once run a minute apart |
| On a cold start the safety net fires before any data exists | Observed: the first tick ran `hourly`, which failed at `validate` because nothing was ingested yet. Left alone deliberately — "there is no data" is a true and useful thing for the ledger to say, and the next ingest starts a real run a minute later |
| An event and the safety net can both fire in the same hour | Their dedup keys are different by construction (the net's is its interval bucket, the event's is the bar hour), so a cold start can produce one extra run. Harmless — every step is idempotent — and the net stands down for its whole interval afterwards |

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

**Outcome.** Done 2026-11-18.

### Traces

- `harness/tracing.py`: a `TracingHook` in `default_hooks`, so every run is
  a span and every stage an event on it. **No `if tracing_enabled:`
  anywhere** — `opentelemetry-api` without an SDK hands back a
  `NonRecordingSpan`, so the instrumentation is always compiled in and costs
  a few no-op calls. Turning it on is installing the `otel` extra and
  setting `OTEL_ENABLED`; the code is identical either way.
- **Nothing wires up the nesting.** A pipeline's steps call `run_job` inside
  the pipeline body on the same thread, so OpenTelemetry's context makes
  each step a child of the pipeline span. Verified against a real collector:
  one trace, `run hourly` at the root, `validate` / `refresh_aggregates` /
  `analyse` / `signal` / `explain` beneath it.
- A stage is an **event**, not a child span: a stage is reported after it
  has already finished, so a span would need a start time we would have to
  invent.
- `docker-compose.otel.yaml` is an opt-in overlay (`make otel-up`,
  `make otel-logs`) running a collector that prints spans and stores
  nothing. The main compose file is untouched, which is what the two October
  start-up post-mortems asked for. The Dockerfile gained `--extra otel`, the
  one deliberate exception — without the SDK in the image the overlay would
  start a collector that never receives a span.

### Aggregates, and what the measurement actually said

The step said to measure before keeping them. Full numbers and reasoning are
in [`../storage/timescaledb/DESIGN.md`](../../storage/timescaledb/DESIGN.md);
the short version:

- **The candle query was never slow.** A decade of hourly data (2.6M rows)
  re-aggregates to daily candles in 64 ms. The aggregate makes it 9.8 ms —
  4 to 7× across every scale tested, but from fast to fast. On its own this
  did not justify a second definition of a candle.
- **The query that *is* slow is one the aggregate cannot serve.** The Market
  dashboard reads raw hourly rows: 610 ms for a five-year window. A daily
  aggregate answers the same window in 22 ms, but the dashboard plots
  *hourly* bars, so serving it daily data changes what the chart shows. That
  is a display decision for whoever owns the page, **not taken here**, and
  the 610 ms is recorded in the design so it is argued from a number.
- **So the aggregate is kept for the candle API only**, where it is a strict
  improvement and returns identical numbers. `chart_statement` routes any
  interval that is a whole number of days to `token_daily`; anything finer
  reads the hourly rows.

Four things the definition had to get right, each now a test:

1. **Sums and counts, never a materialised average.** Hours are missing by
   design here (invariant 19), so an average of daily averages weights a
   four-hour day the same as a full one.
2. **`COUNT(column)` separately from `COUNT(*)`.** A not-collected column is
   `NULL`, not zero (invariant 11); dividing by the wrong count halves the
   figure.
3. **`first()`/`last()`** rather than `array_agg(… ORDER BY …)`.
4. **Real-time aggregation left on**, so the newest bucket is computed live
   and the API's answer does not depend on when the refresh last ran.

### Refresh: the whole range, every time

Measured: a full refresh costs **0.79 ms when nothing changed**, 12 ms after
a 90-day backfill, and 133 ms after a three-year one — a *windowed* refresh
of the same change costs the same 12.6 ms. The refresh is driven by the
invalidation log, so a window buys nothing and introduces a failure that is
hard to see: `backfill --days 365` can rewrite any hour (invariant 13), and
anything older than the window would stay wrong until somebody noticed.

There is no refresh policy. `refresh_aggregates` is a registered job and a
step of `pipelines/hourly.yaml`, so the refresh is a recorded run with a
trigger, an outcome and the view's extent in its counts.

### Things found while building

- **The watermark is a high-water mark and never moves back.** An hour newer
  than it is served live with no refresh; an hour behind it — what a
  backfill rewrote — waits for one. Both are now tests. This also made one
  test order-dependent: an earlier test pushed the watermark forward and a
  later one wrote behind it, so the tests compute their base from the
  watermark rather than from a constant.
- `refresh_continuous_aggregate()` **cannot run inside a transaction block**,
  and psycopg2 opens one before the first statement — hence
  `DatabaseManager.execute_outside_transaction`. `CREATE MATERIALIZED VIEW
  … WITH (timescaledb.continuous)` and `DROP` both *can*, so the migration is
  ordinary; it says `WITH NO DATA` because creating with data refreshes.
- The view argument to `refresh_continuous_aggregate` must be a **string
  literal** cast to `regclass`; a bare qualified identifier parses as a
  column reference and fails with "missing FROM-clause entry".

### Not done, and why

- **Metrics (§8.3).** The exit criterion names traces, and a span already
  carries the duration, the outcome and the counts that table wants — a
  metrics pipeline is a separate backend decision. The circuit breaker that
  step 5 deferred "to step 6's metrics" therefore moves with them.
- **The Market dashboard's 610 ms**, above: a display decision, not a
  storage one.
- **`pool_hours_data` has no aggregate.** Nothing reads pool candles
  by day; `token_daily` exists because `chart_statement` is a consumer.

---

## Step 7 — Closeout

`AGENTS.md` (layout, commands, any new invariant — likely "a pipeline
spec is validated before it runs"), `README.md`, the roadmap's stage 2
marked delivered with its exit criterion checked, skills (`run-stack`,
a new `add-pipeline` skill), archive this file.

**Done when.** `release-check` passes against a real stack; every Status
line under `docs/` is true.

**Outcome.** Done 2026-11-19, with one gate carried forward.

- **`AGENTS.md`**: stage 2 moved to "Delivered" with its exit criterion;
  "In progress" says nothing is; invariant 29 (*a pipeline spec is
  refused whole before anything runs*), pinned by the loader tests in
  `tests/unit/harness/test_pipelines.py` (`test_every_committed_spec_is_valid`
  and the refusal tests beside it); `docs/overview/` in the layout. The
  commands table already carried `run`, `check-pipelines`, `queue`,
  `aggregates` and `otel-*` from steps 3–6.
- **`README.md`**: the status section lists stages 0, 6 and 2 as
  delivered (it still said stage 0 was next); "How it works" names
  traces and `token_daily` and points at the overview.
- **New: [`../../overview/DESIGN.md`](../../overview/DESIGN.md)** — the
  whole stack on one page: purpose, the processes, an hour's life from
  ingest to a cited signal, who may write what, where to look when
  something is wrong, the honest limits, and where it is going (the
  harness in the UI). Because the MCP server serves every
  `docs/**/DESIGN.md`, it is also `candlegraph://docs/overview` to an
  assistant, with no code change.
- **Roadmap**: stage 2 delivered with its exit criterion checked;
  three stale spots fixed on the way — "no stage is open", stage 6's
  posterior clause still marked *remaining* (met 2026-11-10), and the
  diagram drawing stages 0 and 6 as to-do.
- **Status lines made true**: `docs/README.md` (rollout, pipelines,
  overview), pipelines design (`implemented`, with the not-built list),
  runner design (the ingest strip *is* drawn; embed is a pipeline step
  now), TimescaleDB and table placement (`token_daily`, `run_queue`; the
  `run` ledger's heading still said *proposed*), agents usage (the
  observer as a pipeline step).
- **Skills**: a new `add-pipeline`; `run-stack` (`make jobs` lists
  eleven jobs, not four; how to see the hourly pipeline fire);
  `release-check` (pipelines and the queue are part of the gate);
  `run-agents` (`runs --all` includes pipeline families). `CLAUDE.md`'s
  pointer to the current rollout updated.

**Two gaps found while closing, recorded rather than fixed** (this
session was documentation only):

1. **`pipelines/daily.yaml` has no trigger.** It ships and validates, but
   nothing starts it, so `embed` still runs only when a person asks —
   the backlog gap step 4 said it closed is closed *in the spec*, not in
   the running system. The fetch can be scheduled on its own
   (`FETCH_NEWS_INTERVAL`), but that runs `fetch_news` without `embed`
   behind it. The fix is one `schedule:` trigger, held back because it
   spends credits and is a decision for a person.
2. **The `run`-retention `prune` job (design §16, answer 5) was never
   built.** The dispatcher prunes `run_queue` rows (`QUEUE_RETENTION_DAYS`);
   nothing prunes `run`. At one hourly pipeline — a parent and five step rows an hour — that
   is ~50k rows a year — not urgent, and audit finding 13 already lists it.

**Not done: the release check against a real stack.** Docker was not
running in the closing session, so the stack half of `release-check`
(`make up`, `make e2e`, `report --strict`, a real hourly pipeline fired
by ingest) did not run. `make check` was run: format, lint and mypy
clean; 1722 passed, 241 skipped (the integration and e2e tiers), and
**1 failed** —
`test_timescale.py::test_a_server_that_does_not_preload_timescaledb_is_refused_before_any_ddl`,
which starts its own container and, unlike the rest of the tier, raises
rather than skips when Docker is down. That is a test-harness gap, not a
regression (no code changed in this step), and it is recorded in
[`../ROLLOUT.md`](../ROLLOUT.md) beside the stack run. Steps 5 and 6 each verified the pipeline on a real stack, and
this step changed no code, so the risk is small — but "small" is not
"verified". [`../ROLLOUT.md`](../ROLLOUT.md) carries it as the first
thing to do.

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
