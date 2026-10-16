# Offline and self-healing (scope, 2026-10-16)

Status: scope for review. No code changed. Owner requirements (Matt, 2026-10-16):

1. A network problem must never leave Khipu degraded in a way that does not resolve by itself.
2. Khipu should work when the network is down.

Principles this design keeps: no loss of memory quality to save cost; every limit liftable in the app; nothing that needs a terminal step from the owner; Khipu reports and never routes.

## The incident

At about 12:00Z the Mac moved to another network, and the hub (remote Postgres) became intermittently unreachable. Doctor went red on `prompt_recall_outcomes` (87%, then 100%, of the last 100 prompt-time recalls missed their deadline, median about 1.2 s) and on the outbox (5 captures pending). The replica (`hub_snapshot.sqlite`) had last been dumped at 06:33Z. Doctor said it was not fresh and that prompt-time recall was running against the hub. A manual `khipu snapshot refresh` at 13:30Z took 70 s and fixed freshness. Doctor stayed red because the outcome check judges the last 100 attempts. The outbox drained by itself once the hub answered.

Evidence read for this doc: the code at main `1bc7501`, the installed launchd plists, `nightly-last.json`, the replica's meta and freshness files, and the queue-drain and recall-daemon logs. The per-prompt recall log lives in the capture home, which this investigation did not read, so the timing of individual prompt-time calls is inferred from code, not observed.

## Findings

### 1. What refreshes the replica, and why it went 7 hours

- **Only the nightly makes a full dump.** `jobs.run_nightly` calls `_refresh_snapshot` as its ninth step (`packages/cli/khipu/jobs.py:333`, `:355`). The nightly is `StartCalendarInterval` 02:05 local (installed nightly plist), which is 06:05Z; on 2026-10-16 the step ran at 06:33Z (`nightly-last.json`: `snapshot_refresh ok, refreshed_at 06:33:19Z`). Its docstring says so plainly: "Since the doctor stopped refreshing as a side-effect (CLI-6, 2026-10-10) nothing else does" (`jobs.py:358-362`). A failed dump is "tried again tomorrow".
- `hub_snapshot.maybe_refresh` (`hub_snapshot.py:2354`) is dead code: nothing calls it. The module docstring still says doctor calls it (`hub_snapshot.py:16-20`), which is out of date.
- Between dumps the replica is patched in place, but only for this Mac's own captures and only when the embed step succeeds: `embed_on_capture` upserts the episode after a successful embed (`embed.py:2066-2093`). If the hub write or the embed fails, nothing reaches the replica (queue-drain log, 12:42Z: `embed-on-capture skipped: ConnectionTimeout`). An upsert is also skipped while a dump holds the lock (log: `snapshot upsert skipped: hub snapshot refresh in progress`), and it is not retried. Episodes written by other machines, the gateway or cloud agents never reach the replica until the next nightly. Decisions get a separate catch-up that only runs after a drain that captured something (`session_capture.py:2695-2711`).
- **Why it read "not fresh" at 7 hours.** The age limit is 24 hours (`SNAPSHOT_MAX_AGE_S`, `hub_snapshot.py:4310`), so age alone did not do it. `snapshot_is_fresh` (`hub_snapshot.py:4327-4358`) also distrusts the replica for an hour (`LAST_KNOWN_TRUST_S`, `:2451`) whenever the last measurement of this dump found any table more than 30 minutes behind the hub (`SNAPSHOT_LAG_BEHIND_S`, `:2443`). That measurement runs on every `khipu status`, MCP `khipu_status` and doctor call (`drift.py:430-436`, then `snapshot_freshness` writes `hub_snapshot.sqlite.freshness.json`, `hub_snapshot.py:3228-3241`). With a 7 hour old dump and episodes the replica never received, any status read finds it "behind" and switches prompt-time recall to the hub for the next hour. By the code this is the only path to "not fresh" at that age; the pre-refresh freshness file was overwritten at 13:33Z, so the exact lag value is not recoverable.
- **Root cause of the gap:** the design depends on one dump a day plus best-effort patches, and nothing reacts when the replica is found behind. The "behind" verdict turns prompt recall to the hub (the slow path) and no code path repairs it before the next nightly.

### 2. Prompt-time recall

- Whole-hook wall clock `TIMEOUT_S = 1.2` s (`recall_prompt.py:64`); the local lane waits for the embedding leg until `LOCAL_LANE_DEADLINE_S = 0.95` s (`:73`); query embedding on its own `QUERY_EMBED_LOCAL_TIMEOUT_S = 0.7` s, no retries (`:1854`, `:1979-1982`). `KHIPU_RECALL_DEADLINE` can shorten the overall deadline (`:3118`). None of these is an owner setting in the app.
- Replica first: `_snapshot_search_hits` runs keyword search locally and the embedding plus cosine leg beside it; if the embedding is late it answers keyword-only (`:2028-2079`). Offline this lane still works, keyword-only, unless the query vector is cached (`:1961-1984`).
- When the replica is not trusted, `_search_hits` falls back to `embed.hybrid_search(mode="semantic")` against the hub (`:2337-2353`): a hub connect with a 5 s timeout (`db.py:15`, `:100-108`) and a query embedding with 10 s and 2 retries (`embed.py:381-382`). Neither fits in 1.2 s, so offline or on a slow network every prompt ends as `timeout>1.2s` with no hits (`:2878-2887`). That matches the median of about 1.2 s.
- So offline recall does work, but only while the replica is trusted. The fallback to the hub is the wrong direction when the hub is the thing that is down: a distrusted but present replica would still have answered, a few hours staler, inside the deadline.

### 3. Everything else with the network down

| Part | Offline behaviour | Recovers by itself |
|---|---|---|
| Capture (Stop hook) | Detached process (`hook_background.py:75-78`); a failed hub write goes to the outbox (`outbox.enqueue`). No data loss. | Yes. Queue-drain every 300 s (`StartInterval 300`, installed queue-drain plist) replays; an unreachable hub stops the pass and is never counted toward burial (`outbox.py:467`, `:486`, `:496-499`). |
| Outbox doctor check | Red while anything is pending (`cli.py:240`). | Yes, once drained. |
| Replica patch for replayed captures | Upserted only if the replayed embed succeeds (`outbox.py:502-506`). | No, until the nightly. |
| MCP `khipu_search`, `khipu_get`, `khipu_graph`, `khipu_status` | Fall back to the replica on a connection failure (`mcp_server.py:986-1000`, `:1029-1099`, `:1128`, `:1273-1275`). Search embeds first with 10 s and 2 retries, so an offline search can wait tens of seconds before it falls back (inferred from constants, not measured). Library search refuses offline (`:989-992`). | Yes. |
| MCP `khipu_owed`, `khipu_decisions` | Hub only, no fallback (`mcp_server.py:1517-1535`, `:1620-1648`): error after the 5 s connect timeout. | Yes, but useless offline. |
| `khipu_owed_update`, `khipu_decisions_update`, `khipu_forget` | Hub writes; fail offline. | Caller must retry. |
| Routing (`khipu_route`, the push) | Local record (`routing.py:74`); works offline. Plan figures say "can't be read just now" when vendors cannot be reached. | Yes. |
| SessionStart slice | Falls back to replica episodes with a visible "stale: hub unreachable" line after the connect timeout (`recall_rule.py:322-367`, `:379`). | Yes. |
| Desktop app | Each tab runs the CLI with no timeout (`apps/desktop/src-tauri/src/lib.rs:357-392`, `.output()`); Owed reloads every 60 s and on focus (`apps/desktop/src/App.tsx:2306-2325`), so offline it spins for at least the connect timeout per call and shows errors. | Yes, on the next poll. |
| Nightly | One run a day; a step that cannot reach the hub fails and is "tried again tomorrow" (`jobs.py:358-362`). | No: a nightly that ran offline leaves the replica a day older, and after 24 h prompt recall goes to the hub all day. |
| Hub statements | No statement timeout or TCP keepalive on the normal connection (`db.py:100-108`). A network change in the middle of a query can wait on the operating system's TCP timeout (inferred; not reproduced). | Eventually. |

### 4. Doctor checks that stay red after the cause is gone

- `prompt_recall_outcomes`: last 100 searched calls within 7 days (`recall_prompt.py:3144-3151`), red at 50% failures (`:3254`). Count-bounded: after a bad hour it stays red until about 51 good prompts arrive. An idle Mac stays red for up to 7 days. Its advice says "run `khipu snapshot refresh`" (`:3268-3271`), a terminal step.
- `degraded_rate`: last 50 MCP searches, red above 20% degraded (`query_log.py:121-132`). Offline fallbacks are degraded, so this is count-bounded too.
- `nightly_steps`: a failed step stays red until the next nightly records it (`jobs.py:729-743`), so up to 24 h.
- `prompt_recall_snapshot`: never red on its own (`hub_snapshot.py:4361-4381`) but its advice is a terminal command.
- Promotion gate: `gate_doctor` runs `doctor --quick` and refuses on any red `*_ok` except `jobs_ok` and `synth_provider_ok` (`promote.py:551-584`), and `doctor --quick` gates on every `LOCAL_GATES` key including `prompt_recall_outcomes_ok`, `outbox_ok` and `degraded_rate_ok` (`cli.py:209-214`, `:490`). The recall and capture steps need the hub too. So a past network outage blocks a deploy for as long as the sticky windows stay red, and there is no owner override (no force or lift in `promote.py`). On 2026-10-16 a session had to poll health every 5 minutes before it could promote.

### 5. Other things that do not recover by themselves

- The replica, as above: nothing repairs "behind" before 06:05Z the next day.
- Replica patches skipped because a dump held the lock are lost, not queued.
- The hour-long distrust is renewed by every status read (the desktop app and agents call status often), so the hub path persists for as long as the replica is behind.
- An empty `.hub_snapshot.refresh.lock` file stays on disk; it is a `flock`, so it is harmless (`hub_snapshot.py:1793-1812`).
- A promotion refused by the gate is not retried; someone has to run it again.

## Design

One idea: **the replica is the read path, and the replica heals itself.** The hub is where it learns, never what a prompt waits on.

### A. Keep the replica current without waiting for the nightly

- A replica-refresh job (launchd, beside queue-drain) that runs a full dump when the replica is behind, overdue, or after the hub becomes reachable again following a failure, and otherwise does nothing. Throttled by the existing `AUTO_REFRESH_MIN_AGE_S` (15 min) and the refresh lock. It never runs from a hook, a status read or a tab click.
- An incremental pull, not just a full dump: episodes and vectors written since the replica's cursors (the cursors already exist, `hub_snapshot.py:623-725`), so a 70 s dump is the fallback, not the routine. This brings in other machines' and the gateway's episodes.
- Replica patches that miss (dump in progress, embed failed) are recorded and retried by the same job, so they are not lost.
- No quality loss: every table the nightly dumps is still dumped, just more often. The cost is hub reads, not model calls.

### B. Prompt-time recall never waits on an unreachable hub

- When the replica exists but is not trusted, answer from it anyway and mark the answer stale (the same marker MCP search already uses), and wake the refresh job, instead of falling back to the hub. The hub fallback stays only for a Mac with no replica at all.
- A short reachability memo (a file holding "hub unreachable since T", written by any failed connect and cleared by any success) lets the hook and MCP tools skip a 5 s connect they know will fail. It is advisory and expires within a minute, so it can never stick.

### C. Offline reads for the rest

- `khipu_owed` and `khipu_decisions` read the replica on a connection failure (the replica already holds decisions; commitments would be added), marked stale.
- MCP search checks the reachability memo before the 10 s embedding call, so an offline search goes to the replica at once.
- Owner writes made offline (owed close, decision supersede) go to a local queue like the outbox and replay when the hub answers. Forget stays hub-first, because a forget that only looked done would be a privacy failure; offline it says plainly that it is waiting.
- Hub connections get TCP keepalives and a statement timeout per caller, so a network change cannot leave a query hanging.
- Desktop CLI calls get a timeout and an offline state in the app ("Hub not reachable, showing this Mac's copy from HH:MM"), not a spinner.

### D. Doctor checks that heal when the cause heals

- Every rate check judges a time window as well as a count: only calls from the last hour (configurable in the app), so a past outage ages out on an idle Mac. `prompt_recall_outcomes` and `degraded_rate` both change.
- Calls made while the hub was known unreachable are reported as "offline" in their own line, not as failures of the recall lane, because the lane did its job if it answered from the replica.
- Every "fix" a check offers names a button in the app, not a command, and that button exists.
- The nightly step check turns green when the refresh job later succeeds at the same work.

### E. The promotion gate after an outage

- The gate separates "this candidate is broken" from "this Mac was offline": a red check whose evidence is all from an offline period does not refuse a candidate. The recall and capture steps that need the hub wait for reachability (bounded, reported) rather than refusing.
- When the gate refuses only because the hub is unreachable, the promotion is queued and retried by itself when the hub answers, and the app shows that it is waiting.
- A one-time owner override for the gate in the app ("Update anyway"), logged in `promotions.jsonl` and cleared after one use (decision 3).

## Slices (one session each)

Each slice: read the named code first, keep the hermetic pytest oracle green (from `packages/cli`, temp HOME, `KHIPU_KEYCHAIN=0`, max 3 runs), ruff count unchanged, and the slice's own check below. Desktop slices add `npx vitest run` and `npm run build`, and a rendered check of the app.

1. **Self-healing doctor windows (D).** Time-bound `prompt_recall_outcomes` and `degraded_rate`; an "offline" category; app-button fixes. Oracle: tests that write a log of 100 old timeouts plus nothing new and see green after the window; 100 recent timeouts stay red; offline-tagged calls do not count as failures.
2. **Replica refresh job (A, first half).** A launchd job that dumps when the replica is behind, overdue or the hub came back, throttled and locked; installed by `khipu jobs install` and shown in the app's Jobs list. Oracle: tests with a fake hub that is down then up, checking the dump runs once after recovery and not while fresh; a rendered plist test.
3. **Incremental pull and retried patches (A, second half).** Cursor-based pull of episodes, vectors and decisions; missed patches queued and retried. Oracle: tests against a temp Postgres or the existing fake, checking a foreign episode lands in the replica without a full dump, and that the measurement then reads in step.
4. **Recall answers from the replica, never the hub, when the hub is down (B).** Stale-but-present replica answers with a marker; reachability memo with expiry. Oracle: hook test with the hub unreachable and a 7 hour old distrusted replica returns hits inside 1.2 s; memo test shows it expires and clears on success.
5. **Offline owed and decisions, queued owner writes, keepalives (C, CLI side).** Oracle: MCP tests with the hub down return replica rows marked stale; a queued close replays once the fake hub answers; forget offline reports waiting and writes nothing locally that could look done.
6. **Desktop offline state and CLI timeouts (C, app side).** Oracle: vitest for the offline banner, Rust test for the CLI timeout, and a rendered check of the app with the hub unreachable (accessibility tree first).
7. **Promotion gate after an outage (E).** Offline-only evidence does not refuse; a hub-unreachable refusal queues a retry; the app shows waiting; the override only if Matt says yes. Oracle: promote tests with a fake doctor output carrying offline-tagged reds and a fake hub that comes back.

Slices 1 and 4 together fix the incident's visible symptoms; slice 2 removes the root cause. Order: 1, 2, 4, 3, 5, 6, 7.

## Decisions (Matt, 2026-10-16)

1. Prompt-time recall answers from the replica even when it is hours behind, marked stale with its age, rather than give no prior work.
2. Keep the replica within 15 minutes of the hub when the hub is reachable (the existing throttle), changeable in Settings.
3. The promotion gate waits and retries by itself after an outage, and Settings also has an owner override for red checks the owner knows are harmless. The override is one-time (owner's reviewer, approved by Matt): the owner chooses "Update anyway" for the red checks that held the last update back; the next promotion skips them once, records that in `promotions.jsonl`, and the override then clears itself (it lapses after 24 hours if no promotion runs; a promotion queued for the hub keeps it for its retry). Only the owner sets or cancels it: the app, or a typed yes on a terminal. There is no standing list.
4. Owner writes made offline (closing an owed item, superseding a decision) are queued on the Mac, shown as waiting to sync, and replayed when the hub is back, the same way captures already work.
5. Doctor's rate checks judge the last hour, changeable in Settings.
