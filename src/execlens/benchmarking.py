"""Benchmark harness: equivalence-checked, repeated, median-reported timings.

Methodology (see ``docs/BENCHMARKS.md``):

* Both implementations receive identical inputs and compute the same
  order-level TCA metrics. Before any timing, one warm-up run of each is
  compared column by column (:func:`execlens.tca.reference.compare_to_reference`);
  a disagreement aborts the benchmark.
* Timed repetitions of the two implementations are **interleaved**
  (reference, optimized, reference, ...) so that CPU frequency drift, e.g. a
  laptop throttling on battery, affects both alike. The **median** wall-clock
  time is reported with min/max and every individual run.
* Peak resident memory above the pre-run baseline is sampled every few
  milliseconds in a separate, untimed run (approximate, process-wide), so the
  sampler cannot distort the timings.
* Environment (OS, Python, CPU, core count, RAM, power source, library
  versions) and dataset size are recorded with the results.
"""

from __future__ import annotations

import http.client
import json
import os
import platform
import socket
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import numpy as np
import polars as pl

from execlens import __version__
from execlens.data.loader import read_dataset, scan_table, table_path
from execlens.tca.engine import TcaConfig, run_tca
from execlens.tca.reference import compare_to_reference, reference_tca

MIN_MARKET_ROWS: Final = 1_000_000
MIN_FILLS: Final = 100_000
RESULTS_START: Final = "<!-- BENCHMARK-RESULTS:START -->"
RESULTS_END: Final = "<!-- BENCHMARK-RESULTS:END -->"


class BenchmarkError(RuntimeError):
    """The benchmark cannot produce a valid measurement."""


@dataclass
class Measurement:
    runs_s: list[float]
    peak_rss_mb: float | None = None

    @property
    def median_s(self) -> float:
        return statistics.median(self.runs_s)

    def as_dict(self) -> dict[str, Any]:
        return {
            "runs_s": [round(r, 4) for r in self.runs_s],
            "median_s": round(self.median_s, 4),
            "min_s": round(min(self.runs_s), 4),
            "max_s": round(max(self.runs_s), 4),
            "repetitions": len(self.runs_s),
            "peak_rss_mb": None if self.peak_rss_mb is None else round(self.peak_rss_mb, 1),
        }


@dataclass
class _PeakRss:
    """Samples process RSS in a background thread; ``None`` without psutil."""

    interval_s: float = 0.005
    peak_mb: float | None = None
    _stop: threading.Event = field(default_factory=threading.Event)

    def __enter__(self) -> _PeakRss:
        try:
            import psutil
        except ImportError:  # pragma: no cover - psutil is a dev dependency
            return self
        process = psutil.Process()
        baseline = process.memory_info().rss
        self.peak_mb = 0.0

        def sample() -> None:
            while not self._stop.is_set():
                delta = (process.memory_info().rss - baseline) / 2**20
                self.peak_mb = max(self.peak_mb or 0.0, delta)
                time.sleep(self.interval_s)

        self._thread = threading.Thread(target=sample, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if hasattr(self, "_thread"):
            self._thread.join()


def _elapsed(fn: Callable[[], object]) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def peak_memory_mb(fn: Callable[[], object]) -> float | None:
    """Peak RSS growth during one (untimed) call of ``fn``."""
    with _PeakRss() as rss:
        fn()
    return rss.peak_mb


def measure_interleaved(
    reference: Callable[[], object],
    optimized: Callable[[], object],
    reference_repeats: int,
    optimized_repeats: int,
) -> tuple[Measurement, Measurement]:
    """Alternate timed runs of both implementations, then measure memory once each.

    Callers run their own warm-up first.
    """
    if reference_repeats < 1 or optimized_repeats < 1:
        raise ValueError("repeats must be >= 1")
    ref_runs: list[float] = []
    opt_runs: list[float] = []
    for i in range(max(reference_repeats, optimized_repeats)):
        if i < reference_repeats:
            ref_runs.append(_elapsed(reference))
        if i < optimized_repeats:
            opt_runs.append(_elapsed(optimized))
    return (
        Measurement(ref_runs, peak_memory_mb(reference)),
        Measurement(opt_runs, peak_memory_mb(optimized)),
    )


def power_state() -> dict[str, Any] | None:
    """Battery / AC status and (on Windows) the active power scheme, if known."""
    state: dict[str, Any] = {}
    try:
        import psutil

        battery = psutil.sensors_battery()
        if battery is not None:
            state["on_battery"] = not battery.power_plugged
            state["battery_percent"] = round(battery.percent)
    except (ImportError, RuntimeError):  # pragma: no cover - platform dependent
        pass
    if sys.platform == "win32":
        try:
            output = subprocess.run(
                ["powercfg", "/getactivescheme"], capture_output=True, text=True, check=True
            ).stdout
            if "(" in output:
                state["power_scheme"] = output.rsplit("(", 1)[1].split(")", 1)[0]
        except (OSError, subprocess.SubprocessError):
            pass
    return state or None


def cpu_name() -> str:
    try:
        if sys.platform == "win32":
            import winreg

            key_path = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        elif sys.platform == "linux":
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        elif sys.platform == "darwin":
            return subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return platform.processor() or "unknown"


def environment() -> dict[str, Any]:
    memory_gb: float | None = None
    try:
        import psutil

        memory_gb = round(psutil.virtual_memory().total / 2**30, 1)
    except ImportError:  # pragma: no cover - psutil is a dev dependency
        pass
    return {
        "os": platform.platform(),
        "python": platform.python_version(),
        "cpu": cpu_name(),
        "logical_cpus": os.cpu_count(),
        "memory_gb": memory_gb,
        "power": power_state(),
        "polars": pl.__version__,
        "polars_threads": pl.thread_pool_size(),
        "numpy": np.__version__,
        "execlens": __version__,
    }


def _read_eager(
    directory: Path,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Read every column of every table, as a straightforward implementation would."""
    return (
        pl.read_parquet(table_path(directory, "quotes")),
        pl.read_parquet(table_path(directory, "trades")),
        pl.read_parquet(table_path(directory, "orders")),
        pl.read_parquet(table_path(directory, "fills")),
    )


def run_tca_benchmark(
    dataset_dir: Path,
    *,
    reference_repeats: int = 3,
    optimized_repeats: int = 7,
    config: TcaConfig | None = None,
    enforce_scale: bool = True,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Benchmark the loop reference against the vectorised engine.

    Two workloads are measured:

    * ``core_in_memory`` - both start from the same in-memory Polars frames;
    * ``end_to_end_parquet`` - both start from the Parquet files on disk: the
      reference reads every file eagerly, the engine scans lazily so Polars
      can skip unused columns.
    """
    config = config or TcaConfig()
    dataset = read_dataset(dataset_dir)  # validated once, outside any timing
    q, t, o, f = dataset.quotes, dataset.trades, dataset.orders, dataset.fills
    scale = {
        "quotes": q.height,
        "trades": t.height,
        "market_observations": q.height + t.height,
        "orders": o.height,
        "fills": f.height,
        "symbols": q["symbol"].n_unique(),
        "metadata_seed": dataset.metadata.get("spec", {}).get("market", {}).get("seed"),
    }
    if enforce_scale and (scale["market_observations"] < MIN_MARKET_ROWS or f.height < MIN_FILLS):
        raise BenchmarkError(
            f"dataset too small for the benchmark: {scale['market_observations']:,} market rows "
            f"(need {MIN_MARKET_ROWS:,}) and {f.height:,} fills (need {MIN_FILLS:,})"
        )

    workloads: dict[str, tuple[str, Callable[[], pl.DataFrame], Callable[[], pl.DataFrame]]] = {
        "core_in_memory": (
            "Order-level TCA from in-memory Polars frames (no I/O).",
            lambda: reference_tca(q, t, o, f, config),
            lambda: run_tca(q, t, o, f, config, validate=False).orders,
        ),
        "end_to_end_parquet": (
            "Order-level TCA starting from Parquet files on disk (includes reading).",
            lambda: reference_tca(*_read_eager(dataset_dir), config),
            lambda: (
                run_tca(
                    scan_table(dataset_dir, "quotes"),
                    scan_table(dataset_dir, "trades"),
                    scan_table(dataset_dir, "orders"),
                    scan_table(dataset_dir, "fills"),
                    config,
                    validate=False,
                ).orders
            ),
        ),
    }

    results: dict[str, Any] = {}
    for name, (description, reference, optimized) in workloads.items():
        log(f"[{name}] warm-up + equivalence check")
        problems = compare_to_reference(optimized(), reference())
        if problems:
            raise BenchmarkError(f"{name}: implementations disagree: {problems}")
        log(f"[{name}] timing: reference x{reference_repeats}, optimized x{optimized_repeats}")
        ref, opt = measure_interleaved(reference, optimized, reference_repeats, optimized_repeats)
        results[name] = {
            "description": description,
            "reference": ref.as_dict(),
            "optimized": opt.as_dict(),
            "speedup_median": round(ref.median_s / opt.median_s, 3),
        }
        log(
            f"[{name}] reference {ref.median_s:.3f}s | optimized {opt.median_s:.3f}s | "
            f"speedup {ref.median_s / opt.median_s:.1f}x"
        )

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "environment": environment(),
        "dataset": {"path": dataset_dir.as_posix(), **scale},
        "validation": {
            "equivalent": True,
            "rtol": 1e-9,
            "atol": 1e-6,
            "rule": "integer/string columns exact; floats |a-b| <= atol + rtol*|b|; same nulls",
        },
        "workloads": results,
    }


API_TARGET_P95_MS: Final = {
    "/health": 50,
    "/api/v1/orders/{order_id}": 300,
    "/api/v1/orders/{order_id}/tca": 300,
}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _get(connection: http.client.HTTPConnection, path: str) -> bytes:
    connection.request("GET", path)
    response = connection.getresponse()
    body = response.read()
    if response.status != 200:
        raise BenchmarkError(f"GET {path} returned HTTP {response.status}")
    return body


def run_api_benchmark(
    dataset_dir: Path,
    *,
    requests_per_endpoint: int = 300,
    warmup_requests: int = 30,
    startup_timeout_s: float = 180.0,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Latency of the local API after its analysis has been precomputed at startup.

    Starts ``uvicorn`` in a subprocess on a free local port, waits for
    ``/health``, then issues sequential keep-alive GET requests from a single
    client and records client-side wall-clock latency per request.
    """
    port = _free_port()
    env = os.environ | {"EXECLENS_DATA_DIR": str(dataset_dir)}
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "execlens.api.app:create_app",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]
    log(f"[api] starting server on port {port} (analysis is precomputed at startup)")
    server = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + startup_timeout_s
        startup_start = time.perf_counter()
        while True:
            if server.poll() is not None:
                stderr = server.stderr.read().decode(errors="replace") if server.stderr else ""
                raise BenchmarkError(f"API server exited during startup:\n{stderr[-2000:]}")
            try:
                probe = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                _get(probe, "/health")
                probe.close()
                break
            except (OSError, BenchmarkError):
                if time.monotonic() > deadline:
                    raise BenchmarkError("API server did not become healthy in time") from None
                time.sleep(0.2)
        startup_s = time.perf_counter() - startup_start

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        listing = json.loads(_get(connection, "/api/v1/orders?limit=50"))
        order_ids = [item["order_id"] for item in listing["items"]]
        total_orders = int(listing["total"])
        if not order_ids:
            raise BenchmarkError("the API returned no orders to benchmark")
        templates = [
            "/health",
            "/api/v1/orders?limit=100",
            "/api/v1/orders/{order_id}",
            "/api/v1/orders/{order_id}/tca",
            "/api/v1/summary",
        ]
        endpoints: list[dict[str, Any]] = []
        for template in templates:
            paths = [template.format(order_id=oid) for oid in order_ids]
            for i in range(warmup_requests):
                _get(connection, paths[i % len(paths)])
            latencies = []
            for i in range(requests_per_endpoint):
                start = time.perf_counter()
                _get(connection, paths[i % len(paths)])
                latencies.append((time.perf_counter() - start) * 1_000)
            p50, p95, p99 = np.percentile(latencies, [50, 95, 99])
            endpoints.append(
                {
                    "endpoint": template,
                    "p50_ms": round(float(p50), 3),
                    "p95_ms": round(float(p95), 3),
                    "p99_ms": round(float(p99), 3),
                    "mean_ms": round(statistics.fmean(latencies), 3),
                    "target_p95_ms": API_TARGET_P95_MS.get(template, "-"),
                }
            )
            log(f"[api] {template}: p50 {p50:.2f} ms, p95 {p95:.2f} ms")
        connection.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            server.kill()
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "environment": environment(),
        "dataset": dataset_dir.as_posix(),
        "orders": total_orders,
        "startup_seconds": round(startup_s, 2),
        "requests_per_endpoint": requests_per_endpoint,
        "warmup_requests": warmup_requests,
        "client": "single sequential keep-alive HTTP/1.1 client (http.client) on localhost",
        "endpoints": endpoints,
    }


def write_results(results: dict[str, Any], path: Path, section: str) -> dict[str, Any]:
    """Store ``results`` under ``section`` of the JSON file, keeping other sections."""
    existing: dict[str, Any] = {}
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
    existing[section] = results
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8", newline="\n")
    return existing


def _bar(value: float, largest: float, width: int = 40) -> str:
    return "#" * max(1, round(width * value / largest)) if largest > 0 else ""


def render_markdown(results: dict[str, Any]) -> str:
    """Markdown tables for the measured results (numbers come only from ``results``)."""
    lines: list[str] = []
    tca = results.get("tca")
    if tca:
        env, data = tca["environment"], tca["dataset"]
        power = env.get("power") or {}
        power_note = ""
        if "on_battery" in power:
            source = "on battery" if power["on_battery"] else "on AC power"
            scheme = f", power scheme '{power['power_scheme']}'" if "power_scheme" in power else ""
            power_note = f" Machine was {source}{scheme}."
        lines += [
            f"Measured {tca['generated_at']} on {env['cpu']} ({env['logical_cpus']} logical CPUs, "
            f"{env['memory_gb']} GB RAM), {env['os']}, Python {env['python']}, "
            f"Polars {env['polars']} ({env['polars_threads']} threads).{power_note}",
            "",
            f"Dataset: {data['quotes']:,} quotes + {data['trades']:,} trades = "
            f"{data['market_observations']:,} market observations; {data['orders']:,} parent "
            f"orders; {data['fills']:,} fills; {data['symbols']} symbols.",
            "",
            "| Workload | Reference median (s) | Polars median (s) | Speedup | "
            "Runs (ref / Polars) | Peak extra RSS MB (ref / Polars) |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for name, row in tca["workloads"].items():
            ref, opt = row["reference"], row["optimized"]
            lines.append(
                f"| `{name}` | {ref['median_s']:.3f} | {opt['median_s']:.3f} | "
                f"**{row['speedup_median']:.1f}x** | {ref['repetitions']} / "
                f"{opt['repetitions']} | {ref['peak_rss_mb']} / {opt['peak_rss_mb']} |"
            )
        core = tca["workloads"].get("core_in_memory")
        if core:
            ref_s, opt_s = core["reference"]["median_s"], core["optimized"]["median_s"]
            lines += [
                "",
                "```text",
                "Core TCA (in memory), median wall-clock time",
                f"Python reference  {_bar(ref_s, ref_s)} {ref_s:.2f} s",
                f"Polars engine     {_bar(opt_s, ref_s)} {opt_s:.2f} s",
                f"Measured speedup: {core['speedup_median']:.1f}x",
                "```",
            ]
    api = results.get("api")
    if api:
        lines += [
            "",
            f"API latency ({api['requests_per_endpoint']} sequential requests per endpoint "
            f"after {api['warmup_requests']} warm-up requests, {api['orders']:,} orders loaded, "
            f"measured {api['generated_at']}):",
            "",
            "| Endpoint | p50 (ms) | p95 (ms) | p99 (ms) | Target p95 |",
            "|---|---:|---:|---:|---|",
        ]
        for row in api["endpoints"]:
            target = row["target_p95_ms"]
            target_text = f"< {target} ms" if isinstance(target, int | float) else "-"
            lines.append(
                f"| `{row['endpoint']}` | {row['p50_ms']:.2f} | {row['p95_ms']:.2f} | "
                f"{row['p99_ms']:.2f} | {target_text} |"
            )
    return "\n".join(lines)


def update_markdown_block(document: Path, block: str) -> None:
    """Replace the text between the results markers in ``document``."""
    text = document.read_text(encoding="utf-8")
    start, end = text.index(RESULTS_START), text.index(RESULTS_END)
    updated = text[: start + len(RESULTS_START)] + "\n" + block + "\n" + text[end:]
    document.write_text(updated, encoding="utf-8", newline="\n")
