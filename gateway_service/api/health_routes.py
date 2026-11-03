"""Process health route.

This route does not read SQLite or call a provider. It exists so the Phase 1
process can be probed without implementing assessment handlers.
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

router = APIRouter()


class HealthStatus(BaseModel):
    """Liveness payload."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]


@router.get("/health", response_model=HealthStatus)
async def health() -> HealthStatus:
    """Report that the process is serving the Phase 1 scaffold."""
    return HealthStatus(status="ok")
