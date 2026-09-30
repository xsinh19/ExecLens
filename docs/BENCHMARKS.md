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
| Interval VWAP | `bisect` for the interval ends, then per-symbol prefix sums: `cum[hi] - cum[lo]` | Per-symbol running totals: `C(<= end) - C(< start)` via two as-of joins |
| Fill -> order roll-up | Python accumulation per order | `group_by(...).agg(...)` |
| Parallelism | single thread | Polars thread pool; also timed with one thread |

The reference is deliberately **not crippled**. It uses the same algorithmic
ideas as the engine: binary search instead of linear scans, and prefix sums,
so an interval's volume costs O(log n) in both. Both get the same presorted
inputs. The speedup therefore measures implementation technique
(vectorised, compiled kernels versus the interpreter), not a better
algorithm. An earlier version of the reference summed each interval's
trades, which made the ratio look larger; it was replaced after review.

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
4. **Interleaved repetitions.** By default 5 timed runs of each
   implementation. Runs are interleaved across implementations *and*
   workloads (reference, Polars, single-thread Polars for the in-memory
   workload, then the same for the Parquet workload, and repeat), so CPU
   frequency drift (thermal limits, power plans, background load) affects
   every measurement alike instead of penalising whichever runs later. The
   **median** is reported, together with min/max, and every run is kept in the
   JSON file.
5. **Single-thread run.** Polars fixes its thread pool when it is imported,
   so the one-thread engine runs in a worker subprocess started with
   `POLARS_MAX_THREADS=1`. The worker loads the same dataset, its output is
   checked against the reference before any timing, and it times only the
   in-memory TCA call. This separates the gain from vectorisation from the
   gain from using more cores.
6. **Memory.** Peak resident-set growth above the pre-run baseline, sampled
   every 5 ms during one separate, untimed run of each implementation
   (approximate, process-wide). The sampler never runs during timed runs.
7. **No hidden caching.** Every run recomputes from its inputs; nothing is
   memoised between repetitions. Input validation happens once, before
   timing, for both implementations.
8. **Environment.** OS, Python, CPU model, logical CPU count, RAM, power
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
execlens benchmark --smoke         # quick sanity run on data/sample (prints only;
                                   # run from the repository root)
```

## Results

<!-- BENCHMARK-RESULTS:START -->
Measured 2026-09-29T23:41:07+00:00 on 12th Gen Intel(R) Core(TM) i7-12650H (16 logical CPUs, 15.6 GB RAM), Windows-11-10.0.26200-SP0, Python 3.12.14, Polars 1.44.2 (16 threads). Machine was on AC power, power scheme 'Silent'.

Dataset: 1,053,935 quotes + 493,401 trades = 1,547,336 market observations; 5,400 parent orders; 125,714 fills; 6 symbols.

| Workload | Reference median (s) | Polars median (s) | Speedup | Runs (ref / Polars) | Peak extra RSS MB (ref / Polars) |
|---|---:|---:|---:|---:|---:|
| `core_in_memory` | 3.394 | 0.738 | **4.6x** | 5 / 5 | 285.3 / 168.7 |
| `end_to_end_parquet` | 3.443 | 1.004 | **3.4x** | 5 / 5 | 285.1 / 101.4 |

```text
Core TCA (in memory), median wall-clock time
Python reference      ######################################## 3.39 s
Polars, all threads   ######### 0.74 s
Polars, 1 thread      ######## 0.69 s
Measured speedup: 4.6x with 16 threads, 4.9x single-threaded
```

> One thread was at least as fast as all threads in this run, so the extra threads did not pay off here (see *Reading the results honestly* for why this happens on the development laptop).

API latency (300 sequential requests per endpoint after 30 warm-up requests, 5,400 orders loaded, measured 2026-09-29T23:41:32+00:00):

| Endpoint | p50 (ms) | p95 (ms) | p99 (ms) | Target p95 |
|---|---:|---:|---:|---|
| `/health` | 2.10 | 2.78 | 3.06 | < 50 ms |
| `/api/v1/orders?limit=100` | 7.39 | 8.73 | 9.45 | - |
| `/api/v1/orders/{order_id}` | 2.70 | 3.89 | 4.35 | < 300 ms |
| `/api/v1/orders/{order_id}/tca` | 2.75 | 3.81 | 4.15 | < 300 ms |
| `/api/v1/summary` | 1.80 | 2.25 | 2.84 | - |
<!-- BENCHMARK-RESULTS:END -->

## Reading the results honestly

* Speedups are specific to this machine, dataset and library versions.
* The gain comes from moving loops out of the Python interpreter into
  compiled, vectorised kernels, and partly from Polars using several threads
  while the reference uses one. The single-thread row separates the two.
* Extra threads help less than the logical CPU count suggests. The as-of
  joins are partitioned by symbol, and this dataset has only six symbols, so
  much of the work cannot spread across 16 threads. Joining on a composite
  (symbol, time) key instead was tried and rejected: building the key cost
  more than the parallelism gained.
* The development machine is a laptop. Under its quiet power profile,
  all-core load lowers clock speeds within seconds: the same Polars run takes
  about 0.3 s when the CPU is fresh and 0.6 to 1 s once it is throttled, and
  the medians above are the throttled state. The single-threaded reference is
  less affected. Interleaving the runs puts both implementations in the same
  conditions, and the JSON file keeps every individual run so the spread
  stays visible. On a desktop or a machine on a performance profile, expect
  lower absolute times for both and a larger gain from threads.
* The same power limit explains why sixteen Polars threads can be no faster
  than one: all cores share a reduced power budget, so each runs slower, and
  with six symbols much of the work cannot spread across them anyway.
* The absolute times are small, so the end-to-end numbers include
  non-trivial fixed costs such as reading Parquet.
