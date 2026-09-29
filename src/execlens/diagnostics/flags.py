"""Deterministic, explainable execution flags.

Each flag is a transparent rule over one metric, so every flagged order comes
with a human-readable reason stating the value and the comparison point.

Statistical rules use the **modified z-score** (Iglewicz & Hoaglin), which is
robust to the heavy tails typical of execution costs::

    z = 0.6745 * (x - median(x)) / MAD(x),      MAD = median(|x - median(x)|)

computed over all analysed orders where the metric is available. The default
threshold is |z| >= 3.5. Statistical rules are skipped when fewer than
``min_population`` orders have the metric, or when the MAD is zero (no
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
    FlagCode.HIGH_ARRIVAL_COST: "Arrival cost unusually high versus the analysed orders.",
    FlagCode.HIGH_VWAP_COST: "Cost versus interval VWAP unusually high.",
    FlagCode.HIGH_EFFECTIVE_SPREAD: "Effective spread paid unusually high.",
    FlagCode.ADVERSE_MARKOUT: "Mid price moved unusually far against the order after its fills.",
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


def _statistical_rule(
    frame: pl.DataFrame,
    column: str,
    config: FlagConfig,
    *,
    high: bool,
    transform: pl.Expr | None = None,
) -> pl.Expr:
    """Boolean flag for |z| beyond the threshold in the adverse direction."""
    if frame[column].drop_nulls().len() < config.min_population:
        return pl.lit(False)
    source = transform if transform is not None else pl.col(column)
    z = robust_z(source)
    breach = z >= config.z_threshold if high else z <= -config.z_threshold
    return breach.fill_null(False)


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
    log_volume_rate = pl.col("market_volume_per_minute").log1p()

    def median(column: str) -> pl.Expr:
        return pl.col(column).median()

    rules: list[tuple[FlagCode, pl.Expr, pl.Expr]] = [
        (
            FlagCode.HIGH_ARRIVAL_COST,
            _statistical_rule(frame, "arrival_cost_bps", config, high=True)
            & (pl.col("arrival_cost_bps") > 0),
            pl.format(
                "Arrival cost {} bps is unusually high (median of analysed orders {} bps).",
                _fmt(pl.col("arrival_cost_bps")),
                _fmt(median("arrival_cost_bps")),
            ),
        ),
        (
            FlagCode.HIGH_VWAP_COST,
            _statistical_rule(frame, "vwap_cost_bps", config, high=True)
            & (pl.col("vwap_cost_bps") > 0),
            pl.format(
                "Cost versus interval VWAP {} bps is unusually high (median {} bps).",
                _fmt(pl.col("vwap_cost_bps")),
                _fmt(median("vwap_cost_bps")),
            ),
        ),
        (
            FlagCode.HIGH_EFFECTIVE_SPREAD,
            _statistical_rule(frame, "effective_spread_bps", config, high=True)
            & (pl.col("effective_spread_bps") > 0),
            pl.format(
                "Effective spread {} bps is unusually high (median {} bps).",
                _fmt(pl.col("effective_spread_bps")),
                _fmt(median("effective_spread_bps")),
            ),
        ),
        (
            FlagCode.ADVERSE_MARKOUT,
            _statistical_rule(frame, mid_markout, config, high=False) & (pl.col(mid_markout) < 0),
            pl.format(
                "Mid price moved {} bps against the order within {} s of its fills "
                "(median {} bps).",
                _fmt(pl.col(mid_markout).abs()),
                pl.lit(str(h)),
                _fmt(median(mid_markout)),
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
            _statistical_rule(frame, "pre_arrival_volatility_bps", config, high=True),
            pl.format(
                "Execution coincided with elevated short-term volatility: {} bps per 10 s "
                "before arrival (median {} bps).",
                _fmt(pl.col("pre_arrival_volatility_bps"), 2),
                _fmt(median("pre_arrival_volatility_bps"), 2),
            ),
        ),
        (
            FlagCode.WIDE_SPREAD,
            _statistical_rule(frame, "arrival_spread_bps", config, high=True),
            pl.format(
                "Execution coincided with a wide quoted spread at arrival: {} bps (median {} bps).",
                _fmt(pl.col("arrival_spread_bps")),
                _fmt(median("arrival_spread_bps")),
            ),
        ),
        (
            FlagCode.LOW_LIQUIDITY,
            _statistical_rule(
                frame, "market_volume_per_minute", config, high=False, transform=log_volume_rate
            ),
            pl.format(
                "Execution coincided with thin trading: {} shares/min in its interval (median {}).",
                _fmt(pl.col("market_volume_per_minute"), 0),
                _fmt(median("market_volume_per_minute"), 0),
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
        .drop("_stale_fills", "_missing_fills")
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
