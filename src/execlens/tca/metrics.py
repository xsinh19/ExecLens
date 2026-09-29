"""Execution-quality formulas as small, pure Polars expressions.

Every financial formula in ExecLens is defined once, here, and reused by the
TCA engine; the API, CLI and dashboard only ever consume the engine's output.

Sign conventions (see ``docs/METRICS.md``)
------------------------------------------
``side_sign`` is +1 for BUY and -1 for SELL.

* **Cost metrics** (arrival cost, VWAP cost, effective spread):
  positive = worse execution for the order, negative = better.
* **Markouts** use the *opposite* orientation:
  positive = favourable for the order (for a BUY, the later mid is above
  the reference price).

Two markouts are computed with the same formula and different reference
prices: the *fill markout* is measured from the fill price (so it includes
the half-spread and impact paid: a buy at the ask in a flat market has a
negative fill markout) and the *mid markout* is measured from the
contemporaneous mid (so it isolates the subsequent price move, the usual
adverse-selection signal).

All ``*_bps`` values are basis points: 1 bp = 0.01% = 1/10,000.
"""

from __future__ import annotations

import polars as pl

from execlens.data.schemas import BPS


def side_sign(side: pl.Expr) -> pl.Expr:
    """+1 for BUY, -1 for SELL, null for anything else (sides are validated upstream)."""
    return (
        pl.when(side == "BUY")
        .then(pl.lit(1, pl.Int8))
        .when(side == "SELL")
        .then(pl.lit(-1, pl.Int8))
        .otherwise(pl.lit(None, pl.Int8))
    )


def mid_price(bid: pl.Expr, ask: pl.Expr) -> pl.Expr:
    """Quote midpoint: (bid + ask) / 2."""
    return (bid + ask) / 2.0


def spread_bps(bid: pl.Expr, ask: pl.Expr) -> pl.Expr:
    """Quoted spread relative to the midpoint: (ask - bid) / mid * 10,000."""
    return (ask - bid) / mid_price(bid, ask) * BPS


def cost_bps(sign: pl.Expr, price: pl.Expr, benchmark: pl.Expr) -> pl.Expr:
    """Signed execution cost versus a benchmark price.

    ``side_sign * (price - benchmark) / benchmark * 10,000``; positive = worse.
    Used for arrival cost (benchmark = arrival mid) and VWAP cost
    (benchmark = interval market VWAP). Null if either input is null.
    """
    return sign * (price - benchmark) / benchmark * BPS


def effective_spread_bps(sign: pl.Expr, fill_price: pl.Expr, mid: pl.Expr) -> pl.Expr:
    """Effective spread of one fill: ``2 * side_sign * (price - mid) / mid * 10,000``.

    ``mid`` must be the latest valid midpoint at or before the fill time.
    Positive = paid more than the mid (for a buy); twice the half-spread paid.
    Fills inside the quote give less than the quoted spread; only fills on
    the favourable side of the mid give negative values.
    """
    return 2.0 * cost_bps(sign, fill_price, mid)


def markout_bps(sign: pl.Expr, reference_price: pl.Expr, future_mid: pl.Expr) -> pl.Expr:
    """Post-trade markout: ``side_sign * (future_mid - reference) / reference * 10,000``.

    POSITIVE = FAVOURABLE for the order (opposite orientation to the cost
    metrics). With ``reference`` = the fill price this is the spec's fill
    markout, approximately ``-effective_spread / 2 + mid move``; with
    ``reference`` = the mid at the fill it is the mid-to-mid markout, the
    subsequent price move alone. Persistently negative *mid* markouts after
    buys mean prices tended to fall after the order bought (adverse selection).
    """
    return sign * (future_mid - reference_price) / reference_price * BPS


def safe_ratio(numerator: pl.Expr, denominator: pl.Expr) -> pl.Expr:
    """``numerator / denominator`` where the denominator is positive, else null."""
    return pl.when(denominator > 0).then(numerator / denominator).otherwise(None)
