"""Controlled comparison of execution strategies on identical market scenarios.

For each trial a fresh session of the chosen scenario is generated (seeded)
and one parent order arrives at a random time. **Every strategy trades the
same order in the same session**, so differences between strategies come
from the strategies, not from different markets. Results are scored with the
same TCA engine used for any other dataset.

Conclusions are computed from the results (never hard-coded) and are
specific to the scenario, parameters and number of trials; a 95% confidence
interval for mean arrival cost is reported so that small differences are not
over-read.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

import numpy as np
import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from execlens.data.schemas import ORDER_SCHEMA, Side
from execlens.data.synthetic import (
    SCENARIOS,
    TRADING_DAY_SECONDS,
    Instrument,
    MarketConfig,
    MarketData,
    generate_market,
)
from execlens.simulation.base import MarketView, ParentOrder, to_epoch_us
from execlens.simulation.simulator import (
    SimulatedExecution,
    SimulationConfig,
    build_strategies,
    fills_to_frame,
    simulate_order,
)
from execlens.simulation.vwap import OracleVwapBenchmark, VolumeProfile
from execlens.tca.engine import TcaConfig, run_tca

IMPLEMENTABLE_STRATEGIES: Final = ("IMMEDIATE", "TWAP", "VWAP", "POV")
EDGE_SECONDS: Final = 300
MARKOUT_BUFFER_SECONDS: Final = 300
_ARRIVAL_STREAM: Final = 99


class ComparisonRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario: str = "normal"
    side: Side = Side.BUY
    quantity: int = Field(default=20_000, gt=0, le=10_000_000)
    horizon_seconds: int = Field(default=1_800, ge=60, le=14_400)
    strategies: tuple[str, ...] = IMPLEMENTABLE_STRATEGIES
    include_oracle: bool = False
    trials: int = Field(default=20, ge=1, le=100)
    seed: int = Field(default=7, ge=0)
    session_seconds: int = Field(default=TRADING_DAY_SECONDS, ge=1_800, le=TRADING_DAY_SECONDS)
    simulation: SimulationConfig = Field(default_factory=SimulationConfig)

    @field_validator("scenario")
    @classmethod
    def _known_scenario(cls, value: str) -> str:
        if value not in SCENARIOS:
            raise ValueError(f"unknown scenario {value!r}; choose from {sorted(SCENARIOS)}")
        return value

    @field_validator("strategies")
    @classmethod
    def _known_strategies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("choose at least one strategy")
        unknown = set(value) - set(IMPLEMENTABLE_STRATEGIES)
        if unknown:
            raise ValueError(
                f"unknown strategies {sorted(unknown)}; choose from {IMPLEMENTABLE_STRATEGIES}"
            )
        if len(set(value)) != len(value):
            raise ValueError("strategies must be unique")
        return value

    @model_validator(mode="after")
    def _fits_in_session(self) -> ComparisonRequest:
        needed = self.horizon_seconds + 2 * EDGE_SECONDS + MARKOUT_BUFFER_SECONDS
        if needed >= self.session_seconds:
            raise ValueError(
                f"horizon too long: needs {needed} s of session, session is "
                f"{self.session_seconds} s"
            )
        return self


@dataclass(frozen=True)
class ComparisonResult:
    request: ComparisonRequest
    market: MarketData
    orders: pl.DataFrame
    fills: pl.DataFrame
    summary: pl.DataFrame
    conclusions: tuple[str, ...]


def _summary(orders: pl.DataFrame, horizons: tuple[int, ...]) -> pl.DataFrame:
    cost = pl.col("arrival_cost_bps")
    return (
        orders.group_by("strategy", "implementable")
        .agg(
            pl.len().alias("trials"),
            cost.mean().alias("mean_arrival_cost_bps"),
            cost.median().alias("median_arrival_cost_bps"),
            cost.std().alias("std_arrival_cost_bps"),
            (1.96 * cost.std() / cost.count().sqrt()).alias("arrival_cost_ci95_bps"),
            pl.col("vwap_cost_bps").mean().alias("mean_vwap_cost_bps"),
            pl.col("effective_spread_bps").mean().alias("mean_effective_spread_bps"),
            *[pl.col(f"markout_{h}s_bps").mean().alias(f"mean_markout_{h}s_bps") for h in horizons],
            pl.col("completion_ratio").mean().alias("mean_completion_ratio"),
            pl.col("order_duration_seconds").mean().alias("mean_duration_seconds"),
            pl.col("participation_rate").mean().alias("mean_participation_rate"),
            pl.col("n_fills").mean().alias("mean_fills"),
        )
        .sort(pl.col("implementable").not_(), "mean_arrival_cost_bps", nulls_last=True)
    )


def _conclusions(summary: pl.DataFrame, request: ComparisonRequest) -> tuple[str, ...]:
    """Data-driven, scenario-specific observations (no causal claims)."""
    real = summary.filter(pl.col("implementable") & pl.col("mean_arrival_cost_bps").is_not_null())
    if real.is_empty():
        return ("No strategy produced fills with valid arrival context in this run.",)
    notes: list[str] = []
    best = real.row(0, named=True)
    scope = f"{request.scenario} scenario, {request.side.value} {request.quantity:,} shares"
    notes.append(
        f"Lowest mean arrival cost ({scope}, {request.trials} trials): {best['strategy']} at "
        f"{best['mean_arrival_cost_bps']:.2f} bps "
        f"(95% CI +/- {best['arrival_cost_ci95_bps'] or 0.0:.2f} bps)."
    )
    if real.height > 1:
        runner = real.row(1, named=True)
        gap = runner["mean_arrival_cost_bps"] - best["mean_arrival_cost_bps"]
        overlap = gap <= (best["arrival_cost_ci95_bps"] or 0.0) + (
            runner["arrival_cost_ci95_bps"] or 0.0
        )
        notes.append(
            f"Next best: {runner['strategy']} (+{gap:.2f} bps); "
            + (
                "the confidence intervals overlap, so this ranking is not conclusive."
                if overlap
                else "the confidence intervals do not overlap."
            )
        )
    riskiest = real.sort("std_arrival_cost_bps", descending=True, nulls_last=True).row(
        0, named=True
    )
    if riskiest["std_arrival_cost_bps"] is not None:
        notes.append(
            f"Widest dispersion of outcomes (timing risk): {riskiest['strategy']} "
            f"(std {riskiest['std_arrival_cost_bps']:.2f} bps)."
        )
    incomplete = real.filter(pl.col("mean_completion_ratio") < 0.999)
    for row in incomplete.iter_rows(named=True):
        notes.append(
            f"{row['strategy']} completed only {row['mean_completion_ratio']:.0%} of the order "
            "on average; its cost covers executed shares only."
        )
    notes.append(
        "These results hold for this synthetic scenario and parameters only; "
        "they do not show that any strategy is generally best."
    )
    return tuple(notes)


def compare_strategies(request: ComparisonRequest) -> ComparisonResult:
    """Run every requested strategy on the same generated sessions and score them."""
    config = request.simulation
    symbol = f"SIM_{request.scenario.upper()}"
    instrument = Instrument(symbol=symbol, scenario=request.scenario)
    market_config = MarketConfig(
        universe=(instrument,),
        n_sessions=request.trials,
        session_seconds=request.session_seconds,
        seed=request.seed,
    )
    market = generate_market(market_config)
    view = MarketView(
        market.quotes,
        market.trades,
        trailing_window_seconds=config.impact.reference_window_seconds,
    )
    profile = VolumeProfile.from_history(
        instrument,
        session_seconds=request.session_seconds,
        bucket_seconds=config.profile_bucket_seconds,
        n_sessions=config.profile_sessions,
        seed=request.seed,
    )
    available = build_strategies(
        config,
        profiles={symbol: profile},
        hindsight_markets={symbol: view} if request.include_oracle else None,
    )
    selected = [available[name] for name in request.strategies]
    if request.include_oracle:
        selected.append(available[OracleVwapBenchmark.name])

    rng = np.random.default_rng(np.random.SeedSequence(request.seed, spawn_key=(_ARRIVAL_STREAM,)))
    latest = (
        request.session_seconds - request.horizon_seconds - EDGE_SECONDS - MARKOUT_BUFFER_SECONDS
    )
    order_rows: list[dict[str, object]] = []
    executions: list[SimulatedExecution] = []
    trial_of: dict[str, int] = {}
    for trial, session_open in enumerate(market_config.session_opens()):
        offset = float(rng.uniform(EDGE_SECONDS, latest))
        arrival_us = to_epoch_us(session_open) + math.floor(offset * 1_000_000)
        for strategy in selected:
            order = ParentOrder(
                order_id=f"T{trial + 1:03d}-{strategy.name}",
                symbol=symbol,
                side=request.side,
                quantity=request.quantity,
                arrival_us=arrival_us,
                horizon_seconds=float(request.horizon_seconds),
            )
            executions.append(simulate_order(order, strategy, view, config))
            trial_of[order.order_id] = trial + 1
            order_rows.append(
                {
                    "order_id": order.order_id,
                    "symbol": symbol,
                    "side": request.side.value,
                    "quantity": request.quantity,
                    "arrival_timestamp": arrival_us,
                    "execution_horizon_seconds": order.horizon_seconds,
                    "strategy": strategy.name,
                }
            )

    orders = pl.DataFrame(
        order_rows, schema={**ORDER_SCHEMA, "arrival_timestamp": pl.Int64}
    ).with_columns(pl.from_epoch("arrival_timestamp", time_unit="us").dt.replace_time_zone("UTC"))
    tca_config = TcaConfig(stale_quote_tolerance_seconds=config.stale_quote_tolerance_seconds)
    tca = run_tca(market.quotes, market.trades, orders, fills_to_frame(executions), tca_config)
    implementable = {s.name: s.implementable for s in selected}
    scored = tca.orders.with_columns(
        pl.col("order_id").replace_strict(trial_of, return_dtype=pl.Int64).alias("trial"),
        pl.col("strategy")
        .replace_strict(implementable, return_dtype=pl.Boolean)
        .alias("implementable"),
    )
    summary = _summary(scored, tca_config.markout_horizons_seconds)
    return ComparisonResult(
        request=request,
        market=market,
        orders=scored,
        fills=tca.fills,
        summary=summary,
        conclusions=_conclusions(summary, request),
    )
