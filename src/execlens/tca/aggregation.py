"""Portfolio-level summaries of order-level TCA results.

Averages across orders are reported two ways where it matters:

* simple statistics over orders (median / mean / p5 / p95) describe the
  *typical order*;
* the notional-weighted mean arrival cost describes the cost of the
  *whole traded book* (big orders count more), which is what a desk pays.

Percentiles use linear interpolation between order statistics (the NumPy
default). For cost metrics the adverse tail is p95; for markouts and
completion (higher = better) it is p5. :class:`MetricStats` ignores orders
where the metric is unavailable and reports how many orders contributed
(``available``); :func:`summarize_by` reports group means over the orders
where each metric is available.
"""

from __future__ import annotations

import polars as pl
from pydantic import BaseModel, ConfigDict


class MetricStats(BaseModel):
    """Distribution of one metric across orders (null = no data)."""

    model_config = ConfigDict(frozen=True)

    available: int
    mean: float | None
    median: float | None
    p5: float | None
    p95: float | None


class TcaSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    analyzed_orders: int
    orders_with_fills: int
    total_fills: int
    executed_quantity: int
    executed_notional: float
    notional_weighted_arrival_cost_bps: float | None
    full_fill_rate: float | None
    completion_ratio: MetricStats
    arrival_cost_bps: MetricStats
    vwap_cost_bps: MetricStats
    effective_spread_bps: MetricStats
    participation_rate: MetricStats
    markout_bps: dict[str, MetricStats]
    mid_markout_bps: dict[str, MetricStats]
    flagged_orders: int | None = None
    flagged_fraction: float | None = None


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    raise TypeError(f"expected a numeric statistic, got {type(value).__name__}")


def metric_stats(orders: pl.DataFrame, column: str) -> MetricStats:
    values = orders[column].drop_nulls()
    if values.is_empty():
        return MetricStats(available=0, mean=None, median=None, p5=None, p95=None)
    return MetricStats(
        available=values.len(),
        mean=_float_or_none(values.mean()),
        median=_float_or_none(values.median()),
        p5=_float_or_none(values.quantile(0.05, interpolation="linear")),
        p95=_float_or_none(values.quantile(0.95, interpolation="linear")),
    )


def markout_horizons(orders: pl.DataFrame) -> list[int]:
    """Markout horizons present in an order-level result, in ascending order."""
    horizons = [
        int(c.removeprefix("markout_").removesuffix("s_bps"))
        for c in orders.columns
        if c.startswith("markout_") and c.endswith("s_bps")
    ]
    return sorted(horizons)


def summarize_orders(orders: pl.DataFrame) -> TcaSummary:
    """Summarise an order-level TCA table (optionally with an ``is_flagged`` column)."""
    with_cost = orders.filter(pl.col("arrival_cost_bps").is_not_null())
    total_weight = _float_or_none(with_cost["executed_notional"].sum()) or 0.0
    weighted_cost = (
        _float_or_none((with_cost["arrival_cost_bps"] * with_cost["executed_notional"]).sum())
        if total_weight > 0
        else None
    )
    if weighted_cost is not None:
        weighted_cost /= total_weight
    flagged: int | None = None
    flagged_fraction: float | None = None
    if "is_flagged" in orders.columns:
        flagged = int(orders["is_flagged"].sum())
        flagged_fraction = flagged / orders.height if orders.height else None
    horizons = markout_horizons(orders)
    return TcaSummary(
        analyzed_orders=orders.height,
        orders_with_fills=int((orders["n_fills"] > 0).sum()),
        total_fills=int(orders["n_fills"].sum()),
        executed_quantity=int(orders["executed_quantity"].sum()),
        executed_notional=float(orders["executed_notional"].sum()),
        notional_weighted_arrival_cost_bps=weighted_cost,
        full_fill_rate=_float_or_none((orders["completion_ratio"] >= 1.0).mean()),
        completion_ratio=metric_stats(orders, "completion_ratio"),
        arrival_cost_bps=metric_stats(orders, "arrival_cost_bps"),
        vwap_cost_bps=metric_stats(orders, "vwap_cost_bps"),
        effective_spread_bps=metric_stats(orders, "effective_spread_bps"),
        participation_rate=metric_stats(orders, "participation_rate"),
        markout_bps={f"{h}s": metric_stats(orders, f"markout_{h}s_bps") for h in horizons},
        mid_markout_bps={
            f"{h}s": metric_stats(orders, f"mid_markout_{h}s_bps")
            for h in horizons
            if f"mid_markout_{h}s_bps" in orders.columns
        },
        flagged_orders=flagged,
        flagged_fraction=flagged_fraction,
    )


def summarize_by(orders: pl.DataFrame, by: str | list[str]) -> pl.DataFrame:
    """Per-group statistics (e.g. by ``symbol``, ``strategy`` or ``side``)."""
    aggregations = [
        pl.len().alias("orders"),
        pl.col("arrival_cost_bps").count().alias("orders_with_arrival_cost"),
        pl.col("executed_notional").sum().alias("executed_notional"),
        pl.col("completion_ratio").mean().alias("mean_completion_ratio"),
        pl.col("arrival_cost_bps").median().alias("median_arrival_cost_bps"),
        pl.col("arrival_cost_bps").mean().alias("mean_arrival_cost_bps"),
        pl.col("vwap_cost_bps").mean().alias("mean_vwap_cost_bps"),
        pl.col("effective_spread_bps").mean().alias("mean_effective_spread_bps"),
        pl.col("participation_rate").mean().alias("mean_participation_rate"),
    ]
    aggregations += [
        pl.col(f"markout_{h}s_bps").mean().alias(f"mean_markout_{h}s_bps")
        for h in markout_horizons(orders)
    ]
    if "is_flagged" in orders.columns:
        aggregations.append(pl.col("is_flagged").mean().alias("flagged_fraction"))
    keys = [by] if isinstance(by, str) else by
    return orders.group_by(keys).agg(aggregations).sort(keys)
