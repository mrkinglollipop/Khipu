-- Khipu capture that cannot lose — K2 verbatim tier (Phase 2, 2026-09-14).
--
-- The 1-3 sentence model summary destroyed error strings, shell commands,
-- file paths and the user's own words outright — nothing downstream of
-- extraction ever saw them again. verbatim is the regex-extracted tier
-- (never model-summarized) stored alongside the summary: errors, commands,
-- paths, up to three literal user quotes, and note (a `khipu capture now`
-- caller's text). Redacted the same way every other capture field is.

ALTER TABLE episodes ADD COLUMN IF NOT EXISTS verbatim jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN episodes.verbatim IS
    'Regex-extracted errors/commands/paths/quotes from the capture window, plus note (a khipu capture now caller''s text). Never model-summarized; redacted like every other capture field.';

INSERT INTO schema_migrations (version, note)
VALUES (
    '0017_capture_verbatim',
    'episodes.verbatim jsonb — the regex-extracted verbatim tier (K2)'
)
ON CONFLICT (version) DO NOTHING;
