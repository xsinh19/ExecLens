from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from execlens.data.schemas import Fill, Order, Side, to_epoch_us

UTC_TS = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)


def test_side_sign_convention() -> None:
    assert Side.BUY.sign == 1
    assert Side.SELL.sign == -1


def test_epoch_microseconds_by_hand() -> None:
    # One day and one microsecond after the epoch: 86,400 * 10**6 + 1.
    assert to_epoch_us(datetime(1970, 1, 2, 0, 0, 0, 1, tzinfo=UTC)) == 86_400_000_001


def test_epoch_microseconds_are_the_same_instant_in_any_time_zone() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    new_york = timezone(timedelta(hours=-5))
    assert to_epoch_us(datetime(2026, 1, 5, 20, 0, tzinfo=ist)) == to_epoch_us(UTC_TS)
    assert to_epoch_us(datetime(2026, 1, 5, 9, 30, tzinfo=new_york)) == to_epoch_us(UTC_TS)


def test_epoch_microseconds_reject_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        to_epoch_us(datetime(2026, 1, 5, 14, 30))


def test_aware_non_utc_timestamps_are_normalised_to_utc() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    order = Order.model_validate(
        {
            "order_id": "O1",
            "symbol": "SYN",
            "side": "BUY",
            "quantity": 100,
            "arrival_timestamp": datetime(2026, 1, 5, 20, 0, tzinfo=ist),
        }
    )
    assert order.arrival_timestamp == UTC_TS
    assert order.arrival_timestamp.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"arrival_timestamp": datetime(2026, 1, 5, 14, 30)},  # naive timestamp
        {"symbol": ""},
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
