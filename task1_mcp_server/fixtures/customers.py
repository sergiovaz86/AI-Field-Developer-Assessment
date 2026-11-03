"""Deterministic customer seeds.

Records are copied into the process store at startup. Refunds do not change
these seeds; they change only the store's refund counter for a known customer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CustomerSeed:
    """Immutable fixture row."""

    customer_id: str
    full_name: str
    email: str
    status: Literal["active", "suspended"]
    account_balance_cents: int


CUSTOMERS: tuple[CustomerSeed, ...] = (
    CustomerSeed(
        customer_id="CUST-10001",
        full_name="Avery Chen",
        email="avery.chen@example.com",
        status="active",
        account_balance_cents=15_000,
    ),
    CustomerSeed(
        customer_id="CUST-10002",
        full_name="Blake Ortiz",
        email="blake.ortiz@example.com",
        status="active",
        account_balance_cents=8_200,
    ),
    CustomerSeed(
        customer_id="CUST-10003",
        full_name="Casey Nguyen",
        email="casey.nguyen@example.com",
        status="suspended",
        account_balance_cents=0,
    ),
)
