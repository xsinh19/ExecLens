"""Strategy-comparison simulations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Final

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from execlens.api import mapping
from execlens.api.schemas import ErrorResponse, SimulationRequest, SimulationResponse
from execlens.api.state import ApiState
from execlens.simulation.comparison import compare_strategies

router = APIRouter(prefix="/api/v1/simulations", tags=["simulations"])
RETRY_AFTER_SECONDS: Final = 5


def _body_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Domain validation errors in FastAPI's own 422 format, located in the body."""
    return [
        {**error, "loc": ("body", *error["loc"])}
        for error in exc.errors(include_url=False, include_context=False, include_input=False)
    ]


@router.post(
    "",
    response_model=SimulationResponse,
    status_code=201,
    responses={
        503: {
            "model": ErrorResponse,
            "description": "Simulation capacity is busy; retry after the Retry-After delay",
        }
    },
    summary="Run a strategy comparison",
)
def create_simulation(
    request: Request, body: SimulationRequest, response: Response
) -> SimulationResponse:
    """Run every requested strategy on identical synthetic sessions and score them with TCA.

    Runs synchronously and stores the result for retrieval. Requests over the
    simulation budget are rejected with 422; while the maximum number of
    simulations is already running, new ones get 503 with ``Retry-After``.
    """
    try:
        domain_request = mapping.comparison_request(body)
    except ValidationError as exc:
        raise RequestValidationError(_body_errors(exc)) from None
    state: ApiState = request.app.state.execlens
    if not state.simulation_slots.acquire(blocking=False):
        raise HTTPException(
            status_code=503,
            detail="Too many simulations are running; retry shortly.",
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )
    try:
        result = compare_strategies(domain_request)
    finally:
        state.simulation_slots.release()
    simulation = mapping.simulation_response(uuid.uuid4().hex, datetime.now(UTC), body, result)
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
