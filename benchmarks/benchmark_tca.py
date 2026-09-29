"""TCA benchmark: loop-based pure-Python reference vs vectorised Polars engine.

Runs the equivalence-checked, repeated, median-reported benchmark described in
docs/BENCHMARKS.md on the >1M-quote / >100k-fill benchmark dataset (generated
on first use) and records results in benchmarks/results.json.

    python benchmarks/benchmark_tca.py [--reference-repeats 5] [--optimized-repeats 10]

Equivalent to ``execlens benchmark --no-api``.
"""

import sys

from execlens.cli import app

if __name__ == "__main__":
    app(["benchmark", "--no-api", *sys.argv[1:]])
