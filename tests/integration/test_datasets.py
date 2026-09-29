from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from polars.testing import assert_frame_equal
from typer.testing import CliRunner

from execlens.cli import app
from execlens.data.loader import dataset_exists, read_dataset
from execlens.services.datasets import DISCLAIMER, generate_dataset, preset

SAMPLE_DIR = Path(__file__).resolve().parents[2] / "data" / "sample"
TABLES = ("quotes", "trades", "orders", "fills")


@pytest.fixture(scope="module")
def sample() -> object:
    return generate_dataset(preset("sample"))


def test_dataset_generation_is_deterministic(sample) -> None:  # type: ignore[no-untyped-def]
    again = generate_dataset(preset("sample"))
    for table in TABLES:
        assert getattr(again, table).equals(getattr(sample, table)), table


def test_every_order_is_simulated_with_its_labelled_strategy(sample) -> None:  # type: ignore[no-untyped-def]
    filled = sample.fills.group_by("order_id").agg(pl.col("quantity").sum().alias("filled"))
    joined = sample.orders.join(filled, on="order_id", how="left").with_columns(
        pl.col("filled").fill_null(0)
    )
    assert (joined["filled"] <= joined["quantity"]).all()
    assert set(sample.orders["strategy"]) == {"IMMEDIATE", "TWAP", "VWAP", "POV"}
    immediate = joined.filter(pl.col("strategy") == "IMMEDIATE")
    assert (immediate["filled"] == immediate["quantity"]).all()


def test_metadata_records_provenance_and_disclaimer(sample) -> None:  # type: ignore[no-untyped-def]
    assert sample.metadata["synthetic"] is True
    assert sample.metadata["disclaimer"] == DISCLAIMER
    assert sample.metadata["spec"]["market"]["seed"] == 42


def test_committed_sample_matches_a_fresh_generation(sample) -> None:  # type: ignore[no-untyped-def]
    """The committed demo dataset is reproducible from its preset and seed.

    Integer, string and timestamp columns must match exactly; floats are
    compared with a tiny relative tolerance to allow for last-bit differences
    in transcendental functions across platforms.
    """
    assert dataset_exists(SAMPLE_DIR), "data/sample is missing; run execlens generate"
    committed = read_dataset(SAMPLE_DIR)
    for table in TABLES:
        assert_frame_equal(
            getattr(committed, table), getattr(sample, table), check_exact=False, rel_tol=1e-12
        )


def test_unknown_preset_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown preset"):
        preset("enormous")


def test_cli_generate_writes_a_dataset(tmp_path: Path) -> None:
    out = tmp_path / "ds"
    result = CliRunner().invoke(
        app, ["generate", "--preset", "sample", "--out", str(out), "--orders-per-symbol", "3"]
    )
    assert result.exit_code == 0, result.output
    assert dataset_exists(out)
    assert read_dataset(out).orders.height == 3 * 6


def test_cli_generate_rejects_unknown_presets(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["generate", "--preset", "nope", "--out", str(tmp_path)])
    assert result.exit_code != 0
