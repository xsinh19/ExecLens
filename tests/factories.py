"""Tiny builders for hand-written test tables.

Times are given as seconds after ``T0`` so hand calculations stay readable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl

from execlens.data.schemas import FILL_SCHEMA, ORDER_SCHEMA, QUOTE_SCHEMA, TRADE_SCHEMA

T0 = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)


def ts(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def quotes_frame(rows: list[tuple[float, str, float, float, int, int]]) -> pl.DataFrame:
    """rows: (seconds, symbol, bid, ask, bid_size, ask_size)."""
    return pl.DataFrame(
        [
            {
                "timestamp": ts(s),
                "symbol": sym,
                "bid_price": bid,
                "ask_price": ask,
                "bid_size": bs,
                "ask_size": asz,
            }
            for s, sym, bid, ask, bs, asz in rows
        ],
        schema=QUOTE_SCHEMA,
    )


def trades_frame(rows: list[tuple[float, str, float, int]]) -> pl.DataFrame:
    """rows: (seconds, symbol, price, size)."""
    return pl.DataFrame(
        [{"timestamp": ts(s), "symbol": sym, "price": p, "size": q} for s, sym, p, q in rows],
        schema=TRADE_SCHEMA,
    )


def orders_frame(
    rows: list[tuple[str, str, str, int, float, float | None, str | None]],
) -> pl.DataFrame:
    """rows: (order_id, symbol, side, quantity, arrival_seconds, horizon_seconds, strategy)."""
    return pl.DataFrame(
        [
            {
                "order_id": oid,
                "symbol": sym,
                "side": side,
                "quantity": qty,
                "arrival_timestamp": ts(arr),
                "execution_horizon_seconds": hor,
                "strategy": strat,
            }
            for oid, sym, side, qty, arr, hor, strat in rows
        ],
        schema=ORDER_SCHEMA,
    )


def fills_frame(rows: list[tuple[str, str, float, int, float]]) -> pl.DataFrame:
    """rows: (fill_id, order_id, seconds, quantity, price)."""
    return pl.DataFrame(
        [
            {"fill_id": fid, "order_id": oid, "timestamp": ts(s), "quantity": q, "price": p}
            for fid, oid, s, q, p in rows
        ],
        schema=FILL_SCHEMA,
    )


def empty(schema: dict[str, pl.DataType]) -> pl.DataFrame:
    return pl.DataFrame(schema=schema)
