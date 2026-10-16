# Capture identity: immutable source identities for episodes

Approved in principle, Matt 2026-10-11, as the first of four backlog decisions from the 2026-10-10 audit
("If you think this is a good idea and it's reliable, then do it"). This page is the scope; a build session
reads it first and needs nothing else from the conversation that produced it. Audit IDs: CAP-9, RSN-6 (P0);
CAP-14, ARCH-10, CAP-22, ARCH-8 (P1); review RV1-01 (P0).

## Status

| Phase | What | State |
|---|---|---|
| 1 | `episode_sources` table, backfill, dual write; nothing reads it yet | merged (PR #199) |
| 2 | Every identity lookup reads sources; `write_pg` returns the episode id | built, in review (branch feat/capture-identity-p2) |
| 3 | Uniqueness moves to the source key; the three backed-out fixes return | open |

One PR per phase, merged alone and soaked on main (hooks are live from the main checkout) before the next
starts. Update this table as phases land.

## The defect, in one paragraph

An episode's only durable identity is `uq_episodes_ts_summary_md5` on `(ts, md5(summary))`
(`ops/migrations/0003_reconcile_upsert.sql:20`). Both halves are wrong for the job: `ts` is minted per job
at second precision (`session_capture.py`, `_mint_ts`), so two sessions captured in the same second with the
same short summary are one row and the second session's evidence is dropped with a success exit (CAP-9);
and `summary` is mutable, rewritten by dedup merges and edits, so the file mirror, `reconcile_memory_root`,
`drift.py` and `forget.py` all compute an identity that no longer matches the row and either resurrect the
original line (ARCH-10, RSN-6) or cannot find the outbox copy to erase. Three audit fixes were backed out
because they needed an identity the schema does not have: refusing a replay whose evidence differs
(`capture.py:716`, now logged and acknowledged), erasing outbox copies on forget (`forget.py:285`, now
report-only), and acknowledging a capture on outbox enqueue alone for hub-only installs (RV1-01, CAP-22,
ARCH-8: two jobs with the same `(ts, summary)` overwrote each other's outbox file).

## Source facts (verified against main `1cf486b`; paths under `packages/cli/`)

- Hub `episodes`: `id BIGSERIAL` primary key; identity columns `session_id`, `harness`, `transcript_range`,
  `window_id` (migrations 0008, 0016); `part` lives only in `raw->>'part'`; merges append to
  `raw.merged_from[]` (`{source_id, session_id, ts, matched_via, score, payload}`) and keep the target's
  original `ts`/`summary` under `raw.ts`/`raw.summary` (`capture.py:250`, `_merge_into_episode`).
- Payload identity is already stable on the way in: `enqueue_job` derives
  `window_id = sha256("{harness}:{sid}:{off_before}:{new_off}")` and `part = "i/n"`; drain sets
  `session_id = "{harness}:{sid}"` (`session_capture.py` ~1519, ~2045). The audit's "drainer omits part"
  note is stale.
- Writer: `mirror.py:344` `_upsert_episode` is `INSERT ... ON CONFLICT DO NOTHING`, returns a boolean;
  `write_pg` (`capture.py` ~592) then re-selects the row by `(ts, md5(summary))` (`:700`, `:734`).
- Other readers keyed on `(ts, md5(summary))`: `embed.py:1830` `embed_on_capture`; `mcp_server.py:1178`
  `_captured_episode_id`; `drift.py:44`; `mirror.py` `sync_recent_episodes` (~514) and
  `reconcile_memory_root` (~581); `forget.py:74` and `_line_matches` (`:172`).
- Exact-window dedup (`capture.py:69`, `_dedup_exact_window`) already matches on
  `(harness, session_id, transcript_range)` plus `window_id` and `raw->>'part'`, and honours a
  `raw.merged_from[].source_id = sha256(payload)` receipt.
- Outbox (`outbox.py`): file name is `{compact ts}-{md5(summary)[:10]}.json` (`:57`); `enqueue` falls back to
  `<sha256(payload)>.json` on a name collision with a different payload; `drain` deletes the file after
  `write_pg` accepts, including `inserted=False`.
- Embeddings (`memory_embeddings`, PK `(profile, kind, ref, chunk_idx)`, `ref = episodes.id`) and the sqlite
  replica (`hub_snapshot.py`, `episodes.id` primary key) are already id-keyed. They need no identity change.
- `CaptureIdentityConflict` (`capture.py:524`) is defined and caught (`:893`) but never raised.
- Migrations: `ops/migrations/NNNN_name.sql`, latest `0026_library.sql`; applied by `khipu migrate`, join
  import, setup, the local Postgres component and the desktop `khipu_migrate` command. The gateway image
  and the running desktop app never run migrations. Every writer is pre-migration tolerant through
  `has_columns` probes; a new table must get the same treatment because other people's hubs migrate when
  their app does, not when main merges.

## Decisions

1. **A source is the unit of identity, not a summary.** Every capture carries a `source_key`:
   `sha256(harness ‖ session_id ‖ transcript_range ‖ window_id ‖ part)` when `session_id` and
   `transcript_range` are present, otherwise `sha256(canonical payload)` (gateway summary-only captures, the
   legacy writer). The key is computed once, in one function, used by capture, outbox, mirror, reconcile,
   forget and embed.
2. **New table `episode_sources`, one row per source contribution.** Columns: `source_key TEXT PRIMARY KEY`,
   `episode_id BIGINT NOT NULL REFERENCES episodes(id)`, `role TEXT` (`origin` or `merged`), the five source
   fields above, `ts TIMESTAMPTZ` and `summary_md5 TEXT` (the legacy identity as first written, never
   updated), `payload_sha256 TEXT`, `created_at`, `deleted_at`. A non-unique index on `(ts, summary_md5)`
   serves the file mirror and reconcile; an index on `episode_id` serves forget.
3. **Replay semantics.** Same `source_key` and same `payload_sha256`: acknowledge, no change. Same
   `source_key`, different payload (a retry that re-synthesised the same transcript span): keep the stored
   episode, record the new `payload_sha256` in the source row's history, acknowledge, log. Different
   `source_key` with the same `(ts, md5(summary))`: a new episode. The second case replaces today's
   "identity differs; treating as the same episode" log; the third case is the CAP-9 fix.
4. **Keep first, never rewrite evidence.** A merge appends a `merged` source row; it never changes an
   existing source row. `raw.merged_from[]` stays as the human-readable record until phase 3 proves the table
   covers every reader, then it stops being written (not removed from old rows).
5. **The replica does not change.** Offline forget still keys by episode id; alias lookup offline is out of
   scope.
6. **Backfill before any uniqueness change** (the reports' order). The unique constraint on
   `(ts, md5(summary))` is dropped only in phase 3, after a full soak of phases 1 and 2.

## Phase 1: table, backfill, dual write

Inspect `mirror.py::_upsert_episode`, `capture.py::write_pg`, `outbox.py`, `ops/migrations/0026_library.sql`
and `tests/test_pg_scratch.py` before writing anything.

1. Migration `0027_episode_sources.sql`: the table and indexes from decision 2, plus a backfill in the same
   file: one `origin` row per live episode from its columns and `raw` (`part` from `raw->>'part'`;
   `ts`/`summary_md5` from `raw.ts`/`raw.summary` when present, else the row's own), and one `merged` row per
   `raw.merged_from[]` entry (its `source_id` becomes `payload_sha256`; `source_key` derived from the stored
   payload). Idempotent: `ON CONFLICT (source_key) DO NOTHING`. Must run inside the migration's single
   transaction on a hub of the current size in well under a minute; measure on a scratch database first.
2. `source_key()` in one module, with the canonical form written down in its docstring and a test that pins
   example inputs to fixed hashes.
3. `write_pg` inserts the source row after the episode row, in the same transaction, when the table exists
   (`has_table` probe cached like `has_columns`). Merges insert a `merged` row. Nothing reads the table yet.
4. Tests: scratch-PG migration applies on an empty hub and on a hub seeded with merged and edited episodes;
   backfill counts match; a second run changes nothing.

Stop-conditions: if the backfill cannot derive a `source_key` for a class of historical rows (for example
rows with no `session_id` and no stored payload), fall back to `sha256(id)` for those rows and say so in the
PR; do not skip them. If the migration needs more than one transaction, stop and redesign the backfill as a
separate `khipu migrate` step rather than splitting the file.

Build notes (phase 1, 2026-10-15):

- Measured on a scratch database seeded with 13,300 episodes (4 KB raw each, 2,660 merged entries): backfill
  0.43 s, rerun 0.30 s. Read-only counts on this Mac's hub the same day: 19,303 live episodes; 11,468 with a
  natural key, 7,835 without; 85 with `raw.merged_from` holding 104 entries.
- Fallback classes, none skipped: origin rows with no natural key get the episode key
  `sha256("khipu-episode-v1" US id)`, because their `raw` was rewritten after the write and no longer hashes
  to the payload (7,835 here); `payload_sha256` is NULL on every backfilled origin row (not known).
  782 live episodes share a natural key with an older episode (764 have `window_id` but no `part`, 18 have
  neither, 2026-09-05 to 2026-10-11, all with distinct summaries); the older keeps the key and the newer
  gets its episode key. Phase 3's collision gate must decide what these are before uniqueness moves; the
  cause was not investigated. 26 merged entries carry neither a payload nor a `source_id` and are keyed
  `sha256("khipu-merged-v1" US episode id US position)`.
- Origin `ts` comes from `episodes.ts`, which no writer updates after insert, not from `raw.ts`.
- The dual write lives in `mirror._upsert_episode` (so `write_pg`, the file mirror and reconcile all record
  origin rows) and `capture._merge_into_episode`; `write_pg` snapshots the source identity before ingestion
  rewrites topics, tags and scope. An origin key already held by another episode falls back to the episode
  key, as in the backfill. The source row is fail-open under its own savepoint.

Deploy order for this Mac: `khipu migrate` on the hub, then deploy the gateway, then merge (the hooks go
live at merge). The gateway must tolerate the table being absent regardless.

## Phase 2: readers move, `write_pg` returns the id

Inspect every `(ts, md5(summary))` reader listed under source facts before touching one.

1. `write_pg` returns `(episode_id, inserted, source_key)`; `embed_on_capture`, `_captured_episode_id` and
   the capture hook take the id instead of re-selecting.
2. `_dedup_exact_window` matches on `episode_sources.source_key` first, then the existing receipt and column
   match as the pre-migration fallback.
3. `drift.py`, `sync_recent_episodes` and `reconcile_memory_root` anti-join file lines against
   `episode_sources (ts, summary_md5)` instead of the live summary, so a merged or edited episode's original
   line is recognised as present (ARCH-10).
4. `forget.py` derives legacy identities and outbox identities from the episode's source rows (all roles),
   not from the current summary (RSN-6). `_outbox_remainder` can now name the exact files, but still only
   reports in this phase.
5. Tests for the two untested gaps the audit named: merge-then-reconcile does not resurrect; forget after
   an edit erases both legacy lines.

Stop-condition: every reader keeps its old path when the table is absent. A hub without 0027 must behave
exactly as today; add one test per reader that runs with the probe forced false.

Build notes (phase 2, 2026-10-15):

- `write_pg` keeps its dict return and adds `source_key`; `episode_id` is set on every branch (from
  `RETURNING id` on insert, the legacy-key holder on a conflict, the matched row on skip or merge).
  `capture(..., result=)` passes the stats to the MCP tool.
- The exact-window source-key match runs only when it is no broader than the column match: payload-hash
  keys, or natural keys with a harness and, when windowed, a part. The 764 historical window-without-part
  episodes keep inserting through the column path.
- The file-line anti-join counts a line present when either the live identity or any source row matches.
- Forget leaves a source-derived line that another episode also holds (reported as `legacy_shared`) and
  reports `legacy_file` partial when a source row has no `ts` or `summary_md5`.
- A source row is recorded fail-open, so forget checks the rows against raw: an origin or a
  `merged_from` receipt with no row is rebuilt from raw (its payload's `ts` and summary), and one raw
  can no longer identify is counted as unidentified, which makes the forget partial.
- Timed on a scratch database with 20,000 episodes and source rows: anti-join over 20,100 keys 182 ms
  (146 ms without the table); source-key lookup 0.09 ms. The live replica and live capture timing gate
  below has not been run.

Timing gate (standing policy since 2026-10-11): anything on the capture or per-prompt path is timed against
the live replica and a live capture before merge, not only under pytest.

## Phase 3: uniqueness moves, the backed-out fixes return

1. Migration `0028_episode_uniqueness.sql`: drop `uq_episodes_ts_summary_md5`; `episode_sources.source_key`
   is the uniqueness. `_upsert_episode` inserts the episode unconditionally and relies on the source-row
   conflict to detect replays (decision 3).
2. Outbox file name becomes `<source_key>.json`; `enqueue` keeps the equal-payload acknowledgement and the
   collision fallback for pre-key files.
3. Return the three backed-out fixes, each as its own commit with the original test restored or renamed:
   - RV1-01 / CAP-22 / ARCH-8: a hub-only install with the mirror off acknowledges on durable enqueue, because
     two sources can no longer share a file. `run_capture_v2` unconfigured is no longer consulted on that path.
   - Forget erases outbox, dead and quarantine copies by `source_key` under the outbox lock, and reports
     `complete=True` when every source row's copies are gone.
   - Replace the "treating as the same episode" log with decision 3's three outcomes; delete
     `CaptureIdentityConflict` or raise it only for a `source_key` whose stored `episode_id` is tombstoned.
4. Stop writing `raw.merged_from[]`.

Stop-condition: phase 3 merges only after phase 2 has soaked at least one nightly and the harness liveness
check has stayed green. Dropping the constraint on a hub that still has pre-key duplicates is a one-way door:
the migration must first count rows that would collide on `source_key` and refuse if any exist.

## Oracle

Hermetic pytest from `packages/cli` (temp `HOME`, `KHIPU_KEYCHAIN=0`, DSN and key variables unset; baseline
is the two named `test_graph_backup.py::RunOffsiteTest` failures, not green); `tests/test_pg_scratch.py`
against a scratch Postgres for the migrations; `ruff check --no-cache packages/cli` (nine baseline errors).
At most three oracle runs per change. The live gate is `khipu sessions liveness` green and one real capture
landing with a source row after each phase's deploy.

## Out of scope

Gateway-assigned provenance (its own scope, `2026-10-11-gateway-provenance.md`; it adds a column to
`episodes`, not to this table). Materialisation completion (ARCH-7). The outbox query contract (DOC-15).
Replica alias lookup.

## Unverified

- Backfill time on this Mac's hub (about 13,300 episodes) is estimated, not measured.
- Whether any historical row lacks both a `session_id` and a stored payload; phase 1's fallback covers it.
- The audit's claim that an unconfigured `run_capture_v2` returns 70 on the hub-only path was read from the
  code, not reproduced live.

## P2 durable-forget race checkpoint (2026-10-15)

- **Status:** implemented; targeted gates pass; whole suite remains red
- **Output:** `/tmp/khipu-forget-structural-p2/`; final root evidence in
  `/tmp/khipu-forget-root-evidence/verification.md`
- **Scope:** publish a durable local forgotten-source registry before local erase scans; make local capture,
  outbox, and replica writers reject its source/payload identities under their write locks.
- **Oracle attempts:** worker ran one collection-only failure and one behavioral failing-first selector.
  Root ran the focused glob three times (final: 336 passed, 18 skipped, 22 subtests passed), the subprocess
  selector twice (red, then 4 passed), and the whole suite three times. No further suite run was made.
- **Evidence:** `failing-first.log` records the discarded collection-only failure; `behavioral-red.log` records
  the three real pre-fix races (two file writers and a late outbox enqueue). Static syntax compilation passed;
  no worker green pytest was run.
- **Follow-up evidence:** root's source review exposed legacy-child lock inheritance (`subprocess-red.log`: one
  real deadlock failure, one refusal pass). The writer now hands its owned lock FD to the child and validates
  device/inode/path before treating it as borrowed; `subprocess-green.log` records all four checks passing.
- **Final whole suite:** 4,844 passed, 50 failed, 46 errors, 129 skipped, 3 deselected, 2 xpassed,
  3,888 subtests passed. Of the 96 failure/error results, 89 are socket PermissionErrors (listed separately
  in `socket-permission-errors.txt`). The seven other failures are five CLI argument cases reproduced
  against HEAD CLI source, one temporal `KeyError: 'time_interpretation'`, and one temporary-directory
  cleanup `OSError: [Errno 66] Directory not empty`. The last two remain unresolved; their causes are
  unverified. No capture/forget/outbox race or subprocess-registry test failed in this final run.
- **Boundary:** synthetic interleavings and local subprocesses only; real PostgreSQL concurrency remains
  unverified. No live hub, commit, or push.

## Durable forget: the write gate (2026-10-15)

Five review rounds each found one more writer that could bring a forgotten source back after the forget
reported itself complete. The model changes from patching writers one at a time to one gate. Every code
path that writes an episode or its source checks `khipu/tombstones.py`: a local registry of forgotten
source keys and payload hashes, read and written under one flock (`write_gate` holds it for many checks
and the caller's write; `write_guard` is a thin wrapper for one payload). The registry is merge-only:
publication, backup import and every other writer only add identities, never replace the file. Forget
takes the hub row lock, reads the sources, publishes, then re-reads; a source that appeared in between is
published in a further round (at most three, after which the forget reports itself incomplete). In the
same transaction as the tombstone, forget writes the identities to the hub's `forgotten_sources` table
(migration 0030), which hub writers read before they insert, and whose triggers refuse an insert, merge
or un-forget that names one. A write from another Mac, or from code that predates the registry, is
refused by the hub itself.

### Inventory of write paths

Pinned by `tests/test_episode_write_gate_inventory.py`: a new path fails that test until it is routed
through the gate and added here.

| Path (`packages/cli/khipu/`) | Writes | Gated by | Memory lock |
| --- | --- | --- | --- |
| `mirror._upsert_episode` | hub episode row + origin source row | `hub_source_forgotten` before the INSERT; 0030 insert triggers | shared (callers) |
| `capture.write_pg` | hub (via `_upsert_episode`, `_merge_into_episode`) | `hub_source_forgotten` first thing inside the cursor | shared |
| `mirror.mirror_episode` | hub (via `_upsert_episode`), capture graph | the callee; a refusal skips the graph mint | shared |
| `mirror.sync_recent_episodes`, `mirror.reconcile_memory_root` | hub (via `_upsert_episode`) | the callee | shared |
| `capture._merge_into_episode` | hub merge receipt + merged source row | `hub_source_forgotten` under the target row lock; 0030 update trigger | shared (via `write_pg`) |
| `capture_identity.record_source` / `_insert_source` | hub `episode_sources` | called only from the two gated writers above; 0030 insert trigger | shared (callers) |
| `capture.run_capture_v2` | legacy `episodes.jsonl` (subprocess) | `write_guard` held through the child, lock FD handed to it | shared (via `capture.capture`) |
| `outbox.enqueue`, `outbox.drain`, `outbox.retry_dead` | outbox, dead and quarantine job files | `write_guard` at each job write | shared |
| `session_capture.enqueue` | transcript jobs upstream of extraction | none needed: holds no episode; its results go through `write_pg` | none: runs in the Aegis sandbox, which cannot write the data folder; shares forget's claim lock |
| `session_capture._claim`, `_release`, `_discard_claim`, `_save_claimed_job`, `_archive_empty` | claimed native job files and their checkpoints | none for the registry; `_save_claimed_job` takes `write_gate` and raises `JobForgotten` for a forgotten or removed claim; forget removes queued and claimed jobs naming its sources | shared |
| `hub_snapshot.refresh` (`_refresh_locked`, `_insert_episodes`) | replica, full dump | `write_gate` from `_scrub_forgotten` through `os.replace` | shared, from the first hub read through publication |
| `hub_snapshot.upsert_episode` (`_upsert_episode_unfenced`) | replica, one episode | `write_guard` held through the SQLite commit | shared |
| `hub_snapshot.scrub_forgotten_snapshot` | replica deletes | `write_gate` | shared |
| `hub_snapshot.forget_episode_in_snapshot`, `_apply_pending_erasures` | replica tombstones | erase | exclusive (inside a forget) |
| `paths.import_local` | data folder from a backup | registry merged, never copied; jobs naming a forgotten source skipped under `write_gate`; the backup's replica is staged, scrubbed in the staging copy, then `os.replace`d (`hub_snapshot.publish_imported_snapshot`) | shared |
| `join.import_kit` | settings and credentials | writes no episodes; holds the lock because it writes the data folder | shared |
| `ledger` import | ledger rows | writes no episodes | none |
| migrations 0027, 0029, 0030 | backfills (source rows, origin, `forgotten_sources`) | 0030 backfills the forgotten identities | none (table locks, see below) |
| `hygiene.apply_backfill_*`, `capture._claim_unowned_episode`, `cli._cmd_episode_edit` | project/repo_root, origin, summary on existing rows | metadata only; the edit skips forgotten rows | none |
| `forget.forget_episode` | hub tombstone, `forgotten_sources` | erase | exclusive (`forget.forget_everywhere`) |

### The four holes and the test that pins each

All in `tests/test_forget_write_gate.py`.

1. A capture merged between forget's source read and its registry publish:
   `test_merge_committed_after_forget_reads_sources_is_fenced`.
2. The direct mirror and `write_pg` inserting a changed summary for a forgotten natural source:
   `test_direct_mirror_refuses_a_forgotten_natural_source`, `test_write_pg_refuses_a_forgotten_natural_source`,
   and, for a forget made on another Mac, `test_hub_forgotten_sources_table_refuses_a_write_this_mac_never_saw`.
3. The full replica refresh restoring a copy under another id: `test_full_refresh_does_not_restore_forgotten_source_under_new_id`,
   `test_full_refresh_fails_closed_on_a_corrupt_registry`.
4. Backup import restoring a queued job, replacing the registry or restoring the replica:
   `test_backup_import_does_not_restore_forgotten_outbox`, `test_directory_import_does_not_restore_forgotten_outbox`,
   `test_backup_import_preserves_forgotten_registry`, `test_backup_import_adds_the_archives_forgotten_sources`,
   `test_backup_import_refuses_a_corrupt_registry`, `test_imported_snapshot_is_scrubbed_of_forgotten_sources`.

### Known boundaries

- A duplicate copy of a source committed under another episode id before the forget began is a separate
  episode. The gate keeps it out of the replica and refuses new writes of it, but it stays live on the hub
  until it is forgotten by its own id.
- Real PostgreSQL behaviour of the 0030 triggers is now covered by `tests/test_forgotten_sources_pg.py`
  (hermetic cluster, see the next section); the first-draft scratch-database test is gone.
- Once anything is forgotten, every full refresh checks each live replica row against the registry. Rows
  are already redacted when stored, so the scrub uses `Gate.allows_stored`, which skips re-redaction (about
  0.75 ms per large row under load, against about 16 ms with it). The cost on the live replica is
  estimated, not measured; it falls under the capture-path timing gate before merge.
- An imported replica that cannot be scrubbed (a refresh holds its lock) is deleted; the next refresh
  rebuilds it.

## Durable forget: one memory lock (2026-10-15)

Why the model changed. The write gate checked a registry at each writer. Seven review rounds still each
found a new interleaving: two forgets rewriting the same legacy log, a native job claimed across a forget,
an episode row committed before its source row was refused, a backup replica readable before its scrub,
a migration and a writer taking the two tables in opposite orders. Each was a different writer racing a
different step of a forget. The fix is to stop reasoning about steps.

**The lock** (`khipu/memory_lock.py`, file `.memory.lock` in the data folder, one per Mac). `forget_everywhere`
holds it exclusive for its whole run. Every local writer holds it shared for its whole write (the table
above), except `session_capture.enqueue`: it runs inside the Aegis sandbox, which cannot create the lock
file, and queues a transcript, not an episode. Forget's native-job scan takes the same claim lock, so an
enqueue lands before the scan (and is removed) or after it, where the checkpoint and the hub refuse its drain. So two forgets serialize, a writer waits for a whole forget, and a forget waits for every writer
in flight. It is taken before every other Khipu lock (`write_gate` takes it first), it is re-entrant, a
writer that tries to start a forget inside its own write is refused (upgrading would wait on itself), and
a legacy child process inherits the shared descriptor through `handoff`, validated like the registry
handoff.

**The hub decides at the episode row.** A writer names the identities of the row it inserts in the
transaction-local setting `khipu.source_identities` (`tombstones.announce_hub_identities`). The 0030
episodes insert trigger refuses the row itself for its natural key or any named identity, and holds each
identity's advisory lock until commit, so a forget cannot land between the episode row and its source
row. `mirror._upsert_episode` wraps the episode INSERT and its origin source row in the savepoint
`khipu_episode_write`; a forgotten refusal from either rolls both back and returns False with
`out["forgotten_source"]`. `capture._merge_into_episode` does the same around its UPDATE and merged source
row (`khipu_merge_write`). `record_source` re-raises the refusal as `ForgottenSourceRefused`. Forget now
publishes the hub identities after the tombstone UPDATE, in the same transaction.

**Backup import is staged.** `paths.import_local` stages the backup's replica, scrubs it in the staging copy
and only then `os.replace`s it (`hub_snapshot.publish_imported_snapshot`). The replica path never holds a
forgotten row, and a scrub that cannot run leaves the current replica as it was.

**Native jobs in completion.** `forget._native_job_remainder` removes queued and claimed native jobs whose
`session_capture.job_source_identity` matches a source row's key; an unreadable job keeps the forget
incomplete. A drainer's later checkpoint raises `JobForgotten` instead of writing a forgotten or removed
claim back.

**Migration 0030 order.** One transaction: both tables locked ACCESS EXCLUSIVE first (the lock DROP
TRIGGER needs, so the migration never upgrades a lock it holds), then the table, functions and triggers,
then the backfill. The migration waits for `episodes` while it holds nothing and asks for
`episode_sources` with NOWAIT; when a writer holds that, it lets go and tries again within 5 s, so it
never waits while holding a lock a writer needs. Round 11 found that the earlier SHARE ROW EXCLUSIVE
lock, upgraded at DROP TRIGGER, deadlocked against a merge writer holding its target row lock;
`test_migration_runs_while_a_merge_writer_loops_without_deadlock` re-applies the migration while the
real merge writer loops on a second connection. No forget can commit between the backfill's scan and the triggers going
live. The backfill covers source keys and payload hashes of deleted episodes, the natural key of every
deleted episode (so one with no source row is covered) and each merge receipt's id and natural key.
`lock_timeout` is 5 s; `migrate._run_migrations` retries a step that raises `LockNotAvailable` up to
`LOCK_RETRIES` (3) times.

### Tests that pin each

- Two forgets: `test_two_forgets_cannot_rewrite_a_completed_forgets_source`. Writers wait for a whole forget:
  `test_every_kind_of_local_writer_waits_for_a_whole_forget` (outbox, native checkpoint, backup import, replica
  scrub, `write_pg`, mirror). Lock rules: `test_a_forget_cannot_start_inside_a_local_write`,
  `test_the_lock_is_reentrant_for_a_writer_and_for_a_forget`. All in `tests/test_forget_memory_lock.py`.
- Native jobs: `test_a_claimed_native_job_cannot_outlive_a_completed_forget`,
  `test_a_native_job_from_another_source_is_kept`, `test_an_unreadable_native_job_keeps_the_forget_open`.
- Refused source row undoes its episode row: `test_a_refused_source_row_rolls_back_its_episode_row` (fake
  cursor) and, against the real triggers, `test_hash_only_forget_committed_first_refuses_the_episode_row`.
- Staged import: `test_an_imported_replica_is_never_readable_before_its_scrub`,
  `test_an_import_whose_scrub_cannot_run_leaves_the_current_replica`.
- Migration, on a real PostgreSQL (`tests/test_forgotten_sources_pg.py`, cluster and template database from
  `tests/hermetic_pg.py`): `test_the_full_chain_applies_and_0030_is_safe_to_run_twice`,
  `test_backfill_covers_historic_forgotten_episode_without_source_row`,
  `test_backfill_covers_the_merge_receipt_of_a_forgotten_episode`,
  `test_a_forget_committed_before_the_migration_is_backfilled`,
  `test_no_forget_can_commit_between_the_backfill_scan_and_the_triggers`,
  `test_migration_does_not_deadlock_normal_two_table_writer`.
- Writers against the triggers: `test_a_normal_upsert_inserts_the_episode_and_its_origin_source_row`,
  `test_hash_only_writer_first_holds_the_identity_until_it_commits`,
  `test_a_merge_of_a_forgotten_source_returns_false_and_leaves_the_target`.
- Every writer holds the lock: `test_every_local_writer_entry_point_holds_the_memory_lock`,
  `test_every_gated_writer_is_reached_only_from_a_locked_entry_point`,
  `test_locked_entries_exist_and_the_lock_is_taken_before_the_gate` (`tests/test_episode_write_gate_inventory.py`).

On a machine with PostgreSQL older than 19 the hermetic chain drops 0001's `CREATE PROPERTY GRAPH` from
its temporary copy of the migrations (nothing here reads the graph).

### Measured cost

Capture insert path (`mirror._upsert_episode`, episode row plus origin source row, one commit per insert,
distinct natural keys), 3 rounds of 500 after a 50-insert warm-up, on 20,000 seeded episodes and source
rows, hermetic local cluster with fsync off, `hub_source_forgotten` unpatched. Median per insert:
**1.20 ms at 0029 (no triggers), 1.60 ms at 0030 (triggers, `khipu.source_identities`, 200 forgotten
identities), +0.41 ms**. Fsync is off, so the commit on a real hub adds the same cost to both sides.

Migration 0030 as a whole (both table locks, the table, the triggers and the backfill, through
`khipu migrate`), on 20,000 seeded episodes and 20,000 source rows with a merge receipt on every tenth
episode, same hermetic cluster (PostgreSQL 16, fsync off), median of 3 fresh databases: **14 ms with 200
forgotten episodes (640 identities backfilled)** and **0.41 s with all 20,000 forgotten (64,000
identities)**, the worst case. Both table locks are held for that whole time, so captures wait at most
that long; it is well inside the 5 s `lock_timeout`. The migration stays one transaction on purpose, so
**0.41 s is the expected write pause on this Mac's hub**: every capture on every Mac waits up to that long
while 0030 applies. `khipu migrate` records how long each step held table locks, and when one held them
longer than 1 s its output carries a note, which doctor shows as `notes_migrate`.

### Remaining boundary

A writer from older code on another Mac that names no identities and has no natural key (no session id or
transcript range) is not refused at its episode row. It is still refused at its source row, under its own
savepoint, and whether that undoes the episode row depends on that older code. The lock is per Mac: a
forget on one Mac does not wait for a writer on another; the hub's advisory locks and triggers cover that
case.

## Durable forget: one store inventory (2026-10-16)

Why. Ten review rounds each found one more place a copy of a source sits. Round 10 found two: the native
`empty/` archive (transcript text of a window that yielded no episode) and a backup that restored a
forgotten episode id under older raw. Listing places one review at a time does not end.

**The model.** `khipu/capture_stores.py` is the one list (`STORES`): each store names where it is, what it
holds, which functions write it and how a forget covers it. Forget's scans read their folders from it
(`native_job_folders`, `outbox_folders`). A store with no forget scan keeps the forget open (partial
`store_inventory`, location state "not checked"). `tests/test_capture_store_inventory.py` walks every
function in the package that writes a file and fails unless it is a store's writer or a `NOT_CAPTURE`
entry with the reason it holds no capture data. A new writer therefore cannot ship unplaced.

| Store | Where | Holds | How forget covers it |
|---|---|---|---|
| native capture job | capture home `queue/`: `*.json`, `*.claimed.<pid>`, `*.tmp`, `.extract-*.tmp` | transcript text, and the extracted episode once checkpointed | scan "native jobs" |
| native empty archive | capture home `empty/`: `*.json` | transcript text of a window that yielded no episode | scan "native jobs" |
| outbox job | `outbox/`, `outbox/dead/`, `outbox/quarantine/`: `*.json` and `.*.tmp` | whole capture payloads waiting for the hub | scan "outbox" |
| legacy log | memory root: `episodes*.jsonl`, their `.bak` copies, `.khipu-forget-backups/`, `.tmp` files beside them | one capture per line, summary text | scan "legacy logs" |
| replica | data folder `hub_snapshot.sqlite` | episode rows, vectors, decisions and deliverables | scan "replica" |
| replica temp | data folder `.hub_snapshot.<random>.sqlite` and its journal and WAL | a refresh dump or a staged backup replica | scan "replica temps" |
| legacy writer | data folder `.legacy_writers/<pgid>.json` | the process group of a legacy writer that may still append | scan "legacy writers" |
| recall golden set | data folder `recall-golden.jsonl`, or the file `KHIPU_RECALL_GOLDEN` names | recall test queries, which usually quote a captured episode's private text, with the episode ids each expects | scan "recall golden": every line naming the episode in `expect` or `stale` is dropped; a file that cannot be read or judged keeps the forget open |
| work view | data folder `state/work/view.json` and `.view.json.*.tmp` rewrites beside it (the same writer, `work._write_json`, saves `last/*.json`, `links.json` and `hidden.json` there: job summaries, ids and hashes only) | the Work view's read model, which lists open commitments' text | scan "work view": the saved view and its temps are removed, since forget has just closed the commitments the episode opened; the next `khipu work` writes it again without them; a file that cannot be listed or removed keeps the forget open |
| recall eval record | a file the owner names with `khipu recall eval --record` | the golden queries, quoted text included, and the ids each returned | kept: an owner-chosen file outside the data folder that forget cannot find |
| session state | capture home `state/<harness>--<session>.json` and `.capture-now.json` | offsets and hashes; carried text and a capture note not yet queued | none: the carried text belongs to the next window, which is refused when queued if its source is forgotten |
| capture log | capture home `logs/session-capture.log` | drain and hook events: file names, counts and lengths | none: it never holds summary or transcript text |
| conversation media | data folder `media/conversation_memory/<sha256>.<ext>` | images found in transcripts, shared by content hash | kept: content-addressed, can belong to several episodes |
| extraction scratch | data folder `state/synth-cli/work-*/` and its output temp | a model call's working folder and output | kept: removed by the call that made it |
| commitments backup | hygiene backup folder `commitments-<time>/commitments.bin` | a binary copy of the commitments table | kept: closed commitments remain by design |
| local backup | a backup folder or archive the owner chose | a copy of the data folder | kept: backups are kept for recovery; an import restores only what a store allows, through its scrub |

Writers that hold no capture data are listed in the test's `NOT_CAPTURE` with what they write. The Work
view and the account settings added these (2026-10-16): `config._write_config` (settings) and
`config.config_write_lock` (a lock), the one config write path since every settings writer goes through
`config.mutate_config`; `accounts._save_declined` (the logins not offered again: folder names and CLI
kinds) and its lock `accounts._declined_lock`; and `agent_view.emit`, the one way a ledger or work answer
leaves Khipu, which writes a file only for `khipu ledger ... --out` (a ledger board or ledger rows).

**Import is an allow-list.** `capture_stores` says which stores a backup may restore and the exact file
patterns each owns (`Store.restore`, plus `OTHER_DATA` for settings, the forget registry and waiting
ledger writes). Each restored file goes through its entry's scrub (`paths.RESTORE_SCRUBS`): a queued job
for a forgotten source is left out, the replica is staged and scrubbed, the registry is merged, and the
recall golden set loses every line that names a forgotten episode id (a file with a line that cannot be
judged is not restored). Every
other file in a backup (temps such as a crash-left `.job.json.<random>.tmp`, sidecars, locks, writer
records, unknown names) is left out and listed in the report's `not_restored`.

**Writer records fail closed.** A `.legacy_writers` record that cannot be read or parsed is kept and
keeps the forget partial (`legacy_writer`, state "not checked") until a sweep can read it or no process
of the group its file name gives is running. Its group is never stopped, since without the start time it
cannot be told from an unrelated process that reused the number.

**Import scrubs by every identity the registry holds.** The registry (`tombstones`) now also holds
forgotten episode ids (`publish_episode_ids`, `forgotten_episode_ids`, `Gate.episode_ids`; old files without
the key still read). `forget_episode` publishes the id. `hub_snapshot._scrub_forgotten` refuses live rows
whose id the registry holds as well as rows with a forgotten source key or hash, and
`publish_imported_snapshot` applies pending erasures before the scrub. So a backup cannot bring back a
forgotten episode id under older raw.

**Known boundaries.** Conversation media is kept (shared by content hash). Extraction scratch left behind
by a killed call is not swept. Local backups and the commitments backup are kept for recovery. Session
state carry and capture notes are not sources until a window is queued. A `khipu work` build that read the
commitments before the forget closed them and saves after its scan can write the view once more; the next
build replaces it.

**Tests that pin it.** `tests/test_forget_round10.py`:
`test_job_archived_to_empty_is_removed_by_a_completed_forget`,
`test_unreadable_empty_archive_keeps_the_forget_open`,
`test_backup_cannot_restore_a_forgotten_episode_id_under_older_raw`,
`test_registry_merge_keeps_forgotten_episode_ids_and_reads_old_files`.
`tests/test_capture_store_inventory.py`:
`test_every_file_writer_is_a_store_writer_or_writes_no_capture_data`,
`test_inventory_lists_no_writer_that_no_longer_writes`,
`test_every_store_is_scanned_by_forget_or_says_why_not`,
`test_forget_reads_its_folders_from_the_inventory`,
`test_a_store_without_a_forget_scan_keeps_the_forget_open`.

**Placed when merging with main (2026-10-16).** Account discovery (settings clarity 9A) arrived on main
after this list was written. Its writers hold no capture text and sit in the test's not-capture table: the
discovery results (folder names, CLI kinds, check times), its lock, and the temporary empty folder a CLI
status check runs in. 9A's `session_capture._write_at` writes the session state files and is listed as a
writer of the session state store.

## Known boundaries and follow-ups (2026-10-16)

Limits accepted for this merge, each a follow-up rather than a blocker:

- **A process that calls `setsid` itself.** Forget stops each recorded legacy writer's process group
  before it reads the logs. A child that starts its own session leaves that group, so the stop does not
  reach it and it could still append after the forget checked the logs. Follow-up: track writers by
  something a child cannot leave (a held descriptor or a process tree walk), not only the group id.
- **Migration 0030 holds both tables while its backfill runs.** Both table locks are held for the whole
  one-transaction migration, so every capture on every Mac waits while it applies: measured at 14 ms
  typical (200 forgotten episodes) and 0.41 s worst case (all 20,000 forgotten), inside the 5 s
  `lock_timeout` (see "Measured cost" above). Follow-up: a batched backfill outside the table locks if a
  hub ever grows far past the measured size.
- **The recall eval record is kept.** `khipu recall eval --record` writes the golden queries, quoted text
  included, to a file the owner names outside the data folder; forget cannot find it. Follow-up: write
  records into the data folder so the recall golden scan can cover them.
- **Fixed from the final review (round 15).** (1) The native-queue and outbox scans got only source rows,
  not the natural identities recovered from episode columns, so a job keyed by a historical fallback
  identity could stay while forget reported complete. Now `forget._registered_sources` is the one list the
  registry fences and both scans match against (`scan_sources` in the forget result). A pre-0027 hub has
  no source rows and keeps its same-session fallback. (2) A `.recall-golden.*.tmp` left by a crash
  mid-rewrite was never scanned. The recall golden store now owns that temp pattern: forget removes every
  one beside the golden file (only the scrub writes them, under the memory lock forget holds), a temp it
  cannot remove keeps the forget open, and backup import never restores one (a hidden name no store
  claims). Tests: `tests/test_forget_round15.py`.
- **A recall golden line is matched by episode id only.** A line whose query quotes a forgotten episode
  but whose `expect` names only a topic or another episode is kept, since the registry holds identities and
  ids, never the forgotten text.
