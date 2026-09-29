"""Runtime settings read from environment variables.

Only non-secret configuration lives here; ExecLens needs no credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA_DIR = Path("data/sample")
DEFAULT_BENCHMARK_RESULTS = Path("benchmarks/results.json")
DEFAULT_STALE_QUOTE_TOLERANCE_SECONDS = 30.0


@dataclass(frozen=True)
class Settings:
    """Process-wide settings. Construct with :meth:`from_env` in applications."""

    data_dir: Path = DEFAULT_DATA_DIR
    benchmark_results: Path = DEFAULT_BENCHMARK_RESULTS
    stale_quote_tolerance_seconds: float = DEFAULT_STALE_QUOTE_TOLERANCE_SECONDS

    @classmethod
    def from_env(cls) -> Settings:
        tolerance = os.environ.get("EXECLENS_STALE_QUOTE_TOLERANCE_SECONDS")
        return cls(
            data_dir=Path(os.environ.get("EXECLENS_DATA_DIR", str(DEFAULT_DATA_DIR))),
            benchmark_results=Path(
                os.environ.get("EXECLENS_BENCHMARK_RESULTS", str(DEFAULT_BENCHMARK_RESULTS))
            ),
            stale_quote_tolerance_seconds=(
                float(tolerance) if tolerance else DEFAULT_STALE_QUOTE_TOLERANCE_SECONDS
            ),
        )
