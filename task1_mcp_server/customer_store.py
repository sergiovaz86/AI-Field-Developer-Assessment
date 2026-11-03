"""Process-local customer and refund state.

Successful refunds receive ids ``RFND-000001``, ``RFND-000002``, and so on,
in lock order. Concurrent callers receive distinct ids. A missing customer
does not allocate an id and does not change ``refund_count``. Validation
failures never reach this store.

``status`` and ``account_balance_cents`` do not gate a refund. No balance or
suspension rule was specified, so a suspended customer and a refund larger
than the stored balance both complete. The balance is never reduced.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from task1_mcp_server.fixtures.customers import CUSTOMERS, CustomerSeed
from task1_mcp_server.schemas import CustomerRecord, RefundRecord


class UnknownCustomer(Exception):
    """The customer id is well formed and absent from the fixture store."""

    def __init__(self) -> None:
        super().__init__("Unknown customer_id")


@dataclass
class _CustomerState:
    customer_id: str
    full_name: str
    email: str
    status: str
    account_balance_cents: int
    refund_count: int = 0


class CustomerStore:
    """In-memory fixture store guarded by one lock."""

    def __init__(self, seeds: tuple[CustomerSeed, ...] = CUSTOMERS) -> None:
        self._lock = Lock()
        self._customers = {
            seed.customer_id: _CustomerState(
                customer_id=seed.customer_id,
                full_name=seed.full_name,
                email=seed.email,
                status=seed.status,
                account_balance_cents=seed.account_balance_cents,
            )
            for seed in seeds
        }
        self._next_refund_number = 1

    def get_customer(self, customer_id: str) -> CustomerRecord | None:
        """Return a snapshot, or ``None`` when the id is unknown."""
        with self._lock:
            state = self._customers.get(customer_id)
            if state is None:
                return None
            return _snapshot(state)

    def trigger_refund(self, customer_id: str, amount: float, reason: str) -> RefundRecord:
        """Append one completed refund and advance the customer's counter.

        Raises:
            UnknownCustomer: ``customer_id`` is not in the fixture store.
        """
        with self._lock:
            state = self._customers.get(customer_id)
            if state is None:
                raise UnknownCustomer
            prior = state.refund_count
            refund_id = f"RFND-{self._next_refund_number:06d}"
            self._next_refund_number += 1
            state.refund_count = prior + 1
            return RefundRecord(
                refund_id=refund_id,
                customer_id=state.customer_id,
                amount=amount,
                reason=reason,
                status="completed",
                prior_refund_count=prior,
                refund_count=state.refund_count,
            )


def _snapshot(state: _CustomerState) -> CustomerRecord:
    status = state.status
    if status not in ("active", "suspended"):
        raise RuntimeError("fixture status is not supported")
    return CustomerRecord(
        customer_id=state.customer_id,
        full_name=state.full_name,
        email=state.email,
        status=status,
        account_balance_cents=state.account_balance_cents,
        refund_count=state.refund_count,
    )
