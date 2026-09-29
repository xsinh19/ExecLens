"""ExecLens command line interface."""

from __future__ import annotations

import platform

import typer

from execlens import __version__

app = typer.Typer(
    name="execlens",
    help="Execution analytics and transaction cost analysis on synthetic market data.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """ExecLens: execution analytics and transaction cost analysis (synthetic data only)."""


@app.command()
def version() -> None:
    """Print the ExecLens version and runtime information."""
    typer.echo(f"execlens {__version__} (Python {platform.python_version()})")


if __name__ == "__main__":
    app()
