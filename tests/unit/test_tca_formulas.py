"""Every TCA metric checked against a manually calculated expected value.

Expected values are written out as explicit arithmetic on the input numbers;
none are derived by calling production code.
"""

from __future__ import annotations

import polars as pl
import pytest

from execlens.tca.engine import TcaResult, run_tca
from execlens.tca.metrics import cost_bps, effective_spread_bps, markout_bps, side_sign
from factories import fills_frame, hand_example, orders_frame, quotes_frame, trades_frame

REL = 1e-9


@pytest.fixture(scope="module")
def result() -> TcaResult:
    # See factories.hand_example: B1 buys and S1 sells 300 shares in the same
    # market; the benchmark interval is [5, 65].
    return run_tca(*hand_example())


def order_row(result: TcaResult, order_id: str) -> dict[str, object]:
    return result.orders.filter(pl.col("order_id") == order_id).row(0, named=True)


def fill_row(result: TcaResult, fill_id: str) -> dict[str, object]:
    return result.fills.filter(pl.col("fill_id") == fill_id).row(0, named=True)


# --- formula building blocks -------------------------------------------------


def evaluate(expr: pl.Expr) -> float:
    value = pl.select(expr).item()
    assert isinstance(value, float)
    return value


def test_side_sign_expression() -> None:
    signs = pl.select(side_sign(pl.Series(["BUY", "SELL", "HOLD", None]))).to_series().to_list()
    assert signs == [1, -1, None, None]


@pytest.mark.parametrize(
    ("sign", "price", "benchmark", "expected"),
    [
        (1, 100.10, 100.00, 10.0),  # BUY above benchmark: +10 bps (worse)
        (-1, 99.90, 100.00, 10.0),  # SELL below benchmark: +10 bps (worse)
        (1, 99.95, 100.00, -5.0),  # BUY below benchmark: -5 bps (better)
        (-1, 100.05, 100.00, -5.0),  # SELL above benchmark: -5 bps (better)
    ],
)
def test_cost_bps_sign_convention(
    sign: int, price: float, benchmark: float, expected: float
) -> None:
    assert evaluate(cost_bps(pl.lit(sign), pl.lit(price), pl.lit(benchmark))) == pytest.approx(
        expected, rel=REL
    )


def test_effective_spread_is_twice_the_signed_half_spread() -> None:
    # BUY at the ask 100.02 with mid 100.00: 2 * 0.02 / 100 * 1e4 = 4 bps.
    assert evaluate(effective_spread_bps(pl.lit(1), pl.lit(100.02), pl.lit(100.0))) == (
        pytest.approx(4.0, rel=REL)
    )
    # SELL at the bid 99.98: 2 * -1 * (99.98 - 100) / 100 * 1e4 = 4 bps.
    assert evaluate(effective_spread_bps(pl.lit(-1), pl.lit(99.98), pl.lit(100.0))) == (
        pytest.approx(4.0, rel=REL)
    )
    # Filled at the mid: zero; filled inside the spread in our favour: negative.
    assert evaluate(effective_spread_bps(pl.lit(1), pl.lit(100.0), pl.lit(100.0))) == 0.0
    assert evaluate(effective_spread_bps(pl.lit(1), pl.lit(99.99), pl.lit(100.0))) < 0


def test_markout_sign_convention_is_positive_when_favourable() -> None:
    # BUY at 100.02, mid later 100.10: favourable, +(0.08 / 100.02) * 1e4.
    assert evaluate(markout_bps(pl.lit(1), pl.lit(100.02), pl.lit(100.10))) == pytest.approx(
        800 / 100.02, rel=REL
    )
    # SELL at 99.98, mid later 100.10: adverse, -(0.12 / 99.98) * 1e4.
    assert evaluate(markout_bps(pl.lit(-1), pl.lit(99.98), pl.lit(100.10))) == pytest.approx(
        -1200 / 99.98, rel=REL
    )


# --- spec examples (section 14) ---------------------------------------------


@pytest.mark.parametrize(("side", "fill_price"), [("BUY", 100.10), ("SELL", 99.90)])
def test_arrival_cost_spec_examples_are_plus_ten_bps(side: str, fill_price: float) -> None:
    quotes = quotes_frame([(0, "SYN", 99.98, 100.02, 100, 100)])
    orders = orders_frame([("O1", "SYN", side, 100, 1.0, None, None)])
    fills = fills_frame([("F1", "O1", 2.0, 100, fill_price)])
    out = run_tca(quotes, trades_frame([]), orders, fills).orders.row(0, named=True)
    assert out["arrival_mid"] == pytest.approx(100.0, rel=REL)
    assert out["arrival_cost_bps"] == pytest.approx(10.0, rel=REL)


def test_quantity_weighted_average_fill_price() -> None:
    # (100 * 10.00 + 300 * 10.40 + 600 * 10.10) / 1000 = 10180 / 1000 = 10.18
    quotes = quotes_frame([(0, "SYN", 9.99, 10.01, 100, 100)])
    orders = orders_frame([("O1", "SYN", "BUY", 1000, 0.0, None, None)])
    fills = fills_frame(
        [
            ("F1", "O1", 1.0, 100, 10.00),
            ("F2", "O1", 2.0, 300, 10.40),
            ("F3", "O1", 3.0, 600, 10.10),
        ]
    )
    out = run_tca(quotes, trades_frame([]), orders, fills).orders.row(0, named=True)
    assert out["avg_fill_price"] == pytest.approx(10.18, rel=REL)
    assert out["executed_notional"] == pytest.approx(10180.0, rel=REL)
    # Arrival mid 10.00: cost = (10.18 - 10.00) / 10.00 * 1e4 = 180 bps.
    assert out["arrival_cost_bps"] == pytest.approx(180.0, rel=REL)


# --- full hand-calculated example: BUY ---------------------------------------


def test_buy_order_arrival_context(result: TcaResult) -> None:
    row = order_row(result, "B1")
    assert row["arrival_quote_status"] == "OK"
    assert row["arrival_mid"] == pytest.approx(100.00, rel=REL)
    assert row["arrival_spread"] == pytest.approx(0.04, rel=1e-6)
    assert row["arrival_spread_bps"] == pytest.approx(0.04 / 100.00 * 1e4, rel=1e-6)
    assert row["arrival_quote_age_seconds"] == pytest.approx(5.0)


def test_buy_order_execution_metrics(result: TcaResult) -> None:
    row = order_row(result, "B1")
    assert row["executed_quantity"] == 300
    assert row["n_fills"] == 2
    assert row["completion_ratio"] == pytest.approx(1.0)
    # (100 * 100.04 + 200 * 100.10) / 300 = 30024 / 300 = 100.08
    assert row["avg_fill_price"] == pytest.approx(100.08, rel=REL)
    assert row["order_duration_seconds"] == pytest.approx(20.0)  # last fill 25 - arrival 5
    # Arrival cost = (100.08 - 100.00) / 100.00 * 1e4 = 8 bps.
    assert row["arrival_cost_bps"] == pytest.approx(8.0, rel=1e-6)


def test_buy_order_interval_vwap(result: TcaResult) -> None:
    row = order_row(result, "B1")
    # Trades in [5, 65]: 100@100.02, 300@100.08, 200@100.14 -> 60054 / 600 = 100.09.
    assert row["market_volume"] == 600
    assert row["market_vwap"] == pytest.approx(100.09, rel=REL)
    # VWAP cost = (100.08 - 100.09) / 100.09 * 1e4 = -100 / 100.09 bps (better than VWAP).
    assert row["vwap_cost_bps"] == pytest.approx(-100 / 100.09, rel=1e-6)
    assert row["participation_rate"] == pytest.approx(300 / 600)
    assert row["quantity_to_market_volume"] == pytest.approx(300 / 600)


def test_buy_fill_level_effective_spread(result: TcaResult) -> None:
    f1, f2 = fill_row(result, "B1-1"), fill_row(result, "B1-2")
    # F1 at 100.04 vs mid 100.02 (t10 quote): 2 * 0.02 / 100.02 * 1e4 = 400 / 100.02.
    assert f1["fill_mid"] == pytest.approx(100.02, rel=REL)
    assert f1["effective_spread_bps"] == pytest.approx(400 / 100.02, rel=1e-6)
    # F2 at 100.10 vs mid 100.08 (t20 quote): 400 / 100.08.
    assert f2["fill_mid"] == pytest.approx(100.08, rel=REL)
    assert f2["effective_spread_bps"] == pytest.approx(400 / 100.08, rel=1e-6)


def test_buy_order_quantity_weighted_spreads(result: TcaResult) -> None:
    row = order_row(result, "B1")
    expected = (100 * (400 / 100.02) + 200 * (400 / 100.08)) / 300
    assert row["effective_spread_bps"] == pytest.approx(expected, rel=1e-6)
    # Fills at the touch: effective spread equals the quoted spread (0.04 / mid).
    assert row["avg_contemporaneous_spread_bps"] == pytest.approx(expected, rel=1e-6)


def test_buy_markouts_use_prevailing_future_mid(result: TcaResult) -> None:
    f1, f2 = fill_row(result, "B1-1"), fill_row(result, "B1-2")
    # 10 s: F1 (t12) -> mid at t22 = 100.08; F2 (t25) -> mid at t35 = 100.08.
    assert f1["future_mid_10s"] == pytest.approx(100.08, rel=REL)
    assert f1["markout_10s_bps"] == pytest.approx(400 / 100.04, rel=1e-6)
    assert f2["markout_10s_bps"] == pytest.approx(-200 / 100.10, rel=1e-6)
    # 60 s: F1 -> t72 -> 100.18 (t70 quote); F2 -> t85 -> 100.20 (t80 quote).
    assert f1["future_mid_60s"] == pytest.approx(100.18, rel=REL)
    assert f2["future_mid_60s"] == pytest.approx(100.20, rel=REL)

    row = order_row(result, "B1")
    expected = {
        10: (100 * (400 / 100.04) + 200 * (-200 / 100.10)) / 300,
        30: (100 * (800 / 100.04) + 200 * (200 / 100.10)) / 300,
        60: (100 * (1400 / 100.04) + 200 * (1000 / 100.10)) / 300,
        300: (100 * (2600 / 100.04) + 200 * (2000 / 100.10)) / 300,
    }
    for horizon, value in expected.items():
        assert row[f"markout_{horizon}s_bps"] == pytest.approx(value, rel=1e-6), horizon
        assert row[f"markout_{horizon}s_unavailable_fills"] == 0


def test_buy_mid_markout_isolates_the_subsequent_price_move(result: TcaResult) -> None:
    row = order_row(result, "B1")
    # 10 s: B1-1 mid 100.02 -> 100.08: +600 / 100.02; B1-2 mid 100.08 -> 100.08: 0.
    assert row["mid_markout_10s_bps"] == pytest.approx(100 * (600 / 100.02) / 300, rel=1e-6)
    # 60 s: B1-1 100.02 -> 100.18 (+1600/100.02); B1-2 100.08 -> 100.20 (+1200/100.08).
    expected = (100 * (1600 / 100.02) + 200 * (1200 / 100.08)) / 300
    assert row["mid_markout_60s_bps"] == pytest.approx(expected, rel=1e-6)


def test_fill_markout_includes_the_half_spread_paid_but_mid_markout_does_not() -> None:
    # Flat market 9.99 / 10.01 (mid 10.00); a BUY lifts the offer at 10.01.
    quotes = quotes_frame([(s, "SYN", 9.99, 10.01, 100, 100) for s in range(0, 400, 10)])
    orders = orders_frame([("O1", "SYN", "BUY", 100, 1.0, None, None)])
    fills = fills_frame([("F1", "O1", 5.0, 100, 10.01)])
    row = run_tca(quotes, trades_frame([]), orders, fills).orders.row(0, named=True)
    # Fill markout: (10.00 - 10.01) / 10.01 * 1e4 = -100 / 10.01 at every horizon.
    for horizon in (10, 30, 60, 300):
        assert row[f"markout_{horizon}s_bps"] == pytest.approx(-100 / 10.01, rel=1e-9)
        assert row[f"mid_markout_{horizon}s_bps"] == 0.0
    # ... which is exactly minus half the effective spread (20 bps).
    assert row["effective_spread_bps"] == pytest.approx(20.0, rel=1e-9)


# --- the same market for a SELL: direction symmetry ---------------------------


def test_sell_order_costs(result: TcaResult) -> None:
    row = order_row(result, "S1")
    # (100 * 100.00 + 200 * 100.06) / 300 = 30012 / 300 = 100.04
    assert row["avg_fill_price"] == pytest.approx(100.04, rel=REL)
    # Sold 4 bps above the arrival mid: cost = -1 * (100.04 - 100) / 100 * 1e4 = -4 bps (better).
    assert row["arrival_cost_bps"] == pytest.approx(-4.0, rel=1e-6)
    # Sold below the interval VWAP 100.09: -1 * (100.04 - 100.09) / 100.09 * 1e4 = +500 / 100.09.
    assert row["vwap_cost_bps"] == pytest.approx(500 / 100.09, rel=1e-6)
    # Fills at the bid pay the same effective spread as buys at the ask.
    expected_spread = (100 * (400 / 100.02) + 200 * (400 / 100.08)) / 300
    assert row["effective_spread_bps"] == pytest.approx(expected_spread, rel=1e-6)


def test_sell_markouts_are_adverse_in_a_rising_market(result: TcaResult) -> None:
    row = order_row(result, "S1")
    # 10 s: S1-1 sold 100.00, mid 100.08 -> -8 bps; S1-2 sold 100.06, mid 100.08 -> -200/100.06.
    expected_10s = (100 * -8.0 + 200 * (-200 / 100.06)) / 300
    assert row["markout_10s_bps"] == pytest.approx(expected_10s, rel=1e-6)
    assert row["markout_10s_bps"] < 0
