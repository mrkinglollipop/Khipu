# Grok Build pack: Khipu follows Aegis's home, and stock Grok Build gets its own integration

Written 2026-10-13 for the day Aegis splits its home from stock Grok Build (Aegis scope
XSEP-P5-HOME-SPLIT, approved by Matt 2026-10-13 for execution the same day). This page is the scope; a
build session reads it first. Nothing here runs before the Aegis split tool has passed review and the
Aegis orchestrator names the window.

## Status

| Phase | What | State |
|---|---|---|
| 0 | Khipu's Aegis capture state follows Aegis's real home instead of `~/.grok` | open; lands in the split window |
| 1 | A `grok_build` harness: pack for stock Grok Build's own `~/.grok`, capture labelled `grok_build` | open; after phase 0, once stock Grok Build has its own home |
| 2 | Recall for Grok Build on the one channel its host honours (`PreToolUse` `additionalContext`) | open; needs Matt's go; only possible once the homes are separate |

One PR per phase, merged alone. Phase 0 and phase 1 can land the same day because phase 1 only touches a
home that does not exist until the split.

## The situation, in one paragraph

Since the 2026-10-03 home move, `~/.grok` has been a symlink to `~/.aegis`: Aegis's Settings → Storage
override names `~/.aegis` as its home, and the link kept every stored absolute path resolving. Stock xAI
Grok Build (`~/.grok/bin/grok`, 1.0.52) resolves its home as `~/.grok`, so both products read one
`config.toml`, one `auth.json`, one sessions tree. Khipu's Aegis pack (`[mcp_servers.khipu]` plus
`khipu-aegis-capture` on Stop, PreCompact and SessionEnd with `env = { KHIPU_HARNESS = "aegis" }`) therefore
runs inside stock Grok Build too, and every Grok Build session is captured as harness `aegis`. The split
makes `~/.grok` a real, initially empty directory for stock Grok Build (own `grok login`, empty sessions,
`~/.grok-dispatch` retired) and leaves `~/.aegis` where it is. After it, the Aegis pack's hooks still run
(they live in `~/.aegis/config.toml`), but every Khipu path that spells `~/.grok` points at the wrong place.

## Source facts (verified against main `1cc9a7d`; paths under `packages/cli/khipu/`)

- `session_capture.khipu_home()` (`:118`): `KHIPU_CAPTURE_HOME`, else `KHIPU_AEGIS_HOME`, else
  `~/.grok/khipu`. Everything native-capture derives from it: `state_dir`, `queue_dir`, the dispatch
  files, `logs/session-capture.log`, `logs/prompt-recall.log`, and the recall socket
  (`recall_daemon.socket_path()`, today `~/.grok/khipu/recall.sock`). The queue drainer
  (`com.khipu.queue-drain`, launchd, every 300 s) and the recall daemon (`com.khipu.recall-daemon`) read
  the same function with no env set, so they use the default.
- `integrations.AEGIS_TOML` (`:1061`) is `~/.grok/config.toml`; `_aegis_detected()` (`:1265`) is
  `~/.grok` is a directory. After the split both name stock Grok Build's home, not Aegis's.
- Aegis's real home is in `~/Library/Application Support/com.matthewschwartz.aegis/agent-home` (one
  absolute path; absent means `~/.grok`), with `GROK_HOME` outranking it at launch. Aegis's own code
  (`gui/src-tauri/src/storage.rs`, `resolve_agent_home`) is the authority for that order.
- `bin/khipu-aegis-capture` refuses under `GROK_HOOK_NAME`/`GROK_HOOK_EVENT` unless `KHIPU_HARNESS=aegis`,
  then runs `session_capture --harness aegis`; the other hook scripts refuse under those variables
  regardless ([[aegis-native-only]] memory note, 2026-08-18). Stock Grok Build sets the same variables.
- Harness name lists that a new harness must join: `session_capture.HARNESSES` (`:82`),
  `integrations.HARNESSES` (`:81`), the `cli.py` choices at `:4532` and `:4579`, the liveness report
  (`doctor` capture_liveness), the dispatch files under `khipu_home()/dispatch/<harness>.json`, and the
  desktop app's health rows.
- Grok Build 1.0.52 hook facts (its embedded docs, read 2026-10-12): hooks come from `[[hooks.<Event>]]`
  tables in `~/.grok/config.toml` (same schema as hook JSON files, `env` supported); `SessionStart` and
  `Notification` read only a top-level `systemMessage`; an allowing `UserPromptSubmit` hook's stdout and
  `additionalContext` are discarded; `PreToolUse`, `PostToolUse` and `Stop` hooks can send
  `additionalContext` to the model; MCP tools sit behind `search_tool`/`use_tool`; server `instructions`
  reach the model; `[memory] enabled = true` turns on Grok's own Markdown memory. Headless runs with
  `--permission-mode dontAsk` cancel every MCP call; verification runs use `--always-approve` or the
  default mode.
- Stock Grok Build's config today carries Aegis-only keys it reports as unrecognized (`[aegis]`,
  `[claude_compat]`, `[memory.backend]`, `[memory.khipu]`); after the split its fresh `config.toml` has
  none of them.

## Decisions

1. **Khipu does not hardcode `.aegis`.** Phase 0 resolves the Aegis home the way Aegis does: `GROK_HOME`
   when the hook was launched with it, else the override file's path, else `~/.grok`. One function,
   `aegis_home()`, used by `khipu_home()`'s default, `AEGIS_TOML`, `_aegis_detected()` and the recall
   socket. `KHIPU_CAPTURE_HOME` / `KHIPU_AEGIS_HOME` keep overriding it. The installer also writes
   `KHIPU_AEGIS_HOME = "<aegis home>/khipu"` into the managed hook block's `env` so a hook needs no
   lookup, and `khipu jobs install` writes the same variable into the drainer's and the daemon's plists.
2. **Stock Grok Build is a harness of its own, `grok_build`,** with its own pack in its own `config.toml`
   (`[mcp_servers.khipu]` plus a `khipu-grok-build-capture` hook on Stop, PreCompact and SessionEnd marked
   `KHIPU_HARNESS = "grok_build"`), captured under that label, with its own dispatch file and liveness
   row. The hook script is the Aegis script with the mark changed; the refusal guard stays: it runs only
   with its own mark, so an Aegis session can never run it and a Grok Build session can never run the
   Aegis one.
3. **No `[memory]` table in stock Grok Build's config.** `[memory] backend = "khipu"` is an Aegis-fork
   key; in stock Grok Build it only switches on a rival memory. The pack writes the MCP entry and the
   hooks, nothing else.
4. **Recall for Grok Build is phase 2 and needs Matt's go.** The only channel is `PreToolUse`
   `additionalContext`; it would fire on every tool call, so it needs a once-per-session gate and a
   budget. Not designed here.
5. **The Aegis pack is untouched by phase 1.** The [[aegis-native-only]] rule stands: Aegis gets its own
   pack natively and nothing Claude-shaped; the Grok Build pack is a third native pack, not a compat
   import.

## Phase 0: one build session, in the split window

Read this page, then inspect `session_capture.khipu_home`, `recall_daemon.socket_path`,
`integrations._aegis_*`, `jobs.py`'s plist writer and `tests/test_integrations*.py` before writing.

1. `aegis_home()` as decided above; `khipu_home()`, `AEGIS_TOML`, `_aegis_detected()` and the socket path
   use it. Tests: override file present → that path; `GROK_HOME` set → wins; neither → `~/.grok`.
2. Installer writes `KHIPU_AEGIS_HOME` into the managed block's `env`; `jobs install` writes it into the
   two plists. Tests on temp homes only.
3. Gate: hermetic suite at baseline, ruff at baseline, then on the real machine after the split:
   `khipu integrations verify aegis` green (`native_ran`, `compat_refused`), one real Aegis turn captured
   (drain log line under `<aegis home>/khipu/logs`), `khipu recall status` answering on the new socket.

Stop conditions: if the override file is missing on the real machine in the window, stop; the split did
not happen as described. If any Khipu path still resolves to `~/.grok` after the change with the override
present, stop and report rather than adding an exception.

## Phase 1: one build session, after phase 0

1. `grok_build` joins every harness list above; `khipu-grok-build-capture` script; installer, status,
   verify and uninstall for the new harness (`integrations install grok_build`), writing only
   `[mcp_servers.khipu]` and the three hook tables into stock Grok Build's `~/.grok/config.toml`.
2. Gate: suite and ruff at baseline; `khipu integrations verify grok_build` green; one real
   `grok -p` turn (default permission mode) captured with `harness=grok_build`; `grok inspect` shows the
   three hooks and no load errors; `grok mcp doctor` shows `khipu` healthy.

Stop conditions: if `~/.grok` is still a symlink, stop; if `grok inspect` reports any hook load error
from the pack, stop and show it.
