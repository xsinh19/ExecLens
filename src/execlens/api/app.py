"""FastAPI application factory.

The dataset is loaded and fully analysed once at startup (TCA, diagnostics,
summary), so request handlers only look results up. Run with::

    execlens serve --data data/sample
    # or: uvicorn execlens.api.app:create_app --factory
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from execlens import __version__
from execlens.api.routes import health, orders, simulations, summary
from execlens.api.state import ApiState
from execlens.config import Settings
from execlens.services.analysis import AnalysisConfig, AnalysisResult, analyze_directory

logger = logging.getLogger("execlens.api")

DESCRIPTION = """
Execution analytics and transaction cost analysis (TCA) over a **synthetic** dataset.

* Cost metrics (`*_cost_bps`, `effective_spread_bps`): **positive = worse** execution.
* Markouts (`markout_bps`, `mid_markout_bps`): **positive = favourable** subsequent move
  (opposite orientation).

All market data is synthetic and all executions are simulated.
"""


def create_app(
    settings: Settings | None = None, *, analysis: AnalysisResult | None = None
) -> FastAPI:
    """Build the app. Pass ``analysis`` to serve a precomputed result (used in tests)."""
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        result = analysis
        if result is None:
            logger.info("Analysing dataset %s", settings.data_dir)
            config = AnalysisConfig(
                stale_quote_tolerance_seconds=settings.stale_quote_tolerance_seconds
            )
            result = analyze_directory(settings.data_dir, config)
        app.state.execlens = ApiState(
            analysis=result, dataset_name=settings.data_dir.resolve().name
        )
        yield

    app = FastAPI(
        title="ExecLens API",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(orders.router)
    app.include_router(summary.router)
    app.include_router(simulations.router)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Log the details server-side; never leak a traceback to clients.
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    return app
