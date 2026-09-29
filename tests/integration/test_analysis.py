from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from execlens.diagnostics.features import FEATURE_COLUMNS
from execlens.services.analysis import (
    AnalysisConfig,
    AnalysisResult,
    analyze_directory,
    save_analysis,
)
from execlens.tca.engine import run_tca

SAMPLE_DIR = Path(__file__).resolve().parents[2] / "data" / "sample"


@pytest.fixture(scope="module")
def analysis() -> AnalysisResult:
    return analyze_directory(SAMPLE_DIR)


def test_orders_carry_tca_features_and_flags(analysis: AnalysisResult) -> None:
    columns = set(analysis.orders.columns)
    assert {"arrival_cost_bps", "vwap_cost_bps", "markout_60s_bps"} <= columns
    assert set(FEATURE_COLUMNS) <= columns
    assert {"flags", "flag_reasons", "context_flags", "context_notes", "is_flagged"} <= columns
    assert analysis.orders.height == analysis.dataset.orders.height


def test_analysis_uses_the_tca_engine_unchanged(analysis: AnalysisResult) -> None:
    """The service composes domain modules; it must not alter TCA numbers."""
    ds = analysis.dataset
    direct = run_tca(ds.quotes, ds.trades, ds.orders, ds.fills).orders
    cols = ["order_id", "arrival_cost_bps", "vwap_cost_bps", "effective_spread_bps"]
    assert (
        analysis.orders.select(cols).sort("order_id").equals(direct.select(cols).sort("order_id"))
    )


def test_summary_is_consistent_with_the_order_table(analysis: AnalysisResult) -> None:
    summary = analysis.summary
    assert summary.analyzed_orders == analysis.orders.height
    assert summary.flagged_orders == analysis.orders["is_flagged"].sum()
    assert summary.arrival_cost_bps.median == pytest.approx(
        analysis.orders["arrival_cost_bps"].median()
    )
    flagged = analysis.flag_counts.filter(pl.col("category") != "context")
    assert flagged["orders"].sum() == analysis.orders["n_flags"].sum()


def test_sample_data_exercises_the_diagnostics(analysis: AnalysisResult) -> None:
    by_symbol = dict(
        analysis.orders.group_by("symbol")
        .agg(pl.col("context_flags").list.len().gt(0).mean())
        .iter_rows()
    )
    # Regime-specific context is recognised on the stressed synthetic symbols.
    assert by_symbol["SYN_WIDE"] > 0.8
    assert by_symbol["SYN_HIVOL"] > 0.5
    assert by_symbol["SYN_NORMAL"] < 0.3


def test_market_window_and_order_accessors(analysis: AnalysisResult) -> None:
    order_id = analysis.orders["order_id"][0]
    order = analysis.order(order_id)
    assert order is not None
    quotes, trades = analysis.market_window(order_id)
    assert quotes.height > 0
    assert trades.height > 0
    assert set(quotes["symbol"].unique()) == {order["symbol"]}
    assert quotes["timestamp"].min() <= order["arrival_timestamp"]
    fills = analysis.fills_for(order_id)
    assert (fills["order_id"] == order_id).all()
    assert analysis.order("nope") is None
    empty_quotes, empty_trades = analysis.market_window("nope")
    assert empty_quotes.is_empty()
    assert empty_trades.is_empty()


def test_save_analysis_writes_derived_tables(analysis: AnalysisResult, tmp_path: Path) -> None:
    save_analysis(analysis, tmp_path)
    assert pl.read_parquet(tmp_path / "orders_tca.parquet").height == analysis.orders.height
    assert pl.read_parquet(tmp_path / "fills_tca.parquet").height == analysis.fills.height
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["analyzed_orders"] == analysis.orders.height
    config = json.loads((tmp_path / "analysis_config.json").read_text(encoding="utf-8"))
    assert config["stale_quote_tolerance_seconds"] == 30.0


def test_configuration_flows_into_the_engine() -> None:
    config = AnalysisConfig(stale_quote_tolerance_seconds=0.5, markout_horizons_seconds=(5, 60))
    result = analyze_directory(SAMPLE_DIR, config)
    assert "markout_5s_bps" in result.orders.columns
    assert "markout_300s_bps" not in result.orders.columns
    # A half-second tolerance makes some context stale in the sparse scenario.
    assert (result.orders["arrival_quote_status"] == "STALE").any()
    with pytest.raises(ValueError, match="flag markout horizon"):
        AnalysisConfig(markout_horizons_seconds=(10,)).flags()
