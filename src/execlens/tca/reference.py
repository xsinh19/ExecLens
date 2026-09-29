"""Straightforward pure-Python TCA: readable specification and benchmark baseline.

This module recomputes the order-level metrics of :mod:`execlens.tca.engine`
one order at a time with idiomatic Python: dictionaries of per-symbol lists,
``bisect`` for as-of lookups and slice sums for interval VWAP. It serves
three purposes:

1. a readable, loop-by-loop statement of every metric's semantics;
2. an independent oracle - tests and the benchmark assert that the
   vectorised engine agrees with it within a documented tolerance;
3. the baseline of the performance benchmark.

It is intentionally *not* vectorised, but it is not crippled either: lookups
are logarithmic (binary search), not linear scans.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import defaultdict
from typing import Any

import polars as pl

from execlens.tca.engine import TcaConfig

BPS = 10_000.0
MICROS = 1_000_000

# Order-level columns produced by the reference implementation; the engine
# produces a superset and must agree on all of these.
_BASE_COLUMNS = [
    "order_id",
    "arrival_quote_status",
    "arrival_mid",
    "arrival_spread_bps",
    "executed_quantity",
    "completion_ratio",
    "n_fills",
    "avg_fill_price",
    "executed_notional",
    "order_duration_seconds",
    "n_fills_without_quote",
    "market_volume",
    "market_vwap",
    "quantity_to_market_volume",
    "participation_rate",
    "arrival_cost_bps",
    "vwap_cost_bps",
    "effective_spread_bps",
    "avg_contemporaneous_spread_bps",
]


def reference_columns(horizons: tuple[int, ...]) -> list[str]:
    return [
        *_BASE_COLUMNS,
        *[f"markout_{h}s_bps" for h in horizons],
        *[f"mid_markout_{h}s_bps" for h in horizons],
        *[f"markout_{h}s_unavailable_fills" for h in horizons],
    ]


def _epoch_us(frame: pl.DataFrame, column: str) -> list[int]:
    values: list[int] = frame[column].dt.epoch("us").to_list()
    return values


def reference_tca(
    quotes: pl.DataFrame,
    trades: pl.DataFrame,
    orders: pl.DataFrame,
    fills: pl.DataFrame,
    config: TcaConfig | None = None,
) -> pl.DataFrame:
    """Order-level TCA computed with plain Python loops (inputs assumed valid)."""
    config = config or TcaConfig()
    tol = config.stale_quote_tolerance_seconds
    horizons = config.markout_horizons_seconds

    # Per-symbol quote book: stable sort keeps input order among equal
    # timestamps, so bisect_right(...) - 1 picks the last of them.
    quote_rows: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
    for sym, t, bid, ask in zip(
        quotes["symbol"].to_list(),
        _epoch_us(quotes, "timestamp"),
        quotes["bid_price"].to_list(),
        quotes["ask_price"].to_list(),
        strict=True,
    ):
        quote_rows[sym].append((t, bid, ask))
    q_time: dict[str, list[int]] = {}
    q_bid: dict[str, list[float]] = {}
    q_ask: dict[str, list[float]] = {}
    for sym, book in quote_rows.items():
        book.sort(key=lambda row: row[0])
        q_time[sym] = [r[0] for r in book]
        q_bid[sym] = [r[1] for r in book]
        q_ask[sym] = [r[2] for r in book]

    trade_rows: dict[str, list[tuple[int, float, int]]] = defaultdict(list)
    for sym, t, price, size in zip(
        trades["symbol"].to_list(),
        _epoch_us(trades, "timestamp"),
        trades["price"].to_list(),
        trades["size"].to_list(),
        strict=True,
    ):
        trade_rows[sym].append((t, price, size))
    t_time: dict[str, list[int]] = {}
    t_price: dict[str, list[float]] = {}
    t_size: dict[str, list[int]] = {}
    for sym, tape in trade_rows.items():
        tape.sort(key=lambda row: row[0])
        t_time[sym] = [r[0] for r in tape]
        t_price[sym] = [r[1] for r in tape]
        t_size[sym] = [r[2] for r in tape]

    fills_by_order: dict[str, list[tuple[int, int, float]]] = defaultdict(list)
    for oid, t, qty, price in zip(
        fills["order_id"].to_list(),
        _epoch_us(fills, "timestamp"),
        fills["quantity"].to_list(),
        fills["price"].to_list(),
        strict=True,
    ):
        fills_by_order[oid].append((t, qty, price))

    def context(sym: str, t: int) -> tuple[str, float | None, float | None]:
        """Latest quote at or before t: (status, bid, ask); prices only if OK."""
        times = q_time.get(sym)
        if not times:
            return "MISSING", None, None
        i = bisect_right(times, t) - 1
        if i < 0:
            return "MISSING", None, None
        if (t - times[i]) / MICROS > tol:
            return "STALE", None, None
        return "OK", q_bid[sym][i], q_ask[sym][i]

    def future_mid(sym: str, t: int) -> float | None:
        """Prevailing mid at t if the data covers t and the quote is fresh."""
        times = q_time.get(sym)
        if not times or t > times[-1]:
            return None
        i = bisect_right(times, t) - 1
        if i < 0 or (t - times[i]) / MICROS > tol:
            return None
        return (q_bid[sym][i] + q_ask[sym][i]) / 2.0

    records: list[dict[str, Any]] = []
    for oid, sym, side, quantity, arrival, horizon in zip(
        orders["order_id"].to_list(),
        orders["symbol"].to_list(),
        orders["side"].to_list(),
        orders["quantity"].to_list(),
        _epoch_us(orders, "arrival_timestamp"),
        orders["execution_horizon_seconds"].to_list(),
        strict=True,
    ):
        sign = 1 if side == "BUY" else -1
        status, bid, ask = context(sym, arrival)
        arrival_mid = None if bid is None or ask is None else (bid + ask) / 2.0
        arrival_spread = (
            None if bid is None or ask is None else (ask - bid) / ((bid + ask) / 2.0) * BPS
        )

        order_fills = fills_by_order.get(oid, [])
        executed = sum(q for _, q, _ in order_fills)
        notional = sum((p * q for _, q, p in order_fills), 0.0)
        avg_price = notional / executed if executed > 0 else None
        last_fill = max((t for t, _, _ in order_fills), default=None)

        without_quote = 0
        eff_num = 0.0
        spread_num = 0.0
        markout_num = dict.fromkeys(horizons, 0.0)
        mid_markout_num = dict.fromkeys(horizons, 0.0)
        markout_unavailable = dict.fromkeys(horizons, 0)
        for t, q, p in order_fills:
            f_status, f_bid, f_ask = context(sym, t)
            mid: float | None = None
            if f_status != "OK" or f_bid is None or f_ask is None:
                without_quote += 1
            else:
                mid = (f_bid + f_ask) / 2.0
                eff_num += 2.0 * (sign * (p - mid) / mid * BPS) * q
                spread_num += (f_ask - f_bid) / ((f_bid + f_ask) / 2.0) * BPS * q
            for h in horizons:
                fmid = future_mid(sym, t + h * MICROS)
                if fmid is None:
                    markout_unavailable[h] += 1
                    continue
                markout_num[h] += sign * (fmid - p) / p * BPS * q
                if mid is not None:
                    mid_markout_num[h] += sign * (fmid - mid) / mid * BPS * q

        # Interval market VWAP over the closed interval [arrival, end]: the
        # configured horizon, extended to the last fill if executions ran past it.
        end: int | None = last_fill
        if horizon is not None:
            horizon_end = arrival + round(horizon * MICROS)
            end = horizon_end if last_fill is None else max(horizon_end, last_fill)
        volume: int | None = None
        vwap: float | None = None
        if end is not None and sym in t_time:  # no trade data at all: volume unknown
            times = t_time[sym]
            lo = bisect_left(times, arrival)
            hi = bisect_right(times, end)
            sizes = t_size[sym][lo:hi]
            prices = t_price[sym][lo:hi]
            volume = sum(sizes)
            market_notional = sum((p * s for p, s in zip(prices, sizes, strict=True)), 0.0)
            vwap = market_notional / volume if volume > 0 else None

        complete = executed > 0 and without_quote == 0
        record: dict[str, Any] = {
            "order_id": oid,
            "arrival_quote_status": status,
            "arrival_mid": arrival_mid,
            "arrival_spread_bps": arrival_spread,
            "executed_quantity": executed,
            "completion_ratio": executed / quantity,
            "n_fills": len(order_fills),
            "avg_fill_price": avg_price,
            "executed_notional": notional,
            "order_duration_seconds": (
                None if last_fill is None else (last_fill - arrival) / MICROS
            ),
            "n_fills_without_quote": without_quote,
            "market_volume": volume,
            "market_vwap": vwap,
            "quantity_to_market_volume": quantity / volume if volume else None,
            "participation_rate": executed / volume if volume else None,
            "arrival_cost_bps": (
                None
                if avg_price is None or arrival_mid is None
                else sign * (avg_price - arrival_mid) / arrival_mid * BPS
            ),
            "vwap_cost_bps": (
                None
                if avg_price is None or vwap is None
                else sign * (avg_price - vwap) / vwap * BPS
            ),
            "effective_spread_bps": eff_num / executed if complete else None,
            "avg_contemporaneous_spread_bps": spread_num / executed if complete else None,
        }
        for h in horizons:
            ok = executed > 0 and markout_unavailable[h] == 0
            record[f"markout_{h}s_bps"] = markout_num[h] / executed if ok else None
            record[f"mid_markout_{h}s_bps"] = (
                mid_markout_num[h] / executed if ok and complete else None
            )
            record[f"markout_{h}s_unavailable_fills"] = markout_unavailable[h]
        records.append(record)

    integer_columns = {
        "executed_quantity",
        "n_fills",
        "n_fills_without_quote",
        "market_volume",
        *(f"markout_{h}s_unavailable_fills" for h in horizons),
    }
    schema: dict[str, pl.DataType] = {}
    for column in reference_columns(horizons):
        if column in {"order_id", "arrival_quote_status"}:
            schema[column] = pl.String()
        elif column in integer_columns:
            schema[column] = pl.Int64()
        else:
            schema[column] = pl.Float64()
    return pl.DataFrame(records, schema=schema)


def compare_to_reference(
    engine_orders: pl.DataFrame,
    reference: pl.DataFrame,
    *,
    rtol: float = 1e-9,
    atol: float = 1e-6,
) -> list[str]:
    """Describe every disagreement between engine and reference results.

    Integer/string columns must match exactly (including nulls). Float columns
    must have identical null patterns and satisfy
    ``|engine - reference| <= atol + rtol * |reference|``. The tolerance only
    absorbs floating-point summation-order differences. Returns an empty list
    when the two are equivalent.
    """
    columns = list(reference.columns)
    a = engine_orders.select(columns).sort("order_id")
    b = reference.select(columns).sort("order_id")
    if a.height != b.height or not a["order_id"].equals(b["order_id"]):
        return [f"order sets differ: engine {a.height} rows vs reference {b.height} rows"]
    problems: list[str] = []
    for column in columns:
        left, right = a[column], b[column]
        if left.dtype.is_float() or right.dtype.is_float():
            lv, rv = left.cast(pl.Float64).to_list(), right.cast(pl.Float64).to_list()
            nulls = sum((x is None) != (y is None) for x, y in zip(lv, rv, strict=True))
            if nulls:
                problems.append(f"{column}: null pattern differs in {nulls} rows")
                continue
            worst = 0.0
            bad = 0
            for x, y in zip(lv, rv, strict=True):
                if x is None or y is None:
                    continue
                diff = abs(x - y)
                if not math.isfinite(diff) or diff > atol + rtol * abs(y):
                    bad += 1
                worst = max(worst, diff)
            if bad:
                problems.append(f"{column}: {bad} rows outside tolerance (max |diff| {worst:.3g})")
        elif not left.equals(right):
            problems.append(f"{column}: values differ")
    return problems
