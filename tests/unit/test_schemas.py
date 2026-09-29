from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from execlens.data.schemas import Fill, MarketTrade, Order, Quote, Side

UTC_TS = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)


def quote(**overrides: object) -> Quote:
    fields: dict[str, object] = {
        "timestamp": UTC_TS,
        "symbol": "SYN",
        "bid_price": 99.98,
        "ask_price": 100.02,
        "bid_size": 500,
        "ask_size": 700,
    }
    fields.update(overrides)
    return Quote.model_validate(fields)


def test_side_sign_convention() -> None:
    assert Side.BUY.sign == 1
    assert Side.SELL.sign == -1


def test_quote_derived_fields_match_hand_calculation() -> None:
    # mid = (99.98 + 100.02) / 2 = 100.00; spread = 0.04; spread_bps = 0.04 / 100 * 1e4 = 4.
    q = quote()
    assert q.mid == pytest.approx(100.0)
    assert q.spread == pytest.approx(0.04)
    assert q.spread_bps == pytest.approx(4.0)


def test_locked_market_is_allowed() -> None:
    q = quote(bid_price=100.0, ask_price=100.0)
    assert q.spread == 0.0
    assert q.spread_bps == 0.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"bid_price": 100.05, "ask_price": 100.00},  # crossed market
        {"bid_price": 0.0},
        {"ask_price": -1.0},
        {"bid_price": float("nan")},
        {"bid_size": -1},
        {"ask_size": -5},
        {"symbol": ""},
        {"timestamp": datetime(2026, 1, 5, 14, 30)},  # naive timestamp
    ],
)
def test_quote_rejects_invalid_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        quote(**overrides)


def test_aware_non_utc_timestamps_are_normalised_to_utc() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    q = quote(timestamp=datetime(2026, 1, 5, 20, 0, tzinfo=ist))
    assert q.timestamp == UTC_TS
    assert q.timestamp.utcoffset() == timedelta(0)


@pytest.mark.parametrize("overrides", [{"price": 0.0}, {"size": 0}, {"size": -10}])
def test_market_trade_rejects_invalid_values(overrides: dict[str, object]) -> None:
    fields: dict[str, object] = {"timestamp": UTC_TS, "symbol": "SYN", "price": 10.0, "size": 5}
    fields.update(overrides)
    with pytest.raises(ValidationError):
        MarketTrade.model_validate(fields)


@pytest.mark.parametrize(
    "overrides",
    [
        {"quantity": 0},
        {"quantity": -100},
        {"side": "HOLD"},
        {"execution_horizon_seconds": 0.0},
        {"execution_horizon_seconds": -30.0},
    ],
)
def test_order_rejects_invalid_values(overrides: dict[str, object]) -> None:
    fields: dict[str, object] = {
        "order_id": "O1",
        "symbol": "SYN",
        "side": "BUY",
        "quantity": 100,
        "arrival_timestamp": UTC_TS,
    }
    fields.update(overrides)
    with pytest.raises(ValidationError):
        Order.model_validate(fields)


def test_order_parses_side_and_optional_fields() -> None:
    order = Order.model_validate(
        {
            "order_id": "O1",
            "symbol": "SYN",
            "side": "SELL",
            "quantity": 100,
            "arrival_timestamp": UTC_TS,
        }
    )
    assert order.side is Side.SELL
    assert order.execution_horizon_seconds is None
    assert order.strategy is None


@pytest.mark.parametrize("overrides", [{"quantity": 0}, {"quantity": -1}, {"price": 0.0}])
def test_fill_rejects_invalid_values(overrides: dict[str, object]) -> None:
    fields: dict[str, object] = {
        "fill_id": "F1",
        "order_id": "O1",
        "timestamp": UTC_TS,
        "quantity": 10,
        "price": 10.0,
    }
    fields.update(overrides)
    with pytest.raises(ValidationError):
        Fill.model_validate(fields)
