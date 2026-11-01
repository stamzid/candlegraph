"""The budget guard: pure decisions, so every boundary is pinned exactly.

The rollout's step-6 contract: decisions at 79%, 80% and 100% of both caps
and the reserve. 80% is not a refusal boundary — that is the observer's
WARNING threshold — so both scales are tested here and written once.
"""

from __future__ import annotations

import pytest

from candlegraph.knowledge.budget import (
    Budget,
    BudgetError,
    decide,
    usage_fraction,
    usage_level,
)

# monthly 1000, reserve 50 -> the pipeline may spend 950; daily 40.
BUDGET = Budget(monthly_credits=1000, daily_credits=40, reserve=50)


# --- configuration -----------------------------------------------------------


def test_the_reserve_is_subtracted_from_what_the_pipeline_may_spend() -> None:
    assert BUDGET.usable_monthly == 950


@pytest.mark.parametrize(
    "kwargs",
    [
        {"monthly_credits": 0, "daily_credits": 40},
        {"monthly_credits": 1000, "daily_credits": 0},
        {"monthly_credits": 1000, "daily_credits": 40, "reserve": -1},
        {"monthly_credits": 50, "daily_credits": 40, "reserve": 50},
    ],
)
def test_a_budget_that_cannot_work_is_rejected(kwargs: dict[str, int]) -> None:
    with pytest.raises(BudgetError):
        Budget(**kwargs)


# --- the daily cap ------------------------------------------------------------


def test_at_79_percent_of_the_day_a_call_is_allowed() -> None:
    decision = decide(spent_this_month=100, spent_today=31, cost=1, budget=BUDGET)  # 31.6/40 = 79%

    assert decision.allowed and decision.reason == "ok"
    assert bool(decision)


def test_at_80_percent_of_the_day_a_call_is_still_allowed_the_cap_is_the_cap() -> None:
    """80% is the observer's WARNING, not a refusal; spending continues to the cap."""
    assert decide(100, 32, 1, BUDGET).allowed


def test_the_last_credit_of_the_day_is_spendable_and_the_next_is_not() -> None:
    assert decide(100, 39, 1, BUDGET).allowed
    refused = decide(100, 40, 1, BUDGET)
    assert not refused.allowed and refused.reason == "daily"


def test_a_cost_that_would_overshoot_the_day_is_refused_even_from_below() -> None:
    refused = decide(100, 39, 2, BUDGET)

    assert not refused.allowed and refused.reason == "daily"


# --- the monthly cap and the reserve --------------------------------------------


def test_at_79_percent_of_the_month_a_call_is_allowed() -> None:
    assert decide(790, 0, 1, BUDGET).allowed


def test_at_80_percent_of_the_month_a_call_is_allowed() -> None:
    assert decide(800, 0, 1, BUDGET).allowed


def test_the_pipeline_stops_at_the_reserve_not_at_the_plan() -> None:
    assert decide(949, 0, 1, BUDGET).allowed
    refused = decide(950, 0, 1, BUDGET)
    assert not refused.allowed and refused.reason == "reserve"


def test_at_100_percent_of_the_plan_the_reason_is_monthly() -> None:
    """The most absolute constraint names the refusal."""
    refused = decide(1000, 0, 1, BUDGET)

    assert not refused.allowed and refused.reason == "monthly"


def test_a_cost_spanning_the_plan_cap_is_monthly_not_reserve() -> None:
    assert decide(999, 0, 2, BUDGET).reason == "monthly"


def test_the_monthly_refusal_wins_over_the_daily_one() -> None:
    assert decide(1000, 40, 1, BUDGET).reason == "monthly"


# --- caller bugs are errors, not refusals -----------------------------------------


@pytest.mark.parametrize(("month", "today", "cost"), [(0, 0, 0), (0, 0, -1), (-1, 0, 1), (0, -1, 1)])
def test_nonsense_inputs_raise_rather_than_refuse(month: int, today: int, cost: int) -> None:
    with pytest.raises(BudgetError):
        decide(month, today, cost, BUDGET)


# --- the observer's grading -------------------------------------------------------


@pytest.mark.parametrize(
    ("spent", "expected"),
    [(790, "ok"), (799, "ok"), (800, "warning"), (999, "warning"), (1000, "critical"), (1200, "critical")],
)
def test_usage_levels_turn_at_80_and_100_percent(spent: int, expected: str) -> None:
    assert usage_level(usage_fraction(spent, 1000)) == expected


def test_a_zero_cap_has_zero_usage_rather_than_a_division_error() -> None:
    assert usage_fraction(10, 0) == 0.0
