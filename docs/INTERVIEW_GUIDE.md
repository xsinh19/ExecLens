# ExecLens interview guide

How to explain every part of ExecLens. Each topic has a **20-second answer**
(say this first), a **deeper explanation** (for follow-up questions) and the
**source** that implements it, so every claim can be shown in the code.
Numbers quoted from benchmarks live in [BENCHMARKS.md](BENCHMARKS.md); read
them from there rather than from memory.

---

## 1. Motivation

**20 s.** Retail apps show whether an investment went up, but not whether the
*execution* was good given the market at the time. For one order you can
eyeball it; for thousands of institutional fills it becomes a data problem.
ExecLens automates that analysis: it aligns orders and fills with market
data, benchmarks them, flags unusual executions and compares execution
algorithms on reproducible synthetic markets.

**Deeper.** The distinction the project is built on is *investment
performance* ("did the price go up after I bought?") versus *execution
performance* ("how much did I pay to get in, relative to what the market
offered?"). Execution cost is a real, measurable drag on returns that desks
manage with TCA. ExecLens runs on synthetic data by design: it needs no data
licences, and every result is reproducible from a seed.

**Source.** `README.md`, `docs/ARCHITECTURE.md`.

## 2. Electronic trading

**20 s.** Orders are sent electronically to venues that match buyers and
sellers in a continuous limit order book. Large institutional orders are
rarely sent at once. An execution algorithm slices them into child orders over
time to limit market impact.

**Deeper.** The trade-off every execution algorithm manages is between
**impact** (trading fast moves the price against you) and **timing risk**
(trading slowly exposes you to price drift). ExecLens simulates exactly this:
IMMEDIATE takes all the impact at once, while TWAP/VWAP/POV spread it out and
carry timing risk. The Strategy Lab reports both the mean cost and its
dispersion.

**Source.** `src/execlens/simulation/`.

## 3. Order lifecycle

**20 s.** A parent order arrives (the decision time), an algorithm schedules
child orders, each child is executed as one or more fills, and the parent is
complete (or partially complete) at the end of its horizon. TCA compares the
fills with the market at each stage.

**Deeper.** In ExecLens: `orders` holds the parent (`arrival_timestamp`,
`quantity`, `execution_horizon_seconds`, `strategy`); `fills` holds the
executions. The simulator builds a decision grid over the horizon (default
30 s slices). At each decision time it asks the strategy for a child quantity
and fills it at the touch plus impact. Validation rejects fills before
arrival, fills for unknown orders and total fills above the parent quantity.

**Source.** `data/schemas.py`, `data/validation.py`, `simulation/simulator.py`.

## 4. Bid and ask

**20 s.** The bid is the best price someone will pay; the ask is the best
price someone will sell at. An immediate buyer pays the ask and an immediate
seller receives the bid.

**Deeper.** Level-1 data is only the top of the book (best prices and
displayed sizes). ExecLens validates `bid <= ask`: locked markets are allowed,
crossed markets are rejected as bad data. The simulator's fill model starts
from the ask for buys and the bid for sells.

**Source.** `data/schemas.py` (`Quote`), `simulation/impact.py`.

## 5. Midpoint

**20 s.** `(bid + ask) / 2`: the fair-value reference halfway between the buy
and sell prices. Arrival cost, effective spread and markouts are measured
against it.

**Deeper.** The mid formula exists once, as `mid_price()` in
`tca/metrics.py`. Even the dashboard's price chart gets its mid from that
function (via `AnalysisResult.market_window`) instead of recomputing it.

**Source.** `tca/metrics.py`.

## 6. Basis points

**20 s.** 1 bp = 0.01% = 1/10,000. Costs are expressed in bps so they are
comparable across prices: 2 cents on a $10 stock is 20 bps, but on a $200
stock only 1 bp.

**Deeper.** Every `*_bps` metric is `fraction * 10,000`. Basis points hide
money size, which is why the summary also reports a **notional-weighted**
arrival cost (what the whole book paid), not just the median order.

**Source.** `tca/metrics.py`, `tca/aggregation.py`.

## 7. Arrival price

**20 s.** The mid of the latest valid quote **at or before** the moment the
order arrived. It is the price you could have hoped for if execution were
instant and free.

**Deeper.** It is found with a backward as-of join (`quote_ts <= arrival_ts`),
never the nearest quote. If that quote is older than the stale tolerance
(30 s default), the arrival mid is withheld (null), the arrival cost is
unavailable and the order is flagged `STALE_MARKET_DATA`.

**Source.** `tca/alignment.py::attach_quote_context`.

## 8. Implementation shortfall (arrival cost)

**20 s.** `side_sign * (avg_fill_price - arrival_mid) / arrival_mid * 10,000`.
Positive means worse. A buy that averaged 100.10 against an arrival mid of
100.00 cost +10 bps, and a sell at 99.90 against 100.00 also cost +10 bps.

**Deeper.** The side sign (+1 buy, -1 sell) makes one formula serve both
directions. The average fill price is quantity-weighted. ExecLens reports the
*execution* component of Perold's shortfall: unfilled shares show up in the
completion ratio rather than as an opportunity cost, because charging them
needs a reference price the data does not have. Arrival cost includes
spread, impact and drift (timing luck), so it is read together with VWAP cost
and markouts.

**Source.** `tca/metrics.py::cost_bps`, `tca/engine.py`; tests in
`tests/unit/test_tca_formulas.py`.

## 9. VWAP

**20 s.** Market VWAP is `sum(price * size) / sum(size)` over the trades in the
order's benchmark interval. VWAP cost measures whether the order traded better
or worse than the average market price while it was working.

**Deeper.** The interval is `[arrival, arrival + horizon]`, extended to the
last fill if executions ran past the horizon; both ends are included. With no
volume, VWAP is unavailable, so there is never a division by zero. A symbol
with no trade data gets an unknown volume, not zero. The engine computes it
with per-symbol running totals: volume in `[a, b]` is
`C(<= b) - C(< a)`, found by two as-of joins. That is O((trades + orders) log n)
instead of rescanning the tape per order. Caveat: a large order partly
benchmarks itself.

**Source.** `tca/alignment.py::attach_interval_volumes`, `tca/engine.py`.

## 10. Effective spread

**20 s.** `2 * side_sign * (fill_price - mid) / mid * 10,000` with the mid at or
before the fill. It measures the spread actually paid; a buy at the ask gives
exactly the quoted spread.

**Deeper.** Below the quoted spread means price improvement; above it means
the fill walked through the touch. At the order level it is quantity-weighted
and reported only if **every** fill had a fresh quote. Otherwise it is null and
`n_fills_without_quote` says why, so missing data is never averaged away.

**Source.** `tca/metrics.py::effective_spread_bps`.

## 11. Markout

**20 s.** How the mid moved *after* a fill, at 10/30/60/300 s. **Positive =
favourable** for the order: the opposite sign convention to costs, stated in
the code, the API docs and the dashboard.

**Deeper.** Two versions share one formula. The **fill markout** (the spec's
formula) is measured from the fill price, so it includes the half-spread paid:
a buy at the ask in a flat market shows about -half-spread. The **mid
markout** is measured from the mid at the fill and isolates the subsequent
price move, the adverse-selection signal, which is why the `ADVERSE_MARKOUT`
flag uses it. The future mid is the prevailing quote at `t + h`. It is
unavailable if the data does not reach `t + h` or the quote is stale, and the
number of unavailable fills is reported. This is the only place future data is
used, and it is used intentionally. (An independent review flagged the
original "negative markout = price fell" wording, which led to adding the mid
markout.)

**Source.** `tca/alignment.py::attach_future_mids`, `tca/metrics.py::markout_bps`.

## 12. Participation

**20 s.** Our executed shares divided by market volume. ExecLens reports two:
over the whole benchmark interval, and over the execution window [arrival,
last fill].

**Deeper.** Interval participation is comparable across strategies trading
the same order, and the `HIGH_PARTICIPATION` flag (> 25%) uses it. Execution
participation shows intensity while trading: POV at 10% shows about 0.10.
Because the synthetic tape does not include our own fills, execution
participation can exceed 1; real data would include them.

**Source.** `tca/engine.py`, `docs/METRICS.md` §11.

## 13. TWAP

**20 s.** Split the parent evenly across time slices, for example 1,000
shares over 3 slices gives 334/333/333. Each slice trades at its start and
catches up if an earlier slice was skipped.

**Deeper.** Integer allocation uses the largest-remainder method, so the
schedule always sums exactly to the parent quantity and is deterministic
(ties go to earlier slices). The simulator caps every child at the remaining
quantity, so there is never an overfill. Slices without a fresh quote are
skipped and caught up later, and anything missing at the end is reported as
partial completion, never forced.

**Source.** `simulation/twap.py`, `simulation/base.py::allocate`.

## 14. VWAP execution

**20 s.** Allocate the parent in proportion to the volume you *expect* in each
slice, using an intraday profile from **prior** sessions, never the session
being traded.

**Deeper.** The profile is the average fraction of daily volume per 5-minute
bucket over 20 independently generated prior sessions (a separate random
stream). The schedule is fixed at arrival, and a test proves it is identical
when the same session's future volume differs 500x. `ORACLE_VWAP` uses
realised same-session volume to show what perfect foresight would be worth.
It is labelled non-implementable, excluded from rankings and drawn in gray.

**Source.** `simulation/vwap.py`, `data/synthetic.py::historical_volume_profile`,
`tests/unit/test_strategy_leakage.py`.

## 15. POV

**20 s.** Trade a fixed percentage of the volume the market has *already*
printed. At each slice end the target is `floor(rate * observed volume since
arrival)`.

**Deeper.** POV reacts to data at or before its decision time, so it lags the
market by up to one slice. It may not finish: in the illiquid scenario, 10% of
a thin tape fills only part of a large order, and the comparison excludes it
from the "cheapest" ranking because its cost covers fewer shares. A test builds
two markets that diverge after a cut-off and shows identical POV decisions up
to the cut-off.

**Source.** `simulation/pov.py`, `simulation/comparison.py::ranked_strategies`.

## 16. Simulated market impact

**20 s.** Fill price = touch price, pushed against the order by
`coefficient * (clip / recent volume) ^ 0.5` bps. Recent volume is the last
5 minutes of the tape up to the decision.

**Deeper.** The square-root shape is concave, so splitting an order lowers
the per-share impact, which is why TWAP/VWAP beat IMMEDIATE on average in
calm markets. The model is deterministic, capped at 500 bps and floored at a
minimum reference volume. It has no permanent component: the synthetic
market does not react to our trades. It is **not** a calibrated impact model;
it only makes size and liquidity matter.

**Source.** `simulation/impact.py`.

## 17. Timestamp alignment

**20 s.** Every order and fill is paired with the market state *at that
moment*: the latest quote at or before its timestamp, for the same symbol.

**Deeper.** Timestamps are UTC with microsecond precision, and naive
timestamps are rejected. Ties: a quote stamped exactly at the event counts as
prior (spec `<=`); among duplicate quote timestamps, the last in input order
wins. The limitation is honest: with coarse timestamps, a quote at the same
instant can already reflect the fill.

**Source.** `tca/alignment.py`, `docs/METRICS.md` §2.

## 18. As-of joins

**20 s.** An as-of join matches each left row with the most recent right row
whose key is less than or equal to the left key, like a "backward" lookup in
time. It is the core point-in-time operation in ExecLens.

**Deeper.** Polars' `join_asof(strategy="backward", by="symbol")` needs both
sides sorted by the key within each group. ExecLens sorts explicitly,
including after hash joins, which do not preserve order. The reference
implementation does the same thing with `bisect_right(times, t) - 1`. For
markouts, one sort serves all four horizons, because `t + h` keeps the order of
`t`. `allow_exact_matches=False` gives the strict `< start` side of the VWAP
interval.

**Source.** `tca/alignment.py`, `tca/reference.py`.

## 19. Look-ahead bias

**20 s.** Using information that was not available at decision time, for
example matching a fill to the *next* quote. ExecLens prevents it by
construction and proves it with tests.

**Deeper.** Three guards. (1) The TCA context uses backward as-of joins only.
(2) The simulator gives strategies a `MarketSnapshot` of data stamped at or
before the decision time, never the frame itself. (3) Tests: a quote 0.1 s in
the future that is *nearer* than the prior one must not be used; rewriting
every quote after a cut-off must not change any earlier metric (but must change
markouts that cross it); POV/VWAP decisions must be unchanged when future
volume changes.

**Source.** `tests/unit/test_temporal_integrity.py`,
`tests/unit/test_strategy_leakage.py`.

## 20. Polars

**20 s.** A columnar DataFrame library written in Rust, with lazy query
optimisation and multi-threading. ExecLens expresses the whole TCA pipeline
as Polars lazy frames: as-of joins, group-bys and expressions.

**Deeper.** Lazy frames let Polars push projections down (read only the
needed Parquet columns) and plan the joins. Formulas are small functions that
return `pl.Expr`, reused everywhere. The same logic written as Python loops,
the reference implementation, is roughly an order of magnitude slower on the
benchmark dataset; see BENCHMARKS.md for the measured figure.

**Source.** `tca/engine.py`, `tca/metrics.py`.

## 21. Vectorisation

**20 s.** Replace per-row Python loops with whole-column operations that run
in compiled code. It is the main source of the speedup.

**Deeper.** The reference does 5 binary searches per fill and a slice sum per
order in the interpreter. The engine turns them into sorted-merge as-of joins
and a running-total difference. Part of the gain is algorithmic (running
totals versus slice sums) and part is parallelism (16 threads versus 1), and
the benchmark page says so.

**Source.** `tca/reference.py` vs `tca/engine.py`, `docs/BENCHMARKS.md`.

## 22. Parquet

**20 s.** A compressed columnar file format. It keeps types (including UTC
timestamps), compresses market data well and lets readers load only the
columns they need.

**Deeper.** Each dataset is one Parquet file per table plus `metadata.json`
(seed, full generator spec, disclaimer). The `end_to_end_parquet` benchmark
compares the eager "read everything" approach with lazy scanning and column
projection.

**Source.** `data/loader.py`.

## 23. DuckDB

**20 s.** An in-process analytical SQL engine. `execlens query` exposes the
Parquet tables, and the derived TCA tables, as SQL views with no import step
and no server.

**Deeper.** It reads Parquet directly and pushes filters and projections into
the scan. It fits ExecLens because analysts often want ad-hoc SQL ("median
arrival cost by strategy"), not because the pipeline needs a database.

**Source.** `data/repository.py`, `cli.py::query`.

## 24. FastAPI

**20 s.** A typed Python web framework. Pydantic models define requests and
responses, and it generates OpenAPI docs automatically (`/docs`).

**Deeper.** The app factory analyses the dataset once at startup (lifespan)
and indexes it, so order lookups are dictionary lookups. Endpoints: health,
order list/detail/TCA, summary, POST/GET simulations. Errors are 404 for
unknown ids, 422 for invalid input (including domain validation) and a
generic 500 without a traceback. No formula lives in a route. Measured p95
latencies are in BENCHMARKS.md.

**Source.** `api/app.py`, `api/routes/`, `api/schemas.py`.

## 25. Pydantic

**20 s.** Runtime validation from type hints. ExecLens uses it at the
boundaries (API payloads, configs, record models), not for millions of rows.

**Deeper.** Bulk validation is Polars expressions for speed. A test checks
that the Pydantic record models and the frame validators reject the same
invalid inputs. API schema field descriptions carry the sign conventions
into the OpenAPI docs.

**Source.** `data/schemas.py`, `api/schemas.py`, `tests/unit/test_validation.py`.

## 26. Streamlit

**20 s.** A pure-Python dashboard framework. ExecLens has five pages
(overview, order explorer, diagnostics, strategy lab, benchmarks), all fed by
the same analysis service.

**Deeper.** The dashboard computes no metric. Tests run each page headlessly
with `AppTest` and assert the tiles equal the engine's summary. Charts follow a
colour-blind-validated palette with separate light and dark steps and fixed
strategy colours. The benchmark page reads `benchmarks/results.json`.

**Source.** `dashboard/`, `tests/integration/test_dashboard.py`.

## 27. Benchmarking

**20 s.** Pure-Python reference against the Polars engine on ≥1M market rows
and ≥100k fills: outputs checked equal first, then interleaved repeated runs,
median reported. The machine, the power state and every run are recorded.

**Deeper.** The honesty rules: identical inputs and outputs (compared column
by column; any disagreement aborts); warm-up runs excluded; runs interleaved
because the laptop throttles under sustained load; median, not minimum; memory
measured in separate untimed runs; the scale enforced (the harness refuses
undersized data). Two workloads: in-memory core and end-to-end from Parquet.
Docs are generated from the JSON, and a test checks the README matches it.

**Source.** `benchmarking.py`, `docs/BENCHMARKS.md`.

## 28. Testing

**20 s.** Over 250 tests: hand-calculated formulas (BUY and SELL), no-look-ahead
tests, edge cases, engine-versus-reference equivalence, a frozen regression
dataset, API/CLI/dashboard integration, and ≥85% coverage enforced on the core
packages.

**Deeper.** Formula tests never derive expected values from production code;
the arithmetic is written out. The reference implementation is an independent
oracle checked on generated data. The regression fixture freezes inputs and
expected outputs, including strategy fills. CI runs Ruff, the format check,
strict mypy, pytest, a coverage gate and a Docker build.

**Source.** `tests/`, `scripts/check_coverage.py`, `.github/workflows/ci.yml`.

## 29. Docker

**20 s.** One multi-stage image: uv installs the locked dependencies in a
builder, and a slim runtime runs as a non-root user. The API is the default
command; docker-compose runs the API and the dashboard together.

**Deeper.** `uv sync --frozen` from `uv.lock` makes installs reproducible. A
BuildKit cache mount keeps the download cache out of the image. CI builds the
image and smoke-tests the CLI and API inside it.

**Source.** `Dockerfile`, `docker-compose.yml`.

## 30. Architecture

**20 s.** Three layers: domain (data, TCA, diagnostics, simulation) → services
(generate, analyse) → interfaces (CLI, API, dashboard). One formula module;
everything else consumes it.

**Deeper.** Walk the Mermaid diagrams in ARCHITECTURE.md: validation →
as-of alignment → engine → diagnostics → service → interfaces, and market →
snapshot → strategies → simulator → same TCA engine. There is deliberately no
Kafka/Spark/Airflow: one machine and batch data do not need them.

**Source.** `docs/ARCHITECTURE.md`.

## 31. Limitations

**20 s.** Synthetic Level-1 data, a simplified impact model with no
permanent impact, no queue position, venues or latency, and no real broker
connectivity. It is not a best-execution compliance product, and simulated
results say nothing about profitability.

**Deeper.** Be specific: fills are complete at touch plus impact; our prints
are not on the tape; the VWAP profile is synthetic history; flags are
statistical screens, not verdicts; conclusions are scenario-specific with
confidence intervals.

**Source.** `docs/LIMITATIONS.md`.

## 32. What a real institutional implementation would require

**20 s.** Real consolidated and order-book data with sequence numbers, OMS/EMS
child-order records, pre-trade cost models so flags compare against
*expected* cost, a calibrated impact model, opportunity cost for unfilled
shares, and production data pipelines with access control.

**Deeper.** Also venue analysis and routing, peer-group benchmarks (by size,
liquidity bucket and volatility regime), per-desk benchmark choice
(arrival/open/close/interval VWAP), regulatory reporting, and monitoring. The
code structure would survive (formulas, as-of alignment, reference testing),
but the data layer and the models would be replaced.

**Source.** `docs/LIMITATIONS.md` ("What a production implementation would add").

---

## Likely curveballs

* **"Why is your VWAP cost negative when arrival cost is positive?"** The
  market drifted against the order: it paid more than arrival but tracked the
  interval's average price well.
* **"Why can execution participation be above 1?"** Simulated fills are not
  on the synthetic tape, so the denominator excludes our own volume.
* **"Is TWAP better than IMMEDIATE?"** Only in some scenarios and on average.
  TWAP pays less impact but carries timing risk (wider dispersion). In a
  rising market a patient buyer pays the drift. ExecLens reports confidence
  intervals and refuses to call a general winner.
* **"What would you do with a quote stamped exactly at the fill?"** The spec
  says `<=`. In production, use exchange sequence numbers or a latency offset.
* **"How do you know your fast code is right?"** It must match a readable
  loop implementation to 1e-9 relative on every order-level column before any
  timing is taken, and it also matches hand calculations and a frozen
  regression fixture.
