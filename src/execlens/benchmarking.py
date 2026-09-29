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
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
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


class _SingleThreadWorker:
    """A child process running the engine with ``POLARS_MAX_THREADS=1``.

    Polars fixes its thread pool at import, so single-threaded timings need a
    separate process. The worker loads the dataset once, runs one warm-up
    (whose output the parent checks against the reference), then times one
    engine run per ``run`` request so its runs can be interleaved with the
    parent's.
    """

    def __init__(self, dataset_dir: Path, config: TcaConfig, output: Path) -> None:
        config_json = json.dumps(asdict(config))
        self._process: subprocess.Popen[str] = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "execlens.benchmarking",
                str(dataset_dir),
                config_json,
                str(output),
            ],
            env=os.environ | {"POLARS_MAX_THREADS": "1"},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        if self._process.stdin is None or self._process.stdout is None:  # pragma: no cover
            raise BenchmarkError("could not open pipes to the single-thread worker")
        self._stdin, self._stdout = self._process.stdin, self._process.stdout
        ready = self._stdout.readline().strip()
        if not ready.startswith("ready"):
            self._process.kill()
            raise BenchmarkError(f"single-thread worker failed to start: {ready!r}")
        self.threads = int(ready.split()[1])

    def run(self) -> float:
        self._stdin.write("run\n")
        self._stdin.flush()
        return float(self._stdout.readline())

    def close(self) -> None:
        self._stdin.close()
        try:
            self._process.wait(timeout=60)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            self._process.kill()
            self._process.wait()
        self._stdout.close()

    def __enter__(self) -> _SingleThreadWorker:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _single_thread_worker_main(dataset_dir: str, config_json: str, output: str) -> None:
    """Entry point of :class:`_SingleThreadWorker` (``python -m execlens.benchmarking``)."""
    raw = json.loads(config_json)
    config = TcaConfig(
        stale_quote_tolerance_seconds=raw["stale_quote_tolerance_seconds"],
        markout_horizons_seconds=tuple(raw["markout_horizons_seconds"]),
    )
    dataset = read_dataset(Path(dataset_dir), validate=False)  # validated by the parent

    def once() -> pl.DataFrame:
        return run_tca(
            dataset.quotes, dataset.trades, dataset.orders, dataset.fills, config, validate=False
        ).orders

    once().write_parquet(output)  # warm-up; the parent checks it against the reference
    print("ready", pl.thread_pool_size(), flush=True)
    for line in sys.stdin:
        if line.strip() != "run":
            break
        print(_elapsed(once), flush=True)


def run_tca_benchmark(
    dataset_dir: Path,
    *,
    reference_repeats: int = 3,
    optimized_repeats: int = 7,
    config: TcaConfig | None = None,
    enforce_scale: bool = True,
    single_thread: bool = True,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Benchmark the loop reference against the vectorised engine.

    Two workloads are measured:

    * ``core_in_memory`` - both start from the same in-memory Polars frames;
    * ``end_to_end_parquet`` - both start from the Parquet files on disk: the
      reference reads every file eagerly, the engine scans lazily so Polars
      can skip unused columns.

    Timed runs are interleaved across implementations *and* workloads so
    changes in machine state affect everything alike. For ``core_in_memory``
    the engine is also timed single-threaded (``POLARS_MAX_THREADS=1``) to
    separate the gain from vectorisation from the gain from parallelism.
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

    if reference_repeats < 1 or optimized_repeats < 1:
        raise ValueError("repeats must be >= 1")
    reference_outputs: dict[str, pl.DataFrame] = {}
    for name, (_, reference, optimized) in workloads.items():
        log(f"[{name}] warm-up + equivalence check")
        reference_outputs[name] = reference()
        problems = compare_to_reference(optimized(), reference_outputs[name])
        if problems:
            raise BenchmarkError(f"{name}: implementations disagree: {problems}")

    worker: _SingleThreadWorker | None = None
    single_thread_runs: list[float] = []
    reference_output = reference_outputs["core_in_memory"]
    with tempfile.TemporaryDirectory() as scratch:
        if single_thread:
            log("[core_in_memory] starting single-thread (POLARS_MAX_THREADS=1) worker")
            worker = _SingleThreadWorker(dataset_dir, config, Path(scratch) / "st.parquet")
            problems = compare_to_reference(
                pl.read_parquet(Path(scratch) / "st.parquet"), reference_output
            )
            if problems:
                worker.close()
                raise BenchmarkError(f"single-thread engine disagrees: {problems}")
        try:
            log(
                f"timing (interleaved across workloads): reference x{reference_repeats}, "
                f"optimized x{optimized_repeats}"
            )
            runs: dict[str, tuple[list[float], list[float]]] = {n: ([], []) for n in workloads}
            for i in range(max(reference_repeats, optimized_repeats)):
                for name, (_, reference, optimized) in workloads.items():
                    if i < reference_repeats:
                        runs[name][0].append(_elapsed(reference))
                    if i < optimized_repeats:
                        runs[name][1].append(_elapsed(optimized))
                        if name == "core_in_memory" and worker is not None:
                            single_thread_runs.append(worker.run())
        finally:
            if worker is not None:
                worker.close()

    results: dict[str, Any] = {}
    for name, (description, reference, optimized) in workloads.items():
        ref = Measurement(runs[name][0], peak_memory_mb(reference))
        opt = Measurement(runs[name][1], peak_memory_mb(optimized))
        results[name] = {
            "description": description,
            "reference": ref.as_dict(),
            "optimized": opt.as_dict(),
            "speedup_median": round(ref.median_s / opt.median_s, 3),
        }
        if name == "core_in_memory" and single_thread_runs:
            st = Measurement(single_thread_runs)
            results[name]["optimized_single_thread"] = st.as_dict() | {"threads": 1}
            results[name]["speedup_single_thread_median"] = round(ref.median_s / st.median_s, 3)
        log(
            f"[{name}] reference {ref.median_s:.3f}s | optimized {opt.median_s:.3f}s | "
            f"speedup {ref.median_s / opt.median_s:.1f}x"
        )

    notes = []
    core, e2e = results["core_in_memory"], results["end_to_end_parquet"]
    for side in ("reference", "optimized"):
        if e2e[side]["median_s"] < 0.9 * core[side]["median_s"]:
            notes.append(
                f"{side}: end-to-end (which includes reading Parquet) was faster than in-memory; "
                "the machine's speed changed during the run, so compare ratios, not absolutes."
            )
    for note in notes:
        log(f"[warning] {note}")

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
        "notes": notes,
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
            bars = [
                f"Python reference      {_bar(ref_s, ref_s)} {ref_s:.2f} s",
                f"Polars, all threads   {_bar(opt_s, ref_s)} {opt_s:.2f} s",
            ]
            single = core.get("optimized_single_thread")
            if single:
                st_s = single["median_s"]
                bars.append(f"Polars, 1 thread      {_bar(st_s, ref_s)} {st_s:.2f} s")
            lines += [
                "",
                "```text",
                "Core TCA (in memory), median wall-clock time",
                *bars,
                f"Measured speedup: {core['speedup_median']:.1f}x with "
                f"{env['polars_threads']} threads"
                + (
                    f", {core['speedup_single_thread_median']:.1f}x single-threaded"
                    if single
                    else ""
                ),
                "```",
            ]
        for note in tca.get("notes", []):
            lines += ["", f"> Note: {note}"]
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


def render_readme_summary(results: dict[str, Any]) -> str:
    """The compact benchmark visual for README.md (numbers only from ``results``)."""
    tca = results.get("tca")
    if not tca or "core_in_memory" not in tca["workloads"]:
        return "_Run `execlens benchmark` to measure._"
    core, data = tca["workloads"]["core_in_memory"], tca["dataset"]
    ref_s, opt_s = core["reference"]["median_s"], core["optimized"]["median_s"]
    single = core.get("optimized_single_thread")
    threads = tca["environment"]["polars_threads"]
    lines = [
        "```text",
        "TCA processing benchmark (median of interleaved runs, outputs verified equal)",
        "",
        f"Python baseline    {_bar(ref_s, ref_s)} {ref_s:.2f} s",
        f"Polars             {_bar(opt_s, ref_s)} {opt_s:.2f} s",
    ]
    if single:
        lines.append(
            f"Polars, 1 thread   {_bar(single['median_s'], ref_s)} {single['median_s']:.2f} s"
        )
    lines += [
        "",
        f"Measured speedup: {core['speedup_median']:.1f}x ({threads} threads)"
        + (f", {core['speedup_single_thread_median']:.1f}x single-threaded" if single else ""),
        f"Dataset: {data['fills']:,} fills / {data['market_observations']:,} market rows "
        f"({data['quotes']:,} quotes + {data['trades']:,} trades)",
        "```",
    ]
    api = results.get("api")
    if api:
        by_endpoint = {row["endpoint"]: row for row in api["endpoints"]}
        health = by_endpoint.get("/health")
        tca_row = by_endpoint.get("/api/v1/orders/{order_id}/tca")
        if health and tca_row:
            lines += [
                "",
                f"Local API p95 latency with {api['orders']:,} orders loaded: "
                f"`/health` {health['p95_ms']:.1f} ms, "
                f"`/orders/{{id}}/tca` {tca_row['p95_ms']:.1f} ms.",
            ]
    return "\n".join(lines)


def update_markdown_block(document: Path, block: str) -> None:
    """Replace the text between the results markers in ``document``."""
    text = document.read_text(encoding="utf-8")
    start, end = text.index(RESULTS_START), text.index(RESULTS_END)
    updated = text[: start + len(RESULTS_START)] + "\n" + block + "\n" + text[end:]
    document.write_text(updated, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    _single_thread_worker_main(*sys.argv[1:4])
