# ExecLens Progress

Current stage: Phase 9 complete; final local verification passed (2026-09-30).

Implementation baseline: f486b46 fix: bound strategy simulations and tighten API edge cases.
Final handoff: see docs/VERIFICATION.md and docs/RESUME_NOTES.md.

## Completed
- Phase 0 bootstrap: pyproject, uv.lock, Ruff/mypy(strict)/pytest config, CI, coverage gate.
- Phase 1 data layer: schemas, vectorised validation, seeded generator (6 scenarios), Parquet I/O,
  `execlens generate`, committed `data/sample`.
- Phase 2 TCA engine + loop reference + aggregation + METRICS.md; independent financial review done
  and all findings addressed (mid markouts, VWAP interval extension, unknown volume, docs).
- Phase 4 simulator: Immediate/TWAP/VWAP/POV + ORACLE_VWAP (hindsight), impact model, comparison
  with CI-aware, completion-aware conclusions; leakage tests.
- Phase 3 benchmark harness (equivalence check, interleaved runs, medians, power state), results in
  benchmarks/results.json and docs/BENCHMARKS.md.
- Phase 5 diagnostics: features (pre-arrival vol/momentum, interval move, volume rate) and flags
  (robust MAD z-scores, absolute thresholds, data-quality flags, context notes).
- Phase 6 API (FastAPI) + CLI (analyze/simulate/demo/query/serve/dashboard/benchmark) + API latency
  benchmark.
- Phase 7 dashboard (5 pages, validated palette, AppTest suite, screenshot-verified).
- Regression fixture (tests/fixtures/regression) with expected TCA + strategy fills.
- Docker (multi-stage, 1.22 GB, base pinned by digest) built in WSL; smoke tests (CLI, API,
  dashboard) and compose stack verified; CI Docker job; actionlint clean; Dependabot config.
- Independent engineering review: all findings fixed (commits 470c4dc, a38caa1, f486b46):
  - fair benchmark: prefix-sum reference, single-thread worker, cross-workload interleaving;
  - execution flags compare within each symbol (sample: 33 flagged = 13.8%, was 73 = 30.4%);
  - bounded simulations: per-trial market windows, work budget (250k simulated s, 100k
    decisions), API concurrency limit (2, then 503 + Retry-After), dashboard error message;
  - UTC-safe `to_epoch_us` in data.schemas; FastAPI-format domain 422s; bounded `offset`;
    `extra="forbid"`; /health shows dataset name; dead code removed; real VWAP leakage test.
- Docs updated for the above (README excerpt/table regenerated, BENCHMARKS method, LIMITATIONS,
  ARCHITECTURE, DATA_MODEL, INTERVIEW_GUIDE).

## Final handoff
- Benchmark results and README/BENCHMARKS blocks agree, enforced by documentation tests.
- Resume notes contain three compact LaTeX bullets with implementation and measurement evidence.
- Stale dashboard screenshots were archived under ignored data/derived/archived-screenshots;
  browser connections were unavailable, so README does not publish outdated figures.
- Docker image rebuilt with the existing WSL Ubuntu-22.04 engine; CLI, API and dashboard
  smoke tests passed. Compose configuration and actionlint validation passed.
- GitHub destination configured as origin: https://github.com/xsinh19/ExecLens.git.
- No core implementation tasks remain. Publication status is confirmed against remote main
  after the final commit is pushed; hosted CI is separate from local verification.

## Tests last run
- Full pytest suite: 300 passed in 89.24 s, using fresh workspace-local temporary paths.
- Line coverage: TCA 98.6%, simulation 99.4%, diagnostics 100%; all 85% gates passed.
- Ruff lint/format and strict mypy passed; benchmark smoke equivalence passed.

## Benchmark state
- Recorded full benchmark: core 4.6x, end-to-end 3.4x, single-thread core 4.9x.
- 1,547,336 market observations, 125,714 fills, five interleaved runs; synthetic data only.
- API p95: health 2.8 ms, order detail 3.9 ms, order TCA 3.8 ms (sequential local requests).
- Final smoke benchmark checked equivalence on the small sample; it did not overwrite full results.

## Known limitations
- See docs/LIMITATIONS.md (synthetic L1 data, simplified impact, no permanent impact, own fills not on
  the tape, stylised algorithms, windows not whole days in comparisons, machine-specific benchmarks).

## Important decisions
- Python 3.12 via uv-managed interpreter (system 3.12 broken); uv.lock everywhere (local, CI, Docker).
- No look-ahead: backward as-of joins; stale tolerance 30 s; stale prices withheld.
- Markouts positive = favourable; fill markout (spec) + mid markout (adverse selection).
- Strict fill aggregation; unavailable counts reported.
- Flags = execution + data-quality issues, compared within the symbol; market conditions = context
  notes compared book-wide (non-causal wording).
- Strategy ranking only among strategies completing >= 95%.
- Docker Engine installed inside WSL Ubuntu (docker.io, docker-buildx, docker-compose-v2) to build
  and smoke-test the image — mention in final report. Running the Docker build concurrently with
  heavy local work crashed WSL once (memory): run it alone.
- Commit trailers: no Co-Authored-By line (user asked to remove Claude as contributor on portfolio
  repos) — flag in final report.
