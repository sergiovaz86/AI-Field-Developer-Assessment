# Token reservation ledger

SQLite database for Task 4 quota admission and usage reconciliation. The gateway reads this schema. It does not create or alter tables during request handling.

## Apply

From the project root:

```bash
python scripts/init_database.py
```

The default path is `.runtime/gateway.db`. `GATEWAY_DATABASE_PATH` selects another path. `--database` overrides that variable for one invocation. `--now-ms` supplies the integer Unix millisecond timestamp stored on the version row.

The runner executes `database/migrations/001_token_rate_limit.sql` and then inserts schema version `1`. A second run leaves the existing version row in place. Do not run the runner from more than one process at a time.

## Invariants

- Tenant identity is `tenant_id`. The schema has no column for a raw API key.
- Admission timestamps and token counts are integers. The rolling window uses an exclusive lower bound on `admitted_at_ms`.
- `request_id` is unique, so a primary attempt and its backup attempt share one logical reservation.
- `charged_tokens` is what quota queries sum. Reconciliation stores the full reported input plus output, including a total above the quota. A `released` row must charge zero. `settled` and `released` rows require `finalized_at_ms`. Active statuses must leave `finalized_at_ms` null.
- `reserved` means no provider attempt started. Restart recovery may release those rows. `in_flight` and `timed_out` stay charged until the operator procedure in `docs/wire_contracts.md`.
- Active rows stay in the ledger after their admission timestamp leaves the 60-second window. Cleanup is limited to finalized rows.
- The migration enables WAL. `busy_timeout` is connection-local and is set to 5000 milliseconds by the migration script. Every later worker connection must set `foreign_keys` and `busy_timeout` itself. WAL does not remove SQLite's single-writer constraint.
- WAL sidecar files (`gateway.db-wal`, `gateway.db-shm`) must stay on the same filesystem as `gateway.db`. Docker mounts one volume at `/app/.runtime` for that reason.

## Frozen comment on upward reconciliation

`001_token_rate_limit.sql` says `charged_tokens` may be reconciled upward only through an explicitly capacity-checked transaction. That sentence is historical. The migration file is approved and is not edited to revise the comment. The runtime settlement transaction writes `prompt_tokens + completion_tokens` in full when usage is known, including when the sum exceeds the tenant quota. Later admissions for that tenant stay closed while the rolling total is over the quota. The schema `CHECK` still requires `charged_tokens >= 0` and does not encode a quota ceiling.

## Change control

Do not edit `001_token_rate_limit.sql` in place after it has been applied. Add a new numbered migration instead. Phase 6 does not add a second migration.
