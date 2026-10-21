-- Routing replication, slice 11a (docs/plans/2026-10-20-routing-first-class.md,
-- sections 6 and 7). The number is 0042 because 0041 is held by another
-- change; this file takes the next free number after it.
--
-- routing_events holds only the replication payload a Mac sends: the closed
-- projection built by khipu.routing.replication_payload (ids, kinds,
-- structured fields, stable account ids, hlc, base, author as
-- {kind, harness, mac}, the owner's confirmed words as written, owner hold
-- words, and proposal text only under its switch). It never holds a login
-- home, a local source_id, evidence text, project or session names, or any
-- other field the projector does not list. The hub stores the payload
-- append-only, keyed by event id, with the owner's original words (paths
-- included) stored as written; hub_order is the hub's own acceptance order,
-- which slice 11b uses to number rules. A duplicate upload of one event id
-- is ignored by the primary key, never written twice.
--
-- routing_sources is the per-Mac source status row: how fresh each sending
-- Mac's stream is, by the hub's own clock.
--
-- Additive and safe to run twice. Reverse: DROP INDEX IF EXISTS
-- idx_routing_events_received, idx_routing_events_sender; DROP TABLE
-- routing_sources, routing_events; and delete the
-- '0042_routing_replication' row from schema_migrations.

CREATE TABLE IF NOT EXISTS routing_events (
    hub_order   BIGSERIAL,
    event_id    TEXT PRIMARY KEY,
    sender_mac  TEXT NOT NULL,
    payload     JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_routing_events_sender
    ON routing_events (sender_mac, hub_order);

CREATE INDEX IF NOT EXISTS idx_routing_events_received
    ON routing_events (received_at);

CREATE TABLE IF NOT EXISTS routing_sources (
    mac            TEXT PRIMARY KEY,
    last_hub_order BIGINT NOT NULL DEFAULT 0,
    last_event_id  TEXT,
    event_count    BIGINT NOT NULL DEFAULT 0,
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version, note)
VALUES ('0042_routing_replication', 'routing replication: routing_events payload store; routing_sources per-Mac status (slice 11a)')
ON CONFLICT (version) DO NOTHING;
