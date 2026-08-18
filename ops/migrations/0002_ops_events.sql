-- Khipu P2 — ops health events for doctor backup-age / last restore.
-- Apply after 0001_core_schema.

CREATE TABLE IF NOT EXISTS ops_events (
    id          BIGSERIAL PRIMARY KEY,
    kind        TEXT NOT NULL,
    -- kinds: walg_basebackup | pg_dump | restore_drill
    status      TEXT NOT NULL DEFAULT 'ok',
    -- ok | fail | mismatch
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ops_events_kind_created
    ON ops_events (kind, created_at DESC);

COMMENT ON TABLE ops_events IS
    'Backup / restore drill heartbeats for khipu doctor (Mac reads via DSN).';

INSERT INTO schema_migrations (version, note)
VALUES (
    '0002_ops_events',
    'P2: ops_events heartbeats table for doctor backup-age / restore-drill health'
)
ON CONFLICT (version) DO NOTHING;
