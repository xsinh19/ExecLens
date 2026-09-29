"""ExecLens command line interface.

Every command is a thin wrapper over the application services; no financial
formula is implemented here.
"""

from __future__ import annotations

import json
import platform
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from execlens import __version__
from execlens.data.loader import dataset_exists, write_dataset
from execlens.services.datasets import PRESETS, generate_dataset, preset

DEFAULT_BENCHMARK_DATA = Path("data/generated/benchmark")
BENCHMARK_DOC = Path("docs/BENCHMARKS.md")

app = typer.Typer(
    name="execlens",
    help="Execution analytics and transaction cost analysis on synthetic market data.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


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
    console.print("[dim]Synthetic data only: not real market data; fills are simulated.[/dim]")


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
        run_tca_benchmark,
        update_markdown_block,
        write_results,
    )

    if smoke:
        data, reference_repeats, optimized_repeats = Path("data/sample"), 1, 1
    elif not dataset_exists(data):
        console.print(f"Generating the benchmark dataset in {data} (one-off)...")
        write_dataset(generate_dataset(preset("benchmark")), data)
    try:
        tca_results = run_tca_benchmark(
            data,
            reference_repeats=reference_repeats,
            optimized_repeats=optimized_repeats,
            enforce_scale=not smoke,
            log=console.print,
        )
        results = {"tca": tca_results}
        if api:
            from execlens.benchmarking import run_api_benchmark

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
