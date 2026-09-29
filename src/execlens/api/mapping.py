"""Translate analysis rows into API models.

This module only renames and nests fields; every number comes from the
analysis service. No metric is computed here.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

import polars as pl

from execlens.api.schemas import (
    BenchmarkInterval,
    Costs,
    Diagnostics,
    ExecutionStats,
    Fill,
    FillTca,
    FlagCount,
    GroupStats,
    Markout,
    OrderDetail,
    OrderSummary,
    OrderTca,
    QuoteContext,
    SimulationRequest,
    SimulationResponse,
    StrategyResult,
    SummaryResponse,
)
from execlens.services.analysis import AnalysisResult
from execlens.simulation.comparison import ComparisonRequest, ComparisonResult
from execlens.simulation.impact import ImpactModel
from execlens.simulation.simulator import SimulationConfig
from execlens.tca.aggregation import summarize_by

Row = Mapping[str, Any]


def order_summary(row: Row) -> OrderSummary:
    return OrderSummary.model_validate({k: row[k] for k in OrderSummary.model_fields})


def order_detail(row: Row, fills: pl.DataFrame) -> OrderDetail:
    fields = {k: row[k] for k in OrderDetail.model_fields if k != "fills"}
    fields["fills"] = [
        Fill(
            fill_id=f["fill_id"], timestamp=f["timestamp"], quantity=f["quantity"], price=f["price"]
        )
        for f in fills.iter_rows(named=True)
    ]
    return OrderDetail.model_validate(fields)


def order_tca(row: Row, fills: pl.DataFrame, horizons: tuple[int, ...]) -> OrderTca:
    return OrderTca(
        order_id=row["order_id"],
        symbol=row["symbol"],
        side=row["side"],
        strategy=row["strategy"],
        arrival=QuoteContext(
            status=row["arrival_quote_status"],
            quote_timestamp=row["arrival_quote_timestamp"],
            quote_age_seconds=row["arrival_quote_age_seconds"],
            bid=row["arrival_bid"],
            ask=row["arrival_ask"],
            mid=row["arrival_mid"],
            spread_bps=row["arrival_spread_bps"],
        ),
        execution=ExecutionStats(
            requested_quantity=row["quantity"],
            executed_quantity=row["executed_quantity"],
            completion_ratio=row["completion_ratio"],
            n_fills=row["n_fills"],
            avg_fill_price=row["avg_fill_price"],
            executed_notional=row["executed_notional"],
            first_fill_timestamp=row["first_fill_timestamp"],
            last_fill_timestamp=row["last_fill_timestamp"],
            order_duration_seconds=row["order_duration_seconds"],
        ),
        benchmark=BenchmarkInterval(
            start=row["benchmark_start"],
            end=row["benchmark_end"],
            market_volume=row["market_volume"],
            market_vwap=row["market_vwap"],
            participation_rate=row["participation_rate"],
            quantity_to_market_volume=row["quantity_to_market_volume"],
            execution_window_volume=row["execution_window_volume"],
            execution_participation_rate=row["execution_participation_rate"],
        ),
        costs=Costs(
            arrival_cost_bps=row["arrival_cost_bps"],
            vwap_cost_bps=row["vwap_cost_bps"],
            effective_spread_bps=row["effective_spread_bps"],
            avg_contemporaneous_spread_bps=row["avg_contemporaneous_spread_bps"],
        ),
        markouts=[
            Markout(
                horizon_seconds=h,
                markout_bps=row[f"markout_{h}s_bps"],
                mid_markout_bps=row[f"mid_markout_{h}s_bps"],
                unavailable_fills=row[f"markout_{h}s_unavailable_fills"],
            )
            for h in horizons
        ],
        diagnostics=Diagnostics.model_validate({k: row[k] for k in Diagnostics.model_fields}),
        n_fills_without_quote=row["n_fills_without_quote"],
        fills=[
            FillTca(
                fill_id=f["fill_id"],
                timestamp=f["timestamp"],
                quantity=f["quantity"],
                price=f["price"],
                quote_status=f["fill_quote_status"],
                mid=f["fill_mid"],
                effective_spread_bps=f["effective_spread_bps"],
                markouts_bps={f"{h}s": f[f"markout_{h}s_bps"] for h in horizons},
            )
            for f in fills.iter_rows(named=True)
        ],
    )


def _group_stats(orders: pl.DataFrame, key: str) -> list[GroupStats]:
    grouped = summarize_by(orders, key)
    return [
        GroupStats.model_validate(
            {"group": str(row[key])}
            | {k: row.get(k) for k in GroupStats.model_fields if k != "group"}
        )
        for row in grouped.iter_rows(named=True)
    ]


def summary_response(analysis: AnalysisResult) -> SummaryResponse:
    return SummaryResponse(
        overall=analysis.summary,
        by_strategy=_group_stats(
            analysis.orders.with_columns(pl.col("strategy").fill_null("UNLABELLED")), "strategy"
        ),
        by_symbol=_group_stats(analysis.orders, "symbol"),
        flags=[FlagCount.model_validate(row) for row in analysis.flag_counts.iter_rows(named=True)],
    )


def comparison_request(body: SimulationRequest) -> ComparisonRequest:
    """Map the public request onto the domain request (raises pydantic.ValidationError)."""
    return ComparisonRequest(
        scenario=body.scenario,
        side=body.side,
        quantity=body.quantity,
        horizon_seconds=body.horizon_seconds,
        strategies=tuple(body.strategies),
        include_oracle=body.include_oracle,
        trials=body.trials,
        seed=body.seed,
        simulation=SimulationConfig(
            slice_seconds=body.slice_seconds,
            pov_participation_rate=body.participation_rate,
            impact=ImpactModel(coefficient_bps=body.impact_coefficient_bps),
        ),
    )


def simulation_response(
    simulation_id: str, created_at: datetime, body: SimulationRequest, result: ComparisonResult
) -> SimulationResponse:
    markout_columns = [c for c in result.summary.columns if c.startswith("mean_markout_")]
    results = [
        StrategyResult.model_validate(
            {k: row.get(k) for k in StrategyResult.model_fields if k != "mean_markouts_bps"}
            | {
                "mean_markouts_bps": {
                    c.removeprefix("mean_markout_").removesuffix("_bps"): row[c]
                    for c in markout_columns
                }
            }
        )
        for row in result.summary.iter_rows(named=True)
    ]
    return SimulationResponse(
        simulation_id=simulation_id,
        created_at=created_at,
        request=body,
        results=results,
        conclusions=list(result.conclusions),
    )
