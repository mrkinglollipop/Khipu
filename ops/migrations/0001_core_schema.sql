-- Khipu P1b — core product schema (memory wiki + graph + embeddings shell + PROPERTY GRAPH).
-- Apply after 0000_bootstrap. Idempotent-ish: IF NOT EXISTS / OR REPLACE where supported.

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Memory wiki (file dual-run → PG)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS episodes (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    session_id      TEXT,
    summary         TEXT NOT NULL,
    topics          JSONB NOT NULL DEFAULT '[]'::jsonb,
    people          JSONB NOT NULL DEFAULT '[]'::jsonb,
    decisions       JSONB NOT NULL DEFAULT '[]'::jsonb,
    preferences     JSONB NOT NULL DEFAULT '[]'::jsonb,
    scope           TEXT,
    edges           JSONB NOT NULL DEFAULT '[]'::jsonb,
    raw             JSONB,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_episodes_ts ON episodes (ts DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_topics ON episodes USING gin (topics);

CREATE TABLE IF NOT EXISTS topics (
    slug            TEXT PRIMARY KEY,
    title           TEXT,
    body            TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    links           JSONB NOT NULL DEFAULT '[]'::jsonb,
    frontmatter     JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_path     TEXT,
    content_hash    TEXT
);

CREATE INDEX IF NOT EXISTS idx_topics_updated ON topics (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_topics_status ON topics (status);

CREATE TABLE IF NOT EXISTS topic_revisions (
    id              BIGSERIAL PRIMARY KEY,
    slug            TEXT NOT NULL REFERENCES topics (slug) ON DELETE CASCADE,
    revised_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    body            TEXT NOT NULL,
    source          TEXT,
    note            TEXT,
    content_hash    TEXT
);

CREATE INDEX IF NOT EXISTS idx_topic_revisions_slug_ts
    ON topic_revisions (slug, revised_at DESC);

-- ---------------------------------------------------------------------------
-- Knowledge graph (from graph.sqlite nodes/edges — embeddings SKIP Voyage)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS nodes (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    bucket          TEXT,
    name            TEXT,
    payload         JSONB,
    source_path     TEXT,
    built_at        TIMESTAMPTZ,
    frozen          BOOLEAN NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes (type);
CREATE INDEX IF NOT EXISTS idx_nodes_bucket ON nodes (bucket);
CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes (name);

CREATE TABLE IF NOT EXISTS edges (
    src             TEXT NOT NULL REFERENCES nodes (id),
    dst             TEXT NOT NULL REFERENCES nodes (id),
    type            TEXT NOT NULL,
    weight          DOUBLE PRECISION,
    payload         JSONB,
    built_at        TIMESTAMPTZ,
    PRIMARY KEY (src, dst, type)
);

CREATE INDEX IF NOT EXISTS idx_edges_src ON edges (src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges (dst);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges (type);

-- Empty shell for Khipu Gemini embeds (768 + L2). No Voyage ETL.
CREATE TABLE IF NOT EXISTS embeddings (
    node_id         TEXT NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
    chunk_idx       INTEGER NOT NULL DEFAULT 0,
    source_file     TEXT,
    chunk_text      TEXT,
    embedding       vector(768),
    model           TEXT NOT NULL,
    dim             INTEGER NOT NULL DEFAULT 768,
    built_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (node_id, chunk_idx)
);

CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings (model);
-- HNSW after we have rows; creating empty index is fine on pgvector 0.8
CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw
    ON embeddings USING hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- SQL/PGQ property graph (depth-1 MATCH; hops≥2 → recursive CTE in CLI)
-- ---------------------------------------------------------------------------

DROP PROPERTY GRAPH IF EXISTS alzy_graph;

CREATE PROPERTY GRAPH alzy_graph
  VERTEX TABLES (
    nodes KEY (id) LABEL node
      PROPERTIES (id, type, bucket, name, frozen)
  )
  EDGE TABLES (
    edges KEY (src, dst, type)
      SOURCE KEY (src) REFERENCES nodes (id)
      DESTINATION KEY (dst) REFERENCES nodes (id)
      LABEL edge
      PROPERTIES (type, weight)
  );

INSERT INTO schema_migrations (version, note)
VALUES (
    '0001_core_schema',
    'P1b: episodes/topics/topic_revisions/nodes/edges/embeddings + alzy_graph PROPERTY GRAPH'
)
ON CONFLICT (version) DO NOTHING;
