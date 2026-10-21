-- Closed cloud projection only; raw routing events never feed gateway reads.
CREATE TABLE IF NOT EXISTS routing_gateway_copies (
    publisher TEXT PRIMARY KEY,
    projection JSONB NOT NULL CHECK (jsonb_typeof(projection) = 'object'),
    epoch TEXT NOT NULL,
    revision BIGINT NOT NULL,
    folded_at TIMESTAMPTZ NOT NULL,
    published_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    stale_after DOUBLE PRECISION NOT NULL CHECK (stale_after >= 0)
);
CREATE INDEX IF NOT EXISTS idx_routing_gateway_copy_newest
    ON routing_gateway_copies (published_at DESC, publisher);

-- Receipt storage survives container replacement. Identity is a hash of the
-- authenticated token label and caller session, never their literal words.
CREATE TABLE IF NOT EXISTS routing_gateway_delivery (
    identity TEXT PRIMARY KEY,
    state JSONB NOT NULL
);
INSERT INTO schema_migrations(version, note)
VALUES ('0044_routing_gateway_copy', 'closed Mac gateway copies and durable pull receipts')
ON CONFLICT (version) DO NOTHING;
