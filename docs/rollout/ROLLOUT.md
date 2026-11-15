# Rollout: none current

**Status:** none current — 2026-11-15. The harness pre-work is finished
and archived. No next rollout has been written.

This directory holds **one current rollout at a time**. When someone says
"the rollout", "the plan" or "step N", they mean this file. Right now it
is empty, and that is the answer.

## What just shipped

| Rollout | What it delivered |
|---|---|
| [`archive/2026-11-11-harness-prework.md`](archive/2026-11-11-harness-prework.md) | One runner for every scheduled job; ledger rows before and after every run; `trigger`, `outcome`, `parent_run_id`; the generic `run` ledger and `run_log`; the scheduled news fetch; invariant 27. Four steps, 2026-11-12 → 2026-11-15. |

Earlier plans (stages 1, 0 and 6) are in [`archive/`](archive/).

## Candidates for the next rollout

[`../roadmap/DESIGN.md`](../roadmap/DESIGN.md) decides the order. By it,
the next stage is **2, the harness**. In the order they would pay off:

### 1. Split the four large dashboard pages (S, do first)

`_news_page`, `_strategy_page`, `_agents_page` and `_portfolio_page` are
each one long function mixing layout, formatting and control flow. The
other pages are already split into panels. Do this before stage 2 adds a
pipelines page, so the new page copies the good pattern. While there,
draw the ingest stage strip on the Agents page — the data has been in
`ingest_run.detail.stages` since step 2 of the pre-work.

### 2. Stage 2, the harness (L)

Start by writing `docs/harness/pipelines/DESIGN.md` — the YAML spec, the
pipeline graph, how a pipeline run owns its child runs through
`parent_run_id`. No code before that document. The runner it builds on
is [`../harness/runner/DESIGN.md`](../harness/runner/DESIGN.md).

### 3. A scheduled `embed` job (S, needs a decision)

The scheduled news fetch stores chunks that nothing embeds until
`make embed` runs or an `explain` drains them. The observer reports the
backlog, so it is visible. Registering `embed` is about twenty lines
now; the open question is whether the embedder should run on a timer in
the `agents` container.

### 4. The portfolio posterior (M) and continuous aggregates (S)

Both unchanged from the previous list. The posterior belongs with
stage 4's model work; continuous aggregates belong with stage 2, which
is what would refresh them.

### Further out, designed but not planned

The analyst ([`../harness/analyst/DESIGN.md`](../harness/analyst/DESIGN.md))
and its multi-day research loop
([`../harness/research-loop/DESIGN.md`](../harness/research-loop/DESIGN.md)).
Their first steps (a pure DCF module; the loop's guard and stop rules as
pure functions) need nothing from stages 3 or 4 and could be a small
rollout of their own after stage 2.
