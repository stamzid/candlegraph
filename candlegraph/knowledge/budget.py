"""The credit budget: pure decisions over counts the caller supplies.

Tavily is metered in credits and the free tier is monthly, so the failure
mode to design against is a loop spending the month in an hour. This module
reads nothing and calls nothing (``docs/knowledge/tavily/DESIGN.md``): the
caller sums the ``source_call`` ledger and hands the counts in, which is
what makes every boundary here a plain unit test.

Severity thresholds for the observer's ``source_budget`` finding (step 9)
and the News dashboard live here too, so "WARNING at 80%" is written once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Reason = Literal["ok", "daily", "monthly", "reserve"]
Level = Literal["ok", "warning", "critical"]

# The observer's severity thresholds for budget usage.
WARNING_FRACTION = 0.80
CRITICAL_FRACTION = 1.00


class BudgetError(ValueError):
    """The budget configuration cannot work."""


@dataclass(frozen=True)
class Budget:
    """The caps the guard enforces.

    Attributes:
        monthly_credits: The plan's monthly allowance (``TAVILY_MONTHLY_CREDITS``).
        daily_credits: Smooths the month: a runaway loop hits this long
            before it hits the month (``TAVILY_DAILY_CREDITS``).
        reserve: Credits held back for an operator's manual search on the
            last day of the month. The pipeline treats them as already spent.
    """

    monthly_credits: int
    daily_credits: int
    reserve: int = 50

    def __post_init__(self) -> None:
        if self.monthly_credits < 1:
            raise BudgetError("monthly_credits must be at least 1")
        if self.daily_credits < 1:
            raise BudgetError("daily_credits must be at least 1")
        if self.reserve < 0:
            raise BudgetError("reserve must not be negative")
        if self.reserve >= self.monthly_credits:
            raise BudgetError(
                f"a reserve of {self.reserve} leaves nothing of {self.monthly_credits} monthly credits"
            )

    @property
    def usable_monthly(self) -> int:
        """What the pipeline may spend: the month minus the operator reserve."""
        return self.monthly_credits - self.reserve


@dataclass(frozen=True)
class Decision:
    """Allow, or refuse with the binding constraint named."""

    allowed: bool
    reason: Reason

    def __bool__(self) -> bool:
        return self.allowed


ALLOW = Decision(allowed=True, reason="ok")


def decide(spent_this_month: int, spent_today: int, cost: int, budget: Budget) -> Decision:
    """Would spending ``cost`` more credits stay inside the budget?

    The most absolute constraint wins the naming: a call that would breach
    the plan itself is ``monthly`` even though it also breaches the reserve.

    Raises:
        BudgetError: a non-positive cost, or negative spend counts — those
            are caller bugs, not refusals.
    """
    if cost < 1:
        raise BudgetError("a call that costs nothing needs no budget decision")
    if spent_this_month < 0 or spent_today < 0:
        raise BudgetError("spent counts cannot be negative")

    if spent_this_month + cost > budget.monthly_credits:
        return Decision(allowed=False, reason="monthly")
    if spent_this_month + cost > budget.usable_monthly:
        return Decision(allowed=False, reason="reserve")
    if spent_today + cost > budget.daily_credits:
        return Decision(allowed=False, reason="daily")
    return ALLOW


def usage_fraction(spent: int, cap: int) -> float:
    """``spent / cap``, defined as 0 for a zero cap rather than dividing by it."""
    return spent / cap if cap > 0 else 0.0


def usage_level(fraction: float) -> Level:
    """The observer's grading: OK below 80%, WARNING from 80%, CRITICAL from 100%."""
    if fraction >= CRITICAL_FRACTION:
        return "critical"
    if fraction >= WARNING_FRACTION:
        return "warning"
    return "ok"
