"""Request-scoped dependencies.

Phase 1 exposes configuration lookup only. Database connections and HTTP
clients are not constructed here.
"""

from typing import Annotated

from fastapi import Depends, Request

from gateway_service.config import GatewayConfig


def get_config(request: Request) -> GatewayConfig:
    """Return the config stored by ``create_app``."""
    config = getattr(request.app.state, "config", None)
    if not isinstance(config, GatewayConfig):
        raise RuntimeError("Gateway configuration is not initialized")
    return config


ConfigDependency = Annotated[GatewayConfig, Depends(get_config)]
