# Rollout: stage 0 — descriptive statistics, deeper

**Status:** current — opened 2026-11-06, step 1 of 4 done. This is **the**
current rollout: when someone says "the plan" or "step N", they mean this
file. Finished rollouts move to [`archive/`](archive/).
**Designs this executes:** [`../roadmap/DESIGN.md`](../roadmap/DESIGN.md)
§0 — in particular the gap-filling decision table and its four numbered
rules. Decisions live there; this file only sequences them. A step that
forces a decision changes the design document in the same step, Status
line included.
**Operating procedure per step:** the `run-analytics` skill for anything
touching numbers, `release-check` before calling a step done.

## Progress

| # | Step | Size | Depends on | Status |
|---|---|---|---|---|
| 1 | Descriptive display: audit the UI against the analytics layer and close the gaps | M | — | **done** 2026-11-06 |
| 2 | `FillPolicy` seam: the causal estimate anything a strategy consumes may use | M | — | todo |
| 3 | Linear interpolation estimator, display-only, capped and flagged | S | 2 | todo |
| 4 | Kalman / Brownian-bridge estimator, with the `synthetic` flag through `to_wide` | L | 2, 3 | todo |

**Combining steps in one session:** 3 follows 2 naturally and the two can
share a session if 2 lands early. **Do not combine 4 with anything** — it
introduces an estimated parameter per token and is the one step where a
wrong answer is invisible rather than loud.

**Not in this rollout.** The `active` flag in `config/tokens.yaml` (the
other half of the roadmap's stage 0) is independent of all four steps
above and can land in any session; it is tracked on the roadmap, not here.

The exit criterion for the whole stage (roadmap): `candlegraph compare
WETH WBTC` prints correlation, beta and relative performance over aligned
bars *(already met)*; the stack ingests exactly the five active tokens;
and the gap-filling policy exists behind a `FillPolicy` with a test
proving a strategy's signals are unchanged by it.

---

## Step 1 — Descriptive display

**Goal.** Before anything fills a gap, the UI should be able to *show*
one. Audit the Streamlit dashboards against what the analytics and quality
layers already compute, and close the gaps that audit finds — with no
change to any number, no new estimator, and no `FillPolicy`.

**Read first.** `docs/roadmap/DESIGN.md` §0 (rule 2: descriptive display
may interpolate, *with filled bars flagged* — which presupposes a surface
that distinguishes them), invariants 6, 11 and 19.

**Touches.** `candlegraph/ui/viewmodels.py`, `candlegraph/ui/app.py`,
`tests/unit/ui/`, README's UI section.

**Watch for.** The temptation to fix a finding by computing something new
in `app.py`. Every number belongs in `viewmodels.py` (invariant 18), and
the Streamlit script re-runs on every widget change, so an unguarded read
there runs on every click.

**Outcome (2026-11-06).** Done. The audit found five things the layers
below compute and the screen never showed:

1. **The data-quality layer was nearly invisible.** `quality.py` produces
   per-token `Gap` runs, `longest_gap`, `missing_hours`, `violations`,
   field `completeness` and `missing_tokens`; the UI surfaced exactly one
   derived number, a coverage bar.
2. **Compare showed 5 of the 12 numbers `candlegraph compare` prints** —
   Spearman, both total returns, both volatilities and both Sharpes were
   missing, so a reader could not see which leg carried the return.
3. **Token dropped `bandwidth` and `percent_b`**, both already returned by
   `bollinger_bands`.
4. **No gap was visible on any chart.** `to_wide` reindexes onto a
   complete hourly grid *specifically* so gaps show as `NaN`, and then the
   display forward-filled without saying which bars were carried.
5. **No per-token freshness.** `first`/`last` were computed, never shown.

What landed: a new **Quality** dashboard (gap map, gap-length
distribution, field completeness, per-token coverage and staleness,
longest gaps, impossible rows), gap shading and a coverage figure on the
Token candles, band width and %B, and the seven missing Compare numbers
as a per-leg table. `build_quality_view` calls the same
`quality.build_report` the CLI and the `data_quality_report` MCP tool
call, and a unit test compares the screen's per-token row against
`report_dict` field by field, so a fourth shape cannot appear.

What was **not** built, deliberately: no filling of any kind. The page
shades a gap and counts it; it does not interpolate across one. That is
step 3's job, and it now has a surface to be judged on. Nothing in
`analytics/` changed, so every number on every other page is unmoved.

`observed_gaps` reuses `quality.group_missing_hours` rather than
re-deriving what counts as one gap, which is why the Token page and
`candlegraph report` cannot disagree about a gap's length.

---

## Step 2 — The `FillPolicy` seam

**Goal.** A named seam in `analytics/prices.py` for "how do we resolve an
hour with no row", with the existing `ffill`/`drop`/`none` as its first
members and the causal rule written down: anything a strategy or backtest
consumes gets at most a **causal** estimate, never interpolation.

**Read first.** `docs/roadmap/DESIGN.md` §0, decision rules 1 and 3;
invariants 15–17.

**Watch for.** The look-ahead is the whole point. A filled value must stay
distinguishable from an observed one, and the test that matters is the one
proving a strategy's signals are **unchanged** by the policy — that is the
stage's exit criterion, not a nice-to-have.

**Done when.** `FillPolicy` exists as a named seam with a no-look-ahead
test, and `candlegraph backtest` produces identical results with it off
and on.

---

## Step 3 — Linear interpolation, display only

**Goal.** A straight line between the last real bar before a gap and the
first real bar after it, capped the way `ffill` is today, used by
descriptive surfaces only and flagged so the UI renders filled bars
differently from observed ones.

**Read first.** `docs/roadmap/DESIGN.md` §0, decision rule 2 and the
comparison table's **Volatility** and **Correlation** rows.

**Watch for.** A straight line has zero local variance, so every rolling
standard deviation overlapping a filled stretch is **understated** —
Bollinger bands tighten and z-scores inflate. Say so on screen rather than
quietly shipping a prettier chart. Beyond the cap the line is fiction and
the hole stays. Pairwise comparison and the quality report keep filling
nothing (rule 1); the Quality dashboard from step 1 must keep reporting
the gap as a gap.

**Done when.** The Token chart can draw interpolated segments, visibly
distinct from observed bars, and `candlegraph compare` and
`candlegraph report` are provably unaffected.

---

## Step 4 — Kalman / Brownian-bridge estimator

**Goal.** A model-based fill whose *filter* form is causal and therefore
the only one a strategy may consume, with per-token volatility estimation
and the `synthetic` flag plumbed through `to_wide`.

**Read first.** `docs/roadmap/DESIGN.md` §0, the full comparison table and
decision rule 4.

**Watch for.** The smoother uses both endpoints and is **not** safe for a
strategy; only the filter is. The bridge exists to preserve the variance
budget across a gap, so a test that the filled stretch does not collapse
local volatility is the one that proves it works at all. An estimated
parameter per token is new in this codebase — the honest answer when it
cannot be estimated is to decline to fill, not to guess.

**Done when.** The estimator is a `FillPolicy` member, the default
behaviour has not moved, `synthetic` reaches every consumer of `to_wide`,
and a strategy fed filled bars produces signals identical to one fed the
gaps.

---

## Standing rules for every step

- `make check` green before a step is called done; integration skips are
  not green (check for them).
- The UI's arithmetic lives in `viewmodels.py`, never in the Streamlit
  script (invariant 18).
- Nothing filled is ever written to the database. Filling is a read-time
  policy; `token_hours_data` keeps emitting no row for an hour with no
  swaps, and coverage stays falsifiable (invariants 6 and 11).
- A filled value stays distinguishable from an observed one, at every
  layer that carries it.
- Update the progress table above at the end of each session, including
  "partial" with a note, so the next session starts from the truth.
