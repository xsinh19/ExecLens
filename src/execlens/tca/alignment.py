"""Point-in-time alignment of events (orders, fills) with market quotes.

The single most important rule in ExecLens: **no look-ahead**. The market
context of an event at time ``t`` is the *latest quote whose timestamp is
<= t* for the same symbol, found with a backward as-of join. We never pick
the *nearest* quote, because the nearest one may lie in the future.

Duplicate quote timestamps: the as-of join takes the last row among equal
timestamps; quotes are stably sorted, so the quote that appears last in the
input wins (it represents the final state at that instant).

Staleness: if the latest prior quote is older than a configurable tolerance,
the context is ``STALE`` and its prices are withheld (null) so dependent
metrics become unavailable instead of silently using old data; with no prior
quote at all the context is ``MISSING``.

Future data is used in exactly one place, by design: post-trade markouts,
which look up the prevailing quote at ``t + h`` (:func:`attach_future_mid`).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from enum import StrEnum

import polars as pl

from execlens.tca.metrics import mid_price, spread_bps


class ContextStatus(StrEnum):
    OK = "OK"
    STALE = "STALE"
    MISSING = "MISSING"


def prepare_quotes(quotes: pl.LazyFrame) -> pl.LazyFrame:
    """Stable-sort quotes by (symbol, timestamp) and derive mid and spreads."""
    return (
        quotes.select(
            "symbol",
            pl.col("timestamp").alias("quote_timestamp"),
            pl.col("bid_price").alias("bid"),
            pl.col("ask_price").alias("ask"),
        )
        .sort("symbol", "quote_timestamp", maintain_order=True)
        .with_columns(
            mid_price(pl.col("bid"), pl.col("ask")).alias("mid"),
            (pl.col("ask") - pl.col("bid")).alias("spread"),
            spread_bps(pl.col("bid"), pl.col("ask")).alias("spread_bps"),
        )
    )


def _age_seconds(later: pl.Expr, earlier: pl.Expr) -> pl.Expr:
    return (later - earlier).dt.total_microseconds() / 1_000_000.0


def attach_quote_context(
    events: pl.LazyFrame,
    quotes: pl.LazyFrame,
    *,
    event_time: str,
    prefix: str,
    tolerance: timedelta,
) -> pl.LazyFrame:
    """Attach the latest quote at or before ``event_time`` (same symbol).

    ``quotes`` must come from :func:`prepare_quotes`. Adds
    ``{prefix}_quote_timestamp``, ``{prefix}_quote_age_seconds``,
    ``{prefix}_quote_status`` and ``{prefix}_{bid,ask,mid,spread,spread_bps}``;
    the price columns are null unless the status is ``OK``.
    """
    tol_s = tolerance.total_seconds()
    joined = events.sort("symbol", event_time).join_asof(
        quotes,
        left_on=event_time,
        right_on="quote_timestamp",
        by="symbol",
        strategy="backward",
        check_sortedness=False,
    )
    age = _age_seconds(pl.col(event_time), pl.col("quote_timestamp"))
    status = (
        pl.when(pl.col("quote_timestamp").is_null())
        .then(pl.lit(ContextStatus.MISSING.value))
        .when(age > tol_s)
        .then(pl.lit(ContextStatus.STALE.value))
        .otherwise(pl.lit(ContextStatus.OK.value))
    )
    usable = status == ContextStatus.OK.value
    fields = ("bid", "ask", "mid", "spread", "spread_bps")
    return joined.with_columns(
        pl.col("quote_timestamp").alias(f"{prefix}_quote_timestamp"),
        age.alias(f"{prefix}_quote_age_seconds"),
        status.alias(f"{prefix}_quote_status"),
        *[pl.when(usable).then(pl.col(f)).otherwise(None).alias(f"{prefix}_{f}") for f in fields],
    ).drop("quote_timestamp", *fields)


def attach_future_mids(
    fills: pl.LazyFrame,
    quotes: pl.LazyFrame,
    *,
    horizons_seconds: tuple[int, ...],
    tolerance: timedelta,
) -> pl.LazyFrame:
    """Attach the prevailing mid at ``fill time + h`` for each horizon (intentional future data).

    Lookup methodology: the prevailing quote at ``t + h`` is the latest quote
    with ``quote_timestamp <= t + h`` (backward as-of join at the target
    time). The observation is available (``OK``) only if

    * the symbol's quote data extends to at least ``t + h`` (some quote is
      stamped at or after the target), otherwise ``MISSING``; and
    * that prevailing quote is no older than the stale tolerance at ``t + h``,
      otherwise ``STALE``.

    Adds ``future_mid_{h}s`` (null unless OK) and ``markout_{h}s_status``.
    ``fills`` must contain ``symbol`` and ``timestamp``; ``quotes`` must come
    from :func:`prepare_quotes`.
    """
    tol_s = tolerance.total_seconds()
    mids = quotes.select("symbol", "quote_timestamp", "mid")
    last_quote = quotes.group_by("symbol").agg(
        pl.col("quote_timestamp").max().alias("_last_quote_timestamp")
    )
    # Sort once: t + h preserves the (symbol, t) order, so every per-horizon
    # as-of join below can reuse it. (Hash joins do not preserve row order,
    # hence the sort comes after the join.)
    plan = fills.join(last_quote, on="symbol", how="left").sort("symbol", "timestamp")
    for h in horizons_seconds:
        target = f"_target_{h}s"
        plan = (
            plan.with_columns((pl.col("timestamp") + pl.duration(seconds=h)).alias(target))
            .join_asof(
                mids,
                left_on=target,
                right_on="quote_timestamp",
                by="symbol",
                strategy="backward",
                check_sortedness=False,
            )
            .with_columns(
                pl.when(
                    pl.col("_last_quote_timestamp").is_null()
                    | (pl.col(target) > pl.col("_last_quote_timestamp"))
                    | pl.col("quote_timestamp").is_null()
                )
                .then(pl.lit(ContextStatus.MISSING.value))
                .when(_age_seconds(pl.col(target), pl.col("quote_timestamp")) > tol_s)
                .then(pl.lit(ContextStatus.STALE.value))
                .otherwise(pl.lit(ContextStatus.OK.value))
                .alias(f"markout_{h}s_status")
            )
            .with_columns(
                pl.when(pl.col(f"markout_{h}s_status") == ContextStatus.OK.value)
                .then(pl.col("mid"))
                .otherwise(None)
                .alias(f"future_mid_{h}s")
            )
            .drop(target, "quote_timestamp", "mid")
        )
    return plan.drop("_last_quote_timestamp")


def attach_interval_volumes(
    frame: pl.LazyFrame, trades: pl.LazyFrame, intervals: Mapping[str, tuple[str, str]]
) -> pl.LazyFrame:
    """Market volume and notional traded in closed intervals ``[start, end]``.

    ``intervals`` maps an output name to a (start column, end column) pair;
    for each, ``{name}_volume`` (Int64) and ``{name}_notional`` (Float64) are
    added.

    Uses per-symbol running totals of size and price*size, built once: the
    amount traded in ``[a, b]`` is ``C(<= b) - C(< a)``, with both endpoints
    located by backward as-of joins (the ``a`` side excludes exact matches).
    This costs O((trades + rows) log n) instead of scanning the tape once per
    order.

    Both outputs are null where ``end`` is null or the symbol has no trade
    data at all (unknown is not the same as zero). An interval with trade data
    but no prints in it has volume 0.
    """
    running = (
        trades.select("symbol", "timestamp", "price", "size")
        .sort("symbol", "timestamp", maintain_order=True)
        .select(
            "symbol",
            pl.col("timestamp").alias("_trade_ts"),
            pl.col("size").cum_sum().over("symbol").alias("_cum_volume"),
            (pl.col("price") * pl.col("size")).cum_sum().over("symbol").alias("_cum_notional"),
        )
    )
    traded_symbols = trades.select("symbol").unique().with_columns(pl.lit(True).alias("_has_tape"))
    plan = frame.join(traded_symbols, on="symbol", how="left")
    for name, (start, end) in intervals.items():
        end_key = pl.coalesce(pl.col(end), pl.col(start)).alias("_end_key")
        plan = (
            plan.with_columns(end_key)
            .sort("symbol", "_end_key")
            .join_asof(
                running,
                left_on="_end_key",
                right_on="_trade_ts",
                by="symbol",
                strategy="backward",
                check_sortedness=False,
            )
            .rename({"_cum_volume": "_end_volume", "_cum_notional": "_end_notional"})
            .drop("_trade_ts")
            .sort("symbol", start)
            .join_asof(
                running,
                left_on=start,
                right_on="_trade_ts",
                by="symbol",
                strategy="backward",
                allow_exact_matches=False,
                check_sortedness=False,
            )
            .rename({"_cum_volume": "_start_volume", "_cum_notional": "_start_notional"})
            .drop("_trade_ts")
        )
        known = pl.col(end).is_not_null() & pl.col("_has_tape").fill_null(False)
        plan = plan.with_columns(
            pl.when(known)
            .then(pl.col("_end_volume").fill_null(0) - pl.col("_start_volume").fill_null(0))
            .otherwise(None)
            .alias(f"{name}_volume"),
            pl.when(known)
            .then(pl.col("_end_notional").fill_null(0.0) - pl.col("_start_notional").fill_null(0.0))
            .otherwise(None)
            .alias(f"{name}_notional"),
        ).drop("_end_key", "_end_volume", "_end_notional", "_start_volume", "_start_notional")
    return plan.drop("_has_tape")
