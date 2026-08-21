---
name: verify-plan
description: >-
  Audit and verify the plan in the invoking thread; loop plan-document fixes until
  green (max 4 rounds). Use for /verify-plan, audit and verify your plan, verify
  the plan. Uses audit/SKILL.md §4 report format. Authorizes plan/todo edits only —
  not app code or harness encode.
---

# Verify plan

**Mode:** read-only Phase 1; Phase 2 authorized only by **`/verify-plan`** (or trailing `audit-only` skips Phase 2).

**Not** `/myauditandfix` (session deliverables + code fixes) · **Not** `/oracle-retro` (harness encode).

**Pipeline:** orchestrator Freshness pass → dual `session-auditor` (`TRACK=plan`) → confirm-only `audit-verifier` → mandatory §4 report → fix `audit-verifier` when not green. Orchestrator does not solo-audit.

## 0. Confirm scope (one pass, then proceed)

State in one short block:
- **Target** — plan artifact(s) in this thread (`.cursor/plans/*.md`, topic `*-plan.md`, todo list, approved-build scope block in chat)
- **In scope** — plan claims, dependencies, acceptance criteria, verification rows, contradictions, stale paths, freshness of version/API claims
- **Out of scope** — implementing plan items, app/harness source edits, repo-wide git diff, pre-thread WIP
- **Depth** — thorough (default for `/verify-plan`); trailing **`quick`** (Matt-invoked only) → critics report HIGH/MEDIUM only, Freshness free oracles only, round cap 2; green criteria unchanged
- **Track** — **`TRACK=plan`** on every critic and verifier dispatch

If no plan artifact exists: say so in Action summary; offer to draft one or narrow trailing text to a path. Do not invent a plan.

**Trailing text:** names a plan path or subsystem; `audit-only` → Phase 1 + Phase 3 loop exit only (no plan edits).

## Artifact pack (orchestrator → critics + verifier)

Every dispatch for a round must include:
- Scope block (target, in/out, depth)
- **`TRACK=plan`**
- Plan file set (`.cursor/plans/*.md`, topic `*-plan.md`, thread plan text, todo ids)
- Load-bearing plan claims list (verbatim or quoted)
- Plan/todo ids in thread (or "none")
- Prior round finding deltas (rounds 2–4)
- When `DELTA_CHECK=true`: paths touched by last Phase 2 plan fix, prior confirmed findings + clearance claims (critic reports optional)
- Oracle log tails already collected this round (if any)
- **Freshness oracle notes** (orchestrator-collected before critics on **full** re-audit; delta rounds may reuse last notes unless plan paths/claims changed — use "none" only if Freshness never ran)
- For verifier step 1: **both critic reports** (bug_hunt + claim_bust) — **unless** `DELTA_CHECK=true`
- For verifier step 2 only: confirmed finding list from step 1

**Pack once (speed):** compose the artifact pack **once per round**; paste the identical block into every dispatch that round. Cap inline oracle log tails at ~40 lines; do not inline plan file contents critics can read from the plan file set.

**Host dispatch (HARD):** resolve from **`.cursor/dispatch-settings.yaml`**. Cursor critics pin `composer-2.5`; adjudicator pin `cursor-grok-4.6-xhigh` (allow `cursor-grok-4.6-high`); never omit; **never Composer**; never k3 as adjudicator pin. **Grok/Claude: model omit always**. Escape hatch uses host profile + agent `.md`.

## 1. Orient + Freshness pass (graph-first — full re-audit)

Before dual critics on **full** re-audit (round 1 and any escalate), orchestrator runs **Freshness pass** and attaches **Freshness oracle notes** to the artifact pack. **`DELTA_CHECK=true` rounds** may reuse last freshness notes unless plan paths/claims changed.

**Always (free):**
1. `PYTHONPATH=".python_libs" python3 "/Volumes/Cloud Storage/Graph/query_graph.py" search "<plan subject>"`
2. Grep `/Volumes/Cloud Storage/Memory/conversations/topics/` for prior plans/audits on the same target
3. Read the plan file(s) named in thread or trailing text
4. Existence/help oracles for cited paths/commands; compare claims to current files

**When applicable (paid, ≤5/round, orchestrator budget — separate from critic 3-oracle cap):**
- Versioned APIs, deps, SDK/docs, or external facts treated as current → `.cursor/skills/web-search/SKILL.md` / `scripts/_clients`. Cite URL + date in ledger.

Carry forward prior audit deltas when a plan was verified before in-thread.

## 2. Investigate (dual critics — not solo explore)

Dispatch dual critics in parallel with **`TRACK=plan`** using host profile from `dispatch-settings.yaml` — do **not** substitute solo explore.

### Evidence states (Fable rule 2)

| State | Meaning |
|-------|---------|
| **verified** | Checked this session; cite command, file read, log tail, or web URL+date |
| **unverified** | Stated in plan or chat but not checked this session |
| **inferred** | Reasonable from structure/docs; no oracle run |

Forbidden: "should work", "looks good", "probably fine" without a state label.

### Plan verification discipline

- **Verification ledger** — one row per load-bearing plan claim (paths exist, commands work, dependencies shipped, prior decisions still true, freshness of version/API claims)
- **Plan completion** — one row per plan step / todo id: done (verified) / done (unverified) / partial / not started / N/A / blocked on Matt
- **Findings** — plan-quality defects: wrong paths, contradictions, missing acceptance criteria, unverified assumptions marked as fact, stale references, scope creep vs thread intent, freshness failures
- **Documented ≠ enforced** — hunt for plan items that assume rules/hooks enforce behavior without evidence

Critics run shell/read oracles within their 3-run cap. Paid web is orchestrator-owned in Freshness pass.

### Confirm-only verifier

After critics return (or on `DELTA_CHECK=true` without critics), dispatch adjudicator with `fix_authorized=false`, host-profile confirm settings, **`TRACK=plan`**, Freshness oracle notes, and **both critic reports** — **unless** `DELTA_CHECK=true` (then fix-touched paths + prior findings + clearance claims). Verifier dedupes, builds ledger (including freshness rows), and returns §4-ready payload. When both critics return **zero findings**, dispatch the confirm with a slim pack and verdict-only adjudication — oracle re-runs only for contested or HIGH items.

## 3. Report format

Use **`.cursor/skills/audit/SKILL.md` §4** in order: Action summary → Verification ledger → Plan completion → Findings.

Action summary **Verdict** must state plan green Y/N. **Do now** lists plan fixes (not implementation) when not green.

Orchestrator surfaces mandatory report **before any plan edits**.

**Same-turn continue (HARD):** After the §4 report is surfaced in the orchestrator message, if the audit is **not green** and **not stop-early**, **immediately** dispatch Phase 2 (`fix_authorized=true`) in the **same turn** — do **not** wait for Matt acknowledgment, “continue”, or “go fix”. `/myauditandfix` / `/verify-plan` already authorize the fix phase. Two-step only constrains **order** (report text before fix writes) and forbids confirm+fix in one Task — it is **not** a human checkpoint between phases.

## 4. Fix phase (Phase 2 — plan documents only)

When **`/verify-plan`** invoked and audit is **not green** (and not `audit-only`):

1. Complete mandatory report first (§3).
2. Dispatch adjudicator with `fix_authorized=true`, **`TRACK=plan`**, host-profile fix settings, and confirmed finding list only.
3. Edit **only** plan surfaces:
   - `.cursor/plans/*.md`
   - `/Volumes/Cloud Storage/Memory/conversations/topics/*-plan.md` (when that is the plan SSOT for this thread)
   - Todo list via `TodoWrite` when todos mirror the plan
4. Fixes: correct paths/claims, add missing verification rows, resolve contradictions, mark **blocked on Matt** where evidence cannot be obtained, add acceptance criteria — **no scope creep into new features**
5. Do **not** edit app source or harness SSOT; re-verify via reads + ledger updates only (no build/test oracles)
6. Re-verify each plan fix; update ledger and Plan completion rows

Hook/script plan items that require code → list as **blocked on Matt** + `approved — build`; do not implement under `/verify-plan`.

## 5. Loop until green (mandatory for `/verify-plan`)

Run **sequentially** — do not start the next round until the current round's Phase 2 is done **when Phase 2 ran** (or skipped when already green / stop-early).

**Green** when **all** of:
- Findings: **zero** HIGH and **zero** MEDIUM rows — as of the **latest confirm** (full or `DELTA_CHECK`), **not** the fix verifier's self-report
- Plan completion: no **partial** or **not started** rows for in-scope plan items (**blocked on Matt** is OK if blocker is explicit in Action summary)
- Every load-bearing plan claim in Verification ledger is **verified** or explicitly **blocked on Matt** (not silently **unverified**) — **freshness-verified ledger gate**
- Action summary **Do now** is "Nothing blocking — plan green" (or equivalent)

**Not green** when any **fixable-in-session** HIGH/MEDIUM plan finding remains, any in-scope item is partial/not started, unverified claims are treated as verified, or load-bearing freshness claims lack verification or explicit Matt blocker.

**Stop early — Blocked on Matt (HARD):** after confirm (any round), if every remaining HIGH/MEDIUM finding is explicitly **Blocked on Matt** / requires Matt authorization **and** there are **zero** fixable-in-session HIGH/MEDIUM findings — **exit immediately**. Do not start Phase 2 or further rounds. Loop summary `**Green:** N` with blockers listed (not Y).

**Round cap:** **4 rounds** total (**2** when Matt invoked `quick`). After the cap, stop even if not green; report remaining findings and **Blocked on you** items. Stop-early may exit sooner.

**Post-fix re-audit mandatory (HARD):** after **every** Phase 2 that ran, dispatch the next round's audit (full or delta) **before** green / “no more HIGH” / Loop summary — **in the same turn**; do **not** end the turn on fix alone or wait for Matt before re-audit. `NEW_HIGH_FROM_FIX` and whether the prior confirm had any HIGH choose shape only — never skip re-audit / never equals green.

**Post-fix re-audit mode (HARD):** after each Phase 2, choose the next round's audit shape:
1. **Full re-audit** when **any** of:
   - the **prior confirm** (full or delta) had **any HIGH** finding — including HIGH the fix claims to have cleared (clearing prior HIGH requires dual critics, not delta); **or**
   - the fix introduced **any new HIGH** (including a previously-cleared HIGH reopened as HIGH), or the fix verifier reports a HIGH regression  
   → Freshness pass + dual critics + confirm-only verifier.
2. **Confirm-only delta check** only when **both**:
   - the **prior confirm** had **zero HIGH**, **and**
   - the fix introduced **no new HIGH** (`NEW_HIGH_FROM_FIX=false`)  
   → **skip dual critics**. One confirm-only adjudicator (`fix_authorized=false`, host-profile confirm + **`DELTA_CHECK=true`**), fix-touched paths, prior findings + clearance claims. New MEDIUM-only issues do **not** force full re-audit. Delta is still a **real second pass**.
3. If a confirm-only delta check **surfaces a new HIGH**, escalate in the **same round** to full re-audit before Phase 2. Still one round toward the cap.

**Per round:**
1. **Round 1:** Freshness pass → dual critics + confirm verifier → full mandatory report (§3) before any plan edits; apply stop-early
2. **After Phase 2:** always open rounds 2–4 via post-fix re-audit mode (never end on fix alone). Emit **Re-audit (round N/4)** or **Re-audit (round N/4) — delta:** Findings / ledger / Plan completion deltas. Skip unchanged rows. Apply stop-early after confirm (or after escalate-confirm). Freshness pass required before **full** re-audit; delta rounds may reuse last notes unless plan paths/claims changed.
3. If not green, not stop-early, and round `< cap` (4; 2 under `quick`): Phase 2 (fix verifier, `TRACK=plan`) on plan documents only, then **mandatory** next re-audit
4. If green or stop-early **after a confirm/delta confirm**, or round `= cap`: exit loop

**End state:** **Loop summary** — rounds used (e.g. 3/4), full vs delta each round, stop-early if applicable, plan files changed, what was verified vs still blocked on Matt. Use the exact strings `**Green:** Y` / `**Green:** N` (or `Green: Y` / `Green: N`) — the stop hook's `GREEN_RE` matches only these; on green it stamps OK/WS_OK while leaving shared PENDING in place, and loose phrasing does not stamp. **Green: Y** requires a post-fix confirm/delta-confirm Task when Phase 2 ran.

## 6. After green

- Does **not** authorize `approved — build` — Matt still says that explicitly for implementation
- Durable plan decisions: Stop hook writes on this Mac. Cloud / no hook: `khipu_capture`. Do not pipe `capture_v2.py`.

## Triggers

`/verify-plan`, `audit and verify your plan`, `verify the plan`, `verify plan`, `plan audit loop`

## Anti-patterns

- Ending the turn after Phase 1 §4 report when fixable HIGH/MEDIUM remain (waiting for Matt to authorize Phase 2)
- Treating “report before fix writes” as a pause-for-ack checkpoint
- Implementing plan items under `/verify-plan`
- Using `/myauditandfix` scope (session file set) instead of plan artifact scope
- Solo-auditing or solo `Task(explore)` instead of dual critics + verifier
- Stopping after one audit pass when not green (max 4 rounds per command)
- Declaring green / “no more HIGH” after Phase 2 without post-fix confirm or `DELTA_CHECK`
- Treating `NEW_HIGH_FROM_FIX: false` as green or as a substitute for re-audit
- Using confirm-only delta when the prior confirm had any HIGH (must full re-audit until a confirm returns zero HIGH)
- Using confirm-only delta when the fix introduced a new HIGH / HIGH regression
- Editing app/harness code to "make plan true"
- Skipping Action summary, Verification ledger, or Plan completion
- Substituting oracle-retro Closure Summary for audit §4 format
- Skipping Freshness pass on **full** re-audit, or treating delta-round reuse as a skip when plan paths/claims changed
