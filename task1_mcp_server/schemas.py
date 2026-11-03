"""Strict tool arguments and deterministic tool results.

Argument models reject unknown fields and do not coerce JSON types.
A JSON number may be an integer or a float. Booleans, strings, NaN, and
infinities are not amounts.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

CUSTOMER_ID_PATTERN = r"^CUST-[0-9]{5}$"
REFUND_ID_PATTERN = r"^RFND-[0-9]{6}$"


def _require_str(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("value must be a string")
    return value


def _require_positive_amount(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("amount must be a finite number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("amount must be a finite number")
    if value <= 0:
        raise ValueError("amount must be strictly positive")
    return float(value)


CustomerId = Annotated[str, BeforeValidator(_require_str), Field(pattern=CUSTOMER_ID_PATTERN)]
ReasonText = Annotated[str, BeforeValidator(_require_str), Field(min_length=10)]
PositiveAmount = Annotated[float, BeforeValidator(_require_positive_amount), Field(gt=0)]


class StrictArguments(BaseModel):
    """Base for tool arguments installed in place of the SDK's generated model."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=False)

    def model_dump_one_level(self) -> dict[str, Any]:
        """Return field values without walking nested models."""
        return {name: getattr(self, name) for name in type(self).model_fields}


def argument_json_schema(model: type[StrictArguments]) -> dict[str, Any]:
    """Publish the argument contract on ``tools/list``.

    Pydantic 2.14 omits ``pattern`` from the generated schema when a
    ``BeforeValidator`` is attached, and it emits ``gt`` instead of
    ``exclusiveMinimum``. Runtime validation still uses the model. This map
    is the schema clients see.
    """
    schema = model.model_json_schema()
    properties = schema.setdefault("properties", {})
    if "customer_id" in properties:
        properties["customer_id"] = {"type": "string", "pattern": CUSTOMER_ID_PATTERN}
    if "amount" in properties:
        properties["amount"] = {"type": "number", "exclusiveMinimum": 0}
    if "reason" in properties:
        properties["reason"] = {"type": "string", "minLength": 10}
    schema["type"] = "object"
    schema["additionalProperties"] = False
    schema.pop("title", None)
    schema.pop("description", None)
    return schema


class GetCustomerRecordArguments(StrictArguments):
    """Arguments for ``get_customer_record``."""

    customer_id: CustomerId


class TriggerRefundArguments(StrictArguments):
    """Arguments for ``trigger_refund``."""

    customer_id: CustomerId
    amount: PositiveAmount
    reason: ReasonText


class CustomerRecord(BaseModel):
    """Snapshot returned by ``get_customer_record``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    customer_id: str
    full_name: str
    email: str
    status: Literal["active", "suspended"]
    account_balance_cents: int = Field(ge=0)
    refund_count: int = Field(ge=0)


class RefundRecord(BaseModel):
    """One completed simulated refund."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    refund_id: str = Field(pattern=REFUND_ID_PATTERN)
    customer_id: str = Field(pattern=CUSTOMER_ID_PATTERN)
    amount: float = Field(gt=0)
    reason: str = Field(min_length=10)
    status: Literal["completed"]
    prior_refund_count: int = Field(ge=0)
    refund_count: int = Field(ge=1)
