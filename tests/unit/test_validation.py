from datetime import UTC, datetime

import polars as pl
import pytest
from pydantic import ValidationError

from execlens.data.schemas import (
    FILL_SCHEMA,
    ORDER_SCHEMA,
    QUOTE_SCHEMA,
    TRADE_SCHEMA,
    Fill,
    Order,
    Quote,
)
from execlens.data.validation import (
    DataValidationError,
    ExecutionDataset,
    validate_fills,
    validate_orders,
    validate_quotes,
    validate_tables,
    validate_trades,
)
from factories import (
    empty,
    fills_frame,
    orders_frame,
    quotes_frame,
    trades_frame,
    ts,
)


def rules_of(error: DataValidationError) -> list[str]:
    return [issue.rule for issue in error.issues]


def valid_orders() -> pl.DataFrame:
    return orders_frame(
        [
            ("O1", "SYN", "BUY", 100, 10.0, 60.0, "TWAP"),
            ("O2", "SYN", "SELL", 50, 20.0, None, None),
        ]
    )


def test_valid_tables_pass_and_keep_canonical_schema() -> None:
    q = quotes_frame([(0, "SYN", 9.99, 10.01, 100, 100)])
    t = trades_frame([(1, "SYN", 10.0, 10)])
    o = valid_orders()
    f = fills_frame([("F1", "O1", 12.0, 40, 10.01), ("F2", "O1", 15.0, 60, 10.02)])
    vq, vt, vo, vf = validate_tables(q, t, o, f)
    assert vq.schema == pl.Schema(QUOTE_SCHEMA)
    assert vt.schema == pl.Schema(TRADE_SCHEMA)
    assert vo.schema == pl.Schema(ORDER_SCHEMA)
    assert vf.schema == pl.Schema(FILL_SCHEMA)


def test_empty_tables_are_valid() -> None:
    validate_tables(
        empty(QUOTE_SCHEMA), empty(TRADE_SCHEMA), empty(ORDER_SCHEMA), empty(FILL_SCHEMA)
    )


def test_lossless_normalisation_of_dtypes_and_timezones() -> None:
    q = quotes_frame([(0, "SYN", 9.99, 10.01, 100, 100)]).with_columns(
        pl.col("timestamp").dt.cast_time_unit("ns").dt.convert_time_zone("Asia/Kolkata"),
        pl.col("bid_size").cast(pl.Int32),
        pl.col("ask_size").cast(pl.Float64),  # whole numbers are accepted
    )
    out = validate_quotes(q)
    assert out.schema == pl.Schema(QUOTE_SCHEMA)
    assert out["timestamp"][0] == ts(0)


def test_naive_timestamps_are_rejected() -> None:
    q = quotes_frame([(0, "SYN", 9.99, 10.01, 100, 100)]).with_columns(
        pl.col("timestamp").dt.replace_time_zone(None)
    )
    with pytest.raises(DataValidationError, match="timezone-aware"):
        validate_quotes(q)


def test_missing_columns_are_reported() -> None:
    q = quotes_frame([(0, "SYN", 9.99, 10.01, 100, 100)]).drop("ask_size")
    with pytest.raises(DataValidationError, match="missing required column"):
        validate_quotes(q)


def test_fractional_quantities_are_rejected() -> None:
    o = valid_orders().with_columns(pl.col("quantity").cast(pl.Float64) + 0.5)
    with pytest.raises(DataValidationError, match="whole numbers"):
        validate_orders(o)


def test_quote_rules() -> None:
    q = quotes_frame(
        [
            (0, "SYN", 10.02, 10.00, 100, 100),  # crossed market
            (1, "SYN", 0.0, 10.00, 100, 100),  # non-positive bid
            (2, "SYN", 9.99, -1.0, 100, 100),  # non-positive ask (also crossed)
            (3, "SYN", 9.99, 10.01, -1, 100),  # negative bid size
            (4, "SYN", 9.99, 10.01, 100, -2),  # negative ask size
            (5, "", 9.99, 10.01, 100, 100),  # empty symbol
            (6, "SYN", float("nan"), 10.01, 100, 100),  # NaN price
            (7, "SYN", 10.00, 10.00, 0, 0),  # locked market with zero size: valid
        ]
    )
    with pytest.raises(DataValidationError) as info:
        validate_quotes(q)
    rules = rules_of(info.value)
    assert "crossed market: bid_price > ask_price" in rules
    assert "bid_price must be a positive finite number" in rules
    assert "ask_price must be a positive finite number" in rules
    assert "bid_size must be >= 0" in rules
    assert "ask_size must be >= 0" in rules
    assert "symbol must be non-empty" in rules
    by_rule = {i.rule: i.count for i in info.value.issues}
    assert by_rule["bid_price must be a positive finite number"] == 2  # zero and NaN
    assert by_rule["crossed market: bid_price > ask_price"] == 2


def test_trade_rules() -> None:
    t = trades_frame([(0, "SYN", 0.0, 10), (1, "SYN", 10.0, 0), (2, "SYN", 10.0, -3)])
    with pytest.raises(DataValidationError) as info:
        validate_trades(t)
    by_rule = {i.rule: i.count for i in info.value.issues}
    assert by_rule["price must be a positive finite number"] == 1
    assert by_rule["size must be > 0"] == 2


def test_order_rules() -> None:
    o = orders_frame(
        [
            ("O1", "SYN", "BUY", 0, 0.0, None, None),  # zero quantity
            ("O1", "SYN", "BUY", 100, 0.0, None, None),  # duplicate id
            ("O2", "SYN", "buy", 100, 0.0, None, None),  # invalid side (case-sensitive)
            ("O3", "SYN", "SELL", -5, 0.0, None, None),  # negative quantity
            ("O4", "SYN", "SELL", 5, 0.0, 0.0, None),  # zero horizon
        ]
    )
    with pytest.raises(DataValidationError) as info:
        validate_orders(o)
    by_rule = {i.rule: i.count for i in info.value.issues}
    assert by_rule["duplicate order_id"] == 2
    assert by_rule["side must be BUY or SELL"] == 1
    assert by_rule["quantity must be > 0"] == 2
    assert by_rule["execution_horizon_seconds must be a positive finite number when provided"] == 1


def test_fill_rules() -> None:
    o = valid_orders()
    f = fills_frame(
        [
            ("F1", "O1", 12.0, 0, 10.0),  # zero quantity
            ("F2", "O1", 12.0, -5, 10.0),  # negative quantity
            ("F3", "O9", 12.0, 5, 10.0),  # unknown order
            ("F4", "O1", 5.0, 5, 10.0),  # before arrival (O1 arrives at t=10)
            ("F5", "O1", 12.0, 5, 0.0),  # zero price
            ("F5", "O1", 13.0, 5, 10.0),  # duplicate fill id
        ]
    )
    with pytest.raises(DataValidationError) as info:
        validate_fills(f, o)
    by_rule = {i.rule: i.count for i in info.value.issues}
    assert by_rule["quantity must be > 0"] == 2
    assert by_rule["fill references an unknown order_id"] == 1
    assert by_rule["fill timestamp precedes the order's arrival_timestamp"] == 1
    assert by_rule["price must be a positive finite number"] == 1
    assert by_rule["duplicate fill_id"] == 2


def test_fill_at_exact_arrival_time_is_allowed() -> None:
    validate_fills(fills_frame([("F1", "O1", 10.0, 5, 10.0)]), valid_orders())


def test_overfill_is_rejected_not_silently_accepted() -> None:
    o = valid_orders()  # O1 quantity 100
    f = fills_frame([("F1", "O1", 12.0, 60, 10.0), ("F2", "O1", 13.0, 41, 10.0)])
    with pytest.raises(DataValidationError, match="exceeds the parent order quantity") as info:
        validate_fills(f, o)
    assert "O1 (filled 101 > ordered 100)" in info.value.issues[-1].examples


def test_exactly_filled_order_is_valid() -> None:
    f = fills_frame([("F1", "O1", 12.0, 60, 10.0), ("F2", "O1", 13.0, 40, 10.0)])
    assert validate_fills(f, valid_orders()).height == 2


def test_issues_are_collected_across_tables() -> None:
    q = quotes_frame([(0, "SYN", 10.02, 10.00, 100, 100)])
    t = trades_frame([(0, "SYN", 10.0, 0)])
    o = valid_orders()
    f = fills_frame([("F1", "O9", 12.0, 1, 10.0)])
    with pytest.raises(DataValidationError) as info:
        validate_tables(q, t, o, f)
    assert {i.table for i in info.value.issues} == {"quotes", "trades", "fills"}
    assert "3 data validation issue(s)" in str(info.value)


def test_null_required_values_are_rejected() -> None:
    q = quotes_frame([(0, "SYN", 9.99, 10.01, 100, 100)]).with_columns(
        pl.lit(None, dtype=pl.Float64).alias("bid_price")
    )
    with pytest.raises(DataValidationError, match="null values in required column 'bid_price'"):
        validate_quotes(q)


def test_execution_dataset_validated_constructor() -> None:
    ds = ExecutionDataset.validated(
        quotes=quotes_frame([(0, "AAA", 9.99, 10.01, 100, 100)]),
        trades=trades_frame([(1, "AAA", 10.0, 10)]),
        orders=orders_frame([("O1", "BBB", "BUY", 10, 0.0, None, None)]),
        fills=empty(FILL_SCHEMA),
        metadata={"source": "test"},
    )
    assert ds.symbols == ["AAA", "BBB"]
    assert ds.metadata == {"source": "test"}


# The record-level (Pydantic) and frame-level (Polars) validators must agree.
UTC_TS = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
INVALID_QUOTES = [
    (10.02, 10.00, 100, 100),
    (0.0, 10.00, 100, 100),
    (9.99, 10.01, -1, 100),
]


@pytest.mark.parametrize(("bid", "ask", "bid_size", "ask_size"), INVALID_QUOTES)
def test_record_and_frame_validators_agree_on_quotes(
    bid: float, ask: float, bid_size: int, ask_size: int
) -> None:
    with pytest.raises(ValidationError):
        Quote(
            timestamp=UTC_TS,
            symbol="SYN",
            bid_price=bid,
            ask_price=ask,
            bid_size=bid_size,
            ask_size=ask_size,
        )
    with pytest.raises(DataValidationError):
        validate_quotes(quotes_frame([(0, "SYN", bid, ask, bid_size, ask_size)]))


@pytest.mark.parametrize("quantity", [0, -1])
def test_record_and_frame_validators_agree_on_quantities(quantity: int) -> None:
    with pytest.raises(ValidationError):
        Order(order_id="O1", symbol="S", side="BUY", quantity=quantity, arrival_timestamp=UTC_TS)
    with pytest.raises(DataValidationError):
        validate_orders(orders_frame([("O1", "S", "BUY", quantity, 0.0, None, None)]))
    with pytest.raises(ValidationError):
        Fill(fill_id="F1", order_id="O1", timestamp=UTC_TS, quantity=quantity, price=1.0)
    with pytest.raises(DataValidationError):
        validate_fills(
            fills_frame([("F1", "O1", 0.0, quantity, 1.0)]),
            orders_frame([("O1", "S", "BUY", 10, 0.0, None, None)]),
        )
