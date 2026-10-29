# CLAUDE.md

@AGENTS.md

Read `AGENTS.md` first — it holds the commands, layout, invariants, and the
definition of done. This file adds only what is specific to Claude Code.

## Skills

`.claude/skills/` has the repeatable procedures. Prefer invoking one over
reconstructing the steps:

- `run-stack` — bring the stack up, check health, read logs, tear down.
- `add-token` — add a token to `config/tokens.yaml` and verify it ingests.
- `add-migration` — create, apply, and verify an Alembic revision.
- `backfill-and-verify` — load history and read the data-quality report.
- `run-analytics` — volatility, portfolios, signals, backtests, and the UI.
- `run-agents` — run the observer and the janitor, read their reports, and
  tell a real finding from an expected one.
- `add-agent` — add a new LangGraph graph that follows the framework.
- `release-check` — the full pre-merge gate.
- `extend-knowledge-graph` — stage 1 is designed, not built: the order to
  build it in, and how to add a source, an entity, or a retriever to it.

`docs/` holds the design documents, arranged the same way — one directory per
topic, with a `DESIGN.md` inside and a flowchart in most of them:

- `docs/roadmap/` — delivered stages, next stages, exit criteria. Read this
  before proposing new work; the order is reasoned, not arbitrary.
- `docs/storage/timescaledb/`, `docs/storage/table-placement/` — read before
  touching the Postgres image, a migration, or the hourly tables.
- `docs/agents/framework/`, `.../observer/`, `.../janitor/` — read before
  touching `candlegraph/agents/`.
- `docs/knowledge/` — the stage 1 design (graph, vector store, embeddings,
  Tavily, MCP). Status is *proposed*; the code does not exist yet. Read
  before starting any of it, and keep the Status lines truthful as it lands.
- `docs/rollout/ROLLOUT.md` — **the current rollout plan.** When the user
  refers to "the rollout", "the plan", or "step N", this is the document:
  find the step, read its "Read first" pointers, do it, and update the
  progress table in the same change. One current rollout at a time;
  finished ones are archived under `docs/rollout/archive/`.
- `docs/incidents/` — post-mortems. Read the two from October 2026 before
  changing the database image, a migration id, or a compose health check.

## Working here

- Run commands through `make`. If you find yourself typing a bare `uv run
  pytest …` or a raw `docker compose …`, check whether a target already exists;
  if the command is worth repeating, add one.
- Several tests exist specifically to pin down a past bug and say so in their
  docstring. If one fails, read the docstring before changing the test — the
  test is usually right.
- `.env` is gitignored and holds a real `GRAPH_API_KEY`. Never print it, commit
  it, or copy it into `.env.example`, a test fixture, or a log line.
- Ingestion runs in the `worker` service, not the API. `make logs` shows the
  API; `make logs-worker` shows ingestion. A question about missing data is
  almost always answered by `make runs` and `make report`, not by the API log.
- A losing backtest is usually the honest answer, not a bug to tune away.
  Check `excess` (versus buy and hold) and `costs paid` before touching
  parameters; see the `run-analytics` skill.
- The UI's logic belongs in `candlegraph/ui/viewmodels.py`, never in the
  Streamlit script — that is what makes it testable.
- Low coverage in `make report` is frequently correct — a token with no swaps
  in an hour has no row. Check average hourly volume before calling it a bug.
- Integration tests need Docker. Without it they skip rather than fail, so a
  green `pytest` run on a machine without Docker does not mean the data layer
  was exercised — check for skips before declaring success.
- The database is TimescaleDB, and `token_hours_data`, `pool_hours_data` and
  `signal` are hypertables (invariant 24). Two consequences worth holding in
  mind: any unique index you add to one of them must contain
  `period_start_unix`, and a new table needs a placement decision recorded in
  `docs/storage/table-placement/DESIGN.md` before its migration is written.
  Do not reach for compression or retention policies — invariant 4 forbids
  the second and the design document explains why the first waits.
- The observer's findings follow the same rules you would: a gap on a thin
  token is not a finding (invariant 19), drift is a warning for a human to
  read and not an alarm, and "not enough bars" is `INFO`, never a guess. If
  the observer flags something, check `make runs` and `make report` before
  assuming the observer is wrong — and before assuming it is right.
- The janitor never rewrites a test or a type to make a check pass. If it
  reports `typecheck` or `test-unit` failing, that is work for a person.
  Run it locally with `make janitor` (no PR; the fixes stay in your tree).
- Agent graphs are tested by calling nodes with hand-built state and by
  running the compiled graph against `FakeShell` / `FakeDatabase`. A new
  node needs no database to be tested; if it seems to, the I/O is in the
  wrong place.
- This repo is often checked out as several git worktrees at once. Compose
  deliberately has no fixed project name, so each worktree gets its own stack;
  do not add `name:` to `docker-compose.yaml`. If ports clash, set
  `POSTGRES_HOST_PORT` and `APP_HOST_PORT`.
- Two worktrees whose directories share a basename also share a Compose
  project, and therefore a database volume. The symptom is `alembic` failing
  with `Can't locate revision identified by <hash>` — a revision from the
  *other* branch. `make down` would delete their data, so do not reach for it
  reflexively: run your own stack under an explicit
  `COMPOSE_PROJECT_NAME=<something-unique>` with distinct host ports instead.
