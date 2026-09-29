# ExecLens Metrics Reference

This document defines every execution metric ExecLens computes: the formula,
the sign convention, a worked example, how to interpret it and its limitations.
The code that implements each formula is `src/execlens/tca/metrics.py`
(formulas), `src/execlens/tca/alignment.py` (point-in-time market context) and
`src/execlens/tca/engine.py` (aggregation). The same numbers are recomputed by
the loop-based reference implementation in `src/execlens/tca/reference.py`,
and the worked examples below are the unit tests in
`tests/unit/test_tca_formulas.py`.

> All market data in ExecLens is **synthetic** and all executions are
> **simulated**. The metrics are standard, but nothing here describes a real
> market or real trading.

---

## 1. Conventions used everywhere

| Convention | Definition |
|---|---|
| Side sign | `side_sign = +1` for **BUY**, `-1` for **SELL** |
| **Cost metrics** (arrival cost, VWAP cost, effective spread) | **positive = worse** execution for the order, negative = better |
| **Markouts** | **positive = favourable** subsequent move for the order (the *opposite* orientation to costs) |
| Basis point | 1 bp = 0.01% = 1/10,000; `x_bps = x_fraction * 10,000` |
| Timestamps | timezone-aware UTC, microsecond precision |
| Quantities | integer shares |

The side sign makes one formula serve both directions: for a buy, paying *more*
than a benchmark is bad; for a sell, receiving *less* is bad. Multiplying the
price difference by `side_sign` maps both to "positive = bad".

---

## 2. Point-in-time market context (no look-ahead)

Every contemporaneous metric needs "the market at time `t`". ExecLens uses the
**latest quote whose timestamp is `<= t`** for the same symbol, found with a
backward **as-of join**. It never uses the *nearest* quote, because the nearest
quote may be in the future, which would leak information that was not
available when the order or fill happened.

| Situation | Status | Effect |
|---|---|---|
| a quote at or before `t`, no older than the stale tolerance (default **30 s**) | `OK` | prices used |
| latest prior quote older than the tolerance | `STALE` | prices withheld (null); dependent metrics unavailable; order flagged |
| no quote at or before `t` | `MISSING` | same as stale |

* A quote stamped exactly at `t` counts as prior (`<=`).
* Several quotes with the same timestamp: the last one in input order wins (it
  is the final state at that instant).
* Stale prices are never forward-filled. The tolerance is a data-quality
  policy, configurable per run (`TcaConfig.stale_quote_tolerance_seconds`,
  env `EXECLENS_STALE_QUOTE_TOLERANCE_SECONDS`, CLI `--stale-tolerance`).
  In real feeds an unchanged quote is still live, so for illiquid instruments
  the tolerance should be set wider.

Future market data is used in exactly one place, on purpose: **markouts** (§10).

Tests: `tests/unit/test_temporal_integrity.py`, `tests/unit/test_tca_edge_cases.py`.

---

## 3. Bid, ask and midpoint

**Definitions.** The *bid* is the best price at which someone is willing to
buy; the *ask* (offer) is the best price at which someone is willing to sell.
A buyer who wants to trade immediately pays the ask; a seller who wants to
trade immediately receives the bid. The *midpoint* is

```
mid = (bid + ask) / 2
```

**Example.** bid 99.98, ask 100.02 → mid 100.00.

**Interpretation.** The mid is the standard "fair value" reference for
execution analysis: it sits halfway between the prices at which you could buy
and sell right now.

**Limitations.** Level-1 data shows only the best price and the size displayed
there. The mid says nothing about depth beyond the touch, hidden liquidity or
where the next trade will print. A *locked* market (bid = ask) is valid; a
*crossed* market (bid > ask) is rejected by validation.

---

## 4. Basis points

```
value_bps = value_fraction * 10,000
```

**Example.** A cost of 0.08 on a 100.00 price is 0.0008 = 0.08% = **8 bps**.

**Interpretation.** Basis points make costs comparable across instruments with
very different prices: 2 cents on a $10 stock (20 bps) is far more expensive
than 2 cents on a $200 stock (1 bp).

**Limitations.** Basis points hide absolute money. A small bps cost on a huge
order can still be a large dollar amount; see the notional-weighted aggregate
(§13).

---

## 5. Quoted spread

```
spread     = ask - bid
spread_bps = spread / mid * 10,000
```

**Sign convention.** Always `>= 0` (crossed markets are rejected).

**Example.** bid 99.98, ask 100.02: spread 0.04, spread_bps = 0.04 / 100.00 * 10,000 = **4 bps**.

**Interpretation.** The round-trip price of immediacy: buying at the ask and
immediately selling at the bid loses one full spread. A marketable order pays
roughly *half* a spread relative to the mid.

**Where ExecLens reports it.**
* `arrival_spread`, `arrival_spread_bps`: the quote prevailing at order arrival.
* `avg_contemporaneous_spread_bps`: quantity-weighted average of the quoted
  spread (bps) prevailing at each fill.

**Limitations.** The quoted spread applies only to the displayed size; larger
orders typically pay more (market impact).

---

## 6. Quantity-weighted average fill price

```
average_fill_price = sum(fill_price_i * fill_quantity_i) / sum(fill_quantity_i)
```

**Example.** Fills of 100 @ 10.00, 300 @ 10.40 and 600 @ 10.10:
(1,000 + 3,120 + 6,060) / 1,000 = **10.18**.

**Interpretation.** The price actually achieved per share executed; the input
to arrival cost and VWAP cost.

**Limitations.** Unavailable (null) for an order with no fills. It says
nothing about the unfilled remainder of a partially filled order.

---

## 7. Arrival price and arrival cost (implementation shortfall)

**Arrival price.** The mid of the latest valid quote at or before the order's
`arrival_timestamp` (the moment the decision to trade reached the desk). It is
the price you could have hoped to trade at if execution were instantaneous and
free.

**Arrival cost.**

```
arrival_cost_bps = side_sign * (average_fill_price - arrival_mid) / arrival_mid * 10,000
```

**Sign convention.** Positive = worse (paid up / sold down relative to the
arrival mid).

**Examples** (spec examples, tested):
* BUY, arrival mid 100.00, average execution 100.10 → +1 * 0.10 / 100 * 10,000 = **+10 bps**.
* SELL, arrival mid 100.00, average execution 99.90 → -1 * (-0.10) / 100 * 10,000 = **+10 bps**.

**Worked example** (`hand_example` in the tests). A BUY of 300 arrives at t=5 s
when the mid is 100.00. It fills 100 @ 100.04 and 200 @ 100.10, so the average
is 30,024 / 300 = 100.08 and the arrival cost is (100.08 - 100.00) / 100.00 *
10,000 = **+8 bps**. A SELL in the same market fills 100 @ 100.00 and
200 @ 100.06, averaging 100.04, so its arrival cost is -1 * 0.04 / 100 * 10,000
= **-4 bps**. The market rose while it sold, so it did better than arrival.

**Interpretation.** The total cost of implementing the decision to trade:
spread paid, market impact, and the price drift during execution (timing
risk). It is the most common headline TCA metric.

**Relationship to implementation shortfall.** Perold's implementation
shortfall compares a paper portfolio traded at the decision price with the
real one. It includes an *opportunity cost* for shares that were never
executed. ExecLens reports the **execution component only**: arrival cost is
computed on executed shares, and unfilled quantity is surfaced separately
through the completion ratio (§12). Charging opportunity cost would need a
reference price for the unfilled remainder (for example, the close or the
cancel-time price). That choice is left out deliberately to keep the metric
unambiguous.

**Limitations.** It mixes skill with luck. A buy executed while the market
happens to fall looks cheap regardless of how well it was worked. That is why
ExecLens also reports VWAP cost, effective spread and markouts, and never
reads cost differences as causal.

---

## 8. Market VWAP and VWAP cost

**Interval.** The benchmark interval of an order is the closed interval

```
[arrival_timestamp, arrival_timestamp + execution_horizon_seconds]   if a horizon is given
[arrival_timestamp, last_fill_timestamp]                             otherwise
```

Trades stamped exactly at either endpoint are included. With no horizon and no
fills there is no interval, and VWAP is unavailable.

**Market VWAP.**

```
market_vwap = sum(trade_price_i * trade_size_i) / sum(trade_size_i)      over market trades in the interval
```

If no market volume traded in the interval, VWAP is **unavailable**: the
engine never divides by zero.

**VWAP cost.**

```
vwap_cost_bps = side_sign * (average_fill_price - market_vwap) / market_vwap * 10,000
```

**Sign convention.** Positive = worse than the market's volume-weighted price.

**Example.** Trades in [5, 65]: 100 @ 100.02, 300 @ 100.08, 200 @ 100.14.
VWAP = 60,054 / 600 = **100.09**. The BUY above averaged 100.08, so its VWAP
cost is (100.08 - 100.09) / 100.09 * 10,000 = **-0.999 bps**, slightly better
than VWAP. The SELL averaged 100.04: -1 * (100.04 - 100.09) / 100.09 * 10,000
= **+4.995 bps**, worse than VWAP.

**Interpretation.** "Did I trade at a price typical of the market while I was
trading?" It removes most of the market drift that pollutes arrival cost.

**Limitations.**
* If the order itself is a large share of interval volume, VWAP partly
  benchmarks the order against itself and cost looks artificially small.
  Read it together with participation (§11).
* VWAP can be gamed by trading passively along with volume; a good VWAP cost
  with a bad arrival cost means the order tracked a market that moved away.
* In ExecLens's synthetic market the tape does not contain the simulated
  fills, so market VWAP excludes the order's own trades.

**Implementation note.** Interval volume and notional are computed from
per-symbol running totals, so the amount traded in `[a, b]` is
`C(<= b) - C(< a)`, with both endpoints found by as-of joins. The reference
implementation instead slices the trade list with `bisect` and sums directly;
the two agree to floating-point tolerance.

---

## 9. Effective spread

For each fill, using the latest valid mid **at or before** the fill:

```
effective_spread_bps = 2 * side_sign * (fill_price - mid) / mid * 10,000
```

Order level: quantity-weighted mean over fills.

**Sign convention.** Positive = paid more than the mid (buy) or received less
(sell). Fills exactly at the mid give 0; price improvement inside the spread
in the order's favour gives a negative value.

**Examples.**
* BUY at the ask 100.02 with mid 100.00: 2 * 0.02 / 100 * 10,000 = **4 bps**,
  which equals the quoted spread (a fill at the touch).
* SELL at the bid 99.98 with mid 100.00: 2 * (-1) * (-0.02) / 100 * 10,000 = **4 bps**.
* Worked example: B1-1 at 100.04 vs mid 100.02 → 400 / 100.02 = 3.9992 bps;
  B1-2 at 100.10 vs mid 100.08 → 400 / 100.08 = 3.9968 bps; order value
  (100 * 3.9992 + 200 * 3.9968) / 300 = **3.9976 bps**.

**Interpretation.** The factor 2 converts the half-spread actually paid into a
spread comparable with the quoted spread. Effective below quoted means the
fills beat the displayed prices (price improvement). Effective above quoted
means the fills walked through the touch (impact).

**Limitations.** It measures cost against the contemporaneous mid only. It
ignores what happened afterwards (see markouts) and what the order did to the
market before the fill.

---

## 10. Markouts (post-trade price movement)

For a fill at time `t` and horizon `h` in {10 s, 30 s, 60 s, 300 s}:

```
markout_bps(h) = side_sign * (future_mid(t + h) - fill_price) / fill_price * 10,000
```

> **Sign convention: positive = FAVOURABLE.** The market subsequently moved in
> the order's direction: after a buy the mid rose above the price paid.
> This is the **opposite orientation to the cost metrics**, where positive
> means worse. The convention is repeated in the code docs, the API schema
> descriptions and the dashboard labels.

**Future-mid lookup methodology.** `future_mid(t + h)` is the mid of the
prevailing quote at `t + h`, that is, the latest quote with timestamp
`<= t + h` (a backward as-of join at the target time). The observation is
**available** only if

1. the symbol's quote data extends to at least `t + h` (some quote is stamped
   at or after the target); otherwise the markout is `MISSING`, and
2. that prevailing quote is no older than the stale tolerance at `t + h`;
   otherwise it is `STALE`.

This is the only place ExecLens uses data from after an event, and it does so
on purpose.

**Aggregation.** The order value is the quantity-weighted mean over fills, and
it is reported **only if every fill has an available observation** at that
horizon. Otherwise the value is null and `markout_{h}s_missing_fills` counts the
affected fills. Missing observations are never averaged away silently.

**Examples.**
* BUY at 100.02, mid 60 s later 100.10: +(0.08 / 100.02) * 10,000 = **+8.0 bps** (favourable).
* SELL at 99.98, mid later 100.10: -(0.12 / 99.98) * 10,000 = **-12.0 bps** (adverse).
* Worked example, B1 at 10 s: B1-1 bought at 100.04 and the mid at t22 is
  100.08, giving +400 / 100.04 = +3.998 bps; B1-2 bought at 100.10 and the mid
  at t35 is 100.08, giving -200 / 100.10 = -1.998 bps. The quantity-weighted
  order markout is (100 * 3.998 - 200 * 1.998) / 300 = +0.0008 bps.

**Interpretation.** Markouts reveal what the fill "knew". Consistently negative
markouts after buys mean the order bought just before prices fell. That is
the signature of adverse selection or of temporary impact reverting. Short
horizons (10–60 s) show microstructure effects; longer horizons (300 s) are
dominated by general market moves.

**Limitations.** Long-horizon markouts are noisy (market volatility swamps
execution effects), and fills near the end of the data have no observation.
In ExecLens's synthetic market simulated fills have no permanent impact, so
negative markouts come from paying the spread plus temporary impact, which
the future mid does not include.

---

## 11. Participation rate and quantity / market volume

```
participation_rate        = executed_quantity  / market_volume_in_interval
quantity_to_market_volume = requested_quantity / market_volume_in_interval
```

Both are unavailable when market volume in the interval is zero or the interval
is undefined.

**Example.** An order executes 300 shares while 600 shares trade in its
interval, so participation = **0.5 (50%)**.

**Interpretation.** How much of the market the order was. High participation
(the dashboard highlights above 25%) is associated with larger market impact
and with VWAP benchmarks that partly measure the order itself. A POV strategy
targets a fixed participation rate.

**Limitations.** In ExecLens's synthetic market the order's own fills are not
printed to the tape, so `market_volume` excludes them. Participation is then
"our shares per share traded by others". In real data, where your own prints
are on the tape, the conventional denominator includes them.

---

## 12. Completion ratio, fill rate and duration

```
completion_ratio       = executed_quantity / requested_quantity
full_fill_rate         = share of orders with completion_ratio = 1      (portfolio level)
order_duration_seconds = last_fill_timestamp - arrival_timestamp
```

**Example.** A 1,000-share order with fills totalling 400 has a completion of
**0.4**. Its arrival cost is computed on the 400 executed shares only.

**Interpretation.** Low completion is a hidden cost: the unexecuted shares
still have to be traded later, possibly at worse prices (opportunity cost).
Cheap but incomplete executions must not be read as good executions.

**Limitations.** A requested quantity that was deliberately cancelled looks the
same as one the strategy failed to execute; ExecLens has no cancel events.

---

## 13. Portfolio-level aggregation

`src/execlens/tca/aggregation.py` reports, for each metric, the number of
orders where it is available and its **median**, **mean** and **p95**
(linear interpolation between order statistics). It also reports:

* **notional-weighted arrival cost** =
  `sum(arrival_cost_bps_i * executed_notional_i) / sum(executed_notional_i)`,
  which is what the whole book paid, rather than what the typical order paid;
* **full fill rate** and flagged-order fraction.

Example (tested): order costs 10, -2, 4 and 100 bps on notionals 1,000,
3,000, 500 and 500 give a simple mean of 28 bps, a median of 7 bps and a
notional-weighted mean of 56,000 / 5,000 = **11.2 bps**.

---

## 14. Validation tolerance between implementations

The vectorised engine and the loop reference must agree on every order-level
metric:

* integer and string columns exactly, including nulls;
* float columns with identical null patterns and
  `|engine - reference| <= 1e-6 + 1e-9 * |reference|`.

The tolerance absorbs only floating-point summation-order differences
(running totals versus direct sums). It is checked in
`tests/unit/test_reference_equivalence.py` and before every benchmark
measurement.
