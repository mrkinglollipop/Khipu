# MCP tools

Khipu exposes 17 tools through one MCP server (server id `khipu`). Agents call them to read memory, write to it, manage commitments and decisions, ask for synthesis, and read the routing record.

The tool list is returned verbatim by the server's `tools/list`. This page groups the tools by job. For the exact schema of any tool, ask the server.

## How the server is reached

- **Locally (stdio).** The launcher is `packages/cli/bin/khipu-mcp`. `khipu integrations install` writes it into each harness's MCP config. See [Integrations](integrations.md).
- **Over HTTPS (the gateway).** The gateway serves the same tool list. A few tools answer differently there:
  - The five routing tools (`khipu_usage`, `khipu_route`, `khipu_route_report`, `khipu_route_rule`, `khipu_route_priority`) and the `routing` field of `khipu_status` reply `{available: false, reason}`. They are local only.
  - `khipu_forget` only forgets episodes captured by cloud harnesses.
  - `khipu_capture` writes immediately (see below).
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

With `prompt` and no `full`, the answer always has `hub_ok`, `prior_work`, `prior_work_text` and `prior_work_meta`. `notes_freshness`, `routing`, `contract` and `hub_error` are optional: each is left out when it is unavailable or its lookup fails. `prior_work` holds up to three items and `prior_work_text` is a short Markdown block. Without `prompt` (or with `full`), the answer is the complete payload: counts, recent captures, replica freshness (`hub_snapshot`, `snapshot_age_seconds`, `snapshot_state`), `pending_turns`, the capability `contract`, and `warnings` for plans nearly used up (read from the usage cache only). If the hub cannot be reached, the answer falls back to the local replica and carries `hub_error`.

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

Hide one episode from every reader. Parameter: `id` (required). The episode row is tombstoned, not erased: its summary and provenance stay in the database. Its vectors and legacy file lines are removed, the commitments it opened are closed (the closed rows stay) and its decisions are retracted. Graph topic nodes and earlier database backups are not touched. File, replica and outbox cleanup is best effort; a failure returns `complete: false` with the remaining stores in `partial`. Use it for a wrong capture or a test. Over the gateway it only works on episodes from cloud harnesses.

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

## Related

- [CLI reference](cli.md): the same operations from a terminal (`khipu search`, `khipu owed`, `khipu decisions`, `khipu route`, `khipu usage`).
- [Integrations](integrations.md): which harnesses get the server and which hooks push recall.
- [How it works](how-it-works.md): capture, storage and recall.
