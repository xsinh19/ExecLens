"""Building blocks of the execution simulator.

Information discipline
----------------------
Strategies never see the market data frame. At every decision time ``t`` the
simulator builds a :class:`MarketSnapshot` from :class:`MarketView` lookups
that are strictly backward-looking (quotes and trades stamped ``<= t``), and
passes only that snapshot to the strategy. A strategy therefore *cannot* use
future prices or future volume. The one deliberate exception, the hindsight
``ORACLE_VWAP`` benchmark, is constructed with explicit access to the whole
session and is labelled ``implementable = False``.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

import numpy as np
import numpy.typing as npt
import polars as pl

from execlens.data.schemas import Side

MICROS = 1_000_000


def to_epoch_us(value: datetime) -> int:
    """Exact microseconds since the Unix epoch for an aware datetime."""
    delta = value - datetime(1970, 1, 1, tzinfo=value.tzinfo)
    return (delta.days * 86_400 + delta.seconds) * MICROS + delta.microseconds


@dataclass(frozen=True)
class ParentOrder:
    order_id: str
    symbol: str
    side: Side
    quantity: int
    arrival_us: int
    horizon_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.horizon_seconds is not None and not self.horizon_seconds > 0:
            raise ValueError("horizon_seconds must be positive when provided")

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ParentOrder:
        """Build from a row of the canonical orders table."""
        return cls(
            order_id=str(row["order_id"]),
            symbol=str(row["symbol"]),
            side=Side(row["side"]),
            quantity=int(row["quantity"]),
            arrival_us=to_epoch_us(row["arrival_timestamp"]),
            horizon_seconds=row["execution_horizon_seconds"],
        )

    def require_horizon(self) -> float:
        if self.horizon_seconds is None:
            raise ValueError(f"order {self.order_id} needs an execution horizon for this strategy")
        return self.horizon_seconds


@dataclass(frozen=True)
class MarketSnapshot:
    """Everything a strategy may observe at ``timestamp_us`` (data stamped <= it)."""

    timestamp_us: int
    bid: float | None
    ask: float | None
    bid_size: int | None
    ask_size: int | None
    quote_age_seconds: float | None
    volume_since_arrival: int
    trailing_volume: int

    def has_fresh_quote(self, tolerance_seconds: float) -> bool:
        return self.quote_age_seconds is not None and self.quote_age_seconds <= tolerance_seconds


class MarketView:
    """Point-in-time access to one symbol's quotes and trades.

    Lookups use binary search over timestamp-sorted arrays. A query at time
    ``t`` only reads rows stamped ``<= t``; ``volume_between`` is the one
    method able to read beyond a decision time, and only the simulator and
    the hindsight oracle call it.
    """

    def __init__(
        self,
        quotes: pl.DataFrame,
        trades: pl.DataFrame,
        *,
        trailing_window_seconds: float = 300.0,
    ) -> None:
        q = quotes.sort("timestamp", maintain_order=True)
        t = trades.sort("timestamp", maintain_order=True)
        self.quote_us: npt.NDArray[np.int64] = q["timestamp"].dt.epoch("us").to_numpy()
        self.bid: npt.NDArray[np.float64] = q["bid_price"].to_numpy()
        self.ask: npt.NDArray[np.float64] = q["ask_price"].to_numpy()
        self.bid_size: npt.NDArray[np.int64] = q["bid_size"].to_numpy()
        self.ask_size: npt.NDArray[np.int64] = q["ask_size"].to_numpy()
        self.trade_us: npt.NDArray[np.int64] = t["timestamp"].dt.epoch("us").to_numpy()
        self.cum_volume: npt.NDArray[np.int64] = np.cumsum(t["size"].to_numpy())
        self.trailing_window_us = round(trailing_window_seconds * MICROS)

    @classmethod
    def by_symbol(
        cls,
        quotes: pl.DataFrame,
        trades: pl.DataFrame,
        *,
        trailing_window_seconds: float = 300.0,
    ) -> dict[str, MarketView]:
        quote_parts = quotes.partition_by("symbol", as_dict=True)
        trade_parts = trades.partition_by("symbol", as_dict=True)
        empty_trades = trades.clear()
        return {
            str(key[0]): cls(
                part,
                trade_parts.get(key, empty_trades),
                trailing_window_seconds=trailing_window_seconds,
            )
            for key, part in quote_parts.items()
        }

    def volume_until(self, t_us: int) -> int:
        """Total market volume stamped <= t."""
        idx = int(np.searchsorted(self.trade_us, t_us, side="right"))
        return int(self.cum_volume[idx - 1]) if idx > 0 else 0

    def volume_before(self, t_us: int) -> int:
        """Total market volume stamped < t."""
        idx = int(np.searchsorted(self.trade_us, t_us, side="left"))
        return int(self.cum_volume[idx - 1]) if idx > 0 else 0

    def volume_between(self, start_us: int, end_us: int) -> int:
        """Market volume in the closed interval [start, end]."""
        return max(0, self.volume_until(end_us) - self.volume_before(start_us))

    def snapshot(self, t_us: int, arrival_us: int) -> MarketSnapshot:
        idx = int(np.searchsorted(self.quote_us, t_us, side="right")) - 1
        through_now = self.volume_until(t_us)
        observed = through_now - self.volume_before(arrival_us) if t_us >= arrival_us else 0
        trailing = through_now - self.volume_until(t_us - self.trailing_window_us)
        if idx < 0:
            return MarketSnapshot(t_us, None, None, None, None, None, observed, trailing)
        return MarketSnapshot(
            timestamp_us=t_us,
            bid=float(self.bid[idx]),
            ask=float(self.ask[idx]),
            bid_size=int(self.bid_size[idx]),
            ask_size=int(self.ask_size[idx]),
            quote_age_seconds=(t_us - int(self.quote_us[idx])) / MICROS,
            volume_since_arrival=observed,
            trailing_volume=trailing,
        )


def allocate(quantity: int, weights: Sequence[float]) -> list[int]:
    """Split an integer quantity in proportion to ``weights``.

    Largest-remainder rounding: every slice gets ``floor(quantity * w)`` and
    the leftover shares go one each to the slices with the largest fractional
    parts (ties to the earlier slice). The result always sums exactly to
    ``quantity``, is never negative and is fully deterministic.
    """
    if quantity < 0:
        raise ValueError("quantity must be non-negative")
    if not weights or any(w < 0 or not math.isfinite(w) for w in weights):
        raise ValueError("weights must be finite and non-negative")
    total = float(sum(weights))
    if total <= 0:
        raise ValueError("weights must have a positive sum")
    raw = [quantity * w / total for w in weights]
    shares = [math.floor(r) for r in raw]
    leftover = quantity - sum(shares)
    by_remainder = sorted(range(len(raw)), key=lambda i: (-(raw[i] - shares[i]), i))
    for i in by_remainder[:leftover]:
        shares[i] += 1
    return shares


def slice_boundaries(order: ParentOrder, slice_seconds: float) -> list[int]:
    """Boundaries ``arrival = b0 < b1 < ... < bN = arrival + horizon`` in microseconds."""
    if not slice_seconds > 0:
        raise ValueError("slice_seconds must be positive")
    horizon = order.require_horizon()
    n_slices = max(1, math.ceil(horizon / slice_seconds - 1e-9))
    return [
        order.arrival_us + round(min(k * slice_seconds, horizon) * MICROS)
        for k in range(n_slices + 1)
    ]


@dataclass(frozen=True)
class OrderPlan:
    """What a strategy decides at arrival: when to act and the cumulative target.

    ``cumulative_target[k]`` is the number of shares the strategy wants done
    after decision ``k``. Reactive strategies (POV) leave it empty and size
    each child from the snapshot instead.
    """

    decision_times_us: tuple[int, ...]
    cumulative_target: tuple[int, ...] = ()


class ExecutionStrategy(ABC):
    """Common interface of every execution algorithm."""

    name: ClassVar[str]
    implementable: ClassVar[bool] = True

    @abstractmethod
    def plan(self, order: ParentOrder) -> OrderPlan:
        """Decide the schedule at arrival, from the order and prior information only."""

    def child_quantity(
        self, plan: OrderPlan, step: int, snapshot: MarketSnapshot, executed: int
    ) -> int:
        """Shares to send now; default: catch up to the planned cumulative target."""
        return max(0, plan.cumulative_target[step] - executed)
