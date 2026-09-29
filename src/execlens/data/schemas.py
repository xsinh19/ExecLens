"""Canonical ExecLens data model.

* Polars column schemas (``QUOTE_SCHEMA``, ``TRADE_SCHEMA``, ``ORDER_SCHEMA``,
  ``FILL_SCHEMA``) describe the four tables and are used for all bulk,
  vectorised processing. Frame-level validation lives in
  :mod:`execlens.data.validation`.
* Pydantic models for the records that cross a boundary one at a time:
  :class:`Order` (a parent order handed to the simulator) and :class:`Fill`
  (fills returned by the API). They enforce the same rules as the frame
  validators; ``tests/unit/test_validation.py`` checks that both reject the
  same invalid inputs.

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

import polars as pl
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

BPS = 10_000.0
"""Basis points per unit: 1 bp = 0.01% = 1/10,000."""

MICROS = 1_000_000
"""Microseconds per second (timestamps have microsecond resolution)."""

TIMESTAMP_DTYPE = pl.Datetime("us", "UTC")

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def to_epoch_us(value: datetime) -> int:
    """Exact microseconds since the Unix epoch for a timezone-aware datetime.

    Integer ``timedelta`` arithmetic avoids float rounding, and the epoch is
    UTC, so a datetime in any time zone maps to the same instant.
    """
    if value.tzinfo is None:
        raise ValueError("expected a timezone-aware datetime")
    delta = value - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * MICROS + delta.microseconds


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
