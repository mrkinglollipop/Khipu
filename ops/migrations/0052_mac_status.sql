-- One status row per Mac, shared through the hub (the desktop app's "Your Macs").
--
-- mac_status holds only the closed projection khipu.macs builds: the Mac's
-- routing identity and the name its owner gave it, the build it runs, when it
-- last reported (the hub's own clock), whether it is saving, and three small
-- JSON documents (health, running, signins) whose fields khipu.macs lists and
-- drops everything else from, on write and again on read. It never holds a
-- path, a login home, transcript text, a prompt or a key value. Each Mac writes
-- only its own row. An older build ignores the table.
--
-- Additive and safe to run twice. Reverse: DROP TABLE mac_status; and delete the
-- '0052_mac_status' row from schema_migrations.

CREATE TABLE IF NOT EXISTS mac_status (
    mac      TEXT PRIMARY KEY,
    name     TEXT,
    version  TEXT,
    seen_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    saving   BOOLEAN,
    health   JSONB CHECK (health IS NULL OR jsonb_typeof(health) = 'object'),
    running  JSONB CHECK (running IS NULL OR jsonb_typeof(running) = 'object'),
    signins  JSONB CHECK (signins IS NULL OR jsonb_typeof(signins) = 'array')
);

INSERT INTO schema_migrations (version, note)
VALUES ('0052_mac_status', 'one closed status row per Mac for the app''s Your Macs page (app slice B3)')
ON CONFLICT (version) DO NOTHING;
