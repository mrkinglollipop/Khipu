---
name: how
description: How does X work — architecture walkthrough (optional critique). Authoritative contract in how skill.
---

# How

**Authoritative contract:** `.cursor/skills/how/SKILL.md` only.

## Where this command lives

Cursor loads slash commands from:

1. **`~/.cursor/commands/`** (global — any opened folder)
2. **`<workspace-root>/.cursor/commands/`** (project root only)

After harness updates:

```bash
bash "/Volumes/Cloud Storage/Claude/.cursor/scripts/sync-harness.sh"
```

## Scope

- Walkthrough / placement / “how does X work” questions.
- Optional critique when Matt asks for architectural issues.
- **Not:** bare file locate without walkthrough → `explore`; design motivation → `/why`; open-web → `research`.

## Bootstrap

Read and follow `.cursor/skills/how/SKILL.md`. Trailing text after `/how` is the question.
