"""Vectorised validation of the canonical ExecLens tables.

Every rule from the data model is evaluated as a Polars expression over the
whole frame, and all violations are collected into one
:class:`DataValidationError` that names the rule, the number of offending rows
and a few examples. Invalid data is rejected, never silently repaired: the
only normalisation performed is lossless (timezone conversion to UTC,
microsecond precision, widening numeric casts, column order).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from execlens.data.schemas import (
    FILL_SCHEMA,
    NULLABLE_COLUMNS,
    ORDER_SCHEMA,
    QUOTE_SCHEMA,
    TRADE_SCHEMA,
    Side,
)

MAX_EXAMPLES = 5

SCHEMAS: dict[str, dict[str, pl.DataType]] = {
    "quotes": QUOTE_SCHEMA,
    "trades": TRADE_SCHEMA,
    "orders": ORDER_SCHEMA,
    "fills": FILL_SCHEMA,
}


@dataclass(frozen=True)
class ValidationIssue:
    table: str
    rule: str
    count: int
    examples: tuple[str, ...] = ()

    def __str__(self) -> str:
        examples = f"; e.g. {', '.join(self.examples)}" if self.examples else ""
        return f"[{self.table}] {self.rule} ({self.count} affected){examples}"


class DataValidationError(ValueError):
    """Raised when input tables violate the ExecLens data model."""

    def __init__(self, issues: Sequence[ValidationIssue]) -> None:
        self.issues = tuple(issues)
        details = "\n".join(f"  - {issue}" for issue in self.issues)
        super().__init__(f"{len(self.issues)} data validation issue(s):\n{details}")


# ---------------------------------------------------------------------------
# Schema conformance
# ---------------------------------------------------------------------------


def _conform_column(
    df: pl.DataFrame, name: str, target: pl.DataType, table: str, issues: list[ValidationIssue]
) -> pl.Expr | None:
    source = df.schema[name]
    col = pl.col(name)
    if isinstance(target, pl.Datetime):
        if not isinstance(source, pl.Datetime):
            issues.append(
                ValidationIssue(table, f"column '{name}' must be a Datetime, got {source}", 1)
            )
            return None
        if source.time_zone is None:
            issues.append(
                ValidationIssue(
                    table, f"column '{name}' must be timezone-aware UTC (got naive timestamps)", 1
                )
            )
            return None
        return col.dt.convert_time_zone("UTC").dt.cast_time_unit("us")
    if source == target:
        return col
    if target == pl.Float64 and source.is_numeric():
        return col.cast(pl.Float64)
    if target == pl.Int64 and source.is_integer():
        return col.cast(pl.Int64)
    if target == pl.Int64 and source.is_float():
        fractional = df.filter(col.is_not_null() & (col != col.floor())).height
        if fractional:
            issues.append(
                ValidationIssue(table, f"column '{name}' must contain whole numbers", fractional)
            )
            return None
        return col.cast(pl.Int64)
    if target == pl.String and isinstance(source, pl.Categorical | pl.Enum):
        return col.cast(pl.String)
    if target == pl.String and source == pl.Null:
        return col.cast(pl.String)
    if target == pl.Float64 and source == pl.Null:
        return col.cast(pl.Float64)
    issues.append(
        ValidationIssue(table, f"column '{name}' has dtype {source}, expected {target}", 1)
    )
    return None


def _conform(
    df: pl.DataFrame, schema: Mapping[str, pl.DataType], table: str, issues: list[ValidationIssue]
) -> pl.DataFrame | None:
    missing = [name for name in schema if name not in df.columns]
    if missing:
        issues.append(
            ValidationIssue(table, f"missing required column(s): {', '.join(missing)}", 1)
        )
        return None
    exprs: list[pl.Expr] = []
    for name, target in schema.items():
        expr = _conform_column(df, name, target, table, issues)
        if expr is None:
            return None
        exprs.append(expr.alias(name))
    return df.select(exprs)


# ---------------------------------------------------------------------------
# Row rules
# ---------------------------------------------------------------------------


def _positive_finite(name: str) -> pl.Expr:
    """True where the value is NOT a positive finite number (nulls handled separately)."""
    return (pl.col(name) <= 0) | ~pl.col(name).is_finite()


def _apply_rules(
    df: pl.DataFrame,
    table: str,
    rules: Sequence[tuple[str, pl.Expr]],
    label: pl.Expr,
    issues: list[ValidationIssue],
) -> None:
    """Evaluate boolean 'row is invalid' expressions and record violations."""
    nullable = NULLABLE_COLUMNS[table]
    all_rules = [
        (f"null values in required column '{name}'", pl.col(name).is_null())
        for name in SCHEMAS[table]
        if name not in nullable
    ]
    all_rules.extend(rules)
    if df.is_empty():
        return
    masks = df.select(
        [expr.fill_null(False).alias(f"rule_{i}") for i, (_, expr) in enumerate(all_rules)]
    )
    counts = masks.sum().row(0)
    for i, (description, _) in enumerate(all_rules):
        count = int(counts[i])
        if count == 0:
            continue
        examples = (
            df.with_row_index("row")
            .filter(masks[f"rule_{i}"])
            .head(MAX_EXAMPLES)
            .select(label.alias("label"))["label"]
            .to_list()
        )
        issues.append(ValidationIssue(table, description, count, tuple(map(str, examples))))


def _market_label() -> pl.Expr:
    return pl.format(
        "row {} ({} @ {})", pl.col("row"), pl.col("symbol"), pl.col("timestamp").cast(pl.String)
    )


def _check_quotes(df: pl.DataFrame, issues: list[ValidationIssue]) -> None:
    rules = [
        ("symbol must be non-empty", pl.col("symbol").str.len_chars() == 0),
        ("bid_price must be a positive finite number", _positive_finite("bid_price")),
        ("ask_price must be a positive finite number", _positive_finite("ask_price")),
        ("bid_size must be >= 0", pl.col("bid_size") < 0),
        ("ask_size must be >= 0", pl.col("ask_size") < 0),
        (
            # Polars orders NaN above every number, so restrict to finite prices;
            # non-finite prices are reported by the rules above.
            "crossed market: bid_price > ask_price",
            (pl.col("bid_price") > pl.col("ask_price"))
            & pl.col("bid_price").is_finite()
            & pl.col("ask_price").is_finite(),
        ),
    ]
    _apply_rules(df, "quotes", rules, _market_label(), issues)


def _check_trades(df: pl.DataFrame, issues: list[ValidationIssue]) -> None:
    rules = [
        ("symbol must be non-empty", pl.col("symbol").str.len_chars() == 0),
        ("price must be a positive finite number", _positive_finite("price")),
        ("size must be > 0", pl.col("size") <= 0),
    ]
    _apply_rules(df, "trades", rules, _market_label(), issues)


def _check_orders(df: pl.DataFrame, issues: list[ValidationIssue]) -> None:
    horizon = pl.col("execution_horizon_seconds")
    rules = [
        ("duplicate order_id", pl.col("order_id").is_duplicated()),
        ("symbol must be non-empty", pl.col("symbol").str.len_chars() == 0),
        ("side must be BUY or SELL", ~pl.col("side").is_in([s.value for s in Side])),
        ("quantity must be > 0", pl.col("quantity") <= 0),
        (
            "execution_horizon_seconds must be a positive finite number when provided",
            horizon.is_not_null() & ((horizon <= 0) | ~horizon.is_finite()),
        ),
    ]
    _apply_rules(df, "orders", rules, pl.col("order_id"), issues)


def _check_fills(df: pl.DataFrame, orders: pl.DataFrame, issues: list[ValidationIssue]) -> None:
    known = orders.select("order_id", "arrival_timestamp", order_quantity="quantity")
    enriched = df.join(known, on="order_id", how="left")
    rules = [
        ("duplicate fill_id", pl.col("fill_id").is_duplicated()),
        ("quantity must be > 0", pl.col("quantity") <= 0),
        ("price must be a positive finite number", _positive_finite("price")),
        ("fill references an unknown order_id", pl.col("arrival_timestamp").is_null()),
        (
            "fill timestamp precedes the order's arrival_timestamp",
            pl.col("timestamp") < pl.col("arrival_timestamp"),
        ),
    ]
    _apply_rules(enriched, "fills", rules, pl.col("fill_id"), issues)

    # Parent-level rule: fills must never exceed the parent order quantity.
    overfilled = (
        enriched.filter(pl.col("order_quantity").is_not_null())
        .group_by("order_id")
        .agg(pl.col("quantity").sum().alias("filled"), pl.col("order_quantity").first())
        .filter(pl.col("filled") > pl.col("order_quantity"))
        .sort("order_id")
    )
    if overfilled.height:
        examples = tuple(
            f"{row['order_id']} (filled {row['filled']} > ordered {row['order_quantity']})"
            for row in overfilled.head(MAX_EXAMPLES).iter_rows(named=True)
        )
        issues.append(
            ValidationIssue(
                "fills",
                "total filled quantity exceeds the parent order quantity",
                overfilled.height,
                examples,
            )
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _checked(frame: pl.DataFrame | None, issues: list[ValidationIssue]) -> pl.DataFrame:
    """Raise if any issue was recorded; otherwise return the conformed frame."""
    if issues or frame is None:
        raise DataValidationError(issues)
    return frame


def validate_quotes(df: pl.DataFrame) -> pl.DataFrame:
    issues: list[ValidationIssue] = []
    conformed = _conform(df, QUOTE_SCHEMA, "quotes", issues)
    if conformed is not None:
        _check_quotes(conformed, issues)
    return _checked(conformed, issues)


def validate_trades(df: pl.DataFrame) -> pl.DataFrame:
    issues: list[ValidationIssue] = []
    conformed = _conform(df, TRADE_SCHEMA, "trades", issues)
    if conformed is not None:
        _check_trades(conformed, issues)
    return _checked(conformed, issues)


def validate_orders(df: pl.DataFrame) -> pl.DataFrame:
    issues: list[ValidationIssue] = []
    conformed = _conform(df, ORDER_SCHEMA, "orders", issues)
    if conformed is not None:
        _check_orders(conformed, issues)
    return _checked(conformed, issues)


def validate_fills(df: pl.DataFrame, orders: pl.DataFrame) -> pl.DataFrame:
    """Validate fills against already-validated orders."""
    issues: list[ValidationIssue] = []
    conformed = _conform(df, FILL_SCHEMA, "fills", issues)
    if conformed is not None:
        _check_fills(conformed, orders, issues)
    return _checked(conformed, issues)


def validate_tables(
    quotes: pl.DataFrame, trades: pl.DataFrame, orders: pl.DataFrame, fills: pl.DataFrame
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Validate all four tables, reporting every issue across tables at once."""
    issues: list[ValidationIssue] = []
    q = _conform(quotes, QUOTE_SCHEMA, "quotes", issues)
    if q is not None:
        _check_quotes(q, issues)
    t = _conform(trades, TRADE_SCHEMA, "trades", issues)
    if t is not None:
        _check_trades(t, issues)
    o = _conform(orders, ORDER_SCHEMA, "orders", issues)
    if o is not None:
        _check_orders(o, issues)
    f = _conform(fills, FILL_SCHEMA, "fills", issues)
    if f is not None and o is not None:
        _check_fills(f, o, issues)
    return _checked(q, issues), _checked(t, issues), _checked(o, issues), _checked(f, issues)


@dataclass(frozen=True)
class ExecutionDataset:
    """The four canonical tables plus free-form provenance metadata."""

    quotes: pl.DataFrame
    trades: pl.DataFrame
    orders: pl.DataFrame
    fills: pl.DataFrame
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def validated(
        cls,
        *,
        quotes: pl.DataFrame,
        trades: pl.DataFrame,
        orders: pl.DataFrame,
        fills: pl.DataFrame,
        metadata: Mapping[str, Any] | None = None,
    ) -> ExecutionDataset:
        q, t, o, f = validate_tables(quotes, trades, orders, fills)
        return cls(quotes=q, trades=t, orders=o, fills=f, metadata=dict(metadata or {}))

    @property
    def symbols(self) -> list[str]:
        return sorted(set(self.quotes["symbol"].unique()) | set(self.orders["symbol"].unique()))
