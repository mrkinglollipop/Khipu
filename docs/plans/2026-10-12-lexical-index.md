# Lexical index: the replica answers a keyword search in milliseconds, not a quarter second

Written 2026-10-12 after measuring the per-prompt lane under Matt's working load. This page is the scope; a
build session reads it first. Not from the 2026-10-10 audit; it comes from the live per-prompt log.
Related: `2026-10-11-replica-freshness.md` (same file, different problem), the per-prompt lane cost PR of
2026-10-12 (graph leg once per prompt, edge indexes, slug map, stale-served matrix), which goes first.

## Status

| Phase | What | State |
|---|---|---|
| 0 | Per-prompt lane cost outside the lexical leg (graph leg once, edge indexes, slug map, stale-served matrix) | in flight 2026-10-12, branch `fix/per-prompt-lane-cost`; merge and soak one night before phase 1 |
| 1 | FTS5 index on the replica for topics and episodes, LIKE kept as the fallback | open |
| 2 | Retire the LIKE path once two nightlies have refreshed the index and the eval control is unchanged | open |

One PR per phase, merged alone and soaked. Phase 1 waits for a clean nightly after phase 0.

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
   `topics_fts` over body, slug, title), `content=` the base table so the index holds no copy of the text
   and the replica grows by the index alone (tens of MB, not a second copy of 20 MB). Triggers on the base
   tables keep them current, so `upsert_episode` and the topic insert need no new code and cannot drift.
   Tokenizer `unicode61` with `remove_diacritics 2`.
2. **Query semantics stay substring-like where it matters.** Each token becomes a prefix term (`tok*`), OR-ed,
   so `change` still finds `changed` and `changes`; it no longer finds `unchanged`. The candidate set comes
   from FTS (top 50 by `bm25`), and the coverage order is recomputed in Python with the existing
   `token_hit_count` over the same columns, so the result order for any row the old path also returned is
   unchanged. This is the stop-condition the eval control checks.
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
