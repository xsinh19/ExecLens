from __future__ import annotations

import polars as pl
import pytest
from pydantic import ValidationError

from execlens.simulation.comparison import (
    MAX_DECISIONS,
    MAX_SIMULATED_SECONDS,
    ComparisonRequest,
    best_strategy,
    compare_strategies,
    ranked_strategies,
)
from execlens.simulation.simulator import SimulationConfig

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


def test_ranking_ignores_strategies_that_left_the_order_unfilled() -> None:
    summary = pl.DataFrame(
        {
            "strategy": ["TWAP", "POV", "IMMEDIATE", "ORACLE_VWAP"],
            "implementable": [True, True, True, False],
            "mean_arrival_cost_bps": [5.0, 1.0, 9.0, 0.5],
            "mean_completion_ratio": [1.0, 0.2, 1.0, 1.0],
        }
    )
    assert ranked_strategies(summary)["strategy"].to_list() == ["TWAP", "IMMEDIATE"]
    assert best_strategy(summary) == "TWAP"
    everyone_incomplete = summary.with_columns(pl.lit(0.5).alias("mean_completion_ratio"))
    assert best_strategy(everyone_incomplete) == "POV"


def test_low_liquidity_conclusions_do_not_crown_an_incomplete_strategy() -> None:
    result = compare_strategies(
        ComparisonRequest(scenario="low_liquidity", quantity=20_000, horizon_seconds=900, trials=3)
    )
    pov = result.summary.filter(pl.col("strategy") == "POV").row(0, named=True)
    assert pov["mean_completion_ratio"] < 0.95
    assert "POV" not in result.conclusions[0]
    assert "left out of the ranking" in " ".join(result.conclusions)


def test_each_trial_simulates_only_the_window_its_order_needs(comparison) -> None:  # type: ignore[no-untyped-def]
    request = comparison.request
    arrivals = comparison.orders.group_by("trial").agg(pl.col("arrival_timestamp").first())
    for _, arrival in arrivals.iter_rows():
        day = comparison.market.quotes.filter(pl.col("timestamp").dt.date() == arrival.date())
        first, last = day["timestamp"].min(), day["timestamp"].max()
        assert (arrival - first).total_seconds() >= request.lookback_seconds  # impact look-back
        assert (last - arrival).total_seconds() >= request.horizon_seconds + 300  # markouts
        assert (last - first).total_seconds() < request.window_seconds
    # The window is wide enough for every markout of every fill.
    for h in (10, 30, 60, 300):
        assert (comparison.orders[f"markout_{h}s_unavailable_fills"] == 0).all()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"trials": 100, "horizon_seconds": 3_600}, "simulated seconds"),
        ({"trials": 100, "simulation": SimulationConfig(slice_seconds=1.0)}, "strategy decisions"),
    ],
)
def test_requests_over_the_work_budget_are_rejected(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ComparisonRequest(**overrides)  # type: ignore[arg-type]


def test_the_budget_admits_the_documented_workloads() -> None:
    largest = ComparisonRequest(trials=100)  # CLI maximum at the default 30-minute horizon
    assert largest.trials * largest.window_seconds <= MAX_SIMULATED_SECONDS
    assert largest.max_decisions <= MAX_DECISIONS
    with pytest.raises(ValidationError):
        SimulationConfig(slice_seconds=0.5)  # sub-second decisions are not supported
