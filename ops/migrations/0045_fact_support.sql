-- Deduplicated facts retain each independent source. No guessed paraphrase
-- provenance is backfilled: forget recovers only exact kept raw evidence.
SET LOCAL lock_timeout = '5s';
CREATE TABLE IF NOT EXISTS fact_support (
    kind TEXT NOT NULL CHECK (kind IN ('commitment', 'decision', 'deliverable')),
    fact_id BIGINT NOT NULL,
    -- Intentionally no FK: legacy recovery can record another live episode
    -- while its capture holds FOR UPDATE and waits on the support lock. An FK
    -- key-share here would invert those locks. Reads join the live episode.
    episode_id BIGINT NOT NULL,
    value JSONB NOT NULL CHECK (jsonb_typeof(value) = 'object'),
    legacy BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (kind, fact_id, episode_id)
);
CREATE INDEX IF NOT EXISTS idx_fact_support_episode ON fact_support(episode_id);
INSERT INTO fact_support(kind, fact_id, episode_id, value, legacy)
SELECT 'commitment', c.id, c.opened_episode, to_jsonb(c), TRUE
FROM commitments c JOIN episodes e ON e.id = c.opened_episode
WHERE e.deleted_at IS NULL
ON CONFLICT DO NOTHING;
INSERT INTO fact_support(kind, fact_id, episode_id, value, legacy)
SELECT 'decision', d.id, d.episode_id, to_jsonb(d), TRUE
FROM decisions d JOIN episodes e ON e.id = d.episode_id
WHERE e.deleted_at IS NULL
ON CONFLICT DO NOTHING;
INSERT INTO fact_support(kind, fact_id, episode_id, value, legacy)
SELECT 'deliverable', d.id, d.episode_id, to_jsonb(d), TRUE
FROM deliverables d JOIN episodes e ON e.id = d.episode_id
WHERE e.deleted_at IS NULL
ON CONFLICT DO NOTHING;
INSERT INTO schema_migrations(version, note)
VALUES ('0045_fact_support', 'per-source deduplicated fact provenance and conservative legacy recovery')
ON CONFLICT (version) DO NOTHING;
