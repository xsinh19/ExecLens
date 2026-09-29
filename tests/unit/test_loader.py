from pathlib import Path

import polars as pl
import pytest

from execlens.data.loader import (
    dataset_exists,
    read_dataset,
    read_metadata,
    scan_table,
    table_path,
    write_dataset,
)
from execlens.data.schemas import FILL_SCHEMA
from execlens.data.validation import DataValidationError, ExecutionDataset
from factories import empty, orders_frame, quotes_frame, trades_frame


def small_dataset() -> ExecutionDataset:
    return ExecutionDataset.validated(
        quotes=quotes_frame([(0, "SYN", 9.99, 10.01, 100, 100), (1, "SYN", 10.0, 10.02, 50, 70)]),
        trades=trades_frame([(0.5, "SYN", 10.01, 30)]),
        orders=orders_frame([("O1", "SYN", "BUY", 10, 0.2, 30.0, "TWAP")]),
        fills=empty(FILL_SCHEMA),
        metadata={"seed": 7, "synthetic": True},
    )


def test_round_trip_preserves_tables_and_metadata(tmp_path: Path) -> None:
    ds = small_dataset()
    write_dataset(ds, tmp_path / "ds")
    assert dataset_exists(tmp_path / "ds")
    back = read_dataset(tmp_path / "ds")
    for table in ("quotes", "trades", "orders", "fills"):
        assert getattr(back, table).equals(getattr(ds, table)), table
    assert back.metadata == {"seed": 7, "synthetic": True}


def test_reading_revalidates_by_default(tmp_path: Path) -> None:
    ds = small_dataset()
    write_dataset(ds, tmp_path)
    crossed = ds.quotes.with_columns(pl.lit(20.0).alias("bid_price"))
    crossed.write_parquet(table_path(tmp_path, "quotes"))
    with pytest.raises(DataValidationError, match="crossed market"):
        read_dataset(tmp_path)
    assert read_dataset(tmp_path, validate=False).quotes["bid_price"][0] == 20.0


def test_missing_dataset_is_reported(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="not an ExecLens dataset"):
        read_dataset(tmp_path)
    assert read_metadata(tmp_path) == {}


def test_unknown_table_name_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown table"):
        table_path(tmp_path, "positions")


def test_scan_table_supports_projection(tmp_path: Path) -> None:
    write_dataset(small_dataset(), tmp_path)
    projected = scan_table(tmp_path, "quotes").select("symbol", "bid_price").collect()
    assert projected.columns == ["symbol", "bid_price"]
    assert projected.height == 2
