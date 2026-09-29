"""Dashboard pages. Each page is a function so the app (and the tests) can run it."""

from __future__ import annotations

from typing import Any

import polars as pl
import streamlit as st

from execlens.config import Settings
from execlens.dashboard import charts
from execlens.dashboard.data import (
    current_analysis,
    fmt_bps,
    fmt_ratio,
    load_benchmark_results,
    order_label,
    overview_kpis,
    run_comparison,
    symbol_scenarios,
)
from execlens.dashboard.theme import STRATEGY_ORDER, active_palette, strategy_color
from execlens.data.schemas import Side
from execlens.data.synthetic import SCENARIOS
from execlens.simulation.comparison import ComparisonRequest
from execlens.simulation.impact import ImpactModel
from execlens.simulation.simulator import SimulationConfig
from execlens.tca.aggregation import summarize_by

SYNTHETIC_NOTE = (
    "All market data is synthetic and every execution is simulated; "
    "nothing here describes a real market or real trading."
)
COST_NOTE = "Costs: positive = worse execution. Markouts: positive = favourable move."
BPS = st.column_config.NumberColumn(format="%.2f")
RATIO = st.column_config.NumberColumn(format="percent")


def _page_header(title: str, subtitle: str) -> None:
    st.title(title)
    st.caption(f"{subtitle} {COST_NOTE}")


def _plot(figure: Any, key: str) -> None:
    st.plotly_chart(figure, theme=None, key=key, config={"displaylogo": False})


# --------------------------------------------------------------------------- Overview


def overview() -> None:
    analysis = current_analysis()
    palette = active_palette()
    _page_header("Execution overview", "Portfolio-level transaction cost analysis.")

    kpis = overview_kpis(analysis.summary)
    for row in (kpis[:4], kpis[4:]):
        for column, kpi in zip(st.columns(4), row, strict=True):
            column.metric(kpi.label, kpi.value, help=kpi.help, border=True)

    left, right = st.columns([3, 2], gap="large")
    costs = analysis.orders["arrival_cost_bps"].drop_nulls()
    with left:
        st.subheader("Arrival cost distribution")
        _plot(
            charts.histogram(
                costs.to_list(),
                palette,
                x_title="arrival cost (bps, positive = worse)",
                reference=analysis.summary.arrival_cost_bps.median,
            ),
            "overview-hist",
        )
    by_strategy = summarize_by(analysis.orders, "strategy").sort("median_arrival_cost_bps")
    with right:
        st.subheader("Median arrival cost by strategy")
        _plot(
            charts.ranked_bars(
                [str(s) for s in by_strategy["strategy"].to_list()],
                by_strategy["median_arrival_cost_bps"].to_list(),
                palette,
                x_title="bps (positive = worse)",
            ),
            "overview-strategy",
        )

    st.subheader("By symbol")
    scenarios = symbol_scenarios(analysis)
    by_symbol = summarize_by(analysis.orders, "symbol").with_columns(
        pl.col("symbol").replace_strict(scenarios, default="-").alias("scenario")
    )
    st.dataframe(
        by_symbol.select(
            "symbol",
            "scenario",
            "orders",
            "median_arrival_cost_bps",
            "mean_vwap_cost_bps",
            "mean_effective_spread_bps",
            "mean_completion_ratio",
            "flagged_fraction",
        ),
        hide_index=True,
        column_config={
            "median_arrival_cost_bps": BPS,
            "mean_vwap_cost_bps": BPS,
            "mean_effective_spread_bps": BPS,
            "mean_completion_ratio": RATIO,
            "flagged_fraction": RATIO,
        },
    )
    st.caption(SYNTHETIC_NOTE)


# --------------------------------------------------------------------- Order explorer


def order_explorer() -> None:
    analysis = current_analysis()
    palette = active_palette()
    _page_header("Order explorer", "Market context, fills and benchmarks for one order.")
    orders = analysis.orders

    symbol_col, side_col, strategy_col, flag_col = st.columns(4)
    symbol = symbol_col.selectbox("Symbol", ["All", *sorted(orders["symbol"].unique())])
    side = side_col.segmented_control("Side", ["All", "BUY", "SELL"], default="All")
    strategy = strategy_col.selectbox(
        "Strategy", ["All", *sorted(orders["strategy"].drop_nulls().unique())]
    )
    flagged = flag_col.segmented_control("Flags", ["All", "Flagged", "Clean"], default="All")

    conditions = []
    if symbol != "All":
        conditions.append(pl.col("symbol") == symbol)
    if side not in (None, "All"):
        conditions.append(pl.col("side") == side)
    if strategy != "All":
        conditions.append(pl.col("strategy") == strategy)
    if flagged == "Flagged":
        conditions.append(pl.col("is_flagged"))
    elif flagged == "Clean":
        conditions.append(~pl.col("is_flagged"))
    selected = orders.filter(*conditions) if conditions else orders
    if selected.is_empty():
        st.info("No orders match these filters.")
        return

    labels = {row["order_id"]: order_label(row) for row in selected.iter_rows(named=True)}
    order_id = st.selectbox(
        f"Order ({selected.height} matching)", list(labels), format_func=labels.__getitem__
    )
    order = analysis.order(order_id)
    if order is None:  # pragma: no cover - ids come from the same table
        return
    quotes, _ = analysis.market_window(order_id)
    fills = analysis.fills_for(order_id)

    chart_col, metric_col = st.columns([2, 1], gap="large")
    with chart_col:
        _plot(charts.order_chart(quotes, fills, order, palette), f"order-{order_id}")
        st.caption(
            "Step lines show the prevailing quote; the shaded band is the benchmark interval "
            "[arrival, end]; the flat line is the interval market VWAP."
        )
    with metric_col:
        pairs = [
            ("Arrival cost", fmt_bps(order["arrival_cost_bps"])),
            ("VWAP cost", fmt_bps(order["vwap_cost_bps"])),
            ("Effective spread", fmt_bps(order["effective_spread_bps"])),
            ("60 s fill markout", fmt_bps(order["markout_60s_bps"])),
            ("60 s mid markout", fmt_bps(order["mid_markout_60s_bps"])),
            ("Completion", fmt_ratio(order["completion_ratio"])),
            ("Participation", fmt_ratio(order["participation_rate"])),
            ("Arrival spread", fmt_bps(order["arrival_spread_bps"])),
        ]
        for left, right in zip(pairs[::2], pairs[1::2], strict=True):
            a, b = st.columns(2)
            a.metric(*left, border=True)
            b.metric(*right, border=True)
        st.markdown(
            f"**{order['side']} {order['quantity']:,} {order['symbol']}** · "
            f"{order['strategy'] or 'no strategy label'} · {order['n_fills']} fills · "
            f"avg {order['avg_fill_price'] or 0:,.4f} vs arrival mid "
            f"{order['arrival_mid'] or 0:,.4f} ({order['arrival_quote_status']})"
        )
        for code, reason in zip(order["flags"], order["flag_reasons"], strict=True):
            st.markdown(f":material/flag: **{code}**: {reason}")
        for code, note in zip(order["context_flags"], order["context_notes"], strict=True):
            st.markdown(f":material/info: **{code}**: {note}")
        if not order["flags"] and not order["context_flags"]:
            st.markdown(":material/check_circle: No flags for this order.")

    st.subheader("Fills")
    st.dataframe(
        fills.select(
            "fill_id",
            "timestamp",
            "quantity",
            "price",
            "fill_mid",
            "fill_quote_status",
            "effective_spread_bps",
            "markout_10s_bps",
            "markout_60s_bps",
            "mid_markout_60s_bps",
        ),
        hide_index=True,
        column_config={
            "effective_spread_bps": BPS,
            "markout_10s_bps": BPS,
            "markout_60s_bps": BPS,
            "mid_markout_60s_bps": BPS,
        },
    )
    st.caption(SYNTHETIC_NOTE)


# ----------------------------------------------------------------------- Diagnostics


def diagnostics() -> None:
    analysis = current_analysis()
    palette = active_palette()
    _page_header(
        "Diagnostics",
        "How execution costs co-vary with market conditions. These are associations "
        "between observed quantities, not evidence that a condition caused a cost.",
    )
    orders = analysis.orders
    specs = [
        ("arrival_spread_bps", "quoted spread at arrival (bps)", False),
        ("pre_arrival_volatility_bps", "pre-arrival 10 s volatility (bps)", False),
        ("participation_rate", "participation (share of interval volume, log scale)", True),
    ]
    tabs = st.tabs(["Cost vs spread", "Cost vs volatility", "Cost vs participation"])
    for tab, (column, title, log_x) in zip(tabs, specs, strict=True):
        with tab:
            _plot(
                charts.emphasis_scatter(
                    orders,
                    column,
                    "arrival_cost_bps",
                    palette,
                    emphasis="is_flagged",
                    x_title=title,
                    y_title="arrival cost (bps)",
                    emphasis_label="flagged",
                    rest_label="not flagged",
                    log_x=log_x,
                ),
                f"diag-{column}",
            )

    category_rank = {"execution": 0, "data_quality": 1, "context": 2}
    counts = analysis.flag_counts.sort(
        pl.col("category").replace_strict(category_rank, return_dtype=pl.Int8),
        pl.col("orders"),
        descending=[False, True],
    )
    left, right = st.columns([2, 3], gap="large")
    with left:
        st.subheader("Flags and context notes")
        _plot(
            charts.ranked_bars(
                [f"{r['flag']} ({r['category']})" for r in counts.iter_rows(named=True)],
                counts["orders"].cast(pl.Float64).to_list(),
                palette,
                x_title="orders",
                value_format="{:.0f}",
            ),
            "diag-flag-counts",
        )
    with right:
        st.subheader("Rules")
        st.dataframe(
            counts.select("flag", "category", "description", "orders"),
            hide_index=True,
        )

    st.subheader("Flagged orders")
    flagged = orders.filter(pl.col("is_flagged")).sort("n_flags", descending=True)
    st.dataframe(
        flagged.select(
            "order_id",
            "symbol",
            "strategy",
            "side",
            "arrival_cost_bps",
            "flags",
            pl.col("flag_reasons").list.join(" | ").alias("reasons"),
            pl.col("context_notes").list.join(" | ").alias("market context"),
        ),
        hide_index=True,
        column_config={"arrival_cost_bps": BPS},
    )
    st.caption(SYNTHETIC_NOTE)


# ---------------------------------------------------------------------- Strategy lab


def strategy_lab() -> None:
    analysis = current_analysis()
    palette = active_palette()
    _page_header(
        "Strategy lab",
        "Every strategy trades the same parent order in the same generated sessions; "
        "differences come from the strategies, not the market.",
    )
    scenarios = symbol_scenarios(analysis) or {f"SIM_{s.upper()}": s for s in SCENARIOS}
    with st.form("strategy-lab"):
        c1, c2, c3, c4 = st.columns(4)
        symbol = c1.selectbox(
            "Symbol (scenario)",
            list(scenarios),
            format_func=lambda s: f"{s} ({scenarios[s]})",
        )
        side = c2.segmented_control("Side", ["BUY", "SELL"], default="BUY")
        quantity = c3.number_input("Parent quantity", 100, 2_000_000, 20_000, step=1_000)
        horizon_minutes = c4.slider("Horizon (minutes)", 5, 120, 30, step=5)
        c5, c6, c7 = st.columns([2, 1, 1])
        strategies = c5.multiselect("Strategies", STRATEGY_ORDER, default=list(STRATEGY_ORDER))
        pov_rate = c6.slider("POV participation rate", 0.02, 0.30, 0.10, step=0.01)
        slice_seconds = c7.select_slider("Decision interval (s)", [10, 15, 30, 60, 120], 30)
        c8, c9, c10, c11 = st.columns([1, 1, 1, 1])
        impact = c8.slider("Impact coefficient (bps)", 0, 100, 25, step=5)
        trials = c9.slider("Trials (sessions)", 3, 50, 10)
        seed = c10.number_input("Seed", 0, 1_000_000, 7)
        oracle = c11.checkbox(
            "Add ORACLE_VWAP (hindsight)",
            help="A benchmark that sizes slices with volume realised later in the session. "
            "No real strategy could do this; it is shown only as a reference.",
        )
        submitted = st.form_submit_button("Run comparison", type="primary")

    if not submitted and "lab_request" not in st.session_state:
        st.info("Choose parameters and run a comparison.")
        return
    if submitted:
        if not strategies:
            st.warning("Select at least one strategy.")
            return
        request = ComparisonRequest(
            scenario=scenarios[symbol],
            side=Side(side or "BUY"),
            quantity=int(quantity),
            horizon_seconds=int(horizon_minutes) * 60,
            strategies=tuple(strategies),
            include_oracle=oracle,
            trials=int(trials),
            seed=int(seed),
            simulation=SimulationConfig(
                slice_seconds=float(slice_seconds),
                pov_participation_rate=float(pov_rate),
                impact=ImpactModel(coefficient_bps=float(impact)),
            ),
        )
        st.session_state["lab_request"] = request.model_dump_json()
    summary, orders, conclusions = run_comparison(st.session_state["lab_request"])

    for note in conclusions:
        st.markdown(f":material/insights: {note}")
    left, right = st.columns(2, gap="large")
    with left:
        st.subheader("Mean arrival cost (95% CI)")
        _plot(
            charts.ranked_bars(
                summary["strategy"].to_list(),
                summary["mean_arrival_cost_bps"].to_list(),
                palette,
                x_title="bps (positive = worse)",
                colors=[strategy_color(palette, s) for s in summary["strategy"].to_list()],
                errors=summary["arrival_cost_ci95_bps"].to_list(),
            ),
            "lab-mean-cost",
        )
    with right:
        st.subheader("Arrival cost per trial")
        _plot(
            charts.strategy_distribution(
                orders, palette, metric="arrival_cost_bps", title="arrival cost (bps)"
            ),
            "lab-distribution",
        )
    st.subheader("Results")
    st.dataframe(
        summary.select(
            "strategy",
            "implementable",
            "trials",
            "mean_arrival_cost_bps",
            "arrival_cost_ci95_bps",
            "std_arrival_cost_bps",
            "mean_vwap_cost_bps",
            "mean_effective_spread_bps",
            "mean_markout_60s_bps",
            "mean_completion_ratio",
            "mean_duration_seconds",
            "mean_execution_participation_rate",
            "mean_fills",
        ),
        hide_index=True,
        column_config={
            "mean_arrival_cost_bps": BPS,
            "arrival_cost_ci95_bps": BPS,
            "std_arrival_cost_bps": BPS,
            "mean_vwap_cost_bps": BPS,
            "mean_effective_spread_bps": BPS,
            "mean_markout_60s_bps": BPS,
            "mean_completion_ratio": RATIO,
            "mean_execution_participation_rate": RATIO,
            "mean_duration_seconds": st.column_config.NumberColumn(format="%.0f"),
            "mean_fills": st.column_config.NumberColumn(format="%.1f"),
        },
    )
    st.caption(
        "Simulated fills: touch price plus a square-root temporary-impact model; no queue "
        "position, venues or permanent impact. Results are specific to this synthetic scenario "
        "and parameters. " + SYNTHETIC_NOTE
    )


# ------------------------------------------------------------------------ Benchmarks


def benchmarks() -> None:
    palette = active_palette()
    _page_header(
        "Benchmarks",
        "Measured results from `execlens benchmark` (read from the results file, never typed in).",
    )
    path = Settings.from_env().benchmark_results
    results = load_benchmark_results(path)
    if not results or "tca" not in results:
        st.info(f"No benchmark results at `{path}`. Run `execlens benchmark` to produce them.")
        return
    tca = results["tca"]
    env, data = tca["environment"], tca["dataset"]
    core = tca["workloads"].get("core_in_memory")
    if core:
        a, b, c = st.columns(3)
        a.metric("Measured speedup (core TCA)", f"{core['speedup_median']:.1f}x", border=True)
        b.metric("Polars median", f"{core['optimized']['median_s']:.3f} s", border=True)
        c.metric("Python reference median", f"{core['reference']['median_s']:.3f} s", border=True)
    st.markdown(
        f"Dataset: **{data['quotes']:,} quotes + {data['trades']:,} trades** "
        f"({data['market_observations']:,} market observations), **{data['orders']:,} orders**, "
        f"**{data['fills']:,} fills**. Machine: {env['cpu']}, {env['logical_cpus']} logical "
        f"CPUs, {env['os']}, Python {env['python']}, Polars {env['polars']}. "
        f"Measured {tca['generated_at']}."
    )
    _plot(charts.runtime_bars(tca["workloads"], palette), "bench-runtime")
    rows = [
        {
            "workload": name,
            "reference median (s)": w["reference"]["median_s"],
            "Polars median (s)": w["optimized"]["median_s"],
            "speedup": w["speedup_median"],
            "runs (ref / Polars)": f"{w['reference']['repetitions']} / "
            f"{w['optimized']['repetitions']}",
        }
        for name, w in tca["workloads"].items()
    ]
    st.dataframe(pl.DataFrame(rows), hide_index=True)
    api = results.get("api")
    if api:
        st.subheader("API latency")
        st.dataframe(
            pl.DataFrame(api["endpoints"]).select("endpoint", "p50_ms", "p95_ms", "p99_ms"),
            hide_index=True,
        )
        st.caption(
            f"{api['requests_per_endpoint']} sequential requests per endpoint on localhost "
            f"with {api['orders']:,} orders loaded."
        )
    st.caption(
        "Both implementations are checked for equal outputs before timing; runs are "
        "interleaved and the median is reported. See docs/BENCHMARKS.md for the method."
    )
