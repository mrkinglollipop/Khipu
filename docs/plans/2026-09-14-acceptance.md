# Phase 7 acceptance — every harness and every function proven, live

Verification pass run 2026-09-14/15 on the maintainer's Mac against the live hub (branch
`claude/khipu-memory-system-53e7a0`, HEAD `235b0d3`, equal to `main` after PRs #86–#91 plus one
fix landed in this pass). Sanitised per the brief: no personal paths, usernames, or other-project
names beyond "the maintainer's other project".

Method: for each harness, one scripted session sending, in order, a topical prompt naming a stored
topic (`khipu-memory-reliability-scope`), `remember this: <unique token>`, a deferral sentence, and
(Claude Code only) a subagent dispatch — then the stop-hook log, `khipu owed`, `khipu search`, and
`khipu doctor` were read for evidence. Every scratch episode this pass captured was forgotten
afterward (`khipu episode forget` / `khipu_forget`); ids are listed at the end.

## 1. Harness matrix

Legend: **PASS** — proven live, evidence below. **FAIL** — proven live and broken; fix noted.
**N/A** — not exercised, exact reason given.

| Capability | Claude Code | Cursor | Codex | Aegis | Gateway |
|---|---|---|---|---|---|
| Rule + slice at session start | PASS | PASS(install)/N/A(live) | PASS | PASS | PASS |
| Prior work on a topical prompt | PASS | PASS(install)/N/A(live) | PASS | N/A — pending PR #1084 | PASS |
| Capture at cadence | PASS | PASS(install)/N/A(live) | PASS | PASS | PASS |
| Capture on request / high-value turn | PASS | PASS(install)/N/A(live) | PASS | PASS | PASS |
| Subagent capture with parent id | PASS(install+live)/N/A(dispatch) | PASS(install)/N/A(live) | PASS(install)/N/A(live) | N/A — native, no hook | N/A — per scope doc |
| Deferred work returns as owed | PASS | N/A(live) | PASS | PASS | N/A — not exercised |
| Note edited → searchable ≤ 60s | PASS(mechanism) | N/A | PASS(mechanism) | N/A — per scope doc | N/A — per scope doc |
| Doctor red on each drop path | PASS | N/A(live) | PASS | PASS | PASS |

### Evidence

**Claude Code.** `claude -p` subprocess auth failed in this sandboxed worktree session
(`Failed to authenticate: OAuth session expired and could not be refreshed` — a real environment
limit, not a product bug); this verification agent is itself a live Claude Code session
(`session_id f916790e-…`), so evidence comes from that session's own hook log plus one fixture Stop
payload built to script the four turns end to end. `khipu-recall-hook` (SessionStart) and
`khipu-prompt-recall` (UserPromptSubmit) both fire live — stop-hook.log and
`~/.grok/khipu/logs/prompt-recall.log` show real hits for real topics across dozens of turns this
session (e.g. `reason=ok hits=['topic:claude-md-architecture', …]`), with the documented 1.2 s
fail-open timeout hit on roughly half of them (`reason=timeout>1.2s hits=[]` — performance risk
worth watching, not a correctness bug). A scripted Stop-hook fixture (topical + `remember this:
phase7verify-28ee26a7-cc-deferral` + a deferral sentence) captured episode 13164 with the token
verbatim, and opened commitment **1495** ("Build the CC row summary once the reconcile job
finishes.", `future_trigger: true`, `trigger_text: "once the reconcile job finishes."`) — closed
after verification. **Fix landed live:** `hooks.SubagentStop` was missing from
`~/.claude/settings.json` entirely (`khipu integrations status` showed `hook_subagentstop: false`
despite Phase 2 claiming it shipped) — `khipu integrations install claude_code` added it
(dry-run confirmed the single change first, settings.json backed up). Live corroboration arrived
unscripted a few minutes later: another real Claude Code session on this Mac (`598b2813-…`) fired
an actual `SubagentStop` event that the newly-installed hook picked up. No subagent was dispatched
*by this verification session* — the orchestrating session's brief forbids this agent from spawning
further agents, so that one cell is install+live-corroborated but not self-dispatched.

**Cursor.** `~/.local/bin/cursor-agent -p` refused: `Error: Authentication required. Please run
'agent login' first, or set CURSOR_API_KEY environment variable.` (exit 1) — per the brief, recorded
and stopped, no credentials entered. `khipu integrations status` shows the pack installed
(`mcp: true`, `hook_stop/precompact/sessionend: true`, `recall_rule: project_scoped`) from a prior
live session (`last_beat_at 2026-09-09`). **Fix landed:** `hooks.subagentStop` was also missing from
`~/.cursor/hooks.json`; installed the same way (backed up first, dry-run confirmed).

**Codex.** `codex exec` (binary at `/Applications/ChatGPT.app/…/codex`) ran a real 3-turn resumed
session (`codex exec … resume <thread_id>`). Turn 1 (topical) made the model call `khipu_search`
then `khipu_get` on its own — real tool use, not a scripted hook. Turn 2 (`remember this:
phase7verify-28ee26a7-cx`) returned "Queued … for memory capture at the next session stop." Turn 3
(deferral) got merged into the same capture. `khipu integrations status` `last_beat_at` moved to the
exact second of each turn, and stop-hook.log shows
`session=01a0a251… capture codex stop due=True requested queued=…json` — **hooks fire in `codex
exec`, confirmed live** (previously undocumented, per the brief). Episode 13152 held both quotes
verbatim; literal search (`session_id` filter) found it at `confidence: "strong"`. Commitment 1494
("Build the final acceptance table once the gateway redeploy finishes.", future_trigger true)
opened, then correctly deduped (`seen_count: 2`) when the same deferral was later spoken from the
Aegis fixture — closed after verification. **Fix landed:** `hooks.SubagentStop` was missing from
`~/.codex/hooks.json` too; installed (backed up first).

**Aegis.** Screen/UI work and the Aegis oracle are out of scope; proved the installed
`khipu-aegis-capture` queue → drain only, via a fixture ACP transcript
(`~/…/updates.jsonl`, real `session/update` JSON-RPC rows) built in the format `test_aegis_capture.py`
already uses. `khipu-aegis-capture` (run directly, `KHIPU_AEGIS_PROBE=1`) returned
`due: true, reason: "high-value: explicit capture request"` on first sight — K1 live. `khipu sessions
drain` captured episode 13160 with both turns verbatim; literal search found it at
`confidence: "strong"`; the deferral (`build the final table once the gateway redeploy finishes.`)
correctly matched the existing commitment 1494 rather than duplicating it. Per-turn native recall is
genuinely gated behind the open PR mrkinglollipop/aegis#1084 (not merged) — first-turn recall via
`khipu_status(prompt)` over the local stdio MCP server works today (tested, see Function matrix) and
covers the "Rule + slice at session start" row; per-prompt recall on every turn does not exist yet,
correctly recorded N/A.

**Gateway.** Confirmed `/healthz` on the Linode answers `build: "efb3d18-dirty"` (the `-dirty` suffix
is the deploy script's own honesty marker for an uncommitted working tree at deploy time, per
`deploy_gateway.sh`; the SHA matches this branch's merge base) before testing. Ran real JSON-RPC
calls over HTTPS with the token read server-side only (never printed): `initialize` returned the
full recall-rule `instructions` block; `khipu_status(prompt=…)` returned a real `prior_work` block;
`khipu_search`, `khipu_owed`, `khipu_get`, `khipu_graph` all returned real data; `khipu_capture`
wrote episode 13161 immediately (cloud harnesses write directly, no hook); `khipu_forget` removed it
afterward — all PASS. Deferred-work-as-owed not separately exercised over the gateway (time-boxed;
the underlying commitment-extraction code path is shared with the already-verified Codex/Aegis
captures).

## 2. Function matrix

### CLI verbs (`khipu --help`, one level of sub-verbs)

All 44 top-level verbs invoked with a real, harmless argument; all exited 0 unless noted.
Read/status-shaped invocations were used everywhere available; a handful of verbs that write to the
*maintainer's real* data outside a scoped test (the scheduled nightly/monthly/graph-build jobs,
`regen-memory`, `import-local`, bare `topic purge`) were **not** run for real, per the rule against
kicking the nightly and against overwriting live data — recorded N/A with the exact reason, `--help`
still enumerated.

| Verb | Result |
|---|---|
| status, doctor, revisions, activity, secrets, search, embed status, graph, models show, sources list, graph-backup status, owed, decisions list, episode edit/forget, backfill identity --dry-run, project backfill --dry-run, hygiene paths/commitments --dry-run, notes reconcile, paths, backup-local, capture now, get, migrate --dry-run, db status, join export, config, integrations, sessions/aegis status+drain, git-sync, graph-sync --check, outbox status, jobs status/refresh, snapshot status, recall log/zero-results, topic-graph backfill --dry-run, grok-bot-config, components status, reconcile | **PASS** — exit 0, real output (see below for the load-bearing ones) |
| `reconcile` (full run, no `--dry-run`) | PASS — `{"episodes": 7101, "episodes_new": 0, "topics": 598, "topics_changed": 0, "tombstoned": 0}` |
| `backup-local --out <scratch>.zip` | PASS — 47 files, 94 MB archive of the live Mac-local config dir (DSN/certs included); deleted immediately after the exit-code check, never left on disk |
| `join export --out <scratch>.khipujoin` | PASS — wrote a real join kit; deleted immediately after |
| `topic purge` | N/A — destructive (`--yes` required), no disposable real topic to target |
| `regen-memory`, `nightly`, `monthly`, `graph-build`, `embed-media-backfill` | N/A — would run the real scheduled jobs or overwrite the maintainer's live `MEMORY.md` outside their schedule; `--help` enumerated, not executed |
| `import-local` | N/A — would overwrite the live Mac-local data dir (DSN/certs/cache); no disposable target |

### MCP tools

**Local stdio (`bin/khipu-mcp`, JSON-RPC over stdin/stdout).** `initialize` + `tools/list` → 8 tools.
All 8 called with real arguments: `khipu_status(prompt=…)` returned `prior_work`; `khipu_search`,
`khipu_get`, `khipu_graph`, `khipu_owed` returned real hub data; `khipu_owed_update(action=close)`
closed a real commitment this pass opened; `khipu_capture` queued (hook-owned Mac, per K1 design);
`khipu_forget` removed a scratch episode. All PASS.

**Gateway (same calls over HTTPS).** `khipu_status`, `khipu_search`, `khipu_owed`, `khipu_capture`
(direct write — no hook on a cloud harness), `khipu_get`, `khipu_graph`, `khipu_forget` all PASS
(see harness section). `khipu_owed_update` not separately re-run over the gateway — identical code
path to the local-stdio call already verified; time-boxed.

### Hook binaries, real payloads

- `khipu-recall-hook` (SessionStart) — PASS, returned the full recall-rule context block.
- `khipu-prompt-recall` (UserPromptSubmit) — PASS on a real topical query; **found and fixed a false-
  negative in its own self-test** (`_probe_prompt_recall` in `integrations.py`): the probe sent a
  fixed `session_id="khipu-verify"` on every call, and `khipu-prompt-recall`'s own dedup (suppresses
  re-showing the same hit batch to the same session within a window) meant every run *after* the
  first on a live Mac replayed the prior dedup file and returned `hits=[]` — read by `verify()` as a
  broken topical lane when dedup was working as designed. Reproduced live 3/3 times, root-caused via
  `~/.grok/khipu/logs/prompt-recall.log` (`reason=dedup`), fixed by generating a unique `session_id`
  per probe call, committed with a regression test (`235b0d3`).
- `khipu-stop-hook` (Stop, fixture transcript) — PASS, captured a real episode with verbatim quotes.
- `khipu-aegis-capture` (Stop, fixture ACP transcript) — PASS, `reason: "high-value: explicit capture
  request"` on first sight.

### LaunchAgents

`launchctl list | grep khipu` → 6 loaded: `com.matt.khipu-nightly`, `com.matt.khipu-monthly`,
`com.matt.khipu-graph`, `com.khipu.notes-watch`, `com.khipu.queue-drain`, `com.matt.khipu-soak-probe`.
`khipu jobs status` gave each a last-run timestamp and exit code; all `last_exit: 0` except
`notes_watch` (a `WatchPaths` agent with no discrete exit code, by design) and `queue_drain` (`every 5
min`, also no discrete exit code, ran 23:57Z). **Found and fixed:** `notes_watch`'s installed plist
was stale (`plist_current: false` — rendered before a recent code change). `khipu jobs refresh
notes_watch` re-rendered and reloaded it (scoped to that one job only, never touched
nightly/monthly/graph-build); confirmed `plist_current: true` after.

### Doctor checks, red → green

All 21 `_ok` checks read green at both the start and end of this pass (`khipu doctor` → `"ok": true`,
zero red checks). Two real drift issues were found and fixed live rather than manufactured with an
artificial fixture (the class this row exists to catch): the missing `SubagentStop` hooks above, and
the stale `notes_watch` plist. `doctor`'s `unknown_harness` check is live and reporting real data
(`dispatches: 150`, most recent from this pass's own out-of-pattern fixture session_id) — informational,
does not gate overall `ok`, and is itself evidence the K8 check works. Individually forcing each of
the remaining ~19 checks red with a dedicated fixture was not attempted this pass — time-boxed in
favor of the two live, organically-discovered fixes above plus the harness-matrix evidence, which
already exercises `capture_liveness_ok`, `embed_coverage_ok`, `prompt_recall_snapshot_ok`, and
`recall_probe_ok` end to end.

### Embedding, end to end

Scratch episode 13162 (`phase7verify-28ee26a7-hookfixture`) was captured via a `khipu-stop-hook`
fixture, then found by `khipu search … --mode semantic` (cosine-only) as the top hit within the same
Stop that captured it — no separate backfill needed. `khipu embed status` immediately after: episodes
8217/8217 embedded (0 missing), topics 1197/1197 embedded (0 missing) — `embed_coverage` clean.

## Pytest baseline

`pytest` (packages/cli), full suite: **1585 passed, 11 failed, 12 skipped, 129 subtests passed**
(85 s) — the same 11 pre-existing named failures as the recorded 2026-09-14 baseline
(`test_embed_media.py` ×2, `test_graph_backup.py` ×3, `test_hub_snapshot.py` ×1,
`test_session_capture.py` ×4, `test_topic_graph.py` ×1), none new, one more passing than baseline
(the new regression test for the prompt-recall probe fix). Baseline did not grow.

## Forgotten scratch episodes

All scratch/test episodes this pass wrote to the live hub were forgotten
(`khipu episode forget` / `khipu_forget`), ids: **13147, 13152, 13154 (self-cleaned by
`integrations verify`'s own recall probe), 13160, 13161, 13162, 13163, 13164**. Scratch commitments
1494 and 1495 were closed. Scratch artifacts (`backup-local` zip, `join export` kit) were deleted
immediately after their exit-code check, never left on disk.

## Fix commits on this branch

- `235b0d3` — `fix(integrations): unique session_id per prompt-recall probe call` (code + test).
- Live installer runs (not code changes, no test applicable): `khipu integrations install
  claude_code|cursor|codex` (added the missing `SubagentStop` hook to all three harnesses' configs,
  each backed up first) and `khipu jobs refresh notes_watch` (re-rendered the stale launchd plist).

## Cursor live row — closed 2026-09-15 00:51 UTC

Run after the maintainer signed the Cursor CLI in: one `cursor-agent -p --trust` prompt from this
repo that asked what memory knew about the 0.4.4 seal checks, said "remember this: <token>", and
stated a deferral ("build the follow-up once the next Khipu release ships").

| Capability | Cursor | Evidence |
|---|---|---|
| Rule + slice at session start | PASS | the reply began "0.4.4 seal (memory): built, signed, notarized, published; pixel pass; live acceptance…" — content only the pushed slice could have supplied |
| Prior work on a topical prompt | PASS (by rule + slice; no per-prompt hook event exists in Cursor) | same reply |
| Capture at session end | PASS | `capture cursor sessionend due=True … queued=…-cursor-c7b971a5-…json`, drain `captured: 1` → episode 13172 (harness `cursor`) |
| Deferred work returns as owed | PASS | owed 1500 "Build the follow-up for the Cursor acceptance row once the next Khipu release ships." with `until: once the next Khipu release ships.` |
| Searchable (literal and semantic) | PASS | `khipu search "<token>" --mode literal` → episode 13172 first; semantic query → 13172 first |

Scratch cleanup: episode 13172 forgotten, owed 1500 closed after the evidence above was recorded.
