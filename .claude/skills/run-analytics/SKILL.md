---
name: run-analytics
description: Run candlegraph's analytics — rolling volatility, mean-variance portfolios, Bollinger and ARIMA signals, backtests — and interpret the results honestly. Use when asked about volatility, portfolio weights, trading signals, backtests, forecasts, or the Streamlit UI.
---

# Run the analytics

Everything needs stored history first: `make up && make backfill days=90`.

**Every command below has a `make` target.** If you find yourself typing
`docker compose exec worker python -m candlegraph ...`, stop and check
`make help` — the target exists, takes `ARGS`, and a test fails if a CLI
subcommand a person would run interactively has none behind it.

```sh
make stats ARGS='WETH --window 24'       make compare ARGS='WETH WBTC'
make signals ARGS='WETH --store'         make backtest ARGS='WETH --strategy all'
make portfolio ARGS='--objective max_sharpe'
make forecast ARGS='WETH --auto-order'   make forecast-posterior ARGS='WETH'
make risk ARGS='WETH --benchmark WBTC'   make strategies
```

Only the **five active tokens** are ingested (WBTC, WETH, BNB, GNO,
SHIB). The rest are still listed in `config/tokens.yaml` with
`active: false` and their stored history is still readable, so
`make compare ARGS='USDC WETH'` works on what was already collected — it
just stops getting newer.

## Commands

```sh
docker compose exec -T worker python -m candlegraph stats WETH --window 168
docker compose exec -T worker python -m candlegraph compare WETH WBTC --window 168
docker compose exec -T worker python -m candlegraph forecast WETH --auto-order
docker compose exec -T worker python -m candlegraph signals WETH --strategy bollinger --store
# Explain the latest bar with cited news (no LLM key needed; one Tavily search per token per day at most)
make explain ARGS='WETH --strategy bollinger'
docker compose exec -T worker python -m candlegraph backtest WETH --strategy all
docker compose exec -T worker python -m candlegraph portfolio --risk-free-rate 0.04 --max-weight 0.4
docker compose exec -T worker python -m candlegraph risk WETH --benchmark WBTC --confidence 0.99
```

The UI covers all of it interactively at <http://localhost:8501> (`make ui` to
run it outside the stack).

## Reading a pairwise comparison

`compare` works only on **aligned bars** — hours both tokens traded. A thin
token versus a liquid one can leave few aligned bars; the first line of the
report says how many survived, and a correlation over a few hundred bars is
noisier than it looks. Do not "fix" sparsity by forward-filling before
comparing — that measures the fill, not the market, and the aligned-only rule
is tested.

## Reading a risk report

`risk` prints tail risk (VaR and CVaR at the configured confidence and
horizon), drawdown and the ratios, and — against `RISK_BENCHMARK` (WETH by
default) — beta, R² and the systematic/idiosyncratic split. The theory is in
`docs/analytics/risk/DESIGN.md`. Three things to check:

1. **Compare estimators.** Run with `--method historical`, `parametric` and
   `cornish_fisher`. A parametric VaR well below the other two is the tail
   telling you it is fat; prefer the historical or Cornish–Fisher figure.
2. **Read CVaR, not VaR,** when one number must be chosen. VaR is the
   threshold; CVaR is the average bill once it is crossed.
3. **Check the aligned bar count** on the benchmark split. A thin token
   against WETH may have few common hours and a noisy beta.

The rate line prints the nominal and the real risk-free rate and which
source supplied them. `RISK_FREE_RATE_SOURCE=tavily` is refused until the
knowledge-layer provider exists; that is deliberate, not a bug.

## Reading a backtest honestly

The **Strategy** dashboard states all of this on screen now — the execution
lag, the cost model, every metric the CLI prints, a comparison across all
registered strategies, and a gross-against-net chart showing where the edge
went. Point a reader there before explaining it by hand.

This is the part that goes wrong. Three rules:

1. **Compare against buy and hold, not against zero.** The report prints
   `excess` for exactly this reason. A strategy that made 18% while the asset
   made 30% lost money in the only sense that matters.
2. **Check `costs paid`.** On hourly bars at 35bps per side, an active
   strategy routinely pays 15-20% of capital in fees over 90 days. If costs
   exceed gross return, the signal has no edge — tune the thresholds to trade
   less rather than hunting for a better-looking window.
3. **A negative result is a result.** Naive Bollinger and crossover on hourly
   crypto usually lose after costs. That is the honest finding. Do not keep
   re-running with new parameters until one looks good; that is fitting the
   backtest to the sample.

`sharpe` here is annualised from hourly bars, so it is large in magnitude in
both directions. Prefer `excess` and `max drawdown` for judgement.

## Is the edge real, and would it have filled?

A backtest is one number over the one path that happened. Two commands
widen that, and they answer different doubts.

```sh
make simulate ARGS='WETH --strategy bollinger --paths 1000'
make simulate-execution ARGS='WETH'
```

**`simulate`** re-runs the strategy over synthetic markets and reports a
distribution plus an empirical p-value. Read the p-value, not the
percentiles: it is the share of resampled markets that did at least as
well, so **large means no edge**. Expect that outcome — Bollinger and
crossover on hourly crypto routinely land near 0.5, which is the honest
answer. The default generator is the stationary block bootstrap; `--method
iid` is a *stricter* null (it destroys volatility clustering) rather than
a worse one, and is worth running as a second opinion. Resampling
reorders the moves that happened and invents no regime the window does
not contain.

**`simulate --execution`** reconciles the discrete-event engine against
the vectorised backtester and prices its assumptions. The reconciliation
must come back **AGREES** with zero differences; if it does not, do not
reason about which engine is right, find out — every backtest in the repo
depends on the answer. `latency_bars=k` is the same as
`execution_lag=k+1`, the extra bar being structural rather than slow.

## Does the forecast know what it does not know?

```sh
make forecast-posterior ARGS='WETH'
```

Samples the model's parameters instead of fitting a point estimate, so
the interval covers parameter uncertainty as well as innovation noise.
Two rules:

- **Check R-hat and ESS before reading anything else.** The command exits
  non-zero and prints `REFUSED` when the chains did not mix, because an
  unconverged sampler produces confident nonsense and does not announce
  itself. Raise `--draws` if it refuses.
- **A widening near zero on a long window is correct**, not a failure. It
  means the parameters are pinned down and the posterior agrees with the
  point estimate. The number is informative on short windows: roughly
  +30% at 40 bars, +5% at 200, nothing at 800.

## Tuning

| Symptom | Knob |
|---|---|
| Too many trades, costs dominate | Raise `--entry-z`, widen the gap to `--exit-z`, or raise `--min-separation` |
| Strategy never trades | Lower `--threshold` (arima) or `--entry-z` (bollinger); check the warm-up |
| ARIMA stands aside permanently | Its confidence interval exceeds `max_interval_width`; the model is saying it does not know |
| ARIMA backtest is slow | Raise `--refit-every`; it refits per bar by default cadence |
| Portfolio puts everything in one token | Set `--max-weight 0.3`, or `--shrinkage 0.3` to condition a noisy covariance |
| Portfolio loves USDC/USDT | Correct, not a bug: stablecoins priced in USD have near-zero variance. Exclude them with `--symbols` if you want risk-asset-only weights |
| "cannot sum to 1" | `--max-weight` is below `1/n`; raise it |
| VaR "need at least 30 returns" | Too little history for a tail; widen `--days` |
| Parametric VaR much smaller than historical | Fat tails; use `--method cornish_fisher` or `historical` |
| No benchmark split printed | The benchmark is the token itself or is not tracked; pass `--benchmark` |
| A chart has holes and you want them filled | `make fill` caches a gap estimate; `candlegraph fill --evaluate` scores the three estimators. Display only — `load_symbol_series` refuses a non-causal policy, so no backtest ever sees one |

## What not to change

- **Never set `execution_lag=0`** to act on a result. It exists only to
  demonstrate the look-ahead bias, and the Strategy dashboard's **Look-ahead
  check** draws it. Note the bias does **not** always flatter: a trend rule
  gains from it (it goes long on the bar it then earns), while a
  mean-reversion rule is punished by it (it shorts the bar that just rose,
  then collects the rise). Measured on the test fixture: crossover +4.69%,
  Bollinger -37.53%. Both are equally unreal — the size of the gap is the
  lesson, not its sign, and tests pin both directions.
- **Never fit ARIMA on the whole series** and read its in-sample predictions
  back as signals. `arima_signal` refits on `prices[: i + 1]` for a reason,
  and `test_arima_never_fits_on_data_after_the_bar_it_scores` guards it.
- Rolling windows use `min_periods=window` and `ddof=1` deliberately. A
  partial window is a different statistic, not a cheap approximation.
