"""Application service: analyse a dataset once and share the result.

``analyze_dataset`` composes the domain modules in a fixed order:

    TCA engine -> diagnostic features -> explainable flags -> portfolio summary

The CLI, the API and the dashboard all consume the resulting
:class:`AnalysisResult`; none of them recomputes a metric.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from execlens.config import DEFAULT_STALE_QUOTE_TOLERANCE_SECONDS
from execlens.data.loader import read_dataset
from execlens.data.validation import ExecutionDataset
from execlens.diagnostics.features import FeatureConfig, compute_features
from execlens.diagnostics.flags import FlagConfig, flag_counts, flag_orders
from execlens.tca.aggregation import TcaSummary, summarize_orders
from execlens.tca.engine import DEFAULT_MARKOUT_HORIZONS, TcaConfig, run_tca


class AnalysisConfig(BaseModel):
    """User-facing analysis settings (maps onto the domain configs)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stale_quote_tolerance_seconds: float = Field(
        default=DEFAULT_STALE_QUOTE_TOLERANCE_SECONDS, gt=0
    )
    markout_horizons_seconds: tuple[int, ...] = DEFAULT_MARKOUT_HORIZONS
    z_threshold: float = Field(default=3.5, gt=0)
    min_population: int = Field(default=20, ge=3)
    high_participation: float = Field(default=0.25, gt=0)
    low_completion: float = Field(default=0.95, gt=0, le=1)
    flag_markout_horizon_seconds: int = 60

    def tca(self) -> TcaConfig:
        return TcaConfig(
            stale_quote_tolerance_seconds=self.stale_quote_tolerance_seconds,
            markout_horizons_seconds=self.markout_horizons_seconds,
        )

    def features(self) -> FeatureConfig:
        return FeatureConfig(stale_quote_tolerance_seconds=self.stale_quote_tolerance_seconds)

    def flags(self) -> FlagConfig:
        horizon = self.flag_markout_horizon_seconds
        if horizon not in self.markout_horizons_seconds:
            raise ValueError(f"flag markout horizon {horizon}s is not a computed horizon")
        return FlagConfig(
            z_threshold=self.z_threshold,
            min_population=self.min_population,
            high_participation=self.high_participation,
            low_completion=self.low_completion,
            markout_horizon_seconds=horizon,
        )


@dataclass(frozen=True)
class AnalysisResult:
    dataset: ExecutionDataset
    orders: pl.DataFrame
    fills: pl.DataFrame
    summary: TcaSummary
    flag_counts: pl.DataFrame
    config: AnalysisConfig

    def order(self, order_id: str) -> dict[str, Any] | None:
        rows = self.orders.filter(pl.col("order_id") == order_id)
        return rows.row(0, named=True) if rows.height else None

    def fills_for(self, order_id: str) -> pl.DataFrame:
        return self.fills.filter(pl.col("order_id") == order_id).sort("timestamp")

    def market_window(
        self, order_id: str, *, before_seconds: float = 60.0, after_seconds: float = 120.0
    ) -> tuple[pl.DataFrame, pl.DataFrame]:
        """Quotes and trades around an order, for charts (empty frames if unknown)."""
        order = self.order(order_id)
        if order is None:
            return self.dataset.quotes.clear(), self.dataset.trades.clear()
        start = order["arrival_timestamp"] - timedelta(seconds=before_seconds)
        end = (
            order["benchmark_end"] or order["last_fill_timestamp"] or order["arrival_timestamp"]
        ) + timedelta(seconds=after_seconds)
        window = (pl.col("symbol") == order["symbol"]) & pl.col("timestamp").is_between(start, end)
        return self.dataset.quotes.filter(window), self.dataset.trades.filter(window)


def analyze_dataset(
    dataset: ExecutionDataset, config: AnalysisConfig | None = None
) -> AnalysisResult:
    """Full analysis of a dataset that was validated on load."""
    config = config or AnalysisConfig()
    flag_config = config.flags()
    tca = run_tca(
        dataset.quotes,
        dataset.trades,
        dataset.orders,
        dataset.fills,
        config.tca(),
        validate=False,
    )
    with_features = compute_features(dataset.quotes, tca.orders, config.features())
    orders = flag_orders(with_features, tca.fills, flag_config)
    return AnalysisResult(
        dataset=dataset,
        orders=orders,
        fills=tca.fills,
        summary=summarize_orders(orders),
        flag_counts=flag_counts(orders),
        config=config,
    )


def analyze_directory(directory: Path, config: AnalysisConfig | None = None) -> AnalysisResult:
    """Load (and validate) a dataset directory, then analyse it."""
    return analyze_dataset(read_dataset(directory), config)


def save_analysis(result: AnalysisResult, directory: Path) -> Path:
    """Persist derived tables: order/fill TCA (Parquet), flag counts, summary and config."""
    directory.mkdir(parents=True, exist_ok=True)
    result.orders.write_parquet(directory / "orders_tca.parquet", compression="zstd")
    result.fills.write_parquet(directory / "fills_tca.parquet", compression="zstd")
    result.flag_counts.write_parquet(directory / "flag_counts.parquet", compression="zstd")
    (directory / "summary.json").write_text(
        result.summary.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    (directory / "analysis_config.json").write_text(
        json.dumps(result.config.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return directory
