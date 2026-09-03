# Khipu memory reliability — scope (2026-09-03)

**Status: SHIPPED 2026-09-03.** Merged to `main` in PR #54 (`a8e489c`) plus a follow-up fix PR; migrations 0008–0011 live on the hub; gateway on the Linode rebuilt from `main`; every local harness pack re-pointed to the main checkout and verified (recall probe green on Claude Code, Cursor, Codex, Aegis and the grok-bot gateway). Both destructive backfills were applied after a binary COPY backup of all affected tables (`~/Library/Application Support/Khipu/backups/pre-backfill-20260903T162129/`): identity backfill set project/repo_root on 429 episodes; junk-path purge cut `path:` nodes 4207 → 933 with zero dangling edges. Incident during verification, self-inflicted and reversed: an over-broad cleanup loop forgot ten real episodes returned as nearest neighbours by a hybrid search; all ten were restored with embeddings intact, and the embed backfill/catch-up/orphan/coverage paths now all exclude tombstoned rows. Deferred: W5.5 decisions-as-rows; the desktop 0.3.15 build+publish (needs the maintainer's Apple notarization credentials). This is the scope artifact that build briefs point at; it is deliberately the only place the design is written down.

**Origin.** A live Aegis build session asked "is my follow-up in Khipu?" and
got the wrong answer from the default search even though capture had worked.
The session's recommendation (R1–R6, quoted in the appendix) is folded in here,
verified against the hub, and extended with the gaps the survey found.

**Rule for every item:** the fix lands in the shared engine, not in one
harness. Khipu today serves Claude Code, Cursor, Codex, Aegis, Grok Bot (HTTPS
gateway) and Cursor cloud. Section 5 is the per-harness matrix; a workstream is
not done until every column is either "automatic" or explicitly verified.

---

## 1. Evidence (measured 2026-09-03, hub snapshot 16:15Z + live PG)

| Measurement | Value | Meaning |
|---|---|---|
| Sessions in last 30 d with exactly one episode | 1087 of 1225 | identity is per-capture, not per-conversation, for most rows |
| Episode pairs from different session ids within 5 min, last 30 d | 2183 | two writers or split identity, no dedup |
| `scope` column shapes | 5741 free-text labels, 518 empty, 349 absolute paths, 171 worktree paths, 30 `/tmp` | scope is a model guess; cannot filter by repo |
| Top `scope` values last 30 d | `build` 598, empty 506, `general` 239 | see above |
| Distinct capture-topic slugs vs slugs that exist as topic pages | 9783 vs 547 | 94 % of topic links dangle |
| Most-used capture-topic slugs | `aegis` 423, `left-panel-ux-97b929` 406, `tmp` 378, `claude` 323 | worktree tokens and cwd basenames become topics |
| `path:` graph nodes that are not paths (`a/b`, no dot, no `/`, no `~`) | 3048 of 4196 | 73 % of path nodes are noise |
| Episodes with empty `people` | 6635 of 6809 | the native prompt never asks for people |
| Episodes with no decisions | 912 | fine; noted |
| Distinct topic `status` values | 80+ (`active`, `Active`, `"active"`, `LIVE - IWM bot deployed` …) | no vocabulary |
| Topic frontmatter `last_updated` parsed into PG | 0 of 598 | freshness of a topic page is invisible to ranking |
| `~/.claude/projects/*/memory/*.md` indexed as topics | 0 | durable per-repo notes unreachable from search |
| Embedding coverage on active profile | 6809 / 6809 episodes | good |
| Hub snapshot age at time of check | 87 min | refresh only piggybacks on `khipu doctor` |
| Default search for the follow-up (`mobile oracle follow-up W131`) | 4 topic hits above the right episode | per-kind fairness beats score |
| Semantic search, same query | episode 11287 / 11286 top two | the embedding was fine |

Reproduced the recommendation's item 3 exactly: episodes 11283/11285/11287 are
`claude_code:fb929043…` with scope = worktree path; 11286 is
`claude_code:c631b166…` with scope `/private/tmp`; 11284 is
`claude_code:d84a573b…` with scope `aegis project`. 11286 and 11287 record the
same follow-up two minutes apart. Working hypothesis (verify in W1 by reading
`episodes.raw` for 11286): c631b166 is a *dispatched* `claude -p` child started
from the scratchpad directory, so it is a real second session with its own id
and cwd, not a hook keying on cwd. Either way the fix is the same: lineage +
repo-root identity + ingest-time dedup.

## 2. Root causes (six shapes, not thirty bugs)

**A. Identity is unstructured.** `session_id` is the harness id (good) but
`scope` is whatever the model wrote (`extract.py:256`), falling back to
`"<harness> <event>"`. There is no repo-root or project column. The cwd
basename is appended to `topics` unconditionally (`extract.py:247-250`).
Worktrees, scratchpads and child sessions all fragment the record.

**B. Two writers, no dedup.** The only uniqueness is `(ts, md5(summary))`
(`0003_reconcile_upsert.sql:19`), which catches replays and nothing else.
Legacy hooks are gone from Claude Code settings (verified today: only
`khipu-stop-hook` + the nudge remain) but the 5-minute cross-session pairs are
still 2183 in 30 days, so the second writer is now child sessions and
compaction+stop overlap, not `capture_v2`.

**C. Retrieval defaults to the weakest signal.** Default is ILIKE token
overlap split fairly across topic/episode/node regardless of score
(`cli.py:340-478`); `kind` is validated then dropped on that path
(`mcp_server.py:307-317`); nodes appear in default but can never appear in
semantic (nodes are not embedded); no `since`/`project`/`session` filters; no
recency prior; snapshot refresh has no ingest trigger.

**D. The extraction schema has no open-loop concept.** Prompt keys are
`summary/topics/decisions/preferences/scope`. Decisions are immutable strings
in a JSONB array. Nothing can be opened, closed, superseded or retracted.

**E. Topic and graph hygiene is absent.** 94 % dangling topic slugs, 73 % junk
path nodes, no status vocabulary, no `last_confirmed`, no supersession edge,
no purge command (promised in a migration comment, never written), no episode
soft-delete. Per-repo memory notes are not indexed. The pushed slice for this
very session offered `khipu-status` dated 2026-08-12 as current.

**F. Health checks measure plumbing, never answers.** Doctor is green when
mirror lag, coverage and drift are fine. There is no "capture then search finds
it" probe, no fragmentation/duplicate/dangling metrics, no query log, and the
pushed slice swallows every exception into an empty slice.

## 3. Workstreams, ranked by payoff

Each item: change → harness impact → acceptance. "Engine" means
`khipu.session_capture` / `khipu.capture` / `khipu.mirror` / `khipu.cli`
search functions, which every harness already shares.

### W1. Stable identity + ingest dedup (R3, extended)

1. **New episode columns** (migration 0008): `harness`, `repo_root`,
   `project` (canonical: git remote slug if any, else basename of repo_root),
   `parent_session_id`, `transcript_range` (`start_offset:end_offset`).
   `scope` stays as the free-text label it already is.
2. **Resolve repo_root in the hook, not the model.** From the hook's cwd:
   `git rev-parse --show-toplevel`; if the path contains `/.claude/worktrees/`,
   `/.cursor/worktrees/` or `/.codex/worktrees/`, or `--git-common-dir` differs,
   resolve to the main checkout. Scratchpad/`/tmp` cwd → repo_root `null`,
   project inherited from `parent_session_id` if known.
3. **Lineage.** Hook reads `KHIPU_PARENT_SESSION` (dispatchers export it) and
   any parent/agent field the harness payload already carries. Stop appending
   the cwd basename to `topics`; it becomes `project`.
4. **Dedup at ingest** (`capture.write_pg`): (a) exact window — same
   `(harness, session_id, transcript_range)` → skip; (b) overlap — same
   `project`, `ts` within 5 min, summary cosine ≥ 0.92 → merge into the
   earlier row (union decisions/preferences/topics, keep both session ids in
   `raw.merged_from`). Threshold is a config knob; log every merge.
5. **Backfill** (dry-run first, then Matt's go): set `repo_root`/`project`
   where `scope` is an absolute path; collapse the fb929043/c631b166/d84a573b
   cluster from the origin session as the worked example.

Harness: engine → automatic for Claude Code, Cursor, Codex. Aegis: the
sandboxed hook can run `git rev-parse` in the project dir (allowed); verify.
Gateway (`khipu_capture`): `project` becomes a required argument; Grok Bot
packs pass the repo slug they already know.

Accept: one conversation, three cwds at Stop time → one session id, one
`project`; the origin cluster shows as one lineage; 5-min cross-session pair
count on new rows drops below 1 % of episodes.

### W2. Hybrid retrieval by default, ranked by score (R1, R5, R6)

1. **Default = fused.** Every `khipu_search` runs cosine oversample + token
   overlap + literal ILIKE as three rank lists fused by RRF (the RRF code in
   `search_text.hybrid_rerank` already exists; ILIKE becomes a third list).
   `mode: "literal"` keeps today's exact-substring behaviour for ids, hashes,
   error strings.
2. **Fairness only fills.** Sort the fused list by score; apply per-kind
   quotas only to the tail so a near-zero topic never outranks a direct
   decision hit. Nodes: excluded from default results unless `kind: node` or
   the query is id-shaped (contains `:` or `__`); this removes the asymmetry
   between modes.
3. **Filters that exist today in name only.** `kind` honoured on every path;
   add `project`, `since`, `until`, `session_id`, `harness`. Mild recency
   tiebreak (same score → newer first).
4. **Snapshot refresh on ingest.** After a successful PG write, upsert that
   episode row and its embedding into `hub_snapshot.sqlite` (local, cheap);
   full dump stays on the doctor throttle. Status turns the snapshot row red
   when `snapshot.refreshed_at` < `latest_ingested_at` by more than 30 min.
5. **Query log.** Append `{ts, harness, query, mode, top ids, followed_by_get}`
   to a local JSONL; this is the free training set for W6's golden queries and
   the zero-result detector.

Harness: engine → automatic for all six (the gateway and MCP server call the
same functions). Recall rule text (`recall_rule.RULE_MD`, Cursor `.mdc`, Codex
AGENTS block, Aegis MCP instructions) updated once to say "default is hybrid;
use `mode: literal` for exact strings".

Accept: the origin query in default mode returns episode 11287 or 11286 in the
top 3; `kind: topic` returns only topics; `since: 7d` excludes older rows.

### W3. First-class commitments (R2)

1. **Extraction.** Add to the prompt and parser: `open_loops`
   `[{text, kind: followup|blocker|question|promise, due_after, owner}]` and
   `closed_loops` `[{text}]` (explicit "done", "merged", "shipped",
   "no longer needed"). Keep `decisions` as is.
2. **Table** `commitments(id, text, project, owner, kind, opened_episode,
   opened_at, due_after, status open|closed|stale, closed_episode, closed_at,
   close_reason, content_hash)` plus an embedding row per commitment on the
   active profile.
3. **Auto-close at ingest.** For each new episode: every `closed_loop` and
   every decision is matched against open commitments in the same `project`
   (cosine ≥ 0.85, config knob, log every close with the matched text).
   Explicit `done: <text>` in a decision closes unconditionally. Open > 30 d
   → `stale`, never silently dropped.
4. **Surfaces.** MCP `khipu_owed(project?, status?)`; CLI `khipu owed
   [--close ID] [--reopen ID]`; gateway exposes the same tool; the pushed
   slice (W4) leads with open commitments for the repo; doctor reports
   open/stale counts.

Harness: extraction and table are engine-side → automatic for all capture
paths. Recall surface: Claude Code/Cursor/Codex via pushed slice + tool;
Aegis has no pushed slice (stdout discarded at SessionStart) so its recall
rule text tells it to call `khipu_owed` at session start; Grok Bot via
gateway tool.

Accept: the recommendation's acceptance test verbatim: end a turn with
"remember to do X after Y"; within 2 min `khipu_search("X")` default finds it
top-3, `khipu_owed` lists X open with the right project; a later "X is done"
closes it with `closed_episode` set.

### W4. Pushed slice scoped by repo, then recency (R4)

1. Resolve `repo_root`/`project` with the W1 helper (shared code, one
   implementation). Slice = open commitments for the project (≤5) → last N
   episodes for the project (≤5, newest first) → topic pages linked from those
   episodes by real edges (≤3, each stamped with its age) → only then the cwd
   token fallback. Hard budget ~1500 tokens.
2. Fail visibly: on PG failure fall back to the snapshot with a `stale` line,
   never an empty slice; log the exception.
3. **Index harness-native notes as topics.** A nightly reconcile mirrors
   `~/.claude/projects/<slug>/memory/*.md` (map slug → repo path), Codex
   memories, and any Cursor rule files the user marks, into `topics` with
   `source_path` and `kind = note`, through the existing
   `mirror.mirror_topic_file`. They become searchable and slice-eligible.

Harness: SessionStart hook exists for Claude Code, Cursor, Codex → automatic.
Aegis: none (documented limit); compensated by the recall rule + `khipu_owed`.
Gateway: no session start; Grok Bot packs call `khipu_search` with `project`.

Accept: starting a session in this worktree pushes Khipu-project episodes and
this scope doc's topic, and nothing from FT Command/FT Terminal.

### W5. Extraction and graph hygiene (new)

1. **Topics vs tags.** A capture `topic` must resolve to an existing topic
   page (exact slug, alias table, or embedding nearest ≥ 0.90); unresolved
   slugs are stored as `tags`, never as `topic:` nodes. Drop slugs matching
   worktree tokens (`-[0-9a-f]{6}$`), `tmp`, harness names and a short generic
   list. Alias table `topic_aliases(alias, slug)` seeded from the current
   dangling set by the merge job.
2. **Paths.** A `path:` node needs a real path shape (extension, leading `/`
   or `~`, or exists under `repo_root`). Backfill removes the 3048 junk nodes
   — destructive, dry-run report first, Matt's go required.
3. **Status vocabulary.** Normalize to `{seed, active, shipped, superseded,
   abandoned, evergreen}` at mirror time; raw value kept in `frontmatter`.
   Parse `last_updated`/`created` into columns so freshness can rank.
4. **People.** The native prompt never asks for people; either add the key or
   drop the field from the embedding text. Recommendation: add it, since the
   legacy rows have it.
5. **Decisions as rows** (phase 2 of this workstream): `decisions(id, text,
   project, episode, status active|superseded, superseded_by)` with an
   embedding; supersession = same project, cosine ≥ 0.85, newer episode,
   negation or "instead/now/rather" cue. Feeds `supersedes` edges into the
   graph, the first temporal edge type Khipu has.
6. **Forgetting.** `episodes.deleted_at`, `khipu episode forget <id>`, and the
   `khipu topic purge` command the migration comment promised; tombstoned rows
   drop out of search and embeddings.

Harness: engine-side, automatic. The Aegis native extractor and the gateway
`khipu_capture` payload both pass through the same post-processor.

Accept: dangling-topic ratio on new episodes < 5 %; junk path ratio < 5 %;
`khipu graph <slug>` on a topic with a superseded decision shows the edge.

### W6. Recall-quality observability (new)

1. **End-to-end probe in doctor.** Write a nonce episode through the normal
   capture path (`project = khipu-probe`), assert default search finds it
   top-3 within 120 s, then forget it (W5.6). Red on failure. Runs per
   harness pack's verify command so "the host runs it" is actually proven.
2. **Metrics in doctor/status:** sessions-with-one-episode ratio,
   5-min cross-session pair count, dangling-topic ratio, junk-path ratio,
   commitments open/stale, snapshot age vs last ingest, query zero-result
   rate (from the W2 log), pushed-slice error count.
3. **Golden set.** `docs/recall-golden.jsonl` (query → expected id), grown
   from the query log; `khipu recall eval` prints hit@3 and is part of the
   soak checklist. Not a CI gate (GitHub Actions is not used).

Harness: doctor is shared; the probe runs once per installed harness pack.

## 4. What this scope explicitly does not do

- No model-invoked capture "to be sure" (agreed with the recommendation; the
  hook is the writer and a manual write double-captures).
- No hand-maintained "owed items" topic page.
- No new embedding model or profile; coverage is already 100 %.
- No change to the file-wiki dual-run or the soak-gated legacy-hook removal;
  those stay on their own track.

## 5. Harness matrix

| Workstream | Claude Code | Cursor | Codex | Aegis | Grok Bot / gateway | Cursor cloud |
|---|---|---|---|---|---|---|
| W1 identity/dedup | engine, auto | engine, auto | engine, auto | engine; verify `git rev-parse` inside sandbox | `project` arg required on `khipu_capture` | same as gateway |
| W2 hybrid search | MCP, auto | MCP, auto | MCP, auto | MCP, auto | gateway, auto | gateway, auto |
| W3 commitments | extract + slice + tool | extract + slice + tool | extract + slice + tool | extract + tool; rule text says call `khipu_owed` at start | extract + gateway tool | extract + gateway tool |
| W4 pushed slice | SessionStart hook | sessionStart hook + `.mdc` | SessionStart hook | none (documented) | n/a | n/a |
| W5 hygiene | engine, auto | engine, auto | engine, auto | engine, auto | engine, auto | engine, auto |
| W6 probe | pack verify | pack verify | pack verify | pack verify (queue + drain) | gateway verify | gateway verify |

## 6. Phasing (one brief per session slice)

| Phase | Session | Contents | Gate |
|---|---|---|---|
| 1 | A | W1.1–W1.4 migration + hook + dedup, tests | migration reviewed; Linode PG write needs Matt's OK |
| 1 | B | W1.5 backfill dry-run report → apply on go | Matt's go |
| 2 | A | W2.1–W2.3 fused default, filters, fairness-as-fill | golden mini-set green |
| 2 | B | W2.4–W2.5 snapshot-on-ingest, query log, rule text | doctor shows snapshot red/green correctly |
| 3 | A | W3 extraction + table + auto-close | acceptance test from §3.W3 |
| 3 | B | W3 surfaces + W4 slice + note indexing | slice acceptance |
| 4 | A | W5.1–W5.4, W5.6 | dry-run reports |
| 4 | B | W5.2 backfill + W5.5 decisions-as-rows | Matt's go (destructive) |
| 5 | A | W6 | doctor probe green on all installed packs |

Every brief opens "Read docs/plans/2026-09-03-memory-reliability.md first;
inspect the engine functions it names before writing." Oracle for every slice:
from `packages/cli`, `PYTHONPATH="$PWD:/Applications/Khipu.app/Contents/Resources/khipu/lib" python3 -m pytest -q` (662 tests collect this way on 2026-09-03; bare `python3` lacks psycopg, the bundle Python lacks pytest; README line 397 documents the `.python_libs` install as the clean alternative). Max three runs per change.

## 7. Decisions needed from Matt

1. Approve the scope and the ranking, or reorder.
2. Two destructive backfills (junk path nodes; fragmented-session merge) —
   dry-run report first, then an explicit go each.
3. Nodes out of default search results (W2.2) — yes, or keep them.
4. Auto-close threshold posture: err toward leaving commitments open (fewer
   false closes, more stale rows) — recommended — or toward closing.
5. Migrations run against the live shared Linode PG — each one needs the
   standing write authorization.

## Appendix — the originating recommendation (2026-09-03)

R1 hybrid default with literal opt-in · R2 first-class commitments with
`khipu_owed` · R3 stable session identity keyed on harness id + repo root,
dedup, backfill · R4 slice by repo then recency, index the per-repo memory
files · R5 rank across kinds by score, fairness only fills · R6 snapshot
refresh on capture. Not recommended: model-invoked capture; a hand-kept owed
page. Acceptance: "remember to do X after Y" → default search top-3 within
2 min, `khipu_owed` lists it, "X is done" closes it, one session id per
conversation regardless of cwd.

## 8. Recall quality (W6, built 2026-09-03)

`khipu.probe.run_probe` writes a nonce episode via the normal capture path,
polls default hybrid search up to 120s for top-3, then forgets it; result
goes to a state file. `khipu doctor --probe` is the only write path; plain
`khipu doctor`/`status` only read the last result (`recall_probe_ok`, red if
missing/failed/>7d old — a hard gate) and a `recall_quality` block (drift.py:
`recall_quality`) of warn-only ratios — fragmentation, dangling topics, junk
paths, commitments, query zero-result rate, snapshot lag. `khipu integrations
verify` runs the probe once per installed pack, including grok_bot.
`docs/recall-golden.jsonl` + `khipu recall eval [--golden PATH]` (recall_eval.py)
score hit@k against default search, exit 1 below 0.8 — not a CI gate, a soak
check.
