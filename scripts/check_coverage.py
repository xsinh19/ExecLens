"""Enforce per-package line-coverage targets from a coverage.py JSON report.

Usage:
    pytest --cov --cov-report=json
    python scripts/check_coverage.py [coverage.json]

The build spec asks for >= 85% line coverage on the analytical core
(TCA, simulation, diagnostics); this script checks exactly those packages
instead of a single project-wide number that could hide a weak core.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

TARGETS = {
    "src/execlens/tca": 85.0,
    "src/execlens/simulation": 85.0,
    "src/execlens/diagnostics": 85.0,
}


def main(report_path: str = "coverage.json") -> int:
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    failed = False
    for package, target in TARGETS.items():
        statements = covered = 0
        for filename, data in report["files"].items():
            if Path(filename).as_posix().startswith(package + "/"):
                statements += data["summary"]["num_statements"]
                covered += data["summary"]["covered_lines"]
        if statements == 0:
            print(f"{package}: no statements measured")
            failed = True
            continue
        pct = 100.0 * covered / statements
        status = "ok" if pct >= target else "BELOW TARGET"
        print(
            f"{package}: {pct:.1f}% line coverage ({covered}/{statements}), "
            f"target {target}% -> {status}"
        )
        failed = failed or pct < target
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
