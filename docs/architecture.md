# Architecture

Tasks 1 through 4 are implemented.

## Processes

| Process | Entry point | Transport |
| --- | --- | --- |
| Task 1 MCP server | `python -m task1_mcp_server` | stdin/stdout JSON-RPC |
| Gateway | `python -m gateway_service` | HTTP |

Task 1 is a stdio program. It is not an HTTP service, and the gateway does not attach to its standard streams. Task 2 reaches a downstream MCP server over HTTP JSON-RPC at `POST /mcp`. Task 3 serves `POST /v1/chat/completions` and redacts the stream in the gateway process. Task 4 adds tenant admission and one backup attempt on that same route. `GATEWAY_MODE=assessment_mock` applies admission when tenant credentials are configured. `GATEWAY_MODE=production` requires those credentials, both providers, and an initialized ledger before the process binds a port. The provider adapter itself does not retry.

`python -m gateway_service --check` validates configuration and does not bind a port. A normal start serves `GET /health`, `POST /mcp`, and `POST /v1/chat/completions`. The same production check runs in the ASGI lifespan before requests, so `uvicorn gateway_service.main:create_app --factory` has the same readiness rule as the executable.

## Packages

- `task1_mcp_server/` is isolated. It must not import `gateway_service`.
- `gateway_service/` contains Tasks 2, 3, and 4, provider adapters, and gateway-only shared code.
- `gateway_service/shared/` is the gateway-internal shared package.
- `gateway_service/providers/` is separate from routing and guardrails.
- `database/migrations/` holds explicit SQL. Request handling must not create the schema.
- `tests/task1` through `tests/task4` are task tests. `tests/integration/` is for cross-service flows.

Dependency direction inside the gateway is task modules and adapters toward `gateway_service.shared`. Shared code does not import task modules. Task 1 duplicates the small stderr logging setup rather than importing the gateway logger.

## Persistence

The default database path is `.runtime/gateway.db`, relative to the process working directory. Run commands from the project root. In Compose, the working directory is `/app` and a named volume is mounted at `/app/.runtime`, so the database and its WAL sidecars stay on one filesystem.

`scripts/init_database.py` is the only component that creates the schema. Assessment startup does not open the database. Production startup, both in `python -m gateway_service` and in the ASGI lifespan, checks schema version 1 through a read-only connection and does not migrate or create the file. A completion with tenant credentials opens its own SQLite connection per ledger operation. Shutdown waits for detached reservation writes. The operator procedure for uncertain rows is in `docs/wire_contracts.md`. The migration comment that limits upward reconciliation is superseded by the runtime contract in that same note.

## Containers

`Dockerfile.task1` copies `task1_mcp_server` and not the gateway. It runs as user `gateway` (uid 10001) and publishes no port. `Dockerfile.gateway` copies the gateway, the migration, and the scripts, and not Task 1. Its entrypoint starts as root, gives `/app/.runtime` to uid 10001, and executes the migration or the gateway as that user. Both images install the shared lockfile. The gateway image therefore contains the MCP SDK as an unused dependency. The image filesystems still exclude the other service's source.

Compose service `task1` publishes no ports and keeps stdin open for the stdio server. Service `migrate` runs the migration once. Service `downstream` runs the existing JSON-RPC mock on port 9000 inside the Compose network and does not publish that port. Service `gateway` sets `GATEWAY_MODE=production`, listens on port 8080, and starts after the migration exits and `downstream` is healthy. Both `GATEWAY_DOWNSTREAM_MCP_URL` and `GATEWAY_DOWNSTREAM_MCP_ALLOWLIST` are `http://downstream:9000/mcp`. The gateway does not attach to Task 1. Its health check is `GET /health`. Working directories are `/app`, so `.runtime/gateway.db` resolves on the named volume.
