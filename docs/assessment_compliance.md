# Assessment compliance

Phase 1 is the workspace bootstrap. Phase 2 implements Task 1. Phase 3 implements Task 2. Phase 4 implements Task 3. Phase 5 implements Task 4. Phase 6 packages the accepted implementation and checks deployment readiness. It does not add a fifth task.

## What matches the approved blueprint

- Python requirement is `>=3.12`. The pinned interpreter for this workspace is CPython 3.12. The development machine also has CPython 3.14, which satisfies the same lower bound. Dockerfiles use `python:3.12-slim-bookworm`.
- Direct dependencies are the official `mcp` SDK, FastAPI, httpx, Pydantic v2, aiosqlite, tiktoken, and uvicorn. Dev dependencies are pytest, pytest-asyncio, and Ruff. Versions are pinned in `uv.lock`.
- Uvicorn is the ASGI server for the approved FastAPI process. It is not an extra application service.
- Task 1 entry point is `python -m task1_mcp_server`. Gateway entry point is `python -m gateway_service`.
- The approved migration file is present and was not modified.
- `.runtime/` is gitignored. Compose mounts one volume there so `gateway.db` and its WAL sidecars share a filesystem.

## Phase 1 choices that are scaffolding, not task behavior

- `GET /health` reports process liveness. It is not one of the four assessment tasks.
- OpenAPI documentation routes are disabled.
- `redirect_slashes` is false so a slash redirect cannot rewrite a client request.
- Gateway access logs are disabled.
- Task 1 serves the official MCP stdio transport. Exit status 0 after stdin closes means the session ended. Argument validation is mapped to JSON-RPC `-32602` with message `Invalid params` because SDK 2.3.0 would otherwise return `isError: true`. The override of private `_handle_call_tool` is isolated in `task1_mcp_server/sdk_compat.py`. `mcp` is pinned to `==2.3.0`.
- Malformed JSON-RPC input on the Task 1 stdio transport produces no response under SDK 2.3.0. That limitation is carried forward. The stdio transport is not reimplemented to change it. The regression test bounds the wait and does not treat silence as an RPC result.
- Refunds do not consult account status or balance. Suspended customers and amounts above the stored balance both complete, and the balance stays unchanged. Concurrent refunds receive distinct `RFND-` ids.
- Task 2 is `POST /mcp`. Authentication failures are HTTP 401. JSON-RPC failures stay in the body with HTTP 200. Notification-only requests are HTTP 204. Denied `admin_` calls are not forwarded.
- Task 3 streaming chat completions require `stream: true`. The default upstream is the deterministic mock. When `GATEWAY_TENANT_TOKENS` is unset, `GATEWAY_LLM_UPSTREAM=openai` uses the primary provider and does not fail over. The finite PII buffer is 320 characters. See `docs/wire_contracts.md`.
- Task 4 admission and failover run on the same completion route when `GATEWAY_TENANT_TOKENS` is set, and always in `GATEWAY_MODE=production`. The approved SQL migration is unchanged. Reported usage is charged in full even when it exceeds the quota. `cl100k_base` ranks are vendored so token estimates do not download during tests. Unresolved `in_flight` and `timed_out` rows stay charged across restart. The 3000 ms failover deadline is the first HTTP response byte.
- `scripts/init_database.py` applies the approved SQL and inserts version 1. That is the migration runner named by the SQL file. It is not the rate limiter.
- The Task 1 image runs as uid 10001. The gateway image starts as root only to chown `/app/.runtime`, then runs the migration and the server as uid 10001.
- Both images install the full runtime lockfile. Unused packages may be present. The other service's source is not copied.
- `charged_tokens` in `001_token_rate_limit.sql` still says an upward reconcile must be capacity-checked. That comment is frozen. Settlement writes the full reported usage, including a total above the quota. See `database/README.md`.

## Known placement difference

The blueprint puts `.cursorrules` at the `ai-field-assessment/` project root. That file is present and byte-identical to the copy that already existed at the Cursor workspace root, `/home/ubuntu/project/.cursorrules`. The workspace copy was not edited or removed, because this Cursor workspace loads it from that path.

## Traceability

| Requirement | Where it is enforced |
| --- | --- |
| Task 1 stdio MCP server, tools `get_customer_record` and `trigger_refund` | `python -m task1_mcp_server`, `task1_mcp_server/` |
| Customer id `CUST-` plus five digits, positive finite refund, reason length, `-32602` | `task1_mcp_server/schemas.py`, `sdk_compat.py` |
| Stdout reserved for MCP frames | `task1_mcp_server/server.py`, `scripts/verify_stdio.py` |
| Task 2 bearer auth and admin-only `admin_` tools | `POST /mcp`, `gateway_service/task2_mcp_gateway/` |
| Mixed batch authorization and no forward of a denied call | `gateway_service/api/mcp_routes.py` |
| Downstream URL must be allowlisted | `gateway_service/task2_mcp_gateway/downstream_proxy.py` |
| Task 3 SSE framing and cross-chunk PII redaction | `POST /v1/chat/completions`, `gateway_service/task3_stream_guardrail/` |
| Tool-call id and type preserved; arguments redacted | `gateway_service/task3_stream_guardrail/pii_redactor.py` |
| Bounded SSE and PII buffers | 262144-byte frames, 320-character hold, 32 active fields |
| Task 4 tenant auth from server-side bearer map | `GATEWAY_TENANT_TOKENS`, required when `GATEWAY_MODE=production` |
| Atomic rolling 60-second quota | `ReservationLedger.admit`, `BEGIN IMMEDIATE` |
| SQLite WAL ledger, no worker migration | `scripts/init_database.py`, read-only production startup check |
| One backup attempt, no failover after a yielded body byte | `gateway_service/task4_model_router/router.py` |
| First-response-byte deadline is header arrival | `OpenAICompatibleProvider.open_attempt` |
| Backup bytes pass through the Task 3 guardrail | `gateway_service/api/completion_routes.py` |
| Sanitized client errors | OpenAI envelope or JSON-RPC error, no credentials, paths, or SQL |
| No plaintext API keys in SQLite | tenant id is SHA-256 of the tenant name; schema has no key column |
| Uncertain `in_flight` and `timed_out` rows stay charged | `ReservationLedger.recover_once` |

## Known limitations

- `GATEWAY_MODE` defaults to `assessment_mock` when the variable is unset. Production deployment sets it explicitly. Compose does that for the gateway service.
- Malformed JSON-RPC on the Task 1 stdio transport produces no response under MCP SDK 2.3.0.
- Refunds do not consult account status or balance. Suspended customers and amounts above the stored balance both complete, and the balance stays unchanged.
- `in_flight` and `timed_out` rows hold quota until the operator procedure in `docs/wire_contracts.md`. A kill that skips shutdown can leave a row in that state. Three failed ledger writes do the same.
- PII detection is bounded: email and card matches stop at the documented lengths. Usage metadata is not scanned as generated text.
- WAL is local disk. It is not a shared-network filesystem.
- The first-response-byte deadline is wall-clock time around header arrival. Scripted test doubles measure their first yielded body byte because they have no header channel.
- A connection failure before any body byte can still use the one backup attempt. A body read timeout after headers cannot.
- The frozen SQL comment described above does not match the runtime charge. The migration file stays as approved.
