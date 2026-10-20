# MCP tools

Khipu exposes 19 tools through one MCP server (server id `khipu`). Agents call them to read memory, write to it, manage commitments and decisions, ask for synthesis, and read the routing record, and take an owner action the owner asked for.

The tool list is returned verbatim by the server's `tools/list`. This page groups the tools by job. For the exact schema of any tool, ask the server.

## How the server is reached

- **Locally (stdio).** The launcher is `packages/cli/bin/khipu-mcp`. `khipu integrations install` writes it into each harness's MCP config. See [Integrations](integrations.md).
- **Over HTTPS (the gateway).** The gateway serves the same tool list. A few tools answer differently there:
  - The five routing tools (`khipu_usage`, `khipu_route`, `khipu_route_report`, `khipu_route_rule`, `khipu_route_priority`) and the `routing` field of `khipu_status` reply `{available: false, reason}`. They are local only.
  - `khipu_forget` refuses only episodes whose session id starts with a local harness name (`claude_code`, `cursor`, `codex` or `aegis`); any other episode, including one with no session id, can be forgotten through it, because the gateway does not check that the episode came from a cloud harness. A stricter check is planned.
  - `khipu_capture` writes immediately (see below).
  - `khipu_owner_act` is not listed and refuses every call: owner direction needs the owner's typed turns, which only a prompt hook on the owner's Mac records.
- **Features that are off.** `khipu_brief` and `khipu_reflect` reply `{available: false}` while their feature switches are off. Both default to off. `khipu features` shows the switches.

## Memory read

### khipu_search

Search memory. Read-only.

| Parameter | Meaning |
| --- | --- |
| `query` | Required. The text to look for. |
| `limit` | Default 12, capped at 50. |
| `mode` | `hybrid` (default), `literal` or `semantic`. |
| `semantic` | Deprecated alias for `mode: semantic`. |
| `kind` | `episode`, `topic`, `node` or `library`. Semantic mode also accepts `media`. |
| `source` | A library name. Implies `kind: library`. |
| `project` | Boost or filter to one project. |
| `since`, `until` | An ISO date or a relative span such as `7d` or `24h`. |
| `session_id` | Session id prefix. |
| `harness` | The part of the session id before the colon. |
| `t3_thread` | Exact T3 Code thread id. Episodes only. |
| `via` | Exact capture origin, for example `t3`. Episodes only. |
| `tz` | Time zone for natural-language time phrases, used when the `time_interpretation` feature is on. |

Hybrid mode fuses cosine similarity, token overlap and a literal match by reciprocal-rank fusion. With no active embedding profile it falls back to literal plus token overlap and sets `degraded: "no-embedding"`. Graph nodes are left out unless `kind` is `node` or the query looks like an id. Libraries are included in a default search.

### khipu_get

Load a search hit by id. Read-only.

| Parameter | Meaning |
| --- | --- |
| `id` | Required. An episode id (digits), a topic slug, a media id, or a library id. |
| `kind` | Optional hint for the id type. |
| `offset` | Byte offset into a topic body. |
| `max_bytes` | Page size for a topic body. Default 65536, maximum 524288. |

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
| `budget_ms` | Time budget for the prior-work lookup. Default 600. |
| `full` | With `prompt`, return the complete payload instead of the light one. |

With `prompt` and no `full`, the answer always has `hub_ok`. `prior_work`, `prior_work_text`, `prior_work_meta`, `notes_freshness`, `routing`, `contract` and `hub_error` are optional: each is left out when it is unavailable or its lookup fails. `prior_work` holds up to three items and `prior_work_text` is a short Markdown block. Without `prompt` (or with `full`), the answer is the complete payload: counts, recent captures, replica freshness (`hub_snapshot`, `snapshot_age_seconds`, `snapshot_state`), `pending_turns`, the capability `contract`, and `warnings` for plans nearly used up (read from the usage cache only). If the hub cannot be reached, the answer falls back to the local replica and carries `hub_error`.

The prompt path is the pull path for harnesses that get no pushed recall, such as Aegis and the gateway.

### khipu_brief

Read the source-backed brief for one topic. Read-only. Parameter: `topic` (required). Every claim in a brief lists the episode ids it came from. The `state` is `current` or `stale`. Off by default (`briefs` switch).

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

### khipu_forget

Tombstone one episode. Parameter: `id` (required). The row is not erased: its summary and provenance stay in the database. Search and `khipu_get` skip it, but today the recent-capture lists (Activity, doctor, status, the full `khipu_status` and the session-start fallback) can still show it; a fix is planned. Its vectors and legacy file lines are removed, the commitments it opened are closed (the closed rows stay) and its decisions are retracted. Graph topic nodes and earlier database backups are not touched. The legacy file and replica steps can fail, and outbox jobs for the session are reported, not removed; either returns `complete: false` with the remaining stores in `partial`. Use it for a wrong capture or a test. Over the gateway it refuses only episodes whose session id starts with a local harness name (`claude_code`, `cursor`, `codex` or `aegis`); any other episode, including one with no session id, can be forgotten through it, because the gateway does not check that the episode came from a cloud harness. A stricter check is planned.

## Commitments and decisions

### khipu_owed

List commitments (followups, blockers, questions, promises). Read-only.

| Parameter | Meaning |
| --- | --- |
| `project` | Filter to one project. |
| `status` | `open` (default), `closed` or `stale`. |
| `limit` | Default 50, capped at 200. |

Call it at session start in a harness that gets no pushed project slice.

### khipu_owed_update

Close, reopen or snooze one commitment.

| Parameter | Meaning |
| --- | --- |
| `id` | Required. A commitment id from `khipu_owed`. |
| `action` | Required. `close`, `reopen` or `snooze`. |
| `until` | For snooze: a date, or `7d`, `2w`, `1m`. |

### khipu_decisions

List decisions with their validity. Read-only.

| Parameter | Meaning |
| --- | --- |
| `project` | Filter to one project. |
| `status` | `standing` (default), `superseded`, `retracted`, `all` or `conflicts`. |
| `since` | ISO date or relative, such as `7d`. |
| `limit` | Default 20, capped at 100. |

`conflicts` returns unresolved candidate links, with the text of both sides, that nobody has confirmed or rejected yet.

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

Supersede only when the user reverses or replaces a decision, not for a refinement. Retract marks a decision wrong outright.

## Synthesis

### khipu_reflect

Answer one question from memory, with the evidence. Read-only. Parameters: `question` (required), `project`. It runs one search, then asks the configured model to answer only from what it found. Each claim lists the episode or topic ids it rests on, and it abstains rather than answer without sources. It writes no memory. Off by default (`reflect` switch).

`khipu_brief` (above) is the other synthesis tool: a stored, cited summary of one topic rather than an answer to a question.

## Routing

These tools are local only. They report account state and never choose, rank or launch work: Khipu reports, and routing stays with the caller. See [Routing](routing.md) for the full model.

### khipu_usage

How much of each subscription plan is left (Claude, Codex, Grok, Muse). Read-only. It uses no model call and spends no tokens.

| Parameter | Meaning |
| --- | --- |
| `fresh` | Start a refresh now even if the cache is young. |
| `max_age` | Cache age limit in seconds. Default 120. |

It answers from the cache at once and refreshes in the background. Ask again a few seconds later for new figures. Errors are short codes such as `timeout` or `not_signed_in`. A window a CLI does not report is absent, not zero.

### khipu_route

The routing record: which accounts are open, limited (with the end time and whether it is the CLI's own or a guess), paused, down or unknown; the owner's standing rules and note; holds; and job-runner lanes not matched to an account. Read-only.

| Parameter | Meaning |
| --- | --- |
| `since` | A version from an earlier answer. Returns only what changed after it. |
| `account` | Narrow to one account, by label or id. |

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

Record an owner-requested standing rule, or a change to rule number `rule`. Parameters: `text` and `owner_request` (both required), `rule` (optional). The wording is live at once but stays pending until the owner confirms it in Settings, Routing, or at a terminal. This tool cannot confirm, retire, dismiss or undo a rule.

### khipu_route_priority

Record the owner's account order. Parameters: `entries` (the accounts in the owner's order, with his words for each role) and `owner_request` (both required), `until` (optional). It is live at once and marked unconfirmed until the owner confirms or undoes it in Settings, Routing. This tool cannot confirm, undo or clear a priority.

### khipu_owner_act

Take an owner action because the owner asked for it in this conversation (owner direction). Parameters: `action` (one of the owner actions, such as `route.pause`, `route.rule.confirm`, `route.priority.confirm`, `config.answer_request.confirm` or `accounts.request.confirm`), `args` (that action's arguments, the same the CLI command takes, such as `{"account": "Codex"}`), `words` (the owner's words, quoted exactly, at least three words), `turn` (the token from the latest `Owner turn:` line) and, for a dispatched job only, `owner_session` (the session it was started from).

Khipu runs the same command and the same check as `khipu ... --owner-words` on the CLI: the words must be in a recorded, fresh turn the owner typed in this conversation and must ask for this action and state every one of its arguments: what it acts on (the account, the row and its project, the setting, the front end), the field it changes, the value, the end and the order, each in the owner's words or a documented synonym. An argument the owner did not say is refused, naming it; ask the owner to say it. The conversation is read from the MCP server's own environment, never from the arguments. A dispatched job may name its parent session only when the job's first turn is the brief that session dispatched; the words and the token are then the parent's, and the log names both sessions. A request waiting for the owner (a rule, a priority, a routing setting or an account list change) is confirmed the same way, and "that" or "it" names it when it is the only one of its kind waiting.

The answer is the command's own result, or a tool error `{ok: false, code, error}`. On a refusal, tell the owner what Khipu said and ask them to say it again; never retry with different words. Every action is logged and can be undone from Settings or by the owner's words.

## Work

### khipu_ledger

The project's ledger of what work is done and what is not, with the old tracker script's verbs. Parameters: `verb` (required: `add`, `set`, `get`, `list`, `loose-ends`, `needs-owner`, `needs-matt`, `render`, `check`, `remove` or `removed`), plus the same fields as `khipu ledger` as JSON: `project` (default: the folder's project), `id`, `title`, `kind`, `parent`, `state`, `evidence`, `owner`, `note`, `link`, `if_state`, `children`, `op_id`, `stale_hours` and `summary`.

A write returns its result and its `op_id`; pass the same `op_id` to retry it safely, and `if_state` to write only if the row is still in that state. `get`, `list`, `loose-ends` and `needs-owner` return rows; `render` returns the markdown board and `check` the check's result. A refusal is a tool error carrying `error` and `reason`. When the database is away a write waits on this Mac and reaches the ledger when it answers. Restoring a removed row, removing it for good and importing an old tracker are the owner's, in the app.

Rows carry titles and short plain-words lines about the work, never code, diffs, file contents, command output, logs, prompts, briefs, customer or financial details.

`khipu_status` (the full call) carries a `ledger` line: live rows, loose ends, rows waiting on the owner and writes waiting in the outbox, across every project.

## Related

- [CLI reference](cli.md): the same operations from a terminal (`khipu search`, `khipu owed`, `khipu decisions`, `khipu route`, `khipu usage`).
- [Integrations](integrations.md): which harnesses get the server and which hooks push recall.
- [How it works](how-it-works.md): capture, storage and recall.
