"""Correlation identifiers for later public error envelopes."""

from uuid import uuid4


def new_correlation_id() -> str:
    """Return a new opaque correlation identifier."""
    return uuid4().hex
