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
sleep 25
curl -s localhost:8000/status | python3 -m json.tool    # "status": "ok"
make logs | grep "Ingested"                              # one line per token
make e2e                                                 # 9 assertions, 0 skips
```

`make e2e` reporting `skipped` means `CANDLEGRAPH_E2E_URL` was unset — the
target sets it, so a skip means the stack is not up.

## 3. The checklist

From `AGENTS.md`, confirm each one explicitly rather than assuming:

- [ ] `make check` passes.
- [ ] New behaviour has tests in the fastest tier that can prove it.
- [ ] Schema changes ship with a migration that has a working `downgrade`.
- [ ] New configuration is in `.env.example`, with a comment.
- [ ] `make sdl` run if the GraphQL schema changed (a test enforces this).
- [ ] `AGENTS.md` updated if a command, invariant, or layout entry changed.
- [ ] `README.md` updated if user-facing behaviour changed.

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
