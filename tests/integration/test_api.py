from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import get_args

import polars as pl
import pytest
from fastapi.testclient import TestClient

from execlens.api.app import create_app
from execlens.api.schemas import ScenarioName, SimulationResponse
from execlens.api.state import SimulationStore
from execlens.data.synthetic import SCENARIOS
from execlens.services.analysis import AnalysisResult, analyze_directory

SAMPLE_DIR = Path(__file__).resolve().parents[2] / "data" / "sample"


@pytest.fixture(scope="module")
def analysis() -> AnalysisResult:
    return analyze_directory(SAMPLE_DIR)


@pytest.fixture(scope="module")
def client(analysis: AnalysisResult) -> Iterator[TestClient]:
    with TestClient(create_app(analysis=analysis)) as test_client:
        yield test_client


def test_health_reports_the_loaded_dataset(client: TestClient, analysis: AnalysisResult) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["dataset"]["synthetic"] is True
    assert body["dataset"]["orders"] == analysis.dataset.orders.height
    assert body["dataset"]["fills"] == analysis.dataset.fills.height
    assert "SYN_NORMAL" in body["dataset"]["symbols"]


def test_order_listing_paginates(client: TestClient, analysis: AnalysisResult) -> None:
    first = client.get("/api/v1/orders", params={"limit": 5}).json()
    second = client.get("/api/v1/orders", params={"limit": 5, "offset": 5}).json()
    assert first["total"] == analysis.orders.height
    assert len(first["items"]) == 5
    assert {i["order_id"] for i in first["items"]}.isdisjoint(
        i["order_id"] for i in second["items"]
    )
    assert [i["order_id"] for i in first["items"]] == analysis.orders["order_id"].head(5).to_list()


@pytest.mark.parametrize(
    ("params", "column", "value"),
    [
        ({"symbol": "SYN_WIDE"}, "symbol", "SYN_WIDE"),
        ({"side": "SELL"}, "side", "SELL"),
        ({"strategy": "POV"}, "strategy", "POV"),
        ({"flagged": "true"}, "is_flagged", True),
        ({"flagged": "false"}, "is_flagged", False),
    ],
)
def test_order_listing_filters(
    client: TestClient,
    analysis: AnalysisResult,
    params: dict[str, str],
    column: str,
    value: object,
) -> None:
    body = client.get("/api/v1/orders", params=params | {"limit": 1000}).json()
    expected = analysis.orders.filter(pl.col(column) == value).height
    assert body["total"] == expected
    assert len(body["items"]) == expected
    assert all(item[column] == value for item in body["items"])


def test_order_detail_includes_fills(client: TestClient, analysis: AnalysisResult) -> None:
    order_id = analysis.orders.filter(pl.col("n_fills") > 3)["order_id"][0]
    body = client.get(f"/api/v1/orders/{order_id}").json()
    assert body["order_id"] == order_id
    assert len(body["fills"]) == analysis.fills_for(order_id).height
    assert sum(f["quantity"] for f in body["fills"]) == body["executed_quantity"]


def test_order_tca_values_come_from_the_analysis(
    client: TestClient, analysis: AnalysisResult
) -> None:
    order_id = analysis.orders.filter(pl.col("arrival_cost_bps").is_not_null())["order_id"][3]
    row = analysis.order(order_id)
    assert row is not None
    body = client.get(f"/api/v1/orders/{order_id}/tca").json()
    assert body["costs"]["arrival_cost_bps"] == pytest.approx(row["arrival_cost_bps"])
    assert body["costs"]["vwap_cost_bps"] == pytest.approx(row["vwap_cost_bps"])
    assert body["arrival"]["mid"] == pytest.approx(row["arrival_mid"])
    assert body["benchmark"]["market_vwap"] == pytest.approx(row["market_vwap"])
    assert body["execution"]["avg_fill_price"] == pytest.approx(row["avg_fill_price"])
    assert [m["horizon_seconds"] for m in body["markouts"]] == [10, 30, 60, 300]
    assert body["markouts"][2]["markout_bps"] == pytest.approx(row["markout_60s_bps"])
    assert body["diagnostics"]["flags"] == row["flags"]
    assert len(body["fills"]) == row["n_fills"]
    assert set(body["fills"][0]["markouts_bps"]) == {"10s", "30s", "60s", "300s"}


def test_summary_matches_the_analysis(client: TestClient, analysis: AnalysisResult) -> None:
    body = client.get("/api/v1/summary").json()
    assert body["overall"] == analysis.summary.model_dump(mode="json")
    strategies = {g["group"] for g in body["by_strategy"]}
    assert strategies == set(analysis.orders["strategy"].unique())
    assert len(body["flags"]) == analysis.flag_counts.height


def test_unknown_order_returns_404(client: TestClient) -> None:
    for path in ("/api/v1/orders/NOPE", "/api/v1/orders/NOPE/tca"):
        response = client.get(path)
        assert response.status_code == 404
        assert response.json() == {"detail": "Order 'NOPE' not found"}


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 5000}, {"offset": -1}, {"side": "HOLD"}, {"flagged": "x"}]
)
def test_invalid_query_parameters_return_422(client: TestClient, params: dict[str, object]) -> None:
    assert client.get("/api/v1/orders", params=params).status_code == 422


def test_simulation_round_trip(client: TestClient) -> None:
    request = {"scenario": "low_liquidity", "quantity": 2_000, "horizon_seconds": 600, "trials": 2}
    created = client.post("/api/v1/simulations", json=request)
    assert created.status_code == 201
    body = created.json()
    assert created.headers["location"] == f"/api/v1/simulations/{body['simulation_id']}"
    assert {r["strategy"] for r in body["results"]} == {"IMMEDIATE", "TWAP", "VWAP", "POV"}
    assert all(r["trials"] == 2 for r in body["results"])
    assert "scenario-specific" in body["disclaimer"]
    fetched = client.get(f"/api/v1/simulations/{body['simulation_id']}")
    assert fetched.status_code == 200
    assert fetched.json() == body


def test_simulation_oracle_is_marked_non_implementable(client: TestClient) -> None:
    body = client.post(
        "/api/v1/simulations",
        json={"horizon_seconds": 600, "trials": 2, "include_oracle": True, "strategies": ["TWAP"]},
    ).json()
    implementable = {r["strategy"]: r["implementable"] for r in body["results"]}
    assert implementable == {"TWAP": True, "ORACLE_VWAP": False}


@pytest.mark.parametrize(
    "payload",
    [
        {"quantity": 0},
        {"trials": 500},
        {"strategies": ["MAGIC"]},
        {"strategies": []},
        {"scenario": "crash"},
        {"participation_rate": 2},
        {"strategies": ["TWAP", "TWAP"]},  # rejected by the domain model
    ],
)
def test_invalid_simulations_return_422(client: TestClient, payload: dict[str, object]) -> None:
    response = client.post("/api/v1/simulations", json=payload)
    assert response.status_code == 422
    assert "Traceback" not in response.text


def test_unknown_simulation_returns_404(client: TestClient) -> None:
    assert client.get("/api/v1/simulations/does-not-exist").status_code == 404


def test_unexpected_errors_do_not_leak_tracebacks(analysis: AnalysisResult) -> None:
    app = create_app(analysis=analysis)

    def boom() -> None:
        raise RuntimeError("secret internal detail")

    app.add_api_route("/boom", boom)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/boom")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "secret" not in response.text


def test_openapi_documents_every_endpoint_and_sign_conventions(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    paths = set(spec["paths"])
    assert {
        "/health",
        "/api/v1/orders",
        "/api/v1/orders/{order_id}",
        "/api/v1/orders/{order_id}/tca",
        "/api/v1/summary",
        "/api/v1/simulations",
        "/api/v1/simulations/{simulation_id}",
    } <= paths
    text = client.get("/openapi.json").text
    assert "Positive = worse execution" in text
    assert "Positive = FAVOURABLE" in text
    assert client.get("/docs").status_code == 200


def test_app_loads_the_dataset_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXECLENS_DATA_DIR", str(SAMPLE_DIR))
    with TestClient(create_app()) as test_client:
        assert test_client.get("/health").json()["dataset"]["orders"] == 240


def test_simulation_store_is_bounded() -> None:
    store = SimulationStore(max_items=2)
    responses = [
        SimulationResponse.model_validate(
            {
                "simulation_id": f"s{i}",
                "created_at": "2026-01-01T00:00:00Z",
                "request": {},
                "results": [],
                "conclusions": [],
            }
        )
        for i in range(3)
    ]
    for response in responses:
        store.put(response)
    assert len(store) == 2
    assert store.get("s0") is None
    assert store.get("s2") == responses[2]


def test_api_scenario_names_match_the_generator() -> None:
    assert set(get_args(ScenarioName)) == set(SCENARIOS)
