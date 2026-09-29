from __future__ import annotations

import polars as pl
import pytest

from execlens.tca.aggregation import (
    markout_horizons,
    metric_stats,
    summarize_by,
    summarize_orders,
)


def orders_table() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "order_id": ["A", "B", "C", "D", "E"],
            "symbol": ["X", "X", "Y", "Y", "Y"],
            "strategy": ["TWAP", "VWAP", "TWAP", "POV", "POV"],
            "n_fills": [2, 1, 0, 3, 1],
            "executed_quantity": [100, 300, 0, 50, 50],
            "executed_notional": [1_000.0, 3_000.0, 0.0, 500.0, 500.0],
            "completion_ratio": [1.0, 1.0, 0.0, 0.5, 1.0],
            "arrival_cost_bps": [10.0, -2.0, None, 4.0, 100.0],
            "vwap_cost_bps": [1.0, 2.0, None, 3.0, None],
            "effective_spread_bps": [4.0, 6.0, None, None, 2.0],
            "participation_rate": [0.1, 0.2, 0.0, 0.3, 0.4],
            "markout_10s_bps": [1.0, -1.0, None, 3.0, 5.0],
            "markout_60s_bps": [None, None, None, None, 2.0],
            "is_flagged": [False, True, False, False, True],
        }
    )


def test_metric_stats_by_hand() -> None:
    frame = pl.DataFrame({"x": [1.0, 2.0, 3.0, 4.0, 100.0, None]})
    stats = metric_stats(frame, "x")
    assert stats.available == 5
    assert stats.median == 3.0
    assert stats.mean == pytest.approx(22.0)
    # Linear interpolation: position 0.95 * (5 - 1) = 3.8 -> 4 + 0.8 * (100 - 4) = 80.8.
    assert stats.p95 == pytest.approx(80.8)


def test_metric_stats_with_no_data() -> None:
    stats = metric_stats(pl.DataFrame({"x": [None]}, schema={"x": pl.Float64}), "x")
    assert stats.available == 0
    assert stats.mean is None
    assert stats.p95 is None


def test_summary_by_hand() -> None:
    summary = summarize_orders(orders_table())
    assert summary.analyzed_orders == 5
    assert summary.orders_with_fills == 4
    assert summary.total_fills == 7
    assert summary.executed_quantity == 500
    assert summary.executed_notional == pytest.approx(5_000.0)
    # Notional-weighted over orders with a cost:
    # (10*1000 - 2*3000 + 4*500 + 100*500) / 5000 = 56000 / 5000 = 11.2 bps.
    assert summary.notional_weighted_arrival_cost_bps == pytest.approx(11.2)
    # Simple statistics over the four available costs [10, -2, 4, 100].
    assert summary.arrival_cost_bps.available == 4
    assert summary.arrival_cost_bps.mean == pytest.approx(28.0)
    assert summary.arrival_cost_bps.median == pytest.approx(7.0)
    assert summary.full_fill_rate == pytest.approx(3 / 5)
    assert summary.flagged_orders == 2
    assert summary.flagged_fraction == pytest.approx(0.4)
    assert summary.markout_bps["10s"].mean == pytest.approx(2.0)
    assert summary.markout_bps["60s"].available == 1


def test_summary_without_flags_or_costs() -> None:
    table = (
        orders_table()
        .drop("is_flagged")
        .with_columns(pl.lit(None, pl.Float64).alias("arrival_cost_bps"))
    )
    summary = summarize_orders(table)
    assert summary.flagged_orders is None
    assert summary.notional_weighted_arrival_cost_bps is None


def test_markout_horizons_are_discovered_in_order() -> None:
    assert markout_horizons(orders_table()) == [10, 60]


def test_summarize_by_group() -> None:
    grouped = summarize_by(orders_table(), "symbol")
    assert grouped["symbol"].to_list() == ["X", "Y"]
    x = grouped.row(0, named=True)
    assert x["orders"] == 2
    assert x["mean_arrival_cost_bps"] == pytest.approx(4.0)
    assert x["flagged_fraction"] == pytest.approx(0.5)
    assert x["mean_markout_10s_bps"] == pytest.approx(0.0)
    by_two = summarize_by(orders_table(), ["symbol", "strategy"])
    assert by_two.height == 4
