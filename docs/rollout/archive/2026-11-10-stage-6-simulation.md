# Rollout: stage 6 — simulation and strategy evaluation

**Status:** complete — opened 2026-11-09, all three steps done
2026-11-10; archived. This is a record of how the stage was built, not a
plan: the decisions live in
[`../../analytics/simulation/DESIGN.md`](../../analytics/simulation/DESIGN.md),
and [`../ROLLOUT.md`](../ROLLOUT.md) says what is current.
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
| 2 | Monte Carlo + discrete-event simulation | L | 1 | **done** 2026-11-09 |
| 3 | MCMC posteriors + stage closeout | L | 1, 2 | **done** 2026-11-10 |

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

**Outcome (2026-11-09).** Done, and the step's own warning earned its
keep: **writing the reconciliation first found two bugs**, both of which
would have been invisible in a vectorised implementation.

The first: `fill_ratio=1.0` was read as a one-*unit* cap rather than "all
of the gap", so every long-to-short flip — two units of movement —
silently took two bars. Aggression and capacity are now separate
arguments. The second, and the more interesting one: a fill read the
*current* target instead of the one its order was placed with, so any
latency above zero executed a trade nobody had placed. Orders now carry
their own target, which also makes explicit something `shift(k)` implies
and never states — above `execution_lag=1` you are filling **stale
decisions**, with no cancel-replace. After both fixes the engines agree
to `0.0` on positions *and* equity at latencies 0, 1, 2 and 5.

Monte Carlo landed with four generators behind a registry, each declaring
what it preserves and destroys. The block bootstrap was verified to do
its job rather than assumed to: on a fixture with real clustering it
carries lag-1 absolute-return autocorrelation through at +0.357 against
the source's +0.371, while IID and GBM flatten it to zero. A plain random
walk cannot show this, so the test builds a clustered fixture on purpose.

**One deviation from the plan, for a reason worth recording.** The
harness that runs a strategy over the paths is
`strategies/montecarlo.py`, not `analytics/`. `strategies.backtest`
already imports `analytics`, so a harness in `analytics` that needed the
backtester would close an import cycle. The pure generators are in
`analytics/simulation.py` as planned; only the part that needs the
backtester moved.

**What was not built:** no persistence. Nothing a simulation produces is
stored, so there is no new table and no placement decision. The two
surfaces (`candlegraph simulate`, the Simulation dashboard) recompute,
which at these path counts is seconds and keeps the seed the only thing
that has to be remembered.

**Readability pass.** `_quality_page` and `_token_page` had grown to 231
and 195 lines; both were split into panels, and the new analytics modules
top out at 46 lines (`_run_queue`, the event loop). `_news_page`,
`_strategy_page`, `_agents_page` and `_portfolio_page` remain over 140
lines and are the obvious next candidates.

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

**Outcome (2026-11-10).** Done, and the step absorbed stage 0's closeout
as well, so two stages are delivered rather than one.

**MCMC.** `analytics/mcmc.py` is a random-walk Metropolis sampler with
Gelman-Rubin and effective sample size, over a random-walk-with-drift
model in `(mu, log sigma)` — the model every forecast here already
assumed, with the parameters no longer pretended to be known.
`forecast --posterior` exits non-zero on an unconverged chain, and the
dashboard prints a refusal banner: three surfaces, one gate.

**The diagnostics earned their place immediately.** The first working
sampler reported ESS 17 for `mu` against 1228 for `log sigma` and was
correctly refused. The cause was that a single acceptance-based tuning
factor scales every dimension together, so it cannot fix a proposal four
times too narrow in one parameter and twenty times too wide in another —
which is exactly a drift near 1e-4 beside a log volatility near -4.
Warm-up now re-fits the proposal to the spread of the draws before
applying the acceptance nudge.

**A claim that needed softening.** The design said the posterior interval
is wider than the plug-in one. Measured: +31% at 40 bars, +14% at 80,
+5% at 200, and indistinguishable at 800. The convergence is the method
*agreeing* with the point estimate once the parameters are pinned down,
not failing, and both the docstring and the design document now say so.

**Stage 0's closeout: the `active` flag.** `config/tokens.yaml` gained
`active`, defaulting to true so an older file is unchanged. The active
five are WBTC, WETH, BNB (new to the registry), GNO and SHIB — the last
two chosen *because* their history is gapped. Deactivating a token means
**stop polling, never forget**: the entry stays listed, `by_symbol` still
finds it, and every analytics surface still reads its stored history,
because removing it would orphan rows invariant 4 promises to keep. An
all-inactive file is refused, and a non-boolean flag is refused because
YAML turns `no` into `False` but `"false"` into a truthy string.

**A bug the unit tier missed and the stack found in one command.**
`resolve_cursors` builds its map from the active addresses, but
`ingest_once` looped over the whole registry, so the first inactive token
raised a bare `KeyError`. Every fixture registry had all tokens active,
so nothing caught it until `make ingest` ran against real compose. There
is now a regression test with an inactive token in the fixture.

**UI clarity.** The discrete-event panel was reported as needing a trip
through the source to check the implementation against the theory. It now
states the four-event lifecycle, the
`latency_bars=k` / `execution_lag=k+1` correspondence and why the `+1` is
structural, and shows a **worked example** — the real engine on a flat
price and a trivial signal, with a `latency_bars` control, so the
off-by-one and the cost timing are visible rather than asserted. It uses
`run_execution` itself, so an illustration cannot drift from the engine.

**Make targets.** Nine analytics commands (`stats`, `compare`, `signals`,
`backtest`, `strategies`, `portfolio`, `forecast`, `forecast-posterior`,
`risk`) had no target and were reachable only by hand-written
`docker compose exec`, which is the thing the Makefile exists to prevent.
A test now reads both lists and fails if a CLI subcommand that a person
would run interactively has no target behind it.

**Verified against a real stack**, not only in tests: `make up`, one
ingest pass polling exactly the five active tokens, a 20-day backfill,
`make forecast-posterior` converging with R-hat 1.0023, `make simulate`
reporting p = 0.475, `make compare`, `make backtest --strategy all`, and
a clean `make down` with the other worktree's stack untouched.

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
