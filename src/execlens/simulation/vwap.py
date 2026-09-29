"""VWAP execution from a historical volume profile, and a hindsight oracle.

The implementable :class:`VwapStrategy` allocates the parent quantity in
proportion to the volume it *expects* in each slice. The expectation comes
from a :class:`VolumeProfile` built from prior sessions only (see
:func:`execlens.data.synthetic.historical_volume_profile`), never from the
session being traded, so its schedule is fixed at arrival and cannot react to
same-session realised volume it has not seen.

:class:`OracleVwapBenchmark` is **not a strategy anyone could run**: it sizes
slices with the *realised* volume of the session, including volume that
occurs after the decision time. It exists only to measure how much perfect
foresight of volume would be worth, and is labelled ``implementable=False``
and named ``ORACLE_VWAP`` everywhere it appears.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time
from itertools import accumulate, pairwise

import numpy as np

from execlens.data.schemas import MICROS, to_epoch_us
from execlens.data.synthetic import SESSION_OPEN_UTC, Instrument, historical_volume_profile
from execlens.simulation.base import (
    ExecutionStrategy,
    MarketView,
    OrderPlan,
    ParentOrder,
    allocate,
    slice_boundaries,
)


@dataclass(frozen=True)
class VolumeProfile:
    """Expected share of daily volume per time-of-day bucket (UTC).

    ``fractions[j]`` is the expected fraction of the session's volume traded
    in bucket ``j``, i.e. between ``session_open + j * bucket_seconds`` and the
    next bucket; within a bucket volume is assumed uniform.
    """

    session_open: time
    bucket_seconds: int
    fractions: tuple[float, ...]

    def __post_init__(self) -> None:
        if self.bucket_seconds <= 0 or not self.fractions:
            raise ValueError("profile needs a positive bucket size and at least one bucket")
        if not all(math.isfinite(f) and f >= 0 for f in self.fractions):
            raise ValueError("profile fractions must be finite and non-negative")
        if abs(sum(self.fractions) - 1.0) > 1e-9:
            raise ValueError("profile fractions must sum to 1")

    @classmethod
    def from_history(
        cls,
        instrument: Instrument,
        *,
        session_seconds: int,
        bucket_seconds: int = 300,
        n_sessions: int = 20,
        seed: int = 7,
    ) -> VolumeProfile:
        fractions = historical_volume_profile(
            instrument,
            session_seconds=session_seconds,
            bucket_seconds=bucket_seconds,
            n_sessions=n_sessions,
            seed=seed,
        )
        return cls(SESSION_OPEN_UTC, bucket_seconds, tuple(float(f) for f in fractions))

    def slice_fractions(self, bounds_us: Sequence[int]) -> list[float]:
        """Expected fraction of daily volume in each window ``[bounds[k], bounds[k+1])``.

        Times are measured from the session open on the UTC date of the first
        boundary; windows outside the session expect no volume.
        """
        first = datetime.fromtimestamp(bounds_us[0] / MICROS, tz=UTC)
        open_us = to_epoch_us(datetime.combine(first.date(), self.session_open, tzinfo=UTC))
        offsets = (np.asarray(bounds_us, dtype=np.int64) - open_us) / MICROS
        lo, hi = offsets[:-1, None], offsets[1:, None]
        edges = np.arange(len(self.fractions) + 1, dtype=np.float64) * self.bucket_seconds
        overlap = np.clip(np.minimum(edges[1:], hi) - np.maximum(edges[:-1], lo), 0.0, None)
        density = np.asarray(self.fractions) / self.bucket_seconds
        fractions: list[float] = (overlap * density).sum(axis=1).tolist()
        return fractions


def _schedule_plan(order: ParentOrder, starts: list[int], weights: list[float]) -> OrderPlan:
    if sum(weights) <= 0:
        weights = [1.0] * len(starts)  # no expected volume at all: fall back to time slicing
    schedule = allocate(order.quantity, weights)
    return OrderPlan(decision_times_us=tuple(starts), cumulative_target=tuple(accumulate(schedule)))


class VwapStrategy(ExecutionStrategy):
    """Volume-Weighted Average Price execution driven by an *expected* profile.

    Slice ``k`` covering ``[b_k, b_{k+1})`` receives a share of the parent
    order proportional to the profile's expected volume in that window. Like
    TWAP, it trades up to the cumulative target at the start of each slice
    and catches up after skipped slices.
    """

    name = "VWAP"

    def __init__(self, profiles: Mapping[str, VolumeProfile], slice_seconds: float = 30.0) -> None:
        self.profiles = dict(profiles)
        self.slice_seconds = slice_seconds

    def slice_weights(self, order: ParentOrder) -> list[float]:
        profile = self.profiles.get(order.symbol)
        if profile is None:
            raise KeyError(f"no historical volume profile for symbol {order.symbol!r}")
        return profile.slice_fractions(slice_boundaries(order, self.slice_seconds))

    def plan(self, order: ParentOrder) -> OrderPlan:
        starts = slice_boundaries(order, self.slice_seconds)[:-1]
        return _schedule_plan(order, starts, self.slice_weights(order))


class OracleVwapBenchmark(ExecutionStrategy):
    """HINDSIGHT BENCHMARK - NOT IMPLEMENTABLE.

    Sizes each slice by the volume that was actually realised in it later in
    the same session. Real traders cannot know this; the result is only an
    upper-bound reference for how well a VWAP schedule could track volume.
    """

    name = "ORACLE_VWAP"
    implementable = False

    def __init__(
        self, hindsight_markets: Mapping[str, MarketView], slice_seconds: float = 30.0
    ) -> None:
        self.hindsight_markets = dict(hindsight_markets)
        self.slice_seconds = slice_seconds

    def plan(self, order: ParentOrder) -> OrderPlan:
        market = self.hindsight_markets[order.symbol]
        bounds = slice_boundaries(order, self.slice_seconds)
        # Realised volume in [b_k, b_{k+1}): closed interval ending 1 microsecond early.
        realised = [float(market.volume_between(a, b - 1)) for a, b in pairwise(bounds)]
        return _schedule_plan(order, bounds[:-1], realised)
