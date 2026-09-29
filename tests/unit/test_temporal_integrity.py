"""No look-ahead: contemporaneous metrics may only use data stamped <= the event.

Future data is used exclusively by markouts, by design.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import polars as pl
import pytest

from execlens.data.schemas import FILL_SCHEMA
from execlens.data.synthetic import MarketConfig, OrderConfig, generate_market, generate_orders
from execlens.tca.engine import TcaResult, run_tca
from factories import empty, fills_frame, orders_frame, quotes_frame, trades_frame

# The next quote (t=6) is closer to the events at t=5.9 than the prior one
# (t=0); a "nearest timestamp" join would wrongly pick it.
NEAR_FUTURE_QUOTES = quotes_frame(
    [
        (0.0, "SYN", 99.99, 100.01, 100, 100),  # mid 100
        (6.0, "SYN", 199.99, 200.01, 100, 100),  # mid 200, in the future
        (100.0, "SYN", 199.99, 200.01, 100, 100),
    ]
)


def test_arrival_lookup_never_uses_a_future_quote_even_if_it_is_nearer() -> None:
    orders = orders_frame([("O1", "SYN", "BUY", 100, 5.9, None, None)])
    out = run_tca(NEAR_FUTURE_QUOTES, trades_frame([]), orders, empty(FILL_SCHEMA)).orders
    assert out["arrival_mid"][0] == pytest.approx(100.0)
    assert out["arrival_quote_timestamp"][0] <= out["arrival_timestamp"][0]


def test_fill_lookup_never_uses_a_future_quote() -> None:
    orders = orders_frame([("O1", "SYN", "BUY", 100, 0.0, None, None)])
    fills = fills_frame([("F1", "O1", 5.999999, 100, 100.01)])  # 1 microsecond before t=6
    out = run_tca(NEAR_FUTURE_QUOTES, trades_frame([]), orders, fills).fills
    assert out["fill_mid"][0] == pytest.approx(100.0)
    assert out["effective_spread_bps"][0] == pytest.approx(2.0, rel=1e-9)


def test_markout_intentionally_reads_the_future_quote() -> None:
    orders = orders_frame([("O1", "SYN", "BUY", 100, 0.0, None, None)])
    fills = fills_frame([("F1", "O1", 1.0, 100, 100.01)])
    out = run_tca(NEAR_FUTURE_QUOTES, trades_frame([]), orders, fills).fills.row(0, named=True)
    assert out["fill_mid"] == pytest.approx(100.0)  # contemporaneous: past quote
    assert out["future_mid_10s"] == pytest.approx(200.0)  # markout: t1 + 10 -> t6 quote
    assert out["markout_10s_bps"] == pytest.approx((200.0 - 100.01) / 100.01 * 1e4, rel=1e-9)


def random_fills(orders: pl.DataFrame, seed: int) -> pl.DataFrame:
    """Up to three fills per order at random times inside its horizon."""
    rng = np.random.default_rng(seed)
    rows = []
    for order in orders.iter_rows(named=True):
        horizon = order["execution_horizon_seconds"] or 60.0
        times = np.sort(rng.uniform(0.0, horizon, size=3))
        remaining = order["quantity"]
        for k, offset in enumerate(times):
            qty = max(1, remaining // (3 - k)) if k < 2 else remaining
            if qty <= 0 or remaining <= 0:
                break
            rows.append(
                {
                    "fill_id": f"{order['order_id']}-{k}",
                    "order_id": order["order_id"],
                    "timestamp": order["arrival_timestamp"] + timedelta(seconds=float(offset)),
                    "quantity": int(qty),
                    "price": 50.0 + rng.random(),
                }
            )
            remaining -= qty
    return pl.DataFrame(rows, schema=FILL_SCHEMA)


@pytest.fixture(scope="module")
def generated() -> tuple[MarketConfig, pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    config = MarketConfig(session_seconds=3_600, seed=21)
    market = generate_market(config)
    orders = generate_orders(
        config, OrderConfig(orders_per_symbol_per_session=15, horizons_seconds=(300, 600))
    )
    return config, market.quotes, market.trades, orders, random_fills(orders, seed=4)


def test_context_timestamps_never_exceed_event_timestamps(
    generated: tuple[MarketConfig, pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame],
) -> None:
    _, quotes, trades, orders, fills = generated
    result = run_tca(quotes, trades, orders, fills)
    assert (result.orders["arrival_quote_timestamp"] <= result.orders["arrival_timestamp"]).all()
    assert (result.fills["fill_quote_timestamp"] <= result.fills["timestamp"]).all()


def test_changing_future_quotes_cannot_change_contemporaneous_metrics(
    generated: tuple[MarketConfig, pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame],
) -> None:
    """Rewrite every quote after a cut-off; metrics of earlier events must not move."""
    config, quotes, trades, orders, fills = generated
    cutoff = config.session_opens()[0] + timedelta(minutes=30)
    after = pl.col("timestamp") > cutoff
    shocked = quotes.with_columns(
        pl.when(after).then(pl.col("bid_price") * 1.05).otherwise(pl.col("bid_price")),
        pl.when(after).then(pl.col("ask_price") * 1.05).otherwise(pl.col("ask_price")),
    )
    base: TcaResult = run_tca(quotes, trades, orders, fills)
    moved: TcaResult = run_tca(shocked, trades, orders, fills)

    early_fills = base.fills.filter(pl.col("timestamp") <= cutoff)["fill_id"]
    assert early_fills.len() > 20
    cols = ["fill_id", "fill_mid", "fill_spread_bps", "effective_spread_bps"]
    left = base.fills.filter(pl.col("fill_id").is_in(early_fills.implode())).select(cols)
    right = moved.fills.filter(pl.col("fill_id").is_in(early_fills.implode())).select(cols)
    assert left.equals(right)

    early_orders = base.orders.filter(pl.col("arrival_timestamp") <= cutoff)["order_id"]
    order_cols = ["order_id", "arrival_mid", "arrival_spread_bps", "arrival_cost_bps"]
    assert (
        base.orders.filter(pl.col("order_id").is_in(early_orders.implode()))
        .select(order_cols)
        .equals(
            moved.orders.filter(pl.col("order_id").is_in(early_orders.implode())).select(order_cols)
        )
    )

    # ...whereas markouts whose horizon crosses the cut-off DO see the shock.
    crossing = (pl.col("timestamp") <= cutoff) & (
        pl.col("timestamp") + pl.duration(seconds=300) > cutoff + timedelta(seconds=30)
    )
    before = base.fills.filter(crossing)["markout_300s_bps"].drop_nulls()
    after_shock = moved.fills.filter(crossing)["markout_300s_bps"].drop_nulls()
    assert before.len() > 0
    assert not before.equals(after_shock)
