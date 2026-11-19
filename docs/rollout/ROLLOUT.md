# Rollout: the harness, visible — operating candlegraph from the UI

**Status:** current — planned 2026-11-19, not started. Eight steps.
**Where it sits:** a follow-on to stage 2, not a new roadmap stage. It
numbers nothing: the roadmap's next *stage* is still 3 (traditional
markets), and this plan is what makes stage 2 usable by a person before
the system gets wider.
**Designs it follows:** [`../harness/pipelines/DESIGN.md`](../harness/pipelines/DESIGN.md)
(§9 the model picker, §16 answers 2 and 5), [`../harness/models/DESIGN.md`](../harness/models/DESIGN.md)
(proposed), [`../knowledge/budget-manager/DESIGN.md`](../knowledge/budget-manager/DESIGN.md)
(proposed), [`../audit/2026-11-16-system-review/FINDINGS.md`](../audit/2026-11-16-system-review/FINDINGS.md)
(findings 8 and 13).
**How the stages fit together:** [`overview/DESIGN.md`](overview/DESIGN.md).
The previous plan, stage 2, is archived at
[`archive/2026-11-15-stage-2-harness.md`](archive/2026-11-15-stage-2-harness.md).

## Stage 2's closeout gate: done

Carried into this plan and discharged on 2026-11-19, against a real
stack (`COMPOSE_PROJECT_NAME=cg-oak-otel-…`, isolated ports):

| Gate | Result |
|---|---|
| `make check` with the integration tier running | 1953 passed, 11 skipped (e2e only), 0 failed |
| Stack healthy; `/status` | `ok`, `database: up`, `data_fresh: true` |
| `make e2e` | 11 passed, **0 skips** |
| `report --strict` | one token under the threshold: **GNO 65.9%** — expected (invariant 19) |
| `make check-pipelines` | `daily`, `example`, `hourly` all valid |
| **Stage 2's exit criterion** | one ingest → exactly **one** `hourly` run, trigger `event`, with `validate → refresh_aggregates → analyse → signal → explain` as child runs |
| The dedup claim | a second ingest in the same bar hour produced **no** second run; the queue kept one `hour:2026-11-19T09` row |
| `make jobs` | eleven jobs; six marked *pipeline step only* |

Three things the run confirmed that were only written down before: the
cold-start note (the safety net fired before any data existed and
`validate` failed, then the next ingest started a real run a minute
later); the quiet-job rule (ticks that found nothing wrote no row); and
the `run-agents` skill's warning that a fresh stack's **first observer
report is CRITICAL** — it fired while the first ingest was still running
and truthfully saw four tokens with no rows. The next run, fifteen
minutes later, was `warning` with the one expected finding (GNO
coverage). Both skills described the stack correctly, which is the point
of checking them against a real one.

One thing it found: a **stale volume from a draft migration**. The
project's volume was stamped `9b3d7e21c4f5_candle_aggregates`, a revision
renamed to `9d4b7a2c6e15_token_daily_aggregate` before it was committed,
so no checkout could reach it. The preflight named the cause and pointed
at the remedy, `make reset` fixed it for that project alone, and nothing
else on the machine was touched — the 2026-10-28 post-mortem working as
intended. The test-hygiene half of the gate (a test that fails rather
than skips without Docker) is **step 1** below.

## What this plan covers

Everything stage 2 built is reachable only from a terminal. This plan
puts it on screen, and closes the two gaps stage 2 recorded rather than
fixed.

| Candidate from the stage 2 closeout | Where it lands |
|---|---|
| A Pipelines dashboard page | **Steps 2 and 3**, split read-only / can-act |
| The `run` prune job (§16 answer 5) | **Step 4** |
| The budget manager | **Step 5** |
| A schedule for `daily.yaml` | **Step 6**, after the budget manager |
| The chat-model seam and the per-step model picker | **Step 7** |
| Authentication and backups (findings 8, 9) | **Not here**, except the loopback bind in step 1 — see "Left out" |

## Progress

| # | Step | Size | Depends on | Status |
|---|---|---|---|---|
| 1 | Two corrections first: the Docker-less skip, and ports on loopback | S | — | todo |
| 2 | The Pipelines page, read-only | M | 1 | todo |
| 3 | Start a pipeline from the page (enqueue, never execute) | M | 2 | todo |
| 4 | The `run` prune job | S | — | todo |
| 5 | The budget manager | M | — | todo |
| 6 | Give `daily.yaml` a schedule | S | 5 | todo |
| 7 | The chat-model seam and the per-step model picker | L | 2 | todo |
| 8 | Closeout | S | 1–7 | todo |

**Why this order.**

- **Step 1 before the UI gains buttons.** Today every published port binds
  to every interface (finding 8) and there is no authentication. While the
  UI is read-mostly that is a laptop's problem; the moment step 3 lets a
  click start a run and step 6 lets the system spend credits on a timer,
  an open port is someone else's button. Binding to loopback is a
  two-line default and buys the whole plan its safety margin. The
  Docker-less skip rides along because it is the other thing that makes a
  green `make check` mean less than it should.
- **Read before write (2 then 3).** The page is worth having even if no
  button is ever added, and splitting them means the risky half is
  reviewed on its own.
- **Step 4 is independent** and can fill any session; it is listed after
  3 only because nothing depends on it. Do it sooner if `run` grows.
- **Step 5 before step 6.** A scheduled `daily.yaml` spends credits
  without being asked. Today one pot serves both automatic and manual
  searches, so a scheduled fetch could leave a person unable to search at
  all. The budget manager is what makes the schedule safe, so it comes
  first — this is the one ordering constraint in the plan that is not
  merely preference.
- **Step 7 last of the build steps.** It needs the page (its panel lives
  there), it carries a migration, and it is the only step whose design
  (`../harness/models/DESIGN.md`) is still `proposed`.

**One step per session.** Steps 2 and 3 must not share one: 3 is the
first thing in this repository that lets a browser click start work, and
it should be reviewable alone. Steps 4 and 6 may share a session with a
neighbour if time allows.

---

## Step 1 — Two corrections first

**Goal.** `make check` on a machine with Docker stopped reports skips and
passes. The stack's published ports bind to `127.0.0.1` by default.

**Read first.** `tests/integration/conftest.py` (how the tier decides to
skip), `tests/integration/test_timescale.py::test_a_server_that_does_not_preload_timescaledb_is_refused_before_any_ddl`
(the one that does not), `docker-compose.yaml` ports, finding 8, and both
October post-mortems — this plan's only compose change is here.

**Changes.**

- The preload test asks the same question `conftest` asks and skips when
  the answer is no. It catches `ImportError` today and nothing else.
- Every published port becomes `${BIND_HOST:-127.0.0.1}:${…_HOST_PORT:-…}:…`.
  `.env.example` documents `BIND_HOST`, saying plainly that `0.0.0.0`
  publishes an **unauthenticated** UI and API to the network.

**Watch for.** `E2E_URL` and `make mcp` already address `localhost`, so
they are unaffected. CI runs `make check`, not `make up`. Anyone who
reaches their stack from another machine needs `BIND_HOST=0.0.0.0`, and
the release notes should say so.

**Done when.** With Docker stopped, `make check` passes (integration
skipped); with Docker running, it passes with the tier executed;
`docker compose ps` shows `127.0.0.1:8000->8000/tcp`; `make e2e` still
passes.

---

## Step 2 — The Pipelines page, read-only

**Goal.** A dashboard page that answers *what ran, what is queued, and
what did each step do* without a terminal.

**Read first.** `ui/app.py` `_agents_page` and `_stage_strip` (the
pattern, and the strip is reused as-is), `ui/viewmodels.py`
`build_agents_view` / `stages_frame`, `storage/statements.py`
`get_run_log` and `get_run_log_children`, `harness/queue.py`,
`harness/pipelines.py` (the loader — the page lists specs by loading
them), pipelines design §9.3 (the panel step 7 adds here).

**Changes.**

- `build_pipelines_view(db, settings)` in `viewmodels.py`: recent
  pipeline families (parent run plus its children, in one extra query,
  the way the CLI does it), the queue with state, attempt and dedup key,
  the last `dispatch` run, and the committed specs with their triggers,
  steps and deadline.
- Panels: families with a stage strip each, the queue table, the specs,
  and a metric strip (last run, outcome, age, queued depth).
- **A broken spec is drawn, not raised**: the loader's refusal, with file
  and line, appears as a card. A page that dies because one YAML file is
  wrong is worse than the YAML file.

**Watch for.** No writes at all in this step, including no "refresh"
that runs anything. Streamlit reruns on every widget change, so the
queries stay in `viewmodels.py` behind one call. Page functions stay
under 60 lines (the step 1 rule from stage 2).

**Done when.** The page shows the hourly family with its five steps, the
queue row with its dedup key, and a `dead` row when one exists; a
deliberately broken spec shows as a card and the rest of the page still
draws; `AppTest` covers the page; `make check` green.

---

## Step 3 — Start a pipeline from the page

**Goal.** A **Run now** button per spec that *asks* for a run the way
every other trigger does.

**Read first.** Invariant 28 (a pipeline runs because a row says so),
`harness/queue.py::enqueue`, `harness/events.py` (the claim), and
`viewmodels.py::run_observer_now` plus the backfill confirmation (the
house pattern for a button that writes).

**Changes.**

- The button **enqueues** with `trigger="ui"`; it never calls the
  executor. The dispatcher claims it, so the advisory lock, the lease and
  the single-writer rule all keep working, and a UI click is indexed in
  `run_queue` beside every other trigger.
- The dedup key is `ui:<minute>`, so a double-click is one run and a
  deliberate second run a minute later is allowed.
- `daily` sits behind the confirmation step, which states the cost in
  credits before the first click does anything.
- When `EVENT_POLL_INTERVAL=0` there is no dispatcher, so the button is
  disabled **with that sentence on screen** rather than queueing a row
  nothing will claim.

**Watch for.** The UI process must not take the pipeline advisory lock —
it is not the executor. The page should show the queued row immediately
and the run when it appears, so the gap between asking and running is
visible rather than looking like a dead button.

**Done when.** One click on `hourly` produces exactly one queue row with
trigger `ui` and one run within a dispatcher tick; a double-click still
produces one; with the dispatcher off the button explains itself;
`make check` green and the behaviour confirmed on a real stack.

---

## Step 4 — The `run` prune job

**Goal.** Pipeline history stops growing without bound (§16 answer 5;
audit finding 13).

**Read first.** Pipelines design §16 answer 5, `harness/queue.py::prune`
(the pattern and its retention setting), invariant 4 (why deleting these
rows is allowed at all, and why `ingest_run` and `agent_run` are not
touched).

**Changes.** A `prune` job, scheduled daily, role `agents`, writing
`ledger` only; `RUN_RETENTION_DAYS` (default 30) in `settings.py` and
`.env.example`; one statement in `storage/statements.py`.

**Watch for.** A parent and its step children must go together, or the
page draws a family with holes. Never delete a row still `running`. The
`run_log` view unions three tables — deleting from one must leave it
valid, and there is a test that reads the view after a prune.

**Done when.** `make jobs` lists `prune`; an integration test shows a row
older than the retention deleted, a recent one kept, a `running` one
kept, and `run_log` still queryable; `make check` green.

---

## Step 5 — The budget manager

**Goal.** Automatic and manual searches draw from separate pots, the
split can change without a restart, and something warns before a pot runs
out. This is what makes step 6 safe.

**Read first.** [`../knowledge/budget-manager/DESIGN.md`](../knowledge/budget-manager/DESIGN.md)
in full, `knowledge/budget.py` (the guard it changes), `knowledge/news.py`
(the caller), and the News dashboard page.

**Changes.** As the design says: the pots, the `budget_policy` table with
newest-wins, the guard reading the policy per call, the warning agent,
and a panel on the **News** page to see and change the split.

**Watch for.** The refusal path is already correct and must stay: a
refusal is recorded in `source_call` and the job still succeeds. A
policy change must not be retroactive — it governs the next call, not the
month's arithmetic.

**Done when.** An automatic fetch cannot spend the manual pot's credits
(a test), the split changes live, the agent warns at the designed
threshold, and the design's Status line says `implemented`.

---

## Step 6 — Give `daily.yaml` a schedule

**Goal.** News lands and is embedded without anyone asking, within the
pot step 5 gave it. This closes the backlog gap stage 2 closed only in
the spec.

**Read first.** `pipelines/daily.yaml` (its header says why it has no
trigger), §16 answer 2, the step 5 pots, and
`docs/incidents/2026-11-04-explain-ran-lexical-only/` — an unembedded
corpus is exactly that incident's failure mode.

**Changes.** One `schedule: { every: 24h }` trigger with
`once_per: day`; the run draws from the automatic pot; `README.md`,
`AGENTS.md` and `docs/overview/DESIGN.md` stop saying it only runs when
asked.

**Watch for.** The scheduler fires immediately on start by design, so the
first run after a deploy spends credits at once: either accept and
document it, or let the dedup key for "today" absorb it. Decide in the
step, do not discover it in a bill.

**Done when.** Over a day on a real stack, exactly one `daily` run with
dedup key `day:<date>`; the credits came from the automatic pot; embedding
keeps up (`remaining` in the run's counts trends to zero).

---

## Step 7 — The chat-model seam and the per-step model picker

**Goal.** Which model writes the prose is a choice a person makes on
screen, per role, and every run records what wrote it.

**Read first.** [`../harness/models/DESIGN.md`](../harness/models/DESIGN.md)
(the audit and the seam), pipelines design §9 in full,
`knowledge/explain.py` and `agents/narrator.py` (today's two narrators),
`harness/params.py` (`model_role` is already carried on a step).

**Changes.** The seam behind both narrators; a `model_choice` table and
its migration (placement recorded in
[`../storage/table-placement/DESIGN.md`](../storage/table-placement/DESIGN.md)
first); the resolution order *UI choice → environment → template* with
the fallback and its reason recorded on the step's run; the Models panel
on the Pipelines page, greying a provider whose key is absent and saying
which key.

**Watch for.** Invariant 22 does not bend: a model choice changes prose
and never a number, a finding or an outcome. No key is ever stored in the
database — only a provider and a model name. A removed key must fall
back to the template and say so, not fail the step.

**Done when.** Changing the `knowledge` role in the UI changes the next
`explain` step's `detail.model` with no restart; removing the key falls
back to `template` with `model_fallback` recorded; both designs' Status
lines are updated; `make check` green.

---

## Step 8 — Closeout

`AGENTS.md` (commands, layout, any new invariant — a candidate is *the UI
asks for a run and never performs one*), `README.md`,
`docs/overview/DESIGN.md` ("Honest limits" should lose three entries),
[`overview/DESIGN.md`](overview/DESIGN.md), the Status line of every
design this plan touched, the `run-stack` and `release-check` skills, and
a `pipelines-page` note in the UI section of the README. Archive this
file and write a `ROLLOUT.md` saying what is next.

**Done when.** `release-check` passes against a real stack, including the
new page and a UI-triggered run; every Status line under `docs/` is true.

---

## Left out of this plan, deliberately

| Not here | Why, and what would pull it in |
|---|---|
| **Authentication** (finding 8) | Step 1's loopback bind removes the exposure for a laptop, which is where this runs. Real authentication arrives the day the stack is reachable from another machine — that is its own small rollout, and it should land *before* any such deploy, not after |
| **Backups** (finding 9) | Nothing here makes data loss more likely, and stored history is re-ingestible within `LOOKBACK_DAYS` limits. Pull it in when a backfill longer than the subgraph's retention exists, which is when the history stops being reproducible |
| **Step-level resume, metrics, the circuit breaker, hard timeouts** | Stage 2 deferred each with a reason in pipelines §15, and none has shown a symptom yet. Metrics and the breaker travel together; resume matters when a pipeline is long enough that re-running it from the start is expensive |
| **Health checks on `worker` and `agents`** (finding 12) | Still true: four services have one, those two do not, so a wedged worker looks fine to compose. Step 1 is the only step that opens `docker-compose.yaml`, so **fold it in there if you want compose touched once** — it is a few lines and the alternative is opening the file again later |
| **Stage 3 (equities), stage 4 (models), stage 5 (alerting)** | Roadmap stages, each larger than this whole plan. The analyst needs 3 and 4; this plan needs none of them |
| **The Market dashboard's 610 ms five-year read** | A display decision recorded in the TimescaleDB design, not a storage one. It belongs to whoever next owns that page |

## Rules for every step

- `make check` green before a step is done, with the integration tier
  **running** — skips are not green. Schema changes round-trip on tables
  with rows, and a new table gets its placement recorded first.
- Compose and the Dockerfile change in **step 1 only**.
- No new dependency without saying why in the step's Outcome.
- Invariants 1–29 stand. A step that seems to need breaking one stops and
  writes down why first.
- Anything in the UI that spends or writes stays behind an explicit
  button, and anything that spends money behind a confirmation that
  states the cost.
- Update the progress table at the end of every session, and record what
  the step actually did in its **Outcome** — including what it chose not
  to do.
