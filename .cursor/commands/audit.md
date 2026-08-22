---
name: audit
description: Session audit (this thread) with one Grok agent; loop audit→fix until green (default cap 8; trailing full / quick / audit-only)
---

# Session audit (solo)

**Authorizes fix phase** for work in **this thread only**. Load and follow:

1. **`.cursor/skills/audit/SKILL.md`** — session scope, §4 report, prepr, green meaning
2. **`.cursor/skills/audit/references/loop.md`** — **Solo audit loop** (one agent; stamps push OK)

**Track:** `TRACK=session` on the solo Task. **Fix allowlist:** session file set only (paths touched this thread).

**Depth:** slash default = HIGH/MEDIUM, cap **8**; trailing `full` → thorough + cap **8**; trailing `quick` (Matt only) → cap **4**. Trailing `audit-only` skips Phase 2 writes.

**Bugbot:** default **off**; opt in only when Matt asks.

Orchestrator remains hub — one Grok agent per `loop.md` Solo audit loop; do not hire dual critics or a separate confirm Task.
