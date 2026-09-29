"""The vectorised engine and the loop-based reference must agree."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import polars as pl
import pytest

from execlens.data.schemas import FILL_SCHEMA
from execlens.data.synthetic import MarketConfig, OrderConfig, generate_market, generate_orders
from execlens.tca.engine import TcaConfig, run_tca
from execlens.tca.reference import compare_to_reference, reference_columns, reference_tca
from factories import hand_example


def random_fills(orders: pl.DataFrame, seed: int) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for order in orders.iter_rows(named=True):
        n = int(rng.integers(0, 5))  # includes orders with no fills
        horizon = order["execution_horizon_seconds"] or 30.0
        # Up to 20% past the horizon, so interval extension is exercised too.
        offsets = np.sort(rng.uniform(0.0, 1.2 * horizon, size=n))
        quantities = (
            rng.multinomial(int(order["quantity"] * rng.uniform(0.3, 1.0)), [1 / n] * n)
            if n
            else []
        )
        for k, (offset, qty) in enumerate(zip(offsets, quantities, strict=True)):
            if qty == 0:
                continue
            rows.append(
                {
                    "fill_id": f"{order['order_id']}-{k}",
                    "order_id": order["order_id"],
                    "timestamp": order["arrival_timestamp"] + timedelta(seconds=float(offset)),
                    "quantity": int(qty),
                    "price": round(float(40 + 80 * rng.random()), 4),
                }
            )
    return pl.DataFrame(rows, schema=FILL_SCHEMA)


def test_reference_reproduces_the_hand_calculated_example() -> None:
    reference = reference_tca(*hand_example())
    buy = reference.filter(pl.col("order_id") == "B1").row(0, named=True)
    assert buy["avg_fill_price"] == pytest.approx(100.08, rel=1e-9)
    assert buy["arrival_cost_bps"] == pytest.approx(8.0, rel=1e-6)
    assert buy["market_vwap"] == pytest.approx(100.09, rel=1e-9)
    assert buy["vwap_cost_bps"] == pytest.approx(-100 / 100.09, rel=1e-6)
    sell = reference.filter(pl.col("order_id") == "S1").row(0, named=True)
    assert sell["arrival_cost_bps"] == pytest.approx(-4.0, rel=1e-6)


def test_engine_matches_reference_on_the_hand_example() -> None:
    data = hand_example()
    assert compare_to_reference(run_tca(*data).orders, reference_tca(*data)) == []


@pytest.mark.parametrize("tolerance", [30.0, 2.0])
def test_engine_matches_reference_on_generated_data(tolerance: float) -> None:
    """A tight tolerance forces many STALE contexts in the sparse scenario."""
    config = MarketConfig(session_seconds=3_600, seed=5)
    market = generate_market(config)
    orders = generate_orders(
        config, OrderConfig(orders_per_symbol_per_session=20, horizons_seconds=(300, 600, 900))
    )
    fills = random_fills(orders, seed=9)
    tca_config = TcaConfig(stale_quote_tolerance_seconds=tolerance)
    engine = run_tca(market.quotes, market.trades, orders, fills, tca_config).orders
    reference = reference_tca(market.quotes, market.trades, orders, fills, tca_config)
    assert compare_to_reference(engine, reference) == []
    # The comparison must have exercised real values and unavailable ones.
    assert engine["arrival_cost_bps"].null_count() < engine.height
    if tolerance == 2.0:
        assert (engine["arrival_quote_status"] == "STALE").any()
        assert (engine["n_fills_without_quote"] > 0).any()


def test_comparison_detects_disagreement() -> None:
    data = hand_example()
    engine = run_tca(*data).orders
    reference = reference_tca(*data)
    tampered = engine.with_columns(
        pl.when(pl.col("order_id") == "B1")
        .then(pl.col("arrival_cost_bps") + 1e-3)
        .otherwise(pl.col("arrival_cost_bps"))
    )
    problems = compare_to_reference(tampered, reference)
    assert problems == ["arrival_cost_bps: 1 rows outside tolerance (max |diff| 0.001)"]

    nulled = engine.with_columns(pl.lit(None, pl.Float64).alias("market_vwap"))
    assert compare_to_reference(nulled, reference) == [
        "market_vwap: null pattern differs in 2 rows"
    ]
    assert compare_to_reference(engine.head(1), reference)[0].startswith("order sets differ")
    miscounted = engine.with_columns(pl.col("n_fills") + 1)
    assert compare_to_reference(miscounted, reference) == ["n_fills: values differ"]


def test_reference_columns_cover_every_markout_horizon() -> None:
    columns = reference_columns((5, 15))
    assert "markout_5s_bps" in columns
    assert "markout_15s_unavailable_fills" in columns
