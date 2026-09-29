"""In-process API state: the precomputed analysis plus a bounded simulation store."""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field

import polars as pl

from execlens.api import mapping
from execlens.api.schemas import DatasetInfo, SimulationResponse, SummaryResponse
from execlens.services.analysis import AnalysisResult


class SimulationStore:
    """Thread-safe, size-bounded store of simulation results (oldest evicted first)."""

    def __init__(self, max_items: int = 100) -> None:
        self._items: OrderedDict[str, SimulationResponse] = OrderedDict()
        self._lock = threading.Lock()
        self.max_items = max_items

    def put(self, result: SimulationResponse) -> None:
        with self._lock:
            self._items[result.simulation_id] = result
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)

    def get(self, simulation_id: str) -> SimulationResponse | None:
        with self._lock:
            return self._items.get(simulation_id)

    def __len__(self) -> int:
        return len(self._items)


@dataclass
class ApiState:
    """Everything request handlers need, built once at startup.

    The analysis is immutable, so the dataset description and the portfolio
    summary are prepared once, and ``order_rows`` / ``fills_by_order`` index
    the analysis tables so per-order endpoints are dictionary lookups rather
    than table scans.
    """

    analysis: AnalysisResult
    source: str
    simulations: SimulationStore = field(default_factory=SimulationStore)
    dataset_info: DatasetInfo = field(init=False)
    summary: SummaryResponse = field(init=False)
    order_rows: dict[str, int] = field(init=False)
    fills_by_order: dict[str, pl.DataFrame] = field(init=False)

    def __post_init__(self) -> None:
        dataset = self.analysis.dataset
        self.dataset_info = DatasetInfo(
            path=self.source,
            synthetic=bool(dataset.metadata.get("synthetic", False)),
            symbols=dataset.symbols,
            quotes=dataset.quotes.height,
            trades=dataset.trades.height,
            orders=dataset.orders.height,
            fills=dataset.fills.height,
        )
        self.summary = mapping.summary_response(self.analysis)
        self.order_rows = {
            order_id: i for i, order_id in enumerate(self.analysis.orders["order_id"].to_list())
        }
        partitions = self.analysis.fills.partition_by("order_id", as_dict=True)
        self.fills_by_order = {str(key[0]): frame for key, frame in partitions.items()}

    def order(self, order_id: str) -> dict[str, object] | None:
        index = self.order_rows.get(order_id)
        return None if index is None else self.analysis.orders.row(index, named=True)

    def fills(self, order_id: str) -> pl.DataFrame:
        return self.fills_by_order.get(order_id, self.analysis.fills.clear())
