---
name: audit
description: >-
  Session audit with mandatory Action summary, Verification ledger, and Plan
  completion. /audit and "audit your work" = one Grok 4.6 agent, loop until green
  (stamps push OK). /myauditandfix = dual-critic session loop. Read-only: assess,
  health-check, coverage audit, findings without fixes. /verify-plan = verify-plan skill.
---

# Audit

**Mode:** read-only by default except **`/audit`** and informal **audit your work** (solo Grok loop + fix) and **`/myauditandfix`** (dual-critic loop + fix). Audit ≠ build (`master.mdc` build gate). Other informal audits (`assess`, `health check`, `coverage audit`) stay assessment-only unless fix is authorized: `and fix`, `fix any issues`, `fix the highs`.

**Shared loop:** `.cursor/skills/audit/references/loop.md` — dual waves for `/myauditandfix` / `/verify-plan`; **Solo audit loop** section for `/audit` / `audit your work`.

## 0. Confirm scope (one pass, then proceed)

State in one short block:
- **Target** — path, subsystem, diff, or **this thread**
- **In scope** / **Out of scope**
- **Depth** — `/audit` and `audit your work` default HIGH/MEDIUM (solo cap 8); `/myauditandfix` HIGH/MEDIUM (dual cap 3); informal read-only default **thorough**
- **Track** — session audits use **`TRACK=session`**

### Session audit + fix — solo (`/audit`, `audit your work`)

Command: `.cursor/commands/audit.md` (when present). Session scope + fix authorized. **One Grok 4.6 agent** per `loop.md` **Solo audit loop**. Stamps push OK on Green:Y. Derive file set from **this thread's tool history**, not `git diff` alone. **Bugbot:** default **off**.

### Session audit + fix — dual (`/myauditandfix`)

Command: `.cursor/commands/myauditandfix.md`. Session scope + fix authorized. Pipeline = `loop.md` dual critics + verifier with `TRACK=session`.

### Plan verify (`/verify-plan`)

Load **`.cursor/skills/verify-plan/SKILL.md`** (not this file's fix allowlist).

### Session file set (solo and dual)

**Default target:** deliverables from **the invoking thread only** — not repo-wide uncommitted diff.

| In scope | Out of scope |
|----------|--------------|
| Paths touched this thread (`Write` / `StrReplace` / `Delete`) | Pre-session uncommitted WIP |
| Session shell side effects | Unrelated workspace areas |
| Load-bearing chat claims from this thread | Whole-system explore (unless session touched many areas) |
| Process rules followed in this thread | Drive-by fixes beyond findings |

### Code / system / hybrid tracks

- **Code / diff:** session-auditor or explore as needed; bugbot opt-in only
- **System (≥2 areas):** parallel Explore/Task — do not serial-grep
- **Hybrid:** explore + optional bugbot on diff (opt-in only)

## 1. Orient (graph-first when not session-local)

`query_graph.py search` + memory topics when claims need prior context. Session-local file audits may skip.

## 2. Investigate

Follow `loop.md`: **solo** (`/audit` / `audit your work`) → Freshness → prepr (code) → one `ROLE=solo_audit` Task → §4 → re-hire until green. **dual** (`/myauditandfix`) → Freshness → prepr → dual critics → confirm or empty-skip → §4 → same-turn fix → always-delta.

### Evidence states (Fable rule 2)

| State | Meaning |
|-------|---------|
| **verified** | Checked this session; cite command, file, log, or URL+date |
| **unverified** | Stated but not checked this session |
| **inferred** | Reasonable from structure; no oracle |

## 3. Prepr (code `/audit` and `/myauditandfix`)

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

### 4.2b Rule compliance (session / `/audit` / `/myauditandfix`)

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

**Solo loop (fix + push OK):** `/audit`, `audit your work` (also `audit our work` / `audit this work`)

**Read-only:** `assess`, `health check`, `review the system`, `coverage audit`, `session audit` (no slash), bare `audit` without “your work”

**Plan verify:** `/verify-plan` → verify-plan skill

**Dual-critic loop (fix + push OK):** `/myauditandfix`, `audit and fix` / `fix any issues` (with dual-audit context)

## Anti-patterns

- Dual critics on `/audit` / `audit your work`
- Solo-audit instead of dual critics + verifier **on `/myauditandfix`**
- Confirm+fix in one Task **on `/myauditandfix`** (allowed on `/audit` only)
- End turn after Phase 1 when fixable HIGH/MEDIUM remain under slash loop
- Green after Phase 2 without post-fix re-audit
- Treat `NEW_HIGH_FROM_FIX: false` as green
- Repo-wide `git diff` as session-audit surface
- Default bugbot on without Matt opt-in
- Skip `loop.md` always-delta / empty-skip / multi-green rules on the dual path

**Post-fix re-audit mandatory** — see `loop.md` (never end on Phase 2 alone).
