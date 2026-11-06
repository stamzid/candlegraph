# Rollout: none current

**Status:** none current — 2026-11-06. Stage 1 shipped and its plan is
archived; no next rollout has been written yet.

This directory holds **one current rollout at a time**: a stage's work
broken into turn-sized steps with a progress table, updated at the end of
every working session. When someone refers to "the rollout", "the plan" or
"step N", they mean this file — and right now there is nothing in it,
which is itself the answer.

## What just shipped

| Rollout | Stage | Outcome |
|---|---|---|
| [`archive/2026-11-06-stage-1-knowledge-graph.md`](archive/2026-11-06-stage-1-knowledge-graph.md) | 1 · Knowledge graph and signaller | All ten steps, 2026-10-29 → 2026-11-06. Per-step outcomes in the file. |

## What comes next

[`../roadmap/DESIGN.md`](../roadmap/DESIGN.md) is authoritative, and the
next stage by its ordering is **stage 0, descriptive statistics, deeper** —
the `active` flag in `config/tokens.yaml` and the `FillPolicy` gap-filling
seam, both small and both inherited by stages 3 and 4. After that, stage 2,
the harness.

## Writing the next one

Copy the archived file's shape, which worked:

- A **progress table** first — step, size, what it depends on, status — so
  a session can start by reading one row.
- Per step: **Goal**, **Read first** (the design documents the step
  executes), **Touches**, **Watch for**, and **Done when** as a command or
  an observable behaviour.
- An **Outcome** paragraph written when the step lands, including what was
  *not* built and why. Several of stage 1's most useful lines are of that
  kind.
- Say which steps may be combined in one session, and which must not.
  Stage 1's step 1 was the infrastructure change and was verified alone,
  for the reason in `docs/incidents/2026-10-28-stack-would-not-start/`.

A rollout never changes a design silently: a step that forces a decision
changes the design document in the same step, Status line included.
