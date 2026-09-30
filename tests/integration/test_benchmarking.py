"""The benchmark harness itself (run on the small sample so tests stay fast)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from typer.testing import CliRunner

import execlens.benchmarking as bench
from execlens.cli import app

SAMPLE_DIR = Path(__file__).resolve().parents[2] / "data" / "sample"


@pytest.fixture(scope="module")
def results() -> dict[str, Any]:
    return bench.run_tca_benchmark(
        SAMPLE_DIR,
        reference_repeats=2,
        optimized_repeats=2,
        enforce_scale=False,
        log=lambda _: None,
    )


def test_results_record_environment_scale_and_both_workloads(results: dict[str, Any]) -> None:
    assert results["dataset"]["fills"] > 1_000
    assert results["validation"]["equivalent"] is True
    assert set(results["workloads"]) == {"core_in_memory", "end_to_end_parquet"}
    for workload in results["workloads"].values():
        for side in ("reference", "optimized"):
            assert workload[side]["repetitions"] == 2
            assert workload[side]["median_s"] > 0
        assert workload["speedup_median"] > 0
    env = results["environment"]
    assert env["python"].startswith("3.12")
    assert env["logical_cpus"] >= 1


def test_full_benchmark_refuses_undersized_datasets() -> None:
    with pytest.raises(bench.BenchmarkError, match="dataset too small"):
        bench.run_tca_benchmark(SAMPLE_DIR, reference_repeats=1, optimized_repeats=1)


def test_benchmark_aborts_when_implementations_disagree(monkeypatch: pytest.MonkeyPatch) -> None:
    real = bench.reference_tca

    def tampered(*args: Any, **kwargs: Any) -> pl.DataFrame:
        return real(*args, **kwargs).with_columns(pl.col("arrival_cost_bps") + 1.0)

    monkeypatch.setattr(bench, "reference_tca", tampered)
    with pytest.raises(bench.BenchmarkError, match="implementations disagree"):
        bench.run_tca_benchmark(SAMPLE_DIR, enforce_scale=False, log=lambda _: None)


def test_single_thread_engine_is_measured(results: dict[str, Any]) -> None:
    core = results["workloads"]["core_in_memory"]
    single = core["optimized_single_thread"]
    assert single["threads"] == 1
    assert single["repetitions"] == 2
    assert core["speedup_single_thread_median"] > 0
    assert "optimized_single_thread" not in results["workloads"]["end_to_end_parquet"]


def test_changes_in_machine_speed_are_noted(monkeypatch: pytest.MonkeyPatch) -> None:
    # Per repetition the order is: core ref, core opt, e2e ref, e2e opt.
    # Make end-to-end (which does strictly more work) look faster than core.
    times = iter([1.0, 0.1, 0.5, 0.05] * 2)
    monkeypatch.setattr(bench, "_elapsed", lambda fn: next(times))
    results = bench.run_tca_benchmark(
        SAMPLE_DIR,
        reference_repeats=2,
        optimized_repeats=2,
        enforce_scale=False,
        single_thread=False,
        log=lambda _: None,
    )
    assert len(results["notes"]) == 2
    assert "compare ratios, not absolutes" in results["notes"][0]
    assert "> Note:" in bench.render_markdown({"tca": results})


def test_rendering_points_out_when_threads_did_not_help(results: dict[str, Any]) -> None:
    core = results["workloads"]["core_in_memory"]

    def rendered(single_s: float) -> str:
        single = core["optimized_single_thread"] | {"median_s": single_s}
        workloads = results["workloads"] | {
            "core_in_memory": core | {"optimized_single_thread": single}
        }
        return bench.render_markdown({"tca": results | {"workloads": workloads}})

    multi_s = core["optimized"]["median_s"]
    assert "extra threads did not pay off" in rendered(multi_s * 0.9)
    assert "extra threads did not pay off" not in rendered(multi_s * 3)


def test_repeats_must_be_positive() -> None:
    with pytest.raises(ValueError, match="repeats"):
        bench.run_tca_benchmark(SAMPLE_DIR, reference_repeats=0, enforce_scale=False)


def test_results_file_keeps_other_sections(tmp_path: Path) -> None:
    path = tmp_path / "results.json"
    bench.write_results({"x": 1}, path, "api")
    bench.write_results({"y": 2}, path, "tca")
    assert json.loads(path.read_text(encoding="utf-8")) == {"api": {"x": 1}, "tca": {"y": 2}}


def test_markdown_is_rendered_only_from_results(results: dict[str, Any], tmp_path: Path) -> None:
    api = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "orders": 10,
        "requests_per_endpoint": 5,
        "warmup_requests": 1,
        "endpoints": [
            {
                "endpoint": "/health",
                "p50_ms": 1.0,
                "p95_ms": 2.0,
                "p99_ms": 3.0,
                "target_p95_ms": 50,
            }
        ],
    }
    text = bench.render_markdown({"tca": results, "api": api})
    core = results["workloads"]["core_in_memory"]
    assert f"**{core['speedup_median']:.1f}x**" in text
    assert f"{results['dataset']['fills']:,} fills" in text
    assert "| `/health` | 1.00 | 2.00 | 3.00 | < 50 ms |" in text

    doc = tmp_path / "BENCHMARKS.md"
    doc.write_text(
        f"intro\n{bench.RESULTS_START}\nold\n{bench.RESULTS_END}\noutro\n", encoding="utf-8"
    )
    bench.update_markdown_block(doc, "new block")
    assert doc.read_text(encoding="utf-8") == (
        f"intro\n{bench.RESULTS_START}\nnew block\n{bench.RESULTS_END}\noutro\n"
    )


def test_cli_smoke_benchmark_prints_without_writing(tmp_path: Path) -> None:
    output = tmp_path / "results.json"
    result = CliRunner().invoke(app, ["benchmark", "--smoke", "--no-api", "--output", str(output)])
    assert result.exit_code == 0, result.output
    assert "Core TCA (in memory)" in result.output
    assert not output.exists()
