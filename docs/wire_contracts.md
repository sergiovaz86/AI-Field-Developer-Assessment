# Wire contracts

Tasks 1 through 4 are implemented. This note is the wire contract for those tasks.

## Fixed

| Item | Contract |
| --- | --- |
| Task 1 tools | `get_customer_record`, `trigger_refund` |
| Customer id | `CUST-` plus exactly five ASCII digits |
| Refund amount | Finite, strictly positive number. Booleans, NaN, and infinity are rejected |
| Refund reason | String of at least 10 characters |
| Tool models | Pydantic v2, `extra="forbid"`, no silent type coercion |
| Structural validation code | JSON-RPC `-32602` with message `Invalid params` |
| Denied tool call | JSON-RPC `-32001` with message `Unauthorized Tool Call` |
| Example bearers | `admin-token` → `admin`, `viewer-token` → `viewer` |
| Admin tools | `tools/call` whose `params.name` starts with `admin_` |
| Notifications | A valid notification produces no JSON-RPC response |
| Batches | Each element is authorized on its own |
| JSON-RPC ids | Booleans are not numeric ids |
| Redaction replacement | `[REDACTED]` |
| PII classes | Email addresses, US Social Security numbers, Luhn-valid credit card numbers |
| Token encoding | tiktoken `cl100k_base` |
| Default quota | 50,000 tokens per tenant per 60,000 milliseconds |
| Admission clock | Integer Unix milliseconds |
| Admission transaction | `BEGIN IMMEDIATE` around the read-check-write |
| Failover | HTTP 429, HTTP 5xx, network failure, or first-byte timeout |
| First-byte deadline | 3000 milliseconds, applied separately to the single backup attempt |
| Database | SQLite WAL file at `.runtime/gateway.db` |
| Gateway liveness | `GET /health` returns `{"status":"ok"}` |
| MCP gateway | `POST /mcp` |
| Gateway authentication | HTTP 401 body `{"error":"unauthorized","message":"Authentication required"}`. Not a JSON-RPC envelope |
| Notification-only HTTP | HTTP 204 with an empty body |
| JSON-RPC protocol errors | HTTP 200 with the JSON-RPC error object |

## Task 1 tools

`get_customer_record` arguments are `{customer_id}`. `customer_id` matches `^CUST-[0-9]{5}$`.

A known id returns `customer_id`, `full_name`, `email`, `status` (`active` or `suspended`), `account_balance_cents`, and `refund_count`.

`trigger_refund` arguments are `{customer_id, amount, reason}`. `amount` is a finite JSON number strictly greater than zero. `reason` is a string of at least 10 characters and is stored unchanged. A successful refund returns `refund_id` (`RFND-` plus six digits), the customer id, amount, reason, `status: completed`, `prior_refund_count`, and `refund_count`.

Ids start at `RFND-000001` for each process. Only a refund for a known customer allocates an id and increments that customer's `refund_count`. Concurrent refunds take distinct ids under the store lock. The same arguments on a later call allocate the next id.

Account status and account balance do not gate a refund. A suspended customer can be refunded, and a refund may be larger than `account_balance_cents`. Neither field changes. No financial rule was specified for either case.

An unknown but well-formed customer id is a tool execution error (`isError: true`) whose text contains `Unknown customer_id`. It is not `-32602`.

SDK 2.3.0 would report argument `ValidationError` as `isError: true`. The only override of the private `MCPServer._handle_call_tool` is `sdk_compat.InvalidParamsDispatch`, and `mcp` is pinned to `2.3.0`. That override raises `MCPError(-32602, "Invalid params")`. The message is the JSON-RPC 2.0 text for that code. The architecture label "Invalid Params" names the same code. The error object does not echo the rejected values. Wire tests keep this boundary under the pin.

Messages that fail the SDK's JSON-RPC adapter, including malformed JSON, an object with no method, and a boolean id, produce no response. That is a protocol-compliance limitation of the pinned SDK. It is not corrected by replacing the stdio transport. A test treats that silence as unanswered input: it waits at most 0.2 seconds with `select` and does not read the next successful frame as the malformed request's result. The next well-formed request still runs.

## Task 2 gateway

`POST /mcp` accepts one JSON-RPC 2.0 object or a non-empty batch. The gateway does not connect to Task 1 stdio. It POSTs permitted calls with `httpx.AsyncClient` to `GATEWAY_DOWNSTREAM_MCP_URL` only when that exact URL is also listed in `GATEWAY_DOWNSTREAM_MCP_ALLOWLIST`. Inbound `Authorization` is not forwarded.

Authentication runs before JSON parsing. Missing, malformed, and unknown bearer tokens return HTTP 401. The only accepted tokens are `admin-token` and `viewer-token`. A role in the JSON body is ignored.

`tools/list` is forwarded for both roles and is not filtered. `tools/call` requires an object `params` with a string `name` and, when present, an object `arguments`. Any other parameter key is invalid params (`-32602`) and is not authorized or forwarded. A name beginning with `admin_` requires the admin role. A denied call returns `-32001` / `Unauthorized Tool Call` and is not forwarded. Other methods are forwarded after the envelope is valid.

JSON-RPC errors use HTTP 200. Codes are `-32700` Parse error, `-32600` Invalid Request, `-32602` Invalid params, `-32603` Internal error, and `-32001`. A string id, including `""`, an integer id, and JSON `null` are valid. Omitting `id` is a notification. Booleans are not numeric ids and are not coerced to `0` or `1`. Fractional JSON numbers, including `1.0`, are rejected rather than truncated; that is stricter than the JSON-RPC recommendation that numbers should not have fractional parts. When an id cannot be used, the error id is null. When it can, the error echoes it. An empty batch and a batch larger than `GATEWAY_MAX_BATCH_ITEMS` are one invalid-request object, not a partial batch.

A valid notification produces no JSON-RPC response. A notification-only body returns HTTP 204. In a batch, notifications are omitted from the response array. The remaining responses stay in original request order. Denied notifications are not forwarded and still produce no response.

Duplicate request ids are processed independently. Those calls are posted one at a time and the responses stay in request order. When every forwarded request id is unique, the gateway sends one downstream batch and reorders the downstream array by id. The mock returns batch responses in reverse order so that reordering is observable.

Downstream timeouts, connection failures, unexpected HTTP statuses, non-JSON bodies, and envelopes that do not match the request id become `-32603` / `Internal error`. A downstream batch is rejected as a whole when any element is malformed or when the response ids are duplicated, missing, mismatched, or unexpected. The client body does not include the upstream URL, status text, or payload. A well-formed downstream JSON-RPC result or error is preserved, including error `data`. The proxy does not retry. A downstream failure for a notification-only request still returns HTTP 204 and does not echo the upstream body.

## Task 3 chat completions

`POST /v1/chat/completions` accepts an OpenAI-compatible JSON body. `stream` must be JSON `true`. Any other value, including omission, returns HTTP 400 `{"error":{"message":"stream must be true","type":"invalid_request_error","code":"stream_required"}}`. Non-streaming completions are not assembled, so they cannot skip redaction. Invalid JSON is HTTP 400 `invalid_json`. A body that fails the request model is HTTP 400 `invalid_request`. A body larger than `GATEWAY_MAX_JSON_BODY_BYTES` is HTTP 400 `request_too_large`.

A successful stream uses `Content-Type: text/event-stream`. Each event is `data: {json}\n\n`. The gateway normalizes LF and CRLF upstream frames to LF. A successful stream ends with `data: [DONE]\n\n`. The mock provider is the default (`GATEWAY_LLM_UPSTREAM=mock`, or the default when that variable is unset). `openai` posts to `{GATEWAY_PRIMARY_PROVIDER_BASE_URL}/chat/completions` with `GATEWAY_PRIMARY_PROVIDER_API_KEY`. Offline tests never call a real provider. Failover is described under Task 4 and runs only when tenant credentials are configured.

The default mock model returns one plain sentence. `mock-chars` emits the last user message one character at a time. `mock-email-parts` emits `alex`, `@`, `example`, `.`, `com` as separate content deltas. Other `mock-*` names are fixed scripts in `gateway_service/mocks/stream_scenarios.py`.

Generated text is redacted to `[REDACTED]` for three classes only:

- Email: at most 320 characters. The local part is 1 to 64 characters from `A-Za-z0-9._%+-` and starts with an alphanumeric. The domain is dot-separated labels with a final alphabetic label of 2 to 24 characters.
- US SSN: exactly `ddd-dd-dddd`. Area `000`, `666`, and `900-999`, group `00`, and serial `0000` are not matches. Nine digits without dashes are not an SSN.
- Payment card: a maximal span of 13 to 19 digits, at most 37 characters, with optional single spaces or hyphens between digits, and a valid Luhn check. A shorter Luhn-valid window inside a longer digit run is not a match. A run longer than 19 digits is not a match.

The redactor withholds the longest suffix that can still grow into one of those matches. The withheld suffix is at most 320 characters and does not depend on HTTP chunk size. At most 32 generated fields are buffered at once. A 33rd field, a non-integer tool or choice index, or a `choices` value that is not an array fails the stream closed. An incomplete SSE frame, including its data lines, is capped at 262144 bytes. The UTF-8 decoder holds at most one incomplete code point. Upstream reads are processed in slices of that same size. An unterminated or oversized frame is a sanitized upstream failure, and the parser drops the partial frame. A candidate that is still viable at 320 characters and then grows is replaced with `[REDACTED]` instead of being emitted. Text that cannot extend a match is emitted immediately. At finish, usage, or `[DONE]`, the remainder is redacted and emitted in a content delta before the stream ends. Ordinary words that look like an email local part are held until the next character that cannot extend them, usually a space. That is the safety-window latency. It is not zero, and it does not detect matches outside the lengths above.

`role`, tool-call `type`, and tool-call `id` are copied unchanged. `content`, `refusal`, legacy `function_call` name and arguments, and `tool_calls[].function` name and arguments are redacted. Any other delta field, including audio or other nested generated text, is removed and is not forwarded. Each field has its own buffer.

Before the first SSE byte, upstream failure is HTTP 502 `The upstream request failed.` / `upstream_failure`, and a timeout is HTTP 504 `The upstream request timed out.` / `upstream_timeout`. Both use `type` `server_error`. After the first byte, the status stays 200 and the stream ends with one `data: {"error":{"message":"The upstream stream failed.","type":"server_error","code":"upstream_stream_failure"}}` event. `[DONE]` is not sent after that error. Error bodies do not include exception text, upstream URLs, credentials, or stack traces. `x-correlation-id` is a response header. Completion error objects also include `correlation_id`.

## Task 4 tenant quota and failover

`GATEWAY_MODE` selects the gateway operating mode. The process default, used when the variable is unset, is `assessment_mock`. In that mode, chat completions stay on the Task 3 path when `GATEWAY_TENANT_TOKENS` is unset: no bearer check, no quota, and no backup attempt. `production` is explicit. `python -m gateway_service` and the ASGI lifespan of `create_app` both refuse to serve when production is missing tenant bearers, `GATEWAY_LLM_UPSTREAM=openai`, either provider URL, either provider API key, or schema version 1. The database check is read-only. It does not create the file or apply the migration. A missing requirement is logged as `gateway configuration is invalid` with the field name, and the process does not bind a port. An incomplete production configuration is not served as `assessment_mock`. Compose sets `GATEWAY_MODE=production` on the gateway service. Production never serves completions with authentication or quota enforcement turned off.

`GATEWAY_TENANT_TOKENS` is `name=bearer,name=bearer`. When it is set, in either mode, a missing or unknown bearer is HTTP 401 `authentication_error` / `invalid_api_key` / `Authentication required` before the body is read. Task 2's `admin-token` and `viewer-token` are not tenant credentials, and a tenant bearer does not authenticate `POST /mcp`.

The stored tenant id is the hex SHA-256 of the tenant name. The bearer is not written to SQLite and is omitted from `redacted_view`.

Input tokens are `cl100k_base` over the model, roles, and message text. The ranks are the vendored file `gateway_service/task4_model_router/data/cl100k_base.tiktoken`, so a test run does not download them. The output budget is `max_completion_tokens` or `max_tokens` when that value is a positive integer, otherwise `GATEWAY_DEFAULT_MAX_OUTPUT_TOKENS` (256). Values above `GATEWAY_MAX_OUTPUT_TOKEN_BUDGET` (4096) are clamped. A boolean, zero, or non-integer limit is HTTP 400 `invalid_request` and is not reserved. The same budget is sent upstream as `max_tokens`. The reservation is `estimated_input_tokens + output_budget` and is inserted before any provider call.

Admission uses one `BEGIN IMMEDIATE` transaction. The charged sum is:

```sql
SELECT COALESCE(SUM(charged_tokens), 0)
FROM token_reservations
WHERE tenant_id = ?
  AND (
        admitted_at_ms > ?
        OR status IN ('reserved', 'in_flight', 'timed_out')
      )
```

The cutoff is `now_ms - window_ms`. A row admitted exactly `window_ms` earlier is outside the window. An active row is still charged after the window. A row that meets both conditions is counted once. The default quota is 50,000 tokens per 60,000 milliseconds. Exceeding it is HTTP 429 `rate_limit_error` / `quota_exceeded` and calls neither provider. That code is distinct from an upstream HTTP 429.

Each ledger call opens its own connection, sets `PRAGMA foreign_keys = ON`, `PRAGMA busy_timeout` from configuration, and `PRAGMA journal_mode = WAL`, then closes the connection. The gateway does not migrate. A missing schema version 1 is HTTP 503 `database_unavailable`. A lock that outlasts the busy timeout uses the same code. Neither response includes the database path or SQL.

A streamed request is inserted as `in_flight` in the admission transaction, before the provider socket opens, so a concurrent recovery cannot release it. `reserved` means that in-flight commit never happened and no provider attempt has started. One logical request, including its backup attempt, has one `request_id`. Reported `prompt_tokens` and `completion_tokens` replace `charged_tokens` with their full sum in the same transaction, including when that sum is above the tenant quota. The actual columns store the report. The approved migration file still contains the older comment that an upward reconcile must be capacity-checked. That comment is frozen with the file and is superseded by this runtime contract: the settlement transaction writes the entire reported sum and does not cap it at the remaining quota. Further admissions for that tenant fail while the rolling total is over the quota. Another tenant's quota is unchanged. A second settle does not change a terminal row. A completed stream with no usage object stays charged at the full reservation and leaves the actual columns null. A failed stream with no usage is `released` and charged 0 after teardown. A settled charge is not zeroed by a later release.

Restart recovery releases only `reserved` rows, setting `charged_tokens` to 0 and `finalized_at_ms`. It leaves `in_flight` and `timed_out` charged, including after the rolling window, because those rows may already have an upstream attempt whose usage is unknown. Workers do not auto-release those rows.

Cancellation and shutdown write the ledger from a detached task. The row is marked finished only after that write commits. A ledger error is retried three times. If every attempt fails, the row stays `in_flight` or `timed_out` and the log records the correlation id only. Process shutdown waits for those detached writes. A kill that skips shutdown can leave the row active; the charge remains until the operator procedure below.

Operator reconciliation, with every gateway worker stopped:

1. Inspect `token_reservations` for the tenant.
2. Release a `reserved` row by setting `status` to `released`, `charged_tokens` to 0, and `updated_at_ms` and `finalized_at_ms` to the same integer millisecond timestamp. That row never opened a provider socket.
3. Leave `in_flight` and `timed_out` unchanged unless provider usage is known or teardown was confirmed with no usage.
4. When usage is known, set `status` to `settled`, `charged_tokens` to `prompt_tokens + completion_tokens`, store both actual columns, and set both timestamps. Record the full sum even when it exceeds the quota.
5. When teardown was confirmed and no usage was reported, release the row as in step 2.
6. Start the workers again. They do not release the rows left active in steps 3 through 5.

The migration comment on `charged_tokens` says an upward reconcile may happen only through a capacity-checked transaction. The file is unchanged. The running ledger does not apply that ceiling. `database/README.md` records the same distinction. WAL is a local-disk journal and is not a shared-network filesystem.

### First response byte and later body bytes

`GATEWAY_FIRST_BYTE_TIMEOUT_MS` measures the first HTTP response byte on the OpenAI-compatible adapter. That byte is the status line and response headers becoming available from `httpx`. The wait is `asyncio.wait_for` around header arrival, and the client connect and pool timeouts use the same 3000 ms budget. A slow body, a partial SSE frame, and a delayed later chunk are outside that budget. After headers, the read timeout is 30 seconds. A read timeout after headers does not open the backup. A header timeout, HTTP 429, HTTP 5xx, or a connection failure before any body byte can open the one backup attempt. A scripted test double has no header channel, so the same deadline wraps its first yielded body byte.

Eligible primary failures are HTTP 429 (`http_429`), HTTP 5xx (`http_5xx`), connection errors (`network`), and a first-response-byte timeout (`timeout`). Other HTTP 4xx responses and protocol errors stop the request. Exactly one backup attempt is allowed, and only before any body byte is yielded toward the guardrail. A first-response-byte timeout is marked `timed_out`, which keeps the charge until the attempt is closed. After any body byte is yielded, a later upstream failure is the Task 3 mid-stream SSE error and does not open the backup. Both attempts share the Task 3 guardrail. Client cancellation closes the upstream iterator and releases the reservation when no usage was observed. That finalize runs on a task that is not tied to the cancelled request, so a client disconnect can still commit the release.

Client-owned MCP request models use `extra="forbid"`. The chat-completion body allows extra provider fields because the OpenAI protocol is extensible; generated text fields outside the list above are dropped rather than forwarded.
