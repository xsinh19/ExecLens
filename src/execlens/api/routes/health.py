"""Liveness endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request

from execlens import __version__
from execlens.api.schemas import HealthResponse
from execlens.api.state import ApiState

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Service and dataset status")
def health(request: Request) -> HealthResponse:
    state: ApiState = request.app.state.execlens
    return HealthResponse(version=__version__, dataset=state.dataset_info)
