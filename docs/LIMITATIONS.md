# Limitations

ExecLens is a teaching-grade, reproducible execution-analytics platform. The
metrics are standard, but everything around them is simplified. These are the
limitations a reader should know before trusting any number.

## Data

* **Synthetic Level-1 market.** Quotes and trades come from a seeded
  stochastic generator: a log random walk mid, spreads rounded to ticks,
  Poisson arrivals and a U-shaped intraday volume curve. It is not
  calibrated to any real instrument, and nothing in the repository is real
  market data.
* **No depth beyond the touch.** There is no order book: only best bid/ask
  and displayed size. Queue position, hidden or iceberg liquidity, odd-lot
  rules and auctions (open/close) are not modelled.
* **No trading calendar.** Sessions are consecutive business days at a fixed
  UTC time. There are no holidays, half days or venue-specific hours. Markout
  coverage is judged against the symbol's last quote in the dataset, not
  against a session close.
* **Timestamp ties.** Following the specification, a quote stamped at the
  same microsecond as a fill counts as *prior* (`<=`). Real feeds need
  exchange sequence numbers or a latency offset to order such events.
* **Stale-quote policy.** Any quote older than the tolerance (default 30 s)
  is treated as unavailable. In a real feed an unchanged quote is still live,
  so the tolerance must be tuned per instrument's liquidity.

## Execution simulation

* **Simplified fill model.** A marketable child order fills completely at
  the touch plus a deterministic square-root temporary-impact term. There is
  no partial fill at the child level, no rejection and no latency.
* **No permanent impact.** The synthetic market does not react to simulated
  trades, so strategies never move the price they trade against, and mid
  markouts of simulated fills only reflect exogenous price moves.
* **Our prints are not on the tape.** Simulated fills are not added to the
  market trades, so market volume and VWAP exclude the order's own volume,
  and execution-window participation can exceed 1.
* **Stylised algorithms.** IMMEDIATE, TWAP, historical-profile VWAP and POV
  are textbook versions on a fixed decision grid (default 30 s). There are no
  limit orders, no price limits or I-would levels, no dark-pool or venue
  routing, no anti-gaming randomisation and no smart order router.
* **The VWAP profile is synthetic history.** It averages independently
  generated prior sessions of the same scenario. It is realistic in spirit,
  but it comes from the same model that generates the traded session.
* **Scenario-specific conclusions.** Strategy rankings depend on the
  scenario, size, horizon, impact coefficient and number of trials. With
  20 trials the confidence intervals are often wide. The tool reports them
  and says when a ranking is not conclusive; it never declares a generally
  best strategy.

## Analytics

* **Arrival cost is the execution part of implementation shortfall.**
  Unfilled quantity is reported through completion, not charged as an
  opportunity cost.
* **VWAP benchmark caveats.** A large order partly benchmarks itself; an
  order that finishes early in a long horizon is compared with prices after it
  stopped trading. Read VWAP cost next to participation and arrival cost.
* **Strict fill aggregation.** The order-level effective spread and markouts
  need context for every fill. Orders with many fills are therefore slightly
  more likely to be excluded, and portfolio statistics report how many orders
  contributed.
* **Flags are statistical screens, not verdicts.** Robust z-scores against
  the analysed book flag unusual values. They describe co-occurrence (for
  example, "execution coincided with a wide spread") and never establish
  cause. With fewer than 20 orders per metric the statistical rules are
  switched off.
* **Diagnostic features are simple.** Volatility is the standard deviation of
  10-second mid returns over the five minutes before arrival. Momentum and the
  interval move are single-period returns.

## Engineering and scope

* **Single machine, batch processing.** Datasets are analysed in memory.
  There is no streaming ingestion, no incremental recomputation and no
  persistence of API state (simulation results are kept in a bounded
  in-memory store and are lost on restart).
* **API security.** There is no authentication, rate limiting or TLS. It is
  meant for local use and demos. POST `/api/v1/simulations` runs
  synchronously with bounded parameters (50 trials at most).
* **Benchmarks are machine-specific.** They were measured on one laptop,
  whose power profile changes clock speeds under sustained load; see
  [BENCHMARKS.md](BENCHMARKS.md) for the method and caveats.

## What this is not

* Not a stock-price predictor or an alpha signal.
* Not a trading bot and not connected to any broker or exchange.
* Not a regulatory best-execution product (for example MiFID II RTS 27/28 or
  SEC Rule 605/606 reporting).
* Simulated results say nothing about real trading profitability.

## What a production implementation would add

* Real consolidated market data (Level 2 or order-by-order), exchange
  sequence numbers, a trading calendar, corporate actions and symbology.
* Broker / OMS / EMS execution records (FIX drop copies) with venue, order
  type, limit price and child-order lifecycle (new/amend/cancel/fill).
* Peer-group and pre-trade cost models (volatility-, spread- and
  size-conditioned expected cost), with flags relative to expectation rather
  than to the raw book.
* An opportunity-cost treatment of unfilled quantity and benchmark
  customisation per desk (arrival, open, close, interval VWAP).
* A calibrated market-impact model (permanent and temporary) fitted to the
  firm's own executions.
* Scheduled pipelines, a warehouse, access control and audit trails.
