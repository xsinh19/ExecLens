"""POV (percentage of volume): trade a fixed fraction of observed volume."""

from __future__ import annotations

import math

from execlens.simulation.base import (
    ExecutionStrategy,
    MarketSnapshot,
    OrderPlan,
    ParentOrder,
    slice_boundaries,
)


class PovStrategy(ExecutionStrategy):
    """Participate at ``participation_rate`` of market volume already observed.

    Decisions happen at the *end* of each slice, after its volume has been
    observed. At decision time ``t`` the cumulative target is
    ``floor(rate * V[arrival, t])`` where ``V`` is market volume stamped
    between arrival and ``t`` (inclusive), and the child order is whatever
    is needed to reach it, capped by the remaining quantity. Volume after
    ``t`` is never used, so POV lags the market by up to one slice and may
    finish the horizon with the order only partially complete.
    """

    name = "POV"

    def __init__(self, participation_rate: float = 0.10, slice_seconds: float = 30.0) -> None:
        if not 0 < participation_rate <= 1:
            raise ValueError("participation_rate must be in (0, 1]")
        self.participation_rate = participation_rate
        self.slice_seconds = slice_seconds

    def plan(self, order: ParentOrder) -> OrderPlan:
        ends = slice_boundaries(order, self.slice_seconds)[1:]
        return OrderPlan(decision_times_us=tuple(ends))

    def child_quantity(
        self, plan: OrderPlan, step: int, snapshot: MarketSnapshot, executed: int
    ) -> int:
        target = math.floor(self.participation_rate * snapshot.volume_since_arrival)
        return max(0, target - executed)
