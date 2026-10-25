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
            LOCK TABLE episode_sources IN ACCESS EXCLUSIVE MODE NOWAIT;
            LOCK TABLE forgotten_sources IN ACCESS EXCLUSIVE MODE NOWAIT;
            LOCK TABLE commitments IN ACCESS EXCLUSIVE MODE NOWAIT;
            LOCK TABLE decisions IN ACCESS EXCLUSIVE MODE NOWAIT;
            LOCK TABLE decision_links IN ACCESS EXCLUSIVE MODE NOWAIT;
            LOCK TABLE deliverables IN ACCESS EXCLUSIVE MODE NOWAIT;
            LOCK TABLE memory_embeddings IN ACCESS EXCLUSIVE MODE NOWAIT;
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
    kind     TEXT CHECK (kind IN ('commitment', 'decision', 'deliverable')),
    fact_id  BIGINT,
    -- 'project': khipu_project_fact() recorded it (the only kind the fact
    -- gate accepts). 'row': any other write to a decision or deliverable row
    -- (an owner's supersede, retract or confirm), recorded by
    -- khipu_fact_row_change() so replicas see lifecycle changes too.
    -- 'copy': the change token (section 6.1), one per transaction that wrote
    -- a table a replica copy reads; it names no fact.
    via      TEXT NOT NULL DEFAULT 'project' CHECK (via IN ('project', 'row', 'copy')),
    at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fact_changes_fact_check CHECK (
        (via = 'copy') = (kind IS NULL) AND (kind IS NULL) = (fact_id IS NULL))
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
    resolved_at  TIMESTAMPTZ,
    -- The merged capture's payload hash for a merge step's gap (its payload
    -- is raw->'merged_from'[...]); NULL for the episode's own capture.
    source_id    TEXT,
    -- The nightly replay (provenance.replay_gaps): how it ended, and tries.
    resolution   TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0
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

-- A projection runs with its fact row locked, so it must never wait on an
-- episode: a forget holding that episode FOR UPDATE may be waiting on this
-- fact (Astra 7A re-review #3). Assigning a new anchor makes the foreign key
-- take KEY SHARE on it, so the anchor is taken FOR SHARE NOWAIT first
-- (contention: 55P03) and must still be live (a tombstone committed since the
-- anchor was chosen: 40001). Either aborts the whole transaction, which its
-- caller retries from fresh reads.
CREATE OR REPLACE FUNCTION khipu_take_anchor(p_episode BIGINT)
RETURNS void LANGUAGE plpgsql AS $fn$
BEGIN
    PERFORM 1 FROM episodes WHERE id = p_episode AND deleted_at IS NULL FOR SHARE NOWAIT;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'khipu: anchor episode % was forgotten during the projection', p_episode
            USING ERRCODE = 'serialization_failure';
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

        IF v_anchor IS NOT NULL AND v_anchor IS DISTINCT FROM c.opened_episode THEN
            PERFORM khipu_take_anchor(v_anchor);
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
        IF v_anchor IS NOT NULL AND v_anchor IS DISTINCT FROM d.episode_id THEN
            PERFORM khipu_take_anchor(v_anchor);
        END IF;
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
    IF v_anchor IS NOT NULL AND v_anchor IS DISTINCT FROM v.episode_id THEN
        PERFORM khipu_take_anchor(v_anchor);
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
        -- NOWAIT: a claim on another episode is written after fact locks
        -- (the witness backfill, a restatement), so waiting here could close
        -- a cycle with a forget holding that episode (Astra 7A re-review #3).
        -- Contention aborts the transaction (55P03) for its caller to retry.
        SELECT deleted_at INTO gone FROM episodes WHERE id = NEW.episode_id FOR SHARE NOWAIT;
        IF NOT FOUND OR gone IS NOT NULL THEN
            RAISE EXCEPTION 'khipu: episode % is forgotten or missing; it cannot claim % %',
                NEW.episode_id, NEW.kind, NEW.fact_id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$fn$;

-- The writer gate (Astra review 2026-10-23 #5). A capture that repeats
-- facts already on file word for word changes no fact column, so the fact
-- gate above never fires for it, and an old writer would commit the episode
-- with no claims. So an episode written or merged with fact items must come
-- from a writer whose fact steps declared themselves in this transaction
-- (provenance.declare_writer: the transaction-local setting below, set by a
-- fact step that records claims or by the fact-gap record). An old writer
-- never sets it and is refused at COMMIT like every other old wording write.
CREATE OR REPLACE FUNCTION khipu_has_fact_items(p JSONB)
RETURNS BOOLEAN LANGUAGE sql IMMUTABLE AS $fn$
    SELECT coalesce(bool_or(jsonb_typeof(p -> k) = 'array' AND jsonb_array_length(p -> k) > 0), false)
    FROM unnest(ARRAY['open_loops', 'decisions', 'deliverables']) AS k
$fn$;

CREATE OR REPLACE FUNCTION khipu_writer_gate()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF coalesce(current_setting('khipu.fact_writer', true), '') <> 'on' THEN
        RAISE EXCEPTION 'khipu: this writer predates fact provenance; upgrade it'
            USING ERRCODE = '08K01',
                  DETAIL = format('episode %s carries fact items but no fact step recorded claims', NEW.id);
    END IF;
    RETURN NULL;
END
$fn$;

-- An owner supersession endorses its replacement with an owner claim
-- (decisions.supersede, design 3.3); an old writer that reuses an existing
-- decision as the replacement records none, so it is refused too.
CREATE OR REPLACE FUNCTION khipu_supersede_gate()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM fact_changes
                   WHERE kind = 'decision' AND fact_id = NEW.superseded_by
                     AND xid = pg_current_xact_id() AND via = 'project') THEN
        RAISE EXCEPTION 'khipu: this writer predates fact provenance; upgrade it'
            USING ERRCODE = '08K01',
                  DETAIL = format('decision %s was superseded by %s without the owner''s claim',
                                  NEW.id, NEW.superseded_by);
    END IF;
    RETURN NULL;
END
$fn$;

DROP TRIGGER IF EXISTS khipu_episodes_writer_insert ON episodes;
CREATE CONSTRAINT TRIGGER khipu_episodes_writer_insert
    AFTER INSERT ON episodes DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (NEW.deleted_at IS NULL AND khipu_has_fact_items(NEW.raw))
    EXECUTE FUNCTION khipu_writer_gate();

DROP TRIGGER IF EXISTS khipu_episodes_writer_merge ON episodes;
CREATE CONSTRAINT TRIGGER khipu_episodes_writer_merge
    AFTER UPDATE OF raw ON episodes DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (NEW.deleted_at IS NULL
          AND (OLD.raw -> 'merged_from') IS DISTINCT FROM (NEW.raw -> 'merged_from')
          AND khipu_has_fact_items(NEW.raw -> 'merged_from' -> -1 -> 'payload'))
    EXECUTE FUNCTION khipu_writer_gate();

DROP TRIGGER IF EXISTS khipu_decisions_supersede_gate ON decisions;
CREATE CONSTRAINT TRIGGER khipu_decisions_supersede_gate
    AFTER UPDATE OF superseded_by ON decisions DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (NEW.superseded_by IS NOT NULL
          AND OLD.superseded_by IS DISTINCT FROM NEW.superseded_by
          AND NEW.supersede_source IN ('manual', 'agent'))
    EXECUTE FUNCTION khipu_supersede_gate();

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

-- ==== Hub refusals (section 6.1) ================================================
--
-- Every refusal is matched to episodes here, on the hub, in one transaction
-- under the per-identity advisory lock 0030's triggers already take
-- ('khipu-source:' || identity). An identity is bare hex (a source key or a
-- payload hash, as 0030 stores them) or a legacy 'legacy:<ts> US
-- md5(summary)' form (khipu.tombstones.hub_form is the Python side; a test
-- pins the two).
--
--   episode_identities    every identity each episode carries. New writes
--                         are recorded by the triggers below; history by
--                         the index pass (khipu provenance index).
--   fact_copy_ready       one row: 'indexing' until the pass has covered
--                         every id up to target_id (the highest id at this
--                         migration), then 'ready'. No replica copies
--                         facts before 'ready'.
--   forgotten_at_cutover  the episodes tombstoned at this migration: the
--                         only ones whose every identity the pass refuses.
--
-- Refusal and carrier writes need READ COMMITTED (the checks after a lock
-- need a fresh view). Rows of forgotten_sources, episode_identities and
-- forgotten_at_cutover are never updated or removed, and an episode that has
-- identity rows is never deleted.

CREATE TABLE IF NOT EXISTS episode_identities (
    identity    TEXT NOT NULL,
    episode_id  BIGINT NOT NULL,
    PRIMARY KEY (identity, episode_id)
);
CREATE INDEX IF NOT EXISTS idx_episode_identities_episode ON episode_identities (episode_id);

CREATE TABLE IF NOT EXISTS fact_copy_ready (
    one              BOOLEAN PRIMARY KEY DEFAULT true CHECK (one),
    state            TEXT NOT NULL CHECK (state IN ('indexing', 'ready')),
    target_id        BIGINT NOT NULL,
    indexed_through  BIGINT NOT NULL DEFAULT 0
);
INSERT INTO fact_copy_ready (state, target_id)
SELECT 'indexing', coalesce(max(id), 0) FROM episodes
ON CONFLICT (one) DO NOTHING;

CREATE TABLE IF NOT EXISTS forgotten_at_cutover (episode_id BIGINT PRIMARY KEY);
INSERT INTO forgotten_at_cutover (episode_id)
SELECT id FROM episodes WHERE deleted_at IS NOT NULL
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION khipu_require_read_committed()
RETURNS void LANGUAGE plpgsql AS $fn$
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'khipu: refusal and source writes need READ COMMITTED, not %',
            current_setting('transaction_isolation')
            USING ERRCODE = 'check_violation', CONSTRAINT = 'khipu_read_committed';
    END IF;
END
$fn$;

-- Sorted, the order every refusal and source writer takes them.
CREATE OR REPLACE FUNCTION khipu_lock_identities(identities TEXT[])
RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE
    k TEXT;
BEGIN
    FOR k IN SELECT DISTINCT x FROM unnest(identities) AS t(x)
             WHERE x IS NOT NULL AND x <> '' ORDER BY x
    LOOP
        PERFORM pg_advisory_xact_lock(hashtextextended('khipu-source:' || k, 0));
    END LOOP;
END
$fn$;

CREATE OR REPLACE FUNCTION khipu_payload_legacy(p JSONB)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS $fn$
    SELECT CASE WHEN jsonb_typeof(p -> 'ts') = 'string' AND jsonb_typeof(p -> 'summary') = 'string'
                THEN 'legacy:' || (p ->> 'ts') || chr(31) || md5(p ->> 'summary') END
$fn$;

CREATE OR REPLACE FUNCTION khipu_announced()
RETURNS TEXT[] LANGUAGE sql STABLE AS $fn$
    SELECT coalesce(string_to_array(
        nullif(current_setting('khipu.source_identities', true), ''), ','), ARRAY[]::TEXT[])
$fn$;

-- The identities SQL can compute from an episode row: its natural key and
-- the legacy form of its raw. The writer announces the rest.
CREATE OR REPLACE FUNCTION khipu_row_identities(
    p_raw JSONB, p_harness TEXT, p_session TEXT, p_range TEXT, p_window TEXT)
RETURNS TEXT[] LANGUAGE sql IMMUTABLE AS $fn$
    SELECT array_remove(ARRAY[
        khipu_natural_source_key(coalesce(p_harness, p_raw ->> 'harness'), p_session,
            coalesce(p_range, p_raw ->> 'transcript_range'),
            coalesce(p_window, p_raw ->> 'window_id'), p_raw ->> 'part'),
        khipu_payload_legacy(p_raw)], NULL)
$fn$;

CREATE OR REPLACE FUNCTION khipu_receipt_identities(entry JSONB)
RETURNS TEXT[] LANGUAGE sql IMMUTABLE AS $fn$
    SELECT array_remove(ARRAY[
        nullif(entry ->> 'source_id', ''),
        khipu_natural_source_key(
            entry -> 'payload' ->> 'harness',
            coalesce(entry -> 'payload' ->> 'session_id', entry ->> 'session_id'),
            entry -> 'payload' ->> 'transcript_range',
            entry -> 'payload' ->> 'window_id',
            entry -> 'payload' ->> 'part'),
        khipu_payload_legacy(entry -> 'payload')], NULL)
$fn$;

-- The receipts an update adds to raw.merged_from.
CREATE OR REPLACE FUNCTION khipu_new_receipts(old_raw JSONB, new_raw JSONB)
RETURNS SETOF JSONB LANGUAGE sql IMMUTABLE AS $fn$
    SELECT entry FROM jsonb_array_elements(
        CASE WHEN jsonb_typeof(new_raw -> 'merged_from') = 'array'
             THEN new_raw -> 'merged_from' ELSE '[]'::jsonb END) AS t(entry)
    WHERE jsonb_typeof(entry) = 'object'
      AND NOT (CASE WHEN jsonb_typeof(old_raw -> 'merged_from') = 'array'
                    THEN old_raw -> 'merged_from' ELSE '[]'::jsonb END) @> jsonb_build_array(entry)
$fn$;

-- Every identity the receipts already in a raw name (an insert that
-- arrives with merged_from).
CREATE OR REPLACE FUNCTION khipu_raw_receipt_identities(p_raw JSONB)
RETURNS TEXT[] LANGUAGE sql IMMUTABLE AS $fn$
    SELECT coalesce(array_agg(x), ARRAY[]::TEXT[])
    FROM khipu_new_receipts('{}'::jsonb, p_raw) AS r(entry),
         unnest(khipu_receipt_identities(entry)) AS t(x)
$fn$;

CREATE OR REPLACE FUNCTION khipu_record_identities(p_episode BIGINT, identities TEXT[])
RETURNS void LANGUAGE sql AS $fn$
    INSERT INTO episode_identities (identity, episode_id)
    SELECT DISTINCT x, p_episode FROM unnest(identities) AS t(x)
    WHERE x IS NOT NULL AND x <> '' ORDER BY 1
    ON CONFLICT DO NOTHING
$fn$;

CREATE OR REPLACE FUNCTION khipu_refuse_unannounced()
RETURNS void LANGUAGE plpgsql AS $fn$
BEGIN
    IF cardinality(khipu_announced()) = 0 THEN
        RAISE EXCEPTION 'khipu: an episode write must announce its source identities (khipu.source_identities)'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'khipu_unannounced';
    END IF;
END
$fn$;

CREATE OR REPLACE FUNCTION khipu_refuse_tombstoned(p_episode BIGINT)
RETURNS void LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'khipu: tombstoned target: episode % is forgotten; no new source attaches to it', p_episode
        USING ERRCODE = 'check_violation', CONSTRAINT = 'khipu_tombstoned_target';
END
$fn$;

-- 0030's episode insert check, now READ COMMITTED, announced, and with the
-- legacy form of the row's raw and every receipt its raw already holds.
-- Until history is indexed a tombstone still holds its legacy key (its
-- refusals may not all be rows yet), so such an insert is skipped, as the
-- old unique key skipped it; after that the refusals alone decide.
CREATE OR REPLACE FUNCTION khipu_episodes_forgotten_insert()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    PERFORM khipu_require_read_committed();
    PERFORM khipu_refuse_unannounced();
    PERFORM khipu_refuse_forgotten(
        khipu_row_identities(NEW.raw, NEW.harness, NEW.session_id, NEW.transcript_range, NEW.window_id)
        || khipu_raw_receipt_identities(NEW.raw) || khipu_announced());
    IF NOT EXISTS (SELECT 1 FROM fact_copy_ready WHERE indexed_through >= target_id)
       AND EXISTS (SELECT 1 FROM episodes WHERE ts = NEW.ts AND md5(summary) = md5(NEW.summary)
                   AND deleted_at IS NOT NULL) THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END
$fn$;

-- 0030's update check, plus: a merge (a receipt added to raw.merged_from)
-- is READ COMMITTED, announced, never into a tombstoned episode (D1: under
-- READ COMMITTED a merge queued behind the forget sees its tombstone here),
-- and refused for a forgotten legacy form too.
CREATE OR REPLACE FUNCTION khipu_episodes_forgotten_update()
RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    found TEXT[] := ARRAY[]::TEXT[];
    entry JSONB;
    n INTEGER := 0;
BEGIN
    IF OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS NULL THEN
        RAISE EXCEPTION 'khipu: forgotten episode % cannot be restored', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    -- A tombstoned episode's stored payloads are frozen (D1): the index
    -- pass and refuse() derive its identities from them.
    IF OLD.deleted_at IS NOT NULL
       AND (NEW.raw IS DISTINCT FROM OLD.raw OR NEW.harness IS DISTINCT FROM OLD.harness
            OR NEW.session_id IS DISTINCT FROM OLD.session_id
            OR NEW.transcript_range IS DISTINCT FROM OLD.transcript_range
            OR NEW.window_id IS DISTINCT FROM OLD.window_id) THEN
        PERFORM khipu_refuse_tombstoned(OLD.id);
    END IF;
    IF (NEW.raw -> 'merged_from') IS DISTINCT FROM (OLD.raw -> 'merged_from') THEN
        FOR entry IN SELECT khipu_new_receipts(OLD.raw, NEW.raw) LOOP
            n := n + 1;
            found := found || khipu_receipt_identities(entry);
        END LOOP;
        IF n > 0 THEN
            PERFORM khipu_require_read_committed();
            IF OLD.deleted_at IS NOT NULL THEN
                PERFORM khipu_refuse_tombstoned(OLD.id);
            END IF;
            PERFORM khipu_refuse_unannounced();
            PERFORM khipu_refuse_forgotten(found || khipu_announced());
        END IF;
    END IF;
    RETURN NEW;
END
$fn$;

CREATE OR REPLACE FUNCTION khipu_episodes_identities_insert()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    PERFORM khipu_record_identities(NEW.id,
        khipu_row_identities(NEW.raw, NEW.harness, NEW.session_id, NEW.transcript_range, NEW.window_id)
        || khipu_raw_receipt_identities(NEW.raw) || khipu_announced());
    RETURN NULL;
END
$fn$;

CREATE OR REPLACE FUNCTION khipu_episodes_identities_merge()
RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    found TEXT[] := ARRAY[]::TEXT[];
    entry JSONB;
BEGIN
    FOR entry IN SELECT khipu_new_receipts(OLD.raw, NEW.raw) LOOP
        found := found || khipu_receipt_identities(entry);
    END LOOP;
    IF cardinality(found) > 0 THEN
        PERFORM khipu_record_identities(NEW.id, found || khipu_announced());
    END IF;
    RETURN NULL;
END
$fn$;

CREATE OR REPLACE FUNCTION khipu_episodes_delete_gate()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF EXISTS (SELECT 1 FROM episode_identities WHERE episode_id = OLD.id) THEN
        RAISE EXCEPTION 'khipu: episode % carries source identities and cannot be deleted', OLD.id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'khipu_identity_rows';
    END IF;
    RETURN OLD;
END
$fn$;

-- 0030's source row check, plus the episode's row lock after the identity
-- locks (N5: no source commits into an episode a forget holds) and D1.
CREATE OR REPLACE FUNCTION khipu_episode_sources_forgotten()
RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    gone TIMESTAMPTZ;
BEGIN
    PERFORM khipu_require_read_committed();
    PERFORM khipu_refuse_forgotten(ARRAY[NEW.source_key, NEW.payload_sha256]);
    SELECT deleted_at INTO gone FROM episodes WHERE id = NEW.episode_id FOR SHARE;
    IF gone IS NOT NULL THEN
        PERFORM khipu_refuse_tombstoned(NEW.episode_id);
    END IF;
    RETURN NEW;
END
$fn$;

CREATE OR REPLACE FUNCTION khipu_episode_sources_identities()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    PERFORM khipu_record_identities(NEW.episode_id, ARRAY[NEW.source_key, NEW.payload_sha256]);
    RETURN NULL;
END
$fn$;

-- D1's one exception: the index pass (khipu.provenance_index on) records
-- the identities a pre-migration tombstoned episode's stored payloads hold.
CREATE OR REPLACE FUNCTION khipu_episode_identities_insert()
RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    gone TIMESTAMPTZ;
BEGIN
    PERFORM khipu_require_read_committed();
    PERFORM khipu_lock_identities(ARRAY[NEW.identity]);
    SELECT deleted_at INTO gone FROM episodes WHERE id = NEW.episode_id FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'khipu: no episode %', NEW.episode_id USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF gone IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM episode_identities
                       WHERE identity = NEW.identity AND episode_id = NEW.episode_id)
       AND NOT (coalesce(current_setting('khipu.provenance_index', true), '') = 'on'
                AND NEW.episode_id <= (SELECT target_id FROM fact_copy_ready)) THEN
        PERFORM khipu_refuse_tombstoned(NEW.episode_id);
    END IF;
    RETURN NEW;
END
$fn$;

CREATE OR REPLACE FUNCTION khipu_forgotten_sources_insert()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    PERFORM khipu_require_read_committed();
    PERFORM khipu_lock_identities(ARRAY[NEW.identity]);
    RETURN NEW;
END
$fn$;

-- The carrier gate (deferred to COMMIT): no live episode recorded under a
-- refused identity. With the identity locks, a refusal and a carrier of the
-- same identity are never both uncommitted: the second waits for the first.
CREATE OR REPLACE FUNCTION khipu_carrier_gate_refusal()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF EXISTS (SELECT 1 FROM episode_identities ei
               JOIN episodes e ON e.id = ei.episode_id AND e.deleted_at IS NULL
               WHERE ei.identity = NEW.identity) THEN
        RAISE EXCEPTION 'khipu: forgotten source % is still carried by a live episode', NEW.identity
            USING ERRCODE = 'check_violation', CONSTRAINT = 'khipu_live_carrier';
    END IF;
    RETURN NULL;
END
$fn$;

CREATE OR REPLACE FUNCTION khipu_carrier_gate_identity()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF EXISTS (SELECT 1 FROM forgotten_sources WHERE identity = NEW.identity)
       AND EXISTS (SELECT 1 FROM episodes WHERE id = NEW.episode_id AND deleted_at IS NULL) THEN
        RAISE EXCEPTION 'khipu: live episode % carries forgotten source %', NEW.episode_id, NEW.identity
            USING ERRCODE = 'check_violation', CONSTRAINT = 'khipu_live_carrier';
    END IF;
    RETURN NULL;
END
$fn$;

CREATE OR REPLACE FUNCTION khipu_refuse_change()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'khipu: % on % is refused (%)', TG_OP, TG_TABLE_NAME, TG_ARGV[0]
        USING ERRCODE = 'check_violation', CONSTRAINT = 'khipu_append_only';
END
$fn$;

CREATE OR REPLACE FUNCTION khipu_copy_ready_gate()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.state = 'ready' AND NEW.indexed_through < NEW.target_id THEN
        RAISE EXCEPTION 'khipu: copies cannot be ready before the index pass covers episode % (it is at %)',
            NEW.target_id, NEW.indexed_through
            USING ERRCODE = 'check_violation', CONSTRAINT = 'khipu_not_indexed';
    END IF;
    RETURN NEW;
END
$fn$;

-- The change token: one fact_changes row (via 'copy') per transaction that
-- writes a table a replica copy reads; its xid marks the change.
CREATE OR REPLACE FUNCTION khipu_copy_token()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM fact_changes WHERE xid = pg_current_xact_id() AND via = 'copy') THEN
        INSERT INTO fact_changes (via) VALUES ('copy');
    END IF;
    RETURN NULL;
END
$fn$;

DROP TRIGGER IF EXISTS khipu_episodes_identities_insert ON episodes;
CREATE TRIGGER khipu_episodes_identities_insert
    AFTER INSERT ON episodes
    FOR EACH ROW EXECUTE FUNCTION khipu_episodes_identities_insert();

DROP TRIGGER IF EXISTS khipu_episodes_identities_merge ON episodes;
CREATE TRIGGER khipu_episodes_identities_merge
    AFTER UPDATE OF raw ON episodes
    FOR EACH ROW
    WHEN ((OLD.raw -> 'merged_from') IS DISTINCT FROM (NEW.raw -> 'merged_from'))
    EXECUTE FUNCTION khipu_episodes_identities_merge();

DROP TRIGGER IF EXISTS khipu_episodes_delete_gate ON episodes;
CREATE TRIGGER khipu_episodes_delete_gate
    BEFORE DELETE ON episodes
    FOR EACH ROW EXECUTE FUNCTION khipu_episodes_delete_gate();

DROP TRIGGER IF EXISTS khipu_episode_sources_identities ON episode_sources;
CREATE TRIGGER khipu_episode_sources_identities
    AFTER INSERT ON episode_sources
    FOR EACH ROW EXECUTE FUNCTION khipu_episode_sources_identities();

DROP TRIGGER IF EXISTS khipu_episode_sources_frozen ON episode_sources;
CREATE TRIGGER khipu_episode_sources_frozen
    BEFORE UPDATE ON episode_sources
    FOR EACH ROW
    WHEN (NEW.source_key IS DISTINCT FROM OLD.source_key
          OR NEW.payload_sha256 IS DISTINCT FROM OLD.payload_sha256
          OR NEW.episode_id IS DISTINCT FROM OLD.episode_id)
    EXECUTE FUNCTION khipu_refuse_change('a source receipt keeps its identity and its episode');

-- The legacy key is unique among live episodes only: once history is
-- indexed a capture whose identities are not refused lands beside a
-- tombstone holding the same key (the refusal table is the one authority).
CREATE UNIQUE INDEX IF NOT EXISTS uq_episodes_ts_summary_md5_live
    ON episodes (ts, md5(summary)) WHERE deleted_at IS NULL;
DROP INDEX IF EXISTS uq_episodes_ts_summary_md5;
CREATE INDEX IF NOT EXISTS idx_episodes_ts_summary_md5 ON episodes (ts, md5(summary));

DROP TRIGGER IF EXISTS khipu_episode_identities_insert ON episode_identities;
CREATE TRIGGER khipu_episode_identities_insert
    BEFORE INSERT ON episode_identities
    FOR EACH ROW EXECUTE FUNCTION khipu_episode_identities_insert();

DROP TRIGGER IF EXISTS khipu_forgotten_sources_insert ON forgotten_sources;
CREATE TRIGGER khipu_forgotten_sources_insert
    BEFORE INSERT ON forgotten_sources
    FOR EACH ROW EXECUTE FUNCTION khipu_forgotten_sources_insert();

DROP TRIGGER IF EXISTS khipu_forgotten_sources_carrier ON forgotten_sources;
CREATE CONSTRAINT TRIGGER khipu_forgotten_sources_carrier
    AFTER INSERT ON forgotten_sources DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION khipu_carrier_gate_refusal();

DROP TRIGGER IF EXISTS khipu_episode_identities_carrier ON episode_identities;
CREATE CONSTRAINT TRIGGER khipu_episode_identities_carrier
    AFTER INSERT ON episode_identities DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION khipu_carrier_gate_identity();

DROP TRIGGER IF EXISTS khipu_forgotten_sources_append_only ON forgotten_sources;
CREATE TRIGGER khipu_forgotten_sources_append_only
    BEFORE UPDATE OR DELETE ON forgotten_sources
    FOR EACH ROW EXECUTE FUNCTION khipu_refuse_change('a refusal is never changed or removed');

DROP TRIGGER IF EXISTS khipu_episode_identities_append_only ON episode_identities;
CREATE TRIGGER khipu_episode_identities_append_only
    BEFORE UPDATE OR DELETE ON episode_identities
    FOR EACH ROW EXECUTE FUNCTION khipu_refuse_change('an episode keeps every identity it carried');

DROP TRIGGER IF EXISTS khipu_fact_copy_ready_gate ON fact_copy_ready;
CREATE TRIGGER khipu_fact_copy_ready_gate
    BEFORE UPDATE ON fact_copy_ready
    FOR EACH ROW EXECUTE FUNCTION khipu_copy_ready_gate();

DROP TRIGGER IF EXISTS khipu_fact_copy_ready_keep ON fact_copy_ready;
CREATE TRIGGER khipu_fact_copy_ready_keep
    BEFORE DELETE ON fact_copy_ready
    FOR EACH ROW EXECUTE FUNCTION khipu_refuse_change('the readiness row is never removed');

-- After the fill above, in this transaction: the frozen set takes no change.
DROP TRIGGER IF EXISTS khipu_forgotten_at_cutover_frozen ON forgotten_at_cutover;
CREATE TRIGGER khipu_forgotten_at_cutover_frozen
    BEFORE INSERT OR UPDATE OR DELETE ON forgotten_at_cutover
    FOR EACH ROW EXECUTE FUNCTION khipu_refuse_change('the forgets at the cutover are frozen');

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['forgotten_sources', 'episode_identities', 'forgotten_at_cutover', 'fact_copy_ready'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS khipu_%s_truncate ON %I', t, t);
        EXECUTE format('CREATE TRIGGER khipu_%s_truncate BEFORE TRUNCATE ON %I '
                       'FOR EACH STATEMENT EXECUTE FUNCTION khipu_refuse_change(%L)',
                       t, t, 'never truncated');
    END LOOP;
    FOREACH t IN ARRAY ARRAY['commitments', 'decisions', 'decision_links', 'deliverables',
                             'memory_embeddings', 'forgotten_sources'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS khipu_%s_copy_token ON %I', t, t);
        EXECUTE format('CREATE TRIGGER khipu_%s_copy_token AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE '
                       'ON %I FOR EACH STATEMENT EXECUTE FUNCTION khipu_copy_token()', t, t);
    END LOOP;
END
$$;

DROP TRIGGER IF EXISTS khipu_episodes_copy_token ON episodes;
CREATE TRIGGER khipu_episodes_copy_token
    AFTER UPDATE OF deleted_at ON episodes
    FOR EACH STATEMENT EXECUTE FUNCTION khipu_copy_token();

INSERT INTO schema_migrations (version, note)
VALUES (
    '0047_fact_provenance',
    'Fact provenance: fact_claims, fact_changes (+ floor), fact_gaps, provenance_projects; legacy claims proven by the writer inventory (the rest legacy_unproven); initial projection by khipu_project_fact(); deferred gates refuse wording writes outside the projector and forgets of claimed episodes (SQLSTATE 08K01), and claims for forgotten episodes'
)
ON CONFLICT (version) DO NOTHING;
