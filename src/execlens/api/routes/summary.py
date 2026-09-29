"""Portfolio summary endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request

from execlens.api.schemas import SummaryResponse
from execlens.api.state import ApiState

router = APIRouter(prefix="/api/v1", tags=["summary"])


@router.get("/summary", response_model=SummaryResponse, summary="Portfolio-level TCA summary")
def get_summary(request: Request) -> SummaryResponse:
    """Overall statistics, per-strategy and per-symbol groups and flag counts.

    Prepared once at startup from the immutable analysis.
    """
    state: ApiState = request.app.state.execlens
    return state.summary
