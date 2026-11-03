# Acceptance verification

The four tasks are implemented. Task 2 is frozen. This run checked Task 3 and Task 4 through the Docker gateway with deterministic in-network mocks. The production Compose file was not switched to those mocks.

## How to repeat

From the project directory, with a running container engine:

```bash
docker compose -p acceptance -f compose.yaml -f compose.acceptance.yaml up --build
```

The gateway is published on host port **8081**. The production project remains on port 8080. `compose.acceptance.yaml` is not an automatic override, so `docker compose up` alone still uses the production provider settings.

The overlay keeps `GATEWAY_MODE=production` and requires bearer `acceptance-token-value`. Chat traffic goes to `http://llm-primary:9100` and `http://llm-backup:9100`. The quota for this project is 11 tokens, with a default output budget of 4, so two copies of the scripted request cannot both be admitted. Those values are acceptance settings. They are not the production defaults of 50,000 and 256.

## What was observed

Unauthenticated `POST /v1/chat/completions` returned HTTP 401 `invalid_api_key`.

An authenticated stream for model `mock-email-parts` returned HTTP 200. The backup mock emits the address as five content deltas: `alex`, `@`, `example`, `.`, `com`. The gateway returned one content delta, `[REDACTED]`, then `data: [DONE]`. The response did not contain `alex`, `@`, or `example.com`.

`llm-primary` logged one `status=429`. `llm-backup` logged one `status=200`. That is the single backup attempt.

A second authenticated request returned HTTP 429 `quota_exceeded` and did not add a provider log line.

The ledger row before the gateway container was restarted was:

`('settled', 6, 1, 5)`

After `docker compose -p acceptance restart gateway`, the same row was still there. Another completion stayed `quota_exceeded`, and the provider logs stayed at one primary response and one backup response. The database file did not contain `acceptance-token-value` or `acceptance-primary-key`.

## Offline regression

`uv run ruff check .` passed. `uv run ruff format --check .` passed. `uv run python scripts/run_offline_tests.py` reported **173 passed**. `uv run python scripts/verify_stdio.py` passed.

## Remaining limitations

- Unset `GATEWAY_MODE` still means `assessment_mock`. Production and this acceptance overlay both set `production` explicitly.
- The production Compose service still expects real provider URLs and keys. Placeholder endpoints are not valid streaming targets. Use this overlay for deterministic provider checks.
- `in_flight` and `timed_out` rows hold quota until the operator procedure in `docs/wire_contracts.md`.
- The approved SQL comment still says an upward reconcile must be capacity-checked. Settlement stores the full reported total. The migration file is unchanged.
- Malformed JSON-RPC on the Task 1 stdio transport produces no response under MCP SDK 2.3.0. Refunds do not consult balance or account status.
- PII detection stops at the lengths in `docs/wire_contracts.md`. Usage metadata is not scanned as generated text.
- The 3000 ms failover deadline is response-header arrival. A body timeout after headers does not open the backup.
- WAL is local disk.
