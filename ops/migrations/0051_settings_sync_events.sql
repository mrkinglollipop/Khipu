-- Settings sync (slices F/G).  This migration is intentionally only an
-- operations artifact in this change: do not apply it from the app or tests.
-- The existing routing_events payload remains the append-only transport;
-- this partial index makes the off-state owner copy switch poll bounded.
-- Retention is performed by the routing compactor after 120 days, never by a
-- hub delete in this migration.
--
-- Reverse: DROP INDEX IF EXISTS idx_routing_events_settings_sync_switch;

CREATE INDEX IF NOT EXISTS idx_routing_events_settings_sync_switch
    ON routing_events (received_at, event_id)
    WHERE payload ->> 'type' = 'settings_sync_switch';

INSERT INTO schema_migrations (version, note)
VALUES ('0051_settings_sync_events', 'settings sync event/control lookup index; retention stays in compactor')
ON CONFLICT (version) DO NOTHING;
