"""Deterministic, explainable execution flags.

Each flag is a transparent rule over one metric, so every flagged order comes
with a human-readable reason stating the value and the comparison point.

Statistical rules use the **modified z-score** (Iglewicz & Hoaglin), which is
robust to the heavy tails typical of execution costs::

    z = 0.6745 * (x - median(x)) / MAD(x),      MAD = median(|x - median(x)|)

The default threshold is |z| >= 3.5. The comparison group depends on what the
rule asks:

* **execution** rules ask "was this execution unusual *for this
  instrument*?", so each order is compared with the orders in the same symbol
  (its peer group) when at least ``min_population`` of them have the metric;
  otherwise with all analysed orders. Comparing a wide-spread instrument's
  orders with a liquid one's would only rediscover the instrument.
* **context** rules ask "were market conditions unusual across the book?", so
  they compare with all analysed orders.

A rule is skipped when its comparison group is too small or has zero MAD (no
spread to compare against). Rules with a natural scale (participation,
completion) use absolute thresholds instead.

Rules fall into three categories:

* **execution** - the execution itself looks unusual (costs, markouts,
  participation, completion);
* **data_quality** - the analysis could not be completed as designed
  (stale or missing market data);
* **context** - market conditions that coincided with the order (volatility,
  spread, liquidity). They explain; they do not accuse.

An order ``is_flagged`` when it has at least one execution or data-quality
flag; context notes are reported alongside (``context_flags`` /
``context_notes``). All wording describes **co-occurrence, not causation**:
"the order coincided with wide spreads", never "wide spreads caused the cost".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import polars as pl

from execlens.tca.alignment import ContextStatus

MAD_TO_Z = 0.6745
ALL_ORDERS = "all analysed orders"


class FlagCode(StrEnum):
    HIGH_ARRIVAL_COST = "HIGH_ARRIVAL_COST"
    HIGH_VWAP_COST = "HIGH_VWAP_COST"
    HIGH_EFFECTIVE_SPREAD = "HIGH_EFFECTIVE_SPREAD"
    ADVERSE_MARKOUT = "ADVERSE_MARKOUT"
    HIGH_PARTICIPATION = "HIGH_PARTICIPATION"
    LOW_COMPLETION = "LOW_COMPLETION"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    WIDE_SPREAD = "WIDE_SPREAD"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    MISSING_MARKET_DATA = "MISSING_MARKET_DATA"


CONTEXT_FLAGS = frozenset({FlagCode.HIGH_VOLATILITY, FlagCode.WIDE_SPREAD, FlagCode.LOW_LIQUIDITY})
DATA_QUALITY_FLAGS = frozenset({FlagCode.STALE_MARKET_DATA, FlagCode.MISSING_MARKET_DATA})


def flag_category(code: FlagCode) -> str:
    if code in CONTEXT_FLAGS:
        return "context"
    if code in DATA_QUALITY_FLAGS:
        return "data_quality"
    return "execution"


FLAG_DESCRIPTIONS: dict[FlagCode, str] = {
    FlagCode.HIGH_ARRIVAL_COST: "Arrival cost unusually high for the symbol.",
    FlagCode.HIGH_VWAP_COST: "Cost versus interval VWAP unusually high for the symbol.",
    FlagCode.HIGH_EFFECTIVE_SPREAD: "Effective spread paid unusually high for the symbol.",
    FlagCode.ADVERSE_MARKOUT: (
        "Mid price moved unusually far against the order after its fills, for the symbol."
    ),
    FlagCode.HIGH_PARTICIPATION: "Order was a large share of market volume in its interval.",
    FlagCode.LOW_COMPLETION: "A material part of the order was not executed.",
    FlagCode.HIGH_VOLATILITY: "Execution coincided with elevated short-term volatility.",
    FlagCode.WIDE_SPREAD: "Execution coincided with a wide quoted spread at arrival.",
    FlagCode.LOW_LIQUIDITY: "Execution coincided with unusually thin market volume.",
    FlagCode.STALE_MARKET_DATA: "Market context was stale for the arrival or some fills.",
    FlagCode.MISSING_MARKET_DATA: "Required market data was missing.",
}


@dataclass(frozen=True)
class FlagConfig:
    z_threshold: float = 3.5
    min_population: int = 20
    high_participation: float = 0.25
    low_completion: float = 0.95
    markout_horizon_seconds: int = 60
    peer_group: str | None = "symbol"

    def __post_init__(self) -> None:
        if self.z_threshold <= 0 or self.min_population < 3:
            raise ValueError("z_threshold must be positive and min_population >= 3")
        if not self.high_participation > 0 or not 0 < self.low_completion <= 1:
            raise ValueError("thresholds: high_participation > 0 and 0 < low_completion <= 1")


def robust_z(values: pl.Expr) -> pl.Expr:
    """Modified z-score of each value against the column's median and MAD (null if MAD = 0)."""
    median = values.median()
    mad = (values - median).abs().median()
    return pl.when(mad > 0).then(MAD_TO_Z * (values - median) / mad).otherwise(None)


def _fmt(expr: pl.Expr, decimals: int = 1) -> pl.Expr:
    return expr.round(decimals).cast(pl.String)


def add_comparison(
    frame: pl.DataFrame,
    name: str,
    values: pl.Expr,
    config: FlagConfig,
    *,
    peer_group: str | None,
) -> pl.DataFrame:
    """Add ``z_{name}`` / ``median_{name}`` / ``peers_{name}`` for one metric.

    The comparison group is ``peer_group`` (e.g. the order's symbol) when that
    group has at least ``min_population`` values, otherwise all rows. ``z`` is
    null when the chosen group is too small or has zero MAD.
    """
    data = frame.with_columns(values.alias("_v"))
    pooled = data["_v"].drop_nulls()
    pooled_ok = pooled.len() >= config.min_population
    pooled_median = pooled.median() if pooled.len() else None
    pooled_mad = (pooled - pooled_median).abs().median() if pooled.len() else None
    if peer_group is not None and peer_group in frame.columns:
        data = data.with_columns(
            pl.col("_v").count().over(peer_group).alias("_n"),
            pl.col("_v").median().over(peer_group).alias("_group_median"),
        ).with_columns(
            (pl.col("_v") - pl.col("_group_median")).abs().median().over(peer_group).alias("_mad")
        )
        use_group = pl.col("_n") >= config.min_population
        peers = pl.format("{} orders", pl.col(peer_group).cast(pl.String))
    else:
        data = data.with_columns(
            pl.lit(0).alias("_n"),
            pl.lit(None, pl.Float64).alias("_group_median"),
            pl.lit(None, pl.Float64).alias("_mad"),
        )
        use_group = pl.lit(False)
        peers = pl.lit(ALL_ORDERS)
    median = pl.when(use_group).then(pl.col("_group_median")).otherwise(pl.lit(pooled_median))
    mad = pl.when(use_group).then(pl.col("_mad")).otherwise(pl.lit(pooled_mad))
    assessable = (use_group | pl.lit(pooled_ok)) & (mad > 0)
    return data.with_columns(
        pl.when(assessable).then(MAD_TO_Z * (pl.col("_v") - median) / mad).alias(f"z_{name}"),
        median.alias(f"median_{name}"),
        pl.when(use_group).then(peers).otherwise(pl.lit(ALL_ORDERS)).alias(f"peers_{name}"),
    ).drop("_v", "_n", "_group_median", "_mad")


def _breach(name: str, config: FlagConfig, *, high: bool) -> pl.Expr:
    z = pl.col(f"z_{name}")
    return (z >= config.z_threshold if high else z <= -config.z_threshold).fill_null(False)


def flag_orders(
    orders: pl.DataFrame, fills: pl.DataFrame, config: FlagConfig | None = None
) -> pl.DataFrame:
    """Append flag columns to order results.

    Adds ``flags`` / ``flag_reasons`` (execution and data-quality issues),
    ``context_flags`` / ``context_notes`` (coincident market conditions),
    ``n_flags`` and ``is_flagged``. ``orders`` is the order-level TCA table
    with diagnostic features; ``fills`` is the fill-level TCA table (used for
    data-quality flags).
    """
    config = config or FlagConfig()
    h = config.markout_horizon_seconds
    fill_quality = fills.group_by("order_id").agg(
        (pl.col("fill_quote_status") == ContextStatus.STALE.value).sum().alias("_stale_fills"),
        (pl.col("fill_quote_status") == ContextStatus.MISSING.value).sum().alias("_missing_fills"),
    )
    frame = orders.join(fill_quality, on="order_id", how="left").with_columns(
        pl.col("_stale_fills").fill_null(0), pl.col("_missing_fills").fill_null(0)
    )
    mid_markout = f"mid_markout_{h}s_bps"
    peer = config.peer_group
    comparisons = [
        ("arrival_cost", pl.col("arrival_cost_bps"), peer),
        ("vwap_cost", pl.col("vwap_cost_bps"), peer),
        ("effective_spread", pl.col("effective_spread_bps"), peer),
        ("mid_markout", pl.col(mid_markout), peer),
        ("volatility", pl.col("pre_arrival_volatility_bps"), None),
        ("arrival_spread", pl.col("arrival_spread_bps"), None),
        ("volume_rate", pl.col("market_volume_per_minute").log1p(), None),
    ]
    for name, values, group in comparisons:
        frame = add_comparison(frame, name, values, config, peer_group=group)

    def execution_reason(label: str, column: str, name: str) -> pl.Expr:
        return pl.format(
            "{} {} bps is unusually high compared with {} (median {} bps, robust z {}).",
            pl.lit(label),
            _fmt(pl.col(column)),
            pl.col(f"peers_{name}"),
            _fmt(pl.col(f"median_{name}")),
            _fmt(pl.col(f"z_{name}")),
        )

    rules: list[tuple[FlagCode, pl.Expr, pl.Expr]] = [
        (
            FlagCode.HIGH_ARRIVAL_COST,
            _breach("arrival_cost", config, high=True) & (pl.col("arrival_cost_bps") > 0),
            execution_reason("Arrival cost", "arrival_cost_bps", "arrival_cost"),
        ),
        (
            FlagCode.HIGH_VWAP_COST,
            _breach("vwap_cost", config, high=True) & (pl.col("vwap_cost_bps") > 0),
            execution_reason("Cost versus interval VWAP", "vwap_cost_bps", "vwap_cost"),
        ),
        (
            FlagCode.HIGH_EFFECTIVE_SPREAD,
            _breach("effective_spread", config, high=True) & (pl.col("effective_spread_bps") > 0),
            execution_reason("Effective spread", "effective_spread_bps", "effective_spread"),
        ),
        (
            FlagCode.ADVERSE_MARKOUT,
            _breach("mid_markout", config, high=False) & (pl.col(mid_markout) < 0),
            pl.format(
                "Mid price moved {} bps against the order within {} s of its fills; unusual "
                "compared with {} (median {} bps, robust z {}).",
                _fmt(pl.col(mid_markout).abs()),
                pl.lit(str(h)),
                pl.col("peers_mid_markout"),
                _fmt(pl.col("median_mid_markout")),
                _fmt(pl.col("z_mid_markout")),
            ),
        ),
        (
            FlagCode.HIGH_PARTICIPATION,
            (pl.col("participation_rate") >= config.high_participation).fill_null(False),
            pl.format(
                "Order was {}% of market volume in its interval (threshold {}%).",
                _fmt(pl.col("participation_rate") * 100, 0),
                pl.lit(f"{config.high_participation * 100:g}"),
            ),
        ),
        (
            FlagCode.LOW_COMPLETION,
            pl.col("completion_ratio") < config.low_completion,
            pl.format(
                "Only {}% of the order was executed (threshold {}%).",
                _fmt(pl.col("completion_ratio") * 100, 0),
                pl.lit(f"{config.low_completion * 100:g}"),
            ),
        ),
        (
            FlagCode.HIGH_VOLATILITY,
            _breach("volatility", config, high=True),
            pl.format(
                "Execution coincided with elevated short-term volatility: {} bps per 10 s "
                "before arrival (median of all orders {} bps).",
                _fmt(pl.col("pre_arrival_volatility_bps"), 2),
                _fmt(pl.col("median_volatility"), 2),
            ),
        ),
        (
            FlagCode.WIDE_SPREAD,
            _breach("arrival_spread", config, high=True),
            pl.format(
                "Execution coincided with a wide quoted spread at arrival: {} bps "
                "(median of all orders {} bps).",
                _fmt(pl.col("arrival_spread_bps")),
                _fmt(pl.col("median_arrival_spread")),
            ),
        ),
        (
            FlagCode.LOW_LIQUIDITY,
            _breach("volume_rate", config, high=False),
            pl.format(
                "Execution coincided with thin trading: {} shares/min in its interval "
                "(median of all orders {}).",
                _fmt(pl.col("market_volume_per_minute"), 0),
                _fmt(pl.col("median_volume_rate").exp() - 1, 0),
            ),
        ),
        (
            FlagCode.STALE_MARKET_DATA,
            (pl.col("arrival_quote_status") == ContextStatus.STALE.value)
            | (pl.col("_stale_fills") > 0),
            pl.format(
                "Stale market context (arrival quote: {}; fills with a stale quote: {}); "
                "affected metrics are unavailable.",
                pl.col("arrival_quote_status"),
                pl.col("_stale_fills"),
            ),
        ),
        (
            FlagCode.MISSING_MARKET_DATA,
            (pl.col("arrival_quote_status") == ContextStatus.MISSING.value)
            | (pl.col("_missing_fills") > 0)
            | (pl.col("benchmark_end").is_not_null() & pl.col("market_volume").is_null()),
            pl.format(
                "Missing market data (arrival quote: {}; fills without any prior quote: {}; "
                "trade data for the interval: {}).",
                pl.col("arrival_quote_status"),
                pl.col("_missing_fills"),
                pl.when(pl.col("market_volume").is_null())
                .then(pl.lit("none"))
                .otherwise(pl.lit("available")),
            ),
        ),
    ]

    def codes_and_texts(context: bool) -> tuple[pl.Expr, pl.Expr]:
        selected = [r for r in rules if (r[0] in CONTEXT_FLAGS) == context]
        codes = pl.concat_list(
            [pl.when(cond).then(pl.lit(code.value)).otherwise(None) for code, cond, _ in selected]
        ).list.drop_nulls()
        texts = pl.concat_list(
            [pl.when(cond).then(text).otherwise(None) for _, cond, text in selected]
        ).list.drop_nulls()
        return codes, texts

    flags, reasons = codes_and_texts(context=False)
    context_flags, context_notes = codes_and_texts(context=True)
    return (
        frame.with_columns(
            flags.alias("flags"),
            reasons.alias("flag_reasons"),
            context_flags.alias("context_flags"),
            context_notes.alias("context_notes"),
        )
        .with_columns(
            pl.col("flags").list.len().alias("n_flags"),
            (pl.col("flags").list.len() > 0).alias("is_flagged"),
        )
        .drop(
            "_stale_fills",
            "_missing_fills",
            *[f"{kind}_{name}" for name, _, _ in comparisons for kind in ("z", "median", "peers")],
        )
    )


def flag_counts(flagged_orders: pl.DataFrame) -> pl.DataFrame:
    """How many orders carry each flag or context note (every code listed, zero included)."""
    counts = (
        flagged_orders.select(
            pl.concat_list("flags", "context_flags").explode(empty_as_null=True).alias("flag")
        )
        .drop_nulls()
        .group_by("flag")
        .len()
    )
    codes = pl.DataFrame(
        {
            "flag": [c.value for c in FlagCode],
            "category": [flag_category(c) for c in FlagCode],
            "description": [FLAG_DESCRIPTIONS[c] for c in FlagCode],
        }
    )
    return (
        codes.join(counts, on="flag", how="left")
        .with_columns(pl.col("len").fill_null(0).alias("orders"))
        .drop("len")
        .with_columns(
            (pl.col("orders") / max(flagged_orders.height, 1)).alias("fraction_of_orders")
        )
    )
