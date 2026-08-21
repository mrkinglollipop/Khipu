---
name: blast-radius
description: What else could this change break — Assess (default) or Prove with a real local oracle.
---

# Blast radius

**Authoritative contract:** `.cursor/skills/blast-radius/SKILL.md` only.

## Where this command lives

Cursor loads slash commands from:

1. **`~/.cursor/commands/`** (global — any opened folder)
2. **`<workspace-root>/.cursor/commands/`** (project root only)

After harness updates:

```bash
bash "/Volumes/Cloud Storage/Claude/.cursor/scripts/sync-harness.sh"
```

## Scope

- Named change / refactor safety; path-census; Assess vs Prove modes.
- **Not:** session audit → `/myauditandfix`; plan audit → `/verify-plan`.

## Bootstrap

Read and follow `.cursor/skills/blast-radius/SKILL.md`. Trailing text names the change. Default **Assess** unless Matt authorizes Prove or asks to verify an already-applied diff.
