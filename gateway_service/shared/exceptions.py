"""Gateway failures that must be mapped before they reach a client."""


class GatewayError(Exception):
    """Base class for gateway failures."""


class ConfigurationError(GatewayError):
    """Raised when process configuration is missing or invalid."""
