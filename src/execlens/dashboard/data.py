"""Data access and display preparation for the dashboard.

Everything numeric shown on screen is read from the analysis / simulation
services; this module only selects, labels and formats it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl
import streamlit as st

from execlens.config import Settings
from execlens.data.synthetic import SCENARIOS
from execlens.services.analysis import AnalysisConfig, AnalysisResult, analyze_directory
from execlens.simulation.comparison import ComparisonRequest, compare_strategies
from execlens.tca.aggregation import TcaSummary


@st.cache_resource(show_spinner="Analysing dataset (TCA, diagnostics)...")
def _load(data_dir: str, stale_tolerance: float) -> AnalysisResult:
    return analyze_directory(
        Path(data_dir), AnalysisConfig(stale_quote_tolerance_seconds=stale_tolerance)
    )


def current_analysis() -> AnalysisResult:
    """The analysed dataset named by ``EXECLENS_DATA_DIR`` (computed once per process)."""
    settings = Settings.from_env()
    return _load(str(settings.data_dir), settings.stale_quote_tolerance_seconds)


@st.cache_data(show_spinner="Simulating strategies on identical synthetic sessions...")
def run_comparison(
    request_json: str,
) -> tuple[pl.DataFrame, pl.DataFrame, tuple[str, ...]]:
    """Cached strategy comparison keyed by the serialised request."""
    result = compare_strategies(ComparisonRequest.model_validate_json(request_json))
    return result.summary, result.orders, result.conclusions


def fmt_bps(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.2f} bps"


def fmt_ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def compact(value: float) -> str:
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= size:
            return f"{value / size:,.1f}{suffix}"
    return f"{value:,.0f}"


@dataclass(frozen=True)
class Kpi:
    label: str
    value: str
    help: str


def overview_kpis(summary: TcaSummary) -> list[Kpi]:
    """Headline tiles, read directly from the portfolio summary."""
    return [
        Kpi("Analysed orders", f"{summary.analyzed_orders:,}", "Parent orders in the dataset."),
        Kpi(
            "Executed notional",
            compact(summary.executed_notional),
            f"Sum of fill price x quantity; {summary.executed_quantity:,} shares in "
            f"{summary.total_fills:,} fills.",
        ),
        Kpi(
            "Median arrival cost",
            fmt_bps(summary.arrival_cost_bps.median),
            "Implementation shortfall vs arrival mid; positive = worse. "
            f"Notional-weighted: {fmt_bps(summary.notional_weighted_arrival_cost_bps)}.",
        ),
        Kpi(
            "Mean arrival cost",
            fmt_bps(summary.arrival_cost_bps.mean),
            f"Across {summary.arrival_cost_bps.available} orders with arrival context.",
        ),
        Kpi(
            "p95 arrival cost",
            fmt_bps(summary.arrival_cost_bps.p95),
            "95th percentile: the expensive tail.",
        ),
        Kpi(
            "Median VWAP cost",
            fmt_bps(summary.vwap_cost_bps.median),
            "Average fill vs interval market VWAP; positive = worse than VWAP.",
        ),
        Kpi(
            "Mean effective spread",
            fmt_bps(summary.effective_spread_bps.mean),
            "2 x signed distance of fills from the contemporaneous mid.",
        ),
        Kpi(
            "Flagged executions",
            fmt_ratio(summary.flagged_fraction),
            f"{summary.flagged_orders} orders with an execution or data-quality flag.",
        ),
    ]


def symbol_scenarios(analysis: AnalysisResult) -> dict[str, str]:
    """Synthetic symbol -> scenario name from the dataset metadata, in scenario order."""
    scenarios = analysis.dataset.metadata.get("scenarios", {})
    if not isinstance(scenarios, dict):
        return {}
    mapping = {
        str(symbol): str(params["name"])
        for symbol, params in scenarios.items()
        if isinstance(params, dict) and params.get("name") in SCENARIOS
    }
    order = list(SCENARIOS)
    return dict(sorted(mapping.items(), key=lambda item: order.index(item[1])))


def order_label(row: dict[str, Any]) -> str:
    flag = " [flagged]" if row["is_flagged"] else ""
    return f"{row['order_id']} - {row['side']} {row['quantity']:,} {row['strategy'] or ''}{flag}"


def load_benchmark_results(path: Path) -> dict[str, Any] | None:
    """Benchmark results written by ``execlens benchmark`` (None if absent or unreadable)."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None
