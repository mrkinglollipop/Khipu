-- Source-backed topic briefs.
--
-- A brief is a derived summary of one topic in which every claim names the
-- episodes it came from. It is written by khipu.briefs beside the existing
-- topic-page producer and replaces nothing: `topics` is untouched.
--
-- `source_hash` fingerprints the topic's source episodes (id, revision,
-- decision state) at build time; a brief is stale when the fingerprint of
-- today's sources differs. `state` is 'current', 'stale' or 'failed'. A
-- failed row records a build that produced no usable claim and never
-- displaces the brief that was current before it. `superseded_at` marks a
-- brief a newer one replaced; the one current brief per topic is enforced in
-- code (khipu.briefs), not by a unique index.
--
-- Additive only: a new table, no change to any existing one. khipu.briefs and
-- khipu.forget gate every reader and writer on khipu.db.has_columns, so this
-- table's absence degrades every surface instead of failing it.
--
-- NOT applied anywhere by this change. Applied to the hub later, by a
-- person, after authorization.

CREATE TABLE IF NOT EXISTS briefs (
    id                  BIGSERIAL PRIMARY KEY,
    topic_slug          TEXT NOT NULL,
    project             TEXT,
    body                TEXT NOT NULL DEFAULT '',
    claims              JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_episode_ids  BIGINT[] NOT NULL DEFAULT '{}',
    source_hash         TEXT NOT NULL,
    derivation_version  INTEGER NOT NULL DEFAULT 1,
    model               TEXT,
    state               TEXT NOT NULL DEFAULT 'current',
    fail_reason         TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_briefs_topic_created
    ON briefs (topic_slug, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_briefs_source_episodes
    ON briefs USING gin (source_episode_ids);

INSERT INTO schema_migrations (version, note)
VALUES (
    '0025_briefs',
    'Phase 4A: briefs (topic_slug, body, claims, source_episode_ids, source_hash, derivation_version, model, state current|stale|failed, superseded_at) — source-backed derived topic summaries'
)
ON CONFLICT (version) DO NOTHING;
