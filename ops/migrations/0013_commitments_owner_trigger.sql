-- Khipu commitments: owner + future_trigger (2026-09-04, second pass).
--
-- The first quality pass cut 326 open commitments to 29, but about half of
-- the survivors were still the ASSISTANT's own in-session promises from
-- sessions that had already ended ("Reply with the SHA each PR now points
-- at", "Tell user the moment their app relaunches"). Owed is only worth
-- opening if what is in it is genuinely still owed.
--
-- Two fields decide whether a commitment outlives its session:
--   * owner            — 'user' or 'assistant' (the column already exists;
--                        khipu.commitments.resolve_owner now writes one of
--                        those two values instead of whatever the model said).
--                        The desktop's "Needs you" section is owner = 'user'.
--   * future_trigger   — the text carries an explicit CROSS-SESSION condition
--                        ("when Matt says ...", "next session", "after the
--                        wave merges", "if attempt six ..."). An assistant
--                        commitment with no future trigger is closed
--                        ('session-ended') when its own session ends.
--
-- Detection is deterministic in khipu.commitments (regex); the extractor
-- prompt asks the model for both fields, but the regex result wins, so a
-- mislabelled item cannot keep noise alive. Every reader derives the two
-- fields from the text when this column is absent, so an un-migrated hub
-- behaves identically — the column exists so the desktop/gateway can filter
-- and sort in SQL.

ALTER TABLE commitments ADD COLUMN IF NOT EXISTS future_trigger boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_commitments_open_owner
    ON commitments (owner, future_trigger) WHERE status = 'open';

COMMENT ON COLUMN commitments.future_trigger IS
    'True when the commitment names an explicit cross-session condition ("when Matt says ...", "next session", "after X merges"). An assistant commitment with future_trigger = false is closed with close_reason ''session-ended'' when its opening session ends.';
COMMENT ON COLUMN commitments.owner IS
    'Resolved owner: ''user'' (the user must decide/provide/approve/do it, or it is a question for them) or ''assistant''. khipu.commitments.resolve_owner decides it deterministically from the text; the desktop''s "Needs you" section is owner = ''user''.';

INSERT INTO schema_migrations (version, note)
VALUES (
    '0013_commitments_owner_trigger',
    'commitments: future_trigger column (+ open owner index) — assistant commitments with no cross-session trigger close at sessionend'
)
ON CONFLICT (version) DO NOTHING;
