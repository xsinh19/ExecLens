"""Plotly figure builders for the dashboard.

Pure functions: frames in, figures out. They draw values computed by the
analysis and simulation services and never compute a metric themselves.
Mark specs: 2 px lines, >= 8 px markers with a surface-coloured ring, thin
bars with rounded ends and direct value labels, hairline solid grid.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import plotly.graph_objects as go
import polars as pl

from execlens.dashboard.theme import STRATEGY_ORDER, Palette, layout, strategy_color


def histogram(
    values: Sequence[float],
    palette: Palette,
    *,
    x_title: str,
    reference: float | None = None,
    reference_label: str = "median",
) -> go.Figure:
    """Distribution of one metric (single series, so no legend)."""
    fig = go.Figure(
        go.Histogram(
            x=list(values),
            nbinsx=40,
            marker={"color": palette.series[0], "line": {"color": palette.surface, "width": 1}},
            hovertemplate="%{x}<br>%{y} orders<extra></extra>",
        )
    )
    fig.update_layout(**layout(palette, x_title=x_title, y_title="orders", legend=False))
    if reference is not None:
        fig.add_vline(x=reference, line={"color": palette.ink_secondary, "width": 1})
        fig.add_annotation(
            x=reference,
            y=1,
            yref="paper",
            text=f"{reference_label} {reference:.2f}",
            showarrow=False,
            xanchor="left",
            yanchor="top",
            font={"color": palette.ink_secondary, "size": 11},
        )
    return fig


def ranked_bars(
    labels: Sequence[str],
    values: Sequence[float | None],
    palette: Palette,
    *,
    x_title: str,
    colors: Sequence[str] | None = None,
    value_format: str = "{:.2f}",
    errors: Sequence[float | None] | None = None,
) -> go.Figure:
    """Horizontal bars (one colour unless ``colors`` is given).

    Values are labelled at the bar tips; with ``errors`` the whiskers would
    collide with tip labels, so values move to the hover and the page's table.
    """
    bar_colors = list(colors) if colors is not None else [palette.series[0]] * len(labels)
    error_x: dict[str, Any] | None = None
    text: list[str] | None = ["" if v is None else value_format.format(v) for v in values]
    if errors is not None:
        text = None
        error_x = {
            "type": "data",
            "array": [0.0 if e is None else e for e in errors],
            "color": palette.ink_secondary,
            "thickness": 1,
            "width": 4,
        }
    fig = go.Figure(
        go.Bar(
            x=[0.0 if v is None else v for v in values],
            y=list(labels),
            orientation="h",
            marker={"color": bar_colors, "cornerradius": 4},
            text=text,
            textposition="outside",
            textfont={"color": palette.ink_secondary},
            cliponaxis=False,
            error_x=error_x,
            hovertemplate="%{y}: %{x:.2f}<extra></extra>",
        )
    )
    fig.update_layout(
        **layout(palette, height=max(160, 56 + 36 * len(labels)), x_title=x_title, legend=False),
        bargap=0.45,
    )
    fig.update_yaxes(autorange="reversed", showgrid=False)
    return fig


def order_chart(
    quotes: pl.DataFrame,
    fills: pl.DataFrame,
    order: Mapping[str, Any],
    palette: Palette,
) -> go.Figure:
    """Bid/ask band, mid, fills, arrival time, benchmark interval and interval VWAP."""
    fig = go.Figure()
    ts = quotes["timestamp"].to_list()
    band = palette.deemphasis
    fig.add_trace(
        go.Scatter(
            x=ts,
            y=quotes["ask_price"].to_list(),
            name="Ask",
            mode="lines",
            line={"color": band, "width": 1, "shape": "hv"},
            legendgroup="quote",
            showlegend=False,
            hovertemplate="ask %{y:.4f}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=ts,
            y=quotes["bid_price"].to_list(),
            name="Bid / ask",
            mode="lines",
            line={"color": band, "width": 1, "shape": "hv"},
            fill="tonexty",
            fillcolor=palette.wash,
            legendgroup="quote",
            hovertemplate="bid %{y:.4f}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=ts,
            y=quotes["mid"].to_list(),
            name="Mid",
            mode="lines",
            line={"color": palette.series[0], "width": 2, "shape": "hv"},
            hovertemplate="mid %{y:.4f}<extra></extra>",
        )
    )
    if fills.height:
        fig.add_trace(
            go.Scatter(
                x=fills["timestamp"].to_list(),
                y=fills["price"].to_list(),
                name="Fills",
                mode="markers",
                marker={
                    "color": palette.series[1],
                    "size": 10,
                    "line": {"color": palette.surface, "width": 2},
                },
                customdata=fills["quantity"].to_list(),
                hovertemplate="fill %{y:.4f} x %{customdata:,}<extra></extra>",
            )
        )
    start, end = order["benchmark_start"], order["benchmark_end"]
    if end is not None:
        fig.add_vrect(
            x0=start,
            x1=end,
            fillcolor=palette.grid,
            opacity=0.35,
            line_width=0,
            layer="below",
        )
        if order["market_vwap"] is not None:
            fig.add_trace(
                go.Scatter(
                    x=[start, end],
                    y=[order["market_vwap"], order["market_vwap"]],
                    name="Interval VWAP",
                    mode="lines",
                    line={"color": palette.series[2], "width": 2},
                    hovertemplate="VWAP %{y:.4f}<extra></extra>",
                )
            )
            fig.add_annotation(  # direct label at the line end, inside the interval
                x=end,
                y=order["market_vwap"],
                text=f"VWAP {order['market_vwap']:.4f}",
                showarrow=False,
                xanchor="right",
                yanchor="bottom",
                font={"color": palette.ink_secondary, "size": 11},
            )
    fig.add_vline(x=order["arrival_timestamp"], line={"color": palette.ink, "width": 1})
    fig.add_annotation(
        x=order["arrival_timestamp"],
        y=1,
        yref="paper",
        text="arrival",
        showarrow=False,
        xanchor="left",
        yanchor="top",
        xshift=4,
        font={"color": palette.ink_secondary, "size": 11},
    )
    fig.update_layout(**layout(palette, height=420, y_title="price"), hovermode="x unified")
    return fig


def emphasis_scatter(
    frame: pl.DataFrame,
    x: str,
    y: str,
    palette: Palette,
    *,
    emphasis: str,
    x_title: str,
    y_title: str,
    emphasis_label: str,
    rest_label: str,
    log_x: bool = False,
) -> go.Figure:
    """Scatter with one highlighted group (accent) over de-emphasised context (gray)."""
    data = frame.filter(pl.col(x).is_not_null() & pl.col(y).is_not_null())
    if log_x:
        data = data.filter(pl.col(x) > 0)
    fig = go.Figure()
    for flag, name, color in (
        (False, rest_label, palette.deemphasis),
        (True, emphasis_label, palette.series[0]),
    ):
        part = data.filter(pl.col(emphasis) == flag)
        fig.add_trace(
            go.Scatter(
                x=part[x].to_list(),
                y=part[y].to_list(),
                mode="markers",
                name=f"{name} ({part.height})",
                marker={"color": color, "size": 8, "line": {"color": palette.surface, "width": 2}},
                customdata=part.select("order_id", "symbol", "strategy").rows(),
                hovertemplate=(
                    "%{customdata[0]}<br>%{customdata[1]} · %{customdata[2]}"
                    f"<br>{x_title}: %{{x:.2f}}<br>{y_title}: %{{y:.2f}}<extra></extra>"
                ),
            )
        )
    fig.update_layout(**layout(palette, height=360, x_title=x_title, y_title=y_title))
    if log_x:
        fig.update_xaxes(type="log")
    return fig


def strategy_distribution(
    orders: pl.DataFrame, palette: Palette, *, metric: str, title: str
) -> go.Figure:
    """Per-trial outcomes by strategy: the spread is the timing risk."""
    fig = go.Figure()
    present = [s for s in (*STRATEGY_ORDER, "ORACLE_VWAP") if s in set(orders["strategy"])]
    for strategy in present:
        values = orders.filter(pl.col("strategy") == strategy)[metric].drop_nulls().to_list()
        color = strategy_color(palette, strategy)
        fig.add_trace(
            go.Box(
                y=values,
                name=strategy if strategy != "ORACLE_VWAP" else "ORACLE_VWAP (hindsight)",
                marker={"color": color, "size": 8, "line": {"color": palette.surface, "width": 2}},
                line={"color": color, "width": 2},
                fillcolor=palette.wash,
                boxpoints="all",
                jitter=0.35,
                pointpos=0,
                hovertemplate="%{y:.2f} bps<extra></extra>",
            )
        )
    fig.update_layout(**layout(palette, height=380, y_title=title))
    return fig


def runtime_bars(workloads: Mapping[str, Mapping[str, Any]], palette: Palette) -> go.Figure:
    """Median runtime per workload: reference vs Polars (two series, legend + labels)."""
    names = list(workloads)
    fig = go.Figure()
    for key, label, color in (
        ("reference", "Python reference", palette.series[1]),
        ("optimized", "Polars engine", palette.series[0]),
    ):
        medians = [workloads[n][key]["median_s"] for n in names]
        fig.add_trace(
            go.Bar(
                y=names,
                x=medians,
                name=label,
                orientation="h",
                marker={"color": color, "cornerradius": 4},
                text=[f"{m:.2f} s" for m in medians],
                textposition="outside",
                textfont={"color": palette.ink_secondary},
                cliponaxis=False,
                hovertemplate=f"{label}: %{{x:.3f}} s<extra></extra>",
            )
        )
    fig.update_layout(
        **layout(palette, height=120 + 70 * len(names), x_title="median wall-clock time (s)"),
        barmode="group",
        bargap=0.35,
        bargroupgap=0.15,
    )
    fig.update_yaxes(autorange="reversed", showgrid=False)
    return fig
