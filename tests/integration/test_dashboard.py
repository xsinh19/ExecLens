"""Dashboard pages run headlessly (Streamlit AppTest) and show the engine's numbers."""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest
from streamlit.testing.v1 import AppTest

from execlens.dashboard import charts
from execlens.dashboard.data import (
    compact,
    fmt_bps,
    fmt_ratio,
    load_benchmark_results,
    overview_kpis,
    symbol_scenarios,
)
from execlens.dashboard.theme import DARK, LIGHT, STRATEGY_ORDER, strategy_color
from execlens.services.analysis import AnalysisResult, analyze_directory

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DIR = ROOT / "data" / "sample"


@pytest.fixture(autouse=True)
def sample_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXECLENS_DATA_DIR", str(SAMPLE_DIR))
    monkeypatch.setenv("EXECLENS_BENCHMARK_RESULTS", str(ROOT / "benchmarks" / "results.json"))


@pytest.fixture(scope="module")
def analysis() -> AnalysisResult:
    return analyze_directory(SAMPLE_DIR)


def run_page(page: str) -> AppTest:
    app = AppTest.from_string(
        f"from execlens.dashboard import pages\npages.{page}()", default_timeout=180
    )
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    return app


def test_overview_tiles_show_the_domain_summary(analysis: AnalysisResult) -> None:
    app = run_page("overview")
    shown = {metric.label: metric.value for metric in app.metric}
    summary = analysis.summary
    # Independently formatted from the summary, not from dashboard helpers.
    assert shown["Analysed orders"] == f"{summary.analyzed_orders:,}"
    assert shown["Median arrival cost"] == f"{summary.arrival_cost_bps.median:,.2f} bps"
    assert shown["Mean arrival cost"] == f"{summary.arrival_cost_bps.mean:,.2f} bps"
    assert shown["p95 arrival cost"] == f"{summary.arrival_cost_bps.p95:,.2f} bps"
    assert shown["Median VWAP cost"] == f"{summary.vwap_cost_bps.median:,.2f} bps"
    assert shown["Mean effective spread"] == f"{summary.effective_spread_bps.mean:,.2f} bps"
    assert shown["Flagged executions"] == f"{summary.flagged_fraction:.1%}"


def test_order_explorer_shows_the_selected_orders_metrics(analysis: AnalysisResult) -> None:
    app = run_page("order_explorer")
    order_id = app.selectbox[2].value  # symbol, strategy, then the order picker
    row = analysis.order(order_id)
    assert row is not None
    shown = {metric.label: metric.value for metric in app.metric}
    assert shown["Arrival cost"] == f"{row['arrival_cost_bps']:,.2f} bps"
    assert shown["VWAP cost"] == fmt_bps(row["vwap_cost_bps"])
    assert len(app.dataframe) == 1


def test_order_explorer_filters_narrow_the_choice() -> None:
    app = run_page("order_explorer")
    app.selectbox[0].select("SYN_ILLIQ").run()
    assert not app.exception
    assert all(option.startswith("SYN_ILLIQ") for option in app.selectbox[2].options)


def test_diagnostics_page_lists_flagged_orders(analysis: AnalysisResult) -> None:
    app = run_page("diagnostics")
    flagged_table = app.dataframe[-1].value
    assert len(flagged_table) == int(analysis.orders["is_flagged"].sum())


def test_strategy_lab_runs_a_comparison() -> None:
    app = run_page("strategy_lab")
    assert app.info[0].value.startswith("Choose parameters")
    trials = next(s for s in app.slider if s.label == "Trials (sessions)")
    trials.set_value(3).run()
    app.button[0].click().run()
    assert not app.exception, [e.value for e in app.exception]
    conclusions = [m.value for m in app.markdown if "Lowest mean arrival cost" in m.value]
    assert conclusions
    assert "normal scenario" in conclusions[0]  # SYN_NORMAL is the default symbol
    results = app.dataframe[-1].value
    assert set(results["strategy"]) == set(STRATEGY_ORDER)


def test_benchmarks_page_reads_the_results_file() -> None:
    results = json.loads((ROOT / "benchmarks" / "results.json").read_text(encoding="utf-8"))
    app = run_page("benchmarks")
    shown = {metric.label: metric.value for metric in app.metric}
    speedup = results["tca"]["workloads"]["core_in_memory"]["speedup_median"]
    assert shown["Measured speedup (core TCA)"] == f"{speedup:.1f}x"


def test_benchmarks_page_without_results(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXECLENS_BENCHMARK_RESULTS", str(tmp_path / "missing.json"))
    app = run_page("benchmarks")
    assert "Run `execlens benchmark`" in app.info[0].value


def test_full_app_navigation_renders_the_overview() -> None:
    app = AppTest.from_file(str(ROOT / "src" / "execlens" / "dashboard" / "app.py"))
    app.run(timeout=180)
    assert not app.exception
    assert app.title[0].value == "Execution overview"


def test_strategy_colours_are_fixed_per_entity() -> None:
    assert [strategy_color(LIGHT, s) for s in STRATEGY_ORDER] == list(LIGHT.series[:4])
    assert strategy_color(DARK, "TWAP") == DARK.series[1]
    assert strategy_color(LIGHT, "ORACLE_VWAP") == LIGHT.deemphasis


def test_order_chart_draws_quotes_fills_and_benchmarks(analysis: AnalysisResult) -> None:
    order_id = analysis.orders.filter(pl.col("market_vwap").is_not_null())["order_id"][0]
    order = analysis.order(order_id)
    assert order is not None
    quotes, _ = analysis.market_window(order_id)
    figure = charts.order_chart(quotes, analysis.fills_for(order_id), order, LIGHT)
    names = [trace.name for trace in figure.data]
    assert names == ["Ask", "Bid / ask", "Mid", "Fills", "Interval VWAP"]


def test_formatting_helpers() -> None:
    assert compact(322_409_210.0) == "322.4M"
    assert compact(950.0) == "950"
    assert fmt_bps(None) == "n/a"
    assert fmt_ratio(0.304) == "30.4%"
    assert load_benchmark_results(Path("does-not-exist.json")) is None


def test_symbol_scenarios_follow_scenario_order(analysis: AnalysisResult) -> None:
    mapping = symbol_scenarios(analysis)
    assert next(iter(mapping)) == "SYN_NORMAL"
    assert mapping["SYN_ILLIQ"] == "low_liquidity"


def test_kpis_are_read_from_the_summary(analysis: AnalysisResult) -> None:
    kpis = {k.label: k for k in overview_kpis(analysis.summary)}
    assert kpis["Executed notional"].value == compact(analysis.summary.executed_notional)
    assert "positive = worse" in kpis["Median arrival cost"].help
