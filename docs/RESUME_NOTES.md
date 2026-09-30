# Resume evidence

Project: **ExecLens: Transaction Cost Analysis Platform**. Development: September 2026.
Repository: https://github.com/xsinh19/ExecLens

## Claims and evidence

| Claim | Implementation or measurement |
|---|---|
| Python analytics with FastAPI, Polars and DuckDB over Parquet | `src/execlens/api/`, `tca/engine.py`, `data/repository.py`, `data/loader.py` |
| Point-in-time quote alignment | Backward as-of joins partitioned by symbol in `tca/alignment.py`; temporal-integrity tests reject future quotes |
| Interval VWAP using prefix sums | Per-symbol cumulative volume and notional, differenced at interval boundaries in `tca/alignment.py` |
| Execution strategy simulation | IMMEDIATE, TWAP, historical-profile VWAP and POV in `simulation/`; future-volume leakage tests |
| Bounded API simulation concurrency | Two semaphore slots and HTTP 503 with Retry-After; per-request work limits and HTTP 422 |
| 4.6x measured core TCA speedup | `benchmarks/results.json`: 3.3941 s reference / 0.7376 s Polars = 4.602, medians of five interleaved runs |
| Benchmark scale | 1,547,336 synthetic market observations, 125,714 fills, 5,400 orders, six symbols |
| 300 passing tests | Final Windows suite on September 30, 2026; see `VERIFICATION.md` |

The full benchmark was recorded at 2026-09-29T23:41:07Z on an i7-12650H,
Windows 11, Python 3.12.14, Polars 1.44.2, AC power, Silent power scheme.
The reference uses binary search and prefix sums, not deliberately slow
interval scans. Outputs are checked before timing. The 4.6x ratio is for
in-memory core TCA, not API latency or end-to-end I/O; the latter measured
3.4x. Single-thread Polars measured 4.9x, so do not attribute the gain to
parallelism alone. See `BENCHMARKS.md` for all runs and limitations.

Do not claim real trading, profitability, distributed processing,
authentication, production deployment, or concurrent-load throughput.
All market records and fills are synthetic. Simulations run synchronously.

## Resume block

The compact bullets retain the requested LaTeX structure. Exact one-line
fit depends on the existing resume's font and available text width; that
template was not supplied for compilation.

```latex
\item \textbf{Transaction Cost Analysis Platform} $|$ \emph{\underline{\href{https://github.com/xsinh19/ExecLens}{Project Link}}} \hfill [Sep 2026] \\

\[-1.05cm]

\begin{itemize}\itemsep \isep

\item Developed a transaction-cost analysis platform using \textbf{Python}, \textbf{FastAPI}, \textbf{Polars}, and \textbf{DuckDB} over Parquet datasets.

\item Implemented \textbf{backward as-of joins} and \textbf{prefix sums} for point-in-time alignment and interval \textbf{VWAP} computation.

\item Accelerated TCA by \textbf{4.6$\times$} over a Python reference on \textbf{1.55M synthetic records} and \textbf{125K fills}.

\[-0.3cm]

\end{itemize}
```
