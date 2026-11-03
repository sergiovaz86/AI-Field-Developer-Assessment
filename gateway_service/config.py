"""Process configuration read from the environment.

The gateway does not load dotenv files. Empty variables are treated as unset.
Integer variables must be canonical base-10 text. Secret values are held in
``SecretStr`` and omitted from ``redacted_view``.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from gateway_service.shared.exceptions import ConfigurationError

_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_HTTP_SCHEMES = ("http://", "https://")
_SECRET_FIELDS = {
    "primary_provider_api_key",
    "backup_provider_api_key",
    "tenant_bearer_tokens",
}


class GatewayConfig(BaseModel):
    """Operator-owned gateway settings."""

    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    database_path: Path = Path(".runtime/gateway.db")
    sqlite_busy_timeout_ms: int = Field(default=5000, gt=0)
    default_tokens_per_window: int = Field(default=50_000, gt=0)
    window_ms: int = Field(default=60_000, gt=0)
    first_byte_timeout_ms: int = Field(default=3000, gt=0)
    default_max_output_tokens: int = Field(default=256, gt=0)
    max_output_token_budget: int = Field(default=4096, gt=0)
    log_level: str = "INFO"
    tenant_bearer_tokens: tuple[tuple[str, str], ...] = ()
    downstream_mcp_url: str | None = None
    downstream_mcp_allowlist: tuple[str, ...] = ()
    downstream_timeout_ms: int = Field(default=3000, gt=0)
    max_json_body_bytes: int = Field(default=1_048_576, gt=0)
    max_batch_items: int = Field(default=32, gt=0)
    gateway_mode: Literal["assessment_mock", "production"] = "assessment_mock"
    llm_upstream: Literal["mock", "openai"] = "mock"
    primary_provider_base_url: str | None = None
    backup_provider_base_url: str | None = None
    primary_provider_api_key: SecretStr | None = None
    backup_provider_api_key: SecretStr | None = None

    @field_validator("host")
    @classmethod
    def _host_must_be_explicit(cls, value: str) -> str:
        if value == "" or value != value.strip():
            raise ValueError("host must be a non-empty string without surrounding whitespace")
        return value

    @field_validator("log_level")
    @classmethod
    def _log_level_must_be_known(cls, value: str) -> str:
        if value not in _LOG_LEVELS:
            raise ValueError("log_level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        return value

    @field_validator("database_path")
    @classmethod
    def _database_path_must_be_present(cls, value: Path) -> Path:
        rendered = str(value)
        if rendered.strip() == "":
            raise ValueError("database_path must not be empty")
        return value

    @field_validator(
        "downstream_mcp_url",
        "primary_provider_base_url",
        "backup_provider_base_url",
    )
    @classmethod
    def _optional_http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(character.isspace() for character in value) or "@" in value:
            raise ValueError("URLs must not contain whitespace or embedded credentials")
        if not value.startswith(_HTTP_SCHEMES):
            raise ValueError("URLs must use the http or https scheme")
        return value

    @field_validator("downstream_mcp_allowlist")
    @classmethod
    def _allowlist_urls(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if (
                item == ""
                or any(character.isspace() for character in item)
                or "@" in item
                or not item.startswith(_HTTP_SCHEMES)
            ):
                raise ValueError("allowlist entries must be http or https URLs without credentials")
        return value

    @model_validator(mode="after")
    def _downstream_url_must_be_allowlisted(self) -> Self:
        if self.downstream_mcp_url is None:
            return self
        if self.downstream_mcp_url not in self.downstream_mcp_allowlist:
            raise ValueError("downstream_mcp_url is not allowlisted")
        return self

    @model_validator(mode="after")
    def _output_budget_fits_under_the_ceiling(self) -> Self:
        if self.default_max_output_tokens > self.max_output_token_budget:
            raise ValueError("default_max_output_tokens exceeds max_output_token_budget")
        names: set[str] = set()
        secrets: set[str] = set()
        for name, token in self.tenant_bearer_tokens:
            if (
                name == ""
                or token == ""
                or any(character.isspace() for character in name)
                or any(character.isspace() for character in token)
            ):
                raise ValueError("tenant bearer entries must not contain whitespace")
            if name in names or token in secrets:
                raise ValueError("tenant bearer entries must be unique")
            names.add(name)
            secrets.add(token)
        if self.gateway_mode == "production":
            if not self.tenant_bearer_tokens:
                raise ValueError("production mode requires tenant_bearer_tokens")
            if self.llm_upstream != "openai":
                raise ValueError("production mode requires an openai provider")
            if not _secret_present(self.primary_provider_api_key) or (
                self.primary_provider_base_url is None
            ):
                raise ValueError("production mode requires the primary provider")
            if not _secret_present(self.backup_provider_api_key) or (
                self.backup_provider_base_url is None
            ):
                raise ValueError("production mode requires the backup provider")
        return self

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> GatewayConfig:
        """Build a config from process environment variables."""
        source: Mapping[str, str] = os.environ if environ is None else environ
        payload: dict[str, Any] = {
            "host": _env_str(source, "GATEWAY_HOST", "127.0.0.1"),
            "port": _env_int(source, "GATEWAY_PORT", 8080),
            "database_path": Path(_env_str(source, "GATEWAY_DATABASE_PATH", ".runtime/gateway.db")),
            "sqlite_busy_timeout_ms": _env_int(source, "GATEWAY_SQLITE_BUSY_TIMEOUT_MS", 5000),
            "default_tokens_per_window": _env_int(
                source, "GATEWAY_DEFAULT_TOKENS_PER_WINDOW", 50_000
            ),
            "window_ms": _env_int(source, "GATEWAY_WINDOW_MS", 60_000),
            "first_byte_timeout_ms": _env_int(source, "GATEWAY_FIRST_BYTE_TIMEOUT_MS", 3000),
            "default_max_output_tokens": _env_int(source, "GATEWAY_DEFAULT_MAX_OUTPUT_TOKENS", 256),
            "max_output_token_budget": _env_int(source, "GATEWAY_MAX_OUTPUT_TOKEN_BUDGET", 4096),
            "log_level": _env_str(source, "GATEWAY_LOG_LEVEL", "INFO"),
            "tenant_bearer_tokens": _env_tenant_tokens(source, "GATEWAY_TENANT_TOKENS"),
            "downstream_mcp_url": _env_optional(source, "GATEWAY_DOWNSTREAM_MCP_URL"),
            "downstream_mcp_allowlist": _env_allowlist(source, "GATEWAY_DOWNSTREAM_MCP_ALLOWLIST"),
            "downstream_timeout_ms": _env_int(source, "GATEWAY_DOWNSTREAM_TIMEOUT_MS", 3000),
            "max_json_body_bytes": _env_int(source, "GATEWAY_MAX_JSON_BODY_BYTES", 1_048_576),
            "max_batch_items": _env_int(source, "GATEWAY_MAX_BATCH_ITEMS", 32),
            "gateway_mode": _env_str(source, "GATEWAY_MODE", "assessment_mock"),
            "llm_upstream": _env_str(source, "GATEWAY_LLM_UPSTREAM", "mock"),
            "primary_provider_base_url": _env_optional(source, "GATEWAY_PRIMARY_PROVIDER_BASE_URL"),
            "backup_provider_base_url": _env_optional(source, "GATEWAY_BACKUP_PROVIDER_BASE_URL"),
            "primary_provider_api_key": _env_secret(source, "GATEWAY_PRIMARY_PROVIDER_API_KEY"),
            "backup_provider_api_key": _env_secret(source, "GATEWAY_BACKUP_PROVIDER_API_KEY"),
        }
        downstream_url = payload["downstream_mcp_url"]
        allowlist = payload["downstream_mcp_allowlist"]
        if downstream_url is not None and downstream_url not in allowlist:
            raise ConfigurationError("gateway configuration is invalid: downstream_mcp_url")
        try:
            return cls.model_validate(payload)
        except ValidationError as exc:
            fields = ", ".join(str(error["loc"][0]) for error in exc.errors() if error["loc"])
            if fields == "":
                fields = "gateway_mode"
            raise ConfigurationError(f"gateway configuration is invalid: {fields}") from None

    def redacted_view(self) -> dict[str, Any]:
        """Return a JSON-ready view with provider credentials removed."""
        return self.model_dump(mode="json", exclude=_SECRET_FIELDS)


def require_initialized_database(config: GatewayConfig) -> None:
    """Reject production startup until the migrated ledger file exists.

    Assessment mode does not open the database. The check uses a read-only
    connection and does not create a file or apply the migration.
    """
    if config.gateway_mode != "production":
        return
    path = config.database_path
    if not path.is_file():
        raise ConfigurationError("gateway configuration is invalid: database_path")
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        raise ConfigurationError("gateway configuration is invalid: database_path") from None
    try:
        try:
            row = connection.execute(
                "SELECT version FROM schema_migrations WHERE version = 1"
            ).fetchone()
        except sqlite3.Error:
            raise ConfigurationError("gateway configuration is invalid: database_path") from None
    finally:
        connection.close()
    if row is None:
        raise ConfigurationError("gateway configuration is invalid: database_path")


def _secret_present(value: SecretStr | None) -> bool:
    return value is not None and value.get_secret_value() != ""


def _env_str(environ: Mapping[str, str], name: str, default: str) -> str:
    if name not in environ or environ[name] == "":
        return default
    return environ[name]


def _env_allowlist(environ: Mapping[str, str], name: str) -> tuple[str, ...]:
    raw = _env_optional(environ, name)
    if raw is None:
        return ()
    entries: list[str] = []
    for piece in raw.split(","):
        item = piece.strip()
        if item == "":
            continue
        if (
            any(character.isspace() for character in item)
            or "@" in item
            or not item.startswith(_HTTP_SCHEMES)
        ):
            raise ConfigurationError(f"gateway configuration is invalid: {name}")
        entries.append(item)
    return tuple(entries)


def _env_tenant_tokens(environ: Mapping[str, str], name: str) -> tuple[tuple[str, str], ...]:
    raw = _env_optional(environ, name)
    if raw is None:
        return ()
    entries: list[tuple[str, str]] = []
    for piece in raw.split(","):
        item = piece.strip()
        if item == "":
            continue
        tenant_name, separator, token = item.partition("=")
        if separator != "=" or tenant_name == "" or token == "":
            raise ConfigurationError(f"gateway configuration is invalid: {name}")
        entries.append((tenant_name, token))
    return tuple(entries)


def _env_optional(environ: Mapping[str, str], name: str) -> str | None:
    if name not in environ or environ[name] == "":
        return None
    return environ[name]


def _env_secret(environ: Mapping[str, str], name: str) -> SecretStr | None:
    if name not in environ or environ[name] == "":
        return None
    raw = environ[name]
    if raw.strip() == "":
        raise ConfigurationError(f"{name} must not be whitespace")
    return SecretStr(raw)


def _env_int(environ: Mapping[str, str], name: str, default: int) -> int:
    if name not in environ or environ[name] == "":
        return default
    raw = environ[name]
    negative = raw.startswith("-")
    digits = raw[1:] if negative else raw
    if digits == "" or not digits.isdecimal() or digits != str(int(digits)):
        raise ConfigurationError(f"{name} must be a canonical integer")
    value = int(digits)
    if negative:
        return -value
    return value
