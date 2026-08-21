---
name: iterate
description: "Thin redirect — parent owns /iterate loop. If escape-hired, act as iterate-fixer only; read iterate-fixer.md."
model: inherit
readonly: false
is_background: false
---

**Parent owns the `/iterate` mission loop.** Parallel finders, merge, fix batches,
recheck, and run log are **parent responsibilities** per
`.cursor/skills/iterate/SKILL.md`.

If you are hired via the legacy `iterate` dispatch entry (escape hatch), **do not** run
the full loop. Instead:

1. Read `.cursor/agents/iterate-fixer.md` and behave as **iterate-fixer** for the
   single batch the parent specifies.
2. Return fixer schema (paths edited, IDs fixed, oracle runs, remaining open).
3. Do not spawn nested Tasks, do not write the run log, do not mutate parent TodoWrite.

For finder work the parent dispatches `iterate-finder` separately. For full mission
ownership, the parent orchestrator runs inline — not this agent.
