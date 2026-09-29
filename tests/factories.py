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


def hand_example() -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """The worked example of tests/unit/test_tca_formulas.py (quotes, trades, orders, fills).

    One market shared by a BUY and a SELL order. Mids: t0 100.00, t10 100.02,
    t20 100.08, t40 100.12, t70 100.18, t80 100.20, t300 100.30, t330 100.32;
    every spread is 0.04. Both orders arrive at t5 with a 60 s horizon, so the
    benchmark interval is [5, 65].
    """
    quotes = quotes_frame(
        [
            (0, "SYN", 99.98, 100.02, 500, 500),
            (10, "SYN", 100.00, 100.04, 500, 500),
            (20, "SYN", 100.06, 100.10, 500, 500),
            (40, "SYN", 100.10, 100.14, 500, 500),
            (70, "SYN", 100.16, 100.20, 500, 500),
            (80, "SYN", 100.18, 100.22, 500, 500),
            (300, "SYN", 100.28, 100.32, 500, 500),
            (330, "SYN", 100.30, 100.34, 500, 500),
        ]
    )
    trades = trades_frame(
        [
            (3, "SYN", 100.00, 500),  # before the interval: excluded
            (5, "SYN", 100.02, 100),  # exactly at the start: included
            (30, "SYN", 100.08, 300),
            (65, "SYN", 100.14, 200),  # exactly at the end: included
            (70, "SYN", 100.20, 1000),  # after the interval: excluded
        ]
    )
    orders = orders_frame(
        [
            ("B1", "SYN", "BUY", 300, 5.0, 60.0, "TWAP"),
            ("S1", "SYN", "SELL", 300, 5.0, 60.0, "TWAP"),
        ]
    )
    fills = fills_frame(
        [
            ("B1-1", "B1", 12.0, 100, 100.04),  # bought at the t10 ask
            ("B1-2", "B1", 25.0, 200, 100.10),  # bought at the t20 ask
            ("S1-1", "S1", 12.0, 100, 100.00),  # sold at the t10 bid
            ("S1-2", "S1", 25.0, 200, 100.06),  # sold at the t20 bid
        ]
    )
    return quotes, trades, orders, fills
