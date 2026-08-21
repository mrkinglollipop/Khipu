---
name: why
description: Why was X designed this way — internal archaeology (git/gh/Notion/graph/memory). Not open-web research.
---

# Why

**Authoritative contract:** `.cursor/skills/why/SKILL.md` only.

## Where this command lives

Cursor loads slash commands from:

1. **`~/.cursor/commands/`** (global — any opened folder)
2. **`<workspace-root>/.cursor/commands/`** (project root only)

After harness updates:

```bash
bash "/Volumes/Cloud Storage/Claude/.cursor/scripts/sync-harness.sh"
```

## Scope

- Design rationale / tradeoffs for **this workspace’s** code and decisions.
- **Not:** industry/library/market “why” without a code anchor → `research` / `web-search`; runtime mechanics → `/how`.

## Bootstrap

Read and follow `.cursor/skills/why/SKILL.md`. Trailing text after `/why` is the question. Do not call open-web tools from this skill.
