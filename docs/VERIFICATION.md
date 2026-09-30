# Final verification

Checked September 30, 2026 on Windows 11, Python 3.12.14.

| Check | Result |
|---|---|
| Ruff lint | Passed |
| Ruff formatting | All files already formatted |
| Strict mypy | Passed, 47 source files |
| Full pytest suite | 300 passed in 91.94 s |
| TCA line coverage | 98.6% (348/353) |
| Simulation line coverage | 99.4% (472/475) |
| Diagnostics line coverage | 100% (124/124) |
| Core coverage gates | All exceeded 85% |
| Benchmark smoke test | Passed reference equivalence for in-memory and Parquet workloads, including the single-thread worker |
| README claims | Benchmark blocks match results JSON; sample metrics and all six strategy-table rows match fresh computation in tests |
| Obvious credential scan | No token/private-key patterns found in publishable project files |

Commands used (PowerShell from the repository root):

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy
$env:COVERAGE_FILE='data/derived/verification.coverage'
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=data/derived/verification-full-tmp --cov --cov-report=term --cov-report=json:data/derived/verification-coverage.json
.\.venv\Scripts\python.exe scripts/check_coverage.py data/derived/verification-coverage.json
.\.venv\Scripts\python.exe -m execlens.cli benchmark --smoke --no-api
```

Use a fresh temporary directory when repeating the suite. The first attempt
encountered access-denied errors in pre-existing pytest temporary/cache
directories. The complete rerun above used writable workspace-local paths.

The full-scale performance result retained in `benchmarks/results.json` was
recorded earlier in the same project session; this final pass reran the
representative smoke benchmark rather than replacing it with a battery-mode
measurement on a smaller dataset. The smoke result is not the resume speedup.

Dashboard pages passed headless Streamlit AppTest checks. The README
screenshots in `docs/images/` were captured with Playwright (Chromium,
1440 px wide, light theme) from the running dashboard on the committed sample
after the final code changes; they show the per-symbol flag counts (33
flagged, 13.8%) and rule descriptions.

## Container and CI configuration checks

The existing Docker Engine 29.1.3 in WSL Ubuntu-22.04 rebuilt the image with
the pinned base and locked runtime dependencies. The application package
was built and installed non-editably. These checks passed:

- `docker run --rm execlens:latest execlens analyze --top 1`: 240 orders,
  4,605 fills and 33 flagged orders (13.8%), matching the README.
- API subprocess inside the image: health, paginated order list, order TCA
  and an unknown-order 404 response.
- Dashboard subprocess inside the image: `/_stcore/health` returned `ok`.
- `docker compose config --quiet`.
- `rhysd/actionlint` validation of `.github/workflows/ci.yml`.

The API and dashboard smoke processes were terminated after checking them;
the test container was removed automatically. This pass validated Compose
configuration, not a new Compose deployment or a full Linux test-suite run.
GitHub-hosted CI results must be read from the workflow run after publication.
