-- Khipu P1a: empty/bootstrap migration revision only.
-- All production DDL (tables, indexes, PROPERTY GRAPH) lands in P1b.
-- Apply after Postgres 19 cluster is up (see pg19-install-runbook.md).

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    note TEXT
);

INSERT INTO schema_migrations (version, note)
VALUES ('0000_bootstrap', 'P1a empty revision — tooling smoke; no product DDL')
ON CONFLICT (version) DO NOTHING;
