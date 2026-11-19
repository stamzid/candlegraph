# Rollout: none current

**Status:** no current rollout — 2026-11-19. Stage 2 (the harness) closed
today; its plan is archived at
[`archive/2026-11-15-stage-2-harness.md`](archive/2026-11-15-stage-2-harness.md).
The next plan is written here when it is agreed, not before.

## Carried forward from stage 2

The closeout's stack gate was not run, because Docker was not running in
the closing session, and that same session found one test that fails
rather than skips without Docker. Do both before the next plan starts, so
it does not inherit an unverified baseline:

- [ ] The `release-check` skill against a real stack: `make up`,
      `make e2e` with 0 skips, `report --strict` (GNO and SHIB below
      coverage is expected), and one ingest followed by exactly one
      `hourly` run with trigger `event` in `make runs ARGS='--job hourly'`.
      `make check` with the integration tier **running**, not skipped.
- [ ] Make `test_timescale.py::test_a_server_that_does_not_preload_timescaledb_is_refused_before_any_ddl`
      skip without Docker, like the rest of the integration tier. It
      starts its own `PostgresContainer` and catches only `ImportError`,
      so on a machine with Docker stopped `make check` fails on it
      (seen 2026-11-19) instead of skipping.

## Candidates for the next plan, in the order recommended

The direction is the one in [`../overview/DESIGN.md`](../overview/DESIGN.md)
§"Where it is going": **the harness operated from the UI** — see what ran,
start what is allowed, and change what a person is meant to change,
without a terminal. Stage 2 built everything that page would read; none
of it is on screen yet except the stage strip.

| # | Candidate | Size | Why here | Design |
|---|---|---|---|---|
| 1 | **A Pipelines dashboard page** — pipeline runs with their step strip, the queue (`queued`/`claimed`/`dead`), the dispatcher's last tick, and a *Run now* button per spec (behind a confirmation for `daily`, which spends) | M | The first piece of "the harness in the UI", and pure reading of tables that exist. Follows step 1's panel pattern; logic in `viewmodels.py` | pipelines §9.3 already places the model picker on this page (candidate 5); the read-only panels need no new design |
| 2 | **The `run` prune job** | S | Design §16 answer 5 settled it and it was never built; small, and it should exist before pipelines run for months | pipelines §16 |
| 3 | **The budget manager** — two credit pots, changeable live, a warning agent | M | Must come **before** `daily.yaml` gets a schedule: an automatic fetch today draws from the same pot a person needs | [`../knowledge/budget-manager/DESIGN.md`](../knowledge/budget-manager/DESIGN.md) (proposed) |
| 4 | **Schedule `daily.yaml`** | S | One `schedule:` trigger. Closes the embed backlog in the running system rather than only in the spec | — |
| 5 | **The chat-model seam and the per-step model picker** | M | Deferred from step 4; the picker belongs on the page from 1. Needed by the analyst | [`../harness/models/DESIGN.md`](../harness/models/DESIGN.md) (proposed) |
| 6 | **Authentication and backups** (audit findings 8 and 9) | M | Not needed on a laptop; required before the UI can start runs on anything reachable from a network | [`../audit/2026-11-16-system-review/FINDINGS.md`](../audit/2026-11-16-system-review/FINDINGS.md) |

1–4 are one natural rollout ("the harness, visible"); 5 is its own; 6
gates any deployment beyond a laptop. Stage 3 (traditional markets) is
the roadmap's next *stage* and does not depend on any of these, but the
analyst ([`../harness/analyst/DESIGN.md`](../harness/analyst/DESIGN.md))
needs both stage 3 and 5.

**Deferred from stage 2 and not proposed yet**, each with its reason in
the pipelines design §15: step-level resume, metrics with the circuit
breaker that reads them, and hard (non-cooperative) timeouts. None is
worth building until a pipeline is long or flaky enough to show the need.
