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
