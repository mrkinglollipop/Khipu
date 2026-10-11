# Replica freshness: the local copy follows the hub, and says how far behind it is

Approved in principle, Matt 2026-10-11, third of four backlog decisions from the 2026-10-10 audit. This page
is the scope; a build session reads it first. Audit ID: ARCH-6 (P1). Related open IDs: HUB-19 (decision
cursor), REC-14 (installed-code freshness, out of scope).

## Status

| Phase | What | State |
|---|---|---|
| 0 | Scheduled full refresh, so the replica cannot age out while the rest is built | done 2026-10-11 (PR #140): nightly `snapshot_refresh` step, fail-open; `refresh()` keeps the decision-sync cursor (the phase 1 stop-condition test exists); live dump 29 s |
| 1 | Honest freshness: lag per table, stale/unknown states, no false red | open |
| 2 | Bounded incremental sync from the hub on a schedule, independent of embeddings | open |
| 3 | Decision cursor survives refresh and sees restore/unretract (HUB-19 migration) | open |

One PR per phase, merged alone and soaked. Phase 0 is small and goes first.

## The defect, in one paragraph

The sqlite replica (`~/.config/khipu/hub_snapshot.sqlite`) is what every per-prompt recall reads, because
the hub lane cannot be trusted inside a 1.2 s budget. But the replica only learns about rows through four
local side-effects: an episode is written after *this machine* embeds it (`embed.py` ~1893, inside
`embed_on_capture`), vectors after this machine's embed catch-up, decisions after this machine supersedes
one, tombstones after this machine forgets one. Anything that happened on the hub without this machine
doing it, and any local capture whose embedding failed, reaches the replica only through a full
`refresh()`, and since the doctor side-effect was removed (CLI-6, commit `fc24a22`) nothing calls
`refresh()` except a human typing `khipu snapshot refresh`. Freshness is reported as the age of that last
full dump: `snapshot_is_fresh()` trusts the file for 24 hours, `snapshot_freshness()` goes red 30 minutes
after any hub ingest regardless of incremental writes (a false red), and neither sees topics, decisions or
deletions. The 2026-09 incident (85% of per-prompt calls timing out for two weeks while doctor was green) is
the same shape: the check measured a proxy, not the thing.

## Source facts (verified against main `1cf486b`; paths under `packages/cli/khipu/`)

- `refresh()` (`hub_snapshot.py:1059-1121`): non-blocking flock, temp file, full dump of nine tables
  (`_TABLES`, `:57`: episodes, topics, topic_revisions, nodes, edges, embedding_profiles, memory_embeddings,
  decisions, deliverables), `snapshot_meta.refreshed_at` inside the file, pending erasures re-applied,
  `os.replace`, sidecar JSON. Commitments are not a replica table; only their embeddings come across.
  The live file is 356 MB; journal mode is `delete`, not WAL, so writers block readers.
- Incremental writers, all under the same lock and all no-ops while a dump runs: `upsert_episode` (`:1169`),
  `upsert_embeddings` (`:1290`), `apply_decision_changes` (`:483`), `forget_episode_in_snapshot` (`:1360`,
  durable marker in `.hub_snapshot.pending-erasure/`). The all-time stop-hook log shows 21 "snapshot upsert
  failed", 54 "refresh in progress" skips and 8 "database is locked": a skipped write is lost until the next
  full dump.
- `sync_decision_changes` (`:555`) is the only hub-to-replica pull. It runs inside `session_capture.drain`
  only when something was captured, 500 rows per call, cursor `(GREATEST(created_at, superseded_at,
  retracted_at), id)` in meta keys `last_decision_sync_at`/`_id`. `refresh()` rewrites the meta sidecar
  without those keys. Live meta on 2026-10-11: `refreshed_at` 12:56Z, decision cursor at 2026-09-14, id
  13,504 and climbing 500 per drain against a max id of 50,700. The cursor is being replayed from a month
  ago after every refresh. `restore()` and `unretract()` null the timestamps the cursor orders by (HUB-19).
- Freshness reporters: `snapshot_health()` (`:1432`, age only); `snapshot_freshness()` (`:1469`,
  `behind = hub max(ingested_at) − refreshed_at`, red past 30 min, consumed by status, doctor aggregate
  `ok`, `khipu_status`, `drift.py`); `snapshot_is_fresh()` (`:2438`, 24 h, consumed by
  `recall_prompt._snapshot_search_hits:514` which raises `_SnapshotUnusable` and falls back to the hub, and
  by the verify probe); `prompt_recall_snapshot_status()` (`:2452`, never red on its own). Desktop
  `doctorAttention.ts:288` says "Refreshing it is a Settings action" but no Settings control refreshes it.
- `_MATRIX_CACHE` (`:2554`) is keyed on `(path, mtime_ns, size, profile, kind)`; every incremental write
  misses it and the daemon reloads about 20k vector blobs (94 ms warm path after hotfix #137).
- Hub mutation timestamps available without a migration: `episodes.id` (BIGSERIAL), `ingested_at`
  (transaction start, not commit), `deleted_at`; `topics.deleted_at`, `content_hash`, `updated_at` (not
  monotonic: the file mirror backdates it to frontmatter or mtime); `decisions.created_at`,
  `superseded_at`, `retracted_at`. No `updated_at` on episodes or decisions.
- launchd on this Mac: `com.khipu.queue-drain` every 300 s, `com.matt.khipu-nightly` 02:05,
  `com.matt.khipu-graph` 02:17, `notes-watch`, `recall-daemon` (KeepAlive), the 12:15 soak probe. None
  refreshes the replica. `khipu jobs` manages these plists.
- Tests: `tests/test_hub_snapshot.py` (schema, upsert, forget, freshness, decision sync, never-resurrect,
  cosine candidates) and `tests/test_audit1010_huba.py`. Replicas are built in a temp dir with
  `_create_schema` and `snapshot_path`/`meta` patched; no Postgres in unit tests.

## Decisions

1. **No hub migration for phases 0 to 2.** The journal is replica-side: per-table high-water cursors on
   columns the hub already has, a bounded overlap window for `ingested_at` skew, and a periodic manifest
   reconcile `(id, content_hash or deleted_at)` for topics and decisions that catches backdated edits. This is
   eventual consistency with a measured lag, not a transactional guarantee. The transactional hub-side
   change journal the audit proposed is deferred until a second machine actually needs it; the one
   migration in this scope is HUB-19 (phase 3), which the decision cursor needs regardless.
2. **Publication is independent of embeddings.** An episode row reaches the replica when the hub has it,
   whether or not any machine has embedded it. Vectors follow when they exist. The lexical lane of
   `search_snapshot` needs only the row.
3. **Freshness is per table and per kind of change**, reported as a lag in seconds against the hub when the
   hub is reachable, and as `unknown` with the last-known lag when it is not. "Fresh" for the recall hook
   means the newest sync completed within its own interval times three; the 24-hour file age is no longer
   the gate. Doctor is red only when a scheduled sync has failed or has not run, never because a hub ingest
   happened since the last full dump.
4. **A full dump stays as the repair path**, scheduled once a day on the nightly's clock and runnable by hand;
   it must preserve every meta cursor it did not itself advance.
5. **One sync job, bounded.** A `khipu snapshot sync` command does one bounded pass (episodes, tombstones,
   topics, decisions, deliverables, vectors that exist), under the existing refresh lock, and records what it
   did and how far behind each cursor ended. The 5-minute queue-drain job calls it after draining; so does
   the Stop hook's catch-up, replacing the current embed-gated upsert for episodes.
6. **The replica is only ever written by Khipu's own writers under the lock**; no second process touches
   the file. Journal mode moves to WAL in phase 2 so readers never block on the sync.

## Phase 0: stop the clock (small, first)

Inspect `jobs.py` (how the nightly plist is built and what `run_nightly` does), `cli.py::cmd_snapshot` and
`hub_snapshot.refresh` before writing anything.

1. `run_nightly` ends with a full replica refresh (skip quietly with a logged reason if the lock is held).
   The next day's nightly log line is the acceptance.
2. The 24-hour `SNAPSHOT_MAX_AGE_S` stays for now, so that a missed nightly turns the recall lane to the hub
   path visibly rather than silently serving a week-old replica.

Stop-condition: never run the nightly midday to test this (it races the memory git sync with live
captures). Test with `khipu snapshot refresh` by hand and read the next morning's log.

## Phase 1: honest freshness

1. `snapshot_meta` gains per-table cursor rows: `episodes` (max id, max `ingested_at`), `tombstones`
   (max `deleted_at` for episodes and topics), `topics` (last manifest reconcile time), `decisions` (the
   existing tuple), `deliverables`, `vectors` (per profile). Each row carries `synced_at` and the last error.
2. `snapshot_freshness()` returns the lag per table from those cursors against the hub when reachable,
   `unknown` otherwise, and an overall state `fresh` / `behind` / `stale` / `unknown` with the thresholds
   from decision 3. The false red (`behind_ingest` after an incremental write) goes away because the episode
   cursor advances on incremental writes.
3. `snapshot_is_fresh()` reads the overall state; the 24-hour file age becomes one input, not the gate.
4. `khipu status`, `khipu doctor`, `khipu_status`, `drift.py` and the desktop Health row show the per-table
   lag and the state; the desktop text that promises a Settings action is corrected or the action is added.
   UI text changes need no mock; a new control does (`docs/mockups/`).
5. Tests: the audit's five scenarios (remote insertion, topic-only correction, deletion, failed refresh,
   capture without embeddings) as replica-only tests with a mocked hub, each asserting the reported state.

Stop-condition: `refresh()` must carry every existing meta cursor forward; a test asserts that the decision
cursor survives a full dump (this is the live bug above).

## Phase 2: bounded sync

Inspect `sync_decision_changes` (the pattern to generalise), `upsert_episode`, `embed_recent_missing` and
`session_capture.drain` before writing anything.

1. `khipu snapshot sync`: episodes by `(ingested_at, id)` with a 10-minute overlap window and an id-gap
   check; tombstones by `deleted_at` for episodes and topics; topics by `deleted_at` plus a manifest
   reconcile of `(id, content_hash)` every N runs; decisions through the existing cursor; deliverables with
   their episodes; vectors by `(profile, kind, ref)` for refs the replica has rows for. Bounded per pass
   (rows and seconds), oldest first, cursors advanced only after commit, pending erasures re-applied.
2. The queue-drain job and the Stop hook call it; `embed_on_capture` stops being the episode writer for the
   replica (it still writes vectors). Measure the per-prompt recall path and the Stop hook duration before and
   after against the live replica; neither may regress.
3. WAL journal mode, set when the file is created or on the first sync after upgrade, with the reader side
   checked for `immutable` opens that would break.
4. Matrix cache: the sync batches its writes so one pass changes the file once; the cache key stays as is.

Stop-condition: if a bounded pass cannot keep up with the hub on this Mac (lag grows across three
consecutive drains), stop and report the numbers rather than raising the bounds.

Timing gate (standing policy since 2026-10-11): anything on the capture or per-prompt path is timed against
the live replica before merge.

## Phase 3: decision cursor (HUB-19)

Migration (next free number): `decisions.updated_at` maintained by a trigger on every change including
restore and unretract, backfilled from `GREATEST(created_at, superseded_at, retracted_at)`, index
`(updated_at, id)`; `sync_decision_changes` switches to that cursor. Deploy order: `khipu migrate`, gateway,
merge. The sync must keep the old cursor as the fallback when the column is absent.

## Oracle

Hermetic pytest from `packages/cli` (temp `HOME`, `KHIPU_KEYCHAIN=0`; baseline is the two named
`test_graph_backup.py::RunOffsiteTest` failures); `ruff check --no-cache packages/cli`; `npx vitest run` and
`npx tsc --noEmit` in `apps/desktop` for the Health row. At most three oracle runs per change. Live gate: the
per-prompt outcome log (`~/.grok/khipu/logs/prompt-recall.log`) keeps hit rate at or above 70% and timeouts
at or below 2% over 40 prompts after each phase, and `khipu doctor` shows the new state as `fresh` with a
lag under the sync interval.

## Out of scope

A transactional hub-side change journal (decision 1). Installed-code freshness (REC-14). Replica alias
lookup for capture identity. Second-machine onboarding (soak is still blocked on it; this scope makes that
soak meaningful).

## Unverified

- That the decision cursor reset is caused by `refresh()` dropping the meta keys was read from the code and
  the live meta, not reproduced; phase 1's stop-condition test settles it.
- WAL mode's interaction with the Tauri app's reads of the same file.
- Whether `ingested_at` skew ever exceeds the 10-minute overlap on this hub; the id-gap check is the backstop.
