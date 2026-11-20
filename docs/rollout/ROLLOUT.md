# Rollout: the harness, visible — operating candlegraph from the UI

**Status:** current — planned 2026-11-19; steps 1–4 and the 4.1 closeout done, step 5 next. Eight steps plus one interim closeout.
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
| 1 | Two corrections first: the Docker-less skip, and ports on loopback | S | — | done 2026-11-19 |
| 2 | The Pipelines page, read-only | M | 1 | done 2026-11-19 |
| 3 | Start a pipeline from the page (enqueue, never execute) | M | 2 | done 2026-11-19 |
| 4 | The `run` prune job | S | — | done 2026-11-20 (with an unplanned start-up guard first) |
| 4.1 | Interim closeout: the pipeline work concluded, gaps consolidated, orientation written | S | 1–4 | done 2026-11-20 |
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

**Outcome.** Done 2026-11-19 (shared a session with step 2, at the
user's request; 1 was finished and green before 2 started).

- **The plan's diagnosis was half wrong.** The test *did* guard
  `container.start()`; what escaped was the **constructor**.
  `PostgresContainer(...)` builds a Docker client and pings the daemon
  in `__init__`, and the test built it before its `try`. `conftest`
  always built it inside, which is why only this one test failed. The fix
  moves construction into the guard, with a comment saying why.
- **Reproduced without stopping Docker.** Another project and another
  session's stack were running on the daemon, so stopping it was not an
  option. `DOCKER_HOST=unix:///nonexistent/docker.sock` fails in the
  same place: 1 failed before the fix, **231 skipped, 0 failed** after.
  `make check` that way: 1725 passed, 242 skipped. With Docker running:
  **1956 passed**, 11 skipped (e2e only).
- **`BIND_HOST`, not `HOST`.** `HOST` and `UI_HOST` are the in-container
  listen addresses and must stay `0.0.0.0`, or Docker cannot forward a
  published port to the server at all. Reusing the name would have
  invited exactly that mistake, so the host-side address is a separate
  setting and `.env.example`, the README and `run-stack` all say which is
  which. It is a compose interpolation variable, not a `Settings` field:
  the application never reads it.
- **Five ports, not four.** The opt-in collector overlay publishes 4318,
  and an open trace receiver is still an open port. It is bound the same
  way. `OLLAMA_HOST_PORT` and `OTEL_HTTP_PORT` were missing from
  `.env.example` and are now documented beside the others.
- **Pinned by a test.** `test_every_published_port_binds_to_loopback_unless_told_otherwise`
  reads both compose files; run against the previous `docker-compose.yaml`
  it names all four unbound services. A second test checks the warning
  is in `.env.example`.
- **On a real stack** (`cg-elm-1795083268`): every published port shows
  `127.0.0.1:…`; `/status` answers 200 on loopback and the connection is
  **refused on the LAN address**; `make e2e` 11 passed.
- Audit finding 8 is recorded as half closed: the exposure is gone, the
  missing authentication is not.

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

**Outcome.** Done 2026-11-19, in the same session as step 1 and after it
was green.

- **The page.** `Pipelines`, between Agents and News. A metric strip
  (latest run and outcome, age, waiting, **dead**, dispatcher), warnings
  for what stops pipelines quietly, one expander per run with the step
  strip and a steps table, the queue, and a card per spec. The page
  function is 10 lines over five panels; all the arithmetic is in
  `viewmodels.py` (`build_pipelines_view`, `load_spec_cards`,
  `describe_trigger`, `duration_text`, `age_text`). **No button, no
  write** — a unit test asserts the fake recorded no writes, and an
  `AppTest` asserts the page has no buttons at all.
- **One new statement, against the plan's "reuse".** `get_pipeline_runs`
  keeps the newest *N per pipeline* with a window function. One shared
  `LIMIT` — reusing `get_run_log` — would let `hourly` (24 runs a day)
  push `daily` (one) off the page within a day, so that it looked as
  though it had never run. Still one query however many specs. An
  integration test writes twelve `hourly` runs and one `daily` and checks
  both survive the cap.
- **The steps table reads `detail.steps`, not the child runs.** The
  parent's record has the step id, attempts, error class and every
  **skipped** step; a child run has none of those and a skipped step has
  no child run at all. Child runs are used only while a run is still
  going (its `detail` is written when it closes), and the page says the
  table is live then.
- **Owned versus caused.** `parent_run_id` means "owned by" for a step and
  "caused by" for an event-triggered run, so a pipeline started by
  another pipeline finishing would have shown as one of its steps. The
  loader guarantees a pipeline name is never a job name, so a child named
  like a pipeline is listed as *started next*. **Found, not fixed:**
  `candlegraph runs` has the same latent mis-display — it would indent a
  caused pipeline as a step. No committed spec triggers on another
  pipeline, so it is invisible today; it is a CLI change and belongs
  with step 3, which touches the same vocabulary. *(Step 3 did not take
  it — see its Outcome. Still open.)*
- **Dead rows cannot scroll away.** The queue is the newest 50 rows plus
  every `dead` row, fetched separately and merged by id, because a dead
  row is the one thing on the page that needs a person.
- **Specs are loaded one file at a time.** `load_pipeline_dir` stops at
  the first bad file — right for `--check`, wrong for a page. What only
  the set can be wrong about (a shared name, a trigger loop) is checked
  over the specs that loaded and shown as its own error.
- **What the page does not show, on purpose.** Runs of a pipeline whose
  spec no longer exists, and runs of a spec the loader refuses (its name
  cannot be known without trusting the file it refused). The dispatcher
  is a quiet job, so "newest recorded tick" is the newest tick that *did*
  something, and the caption says so rather than implying it stopped.
- **On a real stack** (`cg-elm-1795083268`, after ~40 minutes of real
  ingest): rendered headlessly against its database — no exception, no
  button; `hourly · PARTIAL`, the five-step strip, the cold-start
  `failed` run beside it; the queue with `hour:2026-11-19T10` and
  `every7200:…`; and, after seeding one dead row into that throwaway
  database and pointing `PIPELINES_DIR` at the specs plus a broken one,
  **Dead rows 1** and the card
  `specs/broken.yaml:4: unknown job 'no_such_job'` with the three real
  specs still drawn.
- `make check` **1997 passed**, 11 skipped (e2e), integration ran;
  without Docker the integration tier is 233 skipped, 0 failed.
- Not done, and not claimed: caching. The page makes up to five database
  reads per Streamlit rerun, plus reading the spec files — the Agents page
  is no different; audit finding 14 stands.

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

**Outcome.** Done 2026-11-19, in its own session.

- **The write is one INSERT.** `request_pipeline_run` re-reads the specs,
  refuses or calls `queue.enqueue(trigger="ui")`, and returns. No
  executor, no claim, no advisory lock — a unit test proves it: against a
  fake with no `connection()` (so a lock would raise), the only statement
  handed over is `enqueue_run`. **The dispatcher needed no change**: it
  already claims rows of any trigger and runs them with that trigger.
- **A new `ui_dedup_key`, not `manual_dedup_key`.** The latter existed
  but was dead code (one test, no caller), and its rule — "a person
  asking twice means twice", a key that never collides — would have made
  a double-click two pipeline runs. `ui:<minute>` absorbs double-clicks,
  second tabs and re-run callbacks; `manual_dedup_key` is left for a
  future CLI enqueue.
- **Refused in one more case than the plan named.** Besides the
  dispatcher being off, *Run now* refuses while the specs disagree (a
  loop or a shared name): the dispatcher then dispatches **none** of them,
  so a queued row would only fail later with "no spec named …".
- **"Show the queued row immediately."** The buttons are below the queue
  table, so a plain `if st.button()` would enqueue *after* the table was
  drawn and the click would look dead. They use `on_click` callbacks,
  which run before the rerun.
- **"And the run when it appears."** While a run this page asked for is
  unfinished, the live panels are a `st.fragment` re-read every 5 s; when
  the row is `done` or `dead` it says how the request ended and stops
  itself. An idle page polls nothing.
- **A bug found and fixed while testing:** the click's notice was drawn,
  then thrown away by the watcher's own `st.rerun()` — a double-click on
  an already-finished run showed nothing. The watcher now replaces the
  notice with how the request ended, or carries it over.
- **`daily`'s cost is stated exactly, not as a worst case.** Each active
  token's `(symbol, UTC day)` cache entry is checked: only tokens not
  searched today can cost a credit. Spend today and this month against
  the caps is beside it. Which specs need the confirmation is decided by
  their **jobs** (`SPENDING_JOBS = {"fetch_news"}`), not by the name
  `daily`.
- **A dispatcher that is not running is made visible.** The UI cannot see
  the agents process, only what it did, so a row claimable for more than
  three dispatcher intervals gets a warning naming `make ps` and
  `make logs-agents`.
- Tests: 32 new — the dedup key, every refusal, the insert-only proof,
  the cost estimate, the watcher; `AppTest` for a click, a double-click,
  the confirmation and its cancel, the disabled state, and watching to
  `done` and `dead`; and two integration tests against real SQL: the
  unique index collapses a double-click, and `dispatch_once` runs a UI
  row with `trigger=ui`. `make check` **2029 passed**, 11 skipped (e2e);
  without Docker the integration tier is 235 skipped, 0 failed.
- **On a real stack** (`cg-elm-1795122208`), through the page code against
  its database: two clicks a second apart produced **one** row
  (`ui:2026-11-19T21:07`) and the explanation for the second; the live
  dispatcher claimed it **7 s later** and `run_log` shows one `hourly` run
  with `trigger=ui` and five `ui` steps; with `EVENT_POLL_INTERVAL=0` all
  three buttons were disabled with the sentence on screen; `daily`'s
  confirmation read "up to 5 credit(s) … 0/40 today, 0/1000 this month",
  and **Cancel** queued nothing. Confirm was deliberately not pressed: it
  would spend real credits.

**Found, not fixed** (in scope for later steps or their own work):

| | |
|---|---|
| `SPENDING_JOBS` is a hand-kept list | `Job` has no `spends` declaration; a new metered job not added here would get a *Run now* with no confirmation. A `spends` field checked by a registry test is the real fix |
| The UI judges "dispatcher on" by its **own** `EVENT_POLL_INTERVAL` | The agents container reads its own; compose gives both the same `.env`, but nothing enforces it. The staleness warning is the backstop, after three intervals |
| `candlegraph run` still executes in the foreground | Invariant 28 says a person never starts a pipeline directly; the CLI does, safely (it takes the lock), but inconsistently. `manual_dedup_key` was written for the CLI enqueue that would fix it |
| No MCP `run_pipeline` tool | The UI can now ask for a run and an assistant cannot; the enqueue path it would wrap exists |
| No way to withdraw a queued request | A mistaken click runs. Harmless for idempotent pipelines; worth having before step 6 schedules a spending one |
| A page left open on a stuck row polls every 5 s | Bounded to "while a row you asked for is unfinished", but unbounded in time if the dispatcher is down |
| `candlegraph runs` mis-indents a caused pipeline as a step | Deferred from step 2; still latent (no spec triggers on another pipeline) |
| The 5 s refresh was not watched in a real browser | `AppTest` does not fire `run_every` timers; the watcher's states were verified by re-running it, and the live stack showed the row and run arriving |

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

**Before it: the stack would not start in `elm` — fixed first, at the
user's request.** The elm worktree's default `make up` failed with the
preflight's "stamped at revision … which does not exist in this
checkout" and ran only under an invented project name. Write-up:
[`../incidents/2026-11-20-stack-would-not-start-again/POSTMORTEM.md`](../incidents/2026-11-20-stack-would-not-start-again/POSTMORTEM.md).
In short: the third volume a checkout could not start; all branches were
at one commit, so not divergence; the two proven earlier cases were
draft revisions renamed before commit; this one's revision was not
recoverable (elm was rebuilt while it was being read). Two preventions,
both in `scripts/stack-guard.sh`, run first by `up`, `down`, `reset` and
`otel-up`: **refuse a project whose containers another checkout started**
(verified against the live elm stack from oak — `up` and `down` both
refused, elm untouched), and **refuse `up` on the default stack while
`alembic/versions` has uncommitted changes**. `make stacks` lists who owns
what. Labelling volumes in the compose file was tried on a throwaway
project and rejected: Compose v5 then stalls on every existing volume,
waiting to be told whether to recreate it.

**Outcome.** Done 2026-11-20.

- **`prune`**: scheduled daily (`PRUNE_INTERVAL`, a new setting — the
  plan named only `RUN_RETENTION_DAYS`, but every scheduled job reads an
  interval, and `0` is how a deployment turns it off), role `agents`,
  writes `ledger`, records its own run. Not declared idempotent, so no
  pipeline can include it.
- **It deletes families, in one statement.** A root is a finished `run`
  row older than the cutoff whose parent is not a `run` row (an
  event-triggered pipeline points at an `ingest_run`); it goes with all
  its children whatever their age; a family with any member `running` is
  kept whole. `ingest_run` and `agent_run` are never named — an observer
  step of a pruned pipeline stays in the observer's history.
- **A real gap found in invariant 4's test, and fixed.** It read delete
  targets with a regex over `repr(statement)`, which expected single
  quotes; Python's `repr` switches to double quotes when the SQL holds a
  `'`. So any DELETE with a quoted literal was invisible —
  `prune_done_queue_rows` was, and **a `DELETE FROM token_hours_data WHERE
  symbol = 'GNO'` would have passed** (demonstrated). It now reads the
  composed parts, sees all three deleting statements, and allows exactly
  the fill cache, `run_queue` and `run`.
- Tests: 15 unit (the job, its counts, the delete targets, the registry
  pins) and 9 integration against real SQL — old deleted, recent kept,
  `running` kept, a family with a step past the cutoff deleted whole, a
  family with a running step kept whole, an event-triggered pipeline
  pruned while its `ingest_run` stays, an `agent_run` step kept,
  `run_log` intact, a second prune deleting nothing, the job's own row
  surviving. Plus 33 for the guard. `make check` **2081 passed**, 11
  skipped (e2e); without Docker, 244 skipped and the guard's 33 pass.
- **On a real stack** (`cg-oak-1795159426`): `prune` ran at start-up
  (`deleted 0`); after seeding 40-day-old history and restarting the
  agents container, the scheduled run recorded `{"deleted": 4,
  "retention_days": 30}` — a lone old run and a pipeline with its two
  steps — and kept the family with a `running` step and the recent run.
- **Corrected while here:** step 2's README line said dead queue rows
  stay visible "however old", which I corrected to say they are pruned
  after `QUEUE_RETENTION_DAYS`. **That correction was itself wrong**, and
  step 4.1 found it: the only path to `dead` is the lease reaper, which
  never sets `finished_at`, and the prune matches on `finished_at`. Dead
  rows are never deleted. The original "however old" was right; the docs
  now say *why*, and the statement's unreachable `'dead'` clause is
  backlog `A7`.

**Found, not fixed:**

| | |
|---|---|
| ~~Dead queue rows are deleted after `QUEUE_RETENTION_DAYS` (7)~~ — **wrong, see above**: they are never deleted | The real finding is that `prune_done_queue_rows` names `'dead'` in a clause that cannot match it. Backlog `A7` |
| A family stuck `running` is never pruned | Correct to keep, but nothing ever closes it: the queue has a reaper for leases, the ledgers have none for a process that died mid-run |
| No `candlegraph prune [--dry-run]` | The only way to prune on demand is restarting the agents container. And `RUN_RETENTION_DAYS=1` is accepted and would delete almost everything at the next run |
| A pipeline started by another pipeline is pruned with its cause, as if it were a step; its own steps wait for the next day | The same owned-versus-caused ambiguity as step 2's display bug; latent, no spec triggers on another pipeline |
| The guard runs only through `make` | A raw `docker compose up` from the wrong checkout is not checked |
| The draft check sees uncommitted files only | A draft committed and then amended or rebased under a new id is not caught |
| A volume whose containers are gone has no recorded owner | Declined in the post-mortem, with the reason; `make stacks` lists such volumes as unattributable |

---

## Step 4.1 — Interim closeout of the pipeline work

Not in the original eight. Steps 1–4 finished everything this plan had to
say about **pipelines**; steps 5–7 are credits, a schedule and models. So
the pipeline work is concluded here rather than carried half-remembered
into the budget manager, and the loose ends from four steps are put in one
place.

**Goal.** Someone picking this up cold can find what pipelines do, how to
operate them, and what is knowingly missing, without reading four step
outcomes and three designs.

**Outcome.** Done 2026-11-20.

- **One register for everything flagged:** [`../backlog/DESIGN.md`](../backlog/DESIGN.md).
  Twenty-seven entries with stable ids (`A1`…`D2`), each with the trigger
  that should pull it in and where it was found, gathered from steps 2, 3
  and 4's *Found, not fixed* tables, the pipelines design §15, the
  2026-11-16 audit's nine open findings, the stage 2 archive and the
  2026-11-20 post-mortem. It also records **accepted behaviours** — the
  cold-start CRITICAL, GNO's coverage, a losing backtest — so they stop
  being rediscovered as bugs, and what has closed since.
- **A skill for operating pipelines**, `run-pipelines`: run one, watch the
  queue, read a family, find out why one did not run, and the half-dozen
  things that look broken and are not. `add-pipeline` stays the authoring
  procedure and now points at it.
- **A skill for arriving cold**, `orient`: the system in one page, the
  five commands that answer most questions, where to look for what, and
  the traps that cost a session. Written for a new session, which is how
  this repository is usually picked up.
- `AGENTS.md`, `CLAUDE.md` and `docs/README.md` updated: the backlog and
  the two skills are listed where someone would look for them.
- No code changed: `make check` **2081 passed**, 11 skipped (e2e).

**What is deliberately still open.** Everything in the backlog. The
nearest ones to this plan: `A3` (a queued run cannot be withdrawn) and
`B1` (`SPENDING_JOBS` is hand-kept) both want attention **before step 6**
schedules a pipeline that spends money.

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
