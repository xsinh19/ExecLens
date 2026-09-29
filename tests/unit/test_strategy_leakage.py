"""Information-leakage tests for the execution strategies.

Pattern: build two markets that are identical up to a cut-off time and differ
afterwards. Any decision taken at or before the cut-off must be identical in
both, otherwise the strategy used information it could not have had.
"""

from __future__ import annotations

from datetime import time

import numpy as np
import polars as pl

from execlens.data.schemas import MICROS, Side, to_epoch_us
from execlens.data.synthetic import generate_market, generate_orders
from execlens.services.datasets import preset, volume_profiles
from execlens.simulation.base import MarketView, ParentOrder
from execlens.simulation.impact import ImpactModel
from execlens.simulation.pov import PovStrategy
from execlens.simulation.simulator import (
    SimulationConfig,
    build_strategies,
    market_views,
    simulate_order,
    simulate_orders,
)
from execlens.simulation.twap import TwapStrategy
from execlens.simulation.vwap import VolumeProfile, VwapStrategy
from factories import T0, quotes_frame, trades_frame

T0_US = to_epoch_us(T0)
CUTOFF_S = 200


def diverging_markets() -> tuple[MarketView, MarketView]:
    quotes = quotes_frame([(s, "SYN", 9.99, 10.01, 500, 500) for s in range(900)])
    common = [(s + 0.5, "SYN", 10.0, 100) for s in range(0, CUTOFF_S, 2)]
    quiet_future = [(s + 0.5, "SYN", 10.0, 10) for s in range(CUTOFF_S, 900, 2)]
    busy_future = [(s + 0.5, "SYN", 10.0, 5_000) for s in range(CUTOFF_S, 900, 2)]
    return (
        MarketView(quotes, trades_frame(common + quiet_future)),
        MarketView(quotes, trades_frame(common + busy_future)),
    )


def parent(qty: int = 50_000) -> ParentOrder:
    return ParentOrder("O1", "SYN", Side.BUY, qty, T0_US + 60 * MICROS, 600.0)


def quantities_until(fills: tuple[object, ...], cutoff_us: int) -> list[tuple[int, int]]:
    return [(f.timestamp_us, f.quantity) for f in fills if f.timestamp_us <= cutoff_us]  # type: ignore[attr-defined]


def test_pov_decisions_do_not_depend_on_future_volume() -> None:
    quiet, busy = diverging_markets()
    config = SimulationConfig(impact=ImpactModel(coefficient_bps=0.0))
    strategy = PovStrategy(participation_rate=0.2, slice_seconds=30.0)
    a = simulate_order(parent(), strategy, quiet, config)
    b = simulate_order(parent(), strategy, busy, config)
    cutoff_us = T0_US + CUTOFF_S * MICROS
    assert quantities_until(a.fills, cutoff_us) == quantities_until(b.fills, cutoff_us)
    assert len(quantities_until(a.fills, cutoff_us)) >= 4
    # After the cut-off the two runs diverge, driven by volume observed later.
    assert a.executed_quantity < b.executed_quantity


def test_vwap_schedule_is_fixed_at_arrival_and_ignores_same_session_volume() -> None:
    quiet, busy = diverging_markets()
    profile = VolumeProfile(time(14, 30), 300, tuple([1 / 78] * 78))
    strategy = VwapStrategy({"SYN": profile}, slice_seconds=30.0)
    config = SimulationConfig(impact=ImpactModel(coefficient_bps=0.0))
    a = simulate_order(parent(), strategy, quiet, config)
    b = simulate_order(parent(), strategy, busy, config)
    # The whole schedule is identical although realised volume differs 500x.
    assert [(f.timestamp_us, f.quantity) for f in a.fills] == [
        (f.timestamp_us, f.quantity) for f in b.fills
    ]


def test_dataset_vwap_schedules_ignore_the_traded_sessions_volume() -> None:
    # End to end through the dataset service: re-draw every trade size of the
    # traded sessions. VWAP child sizes and times must not move (prices may:
    # impact reads volume that has already printed).
    spec = preset("sample")
    market = generate_market(spec.market)
    orders = generate_orders(spec.market, spec.orders).filter(pl.col("strategy") == "VWAP")
    rng = np.random.default_rng(0)
    perturbed = market.trades.with_columns(
        pl.col("size") * pl.Series(rng.integers(1, 50, market.trades.height))
    )

    def schedules(label: str, trades: pl.DataFrame) -> list[tuple[str, int, int]]:
        views = market_views(market.quotes, trades, spec.simulation)
        strategies = build_strategies(
            spec.simulation, profiles=volume_profiles(spec), hindsight_markets=views
        )
        labelled = orders.with_columns(pl.lit(label).alias("strategy"))
        executions = simulate_orders(labelled, views, strategies, spec.simulation)
        return [(f.order_id, f.timestamp_us, f.quantity) for e in executions for f in e.fills]

    assert orders.height >= 30
    assert schedules("VWAP", perturbed) == schedules("VWAP", market.trades)
    # Control: the hindsight oracle, which does read realised volume, notices
    # the same perturbation, so the check above is able to fail.
    assert schedules("ORACLE_VWAP", perturbed) != schedules("ORACLE_VWAP", market.trades)


def test_scheduled_strategies_see_only_past_quotes_when_pricing() -> None:
    quotes = quotes_frame(
        [(s, "SYN", 9.99, 10.01, 500, 500) for s in range(CUTOFF_S)]
        + [(s, "SYN", 99.99, 100.01, 500, 500) for s in range(CUTOFF_S, 900)]
    )
    trades = trades_frame([(s + 0.5, "SYN", 10.0, 100) for s in range(0, 900, 2)])
    execution = simulate_order(
        parent(qty=1_000),
        TwapStrategy(30.0),
        MarketView(quotes, trades),
        SimulationConfig(impact=ImpactModel(coefficient_bps=0.0)),
    )
    for fill in execution.fills:
        expected = 10.01 if fill.timestamp_us < T0_US + CUTOFF_S * MICROS else 100.01
        assert fill.price == expected


def test_trade_tape_changes_after_arrival_do_not_change_twap() -> None:
    quiet, busy = diverging_markets()
    config = SimulationConfig(impact=ImpactModel(coefficient_bps=0.0))
    a = simulate_order(parent(), TwapStrategy(30.0), quiet, config)
    b = simulate_order(parent(), TwapStrategy(30.0), busy, config)
    assert a == b


def test_market_view_partition_by_symbol_isolates_symbols() -> None:
    quotes = pl.concat(
        [
            quotes_frame([(0, "AAA", 9.99, 10.01, 1, 1)]),
            quotes_frame([(5, "BBB", 49.99, 50.01, 1, 1)]),
        ]
    )
    views = MarketView.by_symbol(quotes, trades_frame([(1, "AAA", 10.0, 5)]))
    assert set(views) == {"AAA", "BBB"}
    snap = views["BBB"].snapshot(T0_US + 10 * MICROS, T0_US)
    assert snap.bid == 49.99
    assert snap.volume_since_arrival == 0  # AAA's trade is not BBB's volume
