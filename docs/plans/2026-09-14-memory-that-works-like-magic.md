# Memory that works like magic — scope (2026-09-14)

Status: **approved direction (maintainer, 2026-09-14): "I need all issues fixed. We need Khipu rock solid
everywhere. The entire experience is supposed to work like magic for the user. They shouldn't have to
think about whether or not things are working. They should just work."**

Source of truth: six read-only audits run on 2026-09-14 against the code and the live hub (8,199
episodes, 1,102 topics, 1,028 commitments). Their raw notes live in the maintainer's ops repo; every
finding below cites the code it came from.

## The incident that started it

A scope document was approved on day 0 and stored in Khipu three ways (episodes with `decisions`, a
memory-dir note ingested as a topic, the files on disk). On day 4 the user said "start building the
scope for X" and the assistant dispatched a fresh scope-writing agent. Storage was fine; a hybrid search
returned the note as hit #2. Nothing ran that search. Then, when the assistant wrote a seven-point
diagnosis of the failure, the fixed capture cadence (5 turns / 20 min) skipped the two turns that
carried it, the MCP capture tool refused by design, and the note it wrote instead was unsearchable until
the next nightly.

Every one of those is a class, not a one-off. The audits found the classes.

## The bar

The user never checks whether memory worked. Concretely, each of these is a contract with a test:

1. **Prior work surfaces by itself.** Naming a topic in a prompt brings the relevant stored work into
   context before the model acts, in every harness that can inject; where a harness cannot inject, the
   first tool call returns it.
2. **Nothing said is lost.** A turn that carries a decision, a correction, a diagnosis or an explicit
   "remember this" is captured within a minute, not at the next cadence tick; long windows are never
   tail-clipped; exact strings (errors, commands, paths, the user's own words) survive summarisation.
3. **Deferred work comes back.** "Do X once Y happens" becomes an owed followup with its trigger and
   stays in the session-start slice until closed.
4. **Written is searchable within a minute,** whether it was written by a hook, a tool, or a person
   editing a memory file.
5. **The system says so when it cannot.** Every drop, lag, degrade and stale path above turns
   something red in `khipu doctor` and on Home, in plain words, with the one action; nothing stays green
   because evidence never arrived.
6. **Memory stays organised without the user.** Indexes stay under their host's load cap, ledgers are
   split, duplicates are merged, superseded facts are marked, all by Khipu.

## Findings, grouped by the contract they break

Severity: C critical, H high, M medium. Harness columns: CC Claude Code, Cu Cursor, Cx Codex, Ae Aegis
(observe-only hook gates), GW cloud harnesses via the HTTPS gateway.

### 1. Recall that fires by itself

| # | Finding | Sev | Harness | Evidence |
|---|---|---|---|---|
| R1 | No prompt-time recall exists; the rule text says "recall is on demand" and the model measurably does not search | C | all | `recall_rule.py:77`; zero `UserPromptSubmit` handling in `integrations.py` |
| R2 | Session-start slice is recency-only: 5 newest episodes and the first 3 topics met in them; a 4-day-old decision is invisible | H | CC Cu Cx | `activity.py:257-262` `ORDER BY ts DESC LIMIT 5`, topics at 277-296 |
| R3 | The slice is project-scoped but search is global, so feedback *about* this project written while working in another project never reaches this project's slice | H | CC Cu Cx | `activity.py:239-253, 301-308` |
| R4 | Search cannot say "nothing stored": RRF scores are rank-derived and sit in a 0.13–0.20 band for gibberish and real hits alike; no confidence, no matched-token count | H | all | `search_text.py:129`; gibberish query scored 0.14, real best 0.20 |
| R5 | A semantically similar document from the wrong project outranks the right one (reproduced twice: a contradicting decision at #1, the real scope at #3) | H | all | live searches, `recency.py`, no project boost |
| R6 | `topics.status` (superseded/retired/abandoned, free text incl. an emoji) is never read by search or the slice | H | all | `cli.py:568` selects slug/title/body only |
| R7 | Recency ranks by re-index time: 48% of topics share this morning's reconcile timestamp and read as "0d old" | H | all | `mirror.py:227` `COALESCE(updated_at, now())` |
| R8 | Hybrid search costs 3–6 s (literal ILIKE pass 1.4–4 s, no trigram/tsvector index, sequential scans over 67k rows); no per-prompt lane can afford it | H | all | `pg_indexes`; timing from `--json` |
| R9 | Pushed memory is injected with the same authority as the rule; no untrusted-data fence, no field separating the user's verbatim words from the model's paraphrase | H | CC Cu Cx | `recall_rule.session_start_context`; zero "untrusted" strings in the repo |
| R10 | Aegis gets no inject at all; the gateway gets rule text only, no slice, no commitments | H | Ae GW | `recall_rule.py:17-19`, `mcp_server.py:781-793` |
| R11 | Trivial prompts ("ok", "yes") return five hits; there is no floor and no gate | M | all | `search_tokens()` already returns `[]` for them, unused here |

### 2. Capture that cannot lose

| # | Finding | Sev | Harness | Evidence |
|---|---|---|---|---|
| K1 | No on-demand capture: the hook decides only on Stop/PreCompact/SessionEnd by cadence; the MCP tool refuses whenever a hook owns capture; nothing a user or model can invoke | C | CC Cu Cx Ae | `session_capture.py:711-724`, `mcp_server.py:572-590`; log: turns 1–4 "not due", captured at turn 5 |
| K2 | Only a 1–3 sentence summary plus string lists is stored; the transcript window is never attached; error strings, commands, paths and quotes are destroyed at capture | C | all | `extract.py:24-30, 296-341`; `capture.py:261-283` raw = metadata only |
| K3 | Long windows are tail-clipped to the last 14,000 chars and the offset advances past the whole window; the middle is gone, unrecorded (4.8% of captures hit the cap; one range was 0:75,932) | C | all | `session_capture.render()` `out[-max_chars:]` |
| K4 | Subagent work is never captured and gets no slice: no `SubagentStop` hook is installed | H | CC | `integrations.py:244` installs Stop/PreCompact/SessionEnd only |
| K5 | Dedup merge drops the newer capture's summary (unions lists only), inside a 5-minute window at Jaccard 0.6 | H | all | `capture.py:226-314, 129-158` |
| K6 | `project` is NULL on 81% of episodes; the fallback `scope` is free text (3,851 values, 2,541 over 40 chars, worktree paths); commitments fall back to `session_id` so 16+ open items are filed under a session id | H | all | `identity.py:83-135`; `COALESCE(project, scope)` at `activity.py:245`, `embed.py:1289`, `hub_snapshot.py:1023`; `commitments._coalesce_scope` |
| K7 | SessionEnd with a missing transcript is a silent zero-capture | M | CC | log `sessionend due=False transcript missing` |
| K8 | An unrecognised harness heartbeats forever (147 dispatches, "no transcript path") and is never checked | M | any | `~/.grok/khipu/dispatch/unknown.json`; `HARNESSES` excludes it |
| K9 | Gateway captures have no liveness: 401/429/silence stays green | H | GW | `session_capture.py:67`, `liveness_all()` |
| K10 | The Aegis queue has no drainer of its own; it waits for another harness's Stop or the nightly | M | Ae | `khipu-aegis-capture` enqueues only |

### 3. Commitments and decisions that survive

| # | Finding | Sev | Harness | Evidence |
|---|---|---|---|---|
| O1 | "Deferred until X" is not a future trigger (`until`, `before`, `close(s)` missing from the regex), so the session-end rule closes it as `session-ended` at the very next Stop — the incident's cause #2, reproducible | C | all | `commitments.py:190-207, 1073-1077`; `has_future_trigger("… until the ledger closes") → False` |
| O2 | No decisions registry: 36,967 decision strings across 6,815 episodes, no date, rationale or `superseded_by`; a reversal ranks beside the original forever | H | all | `information_schema` has no `decisions` table |
| O3 | `due_after` is wired (extraction and snooze can set it) but is null on all 1,028 rows, so parking never happens in practice and user-owned blockers from 9 days ago still lead the slice at priority 0 | H | all | `commitments.py:27-28, 745`; live count |
| O4 | No deliverables index: 2,193 `path:` nodes come from topic bodies, none from episodes; "have I already built this?" is unanswerable | H | all | nodes `source_id IS NULL` |

### 4. Freshness within a minute

| # | Finding | Sev | Harness | Evidence |
|---|---|---|---|---|
| F1 | Memory-dir notes reconcile only on the nightly; the function named `_if_due` has no due check; no mtime filter (697 rewritten nightly) | H | CC Cx | `jobs.py:350`, `notes.py _build_plan` |
| F2 | The Stop-hook embed catch-up covers episodes only; topics wait for the nightly even after reconcile | H | all | `embed.py:1762` |
| F3 | A missing/expired embedding key aborts the whole nightly backfill; only "budget exhausted" is caught; red a day later via coverage | H | all | `embed.py:158, ~880` |
| F4 | Search degrading to literal is a buried `degraded` key; no aggregate, no doctor check | M | all | `embed.py:1565-1578`, `mcp_server.py:423` |
| F5 | Notes never tombstone on delete/rename | L | CC Cx | `notes.py:24-30` |
| F6 | Cursor and Aegis memory dirs are not ingested at all | M | Cu Ae | `notes.py` scans Claude projects + Codex only |

### 5. Organisation without the user

Measured on the maintainer's Mac: one project's memory dir holds 198 notes (1.32 MB) with its index at
23.9 KB against the host's ~24 KB load cap; one "note" is 395 KB (a running ledger), four more are
50–57 KB; the host slugged the same project path two ways, so three or four directories hold identical
copies and Khipu ingests each as a separate topic.

| # | Finding | Sev | Harness | Evidence |
|---|---|---|---|---|
| G1 | Nothing keeps a host's memory index under its load cap; overflow lines are dropped silently by the host. Khipu never reads or writes that file: `index_freshness` reads the legacy tree's own index, and `memory_md.py` regenerates only that tree, with no ranking or truncation | H | CC | measured 27.6 KB → lines lost; `jobs.py:522-577`; `memory_md.py` |
| G2 | Nothing splits, merges, archives or prunes note files; the only consolidation (episode dedup, commitment hygiene, wiki tombstones, the legacy nightly's stale-page pruning) never touches a host's memory dir; the topic CLI offers `purge` only | H | CC Cx | `capture.py:71-330`, `hygiene.py`, `mirror.py:478-617`, `cli.py:2807-2815` |
| G3 | **Data loss:** the note slug comes from the frontmatter `name` alone, so the same title under different project dirs collides on `ON CONFLICT (slug)` last-write-wins; verified: a 50,944-byte copy from one project silently overwrote two 51,652-byte copies of a different revision from another | H | CC | `notes.py:162, 214-235`, `mirror.py:182-243` |
| G4 | Chunking is fixed 6,000-char windows, not section-aware; a ledger edited by prepending shifts every offset and re-embeds ~all 69 chunks nightly | M | all | `embed.py:56-57, 111-125, 774-779` |
| G5 | Note `type` is captured (normalised to seed/active/shipped/superseded/abandoned/evergreen) but unused by ranking; `[[links]]` do become graph edges; no `superseded_by` exists; no note size warning | M | CC Cx | `notes.py:132-140, 163-170` |
| G6 | The query log records each search's top hit ids but nothing aggregates hits per note, so "what does recall actually use" is unanswerable today | M | all | `query_log.py` |

### 6. Honesty: doctor and Home

| # | Finding | Sev | Evidence |
|---|---|---|---|
| D1 | The nightly returns only the legacy script's exit code; notes reconcile, embed backfill, mark-stale and hygiene outcomes are logged and never read — the "green because evidence never arrived" class again | H | `jobs.py:242-251, 254-260` |
| D2 | Nightly evidence is written to a legacy log dir when an env var is set; the plist's own log is 0 bytes since 09-06; doctor reads neither | L | `jobs.py:89-135` |
| D3 | Home shows four tiles and five captures; `pending_turns` and `queue_depth` from `liveness()` never reach it; no "captured today", no "capture now" | H | `App.tsx:3178-3223` |
| D4 | `khipu_status` has no notes-freshness field; the model cannot tell notes are hours stale | M | `drift.py:314-374` |
| D5 | Multi-Mac nightly is host-scoped by convention only; two Macs would race on the shared hub and the memory repo | M | `git_sync_health.py`; no lock |
| D6 | Outbox/queue checks test `pending == 0`, never age | M | `outbox_ok` |

## What ships, in order

Each phase is one agent brief; oracles after every phase: `pytest` (packages/cli), `npm run build`,
`npm run check:setup`, `cargo check`, and the new contract tests below. The gateway is redeployed and the
desktop released when a phase touches them ("merged is not deployed").

- **Phase 1 — recall fires by itself (R1, R4, R5, R8, R11, R9).** A `UserPromptSubmit` hook for Claude
  Code and the same shape for Codex; query = prompt, gated by `search_tokens()`, semantic lane with a
  1.2 s hard timeout and fail-open; top 3 with kind/date/project labels inside an explicit
  untrusted-data fence; relative score floor tuned with `recall_eval.py`. Search returns `confidence`
  and matched-token counts; the resolved project is a ranking boost; a `pg_trgm` index takes the
  literal pass under 300 ms. Rule text replaced. Cursor keeps the pull rule (no per-prompt event);
  Aegis and the gateway get the same hits from the first tool call of a session (`khipu_status`
  returns "prior work on this topic" when given the prompt).
- **Phase 2 — capture cannot lose (K1, K2, K3, K4, K5, K7).** `khipu capture now` and an MCP
  `khipu_capture` that *enqueues* on hook-owned installs instead of refusing; a high-value-turn
  trigger (assistant turn over N chars, a correction or decision phrase, "remember this") that
  overrides the 5/20 cadence; a regex-extracted `verbatim` tier (errors, commands, paths, up to three
  literal user quotes) stored uncapped and indexed; oversized windows chunked and summarised with
  `truncated_chars` stamped on the episode; `SubagentStop` installed with `parent_session_id`; merges
  append summaries; missing transcripts are red.
- **Phase 3 — commitments and decisions (O1, O2, O3, O4, K6).** Future-trigger grammar covers
  `until/before/once … closes/is done/ready`; deferrals open as followups; a `decisions` table
  (text, project, decided_at, rationale, episode_id, superseded_by) populated at extract; a
  `deliverables` table (kind, path, url, episode_id) with "you produced X on <date>" surfaced on a
  matching new task; `due_after` required or explicitly none on user-owned items and the slice sorted
  by age × kind; `project` backfilled from transcript cwd, `scope` normalised, session ids never used
  as projects, a `project_aliases` table.
- **Phase 4 — freshness within a minute (F1–F6, R7).** mtime-gated `notes.reconcile` from the Stop
  hook plus a debounced launchd `WatchPaths` agent on the memory dirs; topic embedding in the bounded
  catch-up; `event_at` separate from `indexed_at` with file mtime as the fallback, never `now()`;
  per-chunk isolation of embedding failures; tombstones; Cursor and Aegis memory dirs added when they
  exist.
- **Phase 5 — organisation (G1–G6, R6).** First the slug-collision fix (slug namespaced by project
  when titles collide; duplicate `(name, digest)` copies across dirs detected and reported, never
  overwritten). Then Khipu keeps each host index under the host's cap by ranking lines (type,
  note mtime, recall hit count aggregated from the query log) and serves the overflow through recall;
  a note size policy (doctor warns over 50 KB; ledgers split by `## ` section into dated child notes
  with the index line kept); section-aware chunking so an edited ledger re-embeds only the changed
  sections; `status` as an enum surfaced and de-ranked in search and the slice; `type` and `[[links]]`
  used by ranking; stale-note archiving report-only, ported from the legacy heuristic.
- **Phase 6 — honesty (D1–D6, K8, K9, K10, R10).** Doctor checks: `notes_reconcile_ok`,
  `embed_provider_ok`, `topics_embed_lag`, `degraded_rate_ok`, `oldest_pending_seconds`, gateway
  liveness per token, unknown-harness heartbeat, nightly step evidence persisted, nightly overlap lock.
  Home gets a "Right now" panel (pending turns per live session, queue depth, last capture age,
  captured today) and a Capture now button. `khipu_status` reports note freshness. The Aegis queue
  gets a timer drain.

## The gaps become oracles

- **Contract tests (`tests/test_memory_contract.py`).** For each numbered finding, one test that fails
  if it returns: the per-prompt hook injects on a topical prompt and not on "ok"; a "deferred until X"
  sentence opens an owed followup and survives session end; a 30 KB window is captured in full with
  `truncated_chars == 0`; a verbatim error string round-trips search; a note edited on disk is
  searchable after one Stop; `search` returns `confidence: none` on gibberish; superseded topics carry
  their status in every hit; every drop path listed above turns a named doctor check red.
- **Live end-to-end (`tests/test_memory_live.py`, hub).** Capture → search → slice → owed on a scratch
  project against the real hub, tagged so it can be forgotten afterwards.
- **Desktop walkthrough** (render harness): Home shows pending turns and captured-today; Capture now
  works; the health rows name every new check in plain words.

Acceptance: the incident replayed end to end — approve a scope on day 0, name its topic on day 4 —
brings the scope into context before the model acts, in Claude Code and Codex by hook, in Cursor by
rule, in Aegis and the gateway by first tool call; and the diagnosis turn is captured within a minute
with its seven points intact.
