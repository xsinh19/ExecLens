# ExecLens Progress

Current stage: Phase 3 — performance benchmark (Phases 0, 1, 2, 4 done)

Last successful commit: c8bd209 feat: add execution simulator and synthetic dataset generation

## Completed
- Phase 0: bootstrap (pyproject, uv.lock, Ruff/mypy/pytest config, CI workflow, coverage gate script).
- Phase 1: data model (Pydantic records + Polars schemas), vectorised validation, seeded synthetic
  generator (6 scenarios), Parquet persistence, `execlens generate`, committed `data/sample`.
- Phase 2: TCA engine (as-of alignment, arrival/VWAP cost, effective spread, markouts, participation,
  completion), loop-based reference implementation, aggregation, docs/METRICS.md.
  Hand-calculated, edge-case, temporal (no look-ahead) and engine-vs-reference tests.
- Phase 4 (done before 3 because the benchmark needs realistic fills): simulator with Immediate,
  TWAP, historical-profile VWAP, POV, ORACLE_VWAP (hindsight, non-implementable), impact model,
  controlled strategy comparison, dataset presets (sample/demo/benchmark), leakage tests.

## Currently working on
- Independent financial-correctness review (subagent) of the TCA engine — pending results.
- Phase 3 benchmark harness (reference vs Polars on the `benchmark` preset: 1.05M quotes,
  493k trades, 125,714 fills).

## Next
- Phase 5 diagnostics (features + robust MAD flags) → Phase 6 services/CLI/API (+ API benchmark)
  → Phase 7 dashboard → Phase 8 hardening (review, Docker in WSL, CI) → Phase 9 docs.
- CLI `analyze` must expose `--stale-tolerance` (METRICS.md references it).

## Tests last run
- `pytest -q` → 177 passed (ruff, ruff format --check, mypy strict all clean).

## Benchmark state
- Benchmark preset generates in ~13 s: 1,053,935 quotes, 493,401 trades, 5,400 orders, 125,714 fills.
- No timing results yet.

## Known limitations
- Synthetic Level-1 market; square-root temporary impact only (no permanent impact, no queue
  position, no venues/latency). Simulated fills are not printed to the synthetic tape.
- Strategy comparison of 20 full-day high-volatility sessions takes ~3 s (generation dominated).

## Important decisions
- Python 3.12 via uv-managed interpreter (system 3.12 is broken); uv.lock for reproducibility.
- No look-ahead: backward as-of joins (`<=`), stale tolerance default 30 s, stale prices withheld.
- Markout convention: positive = favourable (opposite to costs). Future mid = prevailing quote at t+h;
  unavailable if data does not extend to t+h or the quote is stale.
- Order-level fill-weighted metrics are strict: null if any fill lacks context (count reported).
- VWAP interval: [arrival, arrival+horizon] or [arrival, last fill]; closed; running-total method.
- Arrival cost = execution component of implementation shortfall (no opportunity cost; completion
  ratio reported separately).
- Fills for synthetic datasets come from the simulator (one fill model everywhere).
- Commit trailers: no Co-Authored-By line (user previously asked to remove Claude as contributor on
  their portfolio repos) — flag in final report.
