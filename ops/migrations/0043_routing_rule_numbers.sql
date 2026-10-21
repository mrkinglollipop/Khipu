-- Stable display numbers are separate from the sequence across all event kinds.
-- No uniqueness constraint: pre-replication histories can already duplicate a
-- number. Readers refuse ambiguous numeric targets and offer the entity UUIDs.
ALTER TABLE routing_events ADD COLUMN IF NOT EXISTS rule_number BIGINT;
ALTER TABLE routing_sources ADD COLUMN IF NOT EXISTS rule_number_floor BIGINT NOT NULL DEFAULT 0;

-- Preserve already assigned numbers on records predating this migration.
-- number_pending marks provisional aliases which were never display numbers.
UPDATE routing_events
SET rule_number = coalesce((payload->>'local_order')::BIGINT, (payload->>'n')::BIGINT)
WHERE rule_number IS NULL AND payload->>'type' = 'rule_add'
  AND (payload->>'local_order' IS NOT NULL
       OR coalesce((payload->>'number_pending')::BOOLEAN, false) = false);

INSERT INTO schema_migrations (version, note)
VALUES ('0043_routing_rule_numbers', 'stable rule numbers and per-Mac legacy number reservations')
ON CONFLICT (version) DO NOTHING;
