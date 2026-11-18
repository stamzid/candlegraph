"""The parameters a pipeline spec may set, one model per job.

``Job.params`` points at one of these, and the loader validates a step's
``params:`` against it (``docs/harness/pipelines/DESIGN.md`` §5.2). A job
without one takes no parameters at all, and a spec that passes some is
refused — failing closed, so a typo in a param name can never be ignored.

**This module must stay free of domain imports.** The registry imports it,
and importing the registry must not pull pandas, LangGraph or the strategies
in (invariant 1). Where a check genuinely needs the domain — "is `bollinger`
a registered strategy?" — the import goes *inside* a validator, which runs
when a spec is loaded, not when this module is.

One asymmetry worth stating: strategy names are checked at load time and
**symbols are not**. A strategy is code and is the same everywhere, so CI can
refuse an unknown one. The token list is deployment configuration, and a spec
validated against this repository's `tokens.yaml` must not fail in a
deployment that tracks a different set — so a symbol is checked by the job
body, against the registry that deployment actually runs.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Params(BaseModel):
    """Unknown keys are refused, and a loaded param set never changes."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SymbolsMixin(_Params):
    """Jobs that work over a set of tokens share this one field."""

    symbols: tuple[str, ...] = ()
    """Empty means every **active** token — the set the deployment polls."""

    @field_validator("symbols")
    @classmethod
    def _non_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not symbol.strip() for symbol in value):
            raise ValueError("a symbol must not be blank")
        return value


class ValidateParams(SymbolsMixin):
    """``validate``: is the stored history good enough to compute on?"""

    days: int | None = Field(default=None, ge=1)
    """Window the report covers; omitted means the configured lookback."""
    min_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    """Overrides ``MIN_EXPECTED_COVERAGE`` for this step only."""


class AnalyseParams(SymbolsMixin):
    """``analyse``: rolling statistics over the stored history."""

    window: int = Field(default=24, ge=2)
    days: int | None = Field(default=None, ge=1)


class StrategiesMixin(SymbolsMixin):
    """Jobs that run the registered strategies over those tokens."""

    strategies: tuple[str, ...] = ()
    """Empty means every registered strategy. ``all`` is accepted and means the same."""
    days: int | None = Field(default=None, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _spell_all(cls, data: Any) -> Any:
        # The design's example spec writes `strategies: all`; it reads well and
        # means "every registered one", which is what the empty tuple means.
        if isinstance(data, dict) and str(data.get("strategies", "")).casefold() == "all":
            return {**data, "strategies": ()}
        return data

    @field_validator("strategies")
    @classmethod
    def _registered(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        # Imported here, not at module scope: see the module docstring.
        from candlegraph.strategies.base import available

        known = available()
        unknown = [name for name in value if name not in known]
        if unknown:
            plural = "y" if len(unknown) == 1 else "ies"
            raise ValueError(f"unknown strateg{plural} {', '.join(unknown)}; registered: {', '.join(known)}")
        return value


class SignalParams(StrategiesMixin):
    """``signal``: run the registered strategies and store what they say."""

    store: bool = True
    """False computes and counts without writing, for a dry run."""


class ExplainParams(StrategiesMixin):
    """``explain``: cited prose for the newest signal of each strategy.

    There is deliberately **no ``search`` parameter**. Spending a search
    credit stays something a person asks for (the UI button, the CLI) or the
    daily news job does on its own budget; a pipeline that could turn it on
    would spend once an hour, for every token, without anyone deciding to.
    See the pipelines design §16, answer 2.
    """


class EmbedParams(_Params):
    """``embed``: give the pending chunks the active model's vector."""

    limit: int | None = Field(default=512, ge=1)
    """About this many rows per run; ``None`` runs to the end of the backlog."""
    batch_size: int | None = Field(default=None, ge=1)
