"""Gateway application factory.

The process serves ``GET /health``, ``POST /mcp``, and
``POST /v1/chat/completions``. It does not open SQLite and does not call
providers during application construction.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from gateway_service.api.completion_routes import router as completion_router
from gateway_service.api.health_routes import router as health_router
from gateway_service.api.mcp_routes import router as mcp_router
from gateway_service.config import GatewayConfig, require_initialized_database
from gateway_service.shared.exceptions import ConfigurationError
from gateway_service.shared.logging import configure_logging, stderr_log_config
from gateway_service.task4_model_router.router import drain_ledger_cleanup

LOGGER = logging.getLogger("gateway_service")


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Validate production readiness, then drain detached ledger writes.

    Application construction does not open SQLite. This lifespan is the ASGI
    startup path, including ``uvicorn gateway_service.main:create_app
    --factory``. It uses the same production check as ``python -m
    gateway_service`` and does not apply the migration.
    """
    config = application.state.config
    try:
        require_initialized_database(config)
    except ConfigurationError as exc:
        LOGGER.error("%s", exc)
        raise
    yield
    await drain_ledger_cleanup()


def create_app(config: GatewayConfig | None = None) -> FastAPI:
    """Build the gateway without starting a server or touching the database."""
    resolved = config if config is not None else GatewayConfig.from_environ()
    application = FastAPI(
        title="AI Field Assessment Gateway",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
        lifespan=_lifespan,
    )
    application.state.config = resolved
    application.include_router(health_router)
    application.include_router(mcp_router)
    application.include_router(completion_router)
    return application


def main(argv: list[str] | None = None) -> None:
    """Check configuration, or serve the gateway.

    ``--check`` validates the environment and returns without binding a port.
    Any other invocation serves HTTP. ``GATEWAY_MODE=assessment_mock`` keeps
    the offline completion path. ``GATEWAY_MODE=production`` requires tenant
    credentials, both providers, and an initialized ledger before the process
    binds a port. The ASGI lifespan repeats that production check before the
    first request.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        config = GatewayConfig.from_environ()
    except ConfigurationError as exc:
        configure_logging("INFO")
        LOGGER.error("%s", exc)
        raise SystemExit(1) from None
    configure_logging(config.log_level)
    try:
        require_initialized_database(config)
    except ConfigurationError as exc:
        LOGGER.error("%s", exc)
        raise SystemExit(1) from None
    if "--check" in args:
        LOGGER.info("gateway configuration accepted %s", config.redacted_view())
        LOGGER.info("HTTP server was not started.")
        return
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        log_config=stderr_log_config(config.log_level),
        access_log=False,
    )
