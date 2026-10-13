# Lexical index: the replica answers a keyword search in milliseconds, not a quarter second

Written 2026-10-12 after measuring the per-prompt lane under Matt's working load. This page is the scope; a
build session reads it first. Not from the 2026-10-10 audit; it comes from the live per-prompt log.
Related: `2026-10-11-replica-freshness.md` (same file, different problem), the per-prompt lane cost PR of
2026-10-12 (graph leg once per prompt, edge indexes, slug map, stale-served matrix), which goes first.

## Status

| Phase | What | State |
|---|---|---|
| 0 | Per-prompt lane cost outside the lexical leg (graph leg once, edge indexes, slug map, stale-served matrix) | done 2026-10-12 (PRs #151, #152) |
| 1 | FTS5 index on the replica for topics and episodes, LIKE kept as the fallback | done 2026-10-13, brought forward at Matt's request (he needed to run many agents that night). Built as a **trigram** index with coverage counted in SQL, not as decision 2 below first said; see "What the build found" |
| 2 | Retire the LIKE path once two nightlies have refreshed the index and the eval control is unchanged | open |

One PR per phase, merged alone and soaked.

## The defect, in one paragraph

Every per-prompt recall runs `hub_snapshot.search_snapshot` (`hub_snapshot.py:1650`), a per-token `LIKE
'%tok%'` over five episode columns (`_episode_ilike_columns`, `:1559`: summary, topics, decisions,
preferences, people; 14.7 MB of text over 15,070 rows) and three topic columns (body, slug, title; 6.1 MB
over 1,480 rows), ordered by a coverage score built from the same LIKE terms (`_token_match_sqlite`,
`:1569`). SQLite cannot index a leading-wildcard LIKE, so this is a full scan per token per column: 205 ms
for episodes and 56 ms for topics on an idle machine, 480 to 690 ms through `search_snapshot` at a load
average of 29, inside a lane whose keyword stage must finish within `LOCAL_LANE_DEADLINE_S = 0.95` of
`TIMEOUT_S = 1.2`. Since 15:05Z on 2026-10-12, 72 of 320 prompts timed out at the keyword stage and 163
returned no hits. An FTS5 table over the same columns answered the same seven-token query in 1.2 ms on a
copy built in 372 ms.

## Source facts (verified against main `0e88ce6`; paths under `packages/cli/khipu/`)

- Consumers of `search_snapshot`: the per-prompt lexical leg (`recall_prompt.py:547`, the only
  latency-bound caller) and the hub-unreachable fallback search (`hub_snapshot.py:2925`, also what
  `khipu_search` and the Stop hook use when Postgres is down). Both pass the same kinds, filters and limit.
- Tokens come from `search_text.search_tokens`: lowercased, stopwords and anything under three characters
  dropped (digits of two or more kept), capped at `MAX_TOKENS`. No stemming. The LIKE path matches
  substrings, so `change` matches `changed` and `unchanged`.
- Scoring today: `WHERE` any token hits any column; `ORDER BY` number of distinct tokens that hit (coverage),
  then recency for episodes and slug for topics. The lane then re-ranks by `token_hit_count` over label and
  snippet and fuses with the cosine leg by reciprocal rank (`recall_prompt._fuse_and_annotate`).
- Schema is created by `_create_schema` (`:680`, versioned by `SNAPSHOT_SCHEMA_VERSION`); `refresh()`
  (`:1071`) builds a new file from a full hub dump and replaces it; incremental writers `upsert_episode`
  (`:1190`) and the topic insert at `:1346` keep the live file current between dumps; both run under the
  snapshot lock. The nightly's `snapshot_refresh` step rebuilds the file every night.
- Both Pythons that open the replica have FTS5: Homebrew 3.11 (SQLite 3.53.3) and the app bundle's
  (3.53.1). Verified 2026-10-12 with `CREATE VIRTUAL TABLE ... USING fts5`.
- Tests that exercise `search_snapshot`: `tests/test_hub_snapshot.py`, `tests/test_prompt_lane.py`,
  `tests/test_prompt_lane_provisional.py`, `tests/test_recall_prompt.py`, `tests/test_audit1010_{capb,huba,emba}.py`,
  `tests/test_t3_slice_c.py`. Replicas are built in a temp dir with `_create_schema`; no Postgres.
- The eval control: `khipu recall eval --path prompt --record <file>` then `--compare <file>` on the same
  golden set (eight entries today in `~/.config/khipu/recall-golden.jsonl`). A control for main `0e88ce6`
  exists from 2026-10-12.

## Decisions

1. **External-content FTS5 tables, one per kind** (`episodes_fts` over the five episode columns,
   `topics_fts` over body, slug, title), `content=` the base table so the index holds no copy of the text.
   Triggers on the base tables keep them current, so `upsert_episode` and the topic insert need no new
   code and cannot drift. Tokenizer `trigram` (see decision 2); the replica grew by 72 MB.
2. **The FTS path returns the same rows in the same order as LIKE; only the speed changes.** Tokenizer
   `trigram`, so a quoted token is a substring match exactly as `LIKE '%tok%'` is. Coverage (how many
   distinct tokens hit any indexed column) is counted in SQL (`UNION ALL` of one `MATCH` per token,
   `GROUP BY rowid`) and ordered with the LIKE path's own tie-breaks. A query with any token under three
   characters goes to LIKE whole. `bm25` is not used. *(Superseded text: this decision first said unicode61
   prefix terms with a bm25 top-50 pool; that was wrong, see below.)*
3. **LIKE stays as the fallback** while the file lacks the FTS tables (a replica built by older code,
   until the next refresh) or an FTS query raises. Detection is one `sqlite_master` lookup cached per
   process per replica identity. Phase 2 removes the fallback only after two nightly refreshes and an
   unchanged eval control.
4. **The index is built by `refresh()` and by the schema version bump**, never lazily inside a prompt.
   Bumping `SNAPSHOT_SCHEMA_VERSION` makes the next refresh rebuild the file with the tables; between the
   merge and that refresh the lane uses LIKE. A human can bring it forward with `khipu snapshot refresh`.
5. **Filters are unchanged.** kind, since/until, project, session_id, harness, t3_thread, via keep their
   current behaviour (episode-only when any identity filter is active; Python-side time filtering with
   oversampling). FTS only replaces the WHERE and the first ordering.

## Phase 1: one build session

Read this page, then inspect `_create_schema`, `refresh()`, `search_snapshot`, `_token_match_sqlite` and
`tests/test_hub_snapshot.py` before writing anything.

1. Schema: the two external-content FTS5 tables and their insert/update/delete triggers in `_create_schema`,
   behind a version bump. A fixture replica built by the tests must carry them.
2. `search_snapshot`: when the tables exist, FTS candidates per kind (prefix terms, OR-ed, `bm25`
   order, oversampled 50), then the existing Python coverage sort and the existing filters; else the LIKE
   path unchanged. One log line the first time a replica falls back.
3. Tests: same rows and order as LIKE on a fixture for the golden-set prompts; prefix matching; a replica
   without the tables falls back; triggers keep the index current after `upsert_episode` and a topic
   insert; the `_id_shaped` node path is untouched.
4. Gate: hermetic suite at baseline, ruff at baseline, `khipu recall eval --path prompt --compare` against
   the 2026-10-12 control with no entry changed, and a read-only timing of `search_snapshot` on a copy of
   the live replica rebuilt with the new schema: under 20 ms for the seven-token prompt used on
   2026-10-12, same top rows.

Stop conditions: if the eval control changes, stop and show the diff rather than loosening the comparison;
if the index build in `refresh()` adds more than 60 s to the nightly dump, stop and report; if either Python
turns out to lack FTS5 on another machine, the fallback must carry it, never an import-time failure.

## What the build found (2026-10-13)

- The first build followed the original decision 2 (unicode61, prefix terms, bm25 top 50). On a copy of the
  live replica it returned the same rows as LIKE for 2 of 9 real queries: bm25 penalises long rows (a note
  holding 6 of 7 query tokens ranked 221st of 829), and a prefix term cannot find a token inside a longer
  word. The fixture corpus was too small to show it. Lesson: an order-preservation claim needs a parity run
  on real data, not a fixture.
- The second build is the trigram design above. `tests/tools/fts_parity.py` compares both paths on a copy of
  the replica over the last 400 distinct logged queries plus the golden set: 403 of 403 queries that took
  the FTS path were identical in rows and order (5 fell back for a short token), run twice independently.
  Keyword search at a load average of about 30: LIKE p50 197 ms, p90 247 ms; FTS p50 12.6 ms, p90 18.5 ms,
  max 63 ms. Index rebuild 3.3 s; a write with triggers 3 ms; `integrity-check` clean after insert,
  replace, forget and 20 upserts through the real writers.
- Known and accepted: trigram folds Unicode case where LIKE folds ASCII only, so a non-ASCII token could
  match more rows on the FTS path (not seen in 408 queries). `topics` has an implicit rowid, which a
  `VACUUM` would renumber under the index; nothing vacuums the replica, and a refresh rebuilds the file.
  A replica written by older code (the desktop app's bundled CLI before its next build, a long-lived
  process) has no FTS tables and stays on LIKE until the next refresh by current code.
- Phase 2 (retire the LIKE path) stays open and is not urgent: LIKE is also the fallback for short tokens.

