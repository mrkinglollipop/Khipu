# Slice C implementation and hermetic verification

Scope: [Khipu in T3 Code, Slice C](2026-10-07-khipu-t3.md). Worktree:
the slice worktree, branch
`feat/khipu-t3-slice-c`, parent `7147cca9092a77fa4aec309a0a1ea9147da3ee98`.

## Implementation

- Native sessions map through T3's read-only provider projection, using
  `nativeThreadRef.nativeId`. Multiple providers/accounts and delegated-task
  threads are supported. `KHIPU_T3_DB` overrides the database path.
- The connection uses URI `mode=ro`, `timeout=0.5`, a 2 ms busy timeout and a
  50 ms SQLite VM deadline (immutable read when T3 is closed, so no -wal/-shm appear). Missing/locked/changed databases and malformed JSON
  yield no mapping. Tests use fixture databases only.
- Hook/sweep state caches a positive mapping for that session. Negative reads
  are cached for five minutes, allowing provider rows that arrive later to be
  discovered without querying every Stop. Lookup occurs when enqueueing a
  window, not on every cadence check.
- Queued windows and every split part carry `t3_thread_id` and `via: t3`.
  Drain copies these deterministic fields after extraction. Explicit captures
  entering `capture()` also resolve missing stamps. Existing episode `raw`
  JSON stores them; incremental replica updates carry the same JSON. There is
  no schema migration.
- Similarity deduplication keeps T3 threads separate and excludes T3 episodes
  from non-T3 merges. Exact-window deduplication is unchanged.
- `khipu_search` exposes exact `t3_thread` and `via` filters in all modes.
  Hub candidate queries and the post-fusion guard honor them. Replica literal
  and semantic paths honor them before selecting results; graph expansion and
  outbox rows cannot escape them. HTTPS gateway requests use the same MCP
  dispatcher. Filters are episode-only, like session/harness filters.
- Valid handoff headers initiate thread recall concurrently with ordinary
  recall under the existing deadline. The hub supplies standing decisions
  and open, unsnoozed commitments, joined through non-forgotten episodes.
  Those entries precede the usual three hits. Continuations such as `continue`
  still receive thread continuity, and handoff refreshes bypass prompt dedup.
- The replica supplies standing decisions provisionally while the hub is
  pending; replica-derived entries are labeled. It has no commitment table,
  so stored `open_loops` are never presented as still-open commitments.
  A stalled hub cannot extend the normal search deadline. The rendered block
  reserves its footer and remains within 600 characters, including any
  appended deliverable line on the handoff path.

## Verification

Final oracle result: **no-new-failure gate passed**. The suite exits 1 with
exactly the 11 named baseline failures: 2,896 passed, 11 failed, 96 skipped,
395 subtests passed in 39.88 seconds. `git diff --check` passed.

The full oracle ran with a temporary HOME, keychain disabled, credential/Claude
home overrides unset, no live-test opt-in, a temporary bytecode cache, and only
this worktree plus the main checkout's `.python_libs` on PYTHONPATH. Each full
run had an explicit 600-second subprocess timeout. No install, live capture,
hub write, real T3 database access, push or PR was performed.

Full-suite attempt history:

1. 2,891 passed, 12 failed, 96 skipped, 395 subtests passed, 66.15 seconds.
   The new failure was the sweep fixture's missing user role.
2. 2,895 passed, 12 failed, 96 skipped, 395 subtests passed, 55.59 seconds.
   The sweep now queued correctly; consecutive user messages coalesced into
   one message, so the split assertion required an alternating transcript.
3. 2,896 passed, exactly 11 baseline failures, 96 skipped, 395 subtests passed,
   39.88 seconds. No new failures. The three-full-run budget is exhausted.

The 11 known baseline failures are:

- `tests/test_embed_media.py::EmbedMediaFlagTest::test_conversation_memory_gets_a_root_for_the_checkbox`
- `tests/test_embed_media.py::EmbedMediaFlagTest::test_set_embed_media_round_trip`
- `tests/test_graph_backup.py::RunOffsiteTest::test_copyto_uses_snapshot_not_live_db`
- `tests/test_graph_backup.py::RunOffsiteTest::test_offsite_fail_surfaces_ops_error_when_record_raises`
- `tests/test_graph_backup.py::ScratchDrillTest::test_scratch_drill_on_fixture`
- `tests/test_integrations.py::ProbeTest::test_hook_probe_runs_through_the_shell_like_the_harnesses_do`
- `tests/test_session_capture.py::ConversationImageLandTest::test_land_absolute_png_under_jsonl_parent`
- `tests/test_session_capture.py::ConversationImageLandTest::test_land_claude_image_block_when_opted_in`
- `tests/test_session_capture.py::ConversationImageLandTest::test_land_rejects_absolute_png_outside_allowlist`
- `tests/test_session_capture.py::ConversationImageLandTest::test_land_skips_webp`
- `tests/test_topic_graph.py::ConversationMemoryToggleTest::test_persist_no_ops_when_conversation_memory_disabled`

New coverage includes cross-provider mapping, delegated threads, misses,
missing/locked/WAL/schema-changed databases, malformed payloads, cache lifetime,
VM deadline, hook/drain/raw persistence, split/sweep stamps, explicit captures,
filters across hub modes/MCP/gateway dispatch/replica paths, deep candidates,
graph/outbox isolation, current decision/commitment state, snoozing and forgotten
episodes, ordering, timeout fail-open, dedup, malformed headers and character caps.

Evidence logs: `/tmp/khipu-slice-c/evidence/focused-1.log`, `focused-2.log`,
`full-1.log`, `full-2.log`, `full-3.log`.

## Hermetic latency comparison

Command under the oracle's isolated environment, from `packages/cli`:

```sh
python3.11 tests/bench_t3_recall.py --baseline-ref 7147cca
```

The benchmark evaluates parent and current recall code in one process with
1,000 fixture episodes, a standing decision and an open commitment, 30 warmups
and 500 measured calls per version. Ordinary recall uses a SQLite fixture;
the new thread leg executes its actual SQL through a SQLite dialect adapter.
Hub transport, schema introspection and deliverable lookup are mocked. No
external API is called. These numbers measure local fixture work, not live
Postgres/network latency; the Mac was concurrently running the full suite.

| Metric | Before | After |
|---|---:|---:|
| Median | 0.679 ms | 1.177 ms |
| p95 | 7.093 ms | 7.226 ms |
| Mean | 1.641 ms | 2.253 ms |
| Rendered context | 249 chars | 415 chars |

Median overhead: 0.498 ms. The search deadline remains 1.2 seconds; the existing
optional budget lane retains its safety slack. Thread work shares that
budget. The existing separately bounded deliverable stage is unchanged.
Evidence: `/tmp/khipu-slice-c/evidence/latency-comparison.json`.

## Changed paths

- `packages/cli/khipu/t3.py`
- `packages/cli/khipu/session_capture.py`
- `packages/cli/khipu/capture.py`
- `packages/cli/khipu/embed.py`
- `packages/cli/khipu/hub_snapshot.py`
- `packages/cli/khipu/mcp_server.py`
- `packages/cli/khipu/recall_prompt.py`
- `packages/cli/tests/test_mcp_server.py`
- `packages/cli/tests/test_t3_slice_c.py`
- `packages/cli/tests/fixtures/t3_slice_c.py`
- `packages/cli/tests/bench_t3_recall.py`
- `docs/plans/2026-10-07-khipu-t3.md`
- `docs/plans/2026-10-08-khipu-t3-slice-c-verification.md`

## Decisions and remaining boundaries

- Store provenance in existing `raw` JSON; no migration or historical backfill.
  Episodes captured before this implementation remain unstamped.
- Cache misses for five minutes rather than permanently; cache hits for the
  session. When multiple projection rows match, the newest `updated_at` wins.
- Interleave standing decisions and open commitments so each surface gets
  space before additional items consume the block. Display is bounded; it
  does not promise to include every decision or commitment.
- Hub-unavailable continuity can supply replica decisions only. Live mapping,
  live gateway behavior and a real provider-switch recall remain unverified;
  verifying them requires the parent operator's authorized live acceptance.
- No UI changes, schema changes, release, push or PR are included.
