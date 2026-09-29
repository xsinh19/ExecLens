"""Vectorised transaction cost analysis (TCA) engine.

``run_tca`` turns four tables (quotes, market trades, parent orders, fills)
into two result tables:

* **fill level** - contemporaneous quote context, effective spread and
  fill / mid markouts for every fill;
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
of affected fills is reported alongside (``n_fills_without_quote``,
``markout_{h}s_unavailable_fills``), so unavailable observations are never
silently averaged away. Mid markouts need both, a fresh quote at the fill and
an observation at ``t + h``.

Benchmark interval: ``[arrival, max(arrival + horizon, last fill)]`` when a
horizon is given (the configured horizon, extended if executions ran past
it), otherwise ``[arrival, last fill]``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import polars as pl

from execlens.config import DEFAULT_STALE_QUOTE_TOLERANCE_SECONDS
from execlens.data.validation import validate_tables
from execlens.tca.alignment import (
    ContextStatus,
    attach_future_mids,
    attach_interval_volumes,
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
        for c in (
            f"future_mid_{h}s",
            f"markout_{h}s_status",
            f"markout_{h}s_bps",
            f"mid_markout_{h}s_bps",
        )
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
        "execution_window_volume",
        "execution_participation_rate",
        "arrival_cost_bps",
        "vwap_cost_bps",
        "effective_spread_bps",
        "avg_contemporaneous_spread_bps",
        "n_fills_without_quote",
        *[f"markout_{h}s_bps" for h in horizons],
        *[f"mid_markout_{h}s_bps" for h in horizons],
        *[f"markout_{h}s_unavailable_fills" for h in horizons],
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
    parents = _with_side_sign(orders).select("order_id", "symbol", "side", "side_sign")
    plan = fills.join(parents, on="order_id", how="inner", maintain_order="left").with_columns(
        (pl.col("price") * pl.col("quantity")).alias("notional")
    )
    plan = attach_quote_context(
        plan, quotes, event_time="timestamp", prefix="fill", tolerance=tol
    ).with_columns(
        effective_spread_bps(pl.col("side_sign"), pl.col("price"), pl.col("fill_mid")).alias(
            "effective_spread_bps"
        )
    )
    horizons = config.markout_horizons_seconds
    plan = attach_future_mids(plan, quotes, horizons_seconds=horizons, tolerance=tol)
    plan = plan.with_columns(
        *[
            markout_bps(pl.col("side_sign"), pl.col("price"), pl.col(f"future_mid_{h}s")).alias(
                f"markout_{h}s_bps"
            )
            for h in horizons
        ],
        *[
            markout_bps(pl.col("side_sign"), pl.col("fill_mid"), pl.col(f"future_mid_{h}s")).alias(
                f"mid_markout_{h}s_bps"
            )
            for h in horizons
        ],
    )
    # Rows come out ordered by (symbol, timestamp): the joins preserve order and
    # the sorts are stable, so the order is deterministic without a costly
    # final sort on string ids.
    return plan.with_columns(pl.col("symbol").cast(pl.String)).select(
        fill_columns(config.markout_horizons_seconds)
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
                (pl.col(f"mid_markout_{h}s_bps") * qty).sum().alias(f"_mid_markout_{h}s_num")
                for h in horizons
            ],
            *[
                pl.col(f"markout_{h}s_bps").null_count().alias(f"markout_{h}s_unavailable_fills")
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
        f"markout_{h}s_unavailable_fills" for h in horizons
    ]
    horizon_us = (pl.col("execution_horizon_seconds") * 1_000_000).round(0).cast(pl.Int64)
    horizon_end = pl.col("arrival_timestamp") + pl.duration(microseconds=horizon_us)
    plan = plan.with_columns(
        *[pl.col(c).fill_null(0) for c in counts],
        pl.col("executed_notional").fill_null(0.0),
        pl.col("arrival_timestamp").alias("benchmark_start"),
        # The configured horizon, extended to the last fill if executions ran past it.
        pl.when(pl.col("execution_horizon_seconds").is_not_null())
        .then(pl.max_horizontal(horizon_end, pl.col("last_fill_timestamp")))
        .otherwise(pl.col("last_fill_timestamp"))
        .alias("benchmark_end"),
    )
    plan = attach_interval_volumes(
        plan,
        trades,
        {
            "market": ("benchmark_start", "benchmark_end"),
            # Volume while the order was actually trading: [arrival, last fill].
            "execution_window": ("benchmark_start", "last_fill_timestamp"),
        },
    )

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
        safe_ratio(executed, pl.col("execution_window_volume")).alias(
            "execution_participation_rate"
        ),
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
            pl.when((pl.col("n_fills") > 0) & (pl.col(f"markout_{h}s_unavailable_fills") == 0))
            .then(pl.col(f"_markout_{h}s_num") / executed)
            .alias(f"markout_{h}s_bps")
            for h in horizons
        ],
        *[
            pl.when(all_fills_have_quote & (pl.col(f"markout_{h}s_unavailable_fills") == 0))
            .then(pl.col(f"_mid_markout_{h}s_num") / executed)
            .alias(f"mid_markout_{h}s_bps")
            for h in horizons
        ],
    )
    return (
        plan.with_columns(pl.col("symbol").cast(pl.String))
        .select(order_columns(horizons))
        .sort("arrival_timestamp", "order_id")
    )


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
    # Symbols become Categorical inside the pipeline: each symbol string is
    # hashed once, and every join, partition and sort below works on integer
    # codes. Outputs are cast back to String.
    categorical = pl.col("symbol").cast(pl.Categorical)
    quotes_lf = _lazy(quotes).with_columns(categorical)
    trades_lf = _lazy(trades).with_columns(categorical)
    orders_lf = _lazy(orders).with_columns(categorical)
    quote_context = prepare_quotes(quotes_lf).collect()
    fill_results = build_fill_plan(quote_context, orders_lf, _lazy(fills), config).collect()
    order_results = build_order_plan(
        quote_context, trades_lf, orders_lf, fill_results, config
    ).collect()
    return TcaResult(orders=order_results, fills=fill_results, config=config)
