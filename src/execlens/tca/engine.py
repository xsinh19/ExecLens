"""Vectorised transaction cost analysis (TCA) engine.

``run_tca`` turns four tables (quotes, market trades, parent orders, fills)
into two result tables:

* **fill level** - contemporaneous quote context, effective spread and
  markouts for every fill;
* **order level** - arrival context, average fill price, completion,
  duration, interval market VWAP and volume, arrival cost, VWAP cost,
  quantity-weighted effective spread / quoted spread / markouts, and
  participation.

The pipeline is expressed with Polars lazy frames: as-of joins for
point-in-time market context (see :mod:`execlens.tca.alignment`), grouped
aggregation for fill -> order roll-ups and running totals for interval VWAP.
Formulas come from :mod:`execlens.tca.metrics` only.

Aggregation rule for fill-weighted order metrics (effective spread, average
quoted spread, markouts): the order value is reported only when *every* fill
has valid market context for that metric; otherwise it is null and the number
of affected fills is reported alongside, so missing observations are never
silently averaged away.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import polars as pl

from execlens.config import DEFAULT_STALE_QUOTE_TOLERANCE_SECONDS
from execlens.data.validation import validate_tables
from execlens.tca.alignment import (
    ContextStatus,
    attach_future_mid,
    attach_interval_volume,
    attach_quote_context,
    prepare_quotes,
)
from execlens.tca.metrics import (
    cost_bps,
    effective_spread_bps,
    markout_bps,
    safe_ratio,
    side_sign,
)

DEFAULT_MARKOUT_HORIZONS: tuple[int, ...] = (10, 30, 60, 300)

Frame = pl.DataFrame | pl.LazyFrame


@dataclass(frozen=True)
class TcaConfig:
    """Analysis parameters.

    ``stale_quote_tolerance_seconds``: a quote older than this at the time it
    is needed is treated as stale (metrics depending on it become null).
    ``markout_horizons_seconds``: post-trade markout horizons.
    """

    stale_quote_tolerance_seconds: float = DEFAULT_STALE_QUOTE_TOLERANCE_SECONDS
    markout_horizons_seconds: tuple[int, ...] = DEFAULT_MARKOUT_HORIZONS

    def __post_init__(self) -> None:
        if not self.stale_quote_tolerance_seconds > 0:
            raise ValueError("stale_quote_tolerance_seconds must be positive")
        horizons = self.markout_horizons_seconds
        if not horizons or any(h <= 0 for h in horizons) or len(set(horizons)) != len(horizons):
            raise ValueError("markout_horizons_seconds must be unique positive integers")

    @property
    def tolerance(self) -> timedelta:
        return timedelta(seconds=self.stale_quote_tolerance_seconds)


@dataclass(frozen=True)
class TcaResult:
    orders: pl.DataFrame
    fills: pl.DataFrame
    config: TcaConfig


def fill_columns(horizons: tuple[int, ...]) -> list[str]:
    markouts = [
        c
        for h in horizons
        for c in (f"future_mid_{h}s", f"markout_{h}s_status", f"markout_{h}s_bps")
    ]
    return [
        "fill_id",
        "order_id",
        "symbol",
        "side",
        "side_sign",
        "timestamp",
        "quantity",
        "price",
        "notional",
        "fill_quote_timestamp",
        "fill_quote_age_seconds",
        "fill_quote_status",
        "fill_bid",
        "fill_ask",
        "fill_mid",
        "fill_spread_bps",
        "effective_spread_bps",
        *markouts,
    ]


def order_columns(horizons: tuple[int, ...]) -> list[str]:
    return [
        "order_id",
        "symbol",
        "side",
        "side_sign",
        "strategy",
        "quantity",
        "arrival_timestamp",
        "execution_horizon_seconds",
        "arrival_quote_timestamp",
        "arrival_quote_age_seconds",
        "arrival_quote_status",
        "arrival_bid",
        "arrival_ask",
        "arrival_mid",
        "arrival_spread",
        "arrival_spread_bps",
        "executed_quantity",
        "completion_ratio",
        "n_fills",
        "avg_fill_price",
        "executed_notional",
        "first_fill_timestamp",
        "last_fill_timestamp",
        "order_duration_seconds",
        "benchmark_start",
        "benchmark_end",
        "market_volume",
        "market_vwap",
        "quantity_to_market_volume",
        "participation_rate",
        "arrival_cost_bps",
        "vwap_cost_bps",
        "effective_spread_bps",
        "avg_contemporaneous_spread_bps",
        "n_fills_without_quote",
        *[f"markout_{h}s_bps" for h in horizons],
        *[f"markout_{h}s_missing_fills" for h in horizons],
    ]


def _lazy(frame: Frame) -> pl.LazyFrame:
    return frame.lazy() if isinstance(frame, pl.DataFrame) else frame


def _with_side_sign(orders: pl.LazyFrame) -> pl.LazyFrame:
    return orders.with_columns(side_sign(pl.col("side")).alias("side_sign"))


def build_fill_plan(
    quote_context: pl.DataFrame, orders: pl.LazyFrame, fills: pl.LazyFrame, config: TcaConfig
) -> pl.LazyFrame:
    """Fill-level context, effective spread and markouts (lazy plan)."""
    tol = config.tolerance
    quotes = quote_context.lazy()
    last_quote = quotes.group_by("symbol").agg(
        pl.col("quote_timestamp").max().alias("last_quote_timestamp")
    )
    parents = _with_side_sign(orders).select("order_id", "symbol", "side", "side_sign")
    plan = fills.join(parents, on="order_id", how="inner").with_columns(
        (pl.col("price") * pl.col("quantity")).alias("notional")
    )
    plan = attach_quote_context(
        plan, quotes, event_time="timestamp", prefix="fill", tolerance=tol
    ).with_columns(
        effective_spread_bps(pl.col("side_sign"), pl.col("price"), pl.col("fill_mid")).alias(
            "effective_spread_bps"
        )
    )
    for h in config.markout_horizons_seconds:
        plan = attach_future_mid(
            plan, quotes, last_quote, horizon_seconds=h, tolerance=tol
        ).with_columns(
            markout_bps(pl.col("side_sign"), pl.col("price"), pl.col(f"future_mid_{h}s")).alias(
                f"markout_{h}s_bps"
            )
        )
    return plan.select(fill_columns(config.markout_horizons_seconds)).sort(
        "order_id", "timestamp", "fill_id"
    )


def build_order_plan(
    quote_context: pl.DataFrame,
    trades: pl.LazyFrame,
    orders: pl.LazyFrame,
    fill_results: pl.DataFrame,
    config: TcaConfig,
) -> pl.LazyFrame:
    """Order-level roll-up and benchmark costs (lazy plan)."""
    horizons = config.markout_horizons_seconds
    qty = pl.col("quantity")
    per_order = (
        fill_results.lazy()
        .group_by("order_id")
        .agg(
            qty.sum().alias("executed_quantity"),
            pl.col("notional").sum().alias("executed_notional"),
            pl.len().alias("n_fills"),
            pl.col("timestamp").min().alias("first_fill_timestamp"),
            pl.col("timestamp").max().alias("last_fill_timestamp"),
            (pl.col("fill_quote_status") != ContextStatus.OK.value)
            .sum()
            .alias("n_fills_without_quote"),
            (pl.col("effective_spread_bps") * qty).sum().alias("_effective_spread_num"),
            (pl.col("fill_spread_bps") * qty).sum().alias("_quoted_spread_num"),
            *[
                (pl.col(f"markout_{h}s_bps") * qty).sum().alias(f"_markout_{h}s_num")
                for h in horizons
            ],
            *[
                pl.col(f"markout_{h}s_bps").null_count().alias(f"markout_{h}s_missing_fills")
                for h in horizons
            ],
        )
    )

    plan = attach_quote_context(
        _with_side_sign(orders),
        quote_context.lazy(),
        event_time="arrival_timestamp",
        prefix="arrival",
        tolerance=config.tolerance,
    ).join(per_order, on="order_id", how="left")

    counts = ["executed_quantity", "n_fills", "n_fills_without_quote"] + [
        f"markout_{h}s_missing_fills" for h in horizons
    ]
    horizon_us = (pl.col("execution_horizon_seconds") * 1_000_000).round(0).cast(pl.Int64)
    plan = plan.with_columns(
        *[pl.col(c).fill_null(0) for c in counts],
        pl.col("executed_notional").fill_null(0.0),
        pl.col("arrival_timestamp").alias("benchmark_start"),
        pl.when(pl.col("execution_horizon_seconds").is_not_null())
        .then(pl.col("arrival_timestamp") + pl.duration(microseconds=horizon_us))
        .otherwise(pl.col("last_fill_timestamp"))
        .alias("benchmark_end"),
    )
    plan = attach_interval_volume(plan, trades, start="benchmark_start", end="benchmark_end")

    executed = pl.col("executed_quantity")
    all_fills_have_quote = (pl.col("n_fills") > 0) & (pl.col("n_fills_without_quote") == 0)
    plan = plan.with_columns(
        safe_ratio(pl.col("executed_notional"), executed).alias("avg_fill_price"),
        safe_ratio(pl.col("market_notional"), pl.col("market_volume")).alias("market_vwap"),
    ).with_columns(
        (executed / pl.col("quantity")).alias("completion_ratio"),
        (
            (pl.col("last_fill_timestamp") - pl.col("arrival_timestamp")).dt.total_microseconds()
            / 1_000_000.0
        ).alias("order_duration_seconds"),
        safe_ratio(pl.col("quantity"), pl.col("market_volume")).alias("quantity_to_market_volume"),
        safe_ratio(executed, pl.col("market_volume")).alias("participation_rate"),
        cost_bps(pl.col("side_sign"), pl.col("avg_fill_price"), pl.col("arrival_mid")).alias(
            "arrival_cost_bps"
        ),
        cost_bps(pl.col("side_sign"), pl.col("avg_fill_price"), pl.col("market_vwap")).alias(
            "vwap_cost_bps"
        ),
        pl.when(all_fills_have_quote)
        .then(pl.col("_effective_spread_num") / executed)
        .alias("effective_spread_bps"),
        pl.when(all_fills_have_quote)
        .then(pl.col("_quoted_spread_num") / executed)
        .alias("avg_contemporaneous_spread_bps"),
        *[
            pl.when((pl.col("n_fills") > 0) & (pl.col(f"markout_{h}s_missing_fills") == 0))
            .then(pl.col(f"_markout_{h}s_num") / executed)
            .alias(f"markout_{h}s_bps")
            for h in horizons
        ],
    )
    return plan.select(order_columns(horizons)).sort("arrival_timestamp", "order_id")


def run_tca(
    quotes: Frame,
    trades: Frame,
    orders: Frame,
    fills: Frame,
    config: TcaConfig | None = None,
    *,
    validate: bool = True,
) -> TcaResult:
    """Run the full TCA pipeline.

    With ``validate=True`` (default) inputs are checked against the data model
    first; pass ``validate=False`` only for inputs that were already validated.
    """
    config = config or TcaConfig()
    if validate:
        q, t, o, f = validate_tables(
            *(
                frame.collect() if isinstance(frame, pl.LazyFrame) else frame
                for frame in (quotes, trades, orders, fills)
            )
        )
        quotes, trades, orders, fills = q, t, o, f
    quote_context = prepare_quotes(_lazy(quotes)).collect()
    fill_results = build_fill_plan(quote_context, _lazy(orders), _lazy(fills), config).collect()
    order_results = build_order_plan(
        quote_context, _lazy(trades), _lazy(orders), fill_results, config
    ).collect()
    return TcaResult(orders=order_results, fills=fill_results, config=config)
