# Measurements behind the 28 September review

These are the numbers the [review](../../hindsight-plan-review-2026-09-28.md) cites, with how each was taken. Queries, vectors and result ids are private and are not stored here; the control files stay on the maintainer's machine.

## Test suite

| Run | Command | Result |
|---|---|---|
| Baseline, `main` at `9d7d7e0` | `pytest` from `packages/cli`, real home directory | 11 failed, 1635 passed, 13 skipped |
| Same, `unittest discover` as then documented | | 9 failures and 12 errors of 1641 |
| Same, isolated home directory and Keychain off | | the same 11 failures; 58 skipped |
| After Phase 0 session A, no manual isolation | `pytest` from `packages/cli` | 10 failed, 1599 passed, 59 skipped; live launchers and replica untouched |

The 11 baseline failures are stale tests: two in `test_embed_media`, three in `test_graph_backup`, four in `test_session_capture` (`ConversationImageLandTest`), one in `test_topic_graph`, and the replica `since` filter test, whose fixture dates had aged out. The last one is repaired in Phase 0 session A.

## What the unisolated suite did to a configured machine

Observed after two runs with the real home directory:

- Three of five launcher links under the data directory's `bin` were re-pointed at the checkout under test, which had no vendored libraries. They were restored within seven minutes; the capture log shows no capture attempted in that window.
- Probe episodes written and deleted by two live tests remained in the local replica. A full replica refresh removed them. The hub held no probe rows afterwards and no orphaned embeddings.
- The query log gained eight test queries. The embed budget counter, the notes reconcile state and the desktop app's `versions.json` were rewritten.

## Per-prompt recall lane in production

Source: the hook's own log, entries whose session id is a UUID, 14 to 28 September 2026.

| | Calls | Timed out | Returned results | Suppressed as duplicate |
|---|---|---|---|---|
| Whole period | 2,631 | 2,249 (85.5%) | 350 | 28 |

Daily timeout rate: 67% on the first day, 75%, then between 81% and 97% on every later day with more than 20 calls. Latency of the calls that returned results: median 1,088 ms, 90th percentile 1,183 ms, against a 1,200 ms budget.

Per leg, measured in one process on the production-size replica (about 18,400 scored embedding rows, 768 dimensions), median of five prompts:

| Leg | Before |
|---|---|
| Keyword search | 131 ms |
| Query embedding, not cached | 624 ms (539 to 851) |
| Query-vector cache, load | 46 ms (one 8.5 MB JSON file) |
| Vector scan | 226 ms |
| Row metadata | 6 ms |

## Phase 0 session C, before and after

Both versions ran back to back on the same machine, on a frozen copy of the production replica.

**Same inputs, same outputs.** 149 real queries sampled with a fixed seed from the query log. The query vectors were recorded once and injected into both versions, so neither called the embedding API.

| | Before | After |
|---|---|---|
| Entries with identical rows, order, scores and rendered block | | 149 of 149 |
| Search time, median | 595 ms | 304 ms |
| Search time, 95th percentile | 786 ms | 408 ms |

**Cold path, as the hook runs it.** `prior_work_for_prompt` with no budget, a real embedding call per prompt, prompts that had never been seen (each carried a unique suffix), two rounds of 40 prompts per version, machine load average about 15.

| | Before | After |
|---|---|---|
| Timed out | 21 of 80 | 0 of 80 |
| Returned recall | 59 of 80 | 80 of 80 |
| Median | about 1,110 ms | about 540 ms |
| 90th percentile | about 1,215 ms | 662 ms and 846 ms |
| Slowest | 1,219 ms | 975 ms |

The in-process benchmark understates the production timeout rate because the hook is a new process per prompt and the machine is often under far heavier load. It measures the change, not the absolute rate.

The new doctor check, run read-only against the live hook log on the afternoon of 28 September while the old code was still the deployed hook: 100 of the last 100 searched prompts had timed out, median 1,209 ms.

## Production counts used in the review

Read-only queries against the hub on 28 September: 40,409 decision rows, none superseded, none with a rationale; 92 decision rows belonging to forgotten episodes; 31 groups of duplicate decision text; 10,062 live and 68 forgotten episodes; 1,278 topics with 17 distinct status values, 50 of them `superseded` and none with `superseded_by` set; 497 deliverables; 157,539 edges, of which 40,354 are `wiki_link` and 3,934 `lives_in`; migrations applied through `0023`; PostgreSQL 19 beta 3.

## Isolated PostgreSQL

`packages/cli/scripts/scratch_pg.sh up` builds a throwaway cluster from any PostgreSQL on the path, listening on a Unix socket only. On PostgreSQL 16 without pgvector it loads 17 tables and records all 24 migrations; 17 statements that need pgvector or property graphs are skipped and the two vector tables are replaced by stand-ins.
