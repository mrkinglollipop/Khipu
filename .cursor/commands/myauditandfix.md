---
name: myauditandfix
description: Session audit (this thread) with mandatory report, then loop audit→fix until green (max 4 rounds)
---

# Session audit and fix

**Authoritative contract:** `.cursor/skills/audit/SKILL.md` and `.cursor/rules/audit.mdc`. This command **authorizes the fix phase** — equivalent to "audit your work and fix any issues."

**Pipeline:** orchestrator **Freshness pass** (when applicable) + **prepr prepare oracle** (code sessions) → dual `session-auditor` (`TRACK=session`, bug_hunt + claim_bust) → confirm-only `audit-verifier` (`TRACK=session`) → mandatory §4 report to Matt → fix `audit-verifier` when not green. Orchestrator remains hub; does not solo-audit instead of dual critics. Prepr prepare bundle is oracle evidence for critics/verifier — never a model-oracle Green substitute. The script does not dispatch adversarial review in myaudit; `ROLE=bug_hunt` owns the adversarial prepr lens from the bundle.

## Scope

- **Default:** session audit — work done **only in this thread** (the chat where `/myauditandfix` was invoked):
  1. **Conversation writes** — paths touched by `Write`, `StrReplace`, or `Delete` in this thread (orchestrator + subagents).
  2. **Session side effects** — paths changed by shell in this thread (e.g. `khipu capture` / stop-hook → `episodes.jsonl` + any topic pages).
  3. **Load-bearing chat claims**, plan/todos, and rule-compliance (dispatch, build gate, read-only discipline, **Deliverable Contract compliance + intent alignment vs first user build request**) **from this thread**.
- **Not in scope:** pre-session uncommitted WIP — do **not** treat repo-wide `git diff` as the session audit surface. If a path was not touched in this thread, it is out of scope unless trailing text names it explicitly.
- **Narrow:** trailing text after `/myauditandfix` narrows or adds a target (path, subsystem, constraint). Still run Phase 2 unless trailing text explicitly says audit-only / no fixes.
- **Out of scope:** unrelated workspace areas, drive-by refactors, new features beyond fixing listed findings.

**Depth:** thorough (default); trailing **`quick`** per Fast paths below. **Track:** **`TRACK=session`** on every critic and verifier dispatch (§0 session audit in SKILL). Skip graph orient (§1) when target is only this thread's local files.

**Bugbot:** default **off** for session track. Opt in **only** when Matt's prompt or trailing text explicitly asks for BugBot / `/review-bugbot`. No implicit carve-outs (including security-sensitive sessions).

## Fast paths (speed — quality-preserving)

- **No auditable surface:** session file set is empty (no `Write`/`StrReplace`/`Delete`, no shell side effects) **and** the thread has no load-bearing claims → skip critics and verifier entirely. Emit a minimal §4 report (Action summary + one-line ledger and Plan rows stating the empty surface) and end with **Loop summary** `Green: N/A — no auditable surface`. Never emit `Green: Y` on this path — the push gate requires critic→confirm Task evidence; if this session needs Push OK, run the full pipeline instead.
- **Zero-findings slim confirm:** when both round-1 critics return zero findings, the confirm verifier is still mandatory (pipeline evidence) — but dispatch it with the slim pack (scope block, claims list, critic verdict lines, Freshness notes, prepr metadata) and instruct **verdict-only adjudication**: build the ledger from supplied evidence; re-run oracles only for contested or HIGH items.
- **`quick` depth (Matt-invoked only):** trailing `quick` → critics report **HIGH/MEDIUM only** (skip LOW enumeration), Freshness pass uses free oracles only (no paid web unless a load-bearing external claim exists), round cap **2**. Green criteria unchanged (zero HIGH/MEDIUM). Never self-select `quick` absent Matt's words (trailing `quick` or quick-intent phrasing like "sanity check").

## Artifact pack (orchestrator → critics + verifier)

Every dispatch for a round must include:
- Scope block (target, in/out, depth)
- **`TRACK=session`**
- Session file set (paths from this thread's Write/StrReplace/Delete + named shell side effects)
- Load-bearing chat claims list (verbatim or quoted)
- Plan/todo ids in thread (or "none")
- Prior round finding deltas (rounds 2–4)
- When `DELTA_CHECK=true`: paths touched by last Phase 2 fix, prior confirmed findings + clearance claims, fix-round oracle tails. **Code sessions:** one **`ROLE=bug_hunt`** critic report (after prepr prepare re-run; prepared bundle attached); **skip `ROLE=claim_bust`**. **Docs-only / no-code:** skip all critics.
- Oracle log tails already collected this round (if any)
- **Freshness oracle notes** (orchestrator-collected before critics when Freshness pass ran; use "none" otherwise)
- **Prepr prepare bundle** — for **code** sessions: exit code, fingerprint, `scope_paths`, `files`, `audit_input`, and `prompt` (inline when ≤~200 lines; otherwise metadata + `/tmp/prepr_bundle_<fingerprint>.json` path per Pack-once rule below) from repo-local `prepr_audit.py --worktree --prepare --json --path …` (one `--path` per session code path). Docs-only sessions: `prepr: N/A`. Exit **2/3** or a missing run is a failed/unverified oracle (blocks Green:Y). Adversarial findings come from critics/verifier, not from the prepare script.
- For verifier step 1: **both critic reports** (bug_hunt + claim_bust) on **full** rounds — on **`DELTA_CHECK=true` code rounds**, the **delta `bug_hunt` report** + prepr prepare bundle (no claim_bust); on **docs-only delta**, critic reports omitted
- For verifier step 2 only: the confirmed finding list from step 1 (no re-litigating dropped items without new evidence)

**Pack once, reference big blobs (speed):** compose the artifact pack **once per round** and paste the identical block into every dispatch that round — do not re-derive it per dispatch. Cap inline oracle log tails at ~40 lines each. Do not inline file contents critics can read themselves — the file set paths are the reference. When the prepr bundle's `audit_input`/`prompt` exceeds ~200 lines, write the full JSON to `/tmp/prepr_bundle_<fingerprint>.json` and attach inline only exit code, fingerprint, `scope_paths`, `files`, and that path — critics/verifier read the file.

**Host dispatch (HARD):** resolve critic + adjudicator from **`.cursor/dispatch-settings.yaml`** for the active host (`cursor` | `grok` | `claude`) — that file is the SSOT for dispatch tool, types, models, and readonly/capability modes. Procedure is host-invariant; models/types/tools are not.
- **cursor:** `Task`; critics pin `composer-2.5` + `readonly: true` (native `session-auditor`); adjudicator pin `cursor-grok-4.6-xhigh` (allow `cursor-grok-4.6-high`); never omit; never Composer; never k3 as adjudicator pin. Escape hatch when Task enum lacks the native type: `generalPurpose`/`explore` + read agent `.md`, same model rules.
- **grok / claude:** **model omit always** (harness default) — do not pin `composer-*`, `cursor-grok-*`, `grok-composer-*`, or bare `grok-4.5` / `grok-4.6`. Grok: `spawn_subagent` type `general-purpose` + agent `.md`; critic/confirm `capability_mode: read-only`, fix `all`. Claude: `Agent` + agent `.md`.

## Freshness pass (orchestrator — before dual critics when applicable)

Run when session claims cite paths, versions, APIs, or external "current" facts.

**Owner:** orchestrator collects notes **before** dual critics; critics may add incidental free checks only.

1. **Always (free):** existence/help oracles for cited session paths/commands; compare claims to current files.
2. **When claims need prior context (free):** graph `search` + memory topics grep — not a full-workspace orient every round.
3. **When applicable (paid, ≤5/round, orchestrator budget):** versioned external surfaces → `.cursor/skills/web-search/SKILL.md` / `scripts/_clients`. Cite URL + date in ledger.

Put results in artifact pack as **Freshness oracle notes**. Paid web budget does **not** consume each critic's 3 shell/oracle-run cap.

**Delta freshness reuse:** Freshness pass before dual critics when running **full** re-audit; `DELTA_CHECK=true` rounds may reuse last freshness notes unless session paths/claims changed.

## Prepr prepare oracle (orchestrator — code sessions)

**Owner:** orchestrator. Not a substitute for dual critics/verifier. The script collects and fingerprints session diff input only — it does **not** dispatch Cursor/Fleet or stamp markers in myaudit.

- **When:** session file set includes code (extensions audited by `prepr_audit.py`). Docs-only / no-code → mark **prepr: N/A**; do not run.
- **Command:** pass **every code path from the session file set** as a separate `--path`. Use the **repo-local** script (synced to every fleet repo):

```bash
python3 .cursor/scripts/prepr_audit.py --worktree --prepare --json \
  --path <session/code/path1> --path <session/code/path2>
```

  Any cwd inside the repo works — the script resolves the root itself. `--prepare` requires `--worktree` and **rejects** `--post`, `--pr`, and `--waive` (exit 2).
- **When to run:** (1) before round-1 critics; (2) after every Phase 2, before the mandatory full/delta confirm. Attach the full JSON bundle to critics (full rounds: both; **delta code rounds:** `bug_hunt` only).
- **Critic roles (full rounds):**
  - `ROLE=bug_hunt` — adversarial prepr lens: review `audit_input` / `prompt` from the bundle for logic bugs, edge cases, security, broken error handling.
  - `ROLE=claim_bust` — validate bundle freshness/scope/fingerprint (matches current session file set; re-run after fixes).
  - Verifier adjudicates findings from both critics.
- **Delta code rounds (`DELTA_CHECK=true`):** after prepr prepare re-run, dispatch **exactly one** read-only `session-auditor` with **`ROLE=bug_hunt`**, **`TRACK=session`**, and the prepared bundle. **Skip `claim_bust`**. Verifier receives delta `bug_hunt` report + bundle + prior findings/clearance/oracles.
- **Interpretation:**
  - **exit 0** — prepare succeeded (including no-code). Attach bundle to artifact pack.
  - **exit 2 / exit 3 / no run** — failed or unverified oracle. Blocks **Green: Y** for code sessions until a completed prepare exists.
- **Green gate:** code sessions require a **completed** latest prepare run for the **current fingerprint** (re-run after fixes change the input) **plus** zero confirmed open HIGH/MEDIUM from critics/verifier. Docs-only: prepr N/A does not block green.

## Phase 1 — Audit (read-only)

Do **not** edit files during this phase.

1. State scope block: target, in scope, out of scope, depth.
2. Run **Freshness pass** when applicable; attach **Freshness oracle notes** to artifact pack.
3. Run **prepr prepare oracle** when this is a **code** session; attach **Prepr prepare bundle** (or `prepr: N/A` for docs-only).
4. **Dual critics (parallel):** dispatch two critics in one turn with **`TRACK=session`**, roles `bug_hunt` + `claim_bust`, using the **active host profile** from `dispatch-settings.yaml` (type, model policy, readonly/capability_mode, prompt_from):
   - `ROLE=bug_hunt` — correctness, regressions, edge cases, silent failures; **adversarial prepr lens** from prepare bundle (`audit_input` / `prompt`)
   - `ROLE=claim_bust` — chat claims vs evidence, false done, process gaps, **shared freshness items** (stale/false paths, outdated API/version claims, chat "verified" without evidence, scope creep vs thread intent, freshness failures), and **prepr bundle freshness** (fingerprint, scope_paths, session file set alignment)
5. **Confirm-only verifier:** dispatch adjudicator with `fix_authorized=false`, **`TRACK=session`**, critic report(s) per mode — **full:** both bug_hunt + claim_bust; **`DELTA_CHECK` code:** delta bug_hunt + prepr prepare bundle; **`DELTA_CHECK` docs-only:** no critic reports — plus Freshness oracle notes. Use host profile for type/model/readonly (Cursor: Task `audit-verifier` pin `cursor-grok-4.6-xhigh`; Grok: `spawn_subagent` general-purpose + omit + read-only). Verifier confirms/rejects/dedupes and returns §4-ready payload.
6. **Optional bugbot:** only when Matt opted in — fold into Findings; does not replace §4.1–§4.3.
7. Orchestrator surfaces **mandatory report** to Matt in order (from verifier payload + synthesis):
   - **Action summary** (verdict, do now, blocked on Matt, plan status)
   - **Verification ledger** (every row: verified / unverified / inferred)
   - **Plan completion** (row per plan/todo item, or N/A)
   - **Findings** (severity table + themes)

**Two-step rule (HARD):** mandatory §4 report is surfaced to Matt **before any fix writes**. Verifier never confirm+write in a single Task.

**Same-turn continue (HARD):** After the §4 report is surfaced in the orchestrator message, if the audit is **not green** and **not stop-early**, **immediately** dispatch Phase 2 (`fix_authorized=true`) in the **same turn** — do **not** wait for Matt acknowledgment, “continue”, or “go fix”. `/myauditandfix` / `/verify-plan` already authorize the fix phase. Two-step only constrains **order** (report text before fix writes) and forbids confirm+fix in one Task — it is **not** a human checkpoint between phases.

## Phase 2 — Fix (authorized by this command)

1. Dispatch adjudicator with `fix_authorized=true`, **`TRACK=session`**, host-profile fix settings (Cursor: Task `audit-verifier` pin `cursor-grok-4.6-xhigh`; Grok: `spawn_subagent` general-purpose, **model omit**, `capability_mode: all`), and the **confirmed finding list** from Phase 1 step 1 — fix **only** those findings; no scope creep.
2. Orchestrator reviews diff + runs oracle gate. No LOC force-dispatch.
3. Re-verify each fix via oracle/ledger updates only — **this is not a re-audit**. Fix-agent self-report (`NEW_HIGH_FROM_FIX`, clearance notes) is **input to post-fix mode selection only**; it does **not** authorize **Green: Y**, “zero HIGH/MEDIUM,” or ending the loop.

## Phase 3 — Loop until green (mandatory for `/myauditandfix`)

Run **sequentially** — do not start the next round until the current round's Phase 2 is done **when Phase 2 ran**. Exit without Phase 2 when already **green** or **stop-early** after confirm.

**Green** (stop looping) when **all** of:
- Findings: **zero** HIGH and **zero** MEDIUM rows — as of the **latest confirm** (full confirm or `DELTA_CHECK` confirm), **not** as of the fix verifier's self-report
- Plan completion: no **partial** or **not started** rows for in-scope items
- Every load-bearing claim in Verification ledger is **verified** or explicitly **blocked on Matt** (not silently **unverified**) — **freshness-verified ledger gate**
- **Code sessions:** a **completed** latest prepr prepare run exists for the **current fingerprint**. Exit 2/3, a missing run, or a stale fingerprint → not green. Docs-only: prepr N/A
- Action summary **Do now** is "Nothing blocking — ship" (or equivalent)

**Not green** (keep looping) when any **fixable-in-session** HIGH/MEDIUM finding remains, any in-scope plan item is partial/not started, load-bearing freshness claims lack verification or explicit Matt blocker, code-session prepr prepare is missing / exit 2–3 / stale fingerprint, or fixes introduced new regressions surfaced in re-audit.

**Stop early — Blocked on Matt (HARD):** after confirm (any round), if every remaining HIGH/MEDIUM finding is explicitly **Blocked on Matt** / requires Matt authorization (credentials, commit/push, taste, irreversible outward action, or a decision only Matt can make) **and** there are **zero** fixable-in-session HIGH/MEDIUM findings — **exit the loop immediately**. Do **not** start Phase 2 or further re-audit rounds. Surface the §4 report (or Re-audit block), list blockers under **Blocked on you**, and end with **Loop summary** `**Green:** N` (not Y — Matt action still required). Wasted rounds that cannot clear without Matt are forbidden.

**Round cap:** **4 rounds** total (**2** when Matt invoked `quick`) (round 1 = first Phase 1 + optional Phase 2; rounds 2–4 = re-audit/delta + fix). After round 4, stop even if not green; report remaining findings and **Blocked on you** items. Stop-early (above) may exit sooner.

**Post-fix re-audit mandatory (HARD):** after **every** Phase 2 that ran, you **must** start the next round's audit (full or delta) **before** any claim of green, “no more HIGH,” “cleared,” or Loop summary — **in the same turn**; do **not** end the turn on fix alone or wait for Matt before re-audit. `NEW_HIGH_FROM_FIX` and whether the prior confirm had any HIGH choose shape only — never skip re-audit / never equals green.

**Post-fix re-audit mode (HARD):** after each Phase 2, choose the next round's audit shape before dispatching:
1. **Full re-audit** when **any** of:
   - the **prior confirm** (full or delta) had **any HIGH** finding — including HIGH the fix claims to have cleared (clearing prior HIGH requires dual critics, not delta); **or**
   - the fix introduced **any new HIGH** (including a previously-cleared HIGH reopened as HIGH), or the fix verifier / oracle gate reports a HIGH regression  
   → Freshness pass (when applicable) + **prepr prepare re-run** (code sessions) + dual critics + confirm-only verifier (host profile).
2. **Confirm-only delta check** only when **both**:
   - the **prior confirm** had **zero HIGH**, **and**
   - the fix introduced **no new HIGH** (`NEW_HIGH_FROM_FIX=false`)  
   → Re-run **prepr prepare** (code sessions) before the delta confirm. **Code sessions:** dispatch **one** read-only `session-auditor` with **`ROLE=bug_hunt`**, **`TRACK=session`**, and the prepared bundle (**skip `claim_bust`**). **Docs-only / no-code:** skip all critics. Then dispatch one confirm-only adjudicator (`fix_authorized=false`, host-profile confirm settings + **`DELTA_CHECK=true`**), fix-touched paths, prior confirmed findings + clearance claims, fix-round oracle tails, prepr prepare bundle (code), and delta **`bug_hunt` report** when present. New MEDIUM-only issues do **not** force full re-audit. Delta confirm is still a **real second pass** — clearances are not green until this confirm returns zero HIGH/MEDIUM (or stop-early) **and** (code) a completed prepr prepare exists for the **current fingerprint**.
3. If a confirm-only delta check **surfaces a new HIGH**, do **not** Phase 2 yet — **escalate in the same round** to full re-audit (dual critics + confirm) before further fixes. Still counts as one round toward the cap.

**Per round:**
1. **Round 1:** full mandatory report (§4.1–§4.4) before any edits (always dual critics + confirm). Apply stop-early if only Matt blockers remain.
2. **After Phase 2:** always open rounds 2–4 via post-fix re-audit mode (never end on fix alone). Emit **Re-audit (round N/4)** or **Re-audit (round N/4) — delta**. **Delta code:** one `bug_hunt` critic + confirm; **delta docs-only:** confirm only. Findings / ledger / Plan completion **deltas only**; skip unchanged rows. Apply stop-early after confirm (or after escalate-confirm).
3. If not green, not stop-early, and round `< cap` (4; 2 under `quick`): Phase 2 (fix verifier) on confirmed findings only, then **mandatory** next re-audit round.
4. If green or stop-early **after a confirm/delta confirm**, or round `= cap`: exit loop.

**End state:** short **Loop summary** that includes a string matching `.cursor/hooks/audit_marker.py` `GREEN_RE` when green so the push-audit stamp can fire — prefer `**Green:** Y` or `Green: Y` (also accepted: `rounds used … Green: Y` / `Loop summary … Green: Y`). When not green, use `**Green:** N` or `Green: N`. **Push OK also requires critic→confirm Task dispatch evidence in the transcript** (`tool_use` Task blocks, not assistant prose alone) when `/myauditandfix` was invoked (see `audit_marker.py` pipeline gate; kill switch `/tmp/.cursor_audit_pipeline_gate_disable`): **round 1** = dual critics → confirm; **delta code rounds** = one `bug_hunt` critic → confirm; **delta docs-only** = confirm-only. Also include: rounds used (e.g. 2/4), re-audit mode used each round (full vs delta), what changed across rounds, what was re-verified vs still unverified, any findings left after cap. **Green: Y requires at least one post-fix confirm or delta-confirm Task in the transcript when Phase 2 ran** (fix self-report alone is insufficient).

**After Green: Y — stamp_ok (cloud / stop-miss):** when this conversation may lack a local agent transcript (Cloud Agent `bc-*` ids, or stop hook did not stamp), run before any `/commitprmerge` push. With a real `conversation_id`, `stamp_ok` requires a prior PENDING marker (`/myauditandfix` or `/verify-plan` already invoked); without PENDING it no-ops. Workspace-only stamp (no `conversation_id`) can still mint WS_OK without PENDING by design.

```bash
python3 "/Volumes/Cloud Storage/Claude/.cursor/hooks/audit_marker.py" stamp_ok <<EOF
{"conversation_id":"<id if known>","cwd":"$(pwd)","workspace_roots":["$(pwd)"]}
EOF
```

This is the authorized recovery path — not inventing bypass files. Local chats with a real transcript still rely on stop + pipeline evidence; `stamp_ok` is backup when that path cannot see the transcript.

## Anti-patterns (do not)

- Pausing for Matt between the §4 report and Phase 2, or patching before the §4 report exists (Same-turn continue + two-step order above)
- Confirm+fix in a single `audit-verifier` Task dispatch
- Solo-auditing in the orchestrator thread instead of dual critics + verifier
- Treating this as implicit `approved — build` for new features or broad multi-file work
- Substituting bugbot-only output for the mandatory report sections, or defaulting bugbot on without Matt opt-in
- Auditing repo-wide `git diff` or pre-session WIP instead of this thread's file set
- Stopping before green without stop-early or the 4-round cap
- Declaring **Green: Y** / "no more HIGH" after Phase 2 without a post-fix full or `DELTA_CHECK` confirm Task (`NEW_HIGH_FROM_FIX: false` is not green and not a re-audit substitute)
- Confirm-only delta outside its two conditions — prior confirm had any HIGH, or the fix introduced a new HIGH / HIGH regression → full re-audit until a confirm returns zero HIGH
- Declaring **Green: Y** on a code session without a completed latest `prepr_audit.py --worktree --prepare` run for the current fingerprint, or treating prepr alone as a substitute for dual critics/verifier
- Dropping the prepr prepare bundle (or its temp-file path) from the artifact pack
- Running prepr inside `/commitprmerge` instead of `/myauditandfix`
- Running `--worktree` without `--path` for the session file set (audits pre-session WIP)
- Running prepr without `--prepare` in myaudit (`--worktree` without `--prepare` is legacy worktree dispatch/stamp; branch mode is no `--worktree`, may use `--post` — neither used in myaudit)
- Skipping delta **`bug_hunt`** critic on code-session `DELTA_CHECK` rounds (docs-only delta may skip all critics), or delta without a re-run prepr prepare bundle
- Using the no-auditable-surface fast path when the thread has session writes or load-bearing claims — or emitting `Green: Y` from it
- Self-selecting `quick` depth absent Matt's words
