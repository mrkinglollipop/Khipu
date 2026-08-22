# Audit loop (shared)

Shared by `/myauditandfix` (`TRACK=session`) and `/verify-plan` (`TRACK=plan`). Skills own scope, fix allowlists, and §4 meaning; this file owns waves, green, delta, packs, and host dispatch.

**Solo Grok loop** (`/audit`, informal `audit your work`): see **Solo audit loop** below — one agent, stamps push OK, not dual critics.

## Host dispatch (HARD)

Resolve critic + adjudicator from **`.cursor/dispatch-settings.yaml`** for the active host (`cursor` | `grok` | `claude`).

| Host | Critics | Confirm | Fix |
|------|---------|---------|-----|
| **cursor** | `session-auditor` + pin `composer-2.5` + `readonly: true` (escape: `generalPurpose`/`explore` + same pin) | `audit-verifier` + pin **`cursor-grok-4.6-high`** (allow `xhigh`); never omit; never Composer; never k3 | pin **`cursor-grok-4.6-xhigh`** (allow `high`) |
| **grok** / **claude** | **model omit always** | **model omit always** | **model omit always** |

Every critic/verifier Task prompt must include `ROLE=` / `TRACK=` / `fix_authorized=` **in the prompt text** (hook keys off Task `tool_use` input). Do not bury markers only in metadata/description.

## Depth and round cap

| Invocation | Depth | Cap |
|------------|-------|-----|
| Slash default (`/myauditandfix`, `/verify-plan`) | HIGH/MEDIUM only (skip LOW enumeration) | **3** |
| Trailing `full` | thorough (include LOW) | **4** |
| Trailing `quick` (Matt-invoked only) | HIGH/MEDIUM only; Freshness free oracles only | **2** |
| Informal `audit your work` / slash `/audit` | HIGH/MEDIUM (see Solo audit loop) | **8** (solo, not dual) |

Never self-select `quick` absent Matt's words.

## Wave shape (HARD)

1. **Freshness pass** (orchestrator, when applicable) before full-round critics.
   - **`TRACK=plan` (`/verify-plan`):** **must** run `identifier_freshness.py` before critics — including Matt `quick` (free local grep; not paid web; not inside critic 3-oracle cap). Invoke:
     `PYTHONPATH=".python_libs" python3 .cursor-plugin/skills/audit/references/identifier_freshness.py --plan <plan.md> --root <repo> [--root <repo>...] --out /tmp/plan_freshness_<round>.md`
     (After `sync-harness.sh --repos`, workspace path `.cursor/skills/audit/references/identifier_freshness.py` matches plugin SSOT.)
   - **`TRACK=session`:** graph + memory freshness when claims need prior context (unchanged).
   - **Pack notes field:** path to notes markdown, or inline `NO_IDENTIFIERS` when the script wrote that — **never** `"none"` on `/verify-plan` after this ships.
   - **Missing script run blocks Green:Y** on `/verify-plan` except when notes contain `NO_IDENTIFIERS`.
   - **Always-delta plan track:** re-run the script if Phase 2 edited the plan; attach new notes even when critics are skipped.
   - **Delta confirm AC (`TRACK=plan`):** re-read `/tmp/plan_freshness_<round>.md`; rebuild Verification ledger from current `claim_id`s; set `ESCALATE_FULL_REAUDIT=true` if a previously **verified** identifier is now `ZERO_HITS` or a recant slice contradicts a plan sentence asserting a live hook. Missing notes file on plan delta **blocks Green:Y**.
2. **Prepr prepare** (code `/myauditandfix` only) — Night School 465: repo-local `prepr_audit.py --worktree --prepare --json --path <each session code path>` before round-1 critics and again after each Phase 2 before confirm/delta. Docs-only → `prepr: N/A`. Exit 2/3 or missing run blocks Green:Y. Never a Green substitute.
3. **Round 1:** parallel dual critics (`ROLE=bug_hunt` + `ROLE=claim_bust`) with the skill's TRACK.
4. **Empty-confirm skip (session track / push gate):** when **both** matching critic **subagent** transcripts contain the **substituted** sentinel `No findings for ROLE=<role>, TRACK=session.` (values filled — not the `<role>`/`<track>` placeholder), and the transcript-shape spike/hook path is live, orchestrator may synthesize §4 + ledger and stamp green **without** a confirm Task — **first-green / single-green only**. Keep the **multi-green gate**: if ≥2 `Green:Y`, a confirm Task must appear **after** the prior green. Join is **latest-per-ROLE / same-wave only** with live critic validation (`session-auditor` + `composer-2.5` + `TRACK=session` in prompt / escape hatch). **`TRACK=plan` must not satisfy `/myauditandfix` empty-skip or `push_audit_gate`.** Fail closed if child jsonl missing. Orchestrator prose (`CONFIRM_SKIP`) never counts. Fallback when skip unavailable: slim confirm (`cursor-grok-4.6-high`, verdict-only).
5. **Any finding → confirm** (`fix_authorized=false`) — independent adjudicator; never confirm+fix in one Task.
6. **Mandatory §4 report** to Matt (skill format) before any fix writes.
7. **Same-turn Phase 2** when not green and not stop-early (`fix_authorized=true`) — do not wait for Matt ack.
8. **Post-fix re-audit mandatory (HARD):** after every Phase 2, always open the next round **before** Green:Y / "no more HIGH" / Loop summary — same turn. `NEW_HIGH_FROM_FIX` is **not** green.

## Always-delta post-fix (HARD)

After Phase 2, post-fix is **always delta** even when the prior confirm had HIGH:

- **Code `/myauditandfix`:** one `ROLE=bug_hunt` critic (after prepr re-run) + confirm (`DELTA_CHECK=true`); skip `claim_bust`.
- **Plan / docs-only:** confirm-only delta (`DELTA_CHECK=true`); skip critics when docs-only.

Escalate to **full dual** only when confirm sets `ESCALATE_FULL_REAUDIT=true` (contested clearance or **new** HIGH). Drop "clearing a prior HIGH requires full dual."

## Artifact pack

Compose **once per round** into `/tmp/audit_pack_<round>.md` when large; Task prompts keep ROLE/TRACK/`fix_authorized` inline. Critics Read the pack path. Cap inline oracle tails ~40 lines. Prepr bundles over ~200 lines → `/tmp/prepr_bundle_<fingerprint>.json` + metadata inline.

Pack must include: scope block, TRACK, file/plan set, load-bearing claims, plan/todo ids, prior deltas, Freshness notes (path or inline `NO_IDENTIFIERS` on `/verify-plan`; `"none"` only on session track when Freshness pass did not run), prepr bundle or N/A, critic reports for full rounds (delta code: bug_hunt only).

## Green / stop-early

**Green** when latest confirm (or valid empty-skip on first green) has zero HIGH/MEDIUM, plan/session completion gates pass per skill, and post-fix confirm ran when Phase 2 ran.

**Stop-early — Blocked on Matt:** after confirm, if every remaining HIGH/MEDIUM is explicitly Blocked on Matt and zero fixable-in-session findings remain → exit with `Green: N` (not Y).

**Loop summary** must use exact `**Green:** Y` / `**Green:** N` (or `Green: Y` / `Green: N`) for the stop hook.

## Anti-patterns (loop)

- Merge `bug_hunt` + `claim_bust` into one critic
- Confirm+fix in one Task (dual path only — solo `/audit` may audit+fix in one Task)
- Skip confirm when critics agree on MEDIUMs (judge still required)
- Honor-system `CONFIRM_SKIP` prose as Push OK evidence
- Satisfy myaudit empty-skip / push gate with `TRACK=plan`
- Join every historical `ROLE=` Task or join on `ROLE=` substring alone
- Stamp a later `Green:Y` from stale round-1 empty sentinels
- End turn on Phase 2 without post-fix re-audit
- Treat `NEW_HIGH_FROM_FIX: false` as green
- Dual critics on `/audit` / `audit your work`
- Composer or k3 pin on the solo Grok Task (Cursor)

## Solo audit loop (`/audit`)

Owned by **`/audit`** and informal **audit your work**. Dual-critic waves above stay for `/myauditandfix` and `/verify-plan`.

This path **stamps push OK** on `Green: Y` when the solo pipeline Task exists (`audit_marker.py`). **Latest session-audit invocation wins:** a later `/audit` (or informal audit your/our/this work) overrides an earlier `/myauditandfix`. Both commands in the **same** user message still count as dual.

### Host dispatch (HARD)

Resolve from **`.cursor/dispatch-settings.yaml`** `audit_solo`.

| Host | Type | Model |
|------|------|-------|
| **cursor** | native `audit-solo` when present (escape: `generalPurpose` + loop/agent brief) | pin **`cursor-grok-4.6-xhigh`** (allow `high`); never omit; **never Composer**; **never k3 as an adjudicator** |
| **grok** / **claude** | **model omit always** | harness default; grok `capability_mode: all` |

Every Task prompt must include `ROLE=solo_audit` / `TRACK=session` / `fix_authorized=` **in the prompt text**.

### Depth and round cap

Loops **until green** (zero HIGH/MEDIUM) or stop-early. Hard cap prevents infinite burn.

| Invocation | Depth | Cap |
|------------|-------|-----|
| Slash default / `audit your work` | HIGH/MEDIUM only (skip LOW enumeration) | **8** |
| Trailing `full` | thorough (include LOW) | **8** |
| Trailing `quick` (Matt-invoked only) | HIGH/MEDIUM only | **4** |
| Trailing `audit-only` | same depth as invocation | no Phase 2 writes |

Never self-select `quick` absent Matt's words.

### Wave shape (HARD)

1. **Freshness pass** (orchestrator) when claims need prior context — graph + memory. No identifier-freshness script (`TRACK=plan` only).
2. **Prepr prepare** (code sessions) — Night School 465: repo-local `prepr_audit.py --worktree --prepare --json --path <each session code path>` before the first solo Task and again after any fix round before the next hire. Docs-only → `prepr: N/A`. Exit 2/3 or missing run blocks Green:Y.
3. **Hire one solo Task** (`ROLE=solo_audit`, `TRACK=session`). Prefer **one Task that loops internally** (audit → fix in-scope findings → re-audit) until this round is green or the agent hits its inner stop. `fix_authorized=true` unless `audit-only`.
4. **Mandatory §4 report** to Matt from the agent payload (skill format) after each hire returns.
5. **Same-turn re-hire** when not green, not stop-early, and under cap — pass prior findings + clearance claims. Do not wait for Matt ack.
6. **Post-fix re-audit mandatory (HARD):** Green:Y only after the latest payload shows zero HIGH/MEDIUM **after** any fixes in that Task (or a follow-up hire that only re-audits). `NEW_HIGH_FROM_FIX` is **not** green.

Parent does **not** hire `session-auditor` or `audit-verifier` on this path. Confirm+fix in one Task is **allowed here only**.

### Green / stop-early

**Green** when the latest solo payload has zero HIGH/MEDIUM, session completion gates pass per skill, prepr is current (or N/A), and post-fix re-audit ran when fixes ran.

**Stop-early — Blocked on Matt:** if every remaining HIGH/MEDIUM is explicitly Blocked on Matt and zero fixable-in-session findings remain → exit with `Green: N`.

**Same failure twice** (Fable 3) → stop, `Green: N`, do not burn the rest of the cap.

**Loop summary** must use exact `**Green:** Y` / `**Green:** N` (or `Green: Y` / `Green: N`) for the stop hook.

### Push pipeline (mechanical)

Stop/push OK requires a Task tool_use with:

- `ROLE=solo_audit` and `TRACK=session` in the **prompt**
- top-level `model` `cursor-grok-4.6-xhigh` or `cursor-grok-4.6-high` (Cursor)
- `subagent_type` `audit-solo` or escape `generalPurpose` / `explore` / omitted

Prose-only "I ran solo audit" never stamps. Dual-critic Tasks do **not** satisfy this path.
