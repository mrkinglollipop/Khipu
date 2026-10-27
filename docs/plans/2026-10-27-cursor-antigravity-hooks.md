# Cursor and Antigravity hook packs

Status: draft scope, written 2026-10-27 against origin/main 10720cf5. Not approved yet. The decisions in section 6 belong to Matt.

Owner direction (Matt, 2026-10-27): "Just because we don't use antigravity doesn't mean other people won't. So we need hooks for cursor and anti-gravity so other users can use khipu there."

## 1. Goal

A Khipu user on Cursor or Google Antigravity gets what Claude Code and Codex users get: capture, session-start recall, per-prompt recall, the routing start block, routing at delegation, the MCP tools and owner-direction turn recording. This applies when the harness is used on its own and when it runs as a T3 Code provider. Where the harness can't carry a function, Khipu says so plainly in the app and uses the best fallback. A missing function is never reported as installed.

Out of scope: Grok Bot (cloud, gateway pack, unchanged), Aegis and Grok Build packs, and using Antigravity or Cursor for Matt's own routed work. The routing rules are unchanged: Antigravity is not a work lane unless Matt asks.

## 2. What Khipu has today (origin/main)

- `packages/cli/khipu/integrations.py` has a `cursor` pack. It writes `~/.cursor/mcp.json` `mcpServers.khipu` → `khipu-mcp`, and `~/.cursor/hooks.json` `stop`, `preCompact`, `sessionEnd` and `subagentStop` → `khipu-stop-hook` (timeout 20). It also writes `sessionStart` → `khipu-recall-hook --cursor` (timeout 30, flat `additional_context`). With `--project` it adds `.cursor/rules/khipu.mdc`. `_cursor_status()` reports mcp, the hook flags, `recall_rule: "project_scoped"` and launchers. It has no `prompt_recall` or `delegation_push` key. The module comment and `recall_prompt.py` both say "Cursor has no per-prompt hook event", which the shipped code now contradicts (section 3.1).
- The `claude_code` and `codex` packs use Claude-shaped `{hooks: {Event: [{matcher?, hooks: [{type, command, timeout}]}]}}`. They cover Stop, PreCompact, SessionEnd, SubagentStop → `khipu-stop-hook` (Codex SessionEnd is queue-only with a 3 s cap), SessionStart → `khipu-recall-hook`, UserPromptSubmit → `khipu-prompt-recall` (timeout 3), and PreToolUse (matcher `DELEGATION_MATCHER`) plus SubagentStart → `khipu-delegation-hook`. Codex adds `next_step = CODEX_REVIEW_STEP`, because Codex runs a new hook only after the owner reviews it. Every write backs up to `*.bak-khipu-<stamp>`, and uninstall removes only commands that `_is_launcher_command` matches.
- Shims live in `~/.config/khipu/bin/<name>` (`_shim`) and resolve through the promoted live checkout. This scope never touches `~/.config/khipu/live*`.
- No antigravity or gemini-agent code exists anywhere. The only Gemini code is the embedding and summariser provider.
- `t3.provider_hooks()` returns `installed` only for `claudeAgent` (reads `<homePath or ~/.claude>/settings.json`) and `codex` (reads `<homePath or ~/.codex>/hooks.json`). Every other driver is `missing`, so doctor warns "T3 provider cursor has no Khipu hooks, so sessions on it get no Khipu routing start block". The rail test fixture pins that string. `routing_delegation._t3_child_advisory` adds "add `khipu route --brief`" for such children.

Payload shapes the hook code reads today:

| Code | Reads |
|---|---|
| `session_capture.session_id` | `sessionId`, `session_id`, `conversation_id` |
| `session_capture.session_cwd` | `cwd`, else `workspace_roots[0]` |
| `session_capture.infer_harness` | `KHIPU_HARNESS`; GROK vars → aegis; `conversation_id` or `workspace_roots` or `/.cursor/` in `transcript_path` → cursor; Claude home paths → claude_code; `/.codex/`, `CODEX_HOME` → codex; `CLAUDE_PROJECT_DIR` → claude_code |
| `session_capture.transcript_path` | `transcriptPath`/`transcript_path`; Cursor fallback glob `~/.cursor/projects/*/agent-transcripts/<sid>/<sid>.jsonl` |
| `recall_rule.session_start_main` | `session_id`/`sessionId`, `cwd`; output shape from argv `--cursor` (flat `additional_context`), else nested `hookSpecificOutput` |
| `recall_prompt.hook_main` | `prompt`/`message`, `cwd`, `session_id`/`sessionId`; shape `cursor` accepted but never installed |
| `owner_turns.record` / `harness_of` | `session_id`/`sessionId`, `prompt` (must be str), `cwd`, `transcript_path`; harness from env (no Cursor or Antigravity branch, so both read `unknown`) |
| `routing_delegation` | `hook_event_name`, `tool_name`, `tool_input`, `agent_id`, `session_id`; output is always nested `hookSpecificOutput` |

Episode ids are `<harness>:<native id>` (`canonical_session_ref`). Harness lists that must learn a new name: `session_capture.HARNESSES`, `integrations.HARNESSES`, the `capture.py` T3 prefix set `{"claude_code","codex","cursor","aegis"}`, `owner_turns._HARNESS_WORDS` and `DEFAULT_TRUSTED`, and the desktop `HarnessId` union.

## 3. Evidence: what each harness offers

Labels: **docs** = official page (read 2026-10-27); **binary** = read from the installed program on this Mac (version given); **log** = Khipu's own logs on this Mac; **forum** = vendor forum; **unverified** = from memory or inference.

### 3.1 Cursor (IDE 3.26.13, `cursor-agent` 2026.10.22-27dcf5f, `@cursor/sdk` 1.0.35 inside T3 0.1.2)

- Config: `~/.cursor/hooks.json` (user), `<project>/.cursor/hooks.json`, plus enterprise (`/Library/Application Support/Cursor/hooks.json`) and team; `{version: 1, hooks: {event: [{command, type?, timeout?, matcher?, loop_limit?, failClosed?}]}}`. All matching hooks run. Source: docs, https://cursor.com/docs/agent/hooks.
- Events: sessionStart, sessionEnd, beforeSubmitPrompt, preToolUse, postToolUse, postToolUseFailure, subagentStart, subagentStop, beforeShellExecution, afterShellExecution, beforeMCPExecution, afterMCPExecution, beforeReadFile, afterFileEdit, preCompact, stop, afterAgentResponse, afterAgentThought, tab hooks, workspaceOpen. Source: docs.
- Common payload: `conversation_id`, `generation_id`, `model`, `hook_event_name`, `cursor_version`, `workspace_roots`, `user_email`, `transcript_path`. The executor also adds `session_id` (= `session_id ?? conversation_id`) to every event, and `agent_transcript_path` on subagentStop. Hook env: `CURSOR_PROJECT_DIR`, `CURSOR_VERSION`, `CURSOR_TRANSCRIPT_PATH` **and `CLAUDE_PROJECT_DIR`** (alias). Source: docs and binary (SDK `buildHookEnvironment`).
- Context injection:
  - docs: `additional_context` exists only on sessionStart, postToolUse and postToolUseFailure. beforeSubmitPrompt returns `continue`/`user_message` only, and subagentStart returns `permission`/`user_message`.
  - forum: staff said in 2026 (Cursor 3.2.7) that beforeSubmitPrompt ignores extra fields. The open request is https://forum.cursor.com/t/add-additional-context-to-beforesubmitprompt-hook-output/157231 (Apr–Jul 2026), and postToolUse `additional_context` was reported broken in 3.1.15 (Apr 2026).
  - binary: IDE 3.26.13, CLI 2026.10.22 and SDK 1.0.35 all contain `HOOK_STEPS_SUPPORTING_ADDITIONAL_CONTEXT` = {sessionStart, beforeSubmitPrompt, preToolUse, postToolUse, postToolUseFailure}, with a hard cap of **10,000 characters** (`HookAdditionalContextTooLargeError` above it). So per-prompt injection is probably shipped but undocumented. Only a real run can settle it.
- Third-party (Claude Code) hooks: Cursor also loads `~/.claude/settings.json`, `.claude/settings.json` and `.claude/settings.local.json`. It maps UserPromptSubmit→beforeSubmitPrompt, SessionStart→sessionStart, Stop→stop, PreToolUse→preToolUse, and so on, and tool names Bash→Shell, Task→Task. The IDE toggle is on by default. Sources: docs, https://cursor.com/docs/reference/third-party-hooks; forum, https://forum.cursor.com/t/no-way-to-opt-out-of-third-party-claude-code-codex-config-loading-the-flag-is-hardcoded/166558 (Jul 2026, staff confirmed the CLI cannot opt out). binary: the loader dedupes a Claude hook only when its `command` string equals a Cursor hook's for the same event.
  - log, this Mac 2026-10-27: every Cursor session start runs Khipu's recall **twice**, once as `harness=cursor` (the Cursor pack's `khipu-recall-hook --cursor`) and once as `harness=claude_code` (the imported Claude entry, which has a different command string, so no dedupe). Both inject about 9.2 kB and add about 3.2 s each.
- CLI coverage: forum staff say the CLI runs sessionStart, stop, postToolUse and the shell and file hooks, but not afterAgentResponse or afterAgentThought (https://forum.cursor.com/t/hooks-afteragentresponse-afteragentthought-not-firing-in-headless-cli/156220). Whether the CLI runs sessionEnd, preCompact and beforeSubmitPrompt: unverified.
- T3: the Cursor provider runs `@cursor/sdk` in-process with `settingSources: ["project","user","team","mdm","plugins"]`. T3's own comment says this is "every layer the Cursor CLI loads … hooks, and MCP servers", so `~/.cursor/hooks.json`, `~/.cursor/mcp.json` and the Claude import all apply. Its title helper uses `settingSources: []` (no hooks, correct). The instance config has no `homePath`. Source: binary (T3 app.asar `CursorAgentSdk.ts`). log: T3 worktree Cursor sessions are captured (`capture cursor sessionend … captured=1`), and their sessionStart push runs.
- Routing at start: log shows that only 69 of 383 Cursor session starts carried the routing summary (`routing=yes`). The memory slice plus summary usually overruns the 9,800-unit budget (`recall_rule._HOOK_CONTEXT_BUDGET`). Cursor's real cap is 10,000 characters.

### 3.2 Google Antigravity, standalone (App, CLI `agy`, IDE)

Source for this section: docs, https://antigravity.google/docs/hooks?tab=cli, /docs/mcp and /docs/rules, unless noted.

- Hooks config: `~/.gemini/config/hooks.json` (global, shared by App, CLI and IDE) and `<workspace>/.agents/hooks.json`. The CLI does not read `~/.gemini/antigravity-cli/settings.json`. The file is keyed by a hook *name*: `{"khipu": {"enabled": true, "SessionStart": [handler], "PreToolUse": [{matcher, hooks: [handler]}], …}}`, and a handler is `{type: "command", command, timeout (s, default 30)}`.
- Events: SessionStart, PreToolUse, PostToolUse, PreInvocation, PostInvocation, Stop. There is no SessionEnd, PreCompact, UserPromptSubmit or SubagentStart.
- Payload: camelCase. All events carry `conversationId`, `workspacePaths`, `transcriptPath`, `artifactDirectoryPath` and `modelName`. Pre and PostInvocation add `invocationNum` and `initialNumSteps`. Stop adds `executionNum`, `terminationReason`, `fullyIdle`. Tool events add `toolCall.{name,args}` and `stepIdx`. **No prompt text and no cwd.** binary (ACP 1.3.0 proto): `HookArgsCommon` also has `last_user_input`, `agent_name` and `parent_conversation_id`, all undocumented.
- Injection: SessionStart, PreInvocation and PostInvocation return `injectSteps: [{systemMessage | userMessage | ephemeralMessage | toolCall}]`. PreToolUse returns a **required** `decision`. claude-mem found that registering PreToolUse skipped agy's permission prompt (https://docs.claude-mem.ai/antigravity-cli/setup, agy 1.2.1), so Khipu never installs PreToolUse there.
- Transcript: `<app_data_dir>/brain/<conversationId>/.system_generated/logs/transcript.jsonl`. `app_data_dir` is `~/.gemini/antigravity` (App), `antigravity-cli` (CLI) or `antigravity-ide` (IDE). binary/file: the records are JSON steps with `type` (USER_INPUT, PLANNER_RESPONSE, AGENCY_TOOL_CALL), `content`, `step_index`, `created_at` and `source`.
- MCP: `~/.gemini/config/mcp_config.json` (`mcpServers.<name>: {command, args, env}`) and `<workspace>/.agents/mcp_config.json`. Unconfigured MCP tools run in Ask mode.
- Rules: `~/.gemini/GEMINI.md` and `~/.gemini/config/GEMINI.md` (always on), `~/.gemini/config/rules/*.md` (frontmatter `trigger: always_on`), and in a workspace `AGENTS.md`, `GEMINI.md` and `.agents/rules/*.md`. Files over 24,000 bytes are truncated.
- Reliability: forum, https://discuss.ai.google.dev/t/stop-and-posttooluse-hooks-in-agents-hooks-json-never-fire-antigravity-ide-1-107-0-windows/178288 (14 Aug 2026), reports workspace hooks never firing on IDE 1.107.0 on Windows. Staff said "update to 2.5.5". There's no confirmed fix.
- Prior art: claude-mem's Antigravity integration uses SessionStart injection, PreInvocation (first call of a turn) for per-prompt work, PostInvocation reading `PLANNER_RESPONSE`, and Stop for summaries. It writes MCP to both `~/.gemini/antigravity/mcp_config.json` and `~/.gemini/config/mcp_config.json`. On this Mac the first is a symlink to the second.

### 3.3 Antigravity as a T3 provider (T3 0.1.2, managed ACP runtime 1.3.0)

Source for this section: binary (T3 app.asar and `agy_acp_server.par` 1.3.0).

- T3 runs a downloaded `agy_acp_server.par` over ACP with `GEMINI_HOME=~/.t3/userdata/providers/antigravity/<sha256(instance id) hex>`. Its own comment says skills are linked back to `~/.gemini`, "while MCP servers, hooks, and credentials stay isolated". On every prepare, T3 rewrites only `<profile>/antigravity-acp/settings.json` and the two skill symlinks.
- The runtime loads hooks from `$GEMINI_HOME/config/hooks.json` and `<cwd>/{.agents,.agent,_agents,_agent}/hooks.json`. A workspace hooks file triggers an ask-user trust callback, and the decision is stored in `antigravity-acp/trusted_workspaces.json`.
- The runtime supports only **PreInvocation, PostInvocation, PreToolUse, PostToolUse and Stop**. SessionStart is not wired. **`injectSteps` is ignored**, with the log warning "Trajectory injection (injectSteps) is not supported by the SDK yet". Stop runs once, at session end (`terminationReason: "session_end"`), and `decision: "continue"` is ignored.
- Its PreInvocation stdin is `{invocationNum (session-wide, from 1), initialNumSteps, conversationId, workspacePaths, transcriptPath, artifactDirectoryPath}`, with no prompt.
- MCP: `$GEMINI_HOME/config/mcp_config.json` is merged with the servers T3 supplies (T3 wins on a name clash).
- Transcript: `<profile>/antigravity-acp/brain/<id>/.system_generated/logs/transcript.jsonl`.

## 4. Function matrix

N = native, F = fallback, X = not possible today. "Today" means origin/main.

| Function | Cursor IDE/CLI | Cursor in T3 | Antigravity App/CLI/IDE | Antigravity in T3 |
|---|---|---|---|---|
| Capture | N, installed: stop/preCompact/sessionEnd/subagentStop (CLI preCompact/sessionEnd unverified) | N, same hooks (log-verified) | N: Stop (`fullyIdle`) + transcript; no end/compact event, so F idle-sweep backstop. Needs a new transcript reader | F: PostInvocation (cadence-gated) + Stop at session end + idle sweep; needs a profile or workspace hooks file (decision 6.1) |
| Session-start recall | N, installed (double-fires with the Claude import) | N (log-verified) | N: SessionStart `injectSteps` | X via hooks (no SessionStart, injectSteps ignored). F: workspace rule file + `khipu_status(prompt=…)` pull over MCP |
| Per-prompt recall | N in shipped code, contradicted by docs (unverified). F: `.cursor/rules/khipu.mdc` | same as IDE | N: PreInvocation `injectSteps`, gated to the first model call of each turn; prompt from transcript or `lastUserInput` | X. F: MCP pull |
| Routing start block | N via sessionStart, but fits only about 18% of starts | N | N via SessionStart | X. F: `khipu_route` over MCP, rule text tells the model to call it |
| Routing at delegation | F: postToolUse `additional_context` on Task/Shell (docs; reported broken Apr 2026); preToolUse if real-run proves it; subagentStart cannot inject, so children get the advisory | same | F: PreInvocation after an `invoke_subagent` result; PreToolUse never; children: advisory | X. F: advisory |
| MCP tools | N, installed | N (T3 loads user MCP) | N: `~/.gemini/config/mcp_config.json` | N if the profile `config/mcp_config.json` is written (decision 6.1) |
| Owner-direction turns | F: beforeSubmitPrompt has `prompt` + `session_id`; needs Cursor in `harness_of`, entrypoints and issuer check | same, entrypoint `t3` | F: PreInvocation + transcript USER_INPUT; issuer unverified | F: same as App, entrypoint `t3` |

## 5. Design

### 5.1 Shared plumbing (one harness identity, one command per hook)

1. **Harness and shape come from the payload, not argv.**
   - Cursor: `cursor_version` in the payload, or `CURSOR_VERSION` in env, checked before every Claude test, because Cursor sets `CLAUDE_PROJECT_DIR`.
   - Antigravity: camelCase `conversationId` plus `workspacePaths` or `artifactDirectoryPath`.
   - Add one small function, `khipu.hook_shape.detect(payload, env) -> (harness, shape)`. Use it in `session_capture.infer_harness`, `owner_turns.harness_of`, `recall_rule.session_start_main`, `recall_prompt.hook_main`/`prompt_recall_main`, `routing_delegation` and the stdlib recall client (`khipu-recall-client.py`, which must stay stdlib-only).
   - `--cursor` stays accepted, for installs not yet repointed.
   - Shapes: `claude` (nested `hookSpecificOutput`), `cursor` (flat `additional_context`, capped at 10,000 characters), `antigravity` (`{"injectSteps": [{"ephemeralMessage": ctx}]}` per prompt, `systemMessage` at start; the exact JSON form is confirmed in slice H, else ephemeral for both).
2. **One command string per hook across packs.** The Cursor pack writes exactly the Claude pack's command (`khipu-recall-hook`, `khipu-prompt-recall`, `khipu-stop-hook`, `khipu-delegation-hook`, no flags). Cursor's own dedupe then drops the imported Claude duplicate. As a backstop, recall hooks keep a per-(harness, session, event) single-flight marker (60 s), so a second run prints `{}`. This matters where the Claude entry differs (a custom `CLAUDE_CONFIG_DIR` is never imported, but old installs are).
3. **New harness name `antigravity`.** One name for all surfaces. The surface (`app`/`cli`/`ide`/`t3`) is an entrypoint read from `transcriptPath`'s `app_data_dir` (`antigravity`, `antigravity-cli`, `antigravity-ide`, `antigravity-acp`) plus a T3 thread lookup. Cursor entrypoints are `ide` (`cursor_version` like `3.x`), `cli` (date-shaped `cursor_version`) and `t3` (`t3.thread_for_session`). Session id: `conversationId` for Antigravity; `session_id` (= conversation id) for Cursor. Add `antigravity` to every harness list in section 2.
4. **Per-harness context budget.** Replace `_HOOK_CONTEXT_BUDGET` with a per-shape budget: Claude 9,800 UTF-16 units; Cursor 9,800 characters (cap 10,000); Antigravity to be measured (start at 9,800). Doctor names the harness when routing cannot fit.

### 5.2 Cursor pack changes (`_cursor_install/_uninstall/_status`)

- Repoint sessionStart to plain `khipu-recall-hook`. Migrate old `--cursor` entries in place; `_is_our_recall` already matches both forms.
- Add `beforeSubmitPrompt` → `khipu-prompt-recall` (timeout 3). It always records the owner's turn. Its `additional_context` either lands or is dropped harmlessly, and slice C measures which.
- Add `postToolUse` with matcher `Task|Shell` → `khipu-delegation-hook` (cursor shape, timeout 3). Add `preToolUse` only if slice C shows preToolUse context reaching the model; otherwise the routing seen-marker already stops a double delivery.
- Keep the stop, preCompact, sessionEnd and subagentStop entries and the MCP entry. Keep `.cursor/rules/khipu.mdc` per project as the pull fallback.
- Status gains `prompt_recall`, `delegation_push` and `third_party_import` (does `~/.claude/settings.json` carry Khipu hooks that Cursor will also run) with a per-function `proof` field (`hermetic`, `real-run <date>`, `unproven`). Verify adds a Cursor-shaped `_probe_prompt_recall`.
- Uninstall removes every Khipu entry in all those events and leaves foreign hooks (`session_start.sh`, `before_submit_prompt_chain.sh`, …) byte-identical.

### 5.3 Antigravity pack (new, `antigravity`)

- Detected when `~/.gemini/config/` exists or an Antigravity app data dir does (`~/.gemini/antigravity*`).
- `~/.gemini/config/hooks.json`: one `"khipu"` key, so uninstall deletes only that key:
  - `SessionStart` → `khipu-recall-hook`
  - `PreInvocation` → `khipu-prompt-recall` (timeout 3)
  - `PostInvocation` → `khipu-stop-hook`, queue-only, cadence-gated
  - `Stop` → `khipu-stop-hook`
  - Never PreToolUse or PostToolUse.
- `~/.gemini/config/mcp_config.json`: `mcpServers.khipu = {command: khipu-mcp}`.
- `~/.gemini/config/rules/khipu.md`: `trigger: always_on`, RULE_MD text, including "call `khipu_route` before delegating". It goes in its own file so it can be removed cleanly, never as an edit to the user's GEMINI.md.
- Optional per project (app folder picker): `<project>/.agents/rules/khipu.md`.
- Per-prompt gating: inject only on the first model call after a new USER_INPUT step. The per-session marker holds the last USER_INPUT `step_index` seen, so it never depends on `invocationNum`, which differs between docs (0-based per turn) and ACP (1-based per session). The prompt text comes from that transcript step; `lastUserInput` is used when present.
- Capture:
  - New `session_capture` reader for Antigravity `transcript.jsonl`: USER_INPUT is a user turn, PLANNER_RESPONSE an assistant turn; tool steps are summarised as one line each, like other readers.
  - The usual cadence (5 turns or 20 min) on PostInvocation and Stop.
  - SessionEnd-equivalent is the existing idle sweep (#121), widened to Antigravity transcripts.
  - Heartbeat `dispatch/antigravity.json` feeds liveness.
- Same backups, `_load_json`/`_write_json`, and uninstall-only-ours rules as the other packs.

### 5.4 T3 provider rules (`t3.provider_hooks` and `health`)

- `provider_hooks` returns a per-function dict (`capture`, `start_block`, `prompt_recall`, `delegation`, `mcp`), each `installed`, `missing` or `unsupported`, plus an overall rollup. `unsupported` means the harness itself cannot do it, so it never counts as a gap the owner can fix.
- `cursor` driver: reads `~/.cursor/hooks.json` and `~/.cursor/mcp.json`, since T3 loads the user layer. The project layer is reported but not required.
- `antigravity` driver: profile = `~/.t3/userdata/providers/antigravity/<sha256(id)>`. Capture and MCP read `<profile>/config/hooks.json` and `mcp_config.json` (or the workspace files, per decision 6.1). `start_block`, `prompt_recall` and `delegation` are always `unsupported` while the runtime ignores `injectSteps`. Record the runtime `version` from `~/.t3/tools/antigravity-acp/<platform>/versions/*/.install-complete.json`, so a new runtime triggers a re-check.
- Health warnings use the truth:
  - Unchanged for fixable gaps: "T3 provider Cursor has no Khipu hooks …"
  - For Antigravity: "T3 runs Antigravity without Khipu's start block (its runtime ignores injected context); routing reaches it through `khipu_route` and the brief."
  - `_t3_child_advisory` always fires for an `antigravity` child.
  - Update the rail test fixture string.
- Only provider ids, drivers and `homePath` are read from T3 settings, as today, and never `apiKey`.

### 5.5 Doctor and app (no terminal steps)

- **Harnesses screen.** The Cursor card and a new Antigravity card show one row per function from section 4 with state and proof ("Memory on each prompt: on, Cursor has not confirmed it reaches the model"). Buttons: Install, Verify, Remove, and "Add to a project…" (folder picker for `.cursor/rules/khipu.mdc` or `.agents/rules/khipu.md`).
  - Restart wording is text only: "Quit and reopen Cursor". For Antigravity: "If Antigravity asks whether to trust this workspace's hooks, choose Trust".
  - The T3 section lists each Cursor and Antigravity provider with the same per-function rows.
  - `HarnessId`, `LABEL`, `WHERE` and `HOW` gain `antigravity`.
- **Doctor.** Capture liveness and the owner-direction block include cursor and antigravity. The routing-fit warning names the harness. The T3 block uses the per-function states.
- UI work follows the Better Design MCP workflow and starts from an approved mock of the two cards (`mocks/main.harness.html` is the analog).

### 5.6 Tests (hermetic: fake HOME, `KHIPU_KEYCHAIN=0`, pytest from `packages/cli`)

- **Detection.** Fixture payloads copied from the shapes above (Cursor SDK 1.0.35 sessionStart, beforeSubmitPrompt, stop, sessionEnd, postToolUse; Antigravity docs and ACP SessionStart, PreInvocation, PostInvocation, Stop) give the right harness, shape, session id and entrypoint. Claude and Codex outputs stay byte-identical to current golden files.
- **Cursor pack.** Covered cases:
  - Install into a fake `~/.cursor` that holds foreign hooks.
  - Re-install is a no-op, and old `--cursor` entries get repointed.
  - Uninstall leaves foreign entries byte-identical.
  - Status keys are correct.
  - A simulated Claude-import merge (same dedupe rule as Cursor's loader) yields one run per event.
- **Antigravity pack.** Covered cases:
  - The hooks.json `"khipu"` key round trip leaves other keys intact.
  - The MCP and rules files are written.
  - No PreToolUse is ever written.
  - Uninstall removes only Khipu's parts.
- **Transcript reader.** A redacted Antigravity transcript fixture produces the expected turns, and the cadence fires as designed.
- **Per-prompt gating.** One injection per USER_INPUT, none on later invocations.
- **T3.** Fake `~/.t3/userdata/settings.json` with cursor and antigravity instances and a fake profile tree gives the expected `provider_hooks` per function, health warnings and child advisory.
- **Owner turns.** A Cursor beforeSubmitPrompt and an Antigravity PreInvocation record a turn with the right harness and entrypoint. Untrusted entrypoints are refused (per decision 6.2).
- **Desktop.** Vitest for both cards and the T3 rows; `tsc`.

What a hermetic test cannot prove (real-harness runs, slices C and H):
- that Cursor's dedupe drops the imported duplicate;
- whether Cursor beforeSubmitPrompt, preToolUse or postToolUse context reaches the model in the IDE, the CLI and T3;
- which events the CLI fires (sessionEnd, preCompact, beforeSubmitPrompt);
- Cursor's real size cap behaviour;
- that Antigravity App, CLI and IDE load `~/.gemini/config/hooks.json`, and which `injectSteps` JSON form the model sees;
- the workspace-hook trust prompt;
- that T3 leaves profile `config/` files alone across runtime updates;
- the issuer process tree for owner turns;
- transcript format drift;
- latency under the real timeouts.

The rig to copy is `tests/acceptance/routing_harness` (opt-in flag, fresh private home, credential-file only, evidence to `--routing-evidence`). The probe is a canary topic seeded in a throwaway Khipu home: a session passes when the model quotes the canary from the push, not from an MCP call.

## 6. Decisions that are Matt's

1. **T3 Antigravity: where Khipu writes.** T3 deliberately isolates hooks and MCP in its private profile. There are three options:
   - (a) Write `config/hooks.json` and `config/mcp_config.json` into `~/.t3/userdata/providers/antigravity/<hash>/`. This works for every repo, but it is T3-managed state, and Khipu's rule so far is "never write T3's settings or database".
   - (b) Per project only: `.agents/hooks.json`, `.agents/mcp_config.json` and `.agents/rules/khipu.md`, added from the app. This is opt-in per repo, triggers Antigravity's workspace-trust prompt, and is not yet verified for MCP under ACP.
   - (c) Ask T3 upstream for a supported "extra MCP/hooks" setting, and ship (b) meanwhile.

   Recommendation: (b) now, plus (c).
2. **Owner-direction trust for Cursor and Antigravity turns.** Should `cursor:ide`, `cursor:cli`, `cursor:t3`, `antigravity:app|cli|ide|t3` be in `DEFAULT_TRUSTED`? Recommendation: record turns from day one, but add them to the trusted list only after slice C/H proves the issuer check for that surface, and then only on Matt's word.
3. **Real-run verification spend.** Slices C and H need a few real sessions: Cursor (shared xAI week, about 1–2% per the 2026-10-20 bench) and Antigravity (Matt's Google sign-in in T3; routing rule 13 says Antigravity is not used unless Matt asks). Both need Matt's go.

**Decided (Matt, 2026-10-27):** scope approved; build A-F now, G waits for an approved mock. 6.1: (b) per-project files added from the app, plus (c) ask T3 upstream. 6.2: record Cursor and Antigravity turns now; trust them only after slice C/H proves the issuer check, and only on Matt's word. 6.3: yes to the real-run sessions for both (recorded in Khipu as a 7-day routing exception).

## 7. Slices (one session each, in order)

Each slice runs the hermetic pytest oracle (and desktop `npm test` plus `tsc` where touched) at most 3 times, then stops and reports. Never touch `~/.config/khipu/live*`, real `~/.cursor`, `~/.gemini` or `~/.t3` in tests.

- **A. Harness identity and shapes.** Inspect `session_capture.infer_harness`, `owner_turns.harness_of`, `recall_rule`, `recall_prompt`, `routing_delegation` and `khipu-recall-client.py` before writing `hook_shape`. Add `antigravity` to the harness lists, add the per-shape budgets, and add the single-flight marker. Acceptance: fixture payloads for all four harnesses give the right harness, shape and session id, and the Claude and Codex golden outputs are unchanged.
- **B. Cursor pack completion.** Inspect `_cursor_install`, `_codex_install` and `_install_delegation_hooks` before editing. Do unified commands, beforeSubmitPrompt, postToolUse delegation, status/verify keys and the migration. Acceptance: a fake-HOME install, re-install and uninstall round trip passes, the dedupe simulation runs each Khipu hook once, and foreign hooks are untouched.
- **C. Cursor real-run proof** (needs decision 6.3). Run the canary in the IDE, `cursor-agent` and T3 Cursor. Record per function what landed, and set `proof` and the app wording from the evidence. Evidence goes to the maintainer's dated jobs folder outside the repo, never a temp folder. If per-prompt context does not land, stop and keep the rule fallback. Acceptance: an evidence file per surface, and status matches it.
- **D. Antigravity capture.** Inspect the existing transcript readers and the idle sweep before writing the Antigravity reader, the PostInvocation/Stop cadence and the heartbeat. Acceptance: the fixture transcript yields the expected episode window, and liveness turns red when the heartbeat is stale.
- **E. Antigravity pack.** Inspect the Grok Build pack (closest analog: a home-scoped config plus a rules block) before writing install, uninstall, status and verify for `~/.gemini/config`, the per-prompt gate and the injectSteps output. Acceptance: a fake-HOME round trip passes, no PreToolUse is ever written, and one injection happens per user turn.
- **F. T3 provider rules.** Inspect `t3.provider_hooks`, `health` and `_t3_child_advisory` before writing the per-function states, the Cursor and Antigravity drivers, the runtime-version record and the warnings. Install per decision 6.1. Acceptance: fake `~/.t3` fixtures give the expected states, warnings and advisory, and no settings value other than id, driver and homePath is read.
- **G. App and doctor.** If there is no approved mock of the Cursor and Antigravity cards, stop and ask. Then build the cards, the T3 rows and the doctor rows with the Better Design workflow. Acceptance: vitest and `tsc` are green, and a rendered screenshot of both cards is checked, with every owner action working from the app.
- **H. Antigravity real-run proof** (needs decisions 6.1 and 6.3). Run the canary in App, CLI and IDE where installed, and in T3 Antigravity. Confirm the injectSteps form, the hooks firing and the trust prompt. Acceptance: an evidence file per surface, and status and wording match it. If hooks don't fire on a surface, mark that surface `unsupported` with the evidence.

## Sources (read 2026-10-27)

- Cursor hooks: https://cursor.com/docs/agent/hooks
- Cursor third-party hooks: https://cursor.com/docs/reference/third-party-hooks
- Cursor forum, beforeSubmitPrompt context request: https://forum.cursor.com/t/add-additional-context-to-beforesubmitprompt-hook-output/157231
- Cursor forum, CLI third-party opt-out: https://forum.cursor.com/t/no-way-to-opt-out-of-third-party-claude-code-codex-config-loading-the-flag-is-hardcoded/166558
- Cursor forum, CLI hook coverage: https://forum.cursor.com/t/hooks-afteragentresponse-afteragentthought-not-firing-in-headless-cli/156220
- Antigravity hooks: https://antigravity.google/docs/hooks?tab=cli
- Antigravity MCP: https://antigravity.google/docs/mcp
- Antigravity rules: https://antigravity.google/docs/rules
- Antigravity forum, hooks not firing: https://discuss.ai.google.dev/t/stop-and-posttooluse-hooks-in-agents-hooks-json-never-fire-antigravity-ide-1-107-0-windows/178288
- claude-mem Antigravity setup: https://docs.claude-mem.ai/antigravity-cli/setup
- Entire CLI Antigravity PR (no SessionStart in agy at the time): https://github.com/entireio/cli/pull/1287
- Binaries on this Mac: Cursor.app 3.26.13 (`workbench.desktop.main.js`), `cursor-agent` 2026.10.22-27dcf5f (`1841.index.js`), T3 Code (Beta) 0.1.2 `app.asar` (`apps/server/dist/binCli-*.mjs`, `@cursor/sdk` 1.0.35), T3-managed `agy_acp_server.par` / `localharness_external` 1.3.0.
