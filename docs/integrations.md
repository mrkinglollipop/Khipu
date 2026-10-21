# Integrations: supported harnesses

A harness is an agent tool that Khipu wires into: it records sessions from it and gives it memory to search. This page lists the supported harnesses, what Khipu installs in each, what each hook does, and how sessions are identified.

Related pages: [how it works](how-it-works.md), [setup](setup.md), [CLI reference](cli.md), [MCP tools](mcp-tools.md), [desktop app](desktop-app.md), [routing](routing.md), [operations](operations.md), [FAQ](faq.md).

## Supported harnesses

Khipu ships five integration packs.

| Pack | Where it runs | How it reaches memory |
| --- | --- | --- |
| `claude_code` | Your Mac | Directly (database) |
| `cursor` | Your Mac | Directly (database) |
| `codex` | Your Mac | Directly (database) |
| `aegis` | Your Mac, in a sandbox | Capture is queued in the sandbox and written by a drain outside it. Search goes through the gateway. |
| `grok_bot` | Cloud | HTTPS gateway only |

T3 Code is not a pack. Claude accounts that run inside T3 Code are covered by the `claude_code` pack, which installs into every Claude config home T3 uses. Khipu only reads T3's settings and database (never writes them). It uses them to link threads, skip helper sessions, and show T3 health in the Harnesses screen. Search can filter by T3 thread.

Stock Grok Build has no pack of its own. A Grok Build pack is planned. Today, Grok Build sessions that run the Aegis pack are captured under the label `aegis`.

## What each pack installs

### Claude Code

Khipu finds every Claude config home: `~/.claude`, the folder in `CLAUDE_CONFIG_DIR`, and each Claude instance's home path in T3's settings. Install and uninstall act on all of them, or on one with `--home`. A home whose `settings.json` links to another shares that home's hooks.

In each home's `settings.json`:

| Event | Hook | Timeout |
| --- | --- | --- |
| Stop, PreCompact, SessionEnd, SubagentStop | `khipu-stop-hook` | 20 s |
| SessionStart | `khipu-recall-hook` | 10 s |
| UserPromptSubmit | `khipu-prompt-recall` | 3 s |
| PreToolUse (delegation tools and shell commands) | `khipu-delegation-hook` | 3 s |
| SubagentStart | `khipu-delegation-hook` | 10 s |

The MCP server (id `khipu`, launcher `khipu-mcp`) is written to `~/.claude.json` and to each home's own `.claude.json`.

The delegation hook is a routing feature: it only adds context when the `routing_push` feature is on.

### Codex

- `~/.codex/config.toml` gets an `[mcp_servers.khipu]` entry.
- `~/.codex/hooks.json` gets the same hooks as Claude Code: Stop, PreCompact, SessionEnd and SubagentStop run `khipu-stop-hook`; SessionStart runs `khipu-recall-hook`; UserPromptSubmit runs `khipu-prompt-recall`; PreToolUse and SubagentStart run `khipu-delegation-hook`.
- Codex limits SessionEnd to 3 seconds, so that event uses a queue-only variant of the stop hook.
- Codex keeps new hooks off until you review them. The install prints that next step.

### Cursor

- `~/.cursor/mcp.json` gets `mcpServers.khipu`.
- `~/.cursor/hooks.json` gets `stop`, `preCompact`, `sessionEnd` and `subagentStop` running `khipu-stop-hook` (20 s), and `sessionStart` running `khipu-recall-hook --cursor` (30 s). The `--cursor` flag makes the hook emit Cursor's `additional_context` field.
- Cursor gets no per-prompt recall hook and no delegation hook.
- With `--project DIR`, Khipu also writes `DIR/.cursor/rules/khipu.mdc`, an always-on rule that tells the agent to search memory. This is the only per-prompt mechanism Cursor gets.

### Aegis

- `~/.grok/config.toml` gets an `[mcp_servers.khipu]` entry and a managed block of `[[hooks.Stop]]`, `[[hooks.PreCompact]]` and `[[hooks.SessionEnd]]`. Each runs `khipu-aegis-capture` with `KHIPU_HARNESS=aegis` (15 s).
- That hook only queues. Aegis's sandbox cannot reach the Keychain or the database. The model call and the database write happen outside the sandbox, in `khipu sessions drain` (also reachable as `khipu aegis`), in another harness's stop hook, or in the `queue_drain` background job.
- There is no recall push of any kind: Aegis discards hook output at session start and prompt submit. There is no SubagentStop and no delegation hook. Khipu's recall, prompt-recall and delegation scripts refuse to run when they detect Aegis. Aegis agents pull memory with `khipu_status` and `khipu_owed` instead.
- Aegis search needs the gateway. It reads a bearer token from the `KHIPU_GATEWAY_TOKEN` environment variable or the token file (never the Keychain). `khipu gateway token set` stores it, and `khipu integrations verify aegis` does a real round trip.
- If the token file exists, the install also writes a `[memory]` backend entry that points at Khipu.

### Grok Bot (cloud)

Grok Bot reaches Khipu only through the HTTPS gateway, as server `khipu-cloud`, with the token taken from `KHIPU_GATEWAY_TOKEN`. The token is never written to a repo. A `gateway_url` must be configured first (`khipu config --set-gateway-url`, https only).

- `khipu integrations install grok_bot` prints the account-level config to paste once into Cursor's cloud-agent MCP settings, plus the name of the secret to add. This is the normal path.
- With `--project DIR` it instead writes `DIR/.cursor/mcp.json` and `DIR/.cursor/rules/khipu.mdc` for that one repo.
- `verify` does an MCP handshake and checks that a wrong token is refused.

Through the gateway there is no hook, so `khipu_capture` writes immediately and is the only way the session is remembered. A few tools are unavailable there (usage, the routing write tools, and forgetting episodes from local harnesses); `khipu_route` answers the closed gateway projection described in [Routing](routing.md) when the owner has turned on the cloud routing copy. See [MCP tools](mcp-tools.md).

Cloud routing requires hub migration `0044_routing_gateway_copy.sql`, an updated
Mac daemon with `routing_cloud_copy` enabled and a hub connection, and a gateway
deploy. `khipu route copy publish` manually publishes the Mac's scrubbed closed
projection. The container's own routing configuration is irrelevant. Responses
carry the hub `as_of` and a stale mark after the publishing Mac's stale bound;
no published row means unavailable. Give every conversation a stable, unique
`session_id`; token labels separate clients that reuse session ids. Pull batches
and acknowledgements persist in the hub's `routing_gateway_delivery` table, so
container replacement retains them. A failed database/receipt transaction fails
closed with `available: false`; no ephemeral in-container fallback accepts acks.
Migration apply and gateway deploy remain separate operator steps.


## What each hook does

| Hook | Event | What it does |
| --- | --- | --- |
| `khipu-stop-hook` | Stop, PreCompact, SessionEnd, SubagentStop | Capture. Reads the transcript named in the hook payload and decides whether a capture is due. If so, it queues a job. It also drains the shared queue (summarise, write to the database, mirror, embed), replays the outbox, reconciles edited notes, does a bounded embedding catch-up, and writes a heartbeat per harness. SubagentStop tracks the subagent's own transcript under a separate key. |
| `khipu-recall-hook` | SessionStart | Pushes the recall rule, a project slice (open commitments, recent episodes, linked topics) and the routing summary into context, fitted to the harness's size budget. Helper sessions get nothing. `--cursor` emits Cursor's format. |
| `khipu-prompt-recall` | UserPromptSubmit | Runs a bounded search on the prompt. 1.2 s budget, top 3 hits under a "Prior work on this topic" heading, hard cap of 600 characters, relative score floor, dedup over the last 3 prompts, trivial prompts skipped. No summariser or chat model; on a query-vector cache miss with an embedding profile active it makes one embedding call, so the prompt goes to that provider. Returns `{}` on any error. |
| `khipu-delegation-hook` | PreToolUse, SubagentStart | Before work is handed to another agent, adds routing changes as extra context. A starting subagent gets the full routing summary. It never denies or alters a call. See [routing](routing.md). |
| `khipu-aegis-capture` | Stop, PreCompact, SessionEnd (Aegis) | Queues the session for capture. Nothing else. |

All hooks fail open: a Khipu problem must not block your agent. The recall and delegation pushes are controlled by feature switches (`khipu features`).

### Capture cadence

A Stop is due for capture after 5 user turns or 20 minutes. PreCompact and SessionEnd always capture. A window under 200 characters is never summarised. The first time Khipu sees a transcript it starts a clock and reads from near the end, so a one-question session is not captured on its first Stop.

Each summariser call carries at most 14,000 characters. A longer window is split into parts, and each part becomes its own call and episode. A daily call ceiling delays work rather than dropping it (`khipu budget`). The summariser is set with `khipu models`; the default is Gemini.

Near-duplicate captures made within five minutes merge into the earlier episode. The capture mode (`legacy`, `dual`, `hub`; default `dual`) decides whether captures go to files, the database, or both. Set it in Settings, Capture & models, or with `khipu config --set-capture-mode`.

The `khipu_capture` MCP tool does not write on a Mac where the stop hook is installed. It flags the session and returns `{queued: true}`, and your summary is kept as a note on the episode.

## Capture identity

Every capture is labelled with a harness and a session id of the form `<harness>:<stable id>`. The harness comes from `KHIPU_HARNESS` if set, then from Grok runner environment variables (which give `aegis`), then from the shape of the hook payload and transcript. The capture harnesses are `claude_code`, `cursor`, `codex` and `aegis`; a payload Khipu cannot place is labelled `unknown`. A dispatched child session records its parent through `KHIPU_PARENT_SESSION` or `CLAUDE_CODE_HOST_SESSION_ID`.

Search filters use these labels: `--harness`, `--session-id` and `--via` (capture origin, such as `t3`). The `khipu_search` MCP tool also takes `t3_thread`, an exact T3 thread id.

A source record table (`episode_sources`) now records where each episode came from. Each row has a source key, a hash of the harness, session id, transcript range, window and part. It is written alongside each capture. Nothing reads it yet, and episode uniqueness still works as before. Moving readers onto it is planned.

## Install and repair

In the app: open **Harnesses**. Each card shows whether the harness is wired in and recording, with install, verify and remove actions. The setup flow's Agents step links to the same screen. See [desktop app](desktop-app.md) and [setup](setup.md).

From the command line:

```
khipu integrations status                 # what is present per harness
khipu integrations install claude_code    # or cursor, codex, aegis, grok_bot (default: all)
khipu integrations verify                 # MCP handshake and hook exit checks
khipu integrations uninstall codex        # removes Khipu-owned entries only
```

Options: `--dry-run` (install and uninstall) shows what would change; `--no-verify` (install) skips the check after install; `--home` (install, verify and uninstall) limits Claude Code to one home; `--project` is for Cursor and Grok Bot. Uninstall removes only entries Khipu wrote, and backups are kept. Uninstalling Grok Bot needs `--project`.

To repair a broken harness, run `khipu integrations status`, then install it again. Install is safe to repeat. Check recording with `khipu sessions liveness` (exits 2 if a harness has not been recorded) and `khipu doctor`. See [operations](operations.md) and [CLI reference](cli.md).

There is no installed `khipu` command: run the CLI as `python3 -m khipu`, or alias it.
