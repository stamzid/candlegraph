# Rollout: stage 6 — simulation and strategy evaluation

**Status:** current — opened 2026-11-09, step 1 of 3 done. This is **the**
current rollout: when someone says "the plan" or "step N", they mean this
file. Finished rollouts move to [`archive/`](archive/).
**Designs this executes:**
[`../analytics/simulation/DESIGN.md`](../analytics/simulation/DESIGN.md)
(the three simulators and why they are three),
[`../analytics/models/DESIGN.md`](../analytics/models/DESIGN.md) §8 (the
backtester the simulators reuse). Decisions live there; this file only
sequences them. A step that forces a decision changes the design document
in the same step, Status line included.
**Operating procedure per step:** the `run-analytics` skill for anything
touching numbers, `release-check` before calling a step done.

## Progress

| # | Step | Size | Depends on | Status |
|---|---|---|---|---|
| 1 | Solidify the backtester: make it legible and comparable before simulating against it | M | — | **done** 2026-11-09 |
| 2 | Monte Carlo + discrete-event simulation | L | 1 | todo |
| 3 | MCMC posteriors + stage closeout | L | 1, 2 | todo |

**Why this order.** Step 1 first because the simulators all reuse
`strategies/backtest.py`, and a reference implementation nobody can read
is a poor thing to measure against. Steps 2 and 3 follow the split
suggested in the session that opened this rollout, and the reasoning holds
up: **Monte Carlo and discrete-event share their plumbing** — both end in
"run the existing backtester over a path and collect the result" — while
**MCMC shares nothing with either**. It is a sampler over parameter space,
with its own convergence diagnostics and its own failure mode, and
pairing it with the closeout gives it a turn of its own.

**Sizing warning for step 2.** It is the biggest step here and the one
most likely to overrun, because it is two deliverables that merely *look*
adjacent. If it has to be cut, cut the discrete-event scope rather than
Monte Carlo's: the zero-latency reconciliation against the vectorised
backtester is the part that earns its keep, and the richer execution
models (partial fills sized by volume, size-dependent slippage) can
follow in a later step without changing the seam.

**Do not combine 2 and 3.** Three new numerical subsystems in one session
is how an unconverged sampler ships looking like a result.

The exit criterion for the whole stage is in the simulation design
document, and all three clauses have to hold at once.

---

## Step 1 — Solidify the backtester

**Goal.** Before anything is measured against the backtester, make it
legible: say on screen how it works, show every number the CLI shows, and
let a reader compare strategies and see what the execution assumptions are
worth.

**Read first.** `docs/analytics/models/DESIGN.md` §8, invariant 15.

**Touches.** `candlegraph/ui/viewmodels.py`, `candlegraph/ui/app.py`,
`tests/unit/ui/`, README's UI section.

**Watch for.** The temptation to change the backtester's arithmetic. This
step is about *exposing* it; a number that moves here is a bug.

**Outcome (2026-11-09).** Done. The audit found the Strategy dashboard
showing six of the thirteen numbers `candlegraph backtest` prints, with
no account anywhere on screen of how any of them were produced — no
mention of the execution lag, no mention that the sidebar's fees drive
the result, and no way to compare strategies even though
`backtest --strategy all` has always printed exactly that.

What landed: a "How this backtest works" explainer stating the lag, the
cost model and why `excess` is read before `total return`; all thirteen
metrics, each rendered in its own unit; a gross-against-net chart
answering "did the signal fail or did the fees eat it"; a strategy
comparison table across every registered rule; starting capital and
execution lag as controls beside the result they produce; and a
**look-ahead check** that re-runs the same strategy at `execution_lag=0`
and plots both curves.

**The look-ahead check corrected a belief, including the one this step
started with.** The first draft of the copy said removing the lag
*inflates* returns. It does not, uniformly: measured on the test fixture,
a crossover gains +4.69% from the look-ahead while a Bollinger rule
*loses* 37.53%. A mean-reversion signal shorts the bar that just rose and
then collects that rise, so trading on information it should not have had
punishes it. Both numbers are equally unreal, and the lesson is the size
of the gap rather than its sign. A test pins both directions so the
explainer cannot drift back to the simpler, false version.

No change to `strategies/backtest.py`: every number on the page comes from
the same functions the CLI calls.

---

## Step 2 — Monte Carlo and discrete-event simulation

**Goal.** Two of the three simulators. Monte Carlo answers "is this edge
luck"; discrete-event answers "does the vectorised backtester lie about
execution".

**Read first.** `docs/analytics/simulation/DESIGN.md` §1 and §2 —
especially the generator comparison table and the terminology trap at the
end, which is the easiest thing here to get wrong.

**Touches.** New `candlegraph/analytics/simulation.py` (pure generators)
and `candlegraph/analytics/events.py` (the event loop); a `simulate` CLI
subcommand and `make simulate`; a Simulation dashboard; `tests/unit/`
mirroring both.

**Watch for.**

- **The default generator is the stationary block bootstrap**, not an IID
  one. An IID bootstrap destroys the volatility clustering most of these
  strategies are implicitly trading, which flatters mean reversion and
  punishes trend following for reasons that have nothing to do with
  either.
- **A bootstrap of a bull sample simulates bull markets.** Monte Carlo
  widens the sample along one axis only. Say so on every surface that
  reports a distribution; ten thousand paths from ninety days is still
  ninety days.
- **The discrete-event run must reproduce the vectorised backtester to
  floating-point tolerance at zero latency, full fills and flat cost.**
  That equality *is* the deliverable — it is what makes the fast path
  trustworthy. Write that test first; if it will not pass, one of the two
  is wrong and finding out which is the work.
- Seeds are recorded and reported. A distribution that changes between
  two readings of the same page is not evidence.
- The harness calls the existing backtester rather than reimplementing
  `position * returns`, so invariants 15-17 cannot drift inside it.

**Done when.** `candlegraph simulate WETH --strategy bollinger
--paths 1000` prints the distribution of excess return and an empirical
p-value against the real backtest; a zero-latency discrete-event run
matches the vectorised result to tolerance; and the cost of one bar of
latency is reported as a number.

---

## Step 3 — MCMC posteriors, and closeout

**Goal.** The third simulator — posteriors over model parameters — and
then close the stage.

**Read first.** `docs/analytics/simulation/DESIGN.md` §3 and the
terminology trap; `docs/analytics/models/DESIGN.md` on ARIMA;
`candlegraph/analytics/statespace.py`, whose band is scaled by a
volatility *point estimate* and inherits exactly the criticism this step
answers.

**Touches.** New `candlegraph/analytics/mcmc.py` (sampler and
diagnostics), `forecast --posterior`, the portfolio weight distribution,
the Forecast and Portfolio dashboards.

**Watch for.**

- **The diagnostics are not optional.** An unconverged chain produces
  confident nonsense and, unlike a failed optimisation, does not announce
  itself. R-hat and effective sample size are reported with every
  posterior, and a run that fails them is a refusal, not a result.
- **Hand-rolled Metropolis-Hastings, no new dependency.** `pymc` and
  `numpyro` are large with compilation toolchains, for models of two or
  three parameters where a short sampler is auditable and deterministic
  under a seed. Record the trigger that would change the call: hierarchy,
  or more than a handful of parameters.
- **MCMC is not the regime-switching generator.** They share a name and
  nothing else; the regime-switching model is a step-2 path generator.
  The UI must never present them as one control.
- The posterior interval must come out *wider* than the point-estimate
  one. If it does not, the sampler is not exploring.

**Closeout.** Every `docs/analytics/simulation/` Status line flips to
`implemented` with what was *not* built named; AGENTS.md commands,
layout, data model and invariants updated; `docs/README.md` statuses;
the roadmap marks stage 6 delivered against its exit criterion; the full
`release-check` skill runs clean; this file moves to
`docs/rollout/archive/`, and `ROLLOUT.md` says what is current next.

---

## Standing rules for every step

- `make check` green before a step is called done; integration skips are
  not green (check for them).
- Nothing simulated is written to `token_hours_data`. If a result needs
  persisting it gets its own table and a placement decision in
  `docs/storage/table-placement/DESIGN.md` **before** the migration.
- Generators and samplers are pure functions of `(data, parameters,
  seed)` and live in `analytics/`; anything touching the database stays
  outside it (invariant 18).
- Simulators reuse `strategies/backtest.py`. A second implementation of
  `position * returns` is how invariant 15 comes back.
- A result that says "no edge" is the expected outcome, not a bug.
- Update the progress table above at the end of each session, including
  "partial" with a note, so the next session starts from the truth.
