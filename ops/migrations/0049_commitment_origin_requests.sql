-- Explicit owner confirmation for historical job-origin corrections.
SET LOCAL lock_timeout = '5s';

ALTER TABLE commitments ADD COLUMN IF NOT EXISTS origin_reviewed_at TIMESTAMPTZ;

-- Retain evidence even when a close is reopened or a snooze is removed.
-- Conservatively remember automated changes to these fields as well.
CREATE OR REPLACE FUNCTION khipu_commitment_origin_reviewed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.origin_reviewed_at := clock_timestamp();
    RETURN NEW;
END
$$;
DROP TRIGGER IF EXISTS khipu_commitment_origin_reviewed ON commitments;
CREATE TRIGGER khipu_commitment_origin_reviewed
BEFORE UPDATE OF text, latest_text, owner, owner_label, owner_reassigned_at,
                 status, due_after ON commitments
FOR EACH ROW EXECUTE FUNCTION khipu_commitment_origin_reviewed();

CREATE TABLE IF NOT EXISTS commitment_origin_requests (
    id TEXT PRIMARY KEY,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'applied')),
    preview JSONB NOT NULL,
    result JSONB
);

INSERT INTO schema_migrations (version, note)
VALUES ('0049_commitment_origin_requests', 'Owner-confirmed job-origin requests and conservative edit evidence')
ON CONFLICT (version) DO NOTHING;
