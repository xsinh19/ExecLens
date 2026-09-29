"""Execution strategies, fill model and simulator mechanics."""

from __future__ import annotations

import math
from datetime import time, timedelta, timezone

import polars as pl
import pytest
from pydantic import ValidationError

from execlens.data.schemas import FILL_SCHEMA, MICROS, Side, to_epoch_us
from execlens.simulation.base import MarketView, ParentOrder, allocate, slice_boundaries
from execlens.simulation.immediate import ImmediateStrategy
from execlens.simulation.impact import ImpactModel
from execlens.simulation.pov import PovStrategy
from execlens.simulation.simulator import (
    SimulationConfig,
    build_strategies,
    fills_to_frame,
    market_views,
    simulate_order,
    simulate_orders,
)
from execlens.simulation.twap import TwapStrategy, twap_schedule
from execlens.simulation.vwap import OracleVwapBenchmark, VolumeProfile, VwapStrategy
from factories import T0, quotes_frame, trades_frame

T0_US = to_epoch_us(T0)
NO_IMPACT = SimulationConfig(impact=ImpactModel(coefficient_bps=0.0))


def flat_market(
    seconds: int = 900, trade_every: int = 2, trade_size: int = 100
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Constant 9.99 / 10.01 quote every second and a steady trade tape."""
    quotes = quotes_frame([(s, "SYN", 9.99, 10.01, 500, 500) for s in range(seconds)])
    trades = trades_frame(
        [(s + 0.5, "SYN", 10.00, trade_size) for s in range(0, seconds, trade_every)]
    )
    return quotes, trades


def order(qty: int = 1_000, arrival_s: float = 60.0, horizon: float | None = 300.0) -> ParentOrder:
    return ParentOrder(
        order_id="O1",
        symbol="SYN",
        side=Side.BUY,
        quantity=qty,
        arrival_us=T0_US + round(arrival_s * MICROS),
        horizon_seconds=horizon,
    )


# --- allocation and scheduling ------------------------------------------------


def test_allocate_is_exact_deterministic_and_prefers_earlier_slices_on_ties() -> None:
    assert allocate(10, [1, 1, 1, 1]) == [3, 3, 2, 2]
    assert allocate(1_000, [1, 1, 1]) == [334, 333, 333]
    # raw 3.5 / 1.75 / 1.75 -> floors 3 / 1 / 1; the two leftover shares go to
    # the largest remainders (0.75, 0.75), not to the 0.5 remainder.
    assert allocate(7, [0.5, 0.25, 0.25]) == [3, 2, 2]
    assert allocate(0, [1, 2]) == [0, 0]
    shares = allocate(123_457, [0.1, 0.35, 0.05, 0.5])
    assert sum(shares) == 123_457
    assert all(s >= 0 for s in shares)


@pytest.mark.parametrize("weights", [[], [0, 0], [-1, 2], [math.inf, 1]])
def test_allocate_rejects_invalid_weights(weights: list[float]) -> None:
    with pytest.raises(ValueError, match="weights"):
        allocate(10, weights)


def test_allocate_rejects_negative_quantity() -> None:
    with pytest.raises(ValueError, match="quantity"):
        allocate(-1, [1.0])


def test_twap_schedule_conserves_quantity() -> None:
    assert twap_schedule(1_000, 3) == [334, 333, 333]
    for qty, n in [(1, 5), (99, 7), (100_000, 13)]:
        schedule = twap_schedule(qty, n)
        assert sum(schedule) == qty
        assert max(schedule) - min(schedule) <= 1
    with pytest.raises(ValueError, match="n_slices"):
        twap_schedule(10, 0)


def test_slice_boundaries_cover_the_horizon_exactly() -> None:
    bounds = slice_boundaries(order(horizon=100.0), 30.0)
    offsets = [(b - bounds[0]) / MICROS for b in bounds]
    assert offsets == [0.0, 30.0, 60.0, 90.0, 100.0]
    with pytest.raises(ValueError, match="horizon"):
        slice_boundaries(order(horizon=None), 30.0)
    with pytest.raises(ValueError, match="slice_seconds"):
        slice_boundaries(order(), 0.0)


def test_parent_order_validation() -> None:
    with pytest.raises(ValueError, match="quantity"):
        order(qty=0)
    with pytest.raises(ValueError, match="horizon"):
        order(horizon=0.0)


def test_parent_order_from_a_table_row_is_validated_and_time_zone_safe() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    row = {
        "order_id": "O1",
        "symbol": "SYN",
        "side": "SELL",
        "quantity": 500,
        "arrival_timestamp": T0.astimezone(ist),  # same instant, different zone
        "execution_horizon_seconds": 600.0,
        "strategy": "TWAP",
    }
    parsed = ParentOrder.from_row(row)
    assert parsed == ParentOrder("O1", "SYN", Side.SELL, 500, T0_US, 600.0)
    with pytest.raises(ValidationError):
        ParentOrder.from_row(row | {"side": "HOLD"})


# --- market view: point-in-time lookups ---------------------------------------


def test_snapshot_only_sees_data_at_or_before_its_time() -> None:
    quotes = quotes_frame([(0, "SYN", 9.99, 10.01, 1, 2), (10, "SYN", 19.99, 20.01, 3, 4)])
    trades = trades_frame([(5, "SYN", 10.0, 100), (10, "SYN", 20.0, 50), (11, "SYN", 20.0, 7)])
    view = MarketView(quotes, trades, trailing_window_seconds=6)
    at_10 = view.snapshot(T0_US + 10 * MICROS, arrival_us=T0_US)
    assert at_10.ask == 20.01  # quote stamped exactly at t is visible
    assert at_10.quote_age_seconds == 0.0
    assert at_10.volume_since_arrival == 150  # trades at 5 and 10, not 11
    assert at_10.trailing_volume == 150  # window (4, 10]
    just_before = view.snapshot(T0_US + 10 * MICROS - 1, arrival_us=T0_US)
    assert just_before.ask == 10.01
    assert just_before.volume_since_arrival == 100
    before_any = view.snapshot(T0_US - 1, arrival_us=T0_US)
    assert before_any.bid is None
    assert not before_any.has_fresh_quote(30.0)
    assert view.volume_between(T0_US + 5 * MICROS, T0_US + 10 * MICROS) == 150  # closed interval


# --- fill model ----------------------------------------------------------------


def test_impact_model_by_hand() -> None:
    model = ImpactModel(coefficient_bps=25.0, exponent=0.5)
    # 1,000 shares vs 10,000 recent volume: 25 * sqrt(0.1) = 7.9057 bps.
    assert model.impact_bps(1_000, 10_000) == pytest.approx(25 * math.sqrt(0.1))
    # Concave: doubling the clip multiplies impact by sqrt(2).
    assert model.impact_bps(2_000, 10_000) / model.impact_bps(1_000, 10_000) == pytest.approx(
        math.sqrt(2)
    )
    assert model.impact_bps(0, 10_000) == 0.0
    # Recent volume below the floor is floored (100 shares): 25 * sqrt(50 / 100).
    assert model.impact_bps(50, 0) == pytest.approx(25 * math.sqrt(0.5))
    assert model.impact_bps(10**9, 1) == model.max_impact_bps


def test_execution_price_starts_from_the_touch_and_adds_impact_against_the_order() -> None:
    quotes, trades = flat_market()
    view = MarketView(quotes, trades)
    snap = view.snapshot(T0_US + 400 * MICROS, T0_US)
    model = ImpactModel()
    impact = model.impact_bps(1_000, snap.trailing_volume)
    assert impact > 0
    assert model.execution_price(Side.BUY, 1_000, snap) == pytest.approx(10.01 * (1 + impact / 1e4))
    assert model.execution_price(Side.SELL, 1_000, snap) == pytest.approx(9.99 * (1 - impact / 1e4))
    no_quote = view.snapshot(T0_US - MICROS, T0_US)
    with pytest.raises(ValueError, match="without a quote"):
        model.execution_price(Side.BUY, 10, no_quote)


# --- strategies ----------------------------------------------------------------


def test_immediate_sends_everything_at_arrival() -> None:
    quotes, trades = flat_market()
    execution = simulate_order(
        order(horizon=None), ImmediateStrategy(), MarketView(quotes, trades), NO_IMPACT
    )
    assert [f.quantity for f in execution.fills] == [1_000]
    assert execution.fills[0].timestamp_us == order().arrival_us
    assert execution.fills[0].price == 10.01  # ask, zero impact


def test_immediate_without_a_fresh_quote_does_not_trade_on_stale_prices() -> None:
    quotes, trades = flat_market(seconds=10)
    execution = simulate_order(
        order(arrival_s=100.0), ImmediateStrategy(), MarketView(quotes, trades), NO_IMPACT
    )
    assert execution.fills == ()
    assert execution.skipped_without_quote == 1


def test_twap_conserves_quantity_and_never_overfills() -> None:
    quotes, trades = flat_market()
    execution = simulate_order(
        order(qty=1_000), TwapStrategy(30.0), MarketView(quotes, trades), NO_IMPACT
    )
    quantities = [f.quantity for f in execution.fills]
    assert quantities == [100] * 10
    assert execution.executed_quantity == 1_000
    offsets = [(f.timestamp_us - order().arrival_us) / MICROS for f in execution.fills]
    assert offsets == [0.0, 30.0, 60.0, 90.0, 120.0, 150.0, 180.0, 210.0, 240.0, 270.0]


def test_twap_catches_up_after_a_quote_gap_and_reports_partial_completion() -> None:
    # Quotes stop at t=150: decisions at 60..180 are fresh (tolerance 30 s),
    # later ones have only stale quotes and are skipped.
    quotes, trades = flat_market(seconds=151)
    execution = simulate_order(
        order(qty=1_000), TwapStrategy(30.0), MarketView(quotes, trades), NO_IMPACT
    )
    assert execution.executed_quantity == 500  # decisions at 60, 90, ..., 180
    assert execution.skipped_without_quote == 5
    # A gap in the middle is caught up at the next fresh decision.
    gap = pl.concat(
        [
            quotes_frame([(s, "SYN", 9.99, 10.01, 5, 5) for s in range(0, 80)]),
            quotes_frame([(s, "SYN", 9.99, 10.01, 5, 5) for s in range(125, 900)]),
        ]
    )
    caught_up = simulate_order(
        order(qty=1_000), TwapStrategy(30.0), MarketView(gap, trades), NO_IMPACT
    )
    assert caught_up.executed_quantity == 1_000
    assert [f.quantity for f in caught_up.fills][:3] == [100, 100, 200]  # t=120 skipped


def test_twap_is_deterministic() -> None:
    quotes, trades = flat_market()
    view = MarketView(quotes, trades)
    first = simulate_order(order(), TwapStrategy(30.0), view, SimulationConfig())
    second = simulate_order(order(), TwapStrategy(30.0), view, SimulationConfig())
    assert first == second


def flat_profile(buckets: int = 78) -> VolumeProfile:
    return VolumeProfile(time(14, 30), 300, tuple([1 / buckets] * buckets))


def test_volume_profile_slice_fractions() -> None:
    # Two 60 s buckets holding 25% and 75% of daily volume.
    profile = VolumeProfile(time(14, 30), 60, (0.25, 0.75))
    bounds = [T0_US + s * MICROS for s in (0, 30, 60, 90, 120, 150)]
    fractions = profile.slice_fractions(bounds)
    assert fractions == pytest.approx([0.125, 0.125, 0.375, 0.375, 0.0])
    with pytest.raises(ValueError, match="sum to 1"):
        VolumeProfile(time(14, 30), 60, (0.5, 0.2))


def test_vwap_weights_follow_the_expected_profile_and_sum_to_one() -> None:
    profile = VolumeProfile(time(14, 30), 60, tuple([0.02] * 5 + [0.18] * 5))
    strategy = VwapStrategy({"SYN": profile}, slice_seconds=60.0)
    weights = strategy.slice_weights(order(qty=1_000, arrival_s=0.0, horizon=600.0))
    assert sum(weights) == pytest.approx(1.0)
    plan = strategy.plan(order(qty=1_000, arrival_s=0.0, horizon=600.0))
    schedule = [plan.cumulative_target[0]] + [
        b - a for a, b in zip(plan.cumulative_target, plan.cumulative_target[1:], strict=False)
    ]
    assert schedule == [20] * 5 + [180] * 5
    with pytest.raises(KeyError, match="no historical volume profile"):
        strategy.plan(ParentOrder("X", "OTHER", Side.BUY, 10, T0_US, 60.0))


def test_vwap_falls_back_to_time_slicing_outside_the_profile() -> None:
    strategy = VwapStrategy({"SYN": VolumeProfile(time(14, 30), 60, (1.0,))}, slice_seconds=60.0)
    plan = strategy.plan(order(qty=100, arrival_s=600.0, horizon=120.0))
    assert plan.cumulative_target == (50, 100)


def test_pov_is_limited_by_observed_volume() -> None:
    quotes, trades = flat_market(trade_every=2, trade_size=100)  # 50 shares/s traded
    strategy = PovStrategy(participation_rate=0.1, slice_seconds=30.0)
    execution = simulate_order(order(qty=10_000), strategy, MarketView(quotes, trades), NO_IMPACT)
    view = MarketView(quotes, trades)
    executed = 0
    for fill in execution.fills:
        executed += fill.quantity
        observed = view.volume_between(order().arrival_us, fill.timestamp_us)
        assert executed <= math.floor(0.1 * observed)
    # 300 s * 50 shares/s * 10% = 1,500 of 10,000: POV does not force completion.
    assert execution.executed_quantity == 1_500
    with pytest.raises(ValueError, match="participation_rate"):
        PovStrategy(participation_rate=0.0)


def test_oracle_is_labelled_as_a_non_implementable_hindsight_benchmark() -> None:
    quotes, trades = flat_market()
    views = {"SYN": MarketView(quotes, trades)}
    oracle = OracleVwapBenchmark(views, slice_seconds=30.0)
    assert oracle.name == "ORACLE_VWAP"
    assert oracle.implementable is False
    assert VwapStrategy.implementable is True
    assert "ORACLE_VWAP" not in build_strategies(SimulationConfig(), profiles={})
    assert "ORACLE_VWAP" in build_strategies(SimulationConfig(), hindsight_markets=views)
    # Realised volume is uniform, so the oracle schedule is uniform too.
    assert oracle.plan(order(qty=1_000)).cumulative_target[:3] == (100, 200, 300)


# --- batch simulation ----------------------------------------------------------


def test_simulate_orders_uses_each_orders_strategy_label() -> None:
    quotes, trades = flat_market()
    orders = pl.DataFrame(
        {
            "order_id": ["A", "B"],
            "symbol": ["SYN", "SYN"],
            "side": ["BUY", "SELL"],
            "quantity": [300, 200],
            "arrival_timestamp": [T0, T0],
            "execution_horizon_seconds": [None, 120.0],
            "strategy": ["IMMEDIATE", "TWAP"],
        }
    ).with_columns(pl.col("arrival_timestamp").dt.cast_time_unit("us"))
    config = SimulationConfig()
    strategies = build_strategies(config, profiles={"SYN": flat_profile()})
    executions = simulate_orders(orders, market_views(quotes, trades, config), strategies, config)
    assert [e.strategy for e in executions] == ["IMMEDIATE", "TWAP"]
    fills = fills_to_frame(executions)
    assert fills.schema == pl.Schema(FILL_SCHEMA)
    assert fills.group_by("order_id").agg(pl.col("quantity").sum()).sort("order_id")[
        "quantity"
    ].to_list() == [300, 200]

    with pytest.raises(ValueError, match="unknown strategy"):
        simulate_orders(
            orders.with_columns(pl.lit("MAGIC").alias("strategy")),
            market_views(quotes, trades, config),
            strategies,
            config,
        )
    with pytest.raises(ValueError, match="no market data"):
        simulate_orders(
            orders.with_columns(pl.lit("OTHER").alias("symbol")),
            market_views(quotes, trades, config),
            strategies,
            config,
        )
