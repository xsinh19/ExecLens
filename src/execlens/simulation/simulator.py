"""Run execution strategies against point-in-time market data.

For each decision time of a strategy's plan the simulator:

1. builds a :class:`~execlens.simulation.base.MarketSnapshot` containing only
   data stamped at or before that time;
2. asks the strategy for a child quantity (capped by what is left, so a
   parent is never over-executed);
3. skips the decision if there is no fresh quote (no trading on stale or
   missing prices);
4. otherwise fills the whole clip at the price given by the impact model.

Everything is deterministic: the same market, orders and configuration
always produce the same fills.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from execlens.config import DEFAULT_STALE_QUOTE_TOLERANCE_SECONDS
from execlens.data.schemas import FILL_SCHEMA
from execlens.simulation.base import ExecutionStrategy, MarketView, ParentOrder
from execlens.simulation.immediate import ImmediateStrategy
from execlens.simulation.impact import ImpactModel
from execlens.simulation.pov import PovStrategy
from execlens.simulation.twap import TwapStrategy
from execlens.simulation.vwap import OracleVwapBenchmark, VolumeProfile, VwapStrategy


class SimulationConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    slice_seconds: float = Field(default=30.0, ge=1, description="Decision interval (s).")
    pov_participation_rate: float = Field(default=0.10, gt=0, le=1)
    stale_quote_tolerance_seconds: float = Field(
        default=DEFAULT_STALE_QUOTE_TOLERANCE_SECONDS, gt=0
    )
    impact: ImpactModel = Field(default_factory=ImpactModel)
    profile_bucket_seconds: int = Field(default=300, gt=0)
    profile_sessions: int = Field(default=20, gt=0, le=250)


@dataclass(frozen=True)
class SimulatedFill:
    fill_id: str
    order_id: str
    timestamp_us: int
    quantity: int
    price: float


@dataclass(frozen=True)
class SimulatedExecution:
    order: ParentOrder
    strategy: str
    fills: tuple[SimulatedFill, ...]
    decisions: int
    skipped_without_quote: int

    @property
    def executed_quantity(self) -> int:
        return sum(f.quantity for f in self.fills)


def simulate_order(
    order: ParentOrder,
    strategy: ExecutionStrategy,
    market: MarketView,
    config: SimulationConfig,
) -> SimulatedExecution:
    plan = strategy.plan(order)
    fills: list[SimulatedFill] = []
    executed = 0
    skipped = 0
    for step, t_us in enumerate(plan.decision_times_us):
        remaining = order.quantity - executed
        if remaining <= 0:
            break
        snapshot = market.snapshot(t_us, order.arrival_us)
        clip = min(strategy.child_quantity(plan, step, snapshot, executed), remaining)
        if clip <= 0:
            continue
        if not snapshot.has_fresh_quote(config.stale_quote_tolerance_seconds):
            skipped += 1
            continue
        fills.append(
            SimulatedFill(
                fill_id=f"{order.order_id}-{step:04d}",
                order_id=order.order_id,
                timestamp_us=t_us,
                quantity=clip,
                price=config.impact.execution_price(order.side, clip, snapshot),
            )
        )
        executed += clip
    return SimulatedExecution(
        order=order,
        strategy=strategy.name,
        fills=tuple(fills),
        decisions=len(plan.decision_times_us),
        skipped_without_quote=skipped,
    )


def fills_to_frame(executions: Iterable[SimulatedExecution]) -> pl.DataFrame:
    """Canonical fills table from simulated executions."""
    fills = [f for execution in executions for f in execution.fills]
    frame = pl.DataFrame(
        {
            "fill_id": [f.fill_id for f in fills],
            "order_id": [f.order_id for f in fills],
            "timestamp": [f.timestamp_us for f in fills],
            "quantity": [f.quantity for f in fills],
            "price": [f.price for f in fills],
        },
        schema={
            "fill_id": pl.String,
            "order_id": pl.String,
            "timestamp": pl.Int64,
            "quantity": pl.Int64,
            "price": pl.Float64,
        },
    )
    return frame.with_columns(
        pl.from_epoch("timestamp", time_unit="us").dt.replace_time_zone("UTC")
    ).select(list(FILL_SCHEMA))


def build_strategies(
    config: SimulationConfig,
    *,
    profiles: Mapping[str, VolumeProfile] | None = None,
    hindsight_markets: Mapping[str, MarketView] | None = None,
) -> dict[str, ExecutionStrategy]:
    """Strategy instances keyed by label.

    VWAP needs historical volume profiles; the hindsight ORACLE_VWAP benchmark
    is only created when explicitly given hindsight market access.
    """
    strategies: dict[str, ExecutionStrategy] = {
        ImmediateStrategy.name: ImmediateStrategy(),
        TwapStrategy.name: TwapStrategy(config.slice_seconds),
        PovStrategy.name: PovStrategy(config.pov_participation_rate, config.slice_seconds),
    }
    if profiles is not None:
        strategies[VwapStrategy.name] = VwapStrategy(profiles, config.slice_seconds)
    if hindsight_markets is not None:
        strategies[OracleVwapBenchmark.name] = OracleVwapBenchmark(
            hindsight_markets, config.slice_seconds
        )
    return strategies


def market_views(
    quotes: pl.DataFrame, trades: pl.DataFrame, config: SimulationConfig
) -> dict[str, MarketView]:
    return MarketView.by_symbol(
        quotes, trades, trailing_window_seconds=config.impact.reference_window_seconds
    )


def simulate_orders(
    orders: pl.DataFrame,
    markets: Mapping[str, MarketView],
    strategies: Mapping[str, ExecutionStrategy],
    config: SimulationConfig,
) -> list[SimulatedExecution]:
    """Simulate every order with the strategy named in its ``strategy`` column."""
    executions: list[SimulatedExecution] = []
    for row in orders.iter_rows(named=True):
        label = row["strategy"]
        if label not in strategies:
            raise ValueError(
                f"order {row['order_id']}: unknown strategy {label!r}; "
                f"available: {sorted(strategies)}"
            )
        market = markets.get(row["symbol"])
        if market is None:
            raise ValueError(f"order {row['order_id']}: no market data for {row['symbol']!r}")
        executions.append(
            simulate_order(ParentOrder.from_row(row), strategies[label], market, config)
        )
    return executions
