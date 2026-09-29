"""Canonical ExecLens data model.

Two representations of the same four record types are defined here:

* Polars column schemas (``QUOTE_SCHEMA`` ...) used for bulk, vectorised
  processing. Frame-level validation lives in :mod:`execlens.data.validation`.
* Pydantic models (:class:`Quote` ...) used at external boundaries (API
  payloads, simulator inputs/outputs) where records arrive one at a time.

Both enforce the same rules; ``tests/unit/test_validation.py`` checks that they
agree on the same invalid inputs.

Conventions
-----------
* Timestamps are timezone-aware UTC with microsecond resolution.
* Quantities are integer shares.
* ``Side.BUY.sign == +1`` and ``Side.SELL.sign == -1``. Execution *cost*
  metrics are multiplied by this sign so that a positive cost is always worse
  for the order, whichever its direction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Self

import polars as pl
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

BPS = 10_000.0
"""Basis points per unit: 1 bp = 0.01% = 1/10,000."""

TIMESTAMP_DTYPE = pl.Datetime("us", "UTC")


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        """+1 for BUY, -1 for SELL."""
        return 1 if self is Side.BUY else -1


QUOTE_SCHEMA: dict[str, pl.DataType] = {
    "timestamp": TIMESTAMP_DTYPE,
    "symbol": pl.String(),
    "bid_price": pl.Float64(),
    "ask_price": pl.Float64(),
    "bid_size": pl.Int64(),
    "ask_size": pl.Int64(),
}

TRADE_SCHEMA: dict[str, pl.DataType] = {
    "timestamp": TIMESTAMP_DTYPE,
    "symbol": pl.String(),
    "price": pl.Float64(),
    "size": pl.Int64(),
}

ORDER_SCHEMA: dict[str, pl.DataType] = {
    "order_id": pl.String(),
    "symbol": pl.String(),
    "side": pl.String(),
    "quantity": pl.Int64(),
    "arrival_timestamp": TIMESTAMP_DTYPE,
    "execution_horizon_seconds": pl.Float64(),
    "strategy": pl.String(),
}

FILL_SCHEMA: dict[str, pl.DataType] = {
    "fill_id": pl.String(),
    "order_id": pl.String(),
    "timestamp": TIMESTAMP_DTYPE,
    "quantity": pl.Int64(),
    "price": pl.Float64(),
}

# Columns that may legitimately be null.
NULLABLE_COLUMNS: dict[str, frozenset[str]] = {
    "quotes": frozenset(),
    "trades": frozenset(),
    "orders": frozenset({"execution_horizon_seconds", "strategy"}),
    "fills": frozenset(),
}


def _require_utc(value: datetime) -> datetime:
    """Normalise an aware datetime to UTC (naive values are rejected by the type)."""
    return value.astimezone(UTC)


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Quote(_Record):
    """Top-of-book (Level-1) quote."""

    timestamp: AwareDatetime
    symbol: str = Field(min_length=1)
    bid_price: float = Field(gt=0, allow_inf_nan=False)
    ask_price: float = Field(gt=0, allow_inf_nan=False)
    bid_size: int = Field(ge=0)
    ask_size: int = Field(ge=0)

    _utc = field_validator("timestamp")(_require_utc)

    @model_validator(mode="after")
    def _not_crossed(self) -> Self:
        if self.bid_price > self.ask_price:
            raise ValueError("crossed market: bid_price must be <= ask_price")
        return self

    @property
    def mid(self) -> float:
        return (self.bid_price + self.ask_price) / 2.0

    @property
    def spread(self) -> float:
        return self.ask_price - self.bid_price

    @property
    def spread_bps(self) -> float:
        return self.spread / self.mid * BPS


class MarketTrade(_Record):
    """A print on the market tape (anyone's trade, not necessarily ours)."""

    timestamp: AwareDatetime
    symbol: str = Field(min_length=1)
    price: float = Field(gt=0, allow_inf_nan=False)
    size: int = Field(gt=0)

    _utc = field_validator("timestamp")(_require_utc)


class Order(_Record):
    """A parent order submitted for execution."""

    order_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: Side
    quantity: int = Field(gt=0)
    arrival_timestamp: AwareDatetime
    execution_horizon_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    strategy: str | None = None

    _utc = field_validator("arrival_timestamp")(_require_utc)


class Fill(_Record):
    """An execution (child fill) belonging to a parent order."""

    fill_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    timestamp: AwareDatetime
    quantity: int = Field(gt=0)
    price: float = Field(gt=0, allow_inf_nan=False)

    _utc = field_validator("timestamp")(_require_utc)
