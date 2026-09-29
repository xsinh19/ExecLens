"""DuckDB catalog over ExecLens Parquet files, for ad-hoc analytical SQL.

The canonical tables of a dataset directory (``quotes``, ``trades``,
``orders``, ``fills``) and, when present, the derived analysis tables written
by ``execlens analyze`` (``orders_tca``, ``fills_tca``, ``flag_counts``) are
registered as DuckDB views over the Parquet files. Queries therefore read
only the columns and row groups they need, without loading tables into
Python first::

    execlens query "SELECT symbol, median(arrival_cost_bps) FROM orders_tca GROUP BY 1"
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Final, Self

import duckdb
import polars as pl

from execlens.data.loader import TABLES, table_path

DERIVED_TABLES: Final = ("orders_tca", "fills_tca", "flag_counts")


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class DuckDBCatalog:
    """An in-memory DuckDB connection with one view per available Parquet table."""

    def __init__(self, dataset_dir: Path, derived_dir: Path | None = None) -> None:
        self._connection = duckdb.connect(database=":memory:")
        self._tables: list[str] = []
        sources = {t: table_path(dataset_dir, t) for t in TABLES}
        if derived_dir is not None:
            sources |= {t: derived_dir / f"{t}.parquet" for t in DERIVED_TABLES}
        for name, path in sources.items():
            if path.is_file():
                self._connection.execute(
                    f"CREATE VIEW {name} AS SELECT * FROM read_parquet("
                    f"{_sql_string(path.resolve().as_posix())})"
                )
                self._tables.append(name)
        if not self._tables:
            raise FileNotFoundError(f"no ExecLens Parquet tables found in {dataset_dir}")

    def tables(self) -> list[str]:
        return list(self._tables)

    def query(self, sql: str) -> pl.DataFrame:
        """Run a SQL statement and return the result as a Polars DataFrame."""
        return self._connection.sql(sql).pl()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
