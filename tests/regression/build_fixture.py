"""(Re)build the frozen regression fixture. Run deliberately, then review the diff.

    python tests/regression/build_fixture.py

Writes to tests/fixtures/regression/:

* ``quotes/trades/orders/fills.parquet`` - a tiny synthetic dataset (two
  symbols, 30 minutes, eight parent orders with simulated fills), frozen as
  files so later generator changes cannot alter the regression inputs;
* ``expected_tca.json`` - order-level TCA metrics for every order;
* ``expected_strategies.json`` - the fills each strategy produces for one fixed
  parent order on the frozen market, plus the volume profile VWAP used.

Before writing, the expected TCA values are cross-checked against the
independent loop-based reference implementation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import polars as pl

from execlens.data.loader import write_dataset
from execlens.data.schemas import Side, to_epoch_us
from execlens.data.synthetic import Instrument, MarketConfig, OrderConfig
from execlens.services.datasets import DatasetSpec, generate_dataset
from execlens.simulation.base import MarketView, ParentOrder
from execlens.simulation.simulator import SimulationConfig, build_strategies, simulate_order
from execlens.simulation.vwap import VolumeProfile
from execlens.tca.engine import run_tca
from execlens.tca.reference import compare_to_reference, reference_tca

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "regression"

SPEC = DatasetSpec(
    market=MarketConfig(
        universe=(
            Instrument(symbol="REG_LIQ", scenario="normal"),
            Instrument(symbol="REG_THIN", scenario="low_liquidity"),
        ),
        session_seconds=1_800,
        seed=2024,
    ),
    orders=OrderConfig(orders_per_symbol_per_session=4, horizons_seconds=(300, 600)),
)

TCA_COLUMNS = [
    "order_id",
    "arrival_quote_status",
    "arrival_mid",
    "avg_fill_price",
    "executed_quantity",
    "completion_ratio",
    "arrival_cost_bps",
    "market_volume",
    "market_vwap",
    "vwap_cost_bps",
    "effective_spread_bps",
    "markout_10s_bps",
    "markout_30s_bps",
    "markout_60s_bps",
    "markout_300s_bps",
    "mid_markout_60s_bps",
    "participation_rate",
    "execution_participation_rate",
]

STRATEGY_ORDER = {
    "order_id": "REG-STRAT",
    "symbol": "REG_LIQ",
    "side": "BUY",
    "quantity": 5_000,
    "offset_seconds": 600,
    "horizon_seconds": 300.0,
}
PROFILE = {"bucket_seconds": 300, "fractions": [0.3, 0.1, 0.1, 0.1, 0.1, 0.3]}


def strategy_fills(dataset_quotes: pl.DataFrame, dataset_trades: pl.DataFrame) -> dict[str, Any]:
    config = SimulationConfig()
    symbol = STRATEGY_ORDER["symbol"]
    view = MarketView(
        dataset_quotes.filter(pl.col("symbol") == symbol),
        dataset_trades.filter(pl.col("symbol") == symbol),
        trailing_window_seconds=config.impact.reference_window_seconds,
    )
    opens = SPEC.market.session_opens()
    arrival_us = to_epoch_us(opens[0]) + STRATEGY_ORDER["offset_seconds"] * 1_000_000
    order = ParentOrder(
        order_id=str(STRATEGY_ORDER["order_id"]),
        symbol=symbol,
        side=Side(STRATEGY_ORDER["side"]),
        quantity=int(STRATEGY_ORDER["quantity"]),
        arrival_us=arrival_us,
        horizon_seconds=float(STRATEGY_ORDER["horizon_seconds"]),
    )
    profile = VolumeProfile(
        opens[0].timetz().replace(tzinfo=None),
        int(PROFILE["bucket_seconds"]),
        tuple(PROFILE["fractions"]),
    )
    strategies = build_strategies(config, profiles={symbol: profile})
    return {
        name: [
            {
                "offset_us": fill.timestamp_us - arrival_us,
                "quantity": fill.quantity,
                "price": fill.price,
            }
            for fill in simulate_order(order, strategy, view, config).fills
        ]
        for name, strategy in strategies.items()
    }


def main() -> int:
    dataset = generate_dataset(SPEC)
    engine = run_tca(dataset.quotes, dataset.trades, dataset.orders, dataset.fills)
    reference = reference_tca(dataset.quotes, dataset.trades, dataset.orders, dataset.fills)
    problems = compare_to_reference(engine.orders, reference)
    if problems:
        print("engine and reference disagree; fixture NOT written:", problems)
        return 1

    FIXTURE.mkdir(parents=True, exist_ok=True)
    write_dataset(dataset, FIXTURE)
    expected = engine.orders.select(TCA_COLUMNS).sort("order_id").to_dicts()
    (FIXTURE / "expected_tca.json").write_text(
        json.dumps(expected, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    strategies = {
        "order": STRATEGY_ORDER,
        "profile": PROFILE,
        "fills": strategy_fills(dataset.quotes, dataset.trades),
    }
    (FIXTURE / "expected_strategies.json").write_text(
        json.dumps(strategies, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"wrote {FIXTURE}: {dataset.orders.height} orders, {dataset.fills.height} fills")
    return 0


if __name__ == "__main__":
    sys.exit(main())
