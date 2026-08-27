---
name: verify-plan
description: >-
  Audit and verify the plan in the invoking thread; loop plan-document fixes until
  green (default cap 3; trailing full → 4). Use for /verify-plan (dual),
  /verify-plan-v2 (one Grok), audit and verify your plan, verify the plan.
  Uses audit/SKILL.md §4 report format. Authorizes plan/todo edits only —
  not app code or harness encode.
---

# Verify plan

**Mode:** read-only Phase 1; Phase 2 authorized by **`/verify-plan`** or **`/verify-plan-v2`** (trailing `audit-only` skips Phase 2).

**Not** `/myauditandfix` · **Not** `/oracle-retro`.

**Shared loop:** `.cursor/skills/audit/references/loop.md`. **Identifier grep:** `.cursor/skills/audit/references/identifier_freshness.py`. **Report:** `.cursor/skills/audit/SKILL.md` §4.

## 0. Scope

- **Target** — plan artifact(s) in this thread (`.cursor/plans/*.md`, topic `*-plan.md`, todos, approved-build scope)
- **In scope** — plan claims, dependencies, acceptance criteria, contradictions, stale paths, freshness
- **Out of scope** — implementing plan items, app/harness source edits, repo-wide git diff
- **Track** — **`TRACK=plan`** on every dispatch
- **Depth** — per `loop.md` (dual slash default HIGH/MEDIUM cap 3; `/verify-plan-v2` solo cap 8; `full` / `quick`)

If no plan artifact: say so in Action summary; do not invent a plan.

## 1–2. Pipeline

Follow `loop.md` with `TRACK=plan`. **`/verify-plan`:** identifier freshness → dual critics → confirm → §4 → same-turn plan-only fix → **always-delta**. **`/verify-plan-v2`:** identifier freshness → one `ROLE=solo_audit` Task → §4 → re-hire until green (Solo plan loop).

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

**Verification ledger (§5):** one row per identifier `claim_id` from identifier freshness notes (plus graph+memory freshness rows). `ZERO_HITS` on a load-bearing wire/path symbol, or a recant slice vs a plan sentence asserting the symbol is live → **unverified** (blocks green unless explicitly blocked on Matt). Silent unverified still fails green.

Does **not** authorize `approved — build` for implementation. Does **not** unlock `git push`.

## Anti-patterns

- Implementing plan items under `/verify-plan` or `/verify-plan-v2`
- Using session file-set scope instead of plan artifacts
- Solo-audit **on `/verify-plan`** (full dual path); confirm+fix in one Task **on `/verify-plan`**
- Dual critics **on `/verify-plan-v2`**
- Green without post-fix re-audit when Phase 2 ran
- Skipping `loop.md` always-delta rule

**Post-fix re-audit mandatory** — see `loop.md`.
