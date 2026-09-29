"""Frozen regression dataset: refactors must not silently change results.

Inputs and expected outputs live in tests/fixtures/regression and were built
by ``build_fixture.py`` (after cross-checking the engine against the loop
reference). If a change here is intended, rebuild the fixture and review the
JSON diff in the same commit.
"""

from __future__ import annotations

import json
import math
from datetime import time
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from execlens.data.loader import read_dataset
from execlens.data.schemas import Side
from execlens.data.validation import ExecutionDataset
from execlens.simulation.base import MarketView, ParentOrder, to_epoch_us
from execlens.simulation.simulator import SimulationConfig, build_strategies, simulate_order
from execlens.simulation.vwap import VolumeProfile
from execlens.tca.engine import run_tca
from execlens.tca.reference import reference_tca

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "regression"


@pytest.fixture(scope="module")
def dataset() -> ExecutionDataset:
    return read_dataset(FIXTURE)


def load(name: str) -> Any:
    return json.loads((FIXTURE / name).read_text(encoding="utf-8"))


def assert_matches(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    for key, want in expected.items():
        got = actual[key]
        if isinstance(want, float) and got is not None:
            assert math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-9), (key, got, want)
        else:
            assert got == want, (key, got, want)


@pytest.mark.parametrize("implementation", ["engine", "reference"])
def test_order_level_tca_is_unchanged(dataset: ExecutionDataset, implementation: str) -> None:
    expected = load("expected_tca.json")
    q, t, o, f = dataset.quotes, dataset.trades, dataset.orders, dataset.fills
    engine = implementation == "engine"
    actual = run_tca(q, t, o, f).orders if engine else reference_tca(q, t, o, f)
    rows = {row["order_id"]: row for row in actual.to_dicts()}
    assert len(rows) == len(expected) == 8
    for want in expected:
        assert_matches(rows[want["order_id"]], want)


def test_regression_covers_both_directions_and_partial_fills() -> None:
    expected = load("expected_tca.json")
    assert any(row["completion_ratio"] < 1 for row in expected)
    assert any(row["completion_ratio"] == 1 for row in expected)
    assert all(row["arrival_cost_bps"] is not None for row in expected)


def test_strategy_outputs_are_unchanged(dataset: ExecutionDataset) -> None:
    spec = load("expected_strategies.json")
    order_spec, profile_spec = spec["order"], spec["profile"]
    config = SimulationConfig()
    symbol = order_spec["symbol"]
    view = MarketView(
        dataset.quotes.filter(pl.col("symbol") == symbol),
        dataset.trades.filter(pl.col("symbol") == symbol),
        trailing_window_seconds=config.impact.reference_window_seconds,
    )
    session_open = dataset.quotes["timestamp"].min()
    arrival_us = to_epoch_us(session_open) + order_spec["offset_seconds"] * 1_000_000
    order = ParentOrder(
        order_id=order_spec["order_id"],
        symbol=symbol,
        side=Side(order_spec["side"]),
        quantity=order_spec["quantity"],
        arrival_us=arrival_us,
        horizon_seconds=order_spec["horizon_seconds"],
    )
    profile = VolumeProfile(
        time(14, 30), profile_spec["bucket_seconds"], tuple(profile_spec["fractions"])
    )
    strategies = build_strategies(config, profiles={symbol: profile})
    assert set(strategies) == set(spec["fills"])
    for name, strategy in strategies.items():
        fills = simulate_order(order, strategy, view, config).fills
        expected = spec["fills"][name]
        assert [f.timestamp_us - arrival_us for f in fills] == [e["offset_us"] for e in expected]
        assert [f.quantity for f in fills] == [e["quantity"] for e in expected]
        for fill, want in zip(fills, expected, strict=True):
            assert math.isclose(fill.price, want["price"], rel_tol=1e-12), name
