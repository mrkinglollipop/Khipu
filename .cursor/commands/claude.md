---
name: claude
description: "On-demand Claude Code via Matt's subscription — review, opinion, or codegen (explicit invoke only)"
---

# Claude Code — on-demand subscription lane

**Authorizes a single Claude Code invocation** when Matt explicitly invokes `/claude` or asks you to use Claude / claude sub for a task.

This command does **not** change default orchestration. Do **not** call Claude Code automatically for codegen, review, or anything else unless Matt triggers this lane in-thread.

## Triggers (any one)

- `/claude <task>` (trailing text is the task)
- Matt says: "use claude", "claude sub", "run through claude", "ask claude to …"

## Modes (pick from trailing text)

| Matt wants… | `permission_mode` | Notes |
|---|---|---|
| Review, opinion, architecture read, "what do you think" | `plan` | Read-only — `--allowed-tools Read,Grep,Glob,Bash(git *)` |
| Implement, fix, refactor, edit files | `acceptEdits` | Default when task clearly mutates code |
| Autonomous build/test loops without prompts | `yolo` | Only when Matt says `--yolo`, "skip permissions", or equivalent |

When ambiguous between `plan` and `acceptEdits`, prefer **`plan`** and ask Matt if edits are intended.

## Model + effort (HARD defaults + overrides)

**Always** set `model: claude-opus-4-8` and `effort: high` unless Matt overrides in trailing text.

| Field | Default | Override example |
|---|---|---|
| `model` | `claude-opus-4-8` | `--model sonnet`, `--model haiku`, `--model claude-fable-5` |
| `effort` | `high` | `--effort medium`, `--effort max` |

**Effort levels:** `low`, `medium`, `high`, `xhigh`, `max`

### Override parsing (orchestrator)

Parse trailing text **before** building the dispatch spec:

```
/claude --effort max review hooks/finish_guard.py
/claude --model sonnet --effort low quick take on this error
/claude use sonnet: fix the typo in foo.py   → acceptEdits + sonnet, effort stays high
/claude --new-session plan architecture for X   → fresh Claude session (no --continue)
```

- Strip `--model`, `--effort`, `--yolo`, `--new-session` from the task text before `prompt`.
- Natural-language overrides: "use sonnet", "low effort", "with max effort".
- `--yolo` → `permission_mode: yolo`
- `--new-session` / "fresh claude session" → `new_session: true`

## Invocation (HARD) — prefer `claude_dispatch.py`

Run from workspace root. Use `app-repo-scout` first if the repo is ambiguous.

Build a JSON spec and pipe to dispatch (subscription auth is enforced inside the script).

**Dispatch script path:** always `<CURSOR_ROOT>/.cursor/scripts/claude_dispatch.py` (harness workspace root — **not** copied to `Code/*` app repos). The JSON `workspace` field is the **target repo** Claude `cd`s into.

```bash
CURSOR_ROOT="/Volumes/Cloud Storage/Claude"
cd "<repo-root>" && python3 "${CURSOR_ROOT}/.cursor/scripts/claude_dispatch.py" <<'EOF'
{
  "workspace": "<repo-root>",
  "prompt": "<self-contained task>",
  "permission_mode": "plan",
  "model": "claude-opus-4-8",
  "effort": "high",
  "thread_summary": "<optional: what happened in this Cursor chat>",
  "diff": "<optional: git diff for reviews>",
  "files": ["<optional absolute paths to inline>"],
  "add_dirs": ["<optional extra roots, e.g. CURSOR root>"],
  "worktree": "<optional: acceptEdits/yolo isolated branch>",
  "new_session": false
}
EOF
```

**Dispatch handles:**
- `env -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL` (subscription only)
- Context pack: `thread_summary`, `diff`, `files` (Claude does not see this chat)
- `--continue` per workspace (`.cursor/state/claude-sessions.json`) unless `new_session: true`
- Harness `--append-system-prompt` (oracle cap, no API Keys/)
- Tool scoping for `plan` mode

- Shell tool: `required_permissions: ["all"]`; set `block_until_ms` to match task (`120000`–`600000`).
- **`timeout_s`** in spec (default 600) for long reviews/codegen.

### Fallback (raw CLI — only if dispatch unavailable)

```bash
cd "<repo-root>" && env -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL claude -p "<self-contained prompt>" \
  --model claude-opus-4-8 --effort high \
  --permission-mode <plan|acceptEdits> --output-format text
```

## After Claude returns

**Report line (HARD):** Dispatch prints `Claude: <model> / <effort> / <permission-mode>` on stderr. **Repeat that line** as the first line to Matt.

Example: `Claude: opus-4-8 / high / plan`

**Review / opinion (`plan`):** Return Claude's answer — synthesize if long; cite file paths Claude referenced. No oracle unless Matt asked for verification.

**Codegen / edits (`acceptEdits` or yolo):**

1. `git diff` / `git status` — read what actually changed.
2. Run the repo oracle (pytest, xcodebuild, tsc, `build_graph.py`, etc.).
3. Report pass/fail with log tail. Hard cap: 3 oracle runs per change.

## Out of scope

- Replacing `code-worker`, explore, bugbot, or inline work by default
- Calling Claude without an explicit trigger in this thread
- Sending `API Keys/` contents or PII in the prompt

## Where this command lives

SSOT: `.cursor-plugin/commands/claude.md` + `.cursor/scripts/claude_dispatch.py`. Sync:

```bash
bash "/Volumes/Cloud Storage/Claude/.cursor/scripts/sync-harness.sh" --local
```

Smoke: `bash .cursor/scripts/smoke_claude_command.sh` (set `SMOKE_CLAUDE_LIVE=1` for live probe).
