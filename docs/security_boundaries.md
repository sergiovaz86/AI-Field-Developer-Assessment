# Security boundaries

## Task 1 stdio

Stdout is reserved for MCP transport frames. The Phase 1 process writes its diagnostic line to stderr and does not start the transport. Logging is configured by removing any existing root handlers and attaching one `StreamHandler` aimed at stderr. Task 1 code must not call `print` or write to `sys.stdout`.

A failing operation, once the transport exists, must not write a traceback or an HTTP body to stdout.

## Import boundary

`task1_mcp_server` does not import `gateway_service`. Gateway shared schemas, settings, and test helpers are not available to Task 1. The two logging setups are separate for that reason.

The Task 1 container image does not contain gateway source. The gateway image does not contain Task 1 source. Compose does not connect the two processes with pipes.

## Authentication

The bearer catalog is the static map in `gateway_service/task2_mcp_gateway/authentication.py`. An unrecognized token is not a viewer. Authorization reads the authenticated role, not a role field from the JSON-RPC body. Missing, malformed, and unknown credentials return HTTP 401 before the body is parsed.

The proxy does not log bearer tokens. Gateway access logs are off. `redacted_view` drops provider API keys and the Task 4 tenant bearer map. Configuration errors name the invalid field and do not echo the supplied value.

Provider URLs that contain whitespace or an `@` credential are rejected at configuration time.

## Tenancy and storage

`GATEWAY_MODE=production` requires the tenant bearer map, both provider credentials, and an initialized ledger. The executable and the ASGI lifespan both enforce that check before serving. A missing value does not start the server and does not fall back to unauthenticated completions. Startup logs name the invalid field. They do not include bearer tokens, provider API keys, or the database path from the readiness error. `--check` logs `redacted_view`, which omits those secrets. `assessment_mock` is the default. In that mode, when `GATEWAY_TENANT_TOKENS` is set, chat completions take tenant identity from that server-side bearer map. The ledger stores `sha256` of the tenant name and never the bearer. A tenant id in the request body is ignored. The SQLite schema has no API-key column. `/mcp` keeps the Task 2 admin and viewer map and does not accept a Task 4 tenant bearer. When the mode is `assessment_mock` and `GATEWAY_TENANT_TOKENS` is unset, chat completions stay on the unauthenticated Task 3 path so existing clients keep working.

SQL from application code must be parameterized. The migration runner uses a bound parameter for the version insert. The migration text itself is a trusted file shipped with the repository.

## Downstream and streaming

Denied `admin_` tool calls are not forwarded. Inbound authentication headers are not forwarded. The downstream URL must be the configured URL and an exact allowlist entry. Timeouts, connection failures, and malformed upstream envelopes become JSON-RPC `-32603` without the upstream URL or body.

Untrusted JSON and JSON-RPC are validated in the Task 2 parser before authorization. The chat route validates the JSON body before it reads an upstream stream. SSE frames are decoded with the UTF-8 incremental decoder and `json.loads`. The parser does not use `eval` or `exec`.

Generated `content`, `refusal`, and tool or function arguments are redacted. `role`, tool-call `id`, and tool-call `type` are copied. Other generated delta fields are removed. The chat route does not log prompts or bearer credentials. Upstream failures do not include the provider URL or response body. Backup-provider bytes go through the same redactor.

## Errors

Chat failures before the first SSE byte use an OpenAI-compatible JSON error. The body includes `correlation_id`, and the response includes `x-correlation-id`. They exclude stack traces, database paths, credentials, SQL text, internal hostnames, and raw upstream bodies. Missing or unknown tenant credentials are HTTP 401 `invalid_api_key`. A local quota rejection is HTTP 429 `quota_exceeded` and does not call a provider. A ledger failure is HTTP 503 `database_unavailable`. After streaming has started, the HTTP status is unchanged and the stream ends with one sanitized SSE error object and no `[DONE]`. The health route does not have an error envelope. FastAPI's default 404 remains for unregistered paths and is not an assessment error contract.
