# AI Field Developer Assessment

Stdio MCP server and an integrated FastAPI gateway. Tasks 1 through 4 are implemented. See [docs/assessment_compliance.md](docs/assessment_compliance.md).

## Layout

- `task1_mcp_server/` — Task 1, executable with `python -m task1_mcp_server`
- `gateway_service/` — Tasks 2, 3, and 4, executable with `python -m gateway_service`
- `database/migrations/001_token_rate_limit.sql` — approved SQLite ledger
- `tests/` — task tests and integration tests

Task 1 must not import the gateway.

## Setup

Requires Python 3.12 or newer and [uv](https://docs.astral.sh/uv/). From this directory:

```bash
uv python install 3.12
uv sync --group dev --python 3.12
```

The process reads environment variables. It does not load `.env` by itself. `.env.example` lists the supported names.

## Local startup

Run these from this directory so `.runtime/gateway.db` resolves beside the project.

```bash
uv run python scripts/init_database.py
uv run python -m task1_mcp_server
uv run python -m gateway_service --check
uv run python -m gateway_service
```

`init_database.py` creates `.runtime/gateway.db` and is the only schema migration. The gateway does not create or alter tables on startup. `python -m task1_mcp_server` speaks MCP on stdin and stdout until stdin closes. Diagnostics go to stderr. `python -m gateway_service` serves `GET /health`, `POST /mcp`, and `POST /v1/chat/completions` on `127.0.0.1:8080`.

The ASGI factory is the same application:

```bash
uv run uvicorn gateway_service.main:create_app --factory --host 127.0.0.1 --port 8080
```

Its lifespan runs the same production readiness check before the first request.

## Production startup

Set `GATEWAY_MODE=production` explicitly. Also set tenant tokens, `GATEWAY_LLM_UPSTREAM=openai`, both provider URLs, both provider API keys, and run `init_database.py` first. Either the executable or the ASGI factory then exits before serving if any of those are missing. That failure does not select `assessment_mock`.

```bash
export GATEWAY_MODE=production
export GATEWAY_LLM_UPSTREAM=openai
export GATEWAY_TENANT_TOKENS='alpha=replace-with-tenant-bearer'
export GATEWAY_PRIMARY_PROVIDER_BASE_URL='https://primary.example'
export GATEWAY_BACKUP_PROVIDER_BASE_URL='https://backup.example'
export GATEWAY_PRIMARY_PROVIDER_API_KEY='replace-with-primary-key'
export GATEWAY_BACKUP_PROVIDER_API_KEY='replace-with-backup-key'
uv run python scripts/init_database.py
uv run python -m gateway_service --check
uv run python -m gateway_service
```

`--check` prints the redacted configuration to stderr and does not bind a port. Provider keys and tenant bearers are omitted from that view.

## Environment

The process reads the environment. It does not load `.env`. Names and safe local defaults are in `.env.example`.

| Variable | Role |
| --- | --- |
| `GATEWAY_MODE` | `assessment_mock` (default) or `production` |
| `GATEWAY_HOST`, `GATEWAY_PORT` | Bind address. Compose uses `0.0.0.0` and `8080` |
| `GATEWAY_DATABASE_PATH` | Ledger path. Default `.runtime/gateway.db` |
| `GATEWAY_SQLITE_BUSY_TIMEOUT_MS` | Per-connection SQLite busy timeout. Default `5000` |
| `GATEWAY_DEFAULT_TOKENS_PER_WINDOW` | Quota. Default `50000` |
| `GATEWAY_WINDOW_MS` | Rolling window. Default `60000` |
| `GATEWAY_FIRST_BYTE_TIMEOUT_MS` | First HTTP response-byte deadline. Default `3000` |
| `GATEWAY_DEFAULT_MAX_OUTPUT_TOKENS` | Reserved output budget when the request omits one. Default `256` |
| `GATEWAY_MAX_OUTPUT_TOKEN_BUDGET` | Ceiling for that budget. Default `4096` |
| `GATEWAY_LOG_LEVEL` | Stderr level. Default `INFO` |
| `GATEWAY_TENANT_TOKENS` | `name=bearer,name=bearer`. Required in production |
| `GATEWAY_LLM_UPSTREAM` | `mock` or `openai`. Production requires `openai` |
| `GATEWAY_PRIMARY_PROVIDER_BASE_URL` | Primary OpenAI-compatible origin |
| `GATEWAY_BACKUP_PROVIDER_BASE_URL` | The single backup origin |
| `GATEWAY_PRIMARY_PROVIDER_API_KEY` | Primary bearer. Never logged or stored in SQLite |
| `GATEWAY_BACKUP_PROVIDER_API_KEY` | Backup bearer. Never logged or stored in SQLite |
| `GATEWAY_DOWNSTREAM_MCP_URL` | Task 2 downstream POST target |
| `GATEWAY_DOWNSTREAM_MCP_ALLOWLIST` | Exact URLs that may be that target |
| `GATEWAY_DOWNSTREAM_TIMEOUT_MS` | Downstream HTTP timeout. Default `3000` |
| `GATEWAY_MAX_JSON_BODY_BYTES` | Request body limit. Default `1048576` |
| `GATEWAY_MAX_BATCH_ITEMS` | JSON-RPC batch limit. Default `32` |

Endpoint contracts, the first-response-byte rule, quota accounting, and the operator recovery procedure are in [docs/wire_contracts.md](docs/wire_contracts.md).

## Offline checks

```bash
uv run python scripts/verify_stdio.py
uv run ruff check .
uv run ruff format --check .
uv run python scripts/run_offline_tests.py
```

These commands do not call a live model provider and do not need provider credentials.

## Containers

Export the production variables above, then:

```bash
docker compose up --build
```

`migrate` applies the SQL file on the `gateway-runtime` volume and exits. `gateway` starts after that migration and after `downstream` is healthy. It listens on port 8080 and uses `GATEWAY_MODE=production`. `downstream` is the HTTP JSON-RPC mock at `http://downstream:9000/mcp`. Compose sets `GATEWAY_DOWNSTREAM_MCP_URL` and `GATEWAY_DOWNSTREAM_MCP_ALLOWLIST` to that same address. The mock port is not published, and the gateway does not attach to `task1`. `task1` has no published port; attach to its stdin for MCP. The gateway image entrypoint gives `/app/.runtime` to uid 10001 and runs the process as user `gateway`. The Task 1 image runs as that same user from the start. Compose health for the gateway is `GET /health`.

`docker compose config` renders the file. `docker compose build` builds both images. Runtime smoke tests require a Docker daemon and the production variables.

Streaming acceptance uses a second project and does not change this file's production settings:

```bash
docker compose -p acceptance -f compose.yaml -f compose.acceptance.yaml up --build
```

That overlay keeps `GATEWAY_MODE=production` and a required tenant bearer. It sends chat completions to the in-network mocks `llm-primary` and `llm-backup` on port 8081. The results are recorded in `docs/acceptance_verification.md`.

## Scope

Implemented: the Phase 1 workspace, the Task 1 MCP server (`get_customer_record`, `trigger_refund`), the Task 2 MCP security gateway (`POST /mcp`), the Task 3 streaming PII guardrail (`POST /v1/chat/completions`), and Task 4 token admission with one primary/backup failover. `GATEWAY_MODE=assessment_mock` is the process default. In that mode, tenant admission is active when `GATEWAY_TENANT_TOKENS` is set, and an unset map keeps the Task 3 completion contract. Requirement traceability and known limitations are in [docs/assessment_compliance.md](docs/assessment_compliance.md).
