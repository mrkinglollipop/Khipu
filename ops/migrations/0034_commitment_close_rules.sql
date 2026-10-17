-- Auto-close by pull request and by decision, and who closed an item.
-- Scope: docs/plans/2026-10-16-owed-and-ledger-ssot.md (design (b), slice 2,
-- with the owner's decision 1).
--
-- Why: nothing closed an item when its pull request merged, and a hand close
-- recorded no reason and no author (367 closed rows with no reason on one
-- busy project).
--
--   prs                 the pull request numbers the item names (PR #N,
--                       pull request N, #N next to "PR", a GitHub pull link),
--                       parsed at insert. NULL means not parsed yet: the
--                       nightly backfill (khipu.close_rules.backfill_prs)
--                       fills those; an empty array means none named.
--   closed_by           who closed it: 'Khipu, by rule', 'you, in the app' or
--                       'a conversation'. NULL for closes from before this.
--   close_rule_key      the rule and evidence that closed it, such as
--                       'pr:366=merged' or 'decision:79627'. NULL for a hand
--                       close.
--   close_evidence      what the rule saw (each PR's state and where it came
--                       from, or the decision and how it matched).
--   reopened_rule_keys  every rule key this item was reopened from. A rule
--                       never closes it again with a key in this list.
--   settle_suggestion   a looser decision match, shown as "Looks settled by
--                       decision N"; never closes the item on its own.
--   pr_check            the last PR check of an item still open: each named
--                       PR's state and source, and why the rest are not
--                       known yet (GitHub off, gh missing, repo not known).
--
-- Locks. Every ALTER adds a nullable column or one with a constant default,
-- catalog-only (no table rewrite), under a 5 second lock_timeout so a busy
-- hub fails the step instead of queueing every capture behind it.
--
-- Additive and safe to run twice. A hub without this migration keeps
-- working: writers and readers check for the columns first, and the close
-- rules skip and say why. Reverse with ALTER TABLE commitments DROP COLUMN
-- for each column above, and deleting the 0034 row from schema_migrations.

SET LOCAL lock_timeout = '5s';

ALTER TABLE commitments ADD COLUMN IF NOT EXISTS prs INTEGER[];
ALTER TABLE commitments ADD COLUMN IF NOT EXISTS closed_by TEXT;
ALTER TABLE commitments ADD COLUMN IF NOT EXISTS close_rule_key TEXT;
ALTER TABLE commitments ADD COLUMN IF NOT EXISTS close_evidence JSONB;
ALTER TABLE commitments ADD COLUMN IF NOT EXISTS reopened_rule_keys TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE commitments ADD COLUMN IF NOT EXISTS settle_suggestion JSONB;
ALTER TABLE commitments ADD COLUMN IF NOT EXISTS pr_check JSONB;

COMMENT ON COLUMN commitments.prs IS
    'Pull request numbers the item names, parsed at insert (khipu.close_rules.parse_prs). NULL: not parsed yet (the nightly backfills); empty: none named.';
COMMENT ON COLUMN commitments.closed_by IS
    'Who closed it: ''Khipu, by rule'', ''you, in the app'' or ''a conversation''. NULL for closes from before migration 0034.';
COMMENT ON COLUMN commitments.close_rule_key IS
    'The rule and evidence that closed it (''pr:366=merged'', ''decision:79627''). NULL for a hand close.';
COMMENT ON COLUMN commitments.close_evidence IS
    'What the closing rule saw: each PR''s state and source, or the decision and how it matched.';
COMMENT ON COLUMN commitments.reopened_rule_keys IS
    'Rule keys this item was reopened from; a rule never closes it again with one of these keys.';
COMMENT ON COLUMN commitments.settle_suggestion IS
    'A looser decision match ({decision_id, text, score}), shown as "Looks settled by decision N". Never closes the item.';
COMMENT ON COLUMN commitments.pr_check IS
    'The last PR check of an open item: {states: {pr: words}, waiting: {pr: why not known or not done}}.';

INSERT INTO schema_migrations (version, note)
VALUES (
    '0034_commitment_close_rules',
    'commitments.prs, closed_by, close_rule_key, close_evidence, reopened_rule_keys, settle_suggestion, pr_check (auto-close by PR and by decision)'
)
ON CONFLICT (version) DO NOTHING;
