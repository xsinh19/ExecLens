"""Pydantic request/response models of the ExecLens HTTP API.

Sign conventions are part of the contract and are repeated in every field
description (and therefore in the OpenAPI docs):

* cost metrics (``*_cost_bps``, ``effective_spread_bps``): positive = WORSE
  execution for the order;
* markouts (``markout_bps``, ``mid_markout_bps``): positive = FAVOURABLE
  subsequent move for the order - the opposite orientation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from execlens.data.schemas import Fill, Side
from execlens.tca.aggregation import TcaSummary

COST = "Basis points. Positive = worse execution for the order, negative = better."
MARKOUT = (
    "Basis points. Positive = FAVOURABLE subsequent mid move for the order "
    "(opposite orientation to the cost metrics)."
)

ScenarioName = Literal[
    "normal", "high_volatility", "low_liquidity", "wide_spread", "trend_up", "trend_down"
]
StrategyName = Literal["IMMEDIATE", "TWAP", "VWAP", "POV"]


def _all_strategies() -> list[StrategyName]:
    return ["IMMEDIATE", "TWAP", "VWAP", "POV"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class ErrorResponse(_Model):
    detail: str


class DatasetInfo(_Model):
    name: str = Field(description="Name of the dataset directory (server paths are not exposed).")
    synthetic: bool
    symbols: list[str]
    quotes: int
    trades: int
    orders: int
    fills: int


class HealthResponse(_Model):
    status: Literal["ok"] = "ok"
    version: str
    dataset: DatasetInfo


class OrderSummary(_Model):
    order_id: str
    symbol: str
    side: Side
    strategy: str | None
    quantity: int
    executed_quantity: int
    completion_ratio: float
    arrival_timestamp: datetime
    arrival_cost_bps: float | None = Field(description=COST)
    vwap_cost_bps: float | None = Field(description=COST)
    is_flagged: bool
    flags: list[str]


class OrderListResponse(_Model):
    items: list[OrderSummary]
    total: int = Field(description="Orders matching the filters (before pagination).")
    limit: int
    offset: int


class OrderDetail(_Model):
    order_id: str
    symbol: str
    side: Side
    strategy: str | None
    quantity: int
    arrival_timestamp: datetime
    execution_horizon_seconds: float | None
    executed_quantity: int
    completion_ratio: float
    n_fills: int
    avg_fill_price: float | None
    first_fill_timestamp: datetime | None
    last_fill_timestamp: datetime | None
    fills: list[Fill]


class QuoteContext(_Model):
    status: Literal["OK", "STALE", "MISSING"] = Field(
        description="OK = fresh quote at or before the event; STALE/MISSING = prices withheld."
    )
    quote_timestamp: datetime | None
    quote_age_seconds: float | None
    bid: float | None
    ask: float | None
    mid: float | None
    spread_bps: float | None


class ExecutionStats(_Model):
    requested_quantity: int
    executed_quantity: int
    completion_ratio: float
    n_fills: int
    avg_fill_price: float | None
    executed_notional: float
    first_fill_timestamp: datetime | None
    last_fill_timestamp: datetime | None
    order_duration_seconds: float | None


class BenchmarkInterval(_Model):
    start: datetime
    end: datetime | None = Field(
        description="arrival + horizon, extended to the last fill if executions ran past it."
    )
    market_volume: int | None
    market_vwap: float | None
    participation_rate: float | None = Field(description="executed / market volume in interval")
    quantity_to_market_volume: float | None
    execution_window_volume: int | None = Field(
        description="Market volume in [arrival, last fill]."
    )
    execution_participation_rate: float | None = Field(
        description="executed / market volume while the order was trading"
    )


class Costs(_Model):
    arrival_cost_bps: float | None = Field(description=COST)
    vwap_cost_bps: float | None = Field(description=COST)
    effective_spread_bps: float | None = Field(description=COST)
    avg_contemporaneous_spread_bps: float | None = Field(
        description="Quantity-weighted quoted spread at the fills (bps)."
    )


class Markout(_Model):
    horizon_seconds: int
    markout_bps: float | None = Field(description=MARKOUT + " Measured from the fill price.")
    mid_markout_bps: float | None = Field(description=MARKOUT + " Measured from the mid at fill.")
    unavailable_fills: int = Field(description="Fills without an observation at t + h.")


class Diagnostics(_Model):
    is_flagged: bool
    flags: list[str]
    flag_reasons: list[str]
    context_flags: list[str]
    context_notes: list[str]
    pre_arrival_volatility_bps: float | None
    pre_arrival_momentum_bps: float | None = Field(
        description="Side-adjusted pre-arrival mid move; positive = adverse for the order."
    )
    interval_price_move_bps: float | None = Field(
        description="Side-adjusted mid move over the interval; positive = adverse."
    )
    market_volume_per_minute: float | None


class FillTca(_Model):
    fill_id: str
    timestamp: datetime
    quantity: int
    price: float
    quote_status: Literal["OK", "STALE", "MISSING"]
    mid: float | None
    effective_spread_bps: float | None = Field(description=COST)
    markouts_bps: dict[str, float | None] = Field(description=MARKOUT + " Keyed by horizon.")


class OrderTca(_Model):
    order_id: str
    symbol: str
    side: Side
    strategy: str | None
    arrival: QuoteContext
    execution: ExecutionStats
    benchmark: BenchmarkInterval
    costs: Costs
    markouts: list[Markout]
    diagnostics: Diagnostics
    n_fills_without_quote: int
    fills: list[FillTca]


class GroupStats(_Model):
    group: str
    orders: int
    orders_with_arrival_cost: int
    median_arrival_cost_bps: float | None = Field(description=COST)
    mean_arrival_cost_bps: float | None = Field(description=COST)
    mean_vwap_cost_bps: float | None = Field(description=COST)
    mean_effective_spread_bps: float | None = Field(description=COST)
    mean_completion_ratio: float | None
    flagged_fraction: float | None


class FlagCount(_Model):
    flag: str
    category: Literal["execution", "context", "data_quality"]
    description: str
    orders: int


class SummaryResponse(_Model):
    overall: TcaSummary
    by_strategy: list[GroupStats]
    by_symbol: list[GroupStats]
    flags: list[FlagCount]
    note: str = (
        "Synthetic data and simulated executions. Cost metrics: positive = worse. "
        "Markouts: positive = favourable."
    )


class SimulationRequest(_Model):
    """Controlled comparison of execution strategies on one synthetic scenario.

    Besides the per-field limits, a request must fit the simulation budget:
    ``trials x (horizon + 601 s)`` simulated market seconds and
    ``trials x strategies x horizon / slice_seconds`` decisions are capped
    (422 with the exact figures otherwise).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario: ScenarioName = "normal"
    side: Side = Side.BUY
    quantity: int = Field(default=20_000, gt=0, le=5_000_000)
    horizon_seconds: int = Field(default=1_800, ge=60, le=14_400)
    strategies: list[StrategyName] = Field(default_factory=_all_strategies, min_length=1)
    include_oracle: bool = Field(
        default=False,
        description="Add ORACLE_VWAP: a hindsight benchmark using realised volume. "
        "Not implementable; reported separately.",
    )
    participation_rate: float = Field(default=0.10, gt=0, le=0.5, description="POV rate.")
    slice_seconds: float = Field(default=30.0, ge=5, le=600)
    impact_coefficient_bps: float = Field(default=25.0, ge=0, le=500)
    trials: int = Field(default=20, ge=1, le=50)
    seed: int = Field(default=7, ge=0, le=2**31 - 1)


class StrategyResult(_Model):
    strategy: str
    implementable: bool
    trials: int
    mean_arrival_cost_bps: float | None = Field(description=COST)
    median_arrival_cost_bps: float | None = Field(description=COST)
    std_arrival_cost_bps: float | None
    arrival_cost_ci95_bps: float | None = Field(description="Half-width of a 95% CI of the mean.")
    mean_vwap_cost_bps: float | None = Field(description=COST)
    mean_effective_spread_bps: float | None = Field(description=COST)
    mean_markouts_bps: dict[str, float | None] = Field(description=MARKOUT)
    mean_completion_ratio: float | None
    mean_duration_seconds: float | None
    mean_participation_rate: float | None = Field(
        description="Executed / market volume over the common benchmark interval."
    )
    mean_execution_participation_rate: float | None = Field(
        description="Executed / market volume while the order was trading "
        "([arrival, last fill]); undefined for instantaneous executions."
    )
    mean_fills: float | None


class SimulationResponse(_Model):
    simulation_id: str
    created_at: datetime
    request: SimulationRequest
    results: list[StrategyResult]
    conclusions: list[str]
    disclaimer: str = (
        "Simulated executions on a synthetic market with a simplified impact model; "
        "results are scenario-specific and say nothing about real trading performance."
    )
