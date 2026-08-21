---
name: audit
description: >-
  Read-only system, codebase, or process audit with mandatory Action summary,
  Verification ledger, and Plan completion sections. Use when Matt says audit,
  /audit, audit your work, session audit, assess, health-check, review the system,
  coverage audit, or asks for findings without fixes. Fix phase only when authorized
  (/myauditandfix, audit and fix, fix any issues). /myauditandfix = session loop per
  commands/myauditandfix.md + references/loop.md. /verify-plan = verify-plan skill.
---

# Audit

**Mode:** read-only by default. Audit ≠ build (`master.mdc` build gate). Deliver the assessment; do not implement fixes unless fix phase is authorized: **`/myauditandfix`**, `and fix`, `fix any issues`, `fix the highs`, or a follow-up with fix intent.

**Shared loop:** `.cursor/skills/audit/references/loop.md` (waves, always-delta, empty-skip, host pins, caps). Slash commands authorize fix + load this skill / verify-plan skill.

## 0. Confirm scope (one pass, then proceed)

State in one short block:
- **Target** — path, subsystem, diff, or **this thread**
- **In scope** / **Out of scope**
- **Depth** — informal `audit` default **thorough**; slash `/myauditandfix` default HIGH/MEDIUM (see `loop.md`)
- **Track** — session audits use **`TRACK=session`**

### Session audit + fix (`/myauditandfix`)

Command: `.cursor/commands/myauditandfix.md`. Session scope + fix authorized. Pipeline = `loop.md` with `TRACK=session`.

### Plan verify (`/verify-plan`)

Load **`.cursor/skills/verify-plan/SKILL.md`** (not this file's fix allowlist).

### Session audit (`audit your work`, `session audit`)

**Default target:** deliverables from **the invoking thread only** — not repo-wide uncommitted diff.

| In scope | Out of scope |
|----------|--------------|
| Paths touched this thread (`Write` / `StrReplace` / `Delete`) | Pre-session uncommitted WIP |
| Session shell side effects | Unrelated workspace areas |
| Load-bearing chat claims from this thread | Whole-system explore (unless session touched many areas) |
| Process rules followed in this thread | Drive-by fixes beyond findings |

**Method:** dual-critic → verifier with **`TRACK=session`** per `loop.md`. Derive file set from **this thread's tool history**, not `git diff` alone. **Bugbot:** default **off**. Skip §1 graph orient when target is only this thread's local files.

### Code / system / hybrid tracks

- **Code / diff:** session-auditor or explore as needed; bugbot opt-in only
- **System (≥2 areas):** parallel Explore/Task — do not serial-grep
- **Hybrid:** explore + optional bugbot on diff (opt-in only)

## 1. Orient (graph-first when not session-local)

`query_graph.py search` + memory topics when claims need prior context. Session-local file audits may skip.

## 2. Investigate

Follow `loop.md`: Freshness → prepr (code myaudit) → dual critics → confirm or empty-skip → §4 → same-turn fix → always-delta.

### Evidence states (Fable rule 2)

| State | Meaning |
|-------|---------|
| **verified** | Checked this session; cite command, file, log, or URL+date |
| **unverified** | Stated but not checked this session |
| **inferred** | Reasonable from structure; no oracle |

## 3. Prepr (code `/myauditandfix`)

See `loop.md` + Night School 465. `/commitprmerge` reuses transcript prepr — never re-runs it.

## 4. Report format (mandatory order)

### 4.1 Action summary (always first)

```markdown
## Action summary

**Verdict:** …
**Do now:** …
**Blocked on you:** …
**Plan:** complete | N/A | incomplete — …
```

### 4.2 Verification ledger (always second)

```markdown
## Verification ledger

| Claim | State | Evidence |
|-------|-------|----------|
| … | verified / unverified / inferred | … |
| Intent alignment — build matches user request primary workflow | verified / **FAILED** | … |
```

### 4.2b Rule compliance (session / `/myauditandfix`)

One row per always-on rule file (conduct, master, orchestration, memory, personality, context-compaction, workspace-context): Relevant? / State / Evidence. Procedural/hook **violated** → HIGH.

### 4.3 Plan completion (always third)

```markdown
## Plan completion

| Item | Status | Evidence |
|------|--------|----------|
```

Or one-line N/A when no plan/todos/approved-build scope.

### 4.4 Findings (after mandatory sections)

| Severity | Location | Finding | Evidence |
|----------|----------|---------|----------|

## 5. After the audit

Stop hook captures on this Mac. Cloud / no hook: `khipu_capture`. Do not pipe `capture_v2.py`. Persist topic page only if Matt asks.

## Triggers

**Audit-only:** `audit`, `/audit`, `audit your work`, `session audit`, `assess`, `health check`, `review the system`, `coverage audit`

**Plan verify:** `/verify-plan` → verify-plan skill

**Audit + fix:** `/myauditandfix`, `audit and fix`, `fix any issues` (with audit context)

## Anti-patterns

- Solo-audit instead of dual critics + verifier
- Confirm+fix in one Task
- End turn after Phase 1 when fixable HIGH/MEDIUM remain under slash loop
- Green after Phase 2 without post-fix re-audit
- Treat `NEW_HIGH_FROM_FIX: false` as green
- Repo-wide `git diff` as `/myauditandfix` surface
- Default bugbot on without Matt opt-in
- Skip `loop.md` always-delta / empty-skip / multi-green rules

**Post-fix re-audit mandatory** — see `loop.md` (never end on Phase 2 alone).
