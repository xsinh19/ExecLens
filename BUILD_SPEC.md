# ExecLens — Autonomous End-to-End Build Specification

## EXECUTION DIRECTIVE

You are the primary software engineer responsible for building this project
from an empty repository to a complete, tested, benchmarked, documented,
portfolio-quality GitHub project.

Work AUTONOMOUSLY.

Do not repeatedly ask the user for approval between stages.

Do not stop after planning.
Do not stop after scaffolding.
Do not merely suggest code.
Implement it.

Continue systematically until the project satisfies the Definition of Done
near the end of this document.

If the context window approaches its limit:

1. commit all valid completed work;
2. update `PROGRESS.md`;
3. record tests/benchmarks actually run;
4. record the next exact task;
5. continue after context compaction/recovery by rereading:
   - BUILD_SPEC.md
   - PROGRESS.md
   - git log
   - current repository state.

Do not stop early merely because the project is large.

Only ask the user a question if work is truly impossible without external
information or irreversible permission.

For ordinary technical ambiguity:
- choose the simplest defensible engineering solution;
- document the decision;
- continue.

Use subagents only when useful for genuinely independent review/research.
Do not create an unnecessary swarm of agents.

When independent review is useful, delegate:
- financial/math correctness review;
- Python/performance/testing review;
- final repository audit.

The primary agent remains responsible for integration and final correctness.

---

# 1. PROJECT

Project name:

ExecLens

Full title:

ExecLens — Electronic Execution Analytics & Transaction Cost Analysis Platform

---

# 2. PURPOSE

Build a Python-first electronic trading analytics platform that automates
analysis of trade execution quality.

The system should answer:

"Given the state of the market when an order was submitted and executed,
how efficiently was that order executed?"

The project is motivated by the difference between:

INVESTMENT PERFORMANCE
"Did the asset increase after I bought it?"

and

EXECUTION PERFORMANCE
"Given market conditions when I traded, how efficiently was my order filled?"

For a single retail order, a person could manually inspect prices around the
execution.

For thousands of institutional executions, manually comparing fills with
market conditions becomes repetitive and difficult to scale.

ExecLens automates that workflow.

The project should demonstrate interest and technical ability relevant to
electronic trading:

- Python engineering
- market data processing
- algorithms
- electronic execution
- transaction cost analysis
- quantitative reasoning
- data engineering
- APIs
- performance optimization
- testing
- visualization
- reproducibility

---

# 3. IMPORTANT POSITIONING

This project is NOT:

- a stock price predictor;
- an investment recommendation system;
- an automated live trading bot;
- an alpha-generation claim;
- a portfolio optimizer;
- an HFT system;
- a brokerage integration;
- a regulatory "best execution" compliance platform.

Never describe generated data as real exchange data.

Never describe simulated fills as real executions.

Never claim profitability.

Never invent benchmark numbers.

Never claim a speedup unless measured.

Preferred terminology:

- transaction cost analysis
- execution analytics
- simulated execution
- synthetic/replay market
- execution-quality diagnostics
- algorithm comparison
- reproducible benchmark

---

# 4. PRIMARY USER STORY

An execution analyst has:

1. market quotes/trades;
2. parent orders;
3. fills.

Instead of manually investigating each order, they run ExecLens.

ExecLens automatically:

1. aligns orders and fills to market data;
2. calculates execution benchmarks;
3. quantifies execution cost;
4. computes post-trade markouts;
5. flags unusual executions;
6. explains relevant market conditions;
7. compares execution algorithms under controlled scenarios;
8. exposes results through an API;
9. visualizes results in a dashboard.

---

# 5. HIGH-LEVEL ARCHITECTURE

Conceptually:

                     MARKET DATA
                         |
                         v
                 Data Validation
                         |
                         v
               Temporal Alignment
                         |
                         v
                TCA Analytics Engine
                  /       |       \
                 /        |        \
          Arrival      VWAP       Spread
           Cost         Cost       Analysis
                 \        |        /
                  \       |       /
                         |
                     Markouts
                         |
                         v
                 Diagnostic Engine
                         |
          +--------------+--------------+
          |                             |
          v                             v
   FastAPI Backend              Streamlit Dashboard


Separate execution simulation pipeline:

                 Synthetic Market
                       |
                       v
                  Parent Order
                       |
           +-----------+-----------+
           |           |           |
       Immediate      TWAP        VWAP
                                   |
                                  POV
           |           |           |
           +-----------+-----------+
                       |
                  Simulated Fills
                       |
                       v
                   TCA Engine
                       |
                       v
              Strategy Comparison

---

# 6. TECH STACK

Use Python 3.12.

Primary stack:

- Python
- Polars
- NumPy
- DuckDB
- Parquet
- Pydantic
- FastAPI
- Uvicorn
- Streamlit
- Plotly
- Typer
- pytest
- pytest-cov
- Ruff
- mypy
- Docker
- GitHub Actions

Use standard-library functionality where practical.

Do NOT add technology merely for resume value.

Do NOT initially use:

- Kafka
- Redis
- Celery
- Kubernetes
- React
- Spark
- Airflow
- LLMs
- vector databases
- reinforcement learning

unless an objectively necessary requirement emerges.

The final project should remain understandable by one student during an
interview.

---

# 7. EXPECTED REPOSITORY STRUCTURE

Use a clean `src/` package layout.

Target structure approximately:

execlens/
|
|-- BUILD_SPEC.md
|-- PROGRESS.md
|-- README.md
|-- pyproject.toml
|-- Makefile
|-- Dockerfile
|-- docker-compose.yml
|-- .gitignore
|-- .env.example
|
|-- src/
|   `-- execlens/
|       |-- __init__.py
|       |-- config.py
|       |-- cli.py
|       |
|       |-- data/
|       |   |-- schemas.py
|       |   |-- loader.py
|       |   |-- synthetic.py
|       |   `-- repository.py
|       |
|       |-- tca/
|       |   |-- alignment.py
|       |   |-- metrics.py
|       |   |-- engine.py
|       |   `-- aggregation.py
|       |
|       |-- diagnostics/
|       |   |-- features.py
|       |   `-- flags.py
|       |
|       |-- simulation/
|       |   |-- base.py
|       |   |-- immediate.py
|       |   |-- twap.py
|       |   |-- vwap.py
|       |   |-- pov.py
|       |   `-- impact.py
|       |
|       |-- api/
|       |   |-- app.py
|       |   |-- schemas.py
|       |   `-- routes/
|       |
|       `-- dashboard/
|           `-- app.py
|
|-- tests/
|   |-- unit/
|   |-- integration/
|   |-- regression/
|   `-- fixtures/
|
|-- benchmarks/
|   |-- benchmark_tca.py
|   `-- benchmark_api.py
|
|-- data/
|   |-- sample/
|   `-- README.md
|
|-- docs/
|   |-- ARCHITECTURE.md
|   |-- METRICS.md
|   |-- BENCHMARKS.md
|   |-- DATA_MODEL.md
|   |-- INTERVIEW_GUIDE.md
|   `-- LIMITATIONS.md
|
`-- .github/
    `-- workflows/
        `-- ci.yml

This is guidance, not a requirement to create empty files prematurely.

Create modules when they have real responsibilities.

---

# 8. SOFTWARE ENGINEERING PRINCIPLES

Optimize for:

1. correctness;
2. explainability;
3. reproducibility;
4. maintainability;
5. measured performance.

Avoid unnecessary abstractions.

Do not make five abstraction layers around a dataframe operation.

Do not duplicate financial formulas in:

- API;
- dashboard;
- CLI.

All interfaces should call one domain implementation.

Prefer vectorized Polars expressions to Python row iteration.

Use explicit typing for public/domain interfaces.

Use Pydantic at external boundaries.

Keep financial calculations mostly pure and testable.

---

# 9. DATA MODELS

## Quote

Fields:

- timestamp
- symbol
- bid_price
- ask_price
- bid_size
- ask_size

Rules:

timestamp must be timezone-aware UTC.

bid_price > 0

ask_price > 0

bid_size >= 0

ask_size >= 0

bid_price <= ask_price

Derived:

mid =
(bid_price + ask_price) / 2

spread =
ask_price - bid_price

spread_bps =
spread / mid * 10000

---

## MarketTrade

Fields:

- timestamp
- symbol
- price
- size

Rules:

price > 0

size > 0

---

## Order

Fields:

- order_id
- symbol
- side
- quantity
- arrival_timestamp
- execution_horizon_seconds if applicable
- strategy label if applicable

side:

BUY
SELL

quantity > 0

---

## Fill

Fields:

- fill_id
- order_id
- timestamp
- quantity
- price

Rules:

quantity > 0

price > 0

Total filled quantity must not silently exceed parent-order quantity.

---

# 10. SYNTHETIC MARKET GENERATOR

The project must work without paid data or financial API credentials.

Build a deterministic synthetic market generator.

Every run must support a random seed.

Same configuration + same seed =>
identical generated dataset.

Different seed =>
different stochastic realization.

Generate:

- midpoint prices;
- bid/ask prices;
- bid/ask sizes;
- market trades;
- volumes;
- parent orders;
- sample fills.

Required scenarios:

1. normal/liquid;
2. high volatility;
3. low liquidity;
4. wide spread;
5. upward trend;
6. downward trend.

Keep the model understandable.

A reasonable generator may combine:

- stochastic price changes;
- volatility parameter;
- drift parameter;
- configurable spread;
- configurable depth/liquidity;
- stochastic market volume.

Do not pretend this is an exchange-grade market simulator.

The synthetic generator exists for:

- reproducibility;
- automated testing;
- benchmarks;
- dashboard demonstration;
- controlled execution-strategy comparisons.

Commit only a SMALL demo dataset.

Generate large benchmark datasets locally.

---

# 11. CRITICAL TEMPORAL RULE: NO LOOK-AHEAD

This is one of the most important technical requirements.

For ORDER ARRIVAL context:

use the latest quote whose timestamp is:

quote_timestamp <= order_arrival_timestamp

For FILL context:

use the latest quote whose timestamp is:

quote_timestamp <= fill_timestamp

Prefer an as-of join.

Do NOT simply choose the nearest timestamp.

The nearest timestamp could lie in the future and cause look-ahead leakage.

Support a configurable stale-quote tolerance.

If no sufficiently recent quote exists:

- record missing/stale context;
- return metric as unavailable where required;
- flag the record;
- do not arbitrarily forward-fill far into the past.

Future market data may ONLY be used when the metric intentionally requires
future information, such as post-trade markout.

Write explicit tests proving this.

---

# 12. SIDE SIGN CONVENTION

Use internally:

BUY  => side_sign = +1
SELL => side_sign = -1

For EXECUTION COST metrics:

positive value = worse execution.

negative value = better execution.

This convention must be consistent throughout the project.

---

# 13. QUANTITY-WEIGHTED AVERAGE FILL PRICE

For fills i:

average_fill_price =

sum(fill_price_i * fill_quantity_i)
---------------------------------
sum(fill_quantity_i)

Test exact manually calculated examples.

---

# 14. ARRIVAL COST / IMPLEMENTATION SHORTFALL

Use arrival midpoint as benchmark.

arrival_cost_bps =

side_sign
*
(average_fill_price - arrival_mid)
/
arrival_mid
*
10000

Interpretation:

positive = worse execution

negative = better execution

Example BUY:

arrival midpoint = 100.00
average execution = 100.10

cost = +10 bps

Equivalent SELL:

arrival midpoint = 100.00
average execution = 99.90

cost = +10 bps

Both examples must exist as manually calculated tests.

---

# 15. MARKET VWAP

During the configured execution interval:

market_vwap =

sum(market_trade_price_i * market_trade_size_i)
-----------------------------------------------
sum(market_trade_size_i)

If there is no valid market volume:

VWAP must be unavailable.

Never divide by zero.

---

# 16. VWAP EXECUTION COST

vwap_cost_bps =

side_sign
*
(average_fill_price - market_vwap)
/
market_vwap
*
10000

positive = worse than market VWAP.

---

# 17. EFFECTIVE SPREAD

For each fill use the most recent valid midpoint AT OR BEFORE its timestamp.

effective_spread_bps =

2
*
side_sign
*
(fill_price - contemporaneous_mid)
/
contemporaneous_mid
*
10000

Aggregate across fills using quantity weights.

Again:

positive cost generally represents less favourable execution.

---

# 18. MARKOUTS

Required horizons:

- 10 seconds
- 30 seconds
- 60 seconds
- 300 seconds

For a fill at time t and markout horizon h:

markout_bps(h) =

side_sign
*
(future_mid(t+h) - fill_price)
/
fill_price
*
10000

Here use a clearly documented convention:

positive markout =
subsequent market move was favourable to the order direction.

This convention differs from the cost metrics.

Make this very obvious in:

- code docs;
- METRICS.md;
- API schemas;
- dashboard labels.

For future midpoint selection, document the exact lookup methodology.

Do not hide unavailable future observations.

Aggregate markouts across fills using fill-quantity weights.

---

# 19. ADDITIONAL EXECUTION METRICS

Calculate:

- requested quantity;
- executed quantity;
- completion ratio;
- average fill price;
- number of fills;
- order duration;
- arrival midpoint;
- arrival spread;
- arrival spread bps;
- average contemporaneous spread;
- market VWAP;
- arrival cost bps;
- VWAP cost bps;
- effective spread bps;
- 10s markout;
- 30s markout;
- 60s markout;
- 300s markout;
- market volume during execution;
- order quantity / market volume;
- fill rate where meaningful.

---

# 20. DIAGNOSTIC FEATURES

Create interpretable features including:

- spread;
- spread bps;
- short-term volatility;
- momentum;
- execution duration;
- participation ratio;
- order quantity;
- fill count;
- arrival cost;
- VWAP cost;
- markouts.

---

# 21. EXECUTION FLAGS

Build deterministic, explainable diagnostics.

Potential reasons:

- HIGH_ARRIVAL_COST
- HIGH_VWAP_COST
- HIGH_EFFECTIVE_SPREAD
- ADVERSE_MARKOUT
- HIGH_PARTICIPATION
- HIGH_VOLATILITY
- WIDE_SPREAD
- STALE_MARKET_DATA
- MISSING_MARKET_DATA
- LOW_LIQUIDITY

Use robust statistical approaches such as:

median
MAD

where appropriate.

Do NOT claim causality.

Acceptable:

"Execution coincided with elevated volatility and wide spreads."

Not acceptable:

"Volatility caused this poor execution."

Optional only after everything else is strong:

Isolation Forest for anomaly detection.

If implemented, keep deterministic rules as the primary explainable system.

---

# 22. EXECUTION SIMULATOR

Build a simple, explicit execution simulator.

This is NOT a full limit-order-book simulator.

Implement a common interface for:

1. Immediate
2. TWAP
3. VWAP
4. POV

---

# 23. IMMEDIATE EXECUTION

Attempt to execute the requested quantity as soon as possible.

BUY starts from ask.

SELL starts from bid.

Apply the documented impact model.

---

# 24. TWAP

Time Weighted Average Price execution.

Split parent quantity approximately evenly across time intervals.

Requirements:

- total scheduled quantity equals parent quantity;
- no over-execution;
- support partial completion;
- deterministic scheduling;
- clear rounding handling.

---

# 25. VWAP EXECUTION STRATEGY

Allocate quantity based on an expected intraday volume profile.

CRITICAL:

Do not use future realized SAME-SESSION volume to construct executable
decisions.

Expected profile should come from:

- prior synthetic sessions; or
- precomputed historical profiles.

Optionally implement:

oracle_vwap

as an explicitly labelled hindsight benchmark.

If oracle_vwap exists, make it impossible to confuse with the implementable
VWAP strategy.

---

# 26. POV EXECUTION

Percentage of Volume.

Example:

participation_rate = 0.10

The algorithm attempts to participate at 10% of observed market volume.

It may only respond to volume already observed.

Do not use future market volume.

---

# 27. FILL MODEL

Since this project uses Level-1/synthetic data rather than an actual exchange
order book:

BUY base price = ask

SELL base price = bid

Add a simple temporary impact component based on clip quantity relative to
available/recent liquidity.

For example, conceptually:

impact_bps =
impact_coefficient
*
(clip_quantity / relevant_market_volume)^impact_exponent

Do not blindly use this exact formula if a clearer simple formulation is
preferable.

Requirements:

- explicit;
- deterministic given seed;
- configurable;
- mathematically documented;
- tested;
- easy to explain.

Do not claim it precisely models real market impact.

---

# 28. EXECUTION STRATEGY COMPARISON

Run every strategy on identical underlying market scenarios.

Compare:

- arrival cost;
- VWAP cost;
- effective spread;
- markouts;
- completion;
- duration;
- participation.

Required scenarios:

- liquid/stable;
- volatile;
- low liquidity;
- wide spread;
- trending upward;
- trending downward.

Never state:

"VWAP is always best."

Use scenario-specific conclusions.

---

# 29. STORAGE

Use:

Parquet

for market/execution datasets.

Use DuckDB where useful for local analytical querying.

Separate conceptually:

raw/
generated/
derived/

Do not commit large generated datasets.

---

# 30. COMMAND LINE INTERFACE

Build a Typer CLI.

Aim for commands such as:

execlens generate

execlens analyze

execlens simulate

execlens benchmark

execlens serve

execlens dashboard

execlens demo

Exact syntax may evolve if ergonomically useful.

`execlens demo` should ideally provide a simple end-to-end demonstration.

---

# 31. FASTAPI APPLICATION

Required endpoints:

GET /health

GET /api/v1/orders

GET /api/v1/orders/{order_id}

GET /api/v1/orders/{order_id}/tca

GET /api/v1/summary

POST /api/v1/simulations

GET /api/v1/simulations/{simulation_id}

Use Pydantic schemas.

Return meaningful 4xx errors for invalid input.

Do not expose internal tracebacks as user-facing API responses.

Generated OpenAPI docs should work.

Do not implement financial formulas inside HTTP routes.

---

# 32. STREAMLIT DASHBOARD

Build a professional execution-analytics dashboard.

It should look like an institutional analytics/research tool rather than a
retail brokerage clone.

Required sections/pages:

## Overview

Display:

- analyzed orders;
- total executed quantity/notional where appropriate;
- median arrival cost;
- mean arrival cost;
- p95 arrival cost;
- median VWAP cost;
- mean effective spread;
- flagged execution percentage.

Use clear units.

---

## Order Explorer

Filters:

- symbol;
- BUY/SELL;
- strategy;
- flagged/unflagged.

For selected order visualize:

- bid;
- ask;
- midpoint;
- arrival timestamp;
- fills;
- benchmark interval;
- VWAP where valid.

Show metrics alongside the chart.

---

## Diagnostics

Show relationships such as:

execution cost vs spread

execution cost vs volatility

execution cost vs participation

Display flagged orders and reasons.

Do not imply statistical correlation establishes causation.

---

## Strategy Lab

Controls:

- scenario;
- symbol;
- BUY/SELL;
- parent quantity;
- horizon;
- strategy;
- POV participation rate;
- relevant simulator parameters.

Compare:

Immediate
TWAP
VWAP
POV

with identical market input.

---

## Benchmarks

Display ACTUAL benchmark results generated by benchmark scripts.

Do not hard-code fake timings.

---

# 33. DASHBOARD ENGINEERING RULE

The dashboard must NOT independently calculate TCA formulas.

It should consume domain/application-layer outputs.

One source of truth.

---

# 34. TESTING STRATEGY

Use pytest.

Create:

- unit tests;
- integration tests;
- temporal/leakage tests;
- regression tests;
- API tests;
- strategy tests.

---

# 35. REQUIRED FORMULA TESTS

Every financial metric must have at least one manually calculated expected
result.

Tests must NOT derive expected values by calling the production code.

Test both:

BUY
SELL

where direction matters.

---

# 36. REQUIRED TEMPORAL TESTS

Explicitly test:

- arrival quote lookup never uses future quote;
- fill quote lookup never uses future quote;
- stale quote handling;
- missing quote handling;
- markout intentionally accesses future quote;
- VWAP strategy does not use future same-session realized volume;
- POV does not use future market volume.

---

# 37. EDGE CASES

Include tests for:

- empty fills;
- partially filled orders;
- zero market volume;
- missing quotes;
- stale quotes;
- duplicate timestamps;
- invalid prices;
- crossed markets;
- negative/zero quantities;
- fill quantity exceeding order quantity;
- order outside market-data range;
- missing future markout observation;
- exact timestamp matches.

---

# 38. STRATEGY TESTS

Verify:

TWAP:
- parent quantity conserved;
- no overfill.

VWAP:
- weights sum appropriately;
- profile based on permitted information.

POV:
- quantity limited by observed volume.

All:
- deterministic when expected;
- use same scenario for fair comparison.

---

# 39. REGRESSION DATASET

Create a small deterministic dataset with fixed expected results.

Use it to ensure future refactoring does not silently alter:

- arrival cost;
- VWAP;
- effective spread;
- markouts;
- strategy outputs.

---

# 40. COVERAGE

Target at least:

85% line coverage

for:

src/execlens/tca
src/execlens/simulation
src/execlens/diagnostics

Coverage should not be gamed with meaningless tests.

---

# 41. CODE QUALITY COMMANDS

Final repository should pass:

ruff check .

ruff format --check .

mypy src

pytest

pytest with coverage

Do not suppress legitimate problems merely to achieve green output.

---

# 42. PERFORMANCE BENCHMARK

Create a meaningful computational benchmark.

Minimum benchmark scale:

at least 1,000,000 market observations

at least 100,000 execution/fill records

Create:

1. straightforward reference implementation;
2. optimized/vectorized implementation.

The reference implementation should be simple/idiomatic, not intentionally
crippled.

The optimized implementation should use appropriate:

- Polars vectorization;
- as-of joins;
- grouped aggregation;
- Parquet column projection;
- lazy execution where it materially helps.

---

# 43. PERFORMANCE VALIDITY

Before measuring speed:

verify baseline and optimized implementations agree numerically within
documented floating-point tolerance.

Do not compare outputs that are semantically different.

---

# 44. BENCHMARK METHODOLOGY

Run multiple repetitions.

Prefer median runtime rather than cherry-picked minimum.

Record:

- OS;
- Python version;
- CPU/system information where available;
- input row count;
- fill count;
- number of runs;
- median runtime;
- memory usage if practical.

---

# 45. PERFORMANCE GOALS

Targets, NOT guaranteed results:

Core analytics:

>= 10x faster

than straightforward Python-loop baseline for the benchmarked operation.

Also aim for:

< 30 seconds

for the full benchmark workload on the developer machine.

Stretch:

< 10 seconds

if naturally achievable.

If targets are missed:

- keep real results;
- profile bottlenecks;
- optimize legitimately;
- report remaining limitation honestly.

NEVER manipulate benchmark size just to claim success.

---

# 46. API PERFORMANCE GOALS

After data is prepared/precomputed:

health endpoint local p95 target:

< 50 ms

order detail/TCA local p95 target:

< 300 ms

Measure rather than assume.

These targets are secondary to correctness.

---

# 47. BENCHMARK OUTPUT

Generate machine-readable benchmark results, for example:

benchmarks/results.json

and human-readable:

docs/BENCHMARKS.md

Dashboard benchmark page should read real generated results where practical.

---

# 48. EXPECTED README BENCHMARK VISUAL

Once REAL results exist, README may show something conceptually like:

TCA processing benchmark

Python baseline  █████████████████████████████
Polars           ███

Measured speedup: X.Xx
Dataset: Y fills / Z market rows

X/Y/Z must come from actual benchmarks.

Never invent them.

---

# 49. DOCKER

Create a practical Docker setup.

At minimum:

- application Dockerfile;
- reproducible dependency installation;
- ability to run API;
- ability to run dashboard.

Avoid unnecessarily complicated container orchestration.

docker-compose may be used if it materially improves demo ergonomics.

---

# 50. CI

GitHub Actions should run on push/pull request.

CI should run at minimum:

- Ruff
- formatting check
- mypy
- pytest

Do not run enormous benchmarks on every commit if that makes CI unreasonable.

Use a small smoke benchmark in CI if desired.

---

# 51. PROGRESS TRACKING

Create:

PROGRESS.md

Immediately.

Maintain it throughout autonomous work.

Suggested format:

# ExecLens Progress

Current stage:

Last successful commit:

Completed:
- ...

Currently working on:
- ...

Next:
- ...

Tests last run:
- command
- result

Benchmark state:
- ...

Known limitations:
- ...

Important decisions:
- ...

If context is refreshed or compacted:

read this file and git history before continuing.

---

# 52. GIT WORKFLOW

Initialize Git if required.

Make small coherent commits as meaningful components become stable.

Use messages such as:

chore: bootstrap ExecLens project

feat: add deterministic market generator

feat: implement transaction cost analytics

perf: vectorize execution analytics

feat: add execution simulator

feat: add execution diagnostics

feat: expose analytics through API

feat: add execution dashboard

test: harden execution analytics coverage

docs: complete ExecLens documentation

Do NOT wait until the very end for the first commit.

---

# 53. GIT SAFETY

Never:

- force push;
- rewrite shared history;
- commit secrets;
- commit .env credentials;
- commit personal financial records;
- commit brokerage screenshots;
- commit oversized generated datasets.

If a remote already exists:

push completed stable checkpoints when appropriate.

If no remote exists:

continue local implementation without blocking.

At the END:

if GitHub CLI is installed and authenticated, it is acceptable to create a
repository named:

execlens

and push the completed project.

Prefer a PRIVATE repository initially unless explicitly configured otherwise.

If GitHub authentication is unavailable, this is one of the few acceptable
external blockers: finish everything locally and report the exact remaining
push command.

Never force push.

---

# 54. SECURITY / PRIVACY

The repository must contain no:

- brokerage login details;
- account numbers;
- private portfolio screenshots;
- actual private financial records;
- API tokens;
- authentication secrets.

Use synthetic data in demos.

---

# 55. DEVELOPMENT SEQUENCE

Work in this approximate sequence.

Do NOT require user approval between phases.

## Phase 0 — planning/bootstrap

- inspect repository;
- initialize Git;
- create PROGRESS.md;
- determine minimal dependency set;
- create package skeleton;
- configure pyproject;
- configure Ruff/mypy/pytest;
- create CI;
- establish CLI;
- smoke test;
- commit.

---

## Phase 1 — data layer

Implement:

- models;
- validation;
- synthetic market generator;
- scenario generator;
- Parquet persistence;
- small demo dataset;
- tests;
- CLI generation command.

Validate determinism.

Commit.

---

## Phase 2 — TCA engine

Implement:

- temporal alignment;
- average fill price;
- arrival cost;
- VWAP;
- VWAP cost;
- effective spread;
- markouts;
- participation;
- duration/completion.

Build manual calculation tests.

Perform independent financial correctness review.

Fix legitimate issues.

Create METRICS.md.

Commit.

---

## Phase 3 — performance

Create:

- reference implementation;
- vectorized Polars implementation;
- benchmark harness.

Validate numerical equivalence.

Run benchmark.

Optimize bottlenecks.

Record actual results.

Commit.

---

## Phase 4 — execution simulator

Implement:

- strategy interface;
- Immediate;
- TWAP;
- historical-profile VWAP;
- POV;
- fill model;
- market impact approximation;
- scenario comparison.

Perform explicit information-leakage review.

Commit.

---

## Phase 5 — diagnostics

Implement:

- contextual features;
- robust thresholds;
- explainable flags.

Avoid causal claims.

Commit.

---

## Phase 6 — application interfaces

Implement:

- application services;
- CLI;
- FastAPI;
- OpenAPI;
- API tests;
- local API benchmark.

Commit.

---

## Phase 7 — dashboard

Build professional Streamlit dashboard:

- overview;
- order explorer;
- diagnostics;
- strategy lab;
- benchmark results.

Verify dashboard values match domain engine.

Commit.

---

## Phase 8 — hardening

Perform an independent engineering review covering:

- architecture;
- financial correctness;
- leakage;
- performance;
- typing;
- errors;
- reproducibility;
- tests;
- Docker;
- CI.

Fix legitimate findings.

Run complete quality gate.

Commit.

---

## Phase 9 — documentation/demo

Finish:

README.md
ARCHITECTURE.md
METRICS.md
BENCHMARKS.md
LIMITATIONS.md
INTERVIEW_GUIDE.md

Run fresh-clone-like setup test where feasible.

Commit.

Push if safe and authenticated.

---

# 56. AUTONOMOUS ERROR HANDLING

When a command fails:

1. inspect failure;
2. determine root cause;
3. fix it;
4. rerun relevant test;
5. rerun broader gate if necessary;
6. continue.

Do NOT stop merely to report ordinary build/test failures.

Do NOT solve failures by:

- deleting important tests;
- lowering correctness requirements;
- hardcoding outputs;
- disabling type checks globally;
- swallowing exceptions;
- removing benchmark workloads.

---

# 57. FINANCIAL CORRECTNESS REVIEW

After core TCA implementation, independently review:

- formulas;
- side convention;
- bps conversion;
- quantity weighting;
- as-of timestamp semantics;
- stale data;
- markout convention;
- VWAP interval;
- BUY/SELL symmetry.

Derive at least one calculation manually.

Treat financial correctness as higher priority than UI work.

---

# 58. PERFORMANCE REVIEW

Before finalizing performance claims independently check:

- equivalent workloads;
- equivalent outputs;
- benchmark warmup;
- repeated runs;
- median measurement;
- dataset scale;
- absence of I/O mismatch between implementations;
- no hidden caching advantage unless documented.

---

# 59. FINAL ENGINEERING REVIEW

Before declaring completion check:

- no duplicated domain logic;
- no unnecessary services;
- no dead files;
- no placeholder functions;
- no TODOs blocking core functionality;
- no secrets;
- no private data;
- no fabricated values;
- no misleading finance claims;
- README commands actually work.

---

# 60. DOCUMENTATION

## README.md

README should communicate the project to a technical interviewer in about
3 minutes.

Recommended structure:

1. ExecLens title
2. one-line value proposition
3. problem
4. architecture
5. example
6. features
7. execution metrics
8. strategy simulator
9. performance benchmark
10. screenshots
11. quick start
12. testing
13. technology
14. limitations
15. roadmap

Keep it polished.

Do not make it excessively academic.

---

# 61. PROJECT DESCRIPTION

Use a concise explanation similar to:

"ExecLens is a Python-based electronic execution analytics platform that
automates transaction-cost analysis by aligning orders and fills with market
data, benchmarking executions against arrival price and VWAP, measuring
effective spread and post-trade markouts, flagging anomalous executions, and
comparing algorithmic execution strategies in reproducible market scenarios."

Adjust wording to actual implementation.

---

# 62. ARCHITECTURE DOCUMENTATION

ARCHITECTURE.md should explain:

- domain layers;
- data flow;
- key modules;
- interfaces;
- persistence;
- why Polars;
- why DuckDB/Parquet;
- why FastAPI;
- why Streamlit;
- decisions NOT to use heavier infrastructure.

Include Mermaid diagrams if useful.

---

# 63. METRICS DOCUMENTATION

METRICS.md must explain:

- bid;
- ask;
- midpoint;
- basis point;
- spread;
- VWAP;
- arrival price;
- implementation shortfall;
- effective spread;
- markout;
- participation rate;
- completion ratio.

For each:

- formula;
- sign convention;
- example;
- interpretation;
- limitation.

This document should be strong enough for project interview preparation.

---

# 64. LIMITATIONS DOCUMENT

Be explicit.

Examples:

- synthetic Level-1 market;
- simplistic impact model;
- no queue-position modelling;
- no venue routing;
- no exchange latency model;
- no hidden liquidity;
- no real brokerage connectivity;
- not a regulatory best-execution product;
- simulated results do not establish trading profitability.

A credible limitations section improves the project.

---

# 65. INTERVIEW GUIDE

After implementation is complete, inspect the ACTUAL code and build:

docs/INTERVIEW_GUIDE.md

Prepare the owner to answer questions about:

1. motivation;
2. electronic trading;
3. order lifecycle;
4. bid/ask;
5. midpoint;
6. basis points;
7. arrival price;
8. implementation shortfall;
9. VWAP;
10. effective spread;
11. markout;
12. participation;
13. TWAP;
14. VWAP execution;
15. POV;
16. simulated market impact;
17. timestamp alignment;
18. as-of joins;
19. look-ahead bias;
20. Polars;
21. vectorization;
22. Parquet;
23. DuckDB;
24. FastAPI;
25. Pydantic;
26. Streamlit;
27. benchmarking;
28. testing;
29. Docker;
30. architecture;
31. limitations;
32. what a real institutional implementation would require.

For each important question provide:

- 20-second answer;
- deeper explanation;
- relevant source file.

Do not describe unimplemented features.

---

# 66. PORTFOLIO STORY

Documentation should support this narrative without overemphasizing it:

"I had personal exposure to investing through stocks and mutual funds. Retail
brokerage applications make investment returns very visible, but I became
curious about a different problem: once an order is executed, how do we
measure whether the execution itself was efficient relative to market
conditions?

For an individual trade this could be inspected manually. At institutional
scale, repeated analysis across thousands of fills becomes a data and
automation problem.

That led me to build ExecLens."

Do NOT put personal brokerage screenshots in the repository.

---

# 67. FINAL RESUME DATA

At completion generate:

docs/RESUME_NOTES.md

It should contain several potential resume bullets using ONLY measured and
implemented facts.

For example, only if supported by real results:

"Built a Python transaction-cost-analysis engine benchmarking executions
against arrival price, VWAP, effective spread and post-trade markouts."

"Implemented TWAP, VWAP and POV execution simulations across reproducible
liquidity/volatility regimes."

"Vectorized execution analytics using Polars, processing X fills across Y
market records in Z seconds, N× faster than the reference Python
implementation."

Never invent X/Y/Z/N.

---

# 68. OPTIONAL ENHANCEMENTS

Only after the complete Definition of Done is met consider small high-value
enhancements.

Potential options:

- Isolation Forest anomaly detector;
- downloadable CSV/PDF execution report;
- configurable scenario YAML;
- richer benchmark visualizations;
- profile-guided Polars improvements.

Do NOT implement a major new system at the expense of polish.

Do not add ML simply because the project owner has an AI background.

---

# 69. DEFINITION OF DONE

Do NOT declare completion until all applicable items below are satisfied.

## Installation

A clean environment can install the project.

## Data

Synthetic market generation works deterministically.

## Analytics

TCA engine calculates documented metrics correctly.

## Temporal integrity

No accidental future data leakage exists in contemporaneous metrics.

## Simulation

Immediate/TWAP/VWAP/POV work.

## Diagnostics

Executions can be flagged with transparent reasons.

## CLI

Core workflow is usable from CLI.

## API

FastAPI application works.

## Dashboard

Streamlit application works.

## Tests

Required tests pass.

## Coverage

Core coverage target is achieved or shortfall is explicitly documented.

## Lint

Ruff passes.

## Formatting

Formatting check passes.

## Types

mypy passes at the agreed scope.

## Benchmark

Real benchmark has been executed and documented.

## Docker

Docker image builds and smoke test succeeds.

## CI

CI configuration is valid.

## Documentation

README and docs reflect the actual implementation.

## Git

Repository has coherent commit history.

## Security

No secrets/private financial data are included.

## Honesty

No fabricated metrics or benchmark results are present.

---

# 70. FINAL VERIFICATION

At the very end:

1. run full lint;
2. run formatting check;
3. run mypy;
4. run full tests with coverage;
5. run representative benchmark;
6. build Docker image;
7. perform smoke test;
8. inspect git status;
9. inspect tracked files;
10. scan for obvious secrets/private data;
11. compare README claims with actual implementation.

Fix problems rather than merely reporting them where technically possible.

---

# 71. FINAL REPORT

When everything possible has been completed, provide the user a concise final
report containing:

## Completed

Major capabilities implemented.

## Architecture

Short architecture summary.

## Verification

Actual:
- test count/results;
- coverage;
- lint result;
- mypy result;
- Docker result.

## Benchmarks

Actual measured:
- data sizes;
- baseline time;
- optimized time;
- speedup;
- API performance where measured.

## Repository

- latest commit;
- branch;
- remote status;
- push status.

## Run commands

Exact commands for:
- demo;
- API;
- dashboard;
- tests;
- benchmark.

## Interview highlights

Five strongest technical talking points.

## Remaining limitations

Only genuine remaining limitations.

Do not include a huge implementation diary.

---

# 72. STARTING INSTRUCTION

Begin immediately.

First:

1. inspect the working directory;
2. initialize Git if needed;
3. create PROGRESS.md;
4. build a concise implementation plan internally;
5. bootstrap the project;
6. continue phase by phase.

Do not stop merely to ask whether the plan looks good.

Default to implementation.

Be persistent and autonomous until the Definition of Done is reached.