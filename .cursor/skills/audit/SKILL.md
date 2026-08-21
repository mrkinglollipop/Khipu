---
name: audit
description: >-
  Read-only system, codebase, or process audit with mandatory Action summary,
  Verification ledger, and Plan completion sections on every audit-intent trigger.
  Use when Matt says audit, /audit, audit your work, session audit, assess,
  health-check, review the system, coverage audit, or asks for findings without
  fixes. Does not authorize file changes unless fix phase is authorized (/myauditandfix,
  /verify-plan, audit and fix, fix any issues). /myauditandfix = session audit + loop until green (max 4 rounds) per
  .cursor/commands/myauditandfix.md. /verify-plan = plan audit + loop plan-doc fixes until green (max 4 rounds) per
  .cursor/commands/verify-plan.md.
---

# Audit

**Mode:** read-only by default. Audit ≠ build (`master.mdc` build gate). Deliver the assessment; do not implement fixes unless fix phase is authorized: **`/myauditandfix`**, `and fix`, `fix any issues`, `fix the highs`, or a follow-up message with fix intent.

## 0. Confirm scope (one pass, then proceed)

State in one short block:
- **Target** — what is being audited (path, subsystem, diff, workflow)
- **In scope** — what you will examine
- **Out of scope** — adjacent systems you will not pull in (keep system boundaries strict — e.g. Frozen Tell ≠ Frost Line)
- **Depth** — quick scan vs thorough (default: **thorough** when Matt says "audit"; **quick** when he says "sanity check" / "spot check")

If scope is ambiguous, pick the narrowest reasonable interpretation, state it, proceed.

### Session audit + fix (`/myauditandfix`)

**Slash command:** `.cursor/commands/myauditandfix.md`. **Always** session-audit scope (invoking thread only) + **fix phase authorized** in the same turn.

Equivalent to: audit your work in this thread → mandatory report → surgical fixes to findings only. Trailing text narrows or adds target; does not skip fix unless explicitly audit-only.

### Plan verify loop (`/verify-plan`)

**Slash command:** `.cursor/commands/verify-plan.md`. Load **`.cursor/skills/verify-plan/SKILL.md`**. Plan artifact(s) in invoking thread + **plan-document fix phase authorized** (not app code). **Pipeline:** Freshness pass → dual `session-auditor` (`TRACK=plan`) → confirm-only `audit-verifier` → mandatory §4 report → fix verifier when not green. Loop audit→plan fix until green (max 4 rounds; confirm-only delta when prior confirm had zero HIGH and fix introduces no new HIGH). Equivalent to repeatedly asking "audit and verify your plan."

### Session audit (`audit your work`, `session audit`, `audit what you just did`)

**Default target:** deliverables from **the invoking thread only** — not repo-wide uncommitted diff.

| In scope | Out of scope |
|----------|--------------|
| Paths touched in this thread (`Write` / `StrReplace` / `Delete`; orchestrator + subagents) | Pre-session uncommitted WIP (paths not touched in this thread) |
| Session shell side effects (e.g. `khipu capture` / stop-hook → `episodes.jsonl`) | Unrelated workspace areas |
| Load-bearing chat claims **from this thread** (Fable rule 2) | Whole-system explore (unless session touched many areas) |
| Process rules followed in this thread (dispatch, build gate, read-only) | Drive-by fixes beyond audit findings |
| Plan / todo / approved-build scope completion (§4 Plan completion) | Items outside this thread's stated scope |

**Method:** adversarial dual-critic → verifier pipeline with **`TRACK=session`** on every dispatch (see **Session track pipeline** below). Derive session file set from **this thread's tool history**, not `git diff` alone. **Bugbot:** default **off**; opt in only when Matt explicitly asks. Security-sensitive work uses `security-review`, not an implicit BugBot dispatch. **Skip §1 graph orient** when the target is only this thread's local files.


### Session track pipeline (dual-critic → verifier)

Professional practice: independent parallel critics with different lenses, then a separate moderator/verifier before any write. Same-context self-review is the failure mode.

**Artifact pack** (orchestrator → critics + verifier every round):
- Scope block (target, in/out, depth)
- **`TRACK=session`**
- Session file set (paths from this thread's Write/StrReplace/Delete + named shell side effects)
- Load-bearing chat claims list (verbatim or quoted)
- Plan/todo ids in thread (or "none")
- Prior round finding deltas (rounds 2–4)
- When `DELTA_CHECK=true`: paths touched by last Phase 2 fix, prior confirmed findings + clearance claims. **Code sessions:** one **`ROLE=bug_hunt`** critic report (after prepr prepare re-run; prepared bundle attached); **skip `claim_bust`**. **Docs-only / no-code:** skip all critics.
- Oracle log tails already collected this round (if any)
- **Freshness oracle notes** (orchestrator-collected when Freshness pass ran; use "none" otherwise)
- **Prepr prepare bundle** — code sessions: exit code, fingerprint, `scope_paths`, `files`, `audit_input`, `prompt` (inline when ≤~200 lines; otherwise metadata + `/tmp/prepr_bundle_<fingerprint>.json` path per Pack-once rule) from repo-local `prepr_audit.py --worktree --prepare --json --path …`; docs-only: `prepr: N/A`. Exit 2/3 or a missing run = failed/unverified (blocks Green:Y). Never a Green substitute; adversarial review is owned by critics.
- Verifier step 1: **both critic reports** on **full** rounds — on **`DELTA_CHECK=true` code rounds**, the **delta `bug_hunt` report** + prepr prepare bundle (no claim_bust); on **docs-only delta**, critic reports omitted
- Verifier step 2 only: confirmed finding list from step 1

**Freshness pass (orchestrator — before critics when applicable):** run when session claims cite paths, versions, APIs, or external "current" facts.
1. **Always (free):** existence/help oracles for cited session paths/commands.
2. **When claims need prior context (free):** graph `search` + memory topics grep.
3. **When applicable (paid, ≤5/round, orchestrator budget):** versioned external surfaces → `.cursor/skills/web-search/SKILL.md` / `scripts/_clients`. Cite URL + date in ledger.

**Prepr prepare oracle (orchestrator — `/myauditandfix` code sessions):** run repo-local `prepr_audit.py --worktree --prepare --json --path <each session code path>` before round-1 critics and again after each Phase 2 before full/delta confirm. `--prepare` requires `--worktree` and ≥1 `--path` (session scope only, no pre-session WIP); rejects `--post` / `--pr` / `--waive`. Full rounds: attach bundle to both critics (`bug_hunt` adversarial lens; `claim_bust` bundle freshness). **Delta code rounds:** after prepare re-run, one read-only `bug_hunt` critic with the bundle; skip `claim_bust`. Docs-only → N/A. Prepr does not replace critics/verifier; exit 2/3 or a missing run blocks Green:Y. Green needs a completed prepare for the **current fingerprint** plus zero confirmed open HIGH/MEDIUM from critics/verifier.

Paid web budget is separate from each critic's 3 oracle-run cap.

**Pack once, reference big blobs (speed):** compose the artifact pack **once per round**; paste the identical block into every dispatch that round. Cap inline oracle log tails at ~40 lines; do not inline file contents critics can read from the file set. Prepr bundles with `audit_input`/`prompt` over ~200 lines → write full JSON to `/tmp/prepr_bundle_<fingerprint>.json`, attach metadata (exit code, fingerprint, `scope_paths`, `files`) + that path inline.

**Fast paths (speed — quality-preserving):**
- **No auditable surface (`/myauditandfix`):** empty session file set + no load-bearing claims → skip critics/verifier; minimal §4 report; Loop summary `Green: N/A — no auditable surface` (never `Green: Y` — pipeline gate requires critic→confirm evidence).
- **Zero-findings slim confirm:** both critics return zero findings → confirm verifier still mandatory, but slim pack + verdict-only adjudication (oracle re-runs only for contested or HIGH items).
- **`quick` depth (Matt-invoked only):** trailing `quick` or Matt quick-intent phrasing per §0 ("sanity check" / "spot check") → critics report HIGH/MEDIUM only; Freshness free oracles only; round cap 2 (overrides the default 4). Green criteria unchanged. The agent never self-selects `quick` absent Matt's words.

**Per round (read-only phase):**
1. Orchestrator states scope block; runs Freshness pass when applicable; attaches Freshness oracle notes. Runs prepr prepare on code sessions (or marks N/A). (On `DELTA_CHECK=true` rounds: may reuse last freshness notes unless session paths/claims changed; **re-run prepr prepare** after Phase 2 for code sessions.)
2. **Full confirm rounds:** **Parallel dual critics:** `ROLE=bug_hunt` and `ROLE=claim_bust`, both with **`TRACK=session`**, using the **active host profile** from `.cursor/dispatch-settings.yaml` (type, model policy, readonly/capability_mode).
   - `claim_bust` includes shared freshness items: stale/false paths, outdated API/version claims, chat "verified" without evidence, scope creep vs thread intent, freshness failures, and prepr bundle freshness (fingerprint, scope_paths) when present.
   - **`DELTA_CHECK=true` code rounds (prior confirm zero HIGH, no new HIGH from fix):** after prepr prepare re-run, dispatch **one** read-only `session-auditor` with **`ROLE=bug_hunt`**, **`TRACK=session`**, and the prepared bundle; **skip `claim_bust`**. Docs-only delta may skip all critics.
3. **Confirm-only verifier:** adjudicator with `fix_authorized=false`, **`TRACK=session`**, Freshness oracle notes, and prepr prepare bundle — confirm/reject/dedupe; emit §4-ready payload. Host profile for type/model/readonly (see dispatch-settings).
   - **Full confirm:** pass both critic reports.
   - **`DELTA_CHECK=true` code:** pass delta **`bug_hunt` report** + prepr prepare bundle + fix-touched paths + prior confirmed findings + clearance claims (see `/myauditandfix` Phase 3).
   - **`DELTA_CHECK=true` docs-only:** pass fix-touched paths + prior confirmed findings + clearance claims; no critic reports.
4. **Optional bugbot:** only when Matt opted in — fold into Findings; does not replace §4.1–§4.3.
5. Orchestrator surfaces mandatory report (Action summary → Verification ledger → Plan completion → Findings) to Matt.

**Plain session audit** (`audit your work`, session audit): stop after step 5 — no fix writes.

**`/myauditandfix`:** after step 5, if not green, dispatch adjudicator with `fix_authorized=true`, **`TRACK=session`**, host-profile fix settings, and confirmed finding list only (see §3b). **Green** requires freshness-verified ledger gate — every load-bearing claim **verified** or **blocked on Matt**; silent **unverified** fails green — **and** (code sessions) a completed latest prepr prepare bundle for the current fingerprint.

**Two-step rule (HARD):** §4 report surfaced to Matt **before any fix writes**. Verifier never confirm+write in a single Task.

**Same-turn continue (HARD):** After the §4 report is surfaced in the orchestrator message, if the audit is **not green** and **not stop-early**, **immediately** dispatch Phase 2 (`fix_authorized=true`) in the **same turn** — do **not** wait for Matt acknowledgment, “continue”, or “go fix”. `/myauditandfix` / `/verify-plan` already authorize the fix phase. Two-step only constrains **order** (report text before fix writes) and forbids confirm+fix in one Task — it is **not** a human checkpoint between phases.

**Host dispatch (HARD):** resolve critic + adjudicator from **`.cursor/dispatch-settings.yaml`**. Cursor: pin critic `composer-2.5`, adjudicator pin `cursor-grok-4.6-xhigh` (allow `cursor-grok-4.6-high`); never omit; never Composer; never k3 as adjudicator pin. **Grok/Claude: model omit always** (harness default — do not pin Cursor or composer slugs). Escape hatch = host profile escape type + read agent `.md` with the **same host** model policy. Native types preferred on Cursor when available.

**Stop early — Blocked on Matt:** when every remaining HIGH/MEDIUM is Blocked on Matt and none are fixable in-session, exit the `/myauditandfix` / `/verify-plan` loop immediately (`Green: N`); do not burn further rounds. See command Phase 3.

Orchestrator remains hub — does not solo-audit instead of dual critics.

## 1. Orient (graph-first)

Skip this section for **session audit** when the target is only this thread's local files.

Before broad reads:
1. `PYTHONPATH=".python_libs" python3 "/Volumes/Cloud Storage/Graph/query_graph.py" search "<target>"`
2. Grep `/Volumes/Cloud Storage/Memory/conversations/topics/` for prior audits on the same target
3. Check for existing audit artifacts (`*audit*.md`, `AUDIT.md`, topic pages)

Carry forward prior findings; note what changed since last audit.

## 2. Choose audit track

| Track | When | Method |
|-------|------|--------|
| **System** | Subsystem, hooks, memory, orchestration, multi-area | 2–4 parallel `Task(subagent_type: "explore", readonly: true)` — one scope each |
| **Code** | Branch, PR, uncommitted diff, specific module | Session-auditor / explore + oracle as needed; **`bugbot` opt-in only** when Matt asks or uses `/review-bugbot` (+ `security-review` if security-sensitive or Matt asks) |
| **Hybrid** | Large system with code surface | Parallel explore for architecture; **`bugbot` opt-in only** on diff when Matt asks or uses `/review-bugbot` |
| **Session** | `/myauditandfix`, `audit your work`, `session audit`, audit this thread | Dual `session-auditor` (`TRACK=session`, bug_hunt + claim_bust) → confirm-only `audit-verifier` → §4 report; `/myauditandfix` adds fix `audit-verifier` after report; **`bugbot` opt-in only** |

When ≥2 independent areas: prefer parallel Explore/Task via product Auto. Orchestrator synthesizes; subagents do not write files.

## 3. Investigate

### Evidence states (Fable rule 2 — every row, no exceptions)

| State | Meaning |
|-------|---------|
| **verified** | Checked this session; cite command, file read, or log tail |
| **unverified** | Stated in chat or docs but not checked this session |
| **inferred** | Reasonable from structure/docs; no oracle run |

Forbidden vocabulary: "should work", "looks good", "probably fine" without a state label.

### Source of truth (session build audits)

- **First user build request** in the thread (not orchestrator restatement, not subagent prompt)
- **Deliverable Contract** if one was required

### Verification discipline

- **Verification ledger** — one row per auditable claim: chat assertions, build/merge/test claims, rule-compliance claims, and positive "sound" items. Not limited to bugs.
- **Plan completion** — when the thread has a plan file, todo list, or `approved — build` scope: one row per item; surface **not started** and **partial** in Action summary.
- **Findings** — same three states in the Evidence column.
- **Documented ≠ enforced** — explicitly hunt for prose-only guarantees (rules, prompts, docs without hook/code backstop). Session pipeline + harness writes are backstopped by `audit_marker.py` pipeline gate (GREEN / Push OK requires dual-critic→confirm **Task `tool_use`** evidence in transcript when `/myauditandfix` active — assistant prose alone does not count) and `contract_shell_write.py` (Shell writes into `.cursor-plugin/` / `.cursor/{agents,skills,rules,commands,hooks,scripts}/` require contract or escape).
- **No fix loops on audit-only** — audit-only turn ends at the report unless fix phase is authorized (`/myauditandfix`, `/verify-plan`, `fix` / `and fix` / `fix any issues` / `approved — build`). **`/myauditandfix`** runs the session fix loop per `.cursor/commands/myauditandfix.md` Phase 3 (up to 4 rounds until green; confirm-only delta when prior confirm had zero HIGH and no new HIGH from fix). **`/verify-plan`** runs the plan fix loop per `.cursor/commands/verify-plan.md` Phase 3 (up to 4 rounds until green; same post-fix mode).

## 3b. Fix phase (when authorized)

When Matt runs **`/myauditandfix`** or requests audit **and** fix in the same message:
1. Complete Phase 1 of the session track pipeline first — mandatory §4 report surfaced to Matt before any writes.
2. Dispatch adjudicator with `fix_authorized=true`, **`TRACK=session`**, host-profile fix settings, and the **confirmed finding list** from confirm-only step — fix **only** those findings; no scope creep, no drive-by refactors, no orchestrator solo-fixes.
3. Re-verify fixed items via oracle/ledger updates only — **this is not a re-audit**. Update Verification ledger and Plan completion rows; note **verified** / **unverified** on each fix. Fix-agent `NEW_HIGH_FROM_FIX` / clearance notes select post-fix mode only — they do **not** mean green.
4. Small reversible edits do not require `approved — build`; new features or multi-file builds still do.
5. Orchestrator reviews verifier diff + runs oracle gate. No LOC force-dispatch.
6. **`/myauditandfix` only:** loop per `.cursor/commands/myauditandfix.md` **Phase 3** until **green** (zero HIGH/MEDIUM on the **latest confirm**, not fix self-report), **stop-early** (only Blocked-on-Matt HIGH/MEDIUM left), or **4 rounds** max, sequential. End with **Loop summary**.

**Post-fix re-audit mandatory (HARD):** after **every** Phase 2 that ran, dispatch the next round's audit (full or delta) **before** green / “no more HIGH” / Loop summary — **in the same turn**; do **not** end the turn on fix alone or wait for Matt before re-audit. `NEW_HIGH_FROM_FIX` and whether the prior confirm had any HIGH choose shape only — never skip re-audit / never equals green.

**Post-fix re-audit mode:** full re-audit when the prior confirm had any HIGH or the fix introduced any new HIGH (or HIGH regression); confirm-only `DELTA_CHECK` only when prior confirm had zero HIGH and `NEW_HIGH_FROM_FIX=false` — **code delta:** one `bug_hunt` critic with prepare bundle after re-run prepare, then confirm; **docs-only delta:** confirm only, skip all critics; if delta surfaces a new HIGH, escalate to full re-audit in the same round. After Phase 2 on code sessions, **re-run prepr prepare** before full or delta confirm. See `/myauditandfix` Phase 3 / `/verify-plan` Phase 3 for dispatch details.

## 4. Report format (Flavor-OFF prose)

**Mandatory on every audit-intent trigger** — session, system, code, hybrid. No compact substitute. Bugbot/security subagent output is folded into Findings; §4.1–§4.3 still required.

### 4.1 Action summary (always first)

≤6 lines. Matt should be able to stop reading here.

```markdown
# Audit: <target>
**Date:** <ISO date> · **Scope:** <one line>

## Action summary
**Verdict:** <one sentence>
**Do now:** 1. … 2. … (or "Nothing blocking — ship")
**Blocked on you:** … (or "None")
**Plan:** complete | N/A | **incomplete** — <N> open (<list ids or one-line summary>)
```

`Do now` = ordered, actionable, scoped to this audit. No drive-by refactors.

### 4.2 Verification ledger (always second)

Every load-bearing claim from chat, docs, and investigation — not only defects.

```markdown
## Verification ledger

| Claim | State | Evidence |
|-------|-------|----------|
| <what was asserted or checked> | verified / unverified / inferred | <command, path, log tail, or "not checked"> |
| Intent alignment — build matches user request primary workflow | verified / **FAILED** | Quote user noun + contract primary workflow vs what was built |
```

Include positive claims ("build passed", "PR merged", "hooks enforce X"). If a TL;DR-style bullet matters, it must appear here with a state.

**Intent alignment severity:** missing contract before first novel write = **HIGH**; contract present but primary workflow absent = **HIGH**; built feature checklist without primary workflow = **HIGH**.

### 4.2b Rule compliance (always-on sample)

After Verification ledger, for `/myauditandfix` and session audits:

```markdown
## Rule compliance (always-on sample)

| Rule file | Relevant this thread? | State | Evidence |
|-----------|----------------------|-------|----------|
| conduct | yes/no | complied / violated / N/A | contract present? done-claims verified? |
| master | yes/no | complied / violated / N/A | build gate? boundary? |
| orchestration | yes/no | complied / violated / N/A | oracle cap? delegation spec? |
| memory | yes/no | complied / violated / N/A | capture cadence? |
| personality | yes/no | complied / violated / N/A | Flavor-OFF surfaces? |
| context-compaction | yes/no | complied / violated / N/A | compact point? |
| workspace-context | yes/no | complied / violated / N/A | graph orient? |
```

One row per always-on file. **violated** on procedural/hook tier = **HIGH** finding.

### 4.3 Plan completion (always third)

When no plan, todos, or approved-build scope exists in the thread: **`Plan: N/A`** in Action summary and this one-line block:

```markdown
## Plan completion

No plan, todo list, or approved-build scope in this thread.
```

When a plan exists, one row per item:

```markdown
## Plan completion

| Item | Status | Evidence |
|------|--------|----------|
| <plan step or todo id> | done (verified) / done (unverified) / partial / not started / N/A | <diff, oracle, or gap> |
```

Statuses:
- **done (verified)** — evidence in Verification ledger
- **done (unverified)** — claimed done; not re-checked this session
- **partial** — started but incomplete vs plan spec
- **not started** — no evidence of work
- **N/A** — out of scope for this audit

Any **not started** or **partial** row must appear in Action summary under **Plan: incomplete**.

### 4.4 Detail (after mandatory sections)

```markdown
## Findings

| Severity | Location | Finding | Evidence |
|----------|----------|---------|----------|
| HIGH | path or component | What is wrong / risky | verified / unverified / inferred: … |

Severity: **HIGH** (blocks production / data loss / security) · **MEDIUM** (fragile, drift, false confidence) · **LOW** (cosmetic, docs, nice-to-have)

## Themes
- Cross-cutting patterns (e.g. "documented != enforced")

## Prior audit delta
- New since last audit · unchanged · regressed
```

Omit empty Findings/Themes sections only when truly none. Do **not** omit §4.1–§4.3.

## 5. After the audit

- Offer to capture: if findings are durable, the Stop hook writes on this Mac (`khipu capture`). Cloud / no hook: `khipu_capture` with `scope: audit` (or ask Matt first for trivial scans). Do not pipe `capture_v2.py`.
- Optional: write report to `/Volumes/Cloud Storage/Memory/conversations/topics/<slug>-audit.md` **only if Matt asks to persist**

## Triggers (non-exhaustive)

**Audit-only:** `audit`, `/audit`, `audit your work`, `session audit`, `audit what you just did`, `assess`, `health check`, `review the system`, `what's broken`, `coverage audit`, `audit rules`, `audit hooks`, `audit memory`

**Plan verify loop:** `/verify-plan`, `audit and verify your plan`, `verify the plan`

**Audit + fix:** `/myauditandfix`, `audit and fix`, `fix any issues` (with audit context), `audit your work` + fix language in same message

## Anti-patterns

- Ending the turn after Phase 1 §4 report when fixable HIGH/MEDIUM remain (waiting for Matt to authorize Phase 2)
- Treating “report before fix writes” as a pause-for-ack checkpoint
- Treating audit as implicit approval to fix
- Mixing unrelated systems in one audit scope
- Findings or ledger rows without **verified** / **unverified** / **inferred**
- Skipping Action summary, Verification ledger, or Plan completion (including session audits)
- Auditing repo-wide `git diff` or pre-session WIP under `/myauditandfix` or session-audit triggers
- Stopping after one fix round when `/myauditandfix` was invoked and audit is not green (max 4 rounds per command)
- Declaring **Green: Y**, “no more HIGH,” or “all clear” immediately after Phase 2 **without** a post-fix full or `DELTA_CHECK` confirm Task
- Treating `NEW_HIGH_FROM_FIX: false` as green or as a substitute for re-audit
- Using confirm-only delta when the prior confirm had any HIGH (must full re-audit until a confirm returns zero HIGH)
- Using confirm-only delta when the fix introduced a new HIGH / HIGH regression
- Skipping delta **`bug_hunt`** critic on code-session `DELTA_CHECK` rounds (docs-only delta may skip all critics)
- Burying plan gaps only in Findings instead of Plan completion + Action summary
- Substituting bugbot-only output for the mandatory report sections
- Solo-auditing in orchestrator thread instead of dual critics + verifier
- Confirm+fix in a single `audit-verifier` Task dispatch
- Defaulting bugbot on any audit track (session, code, hybrid) without Matt opt-in — use `/review-bugbot` when he asks
- Skipping parallel explore on multi-area system audits (serial read loops)
