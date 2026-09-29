"""Order listing, detail and TCA endpoints."""

from __future__ import annotations

from typing import Annotated, Any

import polars as pl
from fastapi import APIRouter, HTTPException, Query, Request

from execlens.api import mapping
from execlens.api.schemas import ErrorResponse, OrderDetail, OrderListResponse, OrderTca
from execlens.api.state import ApiState
from execlens.data.schemas import Side

router = APIRouter(prefix="/api/v1/orders", tags=["orders"])
NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Unknown order_id"}
}


def _state(request: Request) -> ApiState:
    state: ApiState = request.app.state.execlens
    return state


def _order_or_404(state: ApiState, order_id: str) -> dict[str, object]:
    row = state.order(order_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Order {order_id!r} not found")
    return row


@router.get("", response_model=OrderListResponse, summary="List analysed orders")
def list_orders(
    request: Request,
    symbol: Annotated[str | None, Query(max_length=64)] = None,
    side: Side | None = None,
    strategy: Annotated[str | None, Query(max_length=32)] = None,
    flagged: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OrderListResponse:
    """Orders in arrival order, optionally filtered by symbol, side, strategy and flag state."""
    orders = _state(request).analysis.orders
    conditions = []
    if symbol is not None:
        conditions.append(pl.col("symbol") == symbol)
    if side is not None:
        conditions.append(pl.col("side") == side.value)
    if strategy is not None:
        conditions.append(pl.col("strategy") == strategy)
    if flagged is not None:
        conditions.append(pl.col("is_flagged") == flagged)
    selected = orders.filter(*conditions) if conditions else orders
    page = selected.slice(offset, limit)
    return OrderListResponse(
        items=[mapping.order_summary(row) for row in page.iter_rows(named=True)],
        total=selected.height,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{order_id}", response_model=OrderDetail, responses=NOT_FOUND, summary="Order and its fills"
)
def get_order(request: Request, order_id: str) -> OrderDetail:
    state = _state(request)
    return mapping.order_detail(_order_or_404(state, order_id), state.fills(order_id))


@router.get(
    "/{order_id}/tca",
    response_model=OrderTca,
    responses=NOT_FOUND,
    summary="Transaction cost analysis of one order",
)
def get_order_tca(request: Request, order_id: str) -> OrderTca:
    """Arrival context, execution statistics, benchmark interval, costs (positive = worse),
    markouts (positive = favourable), diagnostics and per-fill metrics."""
    state = _state(request)
    return mapping.order_tca(
        _order_or_404(state, order_id),
        state.fills(order_id),
        state.analysis.config.markout_horizons_seconds,
    )
