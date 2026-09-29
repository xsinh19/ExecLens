from __future__ import annotations

import polars as pl
import pytest
from pydantic import ValidationError

from execlens.simulation.comparison import ComparisonRequest, compare_strategies

SMALL = {"session_seconds": 3_600, "horizon_seconds": 900, "trials": 3, "quantity": 5_000}


@pytest.fixture(scope="module")
def comparison():  # type: ignore[no-untyped-def]
    return compare_strategies(ComparisonRequest(**SMALL, include_oracle=True))


def test_every_strategy_trades_the_same_order_in_the_same_market(comparison) -> None:  # type: ignore[no-untyped-def]
    per_trial = comparison.orders.group_by("trial").agg(
        pl.col("arrival_timestamp").n_unique().alias("arrivals"),
        pl.col("arrival_mid").n_unique().alias("mids"),
        pl.col("quantity").n_unique().alias("quantities"),
        pl.col("strategy").n_unique().alias("strategies"),
    )
    assert per_trial.height == 3
    assert (per_trial["arrivals"] == 1).all()
    assert (per_trial["mids"] == 1).all()
    assert (per_trial["quantities"] == 1).all()
    assert (per_trial["strategies"] == 5).all()


def test_summary_rows_and_oracle_labelling(comparison) -> None:  # type: ignore[no-untyped-def]
    summary = comparison.summary
    assert set(summary["strategy"]) == {"IMMEDIATE", "TWAP", "VWAP", "POV", "ORACLE_VWAP"}
    oracle = summary.filter(pl.col("strategy") == "ORACLE_VWAP")
    assert oracle["implementable"].to_list() == [False]
    assert summary["implementable"].to_list()[-1] is False  # hindsight benchmark listed last
    assert (summary["trials"] == 3).all()


def test_no_strategy_overfills(comparison) -> None:  # type: ignore[no-untyped-def]
    assert (comparison.orders["executed_quantity"] <= comparison.orders["quantity"]).all()
    assert (comparison.orders["completion_ratio"] <= 1.0).all()


def test_conclusions_are_scenario_specific_and_caveated(comparison) -> None:  # type: ignore[no-untyped-def]
    text = " ".join(comparison.conclusions)
    assert "normal scenario" in text
    assert "do not show that any strategy is generally best" in text
    assert "ORACLE_VWAP" not in comparison.conclusions[0]  # rankings cover implementable only


def test_comparison_is_deterministic(comparison) -> None:  # type: ignore[no-untyped-def]
    again = compare_strategies(ComparisonRequest(**SMALL, include_oracle=True))
    assert again.summary.equals(comparison.summary)
    assert again.conclusions == comparison.conclusions


def test_different_seed_changes_the_realisation() -> None:
    a = compare_strategies(ComparisonRequest(**SMALL, strategies=("TWAP",), seed=1))
    b = compare_strategies(ComparisonRequest(**SMALL, strategies=("TWAP",), seed=2))
    assert not a.orders["arrival_mid"].equals(b.orders["arrival_mid"])


@pytest.mark.parametrize(
    "overrides",
    [
        {"scenario": "martian_market"},
        {"strategies": ("TWAP", "MAGIC")},
        {"strategies": ()},
        {"strategies": ("TWAP", "TWAP")},
        {"horizon_seconds": 3_000},  # does not fit a 3,600 s session with buffers
        {"quantity": 0},
        {"trials": 0},
    ],
)
def test_invalid_requests_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ComparisonRequest(**(SMALL | overrides))  # type: ignore[arg-type]
