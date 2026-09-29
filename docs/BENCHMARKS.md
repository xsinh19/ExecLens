# ExecLens Benchmarks

This page explains what is measured and how. The **Results** section is
generated from `benchmarks/results.json` by `execlens benchmark`; no number on
this page is typed by hand.

## What is compared

Both implementations compute the same order-level TCA table: arrival context,
average fill price, completion, duration, interval market VWAP and volume,
arrival cost, VWAP cost, effective spread, quoted spread at fills, fill and
mid markouts at 10/30/60/300 s, and participation. Stale and missing handling
is identical.

| | Reference (`src/execlens/tca/reference.py`) | Optimized (`src/execlens/tca/engine.py`) |
|---|---|---|
| Style | Idiomatic pure Python: per-symbol lists in dicts, loops over orders and fills | Polars lazy frames |
| As-of lookups | `bisect` (binary search), O(log n) per lookup | `join_asof` by symbol (sorted merge) |
| Interval VWAP | `bisect` to find the slice, then sum the slice | Per-symbol running totals: `C(<= end) - C(< start)` via two as-of joins |
| Fill -> order roll-up | Python accumulation per order | `group_by(...).agg(...)` |
| Parallelism | single thread | Polars thread pool |

The reference is deliberately **not crippled**: it uses binary search rather
than linear scans, and it gets the same presorted inputs.

## Workloads

* `core_in_memory`: both implementations start from the same in-memory
  Polars DataFrames. The reference converts columns to Python lists as part
  of its work, which is the natural cost of pure-Python processing of
  columnar data.
* `end_to_end_parquet`: both start from the Parquet files on disk. The
  reference reads every column of every file eagerly; the engine scans lazily,
  so Polars reads only the columns the query needs (column projection).

## Methodology

1. **Dataset.** The `benchmark` preset (`execlens generate --preset benchmark`,
   seed 1): 6 synthetic symbols x 2 full trading days. The harness refuses to
   run a full benchmark on fewer than **1,000,000 market observations**
   (quotes + trades) or **100,000 fills**.
2. **Validation before timing.** One warm-up run of each implementation is
   compared column by column (`compare_to_reference`). Integer and string
   columns must match exactly; floats must have identical nulls and satisfy
   `|a - b| <= 1e-6 + 1e-9 * |b|`. Any disagreement aborts the run. The
   tolerance absorbs only floating-point summation-order differences.
3. **Warm-up.** The validation runs double as warm-up and are not timed.
4. **Interleaved repetitions.** By default 5 timed runs of each, alternating
   reference, Polars, reference, and so on. Alternating means CPU frequency
   drift (thermal limits, battery power plans, background load) affects both
   implementations alike instead of penalising whichever runs later. The
   **median** is reported, together with min/max, and every run is kept in the
   JSON file.
5. **Memory.** Peak resident-set growth above the pre-run baseline, sampled
   every 5 ms during one separate, untimed run of each implementation
   (approximate, process-wide). The sampler never runs during timed runs.
6. **No hidden caching.** Every run recomputes from its inputs; nothing is
   memoised between repetitions. Input validation happens once, before
   timing, for both implementations.
7. **Environment.** OS, Python, CPU model, logical CPU count, RAM, power
   source (battery or AC, and the power scheme on Windows) and library
   versions are recorded with the results.

API latency (`run_api_benchmark`): the API is started with `uvicorn` in a
subprocess on the same benchmark dataset. Its analysis is precomputed at
startup. A single sequential keep-alive client then issues warm-up requests,
followed by timed GET requests per endpoint, and records client-side latency
percentiles.

Reproduce everything with:

```bash
execlens benchmark                 # full run, writes benchmarks/results.json and this page
execlens benchmark --smoke         # quick sanity run on data/sample (prints only)
```

## Results

<!-- BENCHMARK-RESULTS:START -->
Measured 2026-09-29T21:19:40+00:00 on 12th Gen Intel(R) Core(TM) i7-12650H (16 logical CPUs, 15.6 GB RAM), Windows-11-10.0.26200-SP0, Python 3.12.14, Polars 1.44.2 (16 threads). Machine was on AC power, power scheme 'Silent'.

Dataset: 1,053,935 quotes + 493,401 trades = 1,547,336 market observations; 5,400 parent orders; 125,714 fills; 6 symbols.

| Workload | Reference median (s) | Polars median (s) | Speedup | Runs (ref / Polars) | Peak extra RSS MB (ref / Polars) |
|---|---:|---:|---:|---:|---:|
| `core_in_memory` | 7.859 | 0.687 | **11.4x** | 5 / 5 | 268.8 / 270.0 |
| `end_to_end_parquet` | 4.459 | 0.364 | **12.2x** | 5 / 5 | 243.9 / 152.9 |

```text
Core TCA (in memory), median wall-clock time
Python reference  ######################################## 7.86 s
Polars engine     ### 0.69 s
Measured speedup: 11.4x
```
<!-- BENCHMARK-RESULTS:END -->

## Reading the results honestly

* Speedups are specific to this machine, dataset and library versions.
* Much of the gain comes from moving loops out of the Python interpreter
  and from the as-of join / running-total formulation. Part comes from Polars
  using several threads while the reference uses one.
* The development machine is a laptop. Under its quiet power profile,
  sustained all-core load lowers clock speeds after a few seconds: the same
  Polars run takes about 0.3 s when the CPU is fresh and about 1 s when it is
  throttled. The single-threaded reference is less affected. Interleaving the
  runs puts both implementations in the same conditions, and the JSON file
  keeps every individual run so the spread stays visible. On a desktop or a
  machine on a performance profile, expect lower absolute times for both.
* The absolute times are small, so the end-to-end numbers include
  non-trivial fixed costs such as reading Parquet.
