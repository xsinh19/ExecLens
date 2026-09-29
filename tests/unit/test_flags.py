from __future__ import annotations

from datetime import UTC, datetime

import polars as pl
import pytest

from execlens.diagnostics.flags import (
    FlagCode,
    FlagConfig,
    flag_category,
    flag_counts,
    flag_orders,
    robust_z,
)

N = 30
END = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


def base_orders(n: int = N) -> pl.DataFrame:
    """Unremarkable orders with a little dispersion in every metric (MAD > 0)."""
    wiggle = [(i % 5) - 2 for i in range(n)]  # -2..2
    return pl.DataFrame(
        {
            "order_id": [f"O{i:02d}" for i in range(n)],
            "arrival_cost_bps": [5.0 + w for w in wiggle],
            "vwap_cost_bps": [1.0 + w for w in wiggle],
            "effective_spread_bps": [4.0 + 0.5 * w for w in wiggle],
            "mid_markout_60s_bps": [0.0 + w for w in wiggle],
            "participation_rate": [0.05 + 0.01 * w for w in wiggle],
            "completion_ratio": [1.0] * n,
            "pre_arrival_volatility_bps": [3.0 + 0.2 * w for w in wiggle],
            "arrival_spread_bps": [3.0 + 0.2 * w for w in wiggle],
            "market_volume_per_minute": [20_000.0 + 1_000 * w for w in wiggle],
            "market_volume": [100_000] * n,
            "benchmark_end": [END] * n,
            "arrival_quote_status": ["OK"] * n,
        }
    )


def empty_fills() -> pl.DataFrame:
    return pl.DataFrame(schema={"order_id": pl.String, "fill_quote_status": pl.String})


def with_row(frame: pl.DataFrame, index: int, **values: object) -> pl.DataFrame:
    return frame.with_columns(
        pl.when(pl.int_range(pl.len()) == index)
        .then(pl.lit(value))
        .otherwise(pl.col(column))
        .alias(column)
        for column, value in values.items()
    )


def flags_of(
    frame: pl.DataFrame, order_id: str, fills: pl.DataFrame | None = None
) -> dict[str, object]:
    out = flag_orders(frame, fills if fills is not None else empty_fills())
    return out.filter(pl.col("order_id") == order_id).row(0, named=True)


def test_robust_z_by_hand() -> None:
    z = pl.select(robust_z(pl.Series([1.0, 2.0, 3.0, 4.0, 100.0]))).to_series().to_list()
    # median 3, |deviations| 2,1,0,1,97 -> MAD 1; z = 0.6745 * (x - 3) / 1
    assert z == pytest.approx([-1.349, -0.6745, 0.0, 0.6745, 65.4265])
    constant = pl.select(robust_z(pl.Series([2.0, 2.0, 2.0]))).to_series().to_list()
    assert constant == [None, None, None]  # MAD = 0: cannot assess, never flags


def test_unremarkable_orders_are_not_flagged() -> None:
    out = flag_orders(base_orders(), empty_fills())
    assert not out["is_flagged"].any()
    assert out["context_flags"].list.len().sum() == 0


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"arrival_cost_bps": 60.0}, FlagCode.HIGH_ARRIVAL_COST),
        ({"vwap_cost_bps": 40.0}, FlagCode.HIGH_VWAP_COST),
        ({"effective_spread_bps": 30.0}, FlagCode.HIGH_EFFECTIVE_SPREAD),
        ({"mid_markout_60s_bps": -25.0}, FlagCode.ADVERSE_MARKOUT),
        ({"participation_rate": 0.4}, FlagCode.HIGH_PARTICIPATION),
        ({"completion_ratio": 0.5}, FlagCode.LOW_COMPLETION),
        ({"arrival_quote_status": "STALE"}, FlagCode.STALE_MARKET_DATA),
        ({"arrival_quote_status": "MISSING"}, FlagCode.MISSING_MARKET_DATA),
        ({"market_volume": None}, FlagCode.MISSING_MARKET_DATA),
    ],
)
def test_each_issue_rule_fires_on_its_outlier(changes: dict[str, object], code: FlagCode) -> None:
    row = flags_of(with_row(base_orders(), 7, **changes), "O07")
    assert row["flags"] == [code.value]
    assert row["is_flagged"] is True
    assert len(row["flag_reasons"]) == 1
    others = flag_orders(with_row(base_orders(), 7, **changes), empty_fills()).filter(
        pl.col("order_id") != "O07"
    )
    assert not others["is_flagged"].any()


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"pre_arrival_volatility_bps": 20.0}, FlagCode.HIGH_VOLATILITY),
        ({"arrival_spread_bps": 25.0}, FlagCode.WIDE_SPREAD),
        ({"market_volume_per_minute": 500.0}, FlagCode.LOW_LIQUIDITY),
    ],
)
def test_context_rules_annotate_without_flagging(
    changes: dict[str, object], code: FlagCode
) -> None:
    row = flags_of(with_row(base_orders(), 3, **changes), "O03")
    assert row["context_flags"] == [code.value]
    assert row["flags"] == []
    assert row["is_flagged"] is False
    assert "coincided" in row["context_notes"][0]


def test_favourable_outliers_are_not_flagged() -> None:
    # Unusually *good* executions are outliers, but not problems.
    frame = with_row(base_orders(), 5, arrival_cost_bps=-60.0, mid_markout_60s_bps=30.0)
    assert flags_of(frame, "O05")["flags"] == []


def test_reason_text_states_value_and_comparison() -> None:
    row = flags_of(with_row(base_orders(), 7, arrival_cost_bps=60.0), "O07")
    # 30 costs: one 60, the rest 3..7 around 5 -> median 5, MAD 1,
    # z = 0.6745 * (60 - 5) / 1 = 37.1. There is no symbol column, so the
    # comparison is with all analysed orders.
    assert row["flag_reasons"] == [
        "Arrival cost 60.0 bps is unusually high compared with all analysed orders "
        "(median 5.0 bps, robust z 37.1)."
    ]
    row = flags_of(with_row(base_orders(), 7, participation_rate=0.4), "O07")
    assert row["flag_reasons"] == [
        "Order was 40.0% of market volume in its interval (threshold 25%)."
    ]


def test_no_reason_claims_causation() -> None:
    frame = with_row(
        base_orders(),
        2,
        arrival_cost_bps=60.0,
        pre_arrival_volatility_bps=20.0,
        arrival_spread_bps=25.0,
        market_volume_per_minute=500.0,
    )
    out = flag_orders(frame, empty_fills())
    texts = out["flag_reasons"].explode(empty_as_null=True).drop_nulls().to_list()
    texts += out["context_notes"].explode(empty_as_null=True).drop_nulls().to_list()
    assert texts
    assert not any("caus" in t.lower() or "because" in t.lower() for t in texts)


def test_small_populations_skip_statistical_rules() -> None:
    frame = with_row(base_orders(10), 1, arrival_cost_bps=500.0)
    out = flag_orders(frame, empty_fills(), FlagConfig(min_population=20))
    assert not out["is_flagged"].any()
    # Absolute rules still apply.
    frame = with_row(base_orders(10), 1, completion_ratio=0.1)
    assert flag_orders(frame, empty_fills())["is_flagged"].sum() == 1


def test_stale_fills_raise_a_data_quality_flag() -> None:
    fills = pl.DataFrame({"order_id": ["O04", "O04"], "fill_quote_status": ["OK", "STALE"]})
    row = flags_of(base_orders(), "O04", fills)
    assert row["flags"] == [FlagCode.STALE_MARKET_DATA.value]
    assert "fills with a stale quote: 1" in row["flag_reasons"][0]
    missing = pl.DataFrame({"order_id": ["O04"], "fill_quote_status": ["MISSING"]})
    assert flags_of(base_orders(), "O04", missing)["flags"] == [FlagCode.MISSING_MARKET_DATA.value]


def test_flag_counts_list_every_code_with_its_category() -> None:
    frame = with_row(base_orders(), 7, arrival_cost_bps=60.0, arrival_spread_bps=25.0)
    counts = flag_counts(flag_orders(frame, empty_fills()))
    assert counts.height == len(FlagCode)
    by_flag = {row["flag"]: row for row in counts.iter_rows(named=True)}
    assert by_flag["HIGH_ARRIVAL_COST"]["orders"] == 1
    assert by_flag["WIDE_SPREAD"]["orders"] == 1
    assert by_flag["WIDE_SPREAD"]["category"] == "context"
    assert by_flag["STALE_MARKET_DATA"]["category"] == "data_quality"
    assert by_flag["LOW_LIQUIDITY"]["orders"] == 0
    assert flag_category(FlagCode.ADVERSE_MARKOUT) == "execution"


def test_flag_config_validation() -> None:
    with pytest.raises(ValueError, match="z_threshold"):
        FlagConfig(z_threshold=0)
    with pytest.raises(ValueError, match="thresholds"):
        FlagConfig(low_completion=1.5)


def two_instruments() -> pl.DataFrame:
    """A cheap instrument (50 orders) and a uniformly expensive one (20 orders).

    As in the real book, the expensive instrument is the minority, so a pooled
    comparison would treat every one of its orders as an outlier.
    """
    cheap = base_orders(50).with_columns(pl.lit("CHEAP").alias("symbol"))
    wide = base_orders(20).with_columns(
        pl.lit("WIDE").alias("symbol"),
        (pl.col("order_id") + "W").alias("order_id"),
        (pl.col("effective_spread_bps") + 40.0).alias("effective_spread_bps"),
    )
    return pl.concat([cheap, wide])


def test_execution_flags_compare_orders_within_their_symbol() -> None:
    frame = with_row(two_instruments(), 7, effective_spread_bps=12.0)  # CHEAP outlier
    out = flag_orders(frame, empty_fills())
    flagged = out.filter(pl.col("flags").list.contains("HIGH_EFFECTIVE_SPREAD"))
    # Only the within-symbol outlier: the WIDE orders pay ~44 bps, but that is
    # normal for WIDE, so they are not flagged.
    assert flagged["order_id"].to_list() == ["O07"]
    assert "compared with CHEAP orders" in flagged["flag_reasons"][0][0]


def test_pooled_comparison_would_only_rediscover_the_instrument() -> None:
    out = flag_orders(two_instruments(), empty_fills(), FlagConfig(peer_group=None))
    flagged = out.filter(pl.col("flags").list.contains("HIGH_EFFECTIVE_SPREAD"))
    assert set(flagged["symbol"]) == {"WIDE"}
    assert flagged.height == 20


def test_small_peer_groups_fall_back_to_all_orders() -> None:
    few = base_orders(5).with_columns(
        pl.lit("RARE").alias("symbol"),
        (pl.col("order_id") + "R").alias("order_id"),
    )
    frame = pl.concat([two_instruments(), with_row(few, 2, effective_spread_bps=200.0)])
    row = flags_of(frame, "O02R")
    assert "HIGH_EFFECTIVE_SPREAD" in row["flags"]
    assert "compared with all analysed orders" in row["flag_reasons"][0]


def test_context_notes_compare_with_the_whole_book() -> None:
    frame = two_instruments().with_columns(
        pl.when(pl.col("symbol") == "WIDE")
        .then(pl.col("arrival_spread_bps") + 40.0)
        .otherwise(pl.col("arrival_spread_bps"))
        .alias("arrival_spread_bps")
    )
    out = flag_orders(frame, empty_fills())
    wide = out.filter(pl.col("symbol") == "WIDE")
    # Wide spreads describe the market the order met, relative to the book.
    assert wide["context_flags"].list.contains("WIDE_SPREAD").all()
    assert not wide["is_flagged"].any()
