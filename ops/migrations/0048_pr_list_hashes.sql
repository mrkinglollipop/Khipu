-- Keep khipu_text_prs aligned with close_rules.parse_prs: a #N after the
-- word PR counts only when it is in a comma / "and" / "&" list immediately
-- after that word (not a later "issue #44" in the same clause).

CREATE OR REPLACE FUNCTION khipu_text_prs(t TEXT)
RETURNS INTEGER[] LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE
    nb CONSTANT TEXT := '(?<![^\x21-\x2f\x3a-\x40\x5b-\x5e\x60\x7b-\x7e[:space:]])';
    na CONSTANT TEXT := '(?![^\x21-\x2f\x3a-\x40\x5b-\x5e\x60\x7b-\x7e[:space:]])';
    hb CONSTANT TEXT := '(?<![^\x21-\x25\x27-\x2f\x3a-\x40\x5b-\x5e\x60\x7b-\x7e[:space:]])';
    word CONSTANT TEXT := '(?:prs?|pull[[:space:]]+requests?)';
    m TEXT[];
    h TEXT[];
    run TEXT;
    found INTEGER[] := '{}';
BEGIN
    IF t IS NULL THEN
        RETURN found;
    END IF;
    FOR m IN SELECT regexp_matches(t, nb || word || '[[:space:]]*#?[[:space:]]*([0-9]{1,7})' || na, 'gi') LOOP
        found := found || m[1]::INTEGER;
    END LOOP;
    FOR m IN SELECT regexp_matches(t, nb || word || na || '([^.;:\n]*)', 'gi') LOOP
        run := (regexp_match(m[1], '^\s*(#([0-9]{1,7})(\s*(,|&|and)\s*#([0-9]{1,7}))*)', 'i'))[1];
        IF run IS NOT NULL THEN
            FOR h IN SELECT regexp_matches(run, hb || '#([0-9]{1,7})' || na, 'g') LOOP
                found := found || h[1]::INTEGER;
            END LOOP;
        END IF;
    END LOOP;
    FOR m IN SELECT regexp_matches(t, 'github\.com/([a-z0-9][a-z0-9_.-]*/[a-z0-9_.-]+?)/pull/([0-9]{1,7})' || na, 'gi') LOOP
        found := found || m[2]::INTEGER;
    END LOOP;
    RETURN ARRAY(SELECT DISTINCT n FROM unnest(found) AS n WHERE n > 0 ORDER BY n);
END
$fn$;
