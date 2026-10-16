-- Owner writes applied exactly once: the hub's record of every owner write
-- (owed close, reopen or snooze; decision supersede, retract, restore, confirm
-- or reject) it has applied, by the id the writing Mac gave the write.
-- Scope: docs/plans/2026-10-16-offline-and-self-healing.md (design C,
-- decision 4: owner writes made offline are queued and replayed).
--
-- Why: a Mac that loses the hub's reply to a write cannot tell from the rows
-- whether the write landed (a snooze already in the future may be an older
-- one; a closed item may have been closed by someone else). Each write now
-- carries a unique id; the hub inserts it here in the same transaction as the
-- write, refusing to apply an id it already holds, and a replay asks this
-- table whether its id was applied before sending again.
--
-- Additive only: one new table. No existing table, column or row changes, and
-- creating a new table takes no lock any writer holds. lock_timeout still
-- bounds the schema_migrations row, so a long transaction there fails this
-- migration instead of queueing behind it; khipu migrate retries it. Safe to
-- run twice. Reverse with DROP TABLE owner_writes_applied and deleting the
-- 0031 row from schema_migrations; queued writes then cannot be checked and
-- the code refuses to replay them until the table is back.

SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS owner_writes_applied (
    write_id    TEXT PRIMARY KEY,
    tool        TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE owner_writes_applied IS
    'Ids of owner writes (owed and decision updates) the hub applied, inserted in the same transaction as the write. A replay asks here whether its id landed; an id already here is never applied again.';

INSERT INTO schema_migrations (version, note)
VALUES (
    '0031_owner_writes_applied',
    'owner_writes_applied (write_id PK, tool, applied_at): owner writes recorded in the same transaction as the write, so a replay applies each exactly once'
)
ON CONFLICT (version) DO NOTHING;
