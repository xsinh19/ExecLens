"""Parquet persistence for ExecLens datasets.

A dataset directory contains one Parquet file per canonical table plus a JSON
provenance record::

    <dir>/quotes.parquet
    <dir>/trades.parquet
    <dir>/orders.parquet
    <dir>/fills.parquet
    <dir>/metadata.json

Parquet keeps column types (including UTC timestamps) intact, compresses
market data well and lets readers load only the columns they need.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import polars as pl

from execlens.data.validation import ExecutionDataset

TABLES: Final = ("quotes", "trades", "orders", "fills")
METADATA_FILE: Final = "metadata.json"


def table_path(directory: Path, table: str) -> Path:
    if table not in TABLES:
        raise ValueError(f"unknown table {table!r}; expected one of {TABLES}")
    return directory / f"{table}.parquet"


def dataset_exists(directory: Path) -> bool:
    return all(table_path(directory, t).is_file() for t in TABLES)


def write_dataset(dataset: ExecutionDataset, directory: Path) -> Path:
    """Write all tables (zstd-compressed Parquet) and metadata to ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    for table in TABLES:
        frame: pl.DataFrame = getattr(dataset, table)
        frame.write_parquet(table_path(directory, table), compression="zstd", statistics=True)
    (directory / METADATA_FILE).write_text(
        json.dumps(dataset.metadata, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return directory


def read_metadata(directory: Path) -> dict[str, object]:
    path = directory / METADATA_FILE
    if not path.is_file():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return dict(loaded) if isinstance(loaded, dict) else {}


def read_dataset(directory: Path, *, validate: bool = True) -> ExecutionDataset:
    """Load a dataset directory; by default every table is re-validated."""
    if not dataset_exists(directory):
        missing = [t for t in TABLES if not table_path(directory, t).is_file()]
        raise FileNotFoundError(f"{directory} is not an ExecLens dataset (missing: {missing})")
    frames = {t: pl.read_parquet(table_path(directory, t)) for t in TABLES}
    metadata = read_metadata(directory)
    if validate:
        return ExecutionDataset.validated(**frames, metadata=metadata)
    return ExecutionDataset(**frames, metadata=metadata)


def scan_table(directory: Path, table: str) -> pl.LazyFrame:
    """Lazily scan one table so queries can push down column/row selection."""
    return pl.scan_parquet(table_path(directory, table))
