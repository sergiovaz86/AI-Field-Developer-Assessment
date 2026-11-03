"""When one provider attempt may be replaced by the backup attempt.

Eligible failures are HTTP 429, HTTP 5xx, connection errors, and a first-byte
timeout. Any other category, including other HTTP 4xx responses and protocol
errors, ends the logical request. A second attempt is also forbidden after
any client-visible downstream byte has been emitted.
"""

from __future__ import annotations

FAILOVER_CATEGORIES: frozenset[str] = frozenset({"http_429", "http_5xx", "network", "timeout"})


def is_failover_eligible(category: str) -> bool:
    """Return whether this failure may start the single backup attempt."""
    return category in FAILOVER_CATEGORIES
