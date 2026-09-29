"""Local API latency benchmark.

Starts the FastAPI app with uvicorn on the benchmark dataset (analysis is
precomputed at startup) and measures p50/p95/p99 latency of the main GET
endpoints from a sequential keep-alive client. Results are stored in the
``api`` section of benchmarks/results.json.

    python benchmarks/benchmark_api.py [--data data/generated/benchmark]
"""

import argparse
from pathlib import Path

from execlens.benchmarking import render_markdown, run_api_benchmark, write_results

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data/generated/benchmark"))
    parser.add_argument("--requests", type=int, default=300)
    parser.add_argument("--output", type=Path, default=Path("benchmarks/results.json"))
    args = parser.parse_args()
    results = run_api_benchmark(args.data, requests_per_endpoint=args.requests)
    write_results(results, args.output, "api")
    print(render_markdown({"api": results}))
