from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from rich.console import Console
from typer.testing import CliRunner

from execlens.cli import app

SAMPLE_DIR = Path(__file__).resolve().parents[2] / "data" / "sample"
runner = CliRunner()


@pytest.fixture(autouse=True)
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rich fixes the width when the console is created (from COLUMNS, if set).

    Use a wide console so tables and long paths are not wrapped, whatever the
    test environment's terminal width.
    """
    monkeypatch.setattr("execlens.cli.console", Console(width=250))


def test_analyze_prints_summary_and_writes_derived_tables(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["analyze", "--data", str(SAMPLE_DIR), "--out", str(tmp_path), "--top", "2"]
    )
    assert result.exit_code == 0, result.output
    assert "TCA summary" in result.output
    assert "Top 2 flagged orders" in result.output
    assert pl.read_parquet(tmp_path / "orders_tca.parquet").height == 240


def test_analyze_accepts_a_stale_tolerance(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["analyze", "--data", str(SAMPLE_DIR), "--out", str(tmp_path), "--stale-tolerance", "0.5"],
    )
    assert result.exit_code == 0, result.output
    orders = pl.read_parquet(tmp_path / "orders_tca.parquet")
    assert (orders["arrival_quote_status"] == "STALE").any()


def test_analyze_reports_missing_datasets(tmp_path: Path) -> None:
    result = runner.invoke(app, ["analyze", "--data", str(tmp_path / "nowhere")])
    assert result.exit_code == 1
    assert "Cannot analyse" in result.output


def test_query_runs_sql_over_parquet(tmp_path: Path) -> None:
    runner.invoke(app, ["analyze", "--data", str(SAMPLE_DIR), "--out", str(tmp_path)])
    result = runner.invoke(
        app,
        [
            "query",
            "SELECT count(*) AS n FROM orders_tca WHERE is_flagged",
            "--data",
            str(SAMPLE_DIR),
            "--derived",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "orders_tca" in result.output
    bad = runner.invoke(app, ["query", "SELECT * FROM nope", "--data", str(SAMPLE_DIR)])
    assert bad.exit_code == 1
    assert "Query failed" in bad.output


def test_simulate_compares_strategies() -> None:
    result = runner.invoke(
        app,
        [
            "simulate",
            "--scenario",
            "low_liquidity",
            "--quantity",
            "1000",
            "--horizon",
            "600",
            "--trials",
            "2",
            "--oracle",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Lowest mean arrival cost" in result.output
    assert "ORACLE_VWAP" in result.output


def test_simulate_rejects_unknown_scenarios() -> None:
    result = runner.invoke(app, ["simulate", "--scenario", "moon", "--trials", "1"])
    assert result.exit_code != 0


def test_demo_runs_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["demo", "--trials", "1"])
    assert result.exit_code == 0, result.output
    assert "Strategy comparison" in result.output
    assert (tmp_path / "data" / "generated" / "demo" / "fills.parquet").is_file()
    assert (tmp_path / "data" / "derived" / "demo" / "orders_tca.parquet").is_file()


@pytest.mark.parametrize("command", ["serve", "dashboard"])
def test_servers_refuse_missing_datasets(command: str, tmp_path: Path) -> None:
    result = runner.invoke(app, [command, "--data", str(tmp_path)])
    assert result.exit_code == 1
    assert "not an ExecLens dataset" in result.output
