# Rollout: none current

**Status:** none current — 2026-11-10. Stage 6 shipped and its plan is
archived, and stage 0 closed with it; no next rollout has been written.

This directory holds **one current rollout at a time**: a stage's work
broken into turn-sized steps with a progress table, updated at the end of
every working session. When someone refers to "the rollout", "the plan"
or "step N", they mean this file — and right now there is nothing in it,
which is itself the answer.

## What just shipped

| Rollout | Stage | Outcome |
|---|---|---|
| [`archive/2026-11-10-stage-6-simulation.md`](archive/2026-11-10-stage-6-simulation.md) | 6 · Simulation and strategy evaluation | All three steps, 2026-11-09 → 2026-11-10. Per-step outcomes in the file. |
| [`archive/2026-11-08-stage-0-descriptive-deeper.md`](archive/2026-11-08-stage-0-descriptive-deeper.md) | 0 · Descriptive statistics, deeper | Four steps, plus the `active` flag which closed the stage on 2026-11-10. |

Both stages are **delivered**: every clause of both exit criteria is met,
including the two that were outstanding a session ago — the stack now
ingests exactly the five active tokens, and `forecast --posterior` prints
a wider interval with R-hat reported.

## What comes next

[`../roadmap/DESIGN.md`](../roadmap/DESIGN.md) is authoritative. By its
ordering the next stage is **2, the harness**, and that is the right call:
stages 3, 4 and 5 all assume something schedules and traces their work,
and the harness is what three separate pieces of this codebase have now
each half-built for themselves.

Four candidates, in the order they would actually pay off.

### 1. Pre-work for the harness: collapse the three schedulers (S, do first)

The harness is a large stage, and a third of it already exists three
times. `agents/scheduler.py` runs APScheduler for the observer; the API
starts its own background ingest loop so startup never blocks on the
subgraph (invariant 7); `ingestion/worker.py` is a third loop in its own
process. They have different retry behaviour, different logging and no
shared notion of "a run".

Unifying those into one *runner* abstraction before the harness is
written is the difference between the harness being a new layer and the
harness being a rewrite. It is also independently useful: it would give
`ingest_run` and `agent_run` a common shape, which stage 2's event bus
wants anyway.

**Why first:** it is small, it reduces the harness's size, and it is the
only item here that gets *harder* if the harness lands on top of it.

### 2. Maintenance: the four oversized dashboard pages (S)

`_news_page` (157 lines), `_strategy_page` (156), `_agents_page` (141)
and `_portfolio_page` (140) are each a single function doing layout,
formatting and control flow. `_quality_page`, `_token_page` and
`_simulation_page` have been split into panels and are markedly easier to
change; these four have not.

Not urgent, and worth doing *before* stage 2 adds a pipelines dashboard
rather than after, so the new page copies the good pattern.

### 3. The portfolio posterior (M)

The one thing stage 6 promised and deferred, with the reason recorded in
`docs/analytics/simulation/DESIGN.md` §3: re-optimising a mean-variance
portfolio once per posterior draw is a different shape of work from a
two-parameter forecast, and shipping it hurried inside a step that was
also closing stage 0 was the worse option. The seam is ready — `sample()`
takes any log density and `optimise_portfolio` already takes a returns
frame.

It is the natural answer to Markowitz's known weakness, which
`shrinkage` currently papers over, so it belongs with stage 4's model
work if it does not get its own slot.

### 4. Continuous aggregates (S, deferred twice)

Explicitly deferred from the TimescaleDB migration and named again in
stage 2. Daily and weekly rollups of `token_hours_data` would make the
Market dashboard's long windows cheap. Worth doing *with* the harness
rather than before it, since the harness is what would refresh them, and
worth measuring first: nothing has yet been shown to be slow.

### Not recommended yet

**Stage 3 (traditional markets) before the harness.** It needs a second
ingestion adapter, and a second adapter wants the unified runner from
item 1. Doing it first means writing the adapter twice.

**Compression policies.** Invariant 4 forbids retention outright and
`docs/storage/timescaledb/DESIGN.md` says compression waits for a
measured problem. There still is not one.

## Writing the next one

Copy the archived files' shape, which has worked three times:

- A **progress table** first — step, size, what it depends on, status — so
  a session can start by reading one row.
- Per step: **Goal**, **Read first** (the design documents the step
  executes), **Touches**, **Watch for**, and **Done when** as a command or
  an observable behaviour.
- An **Outcome** paragraph written when the step lands, including what was
  *not* built and why. The most useful lines in all three archives are of
  that kind.
- Say which steps may be combined in one session, and which must not.

Two habits from stage 6 worth keeping. **Write the falsifying test
first** when a step's deliverable is an equality — the discrete-event
reconciliation was specified that way and found two bugs on its first
run, both invisible from the other side. And **put a sizing warning and
a cut line in the step itself**, so a session that runs long knows which
half to drop without having to re-derive the priority.

A rollout never changes a design silently: a step that forces a decision
changes the design document in the same change, Status line included.
