# Audit loop (shared)

Shared by `/myauditandfix` (`TRACK=session`) and `/verify-plan` (`TRACK=plan`). Skills own scope, fix allowlists, and §4 meaning; this file owns waves, green, delta, packs, and host dispatch.

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
| Informal read-only `audit your work` (no slash loop) | thorough | N/A (no loop) |

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
- Confirm+fix in one Task
- Skip confirm when critics agree on MEDIUMs (judge still required)
- Honor-system `CONFIRM_SKIP` prose as Push OK evidence
- Satisfy myaudit empty-skip / push gate with `TRACK=plan`
- Join every historical `ROLE=` Task or join on `ROLE=` substring alone
- Stamp a later `Green:Y` from stale round-1 empty sentinels
- End turn on Phase 2 without post-fix re-audit
- Treat `NEW_HIGH_FROM_FIX: false` as green
