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
- `release-check` — the full pre-merge gate.

## Working here

- Run commands through `make`. If you find yourself typing a bare `uv run
  pytest …` or a raw `docker compose …`, check whether a target already exists;
  if the command is worth repeating, add one.
- Several tests exist specifically to pin down a past bug and say so in their
  docstring. If one fails, read the docstring before changing the test — the
  test is usually right.
- `.env` is gitignored and holds a real `GRAPH_API_KEY`. Never print it, commit
  it, or copy it into `.env.example`, a test fixture, or a log line.
- Integration tests need Docker. Without it they skip rather than fail, so a
  green `pytest` run on a machine without Docker does not mean the data layer
  was exercised — check for skips before declaring success.
- This repo is often checked out as several git worktrees at once. Compose
  deliberately has no fixed project name, so each worktree gets its own stack;
  do not add `name:` to `docker-compose.yaml`. If ports clash, set
  `POSTGRES_HOST_PORT` and `APP_HOST_PORT`.
