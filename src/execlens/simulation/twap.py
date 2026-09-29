"""TWAP: spread the parent quantity evenly over time."""

from __future__ import annotations

from itertools import accumulate

from execlens.simulation.base import (
    ExecutionStrategy,
    OrderPlan,
    ParentOrder,
    allocate,
    slice_boundaries,
)


def twap_schedule(quantity: int, n_slices: int) -> list[int]:
    """Even integer split: ``quantity // n`` per slice, remainder to the earliest slices.

    Example: 1,000 shares over 3 slices -> [334, 333, 333]. Always sums to
    ``quantity``.
    """
    if n_slices <= 0:
        raise ValueError("n_slices must be positive")
    return allocate(quantity, [1.0] * n_slices)


class TwapStrategy(ExecutionStrategy):
    """Time-Weighted Average Price execution.

    The horizon is cut into slices of ``slice_seconds``; at the start of each
    slice the strategy trades up to the cumulative scheduled quantity. If a
    slice could not trade (no fresh quote), the shortfall is caught up at the
    next decision; whatever is still missing at the end stays unexecuted
    (partial completion is reported, never forced).
    """

    name = "TWAP"

    def __init__(self, slice_seconds: float = 30.0) -> None:
        self.slice_seconds = slice_seconds

    def plan(self, order: ParentOrder) -> OrderPlan:
        starts = slice_boundaries(order, self.slice_seconds)[:-1]
        schedule = twap_schedule(order.quantity, len(starts))
        return OrderPlan(
            decision_times_us=tuple(starts), cumulative_target=tuple(accumulate(schedule))
        )
