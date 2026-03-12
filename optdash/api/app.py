"""FastAPI application factory.

Primary entry point:  python run_api.py  (includes scheduler)
Standalone DB-only:   uvicorn run_api:app

This module only exports the create_app() factory and _default_lifespan.
No module-level app instance is created here -- run_api.py is the sole
owner of the live FastAPI object.
"""
from contextlib import asynccontextmanager
from importlib.metadata import version as _pkg_version, PackageNotFoundError
from typing import Any
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from optdash.config import settings
from optdash.api.deps import startup, shutdown
from optdash.api.routers import market, micro, screener, ai, ws
from optdash.metrics import get_error_counts

# Single source of truth: pyproject.toml [project] version.
# Falls back to the last known version when the package is not installed
# (e.g. running with PYTHONPATH=. in a dev environment without pip install -e .).
try:
    _APP_VERSION = _pkg_version("optdash")
except PackageNotFoundError:
    _APP_VERSION = "2.0.0"


@asynccontextmanager
async def _default_lifespan(app: FastAPI):
    """Default lifespan for DB-only use (no scheduler).
    Used when create_app() is called without a custom lifespan,
    e.g. in tests or programmatic standalone usage.
    """
    logger.info("OptDash API starting up (standalone, no scheduler)...")
    await startup(app)
    yield
    logger.info("OptDash API shutting down...")
    await shutdown(app)


def create_app(lifespan: Any = None) -> FastAPI:
    """Factory.  Pass a custom lifespan to add scheduler or other startup
    logic (e.g. from run_api.py).  Defaults to DB-only lifespan.
    """
    app = FastAPI(
        title="OptDash API",
        version=_APP_VERSION,
        description="Options Analytics & AI Trading Engine",
        lifespan=lifespan or _default_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Fix-P (F-12): warn loudly when CORS is still in dev-only mode.
    # localhost origins block all production cross-origin requests silently.
    # The warning fires at factory time (process startup) so it always
    # appears in boot logs before any request is handled.
    _dev_origins = [
        o for o in settings.CORS_ORIGINS
        if "localhost" in o or "127.0.0.1" in o
    ]
    if _dev_origins:
        logger.warning(
            "CORS: dev-only origins detected -- requests from production "
            "frontends will be blocked.  Set CORS_ORIGINS in .env before "
            "deploying.  Active dev origins: {}",
            _dev_origins,
        )

    app.include_router(market.router,   prefix="/api/market",   tags=["market"])
    app.include_router(micro.router,    prefix="/api/micro",    tags=["micro"])
    app.include_router(screener.router, prefix="/api/screener", tags=["screener"])
    app.include_router(ai.router,       prefix="/api/ai",       tags=["ai"])
    app.include_router(ws.router,       prefix="/ws",           tags=["ws"])

    @app.get("/health")
    async def health():
        errors = get_error_counts()
        return {
            "status": "ok",
            "version": _APP_VERSION,
            # Issue-11: surface analytics error counts so operational issues
            # (silent empty returns from swallowed exceptions) are detectable
            # without searching through log files.
            "analytics_errors": errors if errors else None,
        }

    return app
