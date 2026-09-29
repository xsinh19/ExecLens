import math
from datetime import date, datetime

import numpy as np
import polars as pl
import pytest
from pydantic import ValidationError

from execlens.data.synthetic import (
    DEFAULT_UNIVERSE,
    SCENARIOS,
    Instrument,
    MarketConfig,
    OrderConfig,
    Scenario,
    SessionWindow,
    _simulate_session,
    generate_market,
    generate_orders,
    generate_windows,
    historical_volume_profile,
)
from execlens.data.validation import validate_orders, validate_quotes, validate_trades

SHORT = MarketConfig(session_seconds=3_600, seed=11)
ORDERS = OrderConfig(orders_per_symbol_per_session=25, horizons_seconds=(300, 600, 900))


@pytest.fixture(scope="module")
def market() -> tuple[pl.DataFrame, pl.DataFrame]:
    m = generate_market(SHORT)
    return m.quotes, m.trades


def test_required_scenarios_exist() -> None:
    assert set(SCENARIOS) == {
        "normal",
        "high_volatility",
        "low_liquidity",
        "wide_spread",
        "trend_up",
        "trend_down",
    }


def test_same_config_and_seed_reproduce_identical_data(
    market: tuple[pl.DataFrame, pl.DataFrame],
) -> None:
    again = generate_market(SHORT)
    assert again.quotes.equals(market[0])
    assert again.trades.equals(market[1])
    assert generate_orders(SHORT, ORDERS).equals(generate_orders(SHORT, ORDERS))


def test_different_seed_gives_a_different_realisation(
    market: tuple[pl.DataFrame, pl.DataFrame],
) -> None:
    other = generate_market(SHORT.model_copy(update={"seed": 12}))
    assert not other.quotes.equals(market[0])
    assert not generate_orders(SHORT.model_copy(update={"seed": 12}), ORDERS).equals(
        generate_orders(SHORT, ORDERS)
    )


def test_symbol_streams_do_not_depend_on_universe_composition(
    market: tuple[pl.DataFrame, pl.DataFrame],
) -> None:
    solo = generate_market(SHORT.model_copy(update={"universe": (DEFAULT_UNIVERSE[2],)}))
    symbol = DEFAULT_UNIVERSE[2].symbol
    assert solo.quotes.equals(market[0].filter(pl.col("symbol") == symbol))


def test_generated_market_passes_validation(market: tuple[pl.DataFrame, pl.DataFrame]) -> None:
    quotes, trades = market
    validate_quotes(quotes)
    validate_trades(trades)
    assert quotes.select((pl.col("bid_price") <= pl.col("ask_price")).all()).item()


def test_prices_lie_on_the_tick_grid(market: tuple[pl.DataFrame, pl.DataFrame]) -> None:
    quotes = market[0]
    cents = quotes.select(pl.col("bid_price", "ask_price") * 100)
    assert np.allclose(cents.to_numpy(), np.rint(cents.to_numpy()), atol=1e-9)


def test_trades_print_inside_the_prevailing_quote(
    market: tuple[pl.DataFrame, pl.DataFrame],
) -> None:
    quotes, trades = market
    joined = trades.sort("symbol", "timestamp").join_asof(
        quotes.sort("symbol", "timestamp").rename({"timestamp": "qts"}),
        left_on="timestamp",
        right_on="qts",
        by="symbol",
        check_sortedness=False,
    )
    assert joined["qts"].null_count() == 0
    inside = (pl.col("price") >= pl.col("bid_price") - 1e-9) & (
        pl.col("price") <= pl.col("ask_price") + 1e-9
    )
    assert joined.select(inside.all()).item()


def test_every_session_starts_with_an_opening_quote(
    market: tuple[pl.DataFrame, pl.DataFrame],
) -> None:
    first = market[0].group_by("symbol").agg(pl.col("timestamp").min())
    open_ts = SHORT.session_opens()[0]
    assert (first["timestamp"] == open_ts).all()


def test_scenarios_have_the_intended_character(
    market: tuple[pl.DataFrame, pl.DataFrame],
) -> None:
    quotes, trades = market
    by_symbol = quotes.group_by("symbol").agg(
        (
            (pl.col("ask_price") - pl.col("bid_price"))
            / ((pl.col("ask_price") + pl.col("bid_price")) / 2)
            * 1e4
        )
        .mean()
        .alias("spread_bps")
    )
    spreads = dict(by_symbol.iter_rows())
    for instrument in DEFAULT_UNIVERSE:
        target = instrument.params.spread_bps
        assert spreads[instrument.symbol] == pytest.approx(target, rel=0.15)

    volume = dict(trades.group_by("symbol").agg(pl.col("size").sum()).iter_rows())
    assert volume["SYN_ILLIQ"] < volume["SYN_NORMAL"] / 5


def test_volatility_and_trend_scenarios() -> None:
    config = MarketConfig(
        universe=(
            Instrument(symbol="N", scenario="normal"),
            Instrument(symbol="V", scenario="high_volatility"),
            Instrument(symbol="U", scenario="trend_up"),
            Instrument(symbol="D", scenario="trend_down"),
        ),
        n_sessions=5,
        seed=3,
    )
    quotes = generate_market(config).quotes.with_columns(
        ((pl.col("bid_price") + pl.col("ask_price")) / 2).alias("mid")
    )
    # Sample the mid every 60 seconds and compare realised volatility.
    sampled = quotes.group_by_dynamic("timestamp", every="60s", group_by="symbol").agg(
        pl.col("mid").last()
    )
    vol = dict(
        sampled.group_by("symbol").agg(pl.col("mid").log().diff().std().alias("vol")).iter_rows()
    )
    assert vol["V"] > 2 * vol["N"]
    moves = dict(
        quotes.group_by("symbol")
        .agg((pl.col("mid").last() / pl.col("mid").first() - 1).alias("ret"))
        .iter_rows()
    )
    assert moves["U"] > 0.05
    assert moves["D"] < -0.05


def test_sessions_skip_weekends() -> None:
    config = MarketConfig(start_date=date(2026, 1, 9), n_sessions=2)  # a Friday
    opens = config.session_opens()
    assert [o.date() for o in opens] == [date(2026, 1, 9), date(2026, 1, 12)]
    assert all(o.hour == 14 and o.minute == 30 for o in opens)


def test_generated_orders_follow_their_configuration() -> None:
    orders = generate_orders(SHORT, ORDERS)
    validate_orders(orders)
    assert orders.height == 25 * len(DEFAULT_UNIVERSE)
    assert orders["order_id"].is_unique().all()
    immediate = orders.filter(pl.col("strategy") == "IMMEDIATE")
    others = orders.filter(pl.col("strategy") != "IMMEDIATE")
    assert immediate["execution_horizon_seconds"].null_count() == immediate.height
    assert others["execution_horizon_seconds"].is_in([300.0, 600.0, 900.0]).all()
    assert (orders["quantity"] % 100 == 0).all()
    assert (orders["quantity"] >= 100).all()
    assert set(orders["side"].unique()) == {"BUY", "SELL"}

    open_ts = SHORT.session_opens()[0]
    start = (orders["arrival_timestamp"] - open_ts).dt.total_seconds()
    end = start + orders["execution_horizon_seconds"].fill_null(0.0)
    assert (start >= 300).all()
    assert (end <= SHORT.session_seconds - 300).all()


def test_historical_profile_is_a_deterministic_u_shaped_distribution() -> None:
    instrument = DEFAULT_UNIVERSE[0]
    profile = historical_volume_profile(
        instrument, session_seconds=23_400, bucket_seconds=1_800, n_sessions=20, seed=5
    )
    again = historical_volume_profile(
        instrument, session_seconds=23_400, bucket_seconds=1_800, n_sessions=20, seed=5
    )
    assert np.array_equal(profile, again)
    assert profile.sum() == pytest.approx(1.0)
    assert profile[0] > profile[len(profile) // 2]
    assert profile[-1] > profile[len(profile) // 2]


def test_historical_profile_is_not_the_traded_session_profile() -> None:
    """The profile comes from separate prior sessions, not the session itself."""
    config = MarketConfig(universe=(DEFAULT_UNIVERSE[0],), seed=5)
    trades = generate_market(config).trades
    offsets = (trades["timestamp"] - config.session_opens()[0]).dt.total_seconds()
    realised = np.bincount(
        (offsets // 1_800).cast(pl.Int64).to_numpy(),
        weights=trades["size"].to_numpy(),
        minlength=13,
    )
    realised = realised / realised.sum()
    profile = historical_volume_profile(
        DEFAULT_UNIVERSE[0], session_seconds=23_400, bucket_seconds=1_800, seed=5
    )
    assert not np.allclose(profile, realised, atol=1e-3)


def test_invalid_configurations_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Instrument(symbol="X", scenario="no_such_scenario")
    with pytest.raises(ValidationError):
        MarketConfig(universe=(DEFAULT_UNIVERSE[0], DEFAULT_UNIVERSE[0]))
    with pytest.raises(ValidationError):
        SCENARIOS["normal"].model_copy(update={"tick_size": 0.03}).model_validate(
            SCENARIOS["normal"].model_dump() | {"tick_size": 0.03}
        )
    with pytest.raises(ValidationError):
        OrderConfig(strategy_mix={"TWAP": -1.0})
    with pytest.raises(ValueError, match="session too short"):
        generate_orders(MarketConfig(session_seconds=600), OrderConfig())


def test_scenario_ticks_per_unit() -> None:
    scenario = Scenario.model_validate(SCENARIOS["normal"].model_dump() | {"tick_size": 0.05})
    assert scenario.ticks_per_unit == 20


# --- session windows (strategy comparisons simulate only what an order needs) --


def seconds_after(frame: pl.DataFrame, session_open: datetime) -> pl.Series:
    return (frame["timestamp"] - session_open).dt.total_microseconds() / 1e6


def test_a_whole_session_window_is_the_generated_session() -> None:
    config = MarketConfig(universe=(DEFAULT_UNIVERSE[0],), session_seconds=3_600, seed=11)
    whole = SessionWindow(config.session_opens()[0], 0, 3_600)
    windowed = generate_windows(DEFAULT_UNIVERSE[0], [whole], session_seconds=3_600, seed=11)
    session = generate_market(config)
    assert windowed.quotes.equals(session.quotes)
    assert windowed.trades.equals(session.trades)


def test_windows_contain_only_their_seconds_and_pass_validation() -> None:
    opens = MarketConfig(n_sessions=2).session_opens()
    windows = [SessionWindow(opens[0], 7_200, 9_000), SessionWindow(opens[1], 600, 1_500)]
    market = generate_windows(DEFAULT_UNIVERSE[0], windows, seed=3)
    validate_quotes(market.quotes)
    validate_trades(market.trades)
    for window in windows:
        day = pl.col("timestamp").dt.date() == window.session_open.date()
        quotes = seconds_after(market.quotes.filter(day), window.session_open)
        trades = seconds_after(market.trades.filter(day), window.session_open)
        assert quotes.min() == window.start_seconds  # a quote at the window start
        assert quotes.max() < window.end_seconds
        assert trades.min() >= window.start_seconds
        assert trades.max() < window.end_seconds


def test_windows_keep_the_sessions_time_of_day_pattern() -> None:
    session_open = MarketConfig().session_opens()[0]
    near_open = SessionWindow(session_open, 0, 1_800)
    midday = SessionWindow(session_open, 10_800, 12_600)
    busy = generate_windows(DEFAULT_UNIVERSE[0], [near_open], seed=5).trades.height
    quiet = generate_windows(DEFAULT_UNIVERSE[0], [midday], seed=5).trades.height
    # U-shaped intensity: about 1.7x the daily mean near the open, 0.6x at midday.
    assert busy > 2 * quiet


def test_a_window_starts_where_the_random_walk_would_be() -> None:
    # No volatility, strong drift: the first mid of a window starting 11,700 s
    # (half a trading day) after the open is initial * exp(drift / 2).
    scenario = SCENARIOS["normal"].model_copy(update={"daily_volatility": 0.0, "daily_drift": 0.2})
    session = _simulate_session(
        scenario, 0, 23_400, 100.0, np.random.default_rng(1), window=(11_700, 12_000)
    )
    first_mid = (session.bid[0] + session.ask[0]) / 2
    assert first_mid == pytest.approx(100.0 * math.exp(0.1), abs=0.01)


@pytest.mark.parametrize("window", [(100, 100), (-1, 50), (0, 3_601)])
def test_windows_outside_the_session_are_rejected(window: tuple[int, int]) -> None:
    with pytest.raises(ValueError, match="window"):
        _simulate_session(
            SCENARIOS["normal"], 0, 3_600, 100.0, np.random.default_rng(1), window=window
        )
