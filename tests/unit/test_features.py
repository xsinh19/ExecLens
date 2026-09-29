from __future__ import annotations

import math
import statistics

import polars as pl
import pytest

from execlens.data.schemas import FILL_SCHEMA
from execlens.diagnostics.features import FeatureConfig, compute_features
from execlens.tca.engine import run_tca
from factories import empty, orders_frame, quotes_frame, trades_frame

A = 0.001  # 10 bps log move


def zigzag_quotes(symbol: str = "SYN", start: int = 0, end: int = 700) -> pl.DataFrame:
    """A quote every 10 s whose log-mid alternates between 0 and A (+/-10 bps returns)."""
    rows = []
    for k, s in enumerate(range(start, end + 1, 10)):
        mid = 100.0 * math.exp(A * (k % 2))
        rows.append((s, symbol, mid - 0.01, mid + 0.01, 100, 100))
    return quotes_frame(rows)


def features_for(quotes: pl.DataFrame, orders: pl.DataFrame, trades: pl.DataFrame | None = None):  # type: ignore[no-untyped-def]
    trades = trades if trades is not None else trades_frame([])
    tca = run_tca(quotes, trades, orders, empty(FILL_SCHEMA))
    return compute_features(quotes, tca.orders)


def test_pre_arrival_volatility_by_hand() -> None:
    orders = orders_frame([("O1", "SYN", "BUY", 100, 400.0, 60.0, None)])
    row = features_for(zigzag_quotes(), orders).row(0, named=True)
    # 30 returns of +/-10 bps (15 each, mean 0): sample std = 10 * sqrt(30 / 29).
    returns = [10.0 if k % 2 == 0 else -10.0 for k in range(30)]
    assert row["pre_arrival_volatility_bps"] == pytest.approx(statistics.stdev(returns), rel=1e-6)
    assert statistics.stdev(returns) == pytest.approx(10 * math.sqrt(30 / 29))


def test_pre_arrival_momentum_by_hand_and_by_side() -> None:
    # Mid rises linearly from 100 to 101 over the 300 s before arrival at t=300.
    quotes = quotes_frame(
        [(s, "SYN", 100 + s / 300 - 0.005, 100 + s / 300 + 0.005, 1, 1) for s in range(0, 301, 5)]
    )
    orders = orders_frame(
        [("B", "SYN", "BUY", 1, 300.0, None, None), ("S", "SYN", "SELL", 1, 300.0, None, None)]
    )
    out = features_for(quotes, orders)
    momentum = dict(zip(out["order_id"], out["pre_arrival_momentum_bps"], strict=True))
    # (101 - 100) / 100 * 1e4 = +100 bps: adverse for the buyer, favourable for the seller.
    assert momentum["B"] == pytest.approx(100.0, rel=1e-9)
    assert momentum["S"] == pytest.approx(-100.0, rel=1e-9)


def test_pre_arrival_features_ignore_data_after_arrival() -> None:
    orders = orders_frame([("O1", "SYN", "BUY", 100, 400.0, 60.0, None)])
    base = zigzag_quotes()
    shocked = base.with_columns(
        pl.when(pl.col("timestamp") > orders["arrival_timestamp"][0])
        .then(pl.col(c) * 3)
        .otherwise(pl.col(c))
        for c in ("bid_price", "ask_price")
    )
    cols = ["pre_arrival_volatility_bps", "pre_arrival_momentum_bps"]
    assert (
        features_for(base, orders).select(cols).equals(features_for(shocked, orders).select(cols))
    )


def test_interval_move_and_volume_rate() -> None:
    quotes = quotes_frame(
        [(s, "SYN", 99.99, 100.01, 1, 1) for s in range(0, 100, 10)]
        + [(s, "SYN", 100.99, 101.01, 1, 1) for s in range(100, 400, 10)]
    )
    trades = trades_frame([(70, "SYN", 100.0, 600), (150, "SYN", 101.0, 600)])
    orders = orders_frame(
        [("B", "SYN", "BUY", 1, 60.0, 120.0, None), ("S", "SYN", "SELL", 1, 60.0, 120.0, None)]
    )
    out = features_for(quotes, orders, trades)
    move = dict(zip(out["order_id"], out["interval_price_move_bps"], strict=True))
    # Mid 100 at arrival, 101 at the interval end (t=180): +100 bps against the buyer.
    assert move["B"] == pytest.approx(100.0, rel=1e-9)
    assert move["S"] == pytest.approx(-100.0, rel=1e-9)
    # 1,200 shares over the 2-minute interval [60, 180].
    assert out["market_volume_per_minute"].to_list() == pytest.approx([600.0, 600.0])


def test_volatility_needs_enough_fresh_samples() -> None:
    # Quotes 120 s apart: with a 30 s tolerance only the samples 0-30 s after
    # each quote are fresh, giving 3 + 3 + 3 = 9 returns (< 10 required).
    quotes = quotes_frame([(s, "SYN", 99.99, 100.01, 1, 1) for s in range(0, 301, 120)])
    orders = orders_frame([("O1", "SYN", "BUY", 1, 300.0, None, None)])
    assert features_for(quotes, orders)["pre_arrival_volatility_bps"][0] is None


def test_feature_config_validation() -> None:
    with pytest.raises(ValueError, match="lookback"):
        FeatureConfig(sample_seconds=10, lookback_seconds=15)
