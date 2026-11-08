# Rollout: stage 0 — descriptive statistics, deeper

**Status:** complete — opened 2026-11-06, all four steps done 2026-11-08;
archived 2026-11-09. This is a record of how the stage was built, not a
plan: the decisions live in `docs/roadmap/DESIGN.md` §0, and
[`../ROLLOUT.md`](../ROLLOUT.md) says what the current rollout is.

Stage 0's one remaining roadmap item, the `active` flag in
`config/tokens.yaml`, was never part of this rollout (it is independent of
all four steps) and is still tracked on the roadmap.
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
| 2 | `FillPolicy` seam: the causal estimate anything a strategy consumes may use | M | — | **done** 2026-11-07 |
| 3 | Linear interpolation estimator, display-only, capped and flagged, cached in `token_hours_fill` | M | 2 | **done** 2026-11-07 |
| 4 | Kalman / Brownian-bridge estimators, selectable and comparable | L | 2, 3 | **done** 2026-11-08 |

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

**Outcome (2026-11-07).** Done, combined with step 3 in one session.
`FillPolicy` is now four named `FillRule`s in `analytics/prices.py`, each
declaring `causal`. The enforcement point is `load_symbol_series` — the
loader strategies, signals, backtests and the `stats`/`signals`/`backtest`
MCP tools all walk through — which calls `require_causal` and **raises**
rather than quietly downgrading: a silent substitution would make the
chosen policy a lie and the equity curve unexplainable.

`apply_fill` is the pure core and returns a `FilledFrame` carrying a
`synthetic` mask the same shape as the frame, because once a number is in
a column nothing about it says where it came from. `to_wide` is unchanged
for its existing callers (it delegates and drops the mask);
`to_wide_filled` is the variant that keeps it.

The pair of tests that justify the whole seam: moving a bar *after* a gap
changes the interpolated bar *before* it, and the same experiment against
`ffill` leaves the past untouched. The look-ahead is demonstrated, not
asserted in prose.

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

**Outcome (2026-11-07).** Done. `interpolate` is `method="time"` with
`limit_area="inside"`, so nothing is extrapolated before a token's first
bar or after its last — a late start is a short history, not a gap. A run
longer than the cap is left **entirely** alone rather than partly filled:
pandas' own `limit` fills the first N hours of an over-long hole and
leaves the rest, which would draw a line stopping in mid-air.

**Added beyond the design, at the user's request:** the estimates are
cached in a new plain table, `token_hours_fill`, so a dashboard reads them
instead of recomputing on every widget change. Only missing hours are
stored — never a copy of an observation — with `gap_hours` per row as the
nearest thing to an error bar. The roadmap's "no filled rows in the
database" rejection was about `token_hours_data` and stands; §0 now
records the refinement, and an integration test asserts the coverage
report is unchanged by a populated cache. `candlegraph fill`
(`make fill`, `make fill-status`, `--clear`) is the command behind it, and
the Quality dashboard has a refresh button since it writes rows.

What was **not** done: no causal estimator (that is step 4), and nothing
reads the cache except display code. `method_is_causal` treats an unknown
method as unsafe rather than assuming it is fine.

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

**Outcome (2026-11-08).** Done. Two estimators landed rather than one,
because the design's own table distinguishes them and only the split makes
the causal rule meaningful:

* **`causal`** — a local-level (random-walk-plus-noise) Kalman forecast.
  Flat mean, band widening as `sqrt(hours)`. Causal, so it is the one form
  a strategy could consume. A local-linear-trend variant was **rejected**:
  a twenty-hour extrapolated slope on hourly crypto is a confident
  fabrication, and a wrong level is recoverable in a way a wrong direction
  is not.
* **`bridge`** — a Brownian bridge in log space pinned to the observed bars
  either side. Mean is log-linear, band widest halfway across. It reads the
  bar *after* the gap, so it is display only however accurate it is.

Both are deterministic. The design notes a *sampled* path would preserve
the variance budget exactly; that was rejected because a chart that changes
on every reload is not something an operator can reason about. What they
return instead is the posterior mean plus `sigma_log`, so "we do not know"
is representable — the one thing linear interpolation cannot do, and the
reason the comparison table listed "a point with no error bar" as its cost.

The subtle part was **where the volatility comes from**. Scaling the causal
band by volatility measured over the whole column would be a quiet
look-ahead: invisible in the mean, wrong in anything that sized a position
by it. So `causal` estimates sigma from the bars strictly *before* each
gap, and an early gap with too little history behind it gets a mean and no
band rather than a borrowed one. There is a test for exactly that, and it
caught a fixture of mine that had the hole in the wrong place.

**Added beyond the design, at the user's request:** all three estimators
share `token_hours_fill`, keyed apart by `method`, so the same missing hour
can hold three competing estimates and they can be compared. Exactly one is
*selected* at a time — `FILL_METHOD`, `candlegraph fill --method`, or the
Quality dashboard's picker — because they are alternatives, not layers.
`--all-methods` computes each in turn; `--evaluate` holds out hours whose
real price is known and scores every estimator against them, which is the
only falsifiable answer to "which is best". On the first real run it
reported `linear` and `bridge` within 0.2 bps of each other on the liquid
tokens and `causal` ~60% worse — and on **GNO**, the deliberately gappy
token, `causal` *won*. That inversion is the kind of thing the feature
exists to surface.

Migration `f513c5d75c55` adds the nullable `sigma_log`; it stays `NULL` for
`linear`, because "this estimator has no error bar" is not "the error bar
is zero" (invariant 11's rule, applied to an estimate). No change to the
natural key, no change to default behaviour: `FILL_METHOD` defaults to
`linear` and `load_symbol_series` still never reads this table.

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
