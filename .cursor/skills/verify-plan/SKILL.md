---
name: verify-plan
description: >-
  Audit and verify the plan in the invoking thread; loop plan-document fixes until
  green (default cap 3; trailing full → 4). Use for /verify-plan, audit and verify
  your plan, verify the plan. Uses audit/SKILL.md §4 report format. Authorizes
  plan/todo edits only — not app code or harness encode.
---

# Verify plan

**Mode:** read-only Phase 1; Phase 2 authorized only by **`/verify-plan`** (trailing `audit-only` skips Phase 2).

**Not** `/myauditandfix` · **Not** `/oracle-retro`.

**Shared loop:** `.cursor/skills/audit/references/loop.md`. **Report:** `.cursor/skills/audit/SKILL.md` §4.

## 0. Scope

- **Target** — plan artifact(s) in this thread (`.cursor/plans/*.md`, topic `*-plan.md`, todos, approved-build scope)
- **In scope** — plan claims, dependencies, acceptance criteria, contradictions, stale paths, freshness
- **Out of scope** — implementing plan items, app/harness source edits, repo-wide git diff
- **Track** — **`TRACK=plan`** on every dispatch
- **Depth** — per `loop.md` (slash default HIGH/MEDIUM cap 3; `full` / `quick`)

If no plan artifact: say so in Action summary; do not invent a plan.

## 1–2. Pipeline

Follow `loop.md` with `TRACK=plan`: Freshness → dual critics → confirm (or slim confirm when empty) → §4 → same-turn plan-only fix → **always-delta** post-fix.

**Plan lenses:** contradictions, missing acceptance criteria, wrong paths, unverified assumptions as fact, freshness failures, documented≠enforced.

## 3. Report

Action summary → Verification ledger → Plan completion → Findings. Verdict must state plan green Y/N. Surface report **before** plan edits.

## 4. Fix phase (plan documents only)

Edit **only**:
- `.cursor/plans/*.md`
- `/Volumes/Cloud Storage/Memory/conversations/topics/*-plan.md` (when that is plan SSOT)
- Todo list via `TodoWrite` when todos mirror the plan

App/harness SSOT edits → **BLOCKED**. Re-verify via reads + ledger only (no build/test oracles). Hook items needing code → **blocked on Matt** + `approved — build`.

## 5. Green / loop

Per `loop.md`. **Green** when latest confirm has zero HIGH/MEDIUM, Plan completion has no partial/not-started for in-scope items (**blocked on Matt** OK if explicit), load-bearing claims verified or blocked on Matt.

Does **not** authorize `approved — build` for implementation. Does **not** unlock `git push`.

## Anti-patterns

- Implementing plan items under `/verify-plan`
- Using session file-set scope instead of plan artifacts
- Solo-audit; confirm+fix in one Task
- Green without post-fix re-audit when Phase 2 ran
- Skipping `loop.md` always-delta rule

**Post-fix re-audit mandatory** — see `loop.md`.
