"""ExecLens command line interface.

Every command is a thin wrapper over the application services; no financial
formula is implemented here.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import polars as pl
import typer
from rich.console import Console
from rich.table import Table

from execlens import __version__
from execlens.config import DEFAULT_DATA_DIR, DEFAULT_STALE_QUOTE_TOLERANCE_SECONDS
from execlens.data.loader import dataset_exists, write_dataset
from execlens.data.schemas import Side
from execlens.data.synthetic import SCENARIOS
from execlens.data.validation import DataValidationError
from execlens.services.analysis import (
    AnalysisConfig,
    AnalysisResult,
    analyze_directory,
    save_analysis,
)
from execlens.services.datasets import PRESETS, generate_dataset, preset
from execlens.simulation.comparison import ComparisonRequest, compare_strategies
from execlens.tca.aggregation import summarize_by

DEFAULT_BENCHMARK_DATA = Path("data/generated/benchmark")
DEFAULT_DEMO_DATA = Path("data/generated/demo")
BENCHMARK_DOC = Path("docs/BENCHMARKS.md")
DASHBOARD_SCRIPT = Path(__file__).resolve().parent / "dashboard" / "app.py"
DISCLAIMER = "Synthetic data only: not real market data; all fills are simulated."

app = typer.Typer(
    name="execlens",
    help="Execution analytics and transaction cost analysis on synthetic market data.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _cell(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return str(value)


def print_frame(frame: pl.DataFrame, title: str | None = None, max_rows: int = 50) -> None:
    """Render a Polars frame as a Rich table (safe on legacy Windows consoles)."""
    table = Table(title=title)
    for column, dtype in frame.schema.items():
        table.add_column(column, justify="right" if dtype.is_numeric() else "left", overflow="fold")
    for row in frame.head(max_rows).iter_rows():
        table.add_row(*(_cell(v) for v in row))
    console.print(table)
    if frame.height > max_rows:
        console.print(f"[dim]... {frame.height - max_rows} more rows[/dim]")


@app.callback()
def main() -> None:
    """ExecLens: execution analytics and transaction cost analysis (synthetic data only)."""


@app.command()
def version() -> None:
    """Print the ExecLens version and runtime information."""
    typer.echo(f"execlens {__version__} (Python {platform.python_version()})")


@app.command()
def generate(
    preset_name: Annotated[
        str, typer.Option("--preset", help=f"Dataset preset: {', '.join(PRESETS)}.")
    ] = "sample",
    out: Annotated[
        Path | None, typer.Option(help="Output directory [default: data/generated/<preset>].")
    ] = None,
    seed: Annotated[int | None, typer.Option(help="Override the preset's random seed.")] = None,
    sessions: Annotated[
        int | None, typer.Option(min=1, help="Override the number of trading sessions.")
    ] = None,
    orders_per_symbol: Annotated[
        int | None, typer.Option(min=0, help="Override parent orders per symbol per session.")
    ] = None,
) -> None:
    """Generate a deterministic synthetic dataset (market, orders, simulated fills)."""
    try:
        spec = preset(preset_name)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--preset") from exc
    market_updates: dict[str, object] = {}
    if seed is not None:
        market_updates["seed"] = seed
    if sessions is not None:
        market_updates["n_sessions"] = sessions
    spec = spec.model_copy(
        update={
            "market": spec.market.model_validate(spec.market.model_dump() | market_updates),
            "orders": (
                spec.orders.model_copy(update={"orders_per_symbol_per_session": orders_per_symbol})
                if orders_per_symbol is not None
                else spec.orders
            ),
        }
    )
    target = out or Path("data/generated") / preset_name
    with console.status("Generating synthetic market, orders and simulated fills..."):
        dataset = generate_dataset(spec)
        write_dataset(dataset, target)

    table = Table(title=f"Synthetic dataset written to {target}")
    table.add_column("table")
    table.add_column("rows", justify="right")
    for name in ("quotes", "trades", "orders", "fills"):
        table.add_row(name, f"{getattr(dataset, name).height:,}")
    console.print(table)
    console.print(
        f"seed={spec.market.seed} sessions={spec.market.n_sessions} "
        f"symbols={', '.join(i.symbol for i in spec.market.universe)}"
    )
    console.print(f"[dim]{DISCLAIMER}[/dim]")


def _print_analysis(result: AnalysisResult, top: int) -> None:
    s = result.summary
    rows = [
        ("orders analysed", s.analyzed_orders),
        ("fills", s.total_fills),
        ("executed notional", s.executed_notional),
        ("full fill rate", s.full_fill_rate),
        (
            "arrival cost bps: median / mean / p95",
            (s.arrival_cost_bps.median, s.arrival_cost_bps.mean, s.arrival_cost_bps.p95),
        ),
        ("notional-weighted arrival cost bps", s.notional_weighted_arrival_cost_bps),
        ("VWAP cost bps: median", s.vwap_cost_bps.median),
        ("effective spread bps: mean", s.effective_spread_bps.mean),
        ("flagged orders", f"{s.flagged_orders} ({(s.flagged_fraction or 0):.1%})"),
    ]
    table = Table(title="TCA summary (costs: positive = worse)")
    table.add_column("metric")
    table.add_column("value", justify="right")
    for name, value in rows:
        text = " / ".join(_cell(v) for v in value) if isinstance(value, tuple) else _cell(value)
        table.add_row(name, text)
    console.print(table)
    by_strategy = summarize_by(result.orders, "strategy").select(
        "strategy",
        "orders",
        "median_arrival_cost_bps",
        "mean_vwap_cost_bps",
        "mean_effective_spread_bps",
        "mean_completion_ratio",
        "flagged_fraction",
    )
    print_frame(by_strategy, "By strategy")
    flagged = result.orders.filter(pl.col("is_flagged")).sort("n_flags", descending=True)
    if flagged.height and top > 0:
        console.print(f"[bold]Top {min(top, flagged.height)} flagged orders[/bold]")
        for row in flagged.head(top).iter_rows(named=True):
            console.print(f"[cyan]{row['order_id']}[/cyan] ({row['strategy']}): {row['flags']}")
            for reason in row["flag_reasons"] + row["context_notes"]:
                console.print(f"   - {reason}")


@app.command()
def analyze(
    data: Annotated[Path, typer.Option(help="Dataset directory.")] = DEFAULT_DATA_DIR,
    out: Annotated[
        Path | None, typer.Option(help="Derived output directory [default: data/derived/<name>].")
    ] = None,
    stale_tolerance: Annotated[
        float, typer.Option(min=0.001, help="Stale-quote tolerance in seconds.")
    ] = DEFAULT_STALE_QUOTE_TOLERANCE_SECONDS,
    top: Annotated[int, typer.Option(min=0, help="Flagged orders to print.")] = 5,
) -> None:
    """Run TCA and diagnostics on a dataset and write derived Parquet tables."""
    try:
        result = analyze_directory(
            data, AnalysisConfig(stale_quote_tolerance_seconds=stale_tolerance)
        )
    except (FileNotFoundError, DataValidationError) as exc:
        console.print(f"[red]Cannot analyse {data}:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    target = out or Path("data/derived") / data.name
    save_analysis(result, target)
    _print_analysis(result, top)
    console.print(f"Derived tables written to {target}")
    console.print(f"[dim]{DISCLAIMER}[/dim]")


@app.command()
def simulate(
    scenario: Annotated[
        str, typer.Option(help=f"Scenario: {', '.join(SCENARIOS)}, or 'all'.")
    ] = "normal",
    side: Annotated[Side, typer.Option(case_sensitive=False)] = Side.BUY,
    quantity: Annotated[int, typer.Option(min=1)] = 20_000,
    horizon: Annotated[int, typer.Option(min=60, help="Execution horizon (s).")] = 1_800,
    trials: Annotated[int, typer.Option(min=1, max=100)] = 20,
    seed: Annotated[int, typer.Option(min=0)] = 7,
    participation_rate: Annotated[float, typer.Option(help="POV target rate.")] = 0.10,
    slice_seconds: Annotated[float, typer.Option(help="Decision interval (s).")] = 30.0,
    impact_coefficient: Annotated[float, typer.Option(help="Impact coefficient (bps).")] = 25.0,
    oracle: Annotated[
        bool, typer.Option(help="Add the non-implementable ORACLE_VWAP hindsight benchmark.")
    ] = False,
) -> None:
    """Compare IMMEDIATE / TWAP / VWAP / POV on identical synthetic sessions."""
    from pydantic import ValidationError

    from execlens.simulation.impact import ImpactModel
    from execlens.simulation.simulator import SimulationConfig

    scenarios = list(SCENARIOS) if scenario == "all" else [scenario]
    for name in scenarios:
        try:
            request = ComparisonRequest(
                scenario=name,
                side=side,
                quantity=quantity,
                horizon_seconds=horizon,
                trials=trials,
                seed=seed,
                include_oracle=oracle,
                simulation=SimulationConfig(
                    slice_seconds=slice_seconds,
                    pov_participation_rate=participation_rate,
                    impact=ImpactModel(coefficient_bps=impact_coefficient),
                ),
            )
        except ValidationError as exc:
            raise typer.BadParameter(str(exc)) from exc
        with console.status(f"Simulating {name} ({trials} trials)..."):
            result = compare_strategies(request)
        print_frame(
            result.summary.select(
                "strategy",
                "implementable",
                "mean_arrival_cost_bps",
                "arrival_cost_ci95_bps",
                "std_arrival_cost_bps",
                "mean_vwap_cost_bps",
                "mean_effective_spread_bps",
                "mean_markout_60s_bps",
                "mean_completion_ratio",
                "mean_execution_participation_rate",
            ),
            f"{name}: {side.value} {quantity:,} over {horizon} s ({trials} trials; "
            "costs positive = worse, markouts positive = favourable)",
        )
        for note in result.conclusions:
            console.print(f" - {note}")
    console.print(f"[dim]{DISCLAIMER}[/dim]")


@app.command()
def query(
    sql: Annotated[str, typer.Argument(help="SQL over quotes/trades/orders/fills (+ derived).")],
    data: Annotated[Path, typer.Option(help="Dataset directory.")] = DEFAULT_DATA_DIR,
    derived: Annotated[
        Path | None,
        typer.Option(help="Derived tables from `execlens analyze` (orders_tca, fills_tca, ...)."),
    ] = None,
    max_rows: Annotated[int, typer.Option(min=1)] = 50,
) -> None:
    """Run ad-hoc SQL (DuckDB) over the Parquet files of a dataset."""
    import duckdb

    from execlens.data.repository import DuckDBCatalog

    derived = derived or (Path("data/derived") / data.name)
    try:
        with DuckDBCatalog(data, derived if derived.is_dir() else None) as catalog:
            print_frame(catalog.query(sql), max_rows=max_rows)
            console.print(f"[dim]tables: {', '.join(catalog.tables())}[/dim]")
    except (FileNotFoundError, duckdb.Error) as exc:
        console.print(f"[red]Query failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command()
def serve(
    data: Annotated[Path, typer.Option(help="Dataset directory to serve.")] = DEFAULT_DATA_DIR,
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8000,
) -> None:
    """Serve the FastAPI application (OpenAPI docs at /docs)."""
    import uvicorn

    if not dataset_exists(data):
        console.print(f"[red]{data} is not an ExecLens dataset.[/red]")
        raise typer.Exit(code=1)
    os.environ["EXECLENS_DATA_DIR"] = str(data)
    uvicorn.run("execlens.api.app:create_app", factory=True, host=host, port=port)


@app.command()
def dashboard(
    data: Annotated[Path, typer.Option(help="Dataset directory to explore.")] = DEFAULT_DATA_DIR,
    host: Annotated[str, typer.Option()] = "localhost",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8501,
) -> None:
    """Launch the Streamlit dashboard."""
    if not dataset_exists(data):
        console.print(f"[red]{data} is not an ExecLens dataset.[/red]")
        raise typer.Exit(code=1)
    env = os.environ | {"EXECLENS_DATA_DIR": str(data)}
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(DASHBOARD_SCRIPT),
        "--server.address",
        host,
        "--server.port",
        str(port),
        "--browser.gatherUsageStats",
        "false",
    ]
    raise typer.Exit(code=subprocess.call(command, env=env))


@app.command()
def demo(
    out: Annotated[Path, typer.Option(help="Where to write the demo dataset.")] = DEFAULT_DEMO_DATA,
    regenerate: Annotated[bool, typer.Option(help="Regenerate even if it exists.")] = False,
    trials: Annotated[int, typer.Option(min=1, max=100)] = 10,
) -> None:
    """End-to-end demo: generate a synthetic day, analyse it, compare strategies."""
    console.rule("1. Synthetic market, parent orders and simulated fills")
    if regenerate or not dataset_exists(out):
        with console.status("Generating one synthetic trading day for six scenarios..."):
            write_dataset(generate_dataset(preset("demo")), out)
    console.print(f"Dataset: {out}")

    console.rule("2. Transaction cost analysis and diagnostics")
    with console.status("Aligning orders and fills with market data..."):
        result = analyze_directory(out)
        save_analysis(result, Path("data/derived") / out.name)
    _print_analysis(result, top=3)

    console.rule("3. Strategy comparison on identical synthetic sessions")
    for scenario in ("normal", "low_liquidity"):
        quantity = 20_000 if scenario == "normal" else 4_000
        with console.status(f"Simulating {scenario}..."):
            comparison = compare_strategies(
                ComparisonRequest(scenario=scenario, quantity=quantity, trials=trials)
            )
        print_frame(
            comparison.summary.select(
                "strategy",
                "mean_arrival_cost_bps",
                "arrival_cost_ci95_bps",
                "mean_effective_spread_bps",
                "mean_completion_ratio",
            ),
            f"{scenario}: BUY {quantity:,} shares over 30 min ({trials} trials)",
        )
        console.print(f" - {comparison.conclusions[0]}")

    console.rule("Next steps")
    console.print(f"API:        execlens serve --data {out}")
    console.print(f"Dashboard:  execlens dashboard --data {out}")
    console.print(f'SQL:        execlens query --data {out} "SELECT * FROM orders_tca LIMIT 5"')
    console.print(f"[dim]{DISCLAIMER}[/dim]")


@app.command()
def benchmark(
    data: Annotated[
        Path,
        typer.Option(help="Benchmark dataset (generated with the benchmark preset if absent)."),
    ] = DEFAULT_BENCHMARK_DATA,
    reference_repeats: Annotated[int, typer.Option(min=1, help="Timed reference runs.")] = 5,
    optimized_repeats: Annotated[int, typer.Option(min=1, help="Timed Polars runs.")] = 5,
    output: Annotated[Path, typer.Option(help="Machine-readable results file.")] = Path(
        "benchmarks/results.json"
    ),
    api: Annotated[bool, typer.Option(help="Also measure local API latency.")] = True,
    update_docs: Annotated[
        bool, typer.Option(help=f"Rewrite the results block in {BENCHMARK_DOC}.")
    ] = True,
    smoke: Annotated[
        bool,
        typer.Option(help="Quick run on the small sample dataset; prints results, writes nothing."),
    ] = False,
) -> None:
    """Benchmark the loop-based reference TCA against the vectorised Polars engine."""
    from execlens.benchmarking import (
        BenchmarkError,
        render_markdown,
        run_api_benchmark,
        run_tca_benchmark,
        update_markdown_block,
        write_results,
    )

    if smoke:
        data, reference_repeats, optimized_repeats = DEFAULT_DATA_DIR, 1, 1
    elif not dataset_exists(data):
        console.print(f"Generating the benchmark dataset in {data} (one-off)...")
        write_dataset(generate_dataset(preset("benchmark")), data)
    try:
        results = {
            "tca": run_tca_benchmark(
                data,
                reference_repeats=reference_repeats,
                optimized_repeats=optimized_repeats,
                enforce_scale=not smoke,
                log=console.print,
            )
        }
        if api:
            results["api"] = run_api_benchmark(data, log=console.print)
    except BenchmarkError as exc:
        console.print(f"[red]Benchmark aborted:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if smoke:
        console.print(render_markdown(results))
        return
    for section, payload in results.items():
        write_results(payload, output, section)
    console.print(f"Results written to {output}")
    if update_docs and BENCHMARK_DOC.is_file():
        stored = json.loads(output.read_text(encoding="utf-8"))
        update_markdown_block(BENCHMARK_DOC, render_markdown(stored))
        console.print(f"Updated the results section of {BENCHMARK_DOC}")


if __name__ == "__main__":
    app()
