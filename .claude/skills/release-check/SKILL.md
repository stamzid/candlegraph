---
name: release-check
description: Run the full pre-merge gate for candlegraph — formatting, lint, types, all test tiers, the compose stack, and the definition-of-done checklist. Use before opening a PR, merging, or claiming work is finished.
---

# Release check

Run these in order and stop at the first failure.

## 1. Static checks and tests

```sh
make check
```

That is `fmt-check`, `lint`, `typecheck`, `pytest`. Read the summary line: the
e2e tier **skips** unless a stack is running, and the integration tier skips
if Docker is unavailable. A run reporting many skips has not proven much.

```sh
make cov        # coverage should stay at or above ~95%
```

## 2. The stack really works

The exit criterion for this repo is a clean clone coming up and loading data.

```sh
make down && make up
sleep 30
curl -s localhost:8000/status | python3 -m json.tool    # "status": "ok", "data_fresh": true
make runs                                                # one succeeded run
make e2e                                                 # 0 skips
docker compose exec -T worker python -m candlegraph report --strict
```

`make e2e` reporting `skipped` means `CANDLEGRAPH_E2E_URL` was unset — the
target sets it, so a skip means the stack is not up.

The harness is part of the gate. Every committed spec validates, and the
hourly pipeline really runs, both by hand and because ingest finished:

```sh
make check-pipelines                     # every spec in pipelines/, running nothing
make run ARGS='hourly'                   # outcome succeeded or partial; never failed on a stack with data
make runs ARGS='--job hourly'            # a trigger=manual run AND a trigger=event one, steps under each
make queue                               # nothing `dead`; a dead row needs reading, not deleting
```

`partial` from `hourly` is normal when GNO or SHIB is under coverage
(`validate` grades that `partial`, not `failed`). A `failed` `validate`
on a stack with rows is a real failure.

`report --strict` fails on two different things; read which one:

- **`MISSING (no rows at all)`** is a real failure for an **active** token
  (inactive tokens are never polled and are not listed here).
- **`BELOW 80% COVERAGE`** for GNO or SHIB is expected: they are in the
  active set *because* their history is gappy (invariant 19). Check
  `make report` for their average hourly volume before calling it a bug,
  and do not lower `MIN_EXPECTED_COVERAGE` to make the gate pass.

## 3. The checklist

From `AGENTS.md`, confirm each one explicitly rather than assuming:

- [ ] `make check` passes.
- [ ] New behaviour has tests in the fastest tier that can prove it.
- [ ] Schema changes ship with a migration that has a working `downgrade`.
- [ ] New configuration is in `.env.example`, with a comment.
- [ ] `make sdl` run if the GraphQL schema changed (a test enforces this).
- [ ] `make check-pipelines` passes if a spec or a step job changed.
- [ ] `AGENTS.md` updated if a command, invariant, or layout entry changed.
- [ ] `README.md` updated if user-facing behaviour changed.
- [ ] Every **Status** line under `docs/` still tells the truth, including
      `docs/overview/DESIGN.md` if a process or a writer changed.

## 4. Nothing secret is staged

```sh
git status --short
git diff --cached | grep -iE "api[_-]?key|secret|password|bearer " || echo "clean"
```

`.env` must stay untracked. `.env.example` must keep `GRAPH_API_KEY=` empty.

## 5. Clean up

```sh
make down       # drops the volume too
```
