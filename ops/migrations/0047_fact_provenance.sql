-- Fact provenance: who owns each piece of a fact's wording.
-- Scope: docs/plans/2026-10-23-fact-provenance.md (sections 3, 4 and 5.3).
--
-- Why: capture deduplicates. A second episode that restates a commitment
-- overwrote its latest_text and unioned its PR numbers without recording who
-- supplied which value, so forget could only act on a fact by its anchor
-- episode: it retired facts a kept episode still supported, and never touched
-- wording a non-anchor episode wrote. From here every stored wording value has
-- one claimant (a capture episode, the owner, or "legacy_unproven" for a value
-- the code cannot prove), recorded in fact_claims, and a fact's visible
-- wording is a pure function of its live claims: khipu_project_fact() below,
-- which khipu/provenance.py calls as project().
--
--   fact_claims          (kind, fact_id, field, value, claimant, episode_id).
--                        One row per claimed value; a replay writes nothing.
--   fact_changes         one row per projection (via 'project'), and per
--                        other decision/deliverable row write (via 'row'),
--                        stamped with its transaction id: the commit gate
--                        below (via 'project' only) and the replica change
--                        stream (section 6). fact_changes_floor records how
--                        far the 30-day prune has removed rows.
--   fact_gaps            a capture fact step that failed and was kept as a
--                        gap (doctor reports it, the nightly re-runs it).
--   provenance_projects  legacy witness backfill progress, per project
--                        ('' for facts with no project).
--
-- Gates. Refusals of a writer that predates this migration raise SQLSTATE
-- 08K01, which psycopg raises as plain OperationalError and older
-- khipu.hub_errors.unreachable_error() reads as "hub away": an old outbox
-- keeps the job queued and never buries it.
--   * Fact gate (deferred to COMMIT): an INSERT into commitments, decisions
--     or deliverables, or an UPDATE that changes a wording or anchor column,
--     needs a fact_changes row for that fact from the same transaction. Only
--     khipu_project_fact() writes one.
--   * Forget gate (deferred to COMMIT): setting episodes.deleted_at needs that
--     no fact_claims row names the episode (the new forget deletes them and
--     re-projects first).
--   * Claim gate (immediate, BEFORE INSERT on fact_claims): an episode claim
--     takes FOR SHARE on its episode row and is refused for a forgotten or
--     missing episode, with check_violation (an old writer never inserts a
--     claim, so this is never the "upgrade it" case). Immediate, not
--     deferred: the episode lock must come before the fact row locks, the
--     order forget takes them.
--
-- Legacy data (section 4): only what the writer inventory proves is
-- attributed. commitments.text, the PR numbers that text itself names,
-- decisions.text/rationale/evidence and deliverables.title each have one
-- writer, the insert by the anchor episode, so they get an episode claim for
-- a live anchor. A decision with no episode was written by create_decision
-- (the owner's supersede), and the new_id of an owner-made supersede link
-- holds the owner's endorsement: both get owner claims. Everything else
-- (latest_text, PR numbers or refs the text does not name, a fact whose
-- anchor is NULL) becomes a legacy_unproven claim, which the projection
-- never shows. A fact whose anchor episode is already forgotten gets no
-- episode claim: the projection retires it exactly as forget did.
--
-- Locks: ACCESS EXCLUSIVE on episodes, commitments, decisions and
-- deliverables, in writer lock order, before anything else, under a 5 s
-- deadline (the 0030 pattern: wait for episodes holding nothing, then ask for
-- the rest without waiting). Writers in flight finish first, so the
-- classification sees them; later writers wait, then meet the gates.
-- khipu migrate retries the whole file on lock_not_available.
--
-- One file, one transaction: there is no state with the tables but without
-- the gates. Refused when the hub carries 0045 or 0046 (the abandoned
-- fact-support attempt): that hub is disposable and must be rebuilt.
-- Reverse with migration 0048 (drop the four triggers, keep the tables);
-- code that predates this migration is refused until then.

SET LOCAL lock_timeout = '5s';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM schema_migrations
               WHERE version LIKE '0045\_%' OR version LIKE '0046\_%') THEN
        RAISE EXCEPTION 'khipu: this hub carries migration 0045 or 0046 from the abandoned fact-support attempt; 0047 refuses to run on it'
            USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
END
$$;

DO $$
DECLARE
    deadline TIMESTAMPTZ := clock_timestamp() + interval '5 seconds';
BEGIN
    LOOP
        BEGIN
            LOCK TABLE episodes IN ACCESS EXCLUSIVE MODE;
            LOCK TABLE commitments IN ACCESS EXCLUSIVE MODE NOWAIT;
            LOCK TABLE decisions IN ACCESS EXCLUSIVE MODE NOWAIT;
            LOCK TABLE deliverables IN ACCESS EXCLUSIVE MODE NOWAIT;
            RETURN;
        EXCEPTION WHEN lock_not_available THEN
            IF clock_timestamp() >= deadline THEN
                RAISE;
            END IF;
            PERFORM pg_sleep(0.02);
        END;
    END LOOP;
END
$$;

-- ---- Tables -----------------------------------------------------------------

CREATE TABLE IF NOT EXISTS fact_claims (
    id          BIGSERIAL PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('commitment', 'decision', 'deliverable')),
    fact_id     BIGINT NOT NULL,
    field       TEXT NOT NULL,
    value       TEXT NOT NULL,
    claimant    TEXT NOT NULL CHECK (claimant IN ('episode', 'owner', 'legacy_unproven')),
    episode_id  BIGINT,
    xid         XID8 NOT NULL DEFAULT pg_current_xact_id(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fact_claims_field_check CHECK (
        (kind = 'commitment' AND field IN ('text', 'restatement', 'pr', 'pr_ref'))
        OR (kind = 'decision' AND field IN ('text', 'rationale', 'evidence'))
        OR (kind = 'deliverable' AND field IN ('title', 'support'))),
    CONSTRAINT fact_claims_episode_check CHECK (
        (claimant = 'episode') = (episode_id IS NOT NULL))
);

-- The claim's identity. A primary key cannot hold expressions, so this unique
-- index is the key (and the ON CONFLICT target) and id is a surrogate.
CREATE UNIQUE INDEX IF NOT EXISTS uq_fact_claims_identity
    ON fact_claims (kind, fact_id, field, md5(value), coalesce(episode_id, 0), claimant);
CREATE INDEX IF NOT EXISTS idx_fact_claims_episode
    ON fact_claims (episode_id) WHERE episode_id IS NOT NULL;

COMMENT ON TABLE fact_claims IS
    'Who supplied each wording value of a commitment, decision or deliverable (0047). Hub only, never sent to a replica. The projection shows episode claims of live episodes and owner claims, never legacy_unproven ones.';

CREATE TABLE IF NOT EXISTS fact_changes (
    seq      BIGSERIAL PRIMARY KEY,
    xid      XID8 NOT NULL DEFAULT pg_current_xact_id(),
    kind     TEXT NOT NULL CHECK (kind IN ('commitment', 'decision', 'deliverable')),
    fact_id  BIGINT NOT NULL,
    -- 'project': khipu_project_fact() recorded it (the only kind the fact
    -- gate accepts). 'row': any other write to a decision or deliverable row
    -- (an owner's supersede, retract or confirm), recorded by
    -- khipu_fact_row_change() so replicas see lifecycle changes too.
    via      TEXT NOT NULL DEFAULT 'project' CHECK (via IN ('project', 'row')),
    at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fact_changes_fact_xid ON fact_changes (kind, fact_id, xid);
CREATE INDEX IF NOT EXISTS idx_fact_changes_xid ON fact_changes (xid);
CREATE INDEX IF NOT EXISTS idx_fact_changes_at ON fact_changes (at);

COMMENT ON TABLE fact_changes IS
    'One row per khipu_project_fact() call (via project), and per other decision or deliverable row write (via row), stamped with its transaction id (0047). The fact gate requires a via-project row for every wording change; replicas read the table as their decisions and deliverables change stream (section 6). Rows older than 30 days are pruned (khipu.provenance.prune_changes).';

-- The replica stream's floor: the highest xid whose rows the prune removed.
-- A replica whose cursor is at or below it may have missed a change and asks
-- for a full dump. One row at most.
CREATE TABLE IF NOT EXISTS fact_changes_floor (
    one             BOOLEAN PRIMARY KEY DEFAULT true CHECK (one),
    pruned_through  XID8 NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_gaps (
    id           BIGSERIAL PRIMARY KEY,
    episode_id   BIGINT NOT NULL,
    step         TEXT NOT NULL,
    error        TEXT,
    at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_fact_gaps_open ON fact_gaps (at) WHERE resolved_at IS NULL;

CREATE TABLE IF NOT EXISTS provenance_projects (
    project        TEXT PRIMARY KEY,
    backfilled_at  TIMESTAMPTZ
);

COMMENT ON COLUMN provenance_projects.project IS
    'The project the witness backfill covered; the empty string stands for facts with no project.';

-- ---- PR numbers and refs a text names (legacy classification only) --------
--
-- A stricter reading of khipu.close_rules.parse_prs / pr_requirement_labels:
-- every number it returns is one parse_prs returns for the same text, never
-- more (word boundaries and spaces count as ASCII only, so a non-ASCII
-- neighbour makes it miss a number rather than invent one). A missed number
-- becomes a legacy_unproven claim, which only costs auto-close (needs_owner);
-- an invented one could expose another episode's wording. Pinned by
-- tests/test_provenance_gate.py against parse_prs. Used by this migration's
-- classification only; capture records claims from parse_prs itself.

CREATE OR REPLACE FUNCTION khipu_text_prs(t TEXT)
RETURNS INTEGER[] LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE
    nb CONSTANT TEXT := '(?<![^\x21-\x2f\x3a-\x40\x5b-\x5e\x60\x7b-\x7e[:space:]])';
    na CONSTANT TEXT := '(?![^\x21-\x2f\x3a-\x40\x5b-\x5e\x60\x7b-\x7e[:space:]])';
    hb CONSTANT TEXT := '(?<![^\x21-\x25\x27-\x2f\x3a-\x40\x5b-\x5e\x60\x7b-\x7e[:space:]])';
    word CONSTANT TEXT := '(?:prs?|pull[[:space:]]+requests?)';
    m TEXT[];
    h TEXT[];
    found INTEGER[] := '{}';
BEGIN
    IF t IS NULL THEN
        RETURN found;
    END IF;
    FOR m IN SELECT regexp_matches(t, nb || word || '[[:space:]]*#?[[:space:]]*([0-9]{1,7})' || na, 'gi') LOOP
        found := found || m[1]::INTEGER;
    END LOOP;
    FOR m IN SELECT regexp_matches(t, nb || word || na || '([^.;:\n]*)', 'gi') LOOP
        FOR h IN SELECT regexp_matches(m[1], hb || '#([0-9]{1,7})' || na, 'g') LOOP
            found := found || h[1]::INTEGER;
        END LOOP;
    END LOOP;
    FOR m IN SELECT regexp_matches(t, 'github\.com/([a-z0-9][a-z0-9_.-]*/[a-z0-9_.-]+?)/pull/([0-9]{1,7})' || na, 'gi') LOOP
        found := found || m[2]::INTEGER;
    END LOOP;
    RETURN ARRAY(SELECT DISTINCT n FROM unnest(found) AS n WHERE n > 0 ORDER BY n);
END
$fn$;

-- The "owner/repo#N" labels a text's GitHub pull links name, lower-cased as
-- pr_requirement_labels prints them. A plain "PR #41" needs the project's
-- repository, which only the Python side knows, so it is never proven here.
CREATE OR REPLACE FUNCTION khipu_text_pull_refs(t TEXT)
RETURNS TEXT[] LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE
    na CONSTANT TEXT := '(?![^\x21-\x2f\x3a-\x40\x5b-\x5e\x60\x7b-\x7e[:space:]])';
    m TEXT[];
    found TEXT[] := '{}';
BEGIN
    IF t IS NULL THEN
        RETURN found;
    END IF;
    FOR m IN SELECT regexp_matches(t, 'github\.com/([a-z0-9][a-z0-9_.-]*/[a-z0-9_.-]+?)/pull/([0-9]{1,7})' || na, 'gi') LOOP
        IF m[2]::INTEGER > 0 THEN
            found := found || (lower(m[1]) || '#' || m[2]::INTEGER::TEXT);
        END IF;
    END LOOP;
    RETURN ARRAY(SELECT DISTINCT r FROM unnest(found) AS r ORDER BY r);
END
$fn$;

-- ---- The projector ------------------------------------------------------------
--
-- khipu_project_fact(kind, id): recompute a fact's wording from its live
-- claims (section 3.2), record the fact in fact_changes for this
-- transaction, and return what it did: 'projected', 'retired', 'deleted' or
-- 'missing'. A live claim is an episode claim whose episode is not forgotten,
-- or an owner claim; legacy_unproven claims are never shown. Owner fields
-- (status apart from retirement, owner, lineage, snooze, links, supersede and
-- retract state) are never written. content_hash is identity and never
-- recomputed. When text or latest_text changes the fact's embedding is
-- dropped (the nightly re-embeds it).

CREATE OR REPLACE FUNCTION khipu_fact_record_change(p_kind TEXT, p_id BIGINT)
RETURNS void LANGUAGE plpgsql AS $fn$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM fact_changes
                   WHERE kind = p_kind AND fact_id = p_id AND xid = pg_current_xact_id()
                     AND via = 'project') THEN
        INSERT INTO fact_changes (kind, fact_id) VALUES (p_kind, p_id);
    END IF;
END
$fn$;

CREATE OR REPLACE FUNCTION khipu_project_fact(p_kind TEXT, p_id BIGINT)
RETURNS TEXT LANGUAGE plpgsql AS $fn$
DECLARE
    c RECORD;
    d RECORD;
    v RECORD;
    v_anchor BIGINT;
    v_owner BOOLEAN := false;
    v_text TEXT;
    v_latest TEXT;
    v_rationale TEXT;
    v_evidence JSONB;
    v_prs INTEGER[];
    v_refs JSONB;
    v_needs BOOLEAN;
    v_check JSONB;
BEGIN
    IF p_kind NOT IN ('commitment', 'decision', 'deliverable') THEN
        RAISE EXCEPTION 'khipu: unknown fact kind %', p_kind USING ERRCODE = 'invalid_parameter_value';
    END IF;
    PERFORM khipu_fact_record_change(p_kind, p_id);

    -- ---- commitments ----
    IF p_kind = 'commitment' THEN
        SELECT * INTO c FROM commitments WHERE id = p_id FOR UPDATE;
        IF NOT FOUND THEN
            RETURN 'missing';
        END IF;
        SELECT fc.episode_id INTO v_anchor
        FROM fact_claims fc JOIN episodes e ON e.id = fc.episode_id AND e.deleted_at IS NULL
        WHERE fc.kind = 'commitment' AND fc.fact_id = p_id AND fc.claimant = 'episode'
          AND fc.field IN ('text', 'restatement')
        ORDER BY fc.created_at, fc.episode_id LIMIT 1;
        IF v_anchor IS NOT NULL THEN
            SELECT fc.value INTO v_text FROM fact_claims fc
            WHERE fc.kind = 'commitment' AND fc.fact_id = p_id AND fc.claimant = 'episode'
              AND fc.episode_id = v_anchor AND fc.field IN ('text', 'restatement')
            ORDER BY (fc.field = 'text') DESC, fc.created_at, fc.value LIMIT 1;
        ELSE
            SELECT fc.value INTO v_text FROM fact_claims fc
            WHERE fc.kind = 'commitment' AND fc.fact_id = p_id AND fc.claimant = 'owner'
              AND fc.field IN ('text', 'restatement')
            ORDER BY (fc.field = 'text') DESC, fc.created_at, fc.value LIMIT 1;
            v_owner := FOUND;
        END IF;

        IF v_anchor IS NULL AND NOT v_owner THEN
            -- Retirement: no live and no owner claim. Close as forget does,
            -- keep text and the anchor (the reader fences hide it), and drop
            -- every value no live claim holds.
            v_check := coalesce(c.pr_check, '{}'::jsonb) - 'refs' - 'needs_owner';
            IF c.pr_check IS NULL AND v_check = '{}'::jsonb THEN
                v_check := NULL;
            END IF;
            UPDATE commitments SET
                status = CASE WHEN status IN ('open', 'stale') THEN 'closed' ELSE status END,
                closed_at = CASE WHEN status IN ('open', 'stale') THEN now() ELSE closed_at END,
                close_reason = CASE WHEN status IN ('open', 'stale') THEN 'forgotten' ELSE close_reason END,
                latest_text = NULL,
                prs = '{}',
                pr_check = v_check
            WHERE id = p_id
              AND (status IN ('open', 'stale') OR latest_text IS NOT NULL
                   OR prs IS DISTINCT FROM '{}'::INTEGER[] OR pr_check IS DISTINCT FROM v_check);
            IF c.latest_text IS NOT NULL THEN
                DELETE FROM memory_embeddings WHERE kind = 'commitment' AND ref = p_id::TEXT;
            END IF;
            RETURN 'retired';
        END IF;

        SELECT fc.value INTO v_latest FROM fact_claims fc
        LEFT JOIN episodes e ON e.id = fc.episode_id
        WHERE fc.kind = 'commitment' AND fc.fact_id = p_id AND fc.field = 'restatement'
          AND ((fc.claimant = 'episode' AND e.deleted_at IS NULL AND e.id IS NOT NULL)
               OR fc.claimant = 'owner')
          AND fc.value IS DISTINCT FROM v_text
        ORDER BY fc.created_at DESC, fc.episode_id DESC NULLS LAST, fc.value DESC LIMIT 1;

        SELECT coalesce(array_agg(DISTINCT fc.value::INTEGER ORDER BY fc.value::INTEGER), '{}')
        INTO v_prs FROM fact_claims fc LEFT JOIN episodes e ON e.id = fc.episode_id
        WHERE fc.kind = 'commitment' AND fc.fact_id = p_id AND fc.field = 'pr'
          AND ((fc.claimant = 'episode' AND e.deleted_at IS NULL AND e.id IS NOT NULL)
               OR fc.claimant = 'owner');
        -- prs NULL means "not parsed yet" (a row from before 0034): it stays
        -- NULL until close_rules.backfill_prs records the anchor's numbers as
        -- claims and marks it parsed. Any PR claim makes it parsed.
        IF c.prs IS NULL AND NOT EXISTS (
            SELECT 1 FROM fact_claims fc WHERE fc.kind = 'commitment' AND fc.fact_id = p_id AND fc.field = 'pr') THEN
            v_prs := NULL;
        END IF;

        -- In the order the refs were first claimed: the order the writer
        -- appended them to pr_check.refs before claims existed.
        SELECT jsonb_agg(r ORDER BY first_id) INTO v_refs FROM (
            SELECT fc.value AS r, min(fc.id) AS first_id FROM fact_claims fc
            LEFT JOIN episodes e ON e.id = fc.episode_id
            WHERE fc.kind = 'commitment' AND fc.fact_id = p_id AND fc.field = 'pr_ref'
              AND ((fc.claimant = 'episode' AND e.deleted_at IS NULL AND e.id IS NOT NULL)
                   OR fc.claimant = 'owner')
            GROUP BY fc.value) refs;

        -- A legacy PR value no live claim also holds: the item may need a PR
        -- nobody can show, so the close rules leave it to the owner.
        SELECT EXISTS (
            SELECT 1 FROM fact_claims u
            WHERE u.kind = 'commitment' AND u.fact_id = p_id AND u.claimant = 'legacy_unproven'
              AND u.field IN ('pr', 'pr_ref')
              AND NOT (u.field = 'pr' AND u.value::INTEGER = ANY (v_prs))
              AND NOT (u.field = 'pr_ref' AND coalesce(v_refs, '[]'::jsonb) ? u.value))
        INTO v_needs;

        v_check := coalesce(c.pr_check, '{}'::jsonb) - 'refs' - 'needs_owner';
        IF v_refs IS NOT NULL THEN
            v_check := v_check || jsonb_build_object('refs', v_refs);
        END IF;
        IF v_needs THEN
            v_check := v_check || jsonb_build_object('needs_owner', true);
        END IF;
        IF c.pr_check IS NULL AND v_check = '{}'::jsonb THEN
            v_check := NULL;
        END IF;

        UPDATE commitments SET
            text = v_text, latest_text = v_latest, prs = v_prs, pr_check = v_check,
            opened_episode = v_anchor
        WHERE id = p_id
          AND (text IS DISTINCT FROM v_text OR latest_text IS DISTINCT FROM v_latest
               OR prs IS DISTINCT FROM v_prs OR pr_check IS DISTINCT FROM v_check
               OR opened_episode IS DISTINCT FROM v_anchor);
        IF c.text IS DISTINCT FROM v_text OR c.latest_text IS DISTINCT FROM v_latest THEN
            DELETE FROM memory_embeddings WHERE kind = 'commitment' AND ref = p_id::TEXT;
        END IF;
        RETURN 'projected';
    END IF;

    -- ---- decisions ----
    IF p_kind = 'decision' THEN
        SELECT * INTO d FROM decisions WHERE id = p_id FOR UPDATE;
        IF NOT FOUND THEN
            RETURN 'missing';
        END IF;
        SELECT fc.episode_id, fc.value INTO v_anchor, v_text
        FROM fact_claims fc JOIN episodes e ON e.id = fc.episode_id AND e.deleted_at IS NULL
        WHERE fc.kind = 'decision' AND fc.fact_id = p_id AND fc.claimant = 'episode' AND fc.field = 'text'
        ORDER BY fc.created_at, fc.episode_id, fc.value LIMIT 1;
        IF v_anchor IS NULL THEN
            SELECT fc.value INTO v_text FROM fact_claims fc
            WHERE fc.kind = 'decision' AND fc.fact_id = p_id AND fc.claimant = 'owner' AND fc.field = 'text'
            ORDER BY fc.created_at, fc.value LIMIT 1;
            v_owner := FOUND;
        END IF;
        IF v_anchor IS NULL AND NOT v_owner THEN
            UPDATE decisions SET retracted_at = now(), retract_reason = 'forgotten'
            WHERE id = p_id AND retracted_at IS NULL;
            RETURN 'retired';
        END IF;
        SELECT fc.value INTO v_rationale FROM fact_claims fc
        WHERE fc.kind = 'decision' AND fc.fact_id = p_id AND fc.field = 'rationale'
          AND ((v_anchor IS NOT NULL AND fc.claimant = 'episode' AND fc.episode_id = v_anchor)
               OR (v_anchor IS NULL AND fc.claimant = 'owner'))
        ORDER BY fc.created_at, fc.value LIMIT 1;
        SELECT fc.value::JSONB INTO v_evidence FROM fact_claims fc
        WHERE fc.kind = 'decision' AND fc.fact_id = p_id AND fc.field = 'evidence'
          AND ((v_anchor IS NOT NULL AND fc.claimant = 'episode' AND fc.episode_id = v_anchor)
               OR (v_anchor IS NULL AND fc.claimant = 'owner'))
        ORDER BY fc.created_at, fc.value LIMIT 1;
        UPDATE decisions SET text = v_text, episode_id = v_anchor, rationale = v_rationale,
                             evidence = v_evidence
        WHERE id = p_id
          AND (text IS DISTINCT FROM v_text OR episode_id IS DISTINCT FROM v_anchor
               OR rationale IS DISTINCT FROM v_rationale OR evidence IS DISTINCT FROM v_evidence);
        RETURN 'projected';
    END IF;

    -- ---- deliverables ----
    SELECT * INTO v FROM deliverables WHERE id = p_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN 'missing';
    END IF;
    SELECT fc.episode_id INTO v_anchor
    FROM fact_claims fc JOIN episodes e ON e.id = fc.episode_id AND e.deleted_at IS NULL
    WHERE fc.kind = 'deliverable' AND fc.fact_id = p_id AND fc.claimant = 'episode'
      AND fc.field IN ('title', 'support')
    ORDER BY fc.created_at, fc.episode_id LIMIT 1;
    IF v_anchor IS NOT NULL THEN
        SELECT nullif(fc.value, '') INTO v_text FROM fact_claims fc
        WHERE fc.kind = 'deliverable' AND fc.fact_id = p_id AND fc.claimant = 'episode'
          AND fc.episode_id = v_anchor AND fc.field IN ('title', 'support')
        ORDER BY (fc.field = 'title') DESC, fc.created_at, fc.value LIMIT 1;
    ELSE
        SELECT nullif(fc.value, '') INTO v_text FROM fact_claims fc
        WHERE fc.kind = 'deliverable' AND fc.fact_id = p_id AND fc.claimant = 'owner'
          AND fc.field IN ('title', 'support')
        ORDER BY (fc.field = 'title') DESC, fc.created_at, fc.value LIMIT 1;
        v_owner := FOUND;
    END IF;
    IF v_anchor IS NULL AND NOT v_owner THEN
        DELETE FROM deliverables WHERE id = p_id;
        RETURN 'deleted';
    END IF;
    UPDATE deliverables SET title = v_text, episode_id = v_anchor
    WHERE id = p_id AND (title IS DISTINCT FROM v_text OR episode_id IS DISTINCT FROM v_anchor);
    RETURN 'projected';
END
$fn$;

-- ---- Legacy claims (section 4) ---------------------------------------------------

-- Commitments: the anchor's own text, and the PR numbers and pull-link refs
-- that text names.
INSERT INTO fact_claims (kind, fact_id, field, value, claimant, episode_id)
SELECT 'commitment', c.id, 'text', c.text, 'episode', c.opened_episode
FROM commitments c JOIN episodes e ON e.id = c.opened_episode AND e.deleted_at IS NULL
ON CONFLICT DO NOTHING;

INSERT INTO fact_claims (kind, fact_id, field, value, claimant, episode_id)
SELECT 'commitment', c.id, 'text', c.text, 'legacy_unproven', NULL
FROM commitments c WHERE c.opened_episode IS NULL
ON CONFLICT DO NOTHING;

INSERT INTO fact_claims (kind, fact_id, field, value, claimant, episode_id)
SELECT 'commitment', c.id, 'restatement', c.latest_text, 'legacy_unproven', NULL
FROM commitments c WHERE c.latest_text IS NOT NULL
ON CONFLICT DO NOTHING;

INSERT INTO fact_claims (kind, fact_id, field, value, claimant, episode_id)
SELECT 'commitment', c.id, 'pr', n::TEXT,
       CASE WHEN e.id IS NOT NULL AND n = ANY (khipu_text_prs(c.text)) THEN 'episode' ELSE 'legacy_unproven' END,
       CASE WHEN e.id IS NOT NULL AND n = ANY (khipu_text_prs(c.text)) THEN c.opened_episode END
FROM commitments c
LEFT JOIN episodes e ON e.id = c.opened_episode AND e.deleted_at IS NULL
CROSS JOIN LATERAL unnest(coalesce(c.prs, '{}'::INTEGER[])) AS n
WHERE n IS NOT NULL
ON CONFLICT DO NOTHING;

-- A row whose prs was never parsed (NULL, from before 0034) keeps NULL: its
-- one writer, close_rules.backfill_prs, stores parse_prs(text) (the anchor's
-- own words, read by the exact Python parser rather than the stricter SQL
-- one) as the anchor's claims after this migration. A text with no "pr" or
-- "pull" in it names no PR under any reading, so it is settled here as
-- parsed and empty.
UPDATE commitments SET prs = '{}'
WHERE prs IS NULL AND text !~ '[pP][rR]|[pP][uU][lL][lL]';

INSERT INTO fact_claims (kind, fact_id, field, value, claimant, episode_id)
SELECT 'commitment', c.id, 'pr_ref', r.value,
       CASE WHEN e.id IS NOT NULL AND lower(r.value) = ANY (khipu_text_pull_refs(c.text))
            THEN 'episode' ELSE 'legacy_unproven' END,
       CASE WHEN e.id IS NOT NULL AND lower(r.value) = ANY (khipu_text_pull_refs(c.text))
            THEN c.opened_episode END
FROM commitments c
LEFT JOIN episodes e ON e.id = c.opened_episode AND e.deleted_at IS NULL
CROSS JOIN LATERAL jsonb_array_elements_text(
    CASE WHEN jsonb_typeof(c.pr_check->'refs') = 'array' THEN c.pr_check->'refs' ELSE '[]'::jsonb END) AS r(value)
ON CONFLICT DO NOTHING;

-- Decisions: the anchor episode's text, rationale and evidence; a decision
-- with no episode is create_decision's (the owner's); an owner-made
-- supersede link's new decision carries the owner's endorsement.
INSERT INTO fact_claims (kind, fact_id, field, value, claimant, episode_id)
SELECT 'decision', d.id, f.field, f.value, 'episode', d.episode_id
FROM decisions d JOIN episodes e ON e.id = d.episode_id AND e.deleted_at IS NULL
CROSS JOIN LATERAL (VALUES ('text', d.text), ('rationale', d.rationale), ('evidence', d.evidence::TEXT))
    AS f(field, value)
WHERE f.value IS NOT NULL
ON CONFLICT DO NOTHING;

INSERT INTO fact_claims (kind, fact_id, field, value, claimant, episode_id)
SELECT 'decision', d.id, f.field, f.value, 'owner', NULL
FROM decisions d
CROSS JOIN LATERAL (VALUES ('text', d.text), ('rationale', d.rationale), ('evidence', d.evidence::TEXT))
    AS f(field, value)
WHERE d.episode_id IS NULL AND f.value IS NOT NULL
ON CONFLICT DO NOTHING;

INSERT INTO fact_claims (kind, fact_id, field, value, claimant, episode_id)
SELECT DISTINCT 'decision', d.id, 'text', d.text, 'owner', NULL::BIGINT
FROM decision_links l JOIN decisions d ON d.id = l.new_id
WHERE l.kind = 'supersedes' AND l.source IN ('manual', 'agent') AND l.state IN ('applied', 'restored')
ON CONFLICT DO NOTHING;

-- Deliverables: the anchor's title; an anchor with no title supports the
-- fact without wording.
INSERT INTO fact_claims (kind, fact_id, field, value, claimant, episode_id)
SELECT 'deliverable', v.id, CASE WHEN v.title IS NULL THEN 'support' ELSE 'title' END,
       coalesce(v.title, ''), 'episode', v.episode_id
FROM deliverables v JOIN episodes e ON e.id = v.episode_id AND e.deleted_at IS NULL
ON CONFLICT DO NOTHING;

INSERT INTO fact_claims (kind, fact_id, field, value, claimant, episode_id)
SELECT 'deliverable', v.id, 'title', v.title, 'legacy_unproven', NULL
FROM deliverables v WHERE v.episode_id IS NULL AND v.title IS NOT NULL
ON CONFLICT DO NOTHING;

-- ---- Initial projection -------------------------------------------------------------

SELECT count(khipu_project_fact('commitment', id)) FROM (SELECT id FROM commitments ORDER BY id) f;
SELECT count(khipu_project_fact('decision', id)) FROM (SELECT id FROM decisions ORDER BY id) f;
SELECT count(khipu_project_fact('deliverable', id)) FROM (SELECT id FROM deliverables ORDER BY id) f;

-- ---- Gates (after the projection, which they would otherwise check) ------------

CREATE OR REPLACE FUNCTION khipu_fact_gate()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM fact_changes
                   WHERE kind = TG_ARGV[0] AND fact_id = NEW.id AND xid = pg_current_xact_id()
                     AND via = 'project') THEN
        RAISE EXCEPTION 'khipu: this writer predates fact provenance; upgrade it'
            USING ERRCODE = '08K01',
                  DETAIL = format('%s %s changed its wording without khipu_project_fact()', TG_ARGV[0], NEW.id);
    END IF;
    RETURN NULL;
END
$fn$;

CREATE OR REPLACE FUNCTION khipu_forget_gate()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.deleted_at IS NOT NULL AND EXISTS (SELECT 1 FROM fact_claims WHERE episode_id = NEW.id) THEN
        RAISE EXCEPTION 'khipu: this writer predates fact provenance; upgrade it'
            USING ERRCODE = '08K01',
                  DETAIL = format('episode %s was forgotten while fact claims still name it', NEW.id);
    END IF;
    RETURN NULL;
END
$fn$;

CREATE OR REPLACE FUNCTION khipu_claim_gate()
RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    gone TIMESTAMPTZ;
BEGIN
    IF NEW.episode_id IS NOT NULL THEN
        SELECT deleted_at INTO gone FROM episodes WHERE id = NEW.episode_id FOR SHARE;
        IF NOT FOUND OR gone IS NOT NULL THEN
            RAISE EXCEPTION 'khipu: episode % is forgotten or missing; it cannot claim % %',
                NEW.episode_id, NEW.kind, NEW.fact_id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$fn$;

DROP TRIGGER IF EXISTS khipu_commitments_fact_insert ON commitments;
CREATE CONSTRAINT TRIGGER khipu_commitments_fact_insert
    AFTER INSERT ON commitments DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION khipu_fact_gate('commitment');

DROP TRIGGER IF EXISTS khipu_commitments_fact_update ON commitments;
CREATE CONSTRAINT TRIGGER khipu_commitments_fact_update
    AFTER UPDATE ON commitments DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (OLD.text IS DISTINCT FROM NEW.text
          OR OLD.latest_text IS DISTINCT FROM NEW.latest_text
          OR OLD.prs IS DISTINCT FROM NEW.prs
          OR (OLD.pr_check -> 'refs') IS DISTINCT FROM (NEW.pr_check -> 'refs')
          OR OLD.opened_episode IS DISTINCT FROM NEW.opened_episode)
    EXECUTE FUNCTION khipu_fact_gate('commitment');

DROP TRIGGER IF EXISTS khipu_decisions_fact_insert ON decisions;
CREATE CONSTRAINT TRIGGER khipu_decisions_fact_insert
    AFTER INSERT ON decisions DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION khipu_fact_gate('decision');

DROP TRIGGER IF EXISTS khipu_decisions_fact_update ON decisions;
CREATE CONSTRAINT TRIGGER khipu_decisions_fact_update
    AFTER UPDATE ON decisions DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (OLD.text IS DISTINCT FROM NEW.text
          OR OLD.rationale IS DISTINCT FROM NEW.rationale
          OR OLD.evidence IS DISTINCT FROM NEW.evidence
          OR OLD.episode_id IS DISTINCT FROM NEW.episode_id)
    EXECUTE FUNCTION khipu_fact_gate('decision');

DROP TRIGGER IF EXISTS khipu_deliverables_fact_insert ON deliverables;
CREATE CONSTRAINT TRIGGER khipu_deliverables_fact_insert
    AFTER INSERT ON deliverables DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION khipu_fact_gate('deliverable');

DROP TRIGGER IF EXISTS khipu_deliverables_fact_update ON deliverables;
CREATE CONSTRAINT TRIGGER khipu_deliverables_fact_update
    AFTER UPDATE ON deliverables DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (OLD.title IS DISTINCT FROM NEW.title
          OR OLD.episode_id IS DISTINCT FROM NEW.episode_id)
    EXECUTE FUNCTION khipu_fact_gate('deliverable');

DROP TRIGGER IF EXISTS khipu_episodes_forget_gate ON episodes;
CREATE CONSTRAINT TRIGGER khipu_episodes_forget_gate
    AFTER UPDATE OF deleted_at ON episodes DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (OLD.deleted_at IS DISTINCT FROM NEW.deleted_at)
    EXECUTE FUNCTION khipu_forget_gate();

-- The replica stream (section 6): every other write to a decision or
-- deliverable row (owner lifecycle changes, deletes) is recorded too, as
-- via 'row', which the fact gate never accepts. One row per fact per
-- transaction is enough: a replica re-reads the fact's current row.
CREATE OR REPLACE FUNCTION khipu_fact_row_change()
RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    v_id BIGINT := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM fact_changes
                   WHERE kind = TG_ARGV[0] AND fact_id = v_id AND xid = pg_current_xact_id()) THEN
        INSERT INTO fact_changes (kind, fact_id, via) VALUES (TG_ARGV[0], v_id, 'row');
    END IF;
    RETURN NULL;
END
$fn$;

DROP TRIGGER IF EXISTS khipu_decisions_fact_stream ON decisions;
CREATE TRIGGER khipu_decisions_fact_stream
    AFTER INSERT OR UPDATE OR DELETE ON decisions
    FOR EACH ROW EXECUTE FUNCTION khipu_fact_row_change('decision');

DROP TRIGGER IF EXISTS khipu_deliverables_fact_stream ON deliverables;
CREATE TRIGGER khipu_deliverables_fact_stream
    AFTER INSERT OR UPDATE OR DELETE ON deliverables
    FOR EACH ROW EXECUTE FUNCTION khipu_fact_row_change('deliverable');

DROP TRIGGER IF EXISTS khipu_fact_claims_gate ON fact_claims;
CREATE TRIGGER khipu_fact_claims_gate
    BEFORE INSERT ON fact_claims
    FOR EACH ROW EXECUTE FUNCTION khipu_claim_gate();

INSERT INTO schema_migrations (version, note)
VALUES (
    '0047_fact_provenance',
    'Fact provenance: fact_claims, fact_changes (+ floor), fact_gaps, provenance_projects; legacy claims proven by the writer inventory (the rest legacy_unproven); initial projection by khipu_project_fact(); deferred gates refuse wording writes outside the projector and forgets of claimed episodes (SQLSTATE 08K01), and claims for forgotten episodes'
)
ON CONFLICT (version) DO NOTHING;
