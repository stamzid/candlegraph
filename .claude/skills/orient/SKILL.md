---
name: orient
description: Get up to speed on candlegraph fast at the start of a session — what it is, what state the work is in, the commands that answer most questions, where to look for what, and the traps that cost a session. Use when starting fresh, picking the repository up after a break, or when asked "what is this" / "where do I start" / "what were we doing".
---

# Orient: candlegraph in one page

Read this, then one of the three files at the bottom. Not the whole
`docs/` tree — 27 design documents is an afternoon.

## What it is

A market-data service that turns hourly Uniswap v3 prices into something
you can reason about **and be held to**: it ingests into
TimescaleDB, computes statistics, signals and backtests from stored
history, attaches the news that was public at each bar, and records every
piece of work it does. Paper trading only; it runs on a laptop with no
paid service.

Three commitments explain most of the design:

1. **Deterministic first.** Every number comes from tested code. A model
   may phrase findings or pick a tool; it never produces a figure.
2. **Nothing looks ahead.** A signal on bar *t* earns bar *t+1*'s return;
   retrieval never returns text published after the bar.
3. **Everything that runs leaves a row.** One ledger shape for every job,
   scheduled or not (invariant 27).

## Where the work stands (2026-11-20)

- **Delivered:** ingestion, analytics, strategies and the backtester,
  agents (observer, janitor), the knowledge layer with cited
  explanations, the MCP server, and **stage 2, the harness** — YAML
  pipelines, a durable queue, an event dispatcher, traces.
- **Current plan:** `docs/rollout/ROLLOUT.md`, *"the harness, visible"* —
  putting stage 2 on screen. **Steps 1–4 and the 4.1 closeout are done**
  (loopback ports and a stack guard; a Pipelines dashboard; *Run now*
  that queues rather than executes; the `prune` job). **Step 5, the
  budget manager, is next.**
- **Known gaps live in one place:** `docs/backlog/DESIGN.md`, with stable
  ids. Check it before reporting something as broken — it also lists
  behaviours that look wrong and are intended.

Today: 12 registered jobs, 3 pipeline specs, 13 dashboards, 14 MCP tools,
5 actively polled tokens, 29 invariants, ~2,080 tests.

## The five commands that answer most questions

```sh
make stacks                    # which Docker stacks exist, and whose checkout each is
make runs ARGS='--all'         # what has run: every job, steps under their pipeline
make report                    # is the data any good (coverage, gaps, completeness)
make queue                     # what a trigger asked for, and what became of it
make jobs                      # every registered job: schedule, process, write surface
```

`make up` starts the stack; `make check` is everything CI runs. Commands
live only in the `Makefile` — if it is not a target, it is not a command.

## Where to look for what

| Question | Go to |
|---|---|
| How does the whole thing fit together? | `docs/overview/DESIGN.md` |
| Why is it built this way / what is next? | `docs/roadmap/DESIGN.md` |
| What are we doing right now? | `docs/rollout/ROLLOUT.md` (current plan, one step per session) |
| How do the stages stack up? | `docs/rollout/overview/DESIGN.md` |
| What is knowingly missing? | `docs/backlog/DESIGN.md` |
| The rules the build enforces | `AGENTS.md` — commands, layout, data model, 29 invariants, definition of done |
| How do I *do* X? | `.claude/skills/` — `run-stack`, `run-pipelines`, `run-agents`, `run-analytics`, `add-pipeline`, `add-agent`, `add-migration`, `add-token`, `backfill-and-verify`, `extend-knowledge-graph`, `release-check` |
| Why did something break before? | `docs/incidents/` — five post-mortems, all closed |

## Traps that cost a session

- **Compose projects are per checkout.** Several worktrees share one
  Docker daemon. Use `COMPOSE_PROJECT_NAME=cg-<worktree>-$(date +%s)` and
  your own ports; `make up/down/reset` refuse another checkout's stack
  (`make stacks` shows who owns what). `make down` deletes the volume.
- **Never apply a draft migration to your default stack.** Rename it
  before committing and that volume can never start again — it has
  happened twice. `make up` refuses while `alembic/versions` is dirty.
- **Integration tests skip without Docker.** A green `pytest` on a
  machine without it proves much less than it looks; check the skip count.
- **Low coverage is often correct.** No swaps in an hour means no row
  (invariant 19). GNO sits under the threshold on purpose.
- **A fresh stack's first observer report is CRITICAL**, and its first
  `hourly` run fails at `validate`. Both are the cold start, not a fault.
- **A losing backtest is usually the honest answer.** Read `excess` and
  costs before touching parameters.
- **UI arithmetic belongs in `ui/viewmodels.py`**, never in the Streamlit
  script, or it cannot be tested.
- **`.env` holds a real API key.** Never print, commit or copy it.

## How work is done here

- A **design document before the code**, with the rejected alternatives;
  its Status line must stay true.
- **One rollout step per session**, and the step records an **Outcome**
  including what it chose *not* to do. That honesty is why the backlog
  is believable.
- **A fault gets a post-mortem**, and its follow-ups gate the Status.
- A change is not done until `make check` passes, the tests are in the
  fastest tier that can prove it, and `AGENTS.md` / `README.md` / the
  matching design are updated — the definition of done in `AGENTS.md`.

## If you read nothing else

1. `AGENTS.md` — the working agreement, and the invariants that will
   fail your build.
2. `docs/overview/DESIGN.md` — the system on one page, with diagrams.
3. `docs/rollout/ROLLOUT.md` — what is being built right now, and what
   each finished step actually did.
