"""Interpretable market-context features for each order.

Pre-trade features use only data stamped at or before the order's arrival:

* ``pre_arrival_volatility_bps`` - standard deviation of 10-second mid
  log-returns (in bps) over the 5 minutes before arrival. The mid is sampled
  with backward as-of lookups at ``arrival - 300 s, arrival - 290 s, ...,
  arrival``; samples whose quote is stale are dropped. At least 10 valid
  returns are required.
* ``pre_arrival_momentum_bps`` - side-adjusted mid move over the same window,
  ``side_sign * (mid(arrival) - mid(arrival - 300 s)) / mid(arrival - 300 s) * 1e4``.
  **Positive = the price was already moving in the order's direction** (for a
  buy, rising), which is unfavourable for execution.

Post-trade context features describe what happened while the order was
worked. They use data after arrival, which is legitimate for post-trade
diagnosis but they are never inputs to execution decisions:

* ``interval_price_move_bps`` - side-adjusted mid move from arrival to the
  end of the benchmark interval; positive = the market moved against the
  order (for a buy, up).
* ``market_volume_per_minute`` - market volume traded in the benchmark
  interval divided by its length (a liquidity measure independent of the
  order's own size).

All moves are in the cost orientation (positive = adverse for the order) and
computed with :func:`execlens.tca.metrics.cost_bps`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import polars as pl

from execlens.tca.alignment import attach_quote_context, prepare_quotes
from execlens.tca.metrics import cost_bps

FEATURE_COLUMNS = (
    "pre_arrival_volatility_bps",
    "pre_arrival_momentum_bps",
    "interval_price_move_bps",
    "market_volume_per_minute",
)


@dataclass(frozen=True)
class FeatureConfig:
    sample_seconds: int = 10
    lookback_seconds: int = 300
    min_returns: int = 10
    stale_quote_tolerance_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.sample_seconds <= 0 or self.lookback_seconds < 2 * self.sample_seconds:
            raise ValueError("lookback must cover at least two sampling intervals")


def _pre_arrival_features(
    orders: pl.DataFrame, quote_context: pl.DataFrame, config: FeatureConfig
) -> pl.LazyFrame:
    steps = config.lookback_seconds // config.sample_seconds
    samples = (
        orders.lazy()
        .select("order_id", "symbol", "side_sign", "arrival_timestamp")
        .join(pl.LazyFrame({"step": pl.int_range(0, steps + 1, eager=True)}), how="cross")
        .with_columns(
            (
                pl.col("arrival_timestamp")
                - pl.duration(seconds=(steps - pl.col("step")) * config.sample_seconds)
            ).alias("sample_ts")
        )
    )
    sampled = attach_quote_context(
        samples,
        quote_context.lazy(),
        event_time="sample_ts",
        prefix="sample",
        tolerance=timedelta(seconds=config.stale_quote_tolerance_seconds),
    ).sort("order_id", "step")
    log_return_bps = (pl.col("sample_mid").log().diff().over("order_id") * 10_000).alias("ret")
    first_mid = pl.col("sample_mid").filter(pl.col("step") == 0).first()
    last_mid = pl.col("sample_mid").filter(pl.col("step") == steps).first()
    return (
        sampled.with_columns(log_return_bps)
        .group_by("order_id")
        .agg(
            pl.when(pl.col("ret").count() >= config.min_returns)
            .then(pl.col("ret").std())
            .otherwise(None)
            .alias("pre_arrival_volatility_bps"),
            cost_bps(pl.col("side_sign").first(), last_mid, first_mid).alias(
                "pre_arrival_momentum_bps"
            ),
        )
    )


def compute_features(
    quotes: pl.DataFrame | pl.LazyFrame,
    tca_orders: pl.DataFrame,
    config: FeatureConfig | None = None,
) -> pl.DataFrame:
    """Order-level TCA table with the context feature columns appended."""
    config = config or FeatureConfig()
    lazy_quotes = quotes.lazy() if isinstance(quotes, pl.DataFrame) else quotes
    quote_context = prepare_quotes(lazy_quotes).collect()
    pre_arrival = _pre_arrival_features(tca_orders, quote_context, config)

    interval_end = attach_quote_context(
        tca_orders.lazy()
        .select("order_id", "symbol", "benchmark_end")
        .filter(pl.col("benchmark_end").is_not_null()),
        quote_context.lazy(),
        event_time="benchmark_end",
        prefix="end",
        tolerance=timedelta(seconds=config.stale_quote_tolerance_seconds),
    ).select("order_id", "end_mid")

    minutes = (
        pl.col("benchmark_end") - pl.col("benchmark_start")
    ).dt.total_microseconds() / 60_000_000.0
    return (
        tca_orders.lazy()
        .join(pre_arrival, on="order_id", how="left")
        .join(interval_end, on="order_id", how="left")
        .with_columns(
            cost_bps(pl.col("side_sign"), pl.col("end_mid"), pl.col("arrival_mid")).alias(
                "interval_price_move_bps"
            ),
            pl.when(minutes > 0)
            .then(pl.col("market_volume") / minutes)
            .otherwise(None)
            .alias("market_volume_per_minute"),
        )
        .drop("end_mid")
        .sort("arrival_timestamp", "order_id")
        .collect()
    )
