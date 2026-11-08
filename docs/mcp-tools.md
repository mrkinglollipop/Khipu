# MCP tools

Khipu exposes 19 tools locally and 17 through the HTTPS gateway, which omits `khipu_owner_act` and `khipu_needs_you`, through one MCP server (server id `khipu`). Agents call them to read memory, write to it, manage commitments and decisions, and read the routing record, and take an owner action the owner asked for.

The tool list is returned verbatim by the server's `tools/list`. This page groups the tools by job. For the exact schema of any tool, ask the server.

## How the server is reached

- **Locally (stdio).** The launcher is `packages/cli/bin/khipu-mcp`. `khipu integrations install` writes it into each harness's MCP config. See [Integrations](integrations.md).
- **Over HTTPS (the gateway).** The gateway serves the tool list without `khipu_owner_act` and `khipu_needs_you`. A few tools answer differently there:
  - `khipu_usage`, `khipu_route_advise`, `khipu_route_report`, `khipu_route_rule`, `khipu_route_priority` and the `routing` field of `khipu_status` reply `{available: false, reason}`. `khipu_route` replies with the closed gateway projection when the owner has turned on the cloud routing copy (`routing_cloud_copy`); otherwise it too replies `{available: false, reason}`.
  - `khipu_forget` refuses an episode unless the calling token captured it itself: an episode with no known owner, one captured on a Mac, and one captured by another gateway token are all refused. (A hub without the origin column falls back to refusing session ids that start with a local harness name: `claude_code`, `cursor`, `codex`, `aegis` or `grok_build`.)
  - `khipu_capture` writes immediately (see below).
  - `khipu_owner_act` is not listed and refuses every call: owner direction needs the owner's typed turns, which only a prompt hook on the owner's Mac records.

## Memory read

### khipu_search

Search memory. Read-only.

| Parameter | Meaning |
| --- | --- |
| `query` | Required. The text to look for. |
| `limit` | Default 12, capped at 50. |
| `mode` | `hybrid` (default), `literal` or `semantic`. |
| `semantic` | Deprecated alias for `mode: semantic`. |
| `kind` | Hybrid and literal accept `episode`, `topic`, `node` or `library`. Semantic accepts `episode`, `topic`, `media` or `library`. |
| `source` | A library name. Implies `kind: library`. |
| `project` | Boost or filter to one project. |
| `since`, `until` | An ISO date or a relative span such as `7d` or `24h`. |
| `session_id` | Session id prefix. |
| `harness` | The part of the session id before the colon. |
| `t3_thread` | Exact T3 Code thread id. Episodes only. |
| `via` | Exact capture origin, for example `t3`. Episodes only. |
| `tz` | Time zone for natural-language time phrases, used when `time_interpretation` is on and no explicit `since` or `until` is supplied. Defaults to UTC; unknown zones also fall back to UTC. |

Hybrid mode fuses cosine similarity, token overlap and a literal match by reciprocal-rank fusion. With no active embedding profile it falls back to literal plus token overlap and sets `degraded: "no-embedding"`. Graph nodes are left out unless `kind` is `node` or the query looks like an id. Libraries are included in a default search.

### khipu_get

Load a search hit by id. Read-only.

| Parameter | Meaning |
| --- | --- |
| `id` | Required. An episode id (digits), a topic slug, a media id, or a library id. |
| `kind` | Optional hint for the id type. |
| `offset` | Nonnegative integer byte offset into a topic body. Default 0. |
| `max_bytes` | Page size for a topic body. Default 65536, clamped to 4–524288 bytes. |

- An episode returns its full summary, decisions, preferences and topics.
- A topic body comes back in pages of UTF-8 bytes. Each page carries `body_total_bytes`, `body_offset`, `body_next_offset` and `complete`. Keep calling with `body_next_offset` until `complete` is true.
- Media returns path, sha256 and mime type.
- `library:<name>:<doc>#<chunk>` returns that chunk with its neighbours. `library:<name>:<doc>` returns the document.

### khipu_graph

Walk the neighbourhood of a graph node. Read-only.

| Parameter | Meaning |
| --- | --- |
| `id` | Required. A node id. Topic slugs work. |
| `hops` | 1 to 4. Default 1. |
| `limit` | Default 25, capped at 200. |

A digit id is an episode. The walk then covers that episode's capture topics.

### khipu_status

Hub status: table counts, latest episode time, mirror lag and recent captures. Read-only.

| Parameter | Meaning |
| --- | --- |
| `include_drift` | Add the file-versus-database drift sample. Slower. |
| `prompt` | The first user prompt of a session. Returns a light payload (below). |
| `cwd`, `project` | Boost hits from one repo or project when `prompt` is given. |
| `budget_ms` | Time budget for the prior-work lookup. Default 600, clamped to 100–5000 ms. |
| `full` | With `prompt`, return the complete payload instead of the light one. |

With `prompt` and no `full`, the answer always has `hub_ok`. `prior_work`, `prior_work_text`, `prior_work_meta`, `notes_freshness`, `routing`, `contract` and `hub_error` are optional: each is left out when it is unavailable or its lookup fails. `prior_work` holds up to three items and `prior_work_text` is a short Markdown block. Without `prompt` (or with `full`), the answer is the complete payload: counts, recent captures, replica freshness (`hub_snapshot`, `snapshot_age_seconds`, `snapshot_state`), `pending_turns`, the capability `contract`, and `warnings` for plans nearly used up (read from the usage cache only). If the hub cannot be reached, the answer falls back to the local replica and carries `hub_error`.

The prompt path is the pull path for harnesses that get no pushed recall, such as Aegis and the gateway.

## Memory write

### khipu_capture

Remember a session.

| Parameter | Meaning |
| --- | --- |
| `summary` | Required. One to three sentences. |
| `topics` | Short lowercase topic slugs. |
| `people`, `decisions`, `preferences` | Lists of strings. |
| `scope` | Optional scope label. |
| `session_id` | `<harness>:<stable id>`. |
| `project` | Project name. |

Behaviour depends on where it runs:

- **On a Mac where a harness runs a capture hook** (`khipu-stop-hook` or `khipu-aegis-capture`), it does not write, whatever the capture mode. It flags the session and returns `{queued: true, captured_by: "next stop"}`. Your summary is kept as the episode's note.
- **Through the gateway**, there is no hook, so it writes at once. It is the only way a cloud session is remembered.
- **Locally with no capture hook**, it writes only when `capture_mode` is `hub`. In any other mode the call is rejected, because nothing would act on it.

An immediate capture returns `ok`, `mode` and `ts`, plus `episode_id` when the writer identifies the row it actually landed on. This may be an existing episode that the capture merged into. A hook-owned call instead returns `queued`, `captured_by`, `harness`, `session_id` and `resolved_by`.

An immediate capture also says what became of the loops it carried. `ok: true` means the episode is stored; it does not mean every loop took effect. `dropped` lists each loop that did not, as `{kind, text, reason}` (`kind` is `open_loop` or `closed_loop`): an open loop the precision filter rejected (too short, no action, status chatter), or a closed loop that matched no open commitment, matched several equally, or matched one its rules keep open. `partial: true` is present when `dropped` is not empty. `commitments` gives the counts (`opened`, `restated` for words already open, `closed`), or `step_failed` with the error when the commitment step was rolled back, in which case every loop sent is in `dropped`. `fact_gaps` (`{step, error}`) names any other fact step (decisions, close rules, deliverables) that failed and was rolled back. An exact replay of a capture already recorded has `commitments: {not_processed}` and no `dropped` (the first write handled its loops). A capture matched to an existing episode by resemblance stores no loops: they come back in `dropped` with the reason `capture matched episode N; loops not stored`. A `done:` decision that closes nothing is reported with `kind: "decision"`. Error text in `step_failed` and `fact_gaps` is the exception class and one short line; a database error shows only its class and SQLSTATE.

A hook-owned call stores the summary as the note only. Any `open_loops`, `closed_loops`, `decisions`, `decision_details`, `preferences`, `topics`, `people`, `deliverables` or `verbatim` sent with it are not kept and come back in `dropped` as `{field, count, reason}`. The note is capped at 14,000 characters (the newest are kept, across repeated requests); when text was cut the answer has `truncated: true`, `stored_chars`, `sent_chars` and `dropped_chars`. `khipu capture now` prints the same four fields when it cuts.

### khipu_forget

Tombstone one episode. Parameter: `id` (required). The row is not erased: its summary and provenance stay in the database. Search, `khipu_get` and recent-capture readers (Activity, doctor, status, the full `khipu_status` and the session-start fallback) exclude it. Its vectors and legacy file lines are removed, the commitments it opened are closed (the closed rows stay) and its decisions are retracted. Graph topic nodes and earlier database backups are not touched. Forget removes queued jobs that exactly match the episode’s sources under the outbox lock. Unreadable, loosely matching or shared jobs remain and are reported with their reasons and locations. Incomplete legacy-file, replica or queue cleanup returns `complete: false` with the remaining stores in `partial`. Use it for a wrong capture or a test. Over the gateway a token can forget only episodes it captured itself; episodes with no known owner, captured on a Mac or by another token are refused.

`khipu_forget` also accepts `ledger:<project>#<row id>` to remove a ledger row, with optional `note` and `children` (`remove` or `move-up`). The owner can restore the removed row during the configured retention period.

## Commitments and decisions

### khipu_owed

List commitments (followups, blockers, questions, promises). Read-only.

| Parameter | Meaning |
| --- | --- |
| `project` | Filter to one project. |
| `status` | `open` (default), `closed` or `stale`. |
| `owner` | `user`, `assistant`, `agent` or `all` (default). |
| `kind` | A commitment kind, or `routing_direction`. |
| `origin` | `conversation` or `job`. |
| `query`, `pr`, `looks_done` | Filter by words, pull request or a Looks done suggestion. |
| `cursor` | The `next_cursor` from an earlier page. |
| `limit` | Default 50, capped by `owed_page_max` (200 by default, 0 for no cap). |

Follow `next_cursor` for subsequent pages; `total` and `counts` cover all matching items. Call it at session start in a harness that gets no pushed project slice.

### khipu_owed_update

Close, reopen, snooze or reassign commitments.

| Parameter | Meaning |
| --- | --- |
| `id`, `ids` | Supply a commitment id or a list of ids from `khipu_owed`. |
| `action` | Required. `close`, `reopen`, `snooze` or `reassign`. |
| `until` | For snooze: a date, or `7d`, `2w`, `1m`. |
| `owner`, `owner_label` | Reassignment requires `owner` (`user`, `assistant` or `agent`), with an optional label. |
| `reason` | Required when changing multiple ids. |

Multiple ids are changed in one transaction with a result per id, bounded by `owed_bulk_max` (500 by default, 0 for no cap). The answer's `ok` is false, with `changed: 0`, `failed` and an `error`, when no id changed (every id missing, or a reopen of items already open); the per-id `results` still say why, and the tool call is an error. When some ids changed and others did not, `ok` stays true so the ids that landed are kept, with `partial: true` and `failed` listing the ids that did not; check `results` per id. `khipu owed --close/--reopen/--reassign ID,ID` prints the same answer and exits 1 unless `ok`. Bulk changes including user-owned items require owner confirmation or owner-directed `owed.change`.

### khipu_decisions

List decisions with their validity. Read-only.

| Parameter | Meaning |
| --- | --- |
| `project` | Filter to one project. |
| `status` | `standing` (default), `superseded`, `retracted`, `all` or `conflicts`. |
| `since` | ISO date or relative, such as `7d`. |
| `limit` | Default 20, capped at 100. |

`conflicts` returns unresolved candidate links, with the text of both sides, that nobody has confirmed or rejected yet.

When the hub is unavailable, owed and decision reads use this Mac’s replica and mark stale results and pending updates (`pending_sync`). Decision conflicts still require the hub. Local owed and decision updates queue for replay with hub outcome receipts. Gateway updates and episode forget do not queue.

### khipu_decisions_update

Change one decision.

| Parameter | Meaning |
| --- | --- |
| `action` | Required. `supersede`, `restore`, `retract`, `confirm` or `reject`. |
| `id` | The decision, for supersede, restore and retract. |
| `link_id` | The candidate link, for confirm and reject. |
| `by` | For supersede: an existing decision id that replaces this one. |
| `new_text` | For supersede: the replacement wording. Creates a new decision. |
| `source_kind` | With `new_text`: `user`, `assistant` or `tool`. Default `assistant`. |
| `reason` | For supersede and retract, in the user's own words. |
| `project`, `session_id` | For the new row created by `new_text`. |

Supersede requires `id` and exactly one of `by` or `new_text`. Restore and retract require `id`; confirm and reject require `link_id`. Supersede only when the user reverses or replaces a decision, not for a refinement. Retract marks a decision wrong outright.

## Routing

These tools report account state and provide advisory account rankings. They never launch or enforce work: Khipu reports, and routing stays with the caller. Usage and routing write/advice tools are unavailable through the gateway, but `khipu_route` serves the owner-enabled published cloud copy, with its hub timestamp, stale status and persistent pull receipts. See [Routing](routing.md) for the full model.

### khipu_usage

Usage for Claude, Codex, Grok, Muse and GLM, provider-defined HTTP quota adapters, and one DeepSeek account. DeepSeek reports measured spend and coverage rather than subscription headroom. Read-only.

| Parameter | Meaning |
| --- | --- |
| `fresh` | Start a refresh now even if the cache is young. |
| `max_age` | Cache age limit in seconds. Default 120. |

It answers from the cache at once and refreshes in the background. A background refresh may use a tiny Haiku turn when Claude’s usage reply omits figures, or a tiny Muse turn to refresh stale observations, each at most once per 30 minutes per login. Ask again a few seconds later for new figures. Errors are short codes such as `timeout` or `not_signed_in`. A window a CLI does not report is absent, not zero.

### khipu_route

The routing record: which accounts are open, limited (with the end time and whether it is the CLI's own or a guess), paused, down or unknown; the owner's standing rules and note; holds; and job-runner lanes not matched to an account. Read-only.

| Parameter | Meaning |
| --- | --- |
| `since` | A version from an earlier answer. Returns only what changed after it. Not available through the gateway. |
| `account` | Narrow to one account, by label or id. |
| `session_id` | This conversation's own session id, for pull delivery. Through the gateway, clients sharing one token use ids unique among them, of the form `<client>:<stable id>`. |
| `ack` | A routing batch issued to this caller, read and now acknowledged. Through the gateway an ack for another identity's batch is refused (`ack_not_yours`). |

Local pulls return `routing_changes` and `delivery`. A caller without environment identity supplies its own stable `session_id`; a descendant uses the exact canonical root plus `/child`. Foreign local session ids or acknowledgements are refused. Without identity, the read is untracked and marks nothing delivered. Read the returned batch and pass its id as `ack` on the next call; `since` does not acknowledge delivery.

### khipu_route_advise

Return advisory `go`, `hold_back`, `not_eligible` or `closed` results, ranked in the owner’s priority order, with reasons, figures and evidence. This tool never launches or enforces work and is unavailable through the gateway. Optional fields are `account`, `lane`, `tier` (`opus`, `sonnet` or `haiku`), `kind` (`build`, `fix` or `check`), `privacy`, `sensitive` and `purpose` (`job` by default, `check` or `orchestration`).

Routing write tools (`khipu_route_report`, `khipu_route_rule`, and `khipu_route_priority`) accept an optional `op` string (1–256 characters). Reuse it when retrying the same operation; use a fresh value for a new change. Without `op`, the server derives a private, salted identity from the MCP request id, tool arguments, and conversation identity. Retries keep the first event; reusing an explicit `op` with a different body is refused. `op` never changes who the server records as the author.

### khipu_route_report

Record what this conversation learned, so other conversations hear it.

| Parameter | Meaning |
| --- | --- |
| `action` | `report` (default), `hold` or `end_hold`. |
| `state` | For report: `limited`, `open` or `down`. |
| `account` | Label or id, as `khipu_route` lists them. |
| `runner`, `lane` | Report against a job runner's lane. |
| `until` | When a limit or hold ends: a time, or `30m`, `2h`. |
| `window` | `five-hour` or `weekly`. |
| `model` | For a limit that closes one model. |
| `message` | The limit message word for word. Khipu reads the reset time from it. |
| `seen` | When the message was shown. |
| `evidence` | One line of evidence. |
| `text` | For hold: its words. |
| `reason` | For hold: `machine-busy`, `disk-low`, `keep`, `outage` or `other`. |
| `scope` | For hold: `account`, `machine` or `all`. |
| `hold_id` | For end_hold. |
| `name` | What this conversation calls itself. |

A time you give is recorded as a guess. A hold ends after 24 hours unless `until` sets another end, and no hold may run longer than 7 days unless the owner changes that limit in settings. Standing rules, the owner's note, pauses and clears belong to the owner and are refused here.

### khipu_route_rule

Record an owner-requested standing rule, or a change to rule number `rule`. Parameters: `text` and `owner_request` (both required), `rule` (optional), and `holds` (an optional map of hold ids to `end` or `keep`). Changing an existing rule also requires `base`, the owner revision observed when preparing the request; a `base` on an addition is ignored (an addition has nothing to be stale against). A change that crosses a later owner revision is recorded as crossed and never made live, and the write answers that it did not take effect. Otherwise the wording is live at once. When the call comes from a turn that is provably the owner's (the same check `khipu_owner_act` uses), it is also confirmed at once: the answer carries `applied: true` and `instant.undo`, and every conversation gets a receipt. Any other call (a dispatched job, a headless or handed-off turn, a T3 turn without proof) leaves it pending until the owner confirms it in Settings > Accounts and routing, through owner direction, or at a terminal. This tool cannot confirm, retire, dismiss or undo a rule.

### khipu_route_priority

Record the owner’s account order. Parameters: `entries` (the accounts in the owner’s order, with his words for each role), `owner_request` and `base` (the observed owner revision) are required; `until` is optional. Each entry may have its own `until`, in addition to the whole-priority expiry. Every entry `khipu_route` lists carries a `revision`; it moves only when that entry is written. To change one account’s entry, give `account` with `append`, `role`, `exception` or `end_exception` and that entry’s `revision` (or `expect`, the whole priority’s version, which any write to the priority moves) in place of `entries`: the entry is patched under the lock and every other entry stays as it is. An entry written since it was read is not overwritten: nothing is written and the answer is `ok: false`, `status: "crossed"`, with `lost` holding the entry as it is now. A whole list from a conversation must carry the `revision` of every entry the priority already holds and leave none out; otherwise nothing is written and the answer is `ok: false`, `status: "crossed"`, with `lost` holding the entries it could have overwritten. To replace the whole list on purpose, pass `expect` (the priority’s version as read) with `entries`. `khipu route priority amend --at-owner-request` takes `--revision` the same way; without `--revision` or `--expect` it patches the one entry as before. An `exception` may also carry structured `share_pct` (1–100): while that exception is live, pace and `khipu_route_advise` use it in place of the account’s share cap; it is never parsed from the exception’s free text. A request that crosses a later owner change is recorded as crossed and never made live, and the write answers that it did not take effect. Otherwise it is live at once. When the call comes from a turn that is provably the owner's, it is also confirmed at once (`applied: true`, `instant.undo`), with a receipt in every conversation and a one-step Undo that takes the whole request back; any other call leaves it marked unconfirmed until the owner confirms or undoes it in Settings > Accounts and routing or through owner direction. This tool cannot confirm, undo or clear a priority.

### khipu_owner_act

Take an owner action because the owner asked for it in this conversation (owner direction). The only parameters are `action` (an owner action such as `route.pause`, `route.rule.confirm`, `route.priority.confirm`, `config.answer_request.confirm` or `accounts.request.confirm`) and `args` (that action’s arguments, such as `{"account": "Codex"}`). Extra top-level fields are refused.

Khipu invokes the corresponding CLI command with `--owner-direction`. The check requires a recent eligible owner turn in this conversation (within 6 hours by default) and verified prompt-hook provenance. It does not inspect or match the owner’s words. Background, dispatched and unattended jobs cannot use it. Submit only actions the owner requested in this conversation.

The answer is the command’s own result, or a tool error `{ok: false, code, error}`. `khipu_needs_you` lists everything waiting for an owner's answer and the exact call that settles each. On a refusal, report the reason to the owner. Every action is logged and can be undone from Settings > Owner actions or by requesting `direction.undo` with its id.

Conversation overrides: `route.override.grant` (`account`, `request`, and either `until` or `indefinite`; optional `ceiling_pct`, `window` (`weekly` or `five-hour`), `helpers` false to keep the conversations it starts out, and `whole_folder` true for every conversation in this folder) lets *this* conversation run ahead of pace on one account. Khipu names the conversation from this server's own environment and T3's record and freezes it into the ask; a `subject` argument is refused. `route.override.end` and `route.override.keep` (one more week, for an override with no end) take the override's `id`. They go through the same hold, one yes, receipt and Undo as every other routing change, and nothing about them is sent to other conversations. Two reads change nothing: `route.overrides.mine` (what this conversation hears) and `route.walkthrough` (the next priority clean-up item in plain words, with the one action a yes settles; `leave`, `keep` and `restart` are its bookmark, and only a conversation the owner is typing in sees the items).

Routing changes apply as soon as the owner types them: `route.priority.*`, `route.rule.*`, `route.note.*`, `route.pause`, `route.lift` and `route.hold.*` need no confirm when the turn passes the check above (in T3, when T3's own record shows an owner-typed message newer than the last time Khipu asked in that thread and the latest hook turn is typed; a T3 thread nobody has been asked in yet holds its first change, as before). An answer to something waiting on the owner (`route.priority.confirm` or `undo`, `route.rule.confirm`, `undo` or `dismiss`) is stricter in T3: it applies at once only on a typed message newer than a `khipu_needs_you` read in that thread that showed it, and is held otherwise, whatever else was asked there. Each is logged with the evidence it relied on (`instant` on its owner-actions line), and every conversation gets one short whole receipt sentence ahead of its other routing text on its next prompt, naming the change in the owner's recorded words, where it came from, and how to undo it. One Undo (`direction.undo`, `khipu owner-direction undo <id>`, or Settings > Owner actions) reverts exactly that change and is receipted too. Forget, purge, permissions and trust settings, secrets and every other action keep the explicit confirm they need.

Known limits. Khipu trusts each app's prompt hook: it can't tell apart a hook delivery that the app itself replays or reorders, or a process that impersonates the app's own agent. In T3, a held change is confirmed by the owner's latest reply after the question. T3 conversations running Codex, Cursor or Antigravity can't confirm a held change yet; confirm it from a Claude Code or Codex conversation the owner types in. Owner hold and confirm give T3's database a longer read budget with a few retries; if that read still fails, the refusal says Khipu could not read T3's record and to try again (not that the call was a dispatched job).

### khipu_needs_you

Everything waiting on the owner, in plain words: call it when the owner asks what needs confirming or approving, or what is waiting on them. Read-only, and local only: what it quotes includes pending proposals, which never leave through the gateway. Optional parameters: `kind` (one kind of item) and `offset` (items to skip).

It lists a conversation's waiting change to the priority, a standing rule, a routing setting, the account list or the tier table; a request held from any conversation; forgets the hub lacks; job-origin corrections; same-project questions; a hold to look at and an account to sign in. Only what the owner can answer is listed; delivery, job-runner and rule-check problems are in `khipu doctor` and `khipu_route`. A note can't wait: only the owner writes one.

Each item has `kind`, `words` (one or two plain sentences, no ids), `asked_by` (`who`, `at`, `when`), `confirm_does`, `undo_does` and `settle` (`confirm` and `undo`, each the exact `khipu_owner_act` `action` and `args`, with its `expect` pin; `null` where no such action exists, and `do` says what to do). An item carries `cut: true` when its words are shortened, and `read_full` is the `key` read that gives them whole; `permanent: true` when its confirm can't be undone. The answer has `line`, `total`, `counts`, `more` and `more_line` (at most 20 items an answer), `how_to_settle`, `here` (what a yes in this conversation does now) and `unread` (anything it could not read: it says nothing is waiting only when all of it was read).

Reading it confirms nothing and records no rule or priority as shown, but it is the ask: Khipu records where the conversation stood and which answers the read showed whole, and only what the owner types after that can answer. Nothing typed before it counts, the question included. In T3, one typed message newer than the read settles an item shown in it, routing kinds at once and held kinds by the same call; a turn Khipu can't prove stays held, with nothing changed. Outside T3 an answer is refused in the same turn as the read, but an agent that never read the list still passes on a recent typed turn. Khipu proves a new typed message and doesn't read it, so it can't tell a yes from "thanks". `direction.decline` drops a held request on the owner's no, on the same proof as `direction.confirm`, and is logged. `key` reads one item with its words whole. Owed items that need the owner are a separate list: `khipu_owed`.

## Work

### khipu_ledger

The project's ledger of what work is done and what is not, with the old tracker script's verbs. Parameters: `verb` (required: `add`, `set`, `get`, `list`, `loose-ends`, `needs-owner`, `needs-matt`, `render`, `check`, `remove` or `removed`), plus the same fields as `khipu ledger` as JSON: `project` (default: the folder's project), `id`, `title`, `kind`, `parent`, `state`, `evidence`, `owner`, `note`, `link`, `if_state`, `children`, `op_id`, `stale_hours` and `summary`.

Supply `id` to name the row for `add`, `set`, `get` and `remove`; writes reject an empty id rather than generating one. Add requires `title` and `kind` and may set `parent`. Set changes `state`, `evidence`, `owner`, `title` or `link`, and alone supports `if_state` to write only while the row is still in that state. Remove alone accepts `children` (`remove` or `move-up`). A write returns its result and its `op_id`; pass the same `op_id` to retry it safely. `get`, `list`, `loose-ends` and `needs-owner` return rows; `render` returns the markdown board and `check` the check's result. A refusal is a tool error carrying `error` and `reason`. When the database is away a write waits on this Mac and reaches the ledger when it answers. Restoring a removed row and removing it for good are the owner's, in the app.

Rows carry titles and short plain-words lines about the work, never code, diffs, file contents, command output, logs, prompts, briefs, customer or financial details.

`khipu_status` (the full call) carries a `ledger` line: live rows, loose ends, rows waiting on the owner and writes waiting in the outbox, across every project.

## Related

- [CLI reference](cli.md): the same operations from a terminal (`khipu search`, `khipu owed`, `khipu decisions`, `khipu route`, `khipu usage`).
- [Integrations](integrations.md): which harnesses get the server and which hooks push recall.
- [How it works](how-it-works.md): capture, storage and recall.
