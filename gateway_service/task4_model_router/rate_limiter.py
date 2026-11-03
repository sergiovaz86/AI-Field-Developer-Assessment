"""Tenant identity and rolling-window admission.

The bearer map is server configuration. The ledger stores
``sha256(tenant name)`` and never the bearer token. A request is charged
while its admission timestamp is inside the rolling window or its status is
still ``reserved``, ``in_flight``, or ``timed_out``. One row that meets both
conditions is summed once.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping

from gateway_service.task4_model_router.sqlite_store import (
    QuotaRejected,
    Reservation,
    ReservationLedger,
)

__all__ = ["QuotaRejected", "authenticate_tenant", "tenant_hash"]


def tenant_hash(name: str) -> str:
    """Return the stable non-secret identifier stored for a tenant name."""
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def authenticate_tenant(
    authorization: str | None,
    tokens: tuple[tuple[str, str], ...],
) -> str | None:
    """Return the tenant hash for one configured bearer, or ``None``."""
    if not tokens:
        return None
    token = _bearer_token(authorization)
    if token is None:
        return None
    for name, configured in tokens:
        if _constant_equal(token, configured):
            return tenant_hash(name)
    return None


class RateLimiter:
    """Admit one logical request against the tenant's rolling window."""

    def __init__(self, ledger: ReservationLedger) -> None:
        self._ledger = ledger

    @property
    def ledger(self) -> ReservationLedger:
        return self._ledger

    async def admit(
        self,
        *,
        tenant_id: str,
        request_id: str,
        correlation_id: str,
        estimated_input_tokens: int,
        reserved_output_tokens: int,
        now_ms: int,
        status: str = "reserved",
    ) -> Reservation:
        await self._ledger.recover_once(now_ms)
        return await self._ledger.admit(
            tenant_id=tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
            estimated_input_tokens=estimated_input_tokens,
            reserved_output_tokens=reserved_output_tokens,
            now_ms=now_ms,
            status=status,
        )


def configured_names(tokens: tuple[tuple[str, str], ...]) -> Mapping[str, str]:
    """Map a tenant hash back to its configured name for tests and logs."""
    return {tenant_hash(name): name for name, _token in tokens}


def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or token == "" or " " in token:
        return None
    return token


def _constant_equal(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)
