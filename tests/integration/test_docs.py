"""Numbers quoted in the documentation must match what the code produces."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import typer

from execlens.benchmarking import (
    RESULTS_END,
    RESULTS_START,
    render_markdown,
    render_readme_summary,
)
from execlens.cli import app
from execlens.services.analysis import analyze_directory
from execlens.simulation.comparison import ComparisonRequest, best_strategy, compare_strategies

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"


def generated_block(document: Path) -> str:
    text = document.read_text(encoding="utf-8")
    return text[text.index(RESULTS_START) + len(RESULTS_START) : text.index(RESULTS_END)].strip()


def test_benchmark_sections_are_generated_from_results_json() -> None:
    results = json.loads((ROOT / "benchmarks" / "results.json").read_text(encoding="utf-8"))
    assert generated_block(ROOT / "docs" / "BENCHMARKS.md") == render_markdown(results).strip()
    assert generated_block(README) == render_readme_summary(results).strip()


def test_readme_analysis_excerpt_matches_the_sample_dataset() -> None:
    summary = analyze_directory(ROOT / "data" / "sample").summary
    cost = summary.arrival_cost_bps
    readme = README.read_text(encoding="utf-8")
    assert f"{cost.median:.2f} / {cost.mean:.2f} / {cost.p95:.2f}" in readme
    assert f"{summary.flagged_orders} ({summary.flagged_fraction:.1%})" in readme


@pytest.mark.slow
def test_readme_strategy_table_matches_a_fresh_run() -> None:
    """The table was produced by scripts/strategy_matrix.py with these parameters."""
    readme = README.read_text(encoding="utf-8")
    rows = {
        m.group(1): m.group(2)
        for m in re.finditer(r"^\| `(\w+)` \| (.+) \|$", readme, flags=re.MULTILINE)
    }
    assert set(rows) == {
        "normal",
        "high_volatility",
        "low_liquidity",
        "wide_spread",
        "trend_up",
        "trend_down",
    }
    for scenario, cells in rows.items():
        result = compare_strategies(
            ComparisonRequest(
                scenario=scenario, quantity=20_000, horizon_seconds=1_800, trials=30, seed=7
            )
        )
        by_strategy = {r["strategy"]: r for r in result.summary.iter_rows(named=True)}
        expected = []
        for strategy in ("IMMEDIATE", "TWAP", "VWAP", "POV"):
            row = by_strategy[strategy]
            cell = f"{row['mean_arrival_cost_bps']:.1f} (±{row['arrival_cost_ci95_bps']:.1f})"
            if row["mean_completion_ratio"] < 0.999:
                cell += f", {row['mean_completion_ratio']:.0%} filled"
            # The README typesets minus signs as U+2212 for readability.
            expected.append(cell.replace("-", "\N{MINUS SIGN}"))
        expected.append(str(best_strategy(result.summary)))
        assert cells == " | ".join(expected), scenario


def test_cli_commands_mentioned_in_the_readme_exist() -> None:
    commands = {command.name for command in typer.main.get_command(app).commands.values()}
    readme = README.read_text(encoding="utf-8")
    mentioned = set(re.findall(r"execlens (\w+)", readme))
    assert mentioned <= commands, mentioned - commands
