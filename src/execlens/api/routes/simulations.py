"""Strategy-comparison simulations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import ValidationError

from execlens.api import mapping
from execlens.api.schemas import ErrorResponse, SimulationRequest, SimulationResponse
from execlens.api.state import ApiState
from execlens.simulation.comparison import compare_strategies

router = APIRouter(prefix="/api/v1/simulations", tags=["simulations"])


@router.post(
    "",
    response_model=SimulationResponse,
    status_code=201,
    responses={422: {"description": "Invalid simulation parameters"}},
    summary="Run a strategy comparison",
)
def create_simulation(
    request: Request, body: SimulationRequest, response: Response
) -> SimulationResponse:
    """Run every requested strategy on identical synthetic sessions and score them with TCA.

    Runs synchronously (typically 1-5 s) and stores the result for retrieval.
    """
    try:
        domain_request = mapping.comparison_request(body)
    except ValidationError as exc:
        messages = "; ".join(error["msg"] for error in exc.errors())
        raise HTTPException(status_code=422, detail=messages) from None
    result = compare_strategies(domain_request)
    simulation = mapping.simulation_response(uuid.uuid4().hex, datetime.now(UTC), body, result)
    state: ApiState = request.app.state.execlens
    state.simulations.put(simulation)
    response.headers["Location"] = f"/api/v1/simulations/{simulation.simulation_id}"
    return simulation


@router.get(
    "/{simulation_id}",
    response_model=SimulationResponse,
    responses={404: {"model": ErrorResponse, "description": "Unknown or expired simulation"}},
    summary="Fetch a stored simulation",
)
def get_simulation(request: Request, simulation_id: str) -> SimulationResponse:
    state: ApiState = request.app.state.execlens
    simulation = state.simulations.get(simulation_id)
    if simulation is None:
        raise HTTPException(status_code=404, detail=f"Simulation {simulation_id!r} not found")
    return simulation
