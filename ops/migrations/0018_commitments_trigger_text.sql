-- Khipu commitments: trigger_text (O1, 2026-09-14 "memory that works like magic").
--
-- Migration 0013 added `future_trigger` (a bool) so an assistant commitment
-- with an explicit cross-session condition survives its own session's end.
-- This column carries the CONDITION ITSELF ("until the ledger closes",
-- "before the wave lands") so `khipu owed` can show it as "until: …" instead
-- of forcing a re-read of the full commitment text. Additive only — every
-- reader derives the same value from the text via
-- `khipu.commitments.trigger_clause()` when this column is absent (or on a
-- pre-migration hub), so an un-migrated hub behaves identically.

ALTER TABLE commitments ADD COLUMN IF NOT EXISTS trigger_text TEXT;

COMMENT ON COLUMN commitments.trigger_text IS
    'The future-trigger clause itself ("until the ledger closes"), when future_trigger is true. khipu.commitments.trigger_clause() derives it from the text deterministically; this column is a cache for humans/tools reading the table directly, not the source of truth.';

INSERT INTO schema_migrations (version, note)
VALUES (
    '0018_commitments_trigger_text',
    'commitments: trigger_text column — the deferral clause shown as "until: …" in khipu owed'
)
ON CONFLICT (version) DO NOTHING;
