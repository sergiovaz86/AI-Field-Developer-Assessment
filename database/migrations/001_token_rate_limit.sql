
-- ============================================================
-- Migration: 001_token_rate_limit.sql
-- Engine: SQLite 3
-- Purpose: Durable, tenant-scoped token reservation ledger
-- Time unit: Unix epoch milliseconds (INTEGER)
-- ============================================================

PRAGMA foreign_keys = ON;

-- WAL mode is a database-level configuration.
-- Apply outside an explicit transaction.
PRAGMA journal_mode = WAL;

-- Set for each database connection.
PRAGMA busy_timeout = 5000;

-- NORMAL provides the usual WAL performance/durability tradeoff.
-- Use FULL if stronger power-loss durability is required.
PRAGMA synchronous = NORMAL;

BEGIN IMMEDIATE;

-- ------------------------------------------------------------
-- 1. Tenant quota configuration
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tenant_quotas (
    tenant_id TEXT PRIMARY KEY NOT NULL,

    -- Maximum combined input + output tokens.
    tokens_per_window INTEGER NOT NULL DEFAULT 50000
        CHECK (tokens_per_window > 0),

    -- Rolling window duration in milliseconds.
    window_ms INTEGER NOT NULL DEFAULT 60000
        CHECK (window_ms > 0),

    -- Creation and modification timestamps.
    created_at_ms INTEGER NOT NULL
        CHECK (created_at_ms >= 0),

    updated_at_ms INTEGER NOT NULL
        CHECK (updated_at_ms >= created_at_ms),

    -- Never store plaintext API keys.
    CHECK (length(tenant_id) > 0)
);

-- ------------------------------------------------------------
-- 2. Atomic reservation and accounting ledger
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS token_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    tenant_id TEXT NOT NULL,

    -- Globally unique logical request identifier.
    -- Primary and backup attempts reuse this identifier.
    request_id TEXT NOT NULL UNIQUE,

    -- Correlation ID for structured logging and errors.
    correlation_id TEXT NOT NULL,

    -- Admission-time reservation.
    estimated_input_tokens INTEGER NOT NULL
        CHECK (estimated_input_tokens >= 0),

    reserved_output_tokens INTEGER NOT NULL
        CHECK (reserved_output_tokens >= 0),

    reserved_tokens INTEGER GENERATED ALWAYS AS (
        estimated_input_tokens + reserved_output_tokens
    ) STORED,

    -- Charged amount used by quota queries.
    -- Initially equals the reservation budget.
    -- Reconciled downward after actual usage is known.
    -- May only be reconciled upward through an
    -- explicitly capacity-checked transaction.
    charged_tokens INTEGER NOT NULL
        CHECK (charged_tokens >= 0),

    -- Final provider usage; NULL until known.
    actual_input_tokens INTEGER
        CHECK (
            actual_input_tokens IS NULL
            OR actual_input_tokens >= 0
        ),

    actual_output_tokens INTEGER
        CHECK (
            actual_output_tokens IS NULL
            OR actual_output_tokens >= 0
        ),

    -- Reservation lifecycle.
    status TEXT NOT NULL DEFAULT 'reserved'
        CHECK (
            status IN (
                'reserved',
                'in_flight',
                'timed_out',
                'settled',
                'released'
            )
        ),

    -- Timestamp at which this request entered the
    -- rolling admission window.
    admitted_at_ms INTEGER NOT NULL
        CHECK (admitted_at_ms >= 0),

    -- Last accounting modification.
    updated_at_ms INTEGER NOT NULL
        CHECK (updated_at_ms >= admitted_at_ms),

    -- Set only when request accounting is finalized.
    finalized_at_ms INTEGER
        CHECK (
            finalized_at_ms IS NULL
            OR finalized_at_ms >= admitted_at_ms
        ),

    -- Charge of a fully released request is zero.
    CHECK (
        status <> 'released'
        OR charged_tokens = 0
    ),

    CHECK (
        status NOT IN ('settled', 'released')
        OR finalized_at_ms IS NOT NULL
    ),

    CHECK (
        status NOT IN ('reserved', 'in_flight', 'timed_out')
        OR finalized_at_ms IS NULL
    ),

    FOREIGN KEY (tenant_id)
        REFERENCES tenant_quotas(tenant_id)
        ON DELETE RESTRICT
);

-- ------------------------------------------------------------
-- 3. Rolling window index
-- ------------------------------------------------------------

-- Supports:
-- WHERE tenant_id = ?
-- AND admitted_at_ms > ?
--
-- Covers the charged_tokens column used in SUM().
CREATE INDEX IF NOT EXISTS
    idx_reservations_tenant_window
ON token_reservations (
    tenant_id,
    admitted_at_ms,
    charged_tokens
);

-- ------------------------------------------------------------
-- 4. Active reservation index
-- ------------------------------------------------------------

-- Supports identifying active reservations that must
-- remain charged even if their admission time moves
-- outside the 60-second rolling window.
CREATE INDEX IF NOT EXISTS
    idx_reservations_active
ON token_reservations (
    tenant_id,
    admitted_at_ms,
    charged_tokens
)
WHERE status IN (
    'reserved',
    'in_flight',
    'timed_out'
);

-- ------------------------------------------------------------
-- 5. Lifecycle management index
-- ------------------------------------------------------------

-- Supports safe cleanup of finalized historical rows.
-- Active reservations are deliberately excluded.
CREATE INDEX IF NOT EXISTS
    idx_reservations_finalized_cleanup
ON token_reservations (
    finalized_at_ms
)
WHERE status IN ('settled', 'released');

-- ------------------------------------------------------------
-- 6. Quota/accounting invariant index
-- ------------------------------------------------------------

-- Facilitates detecting stale unresolved reservations
-- without scanning settled historical records.
CREATE INDEX IF NOT EXISTS
    idx_reservations_unresolved
ON token_reservations (
    updated_at_ms,
    tenant_id
)
WHERE status IN (
    'reserved',
    'in_flight',
    'timed_out'
);

-- ------------------------------------------------------------
-- 7. Schema migration tracking
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at_ms INTEGER NOT NULL
        CHECK (applied_at_ms >= 0)
);

COMMIT;

-- The migration runner is responsible for inserting
-- migration version 1 after successful application,
-- using its injected database clock.
