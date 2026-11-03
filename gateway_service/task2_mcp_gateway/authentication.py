"""Bearer-token authentication boundary.

The approved example map is the entire static catalog. An unrecognized
token is not an authenticated viewer. Roles are never read from a JSON-RPC
body, and this module does not log the header value.
"""

from typing import Final, Literal

Role = Literal["admin", "viewer"]

EXAMPLE_BEARER_ROLES: Final[dict[str, Role]] = {
    "admin-token": "admin",
    "viewer-token": "viewer",
}


def authenticate(authorization: str | None) -> Role | None:
    """Return the role for one Authorization header, or None when it is refused."""
    if authorization is None or "\n" in authorization or "\r" in authorization:
        return None
    parts = authorization.split(" ")
    if len(parts) != 2:
        return None
    scheme, token = parts
    if scheme.lower() != "bearer" or token == "" or any(character.isspace() for character in token):
        return None
    return EXAMPLE_BEARER_ROLES.get(token)
