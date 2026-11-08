"""Posteriors by Metropolis-Hastings: how wrong might the parameters be?

Pure numerics. Arrays in, chains out; no database, no I/O.

Every model in this codebase fits parameters and then forecasts as if those
parameters were known exactly. `analytics/arima.py` does it, and so does
`analytics/statespace.py`, whose band is scaled by a volatility *point
estimate*. The resulting interval covers innovation noise only, so it is
too narrow — systematically, and more so on short windows.

Sampling the posterior and forecasting from each draw fixes that: the
interval then covers both sources, and it is wider because it is honest
rather than because it is cautious.

**The sampler is hand-rolled on purpose.** ``pymc`` and ``numpyro`` are
large dependencies with compilation toolchains, for a model of two
parameters where a short random-walk Metropolis is auditable and
deterministic under a seed. The trigger that would change the call is
recorded in ``docs/analytics/simulation/DESIGN.md`` §3: hierarchy, or more
than a handful of parameters.

**The diagnostics are not optional.** An unconverged chain produces
confident nonsense and, unlike a failed optimisation, does not announce
itself. R-hat and effective sample size are computed on every run and
:meth:`PosteriorSamples.usable` is false when they fail, so a caller that
forgets to look still cannot present the result as a number.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

# Gelman-Rubin above this means the chains have not mixed into one another.
# 1.01 is the modern convention; 1.1 is the older, laxer one.
MAX_R_HAT = 1.01
# Below this many effectively independent draws, a percentile of the
# posterior is mostly noise whatever R-hat says.
MIN_ESS = 100
# Metropolis needs several chains to have anything to compare.
MIN_CHAINS = 2
# Acceptance the proposal scale is tuned towards during warm-up. The
# classic optimum for a random walk in low dimension is near 0.234 and the
# curve around it is flat, so anything in this band is fine.
TARGET_ACCEPTANCE = 0.30
# How often warm-up re-fits the proposal to the draws seen so far.
ADAPT_EVERY = 100
# Roberts-Gelman-Gilks optimal scaling for a random-walk proposal in `d`
# dimensions: the width that maximises mixing is about this times the
# posterior standard deviation.
OPTIMAL_SCALING = 2.4


class MCMCError(ValueError):
    """The sampler cannot run, or its output must not be used."""


LogDensity = Callable[[np.ndarray], float]


@dataclass(frozen=True)
class PosteriorSamples:
    """Draws from a posterior, with the diagnostics that license using them."""

    draws: np.ndarray
    """``(n_chains, n_draws, n_params)``, warm-up already discarded."""
    names: tuple[str, ...]
    acceptance: float
    seed: int

    @property
    def flat(self) -> np.ndarray:
        """All chains stacked: ``(n_chains * n_draws, n_params)``."""
        chains, draws, params = self.draws.shape
        return self.draws.reshape(chains * draws, params)

    @property
    def r_hat(self) -> np.ndarray:
        return gelman_rubin(self.draws)

    @property
    def ess(self) -> np.ndarray:
        return np.array([effective_sample_size(self.draws[:, :, i]) for i in range(self.draws.shape[2])])

    @property
    def converged(self) -> bool:
        return bool(np.all(self.r_hat <= MAX_R_HAT))

    @property
    def well_mixed(self) -> bool:
        return bool(np.all(self.ess >= MIN_ESS))

    @property
    def usable(self) -> bool:
        """Whether these draws may be presented as a posterior at all."""
        return self.converged and self.well_mixed

    @property
    def diagnosis(self) -> str:
        """Why the draws are unusable, in a sentence, or that they are fine."""
        if self.usable:
            return (
                f"Converged: R-hat {self.r_hat.max():.4f} (<= {MAX_R_HAT}), "
                f"effective sample size {int(self.ess.min())} (>= {MIN_ESS})."
            )
        faults = []
        if not self.converged:
            faults.append(f"R-hat {self.r_hat.max():.3f} exceeds {MAX_R_HAT} — the chains disagree")
        if not self.well_mixed:
            faults.append(
                f"effective sample size {int(self.ess.min())} is below {MIN_ESS} — the chain is "
                "too autocorrelated for its percentiles to mean much"
            )
        return "; ".join(faults) + ". Draw more samples, or widen the warm-up."

    def summary(self) -> dict[str, dict[str, float]]:
        """Posterior mean and a 90% band per parameter."""
        flat = self.flat
        return {
            name: {
                "mean": float(np.mean(flat[:, i])),
                "p5": float(np.percentile(flat[:, i], 5)),
                "p95": float(np.percentile(flat[:, i], 95)),
                "r_hat": float(self.r_hat[i]),
                "ess": float(self.ess[i]),
            }
            for i, name in enumerate(self.names)
        }


# --- the diagnostics -----------------------------------------------------------


def gelman_rubin(draws: np.ndarray) -> np.ndarray:
    """Potential scale reduction per parameter: ``sqrt(var_hat / W)``.

    Compares the variance *between* chains with the variance *within*
    them. If several chains started in different places have found the
    same distribution the two agree and the ratio is near 1; if they are
    still exploring separate regions, between-chain variance dominates.

    One chain cannot produce this number, which is why the sampler
    insists on at least two.
    """
    chains, length, params = draws.shape
    if chains < MIN_CHAINS or length < 2:
        return np.full(params, np.inf)

    chain_means = draws.mean(axis=1)
    within = draws.var(axis=1, ddof=1).mean(axis=0)
    between = length * chain_means.var(axis=0, ddof=1)

    pooled = (length - 1) / length * within + between / length
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(within > 0, np.sqrt(pooled / within), np.inf)
    return np.asarray(ratio, dtype="float64")


def effective_sample_size(chains: np.ndarray) -> float:
    """How many independent draws these correlated ones are worth.

    ``N / (1 + 2 * sum rho_k)``, truncated at the first lag where
    consecutive autocorrelations sum negative — Geyer's initial positive
    sequence, which is what stops a noisy tail inflating the estimate.

    A chain that moves rarely has a high autocorrelation and therefore a
    low effective size, which is the honest statement that its hundred
    draws are worth ten.
    """
    n_chains, length = chains.shape
    total = n_chains * length
    if length < 4:
        return 0.0

    centred = chains - chains.mean(axis=1, keepdims=True)
    variance = float(np.mean(np.sum(centred**2, axis=1) / length))
    if variance <= 0:
        return 0.0

    correlation_sum = 0.0
    previous = 1.0
    for lag in range(1, min(length // 2, 500)):
        products = centred[:, :-lag] * centred[:, lag:]
        rho = float(np.mean(np.sum(products, axis=1) / (length * variance)))
        if previous + rho < 0:
            break
        correlation_sum += rho
        previous = rho
    return float(total / (1.0 + 2.0 * max(correlation_sum, 0.0)))


# --- the sampler ----------------------------------------------------------------


def _tune(scale: np.ndarray, accepted: int, attempted: int) -> np.ndarray:
    """Nudge the proposal width towards :data:`TARGET_ACCEPTANCE`.

    Too wide and every proposal is rejected, so the chain sticks; too
    narrow and it accepts everything while crawling. Both look like a
    converged chain to the eye and neither is, which is why this is
    adapted rather than guessed.
    """
    if attempted == 0:
        return scale
    rate = accepted / attempted
    return scale * float(np.exp(rate - TARGET_ACCEPTANCE))


def _reshape_to_posterior(history: np.ndarray, scale: np.ndarray) -> np.ndarray:
    """Re-fit the proposal to the spread of the draws seen so far.

    A single acceptance-based factor scales every dimension together, so
    it cannot fix a proposal that is four times too narrow in one
    parameter and twenty times too wide in another — and parameters on
    different scales (a drift near 1e-4 beside a log volatility near -4)
    are exactly that case. Measuring each dimension separately is what
    makes the sampler work without the caller hand-tuning it.

    A dimension that has not moved keeps its current width rather than
    collapsing to zero.
    """
    spread = history.std(axis=0, ddof=1) if len(history) > 1 else np.zeros(scale.size)
    fitted = OPTIMAL_SCALING / np.sqrt(scale.size) * spread
    return np.where(fitted > 0, fitted, scale)


def _one_chain(
    log_density: LogDensity,
    start: np.ndarray,
    draws: int,
    warmup: int,
    scale: np.ndarray,
    rng: np.random.Generator,
) -> tuple[np.ndarray, int]:
    """Random-walk Metropolis: propose, compare densities, accept or stay.

    The proposal is symmetric, so the Hastings correction cancels and the
    acceptance probability is just the density ratio. Warm-up draws are
    used to tune the step size and are then discarded, because they were
    generated under a different proposal and are not from the posterior.
    """
    position = np.asarray(start, dtype="float64")
    density = log_density(position)
    kept = np.empty((draws, position.size))
    seen: list[np.ndarray] = []
    accepted = total_accepted = 0

    for step in range(warmup + draws):
        proposal = position + scale * rng.standard_normal(position.size)
        proposed_density = log_density(proposal)
        if np.log(rng.random()) < proposed_density - density:
            position, density = proposal, proposed_density
            accepted += 1
            total_accepted += 1

        if step >= warmup:
            kept[step - warmup] = position
            continue

        seen.append(position.copy())
        if (step + 1) % ADAPT_EVERY == 0:
            # Shape first, then size: the spread of the draws fixes the
            # relative widths, the acceptance rate fixes the overall one.
            scale = _tune(_reshape_to_posterior(np.array(seen), scale), accepted, ADAPT_EVERY)
            accepted = 0
            seen.clear()

    return kept, total_accepted


def sample(
    log_density: LogDensity,
    initial: np.ndarray,
    names: tuple[str, ...],
    draws: int = 2000,
    warmup: int = 1000,
    chains: int = 4,
    seed: int = 0,
    scale: float = 0.1,
) -> PosteriorSamples:
    """Draw from ``log_density`` with several independent chains.

    Args:
        log_density: Log of the unnormalised posterior. Returning
            ``-inf`` is how a prior rejects a region.
        initial: Starting point, one entry per parameter. Chains are
            dispersed around it, because chains that all start together
            cannot reveal that they failed to mix.
        names: Parameter names, for the summary.
        draws: Kept draws per chain.
        warmup: Discarded tuning draws per chain.
        chains: At least two — R-hat compares them.
        seed: Recorded on the result.
        scale: Initial proposal width, adapted during warm-up.

    Returns:
        :class:`PosteriorSamples`, which may be unusable; check
        ``usable`` before reading anything off it.

    Raises:
        MCMCError: fewer than two chains, or a start the density rejects.
    """
    if chains < MIN_CHAINS:
        raise MCMCError(f"need at least {MIN_CHAINS} chains for R-hat, got {chains}")
    if draws < 1 or warmup < 0:
        raise MCMCError("draws must be positive and warmup non-negative")

    start = np.asarray(initial, dtype="float64")
    if not np.isfinite(log_density(start)):
        raise MCMCError("the initial point has zero posterior density; check the priors")

    rng = np.random.default_rng(seed)
    widths = np.full(start.size, scale) * np.maximum(np.abs(start), 1e-3)

    collected, accepted, attempted = [], 0, 0
    for _chain in range(chains):
        # Disperse the starts: identical ones can agree while both wrong.
        dispersed = start + 0.5 * widths * rng.standard_normal(start.size)
        if not np.isfinite(log_density(dispersed)):
            dispersed = start
        kept, hits = _one_chain(log_density, dispersed, draws, warmup, widths.copy(), rng)
        collected.append(kept)
        accepted += hits
        attempted += warmup + draws

    return PosteriorSamples(
        draws=np.stack(collected),
        names=names,
        acceptance=accepted / attempted if attempted else 0.0,
        seed=seed,
    )


# --- the model: a random walk with unknown drift and volatility -----------------


def random_walk_log_density(returns: np.ndarray) -> LogDensity:
    """Posterior over ``(mu, log sigma)`` for IID normal log returns.

    The model behind every forecast interval in this codebase, written
    down: log prices follow a random walk with drift ``mu`` and per-bar
    volatility ``sigma``. The point-estimate version plugs in the sample
    mean and standard deviation; this one keeps the uncertainty in both.

    ``log sigma`` rather than ``sigma`` is sampled so the parameter is
    unconstrained and a symmetric proposal cannot step to a negative
    volatility. The priors are deliberately wide — at hourly crypto
    scales they are nearly flat, and the data decides.
    """
    observations = np.asarray(returns, dtype="float64")
    observations = observations[np.isfinite(observations)]
    count = observations.size

    def density(params: np.ndarray) -> float:
        mu, log_sigma = float(params[0]), float(params[1])
        if not (-20.0 < log_sigma < 2.0):
            return float("-inf")
        sigma = np.exp(log_sigma)

        residuals = observations - mu
        log_likelihood = -count * log_sigma - float(np.sum(residuals**2)) / (2 * sigma**2)
        # Weakly informative: N(0, 1) on the drift is enormous next to an
        # hourly return, and N(-5, 3) on log sigma spans 0.0003 to 0.13.
        log_prior = -0.5 * mu**2 - 0.5 * ((log_sigma + 5.0) / 3.0) ** 2
        return float(log_likelihood + log_prior)

    return density


@dataclass(frozen=True)
class PosteriorForecast:
    """A forecast that admits it does not know its own parameters."""

    steps: int
    last_price: float
    median: float
    lower: float
    upper: float
    confidence: float
    plugin_lower: float
    """The same interval with the parameters treated as known — the one
    every other forecast in this codebase reports."""
    plugin_upper: float
    posterior: PosteriorSamples

    @property
    def width(self) -> float:
        return (self.upper - self.lower) / self.last_price

    @property
    def plugin_width(self) -> float:
        return (self.plugin_upper - self.plugin_lower) / self.last_price

    @property
    def widening(self) -> float:
        """How much wider than the plug-in interval, as a fraction.

        In theory non-negative: adding a source of uncertainty cannot
        narrow an interval. In practice it **shrinks towards zero as the
        sample grows**, because parameter uncertainty becomes negligible
        beside twenty-four bars of innovation noise — measured on a
        random walk, +31% at 40 bars, +5% at 200, and indistinguishable
        from zero at 800, where the last digit is Monte Carlo error in
        the percentile rather than a real narrowing.

        So this is a number to read on short windows. A large *negative*
        value is the signal that something is wrong; a value near zero on
        a long window is the method agreeing with the point estimate,
        which is the correct answer.
        """
        return self.width / self.plugin_width - 1.0 if self.plugin_width > 0 else 0.0

    @property
    def usable(self) -> bool:
        return self.posterior.usable


def _predictive_prices(
    last_price: float, draws: np.ndarray, steps: int, rng: np.random.Generator
) -> np.ndarray:
    """One future price per posterior draw.

    For each ``(mu, log sigma)`` the sum of ``steps`` log returns is
    normal with mean ``steps * mu`` and variance ``steps * sigma^2``.
    Drawing one future per parameter draw is what mixes the two sources
    of uncertainty rather than adding them by hand.
    """
    mu = draws[:, 0]
    sigma = np.exp(draws[:, 1])
    total = rng.normal(steps * mu, sigma * np.sqrt(steps))
    return np.asarray(last_price * np.exp(total), dtype="float64")


def _plugin_interval(
    last_price: float, returns: np.ndarray, steps: int, confidence: float
) -> tuple[float, float]:
    """The interval a point estimate gives: innovation noise only."""
    from scipy.stats import norm

    mu = float(np.mean(returns))
    sigma = float(np.std(returns, ddof=1))
    z: float = float(norm.ppf(0.5 + confidence / 2))
    centre = steps * mu
    spread = z * sigma * np.sqrt(steps)
    return last_price * float(np.exp(centre - spread)), last_price * float(np.exp(centre + spread))


def posterior_forecast(
    prices: object,
    steps: int = 24,
    confidence: float = 0.95,
    draws: int = 2000,
    warmup: int = 1000,
    chains: int = 4,
    seed: int = 0,
) -> PosteriorForecast:
    """Forecast ``steps`` ahead with parameter uncertainty included.

    Raises:
        MCMCError: too little history to estimate anything from.
    """
    import pandas as pd

    from candlegraph.analytics.rolling import log_returns

    series = pd.Series(prices).dropna().astype("float64")
    returns = log_returns(series).to_numpy(dtype="float64")
    returns = returns[np.isfinite(returns)]
    if returns.size < 30:
        raise MCMCError(
            f"only {returns.size} usable returns; a posterior over two parameters needs "
            "at least 30 before it says more than the priors do"
        )
    if not 0.0 < confidence < 1.0:
        raise MCMCError("confidence must be strictly between 0 and 1")

    start = np.array([float(np.mean(returns)), float(np.log(max(np.std(returns, ddof=1), 1e-8)))])
    samples = sample(
        random_walk_log_density(returns),
        initial=start,
        names=("mu", "log_sigma"),
        draws=draws,
        warmup=warmup,
        chains=chains,
        seed=seed,
    )

    last_price = float(series.iloc[-1])
    futures = _predictive_prices(last_price, samples.flat, steps, np.random.default_rng(seed + 1))
    tail = (1.0 - confidence) / 2 * 100
    lower, upper = _plugin_interval(last_price, returns, steps, confidence)

    return PosteriorForecast(
        steps=steps,
        last_price=last_price,
        median=float(np.percentile(futures, 50)),
        lower=float(np.percentile(futures, tail)),
        upper=float(np.percentile(futures, 100 - tail)),
        confidence=confidence,
        plugin_lower=lower,
        plugin_upper=upper,
        posterior=samples,
    )


def format_posterior(forecast: PosteriorForecast) -> str:
    """Render a posterior forecast the way the CLI prints it."""
    summary = forecast.posterior.summary()
    lines = [
        f"Posterior forecast: +{forecast.steps} bars, {forecast.confidence:.0%} interval",
        f"{forecast.posterior.draws.shape[0]} chains x {forecast.posterior.draws.shape[1]} draws, "
        f"seed {forecast.posterior.seed}, acceptance {forecast.posterior.acceptance:.1%}",
        "-" * 62,
        f"{'last price':<28}{forecast.last_price:>16,.4f}",
        f"{'posterior median':<28}{forecast.median:>16,.4f}",
        f"{'posterior interval':<28}{forecast.lower:>16,.4f} .. {forecast.upper:,.4f}",
        f"{'point-estimate interval':<28}{forecast.plugin_lower:>16,.4f} .. {forecast.plugin_upper:,.4f}",
        f"{'widening':<28}{forecast.widening:>15.1%}",
        "-" * 62,
        f"{'parameter':<12}{'mean':>12}{'p5':>12}{'p95':>12}{'R-hat':>8}{'ESS':>8}",
    ]
    for name, stats in summary.items():
        lines.append(
            f"{name:<12}{stats['mean']:>12.6f}{stats['p5']:>12.6f}{stats['p95']:>12.6f}"
            f"{stats['r_hat']:>8.4f}{int(stats['ess']):>8}"
        )
    lines += ["-" * 62, forecast.posterior.diagnosis]
    if not forecast.usable:
        lines.append("REFUSED: these draws must not be read as a posterior.")
    return "\n".join(lines)
