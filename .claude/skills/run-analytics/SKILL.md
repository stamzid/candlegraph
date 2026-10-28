---
name: run-analytics
description: Run candlegraph's analytics — rolling volatility, mean-variance portfolios, Bollinger and ARIMA signals, backtests — and interpret the results honestly. Use when asked about volatility, portfolio weights, trading signals, backtests, forecasts, or the Streamlit UI.
---

# Run the analytics

Everything needs stored history first: `make up && make backfill days=90`.

## Commands

```sh
docker compose exec -T worker python -m candlegraph stats WETH --window 168
docker compose exec -T worker python -m candlegraph compare WETH WBTC --window 168
docker compose exec -T worker python -m candlegraph forecast WETH --auto-order
docker compose exec -T worker python -m candlegraph signals WETH --strategy bollinger --store
docker compose exec -T worker python -m candlegraph backtest WETH --strategy all
docker compose exec -T worker python -m candlegraph portfolio --risk-free-rate 0.04 --max-weight 0.4
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

## Reading a backtest honestly

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

## What not to change

- **Never set `execution_lag=0`** to make a backtest look better. It exists
  only to demonstrate the look-ahead bias, and a test pins that it inflates
  returns.
- **Never fit ARIMA on the whole series** and read its in-sample predictions
  back as signals. `arima_signal` refits on `prices[: i + 1]` for a reason,
  and `test_arima_never_fits_on_data_after_the_bar_it_scores` guards it.
- Rolling windows use `min_periods=window` and `ddof=1` deliberately. A
  partial window is a different statistic, not a cheap approximation.
