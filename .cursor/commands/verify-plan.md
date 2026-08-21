---
name: verify-plan
description: Audit and verify the plan in this thread; loop plan-document fixes until green (max 4 rounds)
---

# Verify plan

**Authoritative contract:** `.cursor/skills/verify-plan/SKILL.md` + audit §4 report format (Action summary → Verification ledger → Plan completion → Findings).

Equivalent to repeatedly asking **"audit and verify your plan"** — automated loop, max **4 rounds**, stops early when **green**.

Distinct from **`/myauditandfix`**: plan scope + plan-document fixes only (no session code fixes). Distinct from **`/oracle-retro`**: no harness encode; standard audit report sections.

**Pipeline:** orchestrator **Freshness pass** → dual `session-auditor` (`TRACK=plan`, bug_hunt + claim_bust) → confirm-only `audit-verifier` (`TRACK=plan`) → mandatory §4 report to Matt → fix `audit-verifier` (`TRACK=plan`) when not green. Orchestrator remains hub; does not solo-audit instead of dual critics.

## Scope

- **Default:** plan artifact(s) from **this thread** — `.cursor/plans/*.md`, topic `*-plan.md`, todo list, approved-build scope stated in chat.
- **In scope:** plan claims, dependencies, acceptance criteria, contradictions, stale paths, verification of assumptions, freshness of version/API claims.
- **Not in scope:** implementing plan items, app/harness source edits, repo-wide `git diff`, pre-thread WIP.
- **Trailing text:** path to plan file or subsystem keyword; `audit-only` → no plan edits (report + loop exit only).

If no plan exists in thread, report that and stop — do not fabricate a plan.

## Fast paths (speed — quality-preserving)

- **Zero-findings slim confirm:** when both round-1 critics return zero findings, the confirm verifier is still mandatory — but dispatch it with the slim pack (scope block, plan claims list, critic verdict lines, Freshness notes) and instruct **verdict-only adjudication**: build the ledger from supplied evidence; re-run read oracles only for contested or HIGH items.
- **`quick` depth (Matt-invoked only):** trailing `quick` → critics report **HIGH/MEDIUM only** (skip LOW enumeration), Freshness pass uses free oracles only (no paid web unless a load-bearing version/API claim exists), round cap **2**. Green criteria unchanged (zero HIGH/MEDIUM). Never self-select `quick` absent Matt's words (trailing `quick` or quick-intent phrasing like "sanity check").

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
- **Freshness oracle notes** (orchestrator-collected before critics on **full** re-audit; delta rounds may reuse last notes unless plan paths/claims changed)
- For verifier step 1: **both critic reports** (bug_hunt + claim_bust) — **unless** `DELTA_CHECK=true`
- For verifier step 2 only: confirmed finding list from step 1

**Pack once (speed):** compose the artifact pack **once per round** and paste the identical block into every dispatch that round — do not re-derive it per dispatch. Cap inline oracle log tails at ~40 lines each; do not inline plan file contents critics can read themselves — the plan file set paths are the reference.

**Host dispatch (HARD):** resolve critic + adjudicator from **`.cursor/dispatch-settings.yaml`** for the active host (`cursor` | `grok` | `claude`). Same table as `/myauditandfix`: Cursor critics pin `composer-2.5`; adjudicator pin `cursor-grok-4.6-xhigh` (allow `cursor-grok-4.6-high`); never omit; never Composer; never k3 as adjudicator pin. **Grok/Claude: model omit always** (harness default). Do not hardcode Cursor slugs into non-Cursor sessions.

## Freshness pass (orchestrator — before dual critics on full re-audit)

**Owner:** orchestrator collects notes **before** dual critics; critics may add incidental free checks only; paid web budget is orchestrator-owned.

1. **Always (free):** existence/help oracles for cited paths/commands; graph `search`; memory topics grep; compare claims to current files.
2. **When applicable (paid, ≤5/round, orchestrator budget):** versioned external surfaces → `.cursor/skills/web-search/SKILL.md` / `scripts/_clients` (not Tavily plugin). Cite URL + date in ledger.
3. Put results in artifact pack as **Freshness oracle notes**.

Paid web budget does **not** consume each critic's 3 shell/oracle-run cap.

**Delta freshness reuse:** Freshness pass before dual critics when running **full** re-audit; `DELTA_CHECK=true` rounds may reuse last freshness notes unless plan paths/claims changed.

## Phase 1 — Plan audit (read-only)

Do **not** edit files during this phase.

1. State scope block: target, in scope, out of scope, depth.
2. Run **Freshness pass**; attach notes to artifact pack.
3. **Dual critics (parallel):** dispatch two critics in one turn with **`TRACK=plan`**, roles `bug_hunt` + `claim_bust`, using the **active host profile** from `dispatch-settings.yaml`:
   - `ROLE=bug_hunt` — plan contradictions, missing acceptance criteria, impossible sequencing
   - `ROLE=claim_bust` — shared freshness items + plan-vs-thread intent
4. **Confirm-only verifier:** dispatch adjudicator with `fix_authorized=false`, **`TRACK=plan`**, both critic reports — **unless** `DELTA_CHECK=true` — and Freshness oracle notes (host-profile type/model/readonly). Verifier confirms/rejects/dedupes and returns §4-ready payload.
5. Orchestrator surfaces **mandatory report** to Matt in order (from verifier payload + synthesis):
   - **Action summary** (verdict, do now, blocked on Matt, plan status)
   - **Verification ledger** (every row: verified / unverified / inferred; freshness rows included)
   - **Plan completion** (row per plan/todo item)
   - **Findings** (severity table)

**Two-step rule (HARD):** mandatory §4 report is surfaced to Matt **before any plan edits**. Verifier never confirm+write in a single Task.

**Same-turn continue (HARD):** After the §4 report is surfaced in the orchestrator message, if the audit is **not green** and **not stop-early**, **immediately** dispatch Phase 2 (`fix_authorized=true`) in the **same turn** — do **not** wait for Matt acknowledgment, “continue”, or “go fix”. `/myauditandfix` / `/verify-plan` already authorize the fix phase. Two-step only constrains **order** (report text before fix writes) and forbids confirm+fix in one Task — it is **not** a human checkpoint between phases.

## Phase 2 — Plan fix (authorized by this command)

Skip when green after Phase 1 or trailing `audit-only`.

1. Dispatch adjudicator with `fix_authorized=true`, **`TRACK=plan`**, host-profile fix settings, and the **confirmed finding list** from Phase 1 — fix **only** plan surfaces (see SKILL §4); no app/harness edits.
2. Re-verify each fix via reads + ledger updates only (no build/test oracles) — **this is not a re-audit**.
3. Update Verification ledger and Plan completion rows. Fix-agent `NEW_HIGH_FROM_FIX` / clearance notes select post-fix mode only — they do **not** authorize green or “no more HIGH.”

## Phase 3 — Loop until green (mandatory)

Run sequentially up to **4 rounds** (**2** when Matt invoked `quick`). See SKILL §5 for green/not-green criteria (includes **stop early** when only Blocked-on-Matt HIGH/MEDIUM remain).

**Post-fix re-audit mandatory (HARD):** after **every** Phase 2 that ran, you **must** start the next round's audit (full or delta) **before** any claim of green, “no more HIGH,” “cleared,” or Loop summary — **in the same turn**; do **not** end the turn on fix alone or wait for Matt before re-audit. `NEW_HIGH_FROM_FIX` and whether the prior confirm had any HIGH choose shape only — never skip re-audit / never equals green.

**Post-fix re-audit mode (HARD):** after each Phase 2, choose the next round's audit shape before dispatching:
1. **Full re-audit** when **any** of:
   - the **prior confirm** (full or delta) had **any HIGH** finding — including HIGH the fix claims to have cleared (clearing prior HIGH requires dual critics, not delta); **or**
   - the fix introduced **any new HIGH** (including a previously-cleared HIGH reopened as HIGH), or the fix verifier reports a HIGH regression  
   → Freshness pass + dual critics + confirm-only verifier (host profile).
2. **Confirm-only delta check** only when **both**:
   - the **prior confirm** had **zero HIGH**, **and**
   - the fix introduced **no new HIGH** (`NEW_HIGH_FROM_FIX=false`)  
   → **skip dual critics**. Dispatch one confirm-only adjudicator (`fix_authorized=false`, host-profile confirm + **`DELTA_CHECK=true`**), fix-touched paths, prior confirmed findings + clearance claims. New MEDIUM-only issues do **not** force full re-audit. Delta confirm is still a **real second pass**.
3. If a confirm-only delta check **surfaces a new HIGH**, do **not** Phase 2 yet — **escalate in the same round** to full re-audit (dual critics + confirm) before further fixes. Still counts as one round toward the cap.

1. Round 1: full report before edits (always dual critics + confirm); stop-early if only Matt blockers remain
2. **After every Phase 2:** mandatory next round — **Re-audit (round N/4)** or **Re-audit (round N/4) — delta** (`NEW_HIGH_FROM_FIX` and prior-confirm HIGH status choose shape only). Never end on fix alone. Deltas only; skip unchanged rows; stop-early after confirm
3. Freshness pass before dual critics when running **full** re-audit; delta rounds may reuse last freshness notes unless plan paths/claims changed
4. Phase 2 between rounds when not green **and** not stop-early; then mandatory re-audit again
5. End with **Loop summary** — rounds used (e.g. 2/4), full vs delta each round, stop-early if applicable, plan files touched, blockers on Matt. Use the exact strings `**Green:** Y` / `**Green:** N` (or `Green: Y` / `Green: N`) — the stop hook's `GREEN_RE` matches only these; on green it stamps OK/WS_OK while leaving shared PENDING in place. Loose phrasing ("plan is green") does not stamp. **Green: Y** only after a confirm/delta-confirm Task when Phase 2 ran.

## Anti-patterns

- Pausing for Matt between the §4 report and Phase 2, or patching the plan before the §4 report exists (Same-turn continue + two-step order above)
- Solo-auditing in orchestrator thread instead of dual critics + verifier, or `Task(explore)` instead of dual critics
- Fixing session deliverables or app code (use `/myauditandfix`)
- Harness encode (use `/oracle-retro`)
- Stopping before green without stop-early or the 4-round cap
- Declaring green / "no more HIGH" after Phase 2 without post-fix confirm or `DELTA_CHECK` (`NEW_HIGH_FROM_FIX: false` is not green and not a re-audit substitute)
- Confirm-only delta outside its two conditions — prior confirm had any HIGH, or the fix introduced a new HIGH / HIGH regression → full re-audit until a confirm returns zero HIGH
- Implicit `approved — build`
- Self-selecting `quick` depth absent Matt's words
