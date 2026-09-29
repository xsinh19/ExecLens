"""Immediate execution: send the whole parent order at arrival."""

from __future__ import annotations

from execlens.simulation.base import ExecutionStrategy, OrderPlan, ParentOrder


class ImmediateStrategy(ExecutionStrategy):
    """One marketable clip for the full quantity at the arrival time.

    A BUY starts from the ask and a SELL from the bid; the fill model adds
    impact for the clip size. If no fresh quote exists at arrival, nothing is
    executed (the order is reported as unfilled rather than executed on stale
    prices).
    """

    name = "IMMEDIATE"

    def plan(self, order: ParentOrder) -> OrderPlan:
        return OrderPlan(decision_times_us=(order.arrival_us,), cumulative_target=(order.quantity,))
