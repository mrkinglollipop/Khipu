---
name: verify-plan-v2
description: Compact one-Grok plan-doc verify+fix until green; does NOT stamp push (cap 8)
---

# Compact verify plan (one Grok)

**Authorizes plan-document fix phase** (not app/harness code). Load and follow:

1. **`.cursor/skills/verify-plan/SKILL.md`** — plan scope, plan-only fix allowlist, green meaning
2. **`.cursor/skills/audit/references/loop.md`** — **Solo plan loop** (one agent; does not stamp push)
3. **`.cursor/skills/audit/references/identifier_freshness.py`** — mandatory TRACK=plan identifier grep before the solo Task
4. **`.cursor/skills/audit/SKILL.md` §4** — report format

**Track:** `TRACK=plan` on the solo Task. **Fix allowlist:** `.cursor/plans/*.md`, topic `*-plan.md`, mirroring todos — never app/harness SSOT.

**Depth:** slash default = HIGH/MEDIUM, cap **8**; trailing `full` → thorough + cap **8**; trailing `quick` (Matt only) → cap **4** (still runs identifier freshness). Trailing `audit-only` skips Phase 2. No plan in thread → report and stop.

Does **not** unlock `git push`.

Orchestrator remains hub — identifier freshness → one Grok agent per `loop.md` Solo plan loop; do not hire dual critics or a separate confirm Task.
