---
name: fanout
description: Decompose the ask into independent legs and hire parallel Task teammates in one turn.
---

# Fanout

Decompose → parallel hire. Orchestrator stays hub; teammates do the legs.

## Where this command lives

Cursor loads slash commands from:

1. **`~/.cursor/commands/`** (global — any opened folder; after sync local mode)
2. **`<workspace-root>/.cursor/commands/`** (project root — **Cloud Agents** need this copy committed + pushed)

After harness updates:

```bash
bash "/Volumes/Cloud Storage/Claude/.cursor/scripts/sync-harness.sh"
```

## Scope

- Break the trailing ask (or the whole user ask after this command) into **independent legs**.
- Fire **one Task per leg in the same turn** (parallel). Synthesize returns for Matt.
- Respect `.cursor/rules/orchestration.mdc` hire table, model policy, and caps — no unbounded fan-out, no force-dispatch theater.
- **HARD:** omit Task `model`, or pin an allowlisted slug — never pass the string `inherit`.
- Do **not** invent product requirements across a comprehension fork; ask Matt when taste/risk/scope is ambiguous.

**Not:** `/iterate` (improve loop), `/myauditandfix` / `/myauditandfix-v2` (session audit), `/verify-plan` / `/verify-plan-v2` (plan verify).

## Bootstrap

1. List the independent legs (short labels).
2. Dispatch parallel `Task` teammates per orchestration hire-early routing.
3. Wait for returns; synthesize outcome + evidence; name blockers.

Trailing text = the work to fan out. If empty, use the rest of the user turn.
