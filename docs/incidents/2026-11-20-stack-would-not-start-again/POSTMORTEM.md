# Post-mortem: a stack would not start again — a volume and the code disagree

**Status:** closed — 2026-11-20. Two guards added and tested; one
follow-up declined with its reason; the exact revision behind this
occurrence could not be recovered (see "What we do not know").
**Severity:** local development, no data that matters lost. The `elm`
worktree's default stack would not start; it ran only under an invented
`COMPOSE_PROJECT_NAME`, on an empty volume. Engineering time, and a third
recurrence of a fault class that two earlier fixes only explained.
**Related:** [`../2026-10-28-stack-would-not-start/POSTMORTEM.md`](../2026-10-28-stack-would-not-start/POSTMORTEM.md),
[`../2026-10-29-compose-startup-robustness/POSTMORTEM.md`](../2026-10-29-compose-startup-robustness/POSTMORTEM.md).

## Summary

On 2026-11-20 the `elm` worktree's `make up` failed with the preflight's
"the database is stamped at revision `<hash>`, which does not exist in this
checkout". The same class of failure has now happened three times:

| Date | Volume | Stamped revision | Cause |
|---|---|---|---|
| 2026-10-28 | `elm_pgdata` | `d2f8b6a41c35` | a draft of the `agent_run` migration, applied, then renamed to `d8f2a6c41e07` before commit — **proven** |
| 2026-11-19 | `cg-oak-otel-…_pgdata` | `9b3d7e21c4f5` | a draft of the aggregates migration, applied, then renamed to `9d4b7a2c6e15` before commit — **proven** |
| 2026-11-20 | `elm_pgdata` (at the time of the failure) | not recovered | consistent with either route below; not proven |

The 2026-10-28 fix made the error **name** its two possible causes, and it
did — but nothing **prevented** either one. This incident adds the two
preventions.

## The two routes to an unreachable volume

A Compose volume outlives the code that migrated it. It becomes
unstartable when it was migrated by a revision that the checkout now
starting it does not have:

1. **A draft revision on a long-lived volume.** A migration is applied to
   the default stack while it is still a draft; it is renamed or rewritten
   before commit; the volume is stamped with an id that exists in no
   commit. Proven twice.
2. **Another checkout's code on this checkout's volume.** The project name
   is the only thing that separates two checkouts' stacks. When it is shared
   — two worktrees with one basename (the case `CLAUDE.md` already warned
   about), or a project name reused across sessions — `make up` in one
   checkout replaces the other's containers with its own image and runs its
   own migrations against the other's database. The evidence that names are
   reused across worktrees is on the daemon: stacks called `elm-test` and
   `cg-elm-…` were started **from the oak directory**, and volumes
   `elm-test_pgdata`, `elm-1_pgdata` and `oakha_pgdata` are left over.

## What the investigation established

- **Not branch divergence.** `main`, the `elm` branch and the `oak` branch
  were at one commit (`a68b239`) with the same fourteen migrations, so no
  committed checkout was "behind" another.
- **Not a shared image.** The compose file tags the image
  `candlegraph:${COMPOSE_PROJECT_NAME}`; with the variable unset, Compose
  still resolves it to the project name (`candlegraph:oak`). The 2026-10-29
  fix holds.
- **Not shared default project names.** The three checkouts resolve to
  `candlegraph`, `elm` and `oak`.
- **`elm_pgdata`'s state, read without touching it**: the volume was copied
  read-only into a scratch volume and a network-less Postgres was started on
  the copy. At 06:57:04 UTC it was stamped `9d4b7a2c6e15` — the committed
  head — with data up to 06:00 the same day. A raw-file `grep` had earlier
  found neither id; it ran as an unprivileged user that could not read every
  data file, and was a bad test rather than evidence.
- **Thirteen seconds later the elm stack was rebuilt** by someone working in
  that worktree (its migrator log runs from `initial_schema`), so the
  failure was no longer present to inspect.

## What we do not know

The revision `elm_pgdata` was stamped with **when the failure was seen**. By
the time it could be read it was consistent, and then it was rebuilt. We are
not claiming which route this occurrence took. Both guards below apply to
it either way.

## What changed

| Change | Closes | Test |
|---|---|---|
| `scripts/stack-guard.sh`, run first by `make up`, `make down`, `make reset` and `make otel-up`: **refuses** when the project's containers were started from a different checkout, names that checkout and whether it still exists, and gives a command that would not collide | route 2 — and `down`/`reset` deleting another checkout's database, which `CLAUDE.md` warned about but nothing stopped | `tests/unit/test_stack_guard.py` (fake `docker` on PATH), including that every one of the four targets calls the guard **first** |
| The same guard **refuses `up` on the default stack while `alembic/versions` has uncommitted changes**, and points at a throwaway project. An explicitly named project passes; `down` and `reset` are never blocked, because rebuilding is the remedy | route 1, at the moment it happens | the same file (fake `git`) |
| `make stacks`: every candlegraph stack on the daemon, the checkout it belongs to, and the volumes nobody owns any more | seeing a collision before acting on it | the same file |
| `FORCE=1` on any guarded target | a deliberate takeover — the other checkout is gone, or the revision is final | the same file |

The owner is read from `com.docker.compose.project.working_dir`, which
Compose already writes on every container. Verified against the live `elm`
stack from the oak checkout, calling only the guard: `up` and `down` were
refused with the elm path named, and `FORCE=1` was the only way through.

## Rejected, and why

- **Labelling the volume with its owning checkout in the compose file.**
  Tested on a throwaway project first: with the label added, Compose v5
  treated the existing, unlabelled volume as changed and **stopped to wait
  for an answer on stdin** — its question is whether to recreate the volume,
  that is, delete the database. Shipping it would have stalled every
  existing stack's `make up`, or invited someone to answer yes.
- **A per-path default project name** (`<basename>-<hash of the path>`).
  It would end basename collisions for good, but it renames every existing
  default project, orphaning `candlegraph_pgdata`, `elm_pgdata` and
  `oak_pgdata` — weeks of history in the main checkout — on the next
  `make up`. The guard gets the protection without moving anyone's data.

## Follow-ups

- **Declined: attributing a volume whose containers are gone.** Compose
  records ownership on containers, not volumes, and the in-file label is
  rejected above. `make down` removes containers and volume together, so a
  volume without containers only arises from a raw `docker compose down`
  without `-v`. `make stacks` lists such volumes as unattributable rather
  than guessing. Revisit if it is ever the cause of a fault.
- **Process, written into `CLAUDE.md` and the `add-migration` skill:** try a
  draft revision on a throwaway `COMPOSE_PROJECT_NAME`, never on the default
  stack; the guard now enforces the default-stack half.
