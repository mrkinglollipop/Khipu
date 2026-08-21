---
name: oracle-retro
description: End-of-session verify/oracle harness retro — audit friction, encode cures to runbooks/agents/repo rules, mandatory Closure Summary
---

# Oracle retro

**Authoritative contract:** `.cursor/skills/oracle-retro/SKILL.md` only — **do not** substitute `audit/SKILL.md` §4 (Action summary / Plan completion). This command **authorizes harness encode Phase 2** for verify/oracle gaps only.

Distinct from `/myauditandfix`: verify slice only, single implement pass (+ one optional remediation), default outcome = no encode warranted, ends with **Closure Summary**.

## Where this command lives

Cursor loads slash commands from:

1. **`~/.cursor/commands/`** (global — any opened folder)
2. **`<workspace-root>/.cursor/commands/`** (project root only)

Nested parents do not inherit. After harness updates:

```bash
bash "/Volumes/Cloud Storage/Claude/.cursor/scripts/sync-harness.sh"
```

## Scope

- **Default:** verify/oracle behavior **only in this thread** — build/test/visual claims, shell commands, dispatches, verify friction.
- **Not in scope:** pre-session WIP, repo-wide `git diff`, features, refactors, non-verify audit items (use `/myauditandfix`).
- **Trailing text:** `audit-only` → Phase 1 + Phase 3, no edits; repo name → narrow `$REPO_ROOT`.

## Bootstrap

```bash
source "/Volumes/Cloud Storage/Claude/.cursor/scripts/cursor_root.sh"
export_cursor_root
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
```

If `CURSOR_ROOT` empty → STOP in Closure Summary with remediation.

## Phase 1 — Verify audit (read-only)

Do **not** edit files during this phase.

1. Scope block
2. Verify ledger (verified / unverified / model-oracle per claim)
3. Friction log
4. Findings + encode decision table (encode / defer / reject)

## Phase 2 — Encode (authorized by this command)

Skip when no `encode` rows or trailing `audit-only`.

1. Edit **one primary target** per finding (see SKILL routing matrix)
2. Clutter gates: dedup, delete stale lines, ≤5 lines per rule/agent
3. Prefer product Auto / Task for large hook edits; orchestrator runs oracle. No LOC force-dispatch.
4. Re-run changed oracle (3-run cap); smoke if `$CURSOR_ROOT/.cursor/**` touched
5. `sync-harness.sh` if plugin/commands/rules changed
6. `/commitprmerge` per affected git root (hook owns capture; do not pipe `capture_v2`)

**Write authorization:** harness SSOT only — see SKILL §2. Includes `$REPO_ROOT/.cursor/**` and `~/Projects/*` harness when thread touched repo.

## Phase 3 — Closure Summary (mandatory last message)

Emit **`## Oracle retro — closure summary`** with all four subsections per SKILL §9. **No prose after it.**

If removable in-scope gap listed → one fix attempt, re-emit Closure Summary once.

## Anti-patterns

- Patching before Phase 1 encode table exists
- Using audit skill report format instead of Closure Summary
- Inventing harness work when no evidence gate fired
- Stopping without Closure Summary
