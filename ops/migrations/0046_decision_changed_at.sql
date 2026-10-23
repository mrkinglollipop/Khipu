-- A decision can change provenance or words without being superseded/retracted.
-- Track that change separately from owner/authored/lifecycle timestamps.
-- Existing rows retain NULL until their next actual edit; readers fall back
-- to the existing created/superseded/retracted watermark for those rows.
SET LOCAL lock_timeout = '5s';
LOCK TABLE decisions IN ACCESS EXCLUSIVE MODE;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS changed_at TIMESTAMPTZ;
CREATE OR REPLACE FUNCTION khipu_decisions_changed_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN
        RETURN NEW;
    END IF;
    NEW.changed_at := clock_timestamp();
    RETURN NEW;
END
$$;
DROP TRIGGER IF EXISTS khipu_decisions_changed_at ON decisions;
CREATE TRIGGER khipu_decisions_changed_at
    BEFORE INSERT OR UPDATE ON decisions
    FOR EACH ROW EXECUTE FUNCTION khipu_decisions_changed_at();
CREATE INDEX IF NOT EXISTS idx_decisions_changed_at
    ON decisions(changed_at, id) WHERE changed_at IS NOT NULL;
COMMENT ON COLUMN decisions.changed_at IS
    'Last actual row change, including provenance edits; distinct from authored/lifecycle times. NULL for an unchanged pre-0046 row.';
INSERT INTO schema_migrations(version, note)
VALUES ('0046_decision_changed_at', 'durable decision provenance change watermark for incremental replicas')
ON CONFLICT (version) DO NOTHING;
