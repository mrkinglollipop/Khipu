---
name: verify-plan
description: Audit and verify the plan in this thread; loop plan-document fixes until green (default cap 3; trailing full → 4)
---

# Verify plan

**Authorizes plan-document fix phase** (not app/harness code). Load and follow:

1. **`.cursor/skills/verify-plan/SKILL.md`** — plan scope, plan-only fix allowlist, green meaning
2. **`.cursor/skills/audit/references/loop.md`** — waves, always-delta, host pins, caps
3. **`.cursor/skills/audit/references/identifier_freshness.py`** — mandatory TRACK=plan identifier grep before critics
4. **`.cursor/skills/audit/SKILL.md` §4** — report format

**Track:** `TRACK=plan` on every critic and verifier Task. **Fix allowlist:** `.cursor/plans/*.md`, topic `*-plan.md`, mirroring todos — never app/harness SSOT.

**Depth:** slash default = HIGH/MEDIUM, cap **3**; trailing `full` → thorough + cap **4**; trailing `quick` (Matt only) → cap **2** (still runs identifier freshness). Trailing `audit-only` skips Phase 2. No plan in thread → report and stop.

Does **not** clear session-audit push PENDING and does **not** unlock `git push`.

Orchestrator remains hub — identifier freshness → dual critics + verifier per `loop.md`; do not solo-audit.
