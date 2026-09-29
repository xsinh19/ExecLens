"""TCA behaviour on edge cases: empty/partial fills, missing or stale data, ties."""

from __future__ import annotations

import polars as pl
import pytest

from execlens.data.schemas import FILL_SCHEMA, ORDER_SCHEMA, QUOTE_SCHEMA, TRADE_SCHEMA
from execlens.data.validation import DataValidationError
from execlens.tca.engine import TcaConfig, fill_columns, order_columns, run_tca
from factories import empty, fills_frame, orders_frame, quotes_frame, trades_frame

QUOTES = quotes_frame([(0, "SYN", 9.99, 10.01, 100, 100), (100, "SYN", 9.99, 10.01, 100, 100)])
TRADES = trades_frame([(10, "SYN", 10.00, 100), (20, "SYN", 10.02, 300)])


def only_order(**kwargs: object) -> dict[str, object]:
    result = run_tca(**kwargs)  # type: ignore[arg-type]
    assert result.orders.height == 1
    return result.orders.row(0, named=True)


def test_order_without_fills() -> None:
    orders = orders_frame([("O1", "SYN", "BUY", 500, 5.0, 30.0, "TWAP")])
    row = only_order(quotes=QUOTES, trades=TRADES, orders=orders, fills=empty(FILL_SCHEMA))
    assert row["executed_quantity"] == 0
    assert row["n_fills"] == 0
    assert row["completion_ratio"] == 0.0
    assert row["avg_fill_price"] is None
    assert row["arrival_cost_bps"] is None
    assert row["vwap_cost_bps"] is None
    assert row["effective_spread_bps"] is None
    assert row["order_duration_seconds"] is None
    assert row["markout_10s_bps"] is None
    assert row["markout_10s_unavailable_fills"] == 0
    # The benchmark interval [5, 35] still exists, so market volume is known.
    assert row["market_volume"] == 400
    assert row["participation_rate"] == 0.0
    assert row["arrival_mid"] == pytest.approx(10.0)


def test_order_without_fills_or_horizon_has_no_benchmark_interval() -> None:
    orders = orders_frame([("O1", "SYN", "BUY", 500, 5.0, None, None)])
    row = only_order(quotes=QUOTES, trades=TRADES, orders=orders, fills=empty(FILL_SCHEMA))
    assert row["benchmark_end"] is None
    assert row["market_volume"] is None
    assert row["market_vwap"] is None


def test_without_horizon_the_interval_ends_at_the_last_fill() -> None:
    orders = orders_frame([("O1", "SYN", "BUY", 500, 5.0, None, None)])
    fills = fills_frame([("F1", "O1", 12.0, 100, 10.01)])
    row = only_order(quotes=QUOTES, trades=TRADES, orders=orders, fills=fills)
    # Interval [5, 12] contains only the t10 trade.
    assert row["market_volume"] == 100
    assert row["market_vwap"] == pytest.approx(10.00)


def test_fills_after_the_horizon_extend_the_benchmark_interval() -> None:
    # Horizon ends at t=13 but the order kept filling until t=25, so the
    # interval becomes [5, 25] and covers both trades (t10 and t20).
    orders = orders_frame([("O1", "SYN", "BUY", 500, 5.0, 8.0, "TWAP")])
    fills = fills_frame([("F1", "O1", 6.0, 100, 10.01), ("F2", "O1", 25.0, 100, 10.03)])
    row = only_order(quotes=QUOTES, trades=TRADES, orders=orders, fills=fills)
    assert row["benchmark_end"] == row["last_fill_timestamp"]
    assert row["market_volume"] == 400
    # (100 * 10.00 + 300 * 10.02) / 400 = 4006 / 400 = 10.015
    assert row["market_vwap"] == pytest.approx(10.015, rel=1e-12)


def test_symbol_without_any_trade_data_has_unknown_volume_not_zero() -> None:
    quotes = pl.concat([QUOTES, quotes_frame([(0, "NOTAPE", 4.99, 5.01, 100, 100)])])
    orders = orders_frame(
        [
            ("O1", "NOTAPE", "BUY", 100, 5.0, 30.0, None),  # no trade data at all
            ("O2", "SYN", "BUY", 100, 30.0, 20.0, None),  # trade data, but none in [30, 50]
        ]
    )
    out = run_tca(quotes, TRADES, orders, empty(FILL_SCHEMA)).orders
    volumes = dict(zip(out["order_id"], out["market_volume"], strict=True))
    assert volumes == {"O1": None, "O2": 0}


def test_empty_inputs_produce_empty_results_with_full_schema() -> None:
    result = run_tca(
        empty(QUOTE_SCHEMA), empty(TRADE_SCHEMA), empty(ORDER_SCHEMA), empty(FILL_SCHEMA)
    )
    horizons = TcaConfig().markout_horizons_seconds
    assert result.orders.is_empty()
    assert result.fills.is_empty()
    assert result.orders.columns == order_columns(horizons)
    assert result.fills.columns == fill_columns(horizons)


def test_partially_filled_order_costs_only_the_executed_quantity() -> None:
    orders = orders_frame([("O1", "SYN", "BUY", 1000, 5.0, 30.0, None)])
    fills = fills_frame([("F1", "O1", 6.0, 400, 10.02)])
    row = only_order(quotes=QUOTES, trades=TRADES, orders=orders, fills=fills)
    assert row["executed_quantity"] == 400
    assert row["completion_ratio"] == pytest.approx(0.4)
    # (10.02 - 10.00) / 10.00 * 1e4 = 20 bps on the 400 executed shares.
    assert row["arrival_cost_bps"] == pytest.approx(20.0, rel=1e-9)
    assert row["quantity_to_market_volume"] == pytest.approx(1000 / 400)
    assert row["participation_rate"] == pytest.approx(400 / 400)


def test_zero_market_volume_makes_vwap_unavailable() -> None:
    orders = orders_frame([("O1", "SYN", "SELL", 100, 30.0, 20.0, None)])  # interval [30, 50]
    fills = fills_frame([("F1", "O1", 31.0, 100, 9.99)])
    row = only_order(quotes=QUOTES, trades=TRADES, orders=orders, fills=fills)
    assert row["market_volume"] == 0
    assert row["market_vwap"] is None
    assert row["vwap_cost_bps"] is None
    assert row["participation_rate"] is None
    assert row["quantity_to_market_volume"] is None
    assert row["arrival_cost_bps"] is not None


def test_missing_quotes_for_a_symbol() -> None:
    orders = orders_frame([("O1", "OTHER", "BUY", 100, 5.0, None, None)])
    fills = fills_frame([("F1", "O1", 6.0, 100, 10.0)])
    result = run_tca(QUOTES, TRADES, orders, fills)
    row = result.orders.row(0, named=True)
    assert row["arrival_quote_status"] == "MISSING"
    assert row["arrival_mid"] is None
    assert row["arrival_cost_bps"] is None
    assert row["effective_spread_bps"] is None
    assert row["n_fills_without_quote"] == 1
    fill = result.fills.row(0, named=True)
    assert fill["fill_quote_status"] == "MISSING"
    assert fill["effective_spread_bps"] is None
    assert fill["markout_10s_status"] == "MISSING"


def test_stale_quote_tolerance_is_configurable() -> None:
    quotes = quotes_frame([(0, "SYN", 9.99, 10.01, 100, 100), (200, "SYN", 9.99, 10.01, 1, 1)])
    orders = orders_frame([("O1", "SYN", "BUY", 100, 45.0, None, None)])
    fills = fills_frame([("F1", "O1", 46.0, 100, 10.01)])

    strict = run_tca(quotes, TRADES, orders, fills, TcaConfig(stale_quote_tolerance_seconds=30))
    row = strict.orders.row(0, named=True)
    assert row["arrival_quote_status"] == "STALE"
    assert row["arrival_quote_age_seconds"] == pytest.approx(45.0)
    assert row["arrival_mid"] is None  # stale prices are withheld, not forward-filled
    assert row["arrival_cost_bps"] is None
    assert row["effective_spread_bps"] is None
    assert strict.fills["fill_quote_status"].to_list() == ["STALE"]

    lenient = run_tca(quotes, TRADES, orders, fills, TcaConfig(stale_quote_tolerance_seconds=60))
    row = lenient.orders.row(0, named=True)
    assert row["arrival_quote_status"] == "OK"
    assert row["arrival_cost_bps"] == pytest.approx(10.0, rel=1e-9)


def test_quote_exactly_at_tolerance_is_still_fresh() -> None:
    orders = orders_frame([("O1", "SYN", "BUY", 100, 30.0, None, None)])
    row = only_order(quotes=QUOTES, trades=TRADES, orders=orders, fills=empty(FILL_SCHEMA))
    assert row["arrival_quote_age_seconds"] == pytest.approx(30.0)
    assert row["arrival_quote_status"] == "OK"


def test_one_stale_fill_makes_fill_weighted_order_metrics_unavailable() -> None:
    quotes = quotes_frame([(0, "SYN", 9.99, 10.01, 100, 100), (100, "SYN", 9.99, 10.01, 1, 1)])
    orders = orders_frame([("O1", "SYN", "BUY", 200, 0.0, None, None)])
    fills = fills_frame([("F1", "O1", 5.0, 100, 10.01), ("F2", "O1", 50.0, 100, 10.01)])
    row = only_order(quotes=quotes, trades=TRADES, orders=orders, fills=fills)
    assert row["n_fills_without_quote"] == 1
    assert row["effective_spread_bps"] is None
    assert row["avg_contemporaneous_spread_bps"] is None
    assert row["arrival_cost_bps"] == pytest.approx(10.0, rel=1e-9)  # arrival context is fine


def test_duplicate_quote_timestamps_use_the_last_input_row() -> None:
    quotes = quotes_frame(
        [
            (0, "SYN", 9.99, 10.01, 100, 100),
            (10, "SYN", 10.09, 10.11, 100, 100),  # superseded at the same instant
            (10, "SYN", 10.19, 10.21, 100, 100),  # final state at t=10
            (100, "SYN", 9.99, 10.01, 100, 100),
        ]
    )
    orders = orders_frame([("O1", "SYN", "BUY", 100, 10.0, None, None)])
    row = only_order(quotes=quotes, trades=TRADES, orders=orders, fills=empty(FILL_SCHEMA))
    assert row["arrival_mid"] == pytest.approx(10.20)


def test_duplicate_trade_timestamps_are_all_counted() -> None:
    trades = trades_frame([(10, "SYN", 10.00, 100), (10, "SYN", 10.10, 100)])
    orders = orders_frame([("O1", "SYN", "BUY", 100, 10.0, 0.5, None)])  # interval [10, 10.5]
    row = only_order(quotes=QUOTES, trades=trades, orders=orders, fills=empty(FILL_SCHEMA))
    assert row["market_volume"] == 200
    assert row["market_vwap"] == pytest.approx(10.05)


def test_exact_timestamp_match_uses_the_quote_at_that_instant() -> None:
    quotes = quotes_frame(
        [
            (0, "SYN", 9.99, 10.01, 100, 100),
            (10, "SYN", 10.09, 10.11, 100, 100),
            (100, "SYN", 9.99, 10.01, 100, 100),
        ]
    )
    orders = orders_frame([("O1", "SYN", "BUY", 100, 10.0, None, None)])
    fills = fills_frame([("F1", "O1", 10.0, 100, 10.11)])
    result = run_tca(quotes, TRADES, orders, fills)
    assert result.orders["arrival_mid"][0] == pytest.approx(10.10)
    assert result.fills["fill_mid"][0] == pytest.approx(10.10)
    assert result.fills["fill_quote_age_seconds"][0] == 0.0


def test_order_before_the_first_quote_has_missing_context() -> None:
    quotes = quotes_frame([(60, "SYN", 9.99, 10.01, 100, 100)])
    orders = orders_frame([("O1", "SYN", "BUY", 100, 30.0, None, None)])
    row = only_order(quotes=quotes, trades=TRADES, orders=orders, fills=empty(FILL_SCHEMA))
    assert row["arrival_quote_status"] == "MISSING"
    assert row["arrival_mid"] is None


def test_order_long_after_the_last_quote_has_stale_context() -> None:
    orders = orders_frame([("O1", "SYN", "BUY", 100, 500.0, None, None)])
    row = only_order(quotes=QUOTES, trades=TRADES, orders=orders, fills=empty(FILL_SCHEMA))
    assert row["arrival_quote_status"] == "STALE"
    assert row["arrival_quote_age_seconds"] == pytest.approx(400.0)


def test_markout_beyond_the_end_of_market_data_is_missing_not_hidden() -> None:
    # Quotes end at t=100; fills at t=75/80 have 10 s data but no 30/60/300 s data.
    quotes = quotes_frame([(s, "SYN", 9.99, 10.01, 100, 100) for s in (0, 70, 80, 90, 100)])
    orders = orders_frame([("O1", "SYN", "BUY", 200, 70.0, None, None)])
    fills = fills_frame([("F1", "O1", 75.0, 100, 10.01), ("F2", "O1", 80.0, 100, 10.01)])
    result = run_tca(quotes, TRADES, orders, fills)
    row = result.orders.row(0, named=True)
    # Mid 10.00 vs 10.01 paid: (10.00 - 10.01) / 10.01 * 1e4 = -100 / 10.01 bps.
    assert row["markout_10s_bps"] == pytest.approx(-100 / 10.01, rel=1e-9)
    assert row["markout_10s_unavailable_fills"] == 0
    # F1 (t75 + 30 = 105) and F2 (t110) are both beyond the data at 30 s.
    assert row["markout_30s_bps"] is None
    assert row["markout_30s_unavailable_fills"] == 2
    assert result.fills["markout_30s_status"].to_list() == ["MISSING", "MISSING"]


def test_markout_on_a_stale_future_quote_is_unavailable() -> None:
    quotes = quotes_frame(
        [
            (0, "SYN", 9.99, 10.01, 100, 100),
            (10, "SYN", 9.99, 10.01, 1, 1),
            (500, "SYN", 9, 11, 1, 1),
        ]
    )
    orders = orders_frame([("O1", "SYN", "BUY", 100, 5.0, None, None)])
    fills = fills_frame([("F1", "O1", 5.0, 100, 10.01)])
    result = run_tca(quotes, TRADES, orders, fills)
    # t5 + 60 = 65: prevailing quote is from t10 (55 s old) -> stale.
    assert result.fills["markout_60s_status"][0] == "STALE"
    assert result.fills["markout_60s_bps"][0] is None
    # t5 + 10 = 15: t10 quote is 5 s old -> available.
    assert result.fills["markout_10s_status"][0] == "OK"


def test_symbols_never_borrow_each_others_quotes() -> None:
    quotes = quotes_frame([(0, "AAA", 9.99, 10.01, 1, 1), (9, "BBB", 49.99, 50.01, 1, 1)])
    orders = orders_frame(
        [("O1", "AAA", "BUY", 1, 10.0, None, None), ("O2", "BBB", "BUY", 1, 10.0, None, None)]
    )
    out = run_tca(quotes, empty(TRADE_SCHEMA), orders, empty(FILL_SCHEMA)).orders
    mids = dict(zip(out["order_id"], out["arrival_mid"], strict=True))
    assert mids == {"O1": pytest.approx(10.0), "O2": pytest.approx(50.0)}


@pytest.mark.parametrize(
    ("quotes", "fills", "message"),
    [
        (
            quotes_frame([(0, "SYN", 10.02, 10.00, 100, 100)]),
            fills_frame([("F1", "O1", 6.0, 100, 10.0)]),
            "crossed market",
        ),
        (
            quotes_frame([(0, "SYN", -1.0, 10.00, 100, 100)]),
            fills_frame([("F1", "O1", 6.0, 100, 10.0)]),
            "bid_price must be a positive",
        ),
        (QUOTES, fills_frame([("F1", "O1", 6.0, 0, 10.0)]), "quantity must be > 0"),
        (QUOTES, fills_frame([("F1", "O1", 6.0, -5, 10.0)]), "quantity must be > 0"),
        (QUOTES, fills_frame([("F1", "O1", 6.0, 101, 10.0)]), "exceeds the parent order quantity"),
        (QUOTES, fills_frame([("F1", "O1", 6.0, 10, 0.0)]), "price must be a positive"),
    ],
)
def test_invalid_inputs_are_rejected_before_analysis(
    quotes: pl.DataFrame, fills: pl.DataFrame, message: str
) -> None:
    orders = orders_frame([("O1", "SYN", "BUY", 100, 5.0, None, None)])
    with pytest.raises(DataValidationError, match=message):
        run_tca(quotes, TRADES, orders, fills)


def test_invalid_configuration_is_rejected() -> None:
    with pytest.raises(ValueError, match="stale_quote_tolerance_seconds"):
        TcaConfig(stale_quote_tolerance_seconds=0)
    with pytest.raises(ValueError, match="markout_horizons_seconds"):
        TcaConfig(markout_horizons_seconds=(10, 10))
    with pytest.raises(ValueError, match="markout_horizons_seconds"):
        TcaConfig(markout_horizons_seconds=())


def test_lazy_inputs_are_accepted() -> None:
    orders = orders_frame([("O1", "SYN", "BUY", 100, 5.0, 30.0, None)])
    fills = fills_frame([("F1", "O1", 6.0, 100, 10.01)])
    eager = run_tca(QUOTES, TRADES, orders, fills)
    lazy = run_tca(QUOTES.lazy(), TRADES.lazy(), orders.lazy(), fills.lazy())
    assert eager.orders.equals(lazy.orders)
    unvalidated = run_tca(QUOTES.lazy(), TRADES.lazy(), orders.lazy(), fills.lazy(), validate=False)
    assert eager.orders.equals(unvalidated.orders)
