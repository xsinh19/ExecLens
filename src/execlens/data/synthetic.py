"""Deterministic synthetic Level-1 market and parent-order generator.

This is a deliberately simple, explainable market model. It exists for
reproducibility, automated tests, benchmarks, dashboard demos and controlled
strategy comparisons. It is **not** an exchange-grade simulator and its output
is never real market data.

Model, per symbol and trading session
-------------------------------------
Quotes
    An opening quote at the session open, then updates arriving as a Poisson
    process with rate ``quote_rate_per_second``. Between updates the latent
    mid follows a log random walk with drift::

        log m(t + dt) = log m(t) + mu * dt + sigma * sqrt(dt) * Z,   Z ~ N(0, 1)

    with ``mu = daily_drift / 23,400`` and
    ``sigma = daily_volatility / sqrt(23,400)`` (a trading day is 6.5 hours =
    23,400 seconds). The quoted spread is ``mid * spread_bps / 10,000 * M``
    with a mean-one log-normal multiplier ``M``, rounded to whole ticks (at
    least one), and bid/ask sit on the tick grid around the latent mid.
    Displayed sizes are log-normal round lots around ``mean_depth``.

Trades
    Arrivals follow a Poisson process whose intensity has an intraday U-shape
    (busier near the open and close) multiplied by per-session noise on
    15-minute buckets, so no two sessions have the same volume profile. Each
    trade prints at the ask (buyer-initiated), bid (seller-initiated) or
    midpoint of the latest quote at or before the trade time.

Parent orders
    Random arrival time, side, strategy label and horizon; quantity is a
    log-normally distributed fraction of the *expected* market volume over the
    order horizon.

Determinism
-----------
Every draw comes from ``SeedSequence(seed, spawn_key=(stream, crc32(symbol),
session_index))``: the same configuration and seed reproduce the same data,
symbol streams do not depend on list order, and the market, order and
historical-profile streams never share random numbers.
"""

from __future__ import annotations

import math
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final

import numpy as np
import numpy.typing as npt
import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from execlens.data.schemas import ORDER_SCHEMA, QUOTE_SCHEMA, TRADE_SCHEMA

TRADING_DAY_SECONDS: Final = 23_400
SESSION_OPEN_UTC: Final = time(14, 30)
DEFAULT_START_DATE: Final = date(2026, 1, 5)
PROFILE_NOISE_BUCKET_SECONDS: Final = 900
MICROS: Final = 1_000_000

# Independent random streams.
_STREAM_MARKET: Final = 1
_STREAM_ORDERS: Final = 2
_STREAM_HISTORY: Final = 3

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Scenario(_Frozen):
    """Parameters of one market regime."""

    name: str
    description: str
    initial_price: float = Field(gt=0)
    daily_volatility: float = Field(ge=0, description="Std. dev. of log return per trading day.")
    daily_drift: float = Field(description="Expected log return per trading day.")
    spread_bps: float = Field(gt=0, description="Mean quoted spread in basis points.")
    spread_dispersion: float = Field(default=0.3, ge=0)
    quote_rate_per_second: float = Field(gt=0)
    trade_rate_per_second: float = Field(gt=0)
    mean_trade_size: float = Field(gt=0)
    mean_depth: float = Field(gt=0, description="Mean displayed size at the touch (shares).")
    tick_size: float = Field(default=0.01, gt=0)
    lot_size: int = Field(default=100, gt=0)
    volume_profile_amplitude: float = Field(default=2.0, ge=0)
    volume_profile_noise: float = Field(default=0.25, ge=0)
    midpoint_trade_fraction: float = Field(default=0.1, ge=0, le=1)

    @field_validator("tick_size")
    @classmethod
    def _tick_divides_unit(cls, value: float) -> float:
        ticks_per_unit = 1.0 / value
        if abs(ticks_per_unit - round(ticks_per_unit)) > 1e-9:
            raise ValueError("tick_size must divide 1.0 exactly (e.g. 0.01, 0.005, 0.05)")
        return value

    @property
    def ticks_per_unit(self) -> int:
        return round(1.0 / self.tick_size)


SCENARIOS: Final[dict[str, Scenario]] = {
    s.name: s
    for s in (
        Scenario(
            name="normal",
            description="Liquid, stable market: tight spreads, steady volume, no drift.",
            initial_price=100.0,
            daily_volatility=0.015,
            daily_drift=0.0,
            spread_bps=3.0,
            quote_rate_per_second=4.0,
            trade_rate_per_second=2.0,
            mean_trade_size=200,
            mean_depth=1_500,
        ),
        Scenario(
            name="high_volatility",
            description="Volatile market: ~3x normal volatility, wider spreads, busier tape.",
            initial_price=80.0,
            daily_volatility=0.05,
            daily_drift=0.0,
            spread_bps=6.0,
            quote_rate_per_second=8.0,
            trade_rate_per_second=3.0,
            mean_trade_size=200,
            mean_depth=1_000,
        ),
        Scenario(
            name="low_liquidity",
            description="Thin market: sparse quotes and trades, small displayed size.",
            initial_price=40.0,
            daily_volatility=0.025,
            daily_drift=0.0,
            spread_bps=15.0,
            quote_rate_per_second=0.5,
            trade_rate_per_second=0.25,
            mean_trade_size=100,
            mean_depth=300,
        ),
        Scenario(
            name="wide_spread",
            description="Wide quoted spread (~40 bps) with moderate volume.",
            initial_price=25.0,
            daily_volatility=0.015,
            daily_drift=0.0,
            spread_bps=40.0,
            quote_rate_per_second=2.0,
            trade_rate_per_second=1.0,
            mean_trade_size=300,
            mean_depth=1_500,
        ),
        Scenario(
            name="trend_up",
            description="Liquid market with a persistent upward drift (+3% per day).",
            initial_price=120.0,
            daily_volatility=0.015,
            daily_drift=0.03,
            spread_bps=3.0,
            quote_rate_per_second=4.0,
            trade_rate_per_second=2.0,
            mean_trade_size=200,
            mean_depth=1_500,
        ),
        Scenario(
            name="trend_down",
            description="Liquid market with a persistent downward drift (-3% per day).",
            initial_price=120.0,
            daily_volatility=0.015,
            daily_drift=-0.03,
            spread_bps=3.0,
            quote_rate_per_second=4.0,
            trade_rate_per_second=2.0,
            mean_trade_size=200,
            mean_depth=1_500,
        ),
    )
}


class Instrument(_Frozen):
    """A synthetic symbol and the scenario that drives it."""

    symbol: str = Field(min_length=1)
    scenario: str

    @field_validator("scenario")
    @classmethod
    def _known_scenario(cls, value: str) -> str:
        if value not in SCENARIOS:
            raise ValueError(f"unknown scenario {value!r}; choose from {sorted(SCENARIOS)}")
        return value

    @property
    def params(self) -> Scenario:
        return SCENARIOS[self.scenario]


DEFAULT_UNIVERSE: Final = (
    Instrument(symbol="SYN_NORMAL", scenario="normal"),
    Instrument(symbol="SYN_HIVOL", scenario="high_volatility"),
    Instrument(symbol="SYN_ILLIQ", scenario="low_liquidity"),
    Instrument(symbol="SYN_WIDE", scenario="wide_spread"),
    Instrument(symbol="SYN_UP", scenario="trend_up"),
    Instrument(symbol="SYN_DOWN", scenario="trend_down"),
)


class MarketConfig(_Frozen):
    """What to generate: which instruments, how many sessions, which seed."""

    universe: tuple[Instrument, ...] = DEFAULT_UNIVERSE
    start_date: date = DEFAULT_START_DATE
    n_sessions: int = Field(default=1, ge=1)
    session_seconds: int = Field(default=TRADING_DAY_SECONDS, ge=60)
    seed: int = Field(default=7, ge=0)

    @model_validator(mode="after")
    def _unique_symbols(self) -> MarketConfig:
        symbols = [i.symbol for i in self.universe]
        if len(symbols) != len(set(symbols)):
            raise ValueError("universe symbols must be unique")
        return self

    def session_opens(self) -> list[datetime]:
        """Session open timestamps (UTC) on consecutive business days."""
        opens: list[datetime] = []
        day = self.start_date
        while len(opens) < self.n_sessions:
            if day.weekday() < 5:
                opens.append(datetime.combine(day, SESSION_OPEN_UTC, tzinfo=UTC))
            day += timedelta(days=1)
        return opens


class OrderConfig(_Frozen):
    """How synthetic parent orders are drawn."""

    orders_per_symbol_per_session: int = Field(default=40, ge=0)
    strategy_mix: dict[str, float] = Field(
        default_factory=lambda: {"IMMEDIATE": 0.15, "TWAP": 0.35, "VWAP": 0.30, "POV": 0.20}
    )
    horizons_seconds: tuple[int, ...] = (300, 600, 900, 1800)
    immediate_sizing_seconds: int = Field(default=120, gt=0)
    participation_median: float = Field(default=0.05, gt=0, le=1)
    participation_dispersion: float = Field(default=0.75, ge=0)
    edge_buffer_seconds: int = Field(default=300, ge=0)

    @field_validator("strategy_mix")
    @classmethod
    def _valid_mix(cls, value: dict[str, float]) -> dict[str, float]:
        if not value or any(w < 0 for w in value.values()) or sum(value.values()) <= 0:
            raise ValueError("strategy_mix needs non-negative weights with a positive sum")
        return value

    @field_validator("horizons_seconds")
    @classmethod
    def _valid_horizons(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or any(h <= 0 for h in value):
            raise ValueError("horizons_seconds must be positive")
        return value


def _rng(seed: int, stream: int, symbol: str, session_index: int) -> np.random.Generator:
    key = (stream, zlib.crc32(symbol.encode("utf-8")), session_index)
    return np.random.default_rng(np.random.SeedSequence(seed, spawn_key=key))


def _mean_one_lognormal(rng: np.random.Generator, sigma: float, size: int) -> FloatArray:
    """Log-normal multipliers with E[M] = 1."""
    return np.exp(sigma * rng.standard_normal(size) - 0.5 * sigma * sigma)


def _to_epoch_us(ts: datetime) -> int:
    delta = ts - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * MICROS + delta.microseconds


def trade_intensity(
    scenario: Scenario, session_seconds: int, rng: np.random.Generator
) -> FloatArray:
    """Expected number of trades in each one-second bin of a session.

    U-shaped intraday profile (mean one over the session) times per-session
    log-normal noise on 15-minute buckets, scaled by ``trade_rate_per_second``.
    """
    x = (np.arange(session_seconds, dtype=np.float64) + 0.5) / session_seconds
    amplitude = scenario.volume_profile_amplitude
    shape = (1.0 + amplitude * (2.0 * x - 1.0) ** 2) / (1.0 + amplitude / 3.0)
    n_buckets = math.ceil(session_seconds / PROFILE_NOISE_BUCKET_SECONDS)
    noise = _mean_one_lognormal(rng, scenario.volume_profile_noise, n_buckets)
    bucket = np.arange(session_seconds) // PROFILE_NOISE_BUCKET_SECONDS
    intensity: FloatArray = scenario.trade_rate_per_second * shape * noise[bucket]
    return intensity


def _trade_arrivals(
    scenario: Scenario, session_seconds: int, rng: np.random.Generator
) -> tuple[IntArray, IntArray]:
    """Trade offsets from the open (microseconds, sorted) and sizes (shares)."""
    counts = rng.poisson(trade_intensity(scenario, session_seconds, rng))
    seconds = np.repeat(np.arange(session_seconds, dtype=np.int64), counts)
    offsets = seconds * MICROS + rng.integers(0, MICROS, size=seconds.size, dtype=np.int64)
    offsets.sort(kind="stable")
    sizes = np.maximum(
        1, np.rint(scenario.mean_trade_size * _mean_one_lognormal(rng, 0.8, offsets.size))
    ).astype(np.int64)
    return offsets, sizes


@dataclass(frozen=True)
class _SessionMarket:
    quote_us: IntArray
    bid: FloatArray
    ask: FloatArray
    bid_size: IntArray
    ask_size: IntArray
    trade_us: IntArray
    trade_price: FloatArray
    trade_size: IntArray
    closing_mid: float


def _simulate_session(
    scenario: Scenario,
    open_us: int,
    session_seconds: int,
    start_price: float,
    rng: np.random.Generator,
) -> _SessionMarket:
    session_us = session_seconds * MICROS
    n_updates = int(rng.poisson(scenario.quote_rate_per_second * session_seconds))
    updates = np.sort(rng.integers(1, session_us, size=n_updates, dtype=np.int64))
    quote_offsets = np.concatenate([np.zeros(1, dtype=np.int64), updates])

    dt = np.diff(quote_offsets, prepend=0).astype(np.float64) / MICROS
    mu = scenario.daily_drift / TRADING_DAY_SECONDS
    sigma = scenario.daily_volatility / math.sqrt(TRADING_DAY_SECONDS)
    shocks = rng.standard_normal(quote_offsets.size)
    log_mid = math.log(start_price) + np.cumsum(mu * dt + sigma * np.sqrt(dt) * shocks)
    mid = np.exp(log_mid)

    tpu = scenario.ticks_per_unit
    spread_mult = _mean_one_lognormal(rng, scenario.spread_dispersion, mid.size)
    spread_ticks = np.maximum(1, np.rint(mid * scenario.spread_bps / 1e4 * spread_mult * tpu))
    bid_ticks = np.maximum(1, np.floor(mid * tpu - spread_ticks / 2.0 + 0.5))
    ask_ticks = bid_ticks + spread_ticks
    # Dividing integer tick counts by ticks-per-unit yields the correctly
    # rounded decimal price (e.g. 10001 / 100 == 100.01 exactly as a literal).
    bid = bid_ticks / tpu
    ask = ask_ticks / tpu

    lot = scenario.lot_size
    lots_mean = scenario.mean_depth / lot
    bid_size = lot * np.maximum(1, np.rint(lots_mean * _mean_one_lognormal(rng, 0.5, mid.size)))
    ask_size = lot * np.maximum(1, np.rint(lots_mean * _mean_one_lognormal(rng, 0.5, mid.size)))

    trade_offsets, trade_size = _trade_arrivals(scenario, session_seconds, rng)
    prevailing = np.searchsorted(quote_offsets, trade_offsets, side="right") - 1
    u = rng.random(trade_offsets.size)
    frac_mid = scenario.midpoint_trade_fraction
    buyer_initiated = u < frac_mid + (1.0 - frac_mid) / 2.0
    trade_price = np.where(
        u < frac_mid,
        (bid[prevailing] + ask[prevailing]) / 2.0,
        np.where(buyer_initiated, ask[prevailing], bid[prevailing]),
    )

    return _SessionMarket(
        quote_us=open_us + quote_offsets,
        bid=bid,
        ask=ask,
        bid_size=bid_size.astype(np.int64),
        ask_size=ask_size.astype(np.int64),
        trade_us=open_us + trade_offsets,
        trade_price=trade_price,
        trade_size=trade_size,
        closing_mid=float(mid[-1]),
    )


def _epoch_us_to_utc(column: str) -> pl.Expr:
    return pl.from_epoch(pl.col(column), time_unit="us").dt.replace_time_zone("UTC")


@dataclass(frozen=True)
class MarketData:
    quotes: pl.DataFrame
    trades: pl.DataFrame


def generate_market(config: MarketConfig) -> MarketData:
    """Generate quotes and market trades for every instrument and session."""
    quote_parts: list[pl.DataFrame] = []
    trade_parts: list[pl.DataFrame] = []
    opens = config.session_opens()
    for instrument in config.universe:
        scenario = instrument.params
        price = scenario.initial_price
        for index, session_open in enumerate(opens):
            rng = _rng(config.seed, _STREAM_MARKET, instrument.symbol, index)
            session = _simulate_session(
                scenario, _to_epoch_us(session_open), config.session_seconds, price, rng
            )
            price = session.closing_mid
            quote_parts.append(
                pl.DataFrame(
                    {
                        "timestamp": session.quote_us,
                        "bid_price": session.bid,
                        "ask_price": session.ask,
                        "bid_size": session.bid_size,
                        "ask_size": session.ask_size,
                    }
                ).with_columns(pl.lit(instrument.symbol).alias("symbol"))
            )
            trade_parts.append(
                pl.DataFrame(
                    {
                        "timestamp": session.trade_us,
                        "price": session.trade_price,
                        "size": session.trade_size,
                    }
                ).with_columns(pl.lit(instrument.symbol).alias("symbol"))
            )

    quotes = (
        pl.concat(quote_parts)
        .with_columns(_epoch_us_to_utc("timestamp"))
        .select(list(QUOTE_SCHEMA))
        .sort("symbol", "timestamp", maintain_order=True)
    )
    trades = (
        pl.concat(trade_parts)
        .with_columns(_epoch_us_to_utc("timestamp"))
        .select(list(TRADE_SCHEMA))
        .sort("symbol", "timestamp", maintain_order=True)
    )
    return MarketData(quotes=quotes, trades=trades)


def generate_orders(config: MarketConfig, order_config: OrderConfig) -> pl.DataFrame:
    """Draw parent orders (without fills) for every instrument and session."""
    strategies = sorted(order_config.strategy_mix)
    weights = np.array([order_config.strategy_mix[s] for s in strategies], dtype=np.float64)
    weights /= weights.sum()
    horizons = np.array(order_config.horizons_seconds, dtype=np.float64)
    edge = float(order_config.edge_buffer_seconds)
    if config.session_seconds <= 2 * edge + horizons.max():
        raise ValueError("session too short for the configured horizons and edge buffer")

    parts: list[pl.DataFrame] = []
    n = order_config.orders_per_symbol_per_session
    for instrument in config.universe:
        scenario = instrument.params
        for index, session_open in enumerate(config.session_opens()):
            rng = _rng(config.seed, _STREAM_ORDERS, instrument.symbol, index)
            strategy = rng.choice(np.array(strategies), size=n, p=weights)
            is_immediate = strategy == "IMMEDIATE"
            horizon = np.where(is_immediate, np.nan, rng.choice(horizons, size=n))
            latest = config.session_seconds - edge - np.where(is_immediate, 0.0, horizon)
            offset_s = rng.uniform(edge, latest)
            side = rng.choice(np.array(["BUY", "SELL"]), size=n)
            sizing = np.where(is_immediate, order_config.immediate_sizing_seconds, horizon)
            expected_volume = scenario.trade_rate_per_second * scenario.mean_trade_size * sizing
            participation = order_config.participation_median * np.exp(
                order_config.participation_dispersion * rng.standard_normal(n)
            )
            lot = scenario.lot_size
            quantity = np.maximum(lot, np.rint(expected_volume * participation / lot) * lot)

            order = np.argsort(offset_s, kind="stable")
            day = session_open.strftime("%Y%m%d")
            parts.append(
                pl.DataFrame(
                    {
                        "order_id": [f"{instrument.symbol}-{day}-{k:04d}" for k in range(1, n + 1)],
                        "symbol": [instrument.symbol] * n,
                        "side": side[order].tolist(),
                        "quantity": quantity[order].astype(np.int64),
                        "arrival_timestamp": _to_epoch_us(session_open)
                        + np.floor(offset_s[order] * MICROS).astype(np.int64),
                        "execution_horizon_seconds": horizon[order],
                        "strategy": strategy[order].tolist(),
                    },
                    schema_overrides={"arrival_timestamp": pl.Int64},
                )
            )
    if not parts:
        return pl.DataFrame(schema=ORDER_SCHEMA)
    return (
        pl.concat(parts)
        .with_columns(
            _epoch_us_to_utc("arrival_timestamp"),
            pl.col("execution_horizon_seconds").fill_nan(None),
        )
        .select(list(ORDER_SCHEMA))
        .sort("arrival_timestamp", "order_id")
    )


def historical_volume_profile(
    instrument: Instrument,
    *,
    session_seconds: int,
    bucket_seconds: int,
    n_sessions: int = 20,
    seed: int = 7,
) -> FloatArray:
    """Average fraction of daily volume per time bucket over *prior* sessions.

    Built only from independently generated historical sessions (a separate
    random stream), never from the session being traded, so a VWAP schedule
    derived from it cannot peek at same-session realised volume.
    """
    if bucket_seconds <= 0 or n_sessions <= 0:
        raise ValueError("bucket_seconds and n_sessions must be positive")
    n_buckets = math.ceil(session_seconds / bucket_seconds)
    total = np.zeros(n_buckets, dtype=np.float64)
    for index in range(n_sessions):
        rng = _rng(seed, _STREAM_HISTORY, instrument.symbol, index)
        offsets, sizes = _trade_arrivals(instrument.params, session_seconds, rng)
        volume = np.bincount(
            offsets // (bucket_seconds * MICROS), weights=sizes, minlength=n_buckets
        )[:n_buckets]
        if volume.sum() > 0:
            total += volume / volume.sum()
    profile: FloatArray = total / total.sum()
    return profile
