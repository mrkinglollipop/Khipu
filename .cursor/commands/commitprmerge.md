---
name: commitprmerge
description: "Ship all session changes in the current repo: branch, commit, push, PR, merge, sync main"
---

# Commit, PR, merge — session ship loop

**Authorizes** the full git ship loop for the **current repository**: inventory session changes, branch off `main`, commit, push, open PR, merge on GitHub, sync local `main`. Use at end of a working session when the conversation produced real file changes you want on GitHub.

Equivalent to explicit authorization for commit, push, PR create, and merge in one command.

**Incomplete ship is failure.** Local commit alone is **not** done. An **open** PR is **not** done. Never tell Matt the work is "committed" / "shipped" / "caught up" unless push + PR + **merge** (or an explicit **INCOMPLETE** stop with the blocker) completed with shell/`gh` evidence.

**Matt said push (HARD).** `push`, `ship`, `PR it`, or `/commitprmerge` authorizes the **full** loop: commit dirty session files → push → open PR → **merge on GitHub** → sync local `main`.
- Dirty session files + "push" → **commit first**. Never `git push` while in-scope work is still uncommitted.
- Stopping after `gh pr create` is **INCOMPLETE**, not success. Matt treats unmerged PRs as a mess he already thought landed.
- If a merge gate blocks, lead with **INCOMPLETE** and the blocker — do not imply GitHub is caught up.

## Where this command lives

Cursor only loads slash commands from:

1. **`~/.cursor/commands/`** (global — works in any opened folder)
2. **`<workspace-root>/.cursor/commands/`** (project — only when that folder is the workspace root)

Nested paths (e.g. opening `/Volumes/Cloud Storage/Code/mrs-lollipop` while the command file sits only at `CURSOR/.cursor/commands/`) **do not** inherit parent commands. Run once from claude-os-workspace root (or any machine after clone):

```bash
bash "/Volumes/Cloud Storage/Claude/.cursor/scripts/sync-harness.sh" --local
```

That symlinks the cursor-harness plugin, merges user hooks, and mirrors harness commands into `~/.cursor/commands/`. Re-run after harness command or plugin updates.

## Repo eligibility

Operate in the git repo containing the cwd (`git rev-parse --show-toplevel`). **Preferred roots:**

- `/Volumes/Cloud Storage/Claude/` (claude-os-workspace)
- `/Volumes/Cloud Storage/Code/*` (app repos — canonical SSOT)
- `~/Projects/*` (FT Terminal, FT Command, etc. — same ship loop)

**Stop** (report why) when:

- Not inside a git work tree
- No `origin` remote, or remote is not GitHub
- `main` (or repo default branch) cannot be resolved
- Matt said audit-only / no ship

## Push-audit gate (HARD — read before push)

`beforeShellExecution` → `push_audit_gate.py` **allows `git commit`, denies `git push`** until this conversation has a green `/audit` or `/myauditandfix` marker (or bypass).

| Marker | Meaning |
|--------|---------|
| `/tmp/.cursor_audit_ok_<conversation_id>` | Stop stamped after `Green: Y` (+ Task pipeline evidence when `/audit` or `/myauditandfix` is pending) |
| `/tmp/.cursor_audit_ok_ws_<workspace>` | Workspace-root fallback OK |
| Bypass phrases | Matt says `push without audit` / `skip audit` / `bypass push audit` **anywhere in the prompt** (inline is enough). Option-list / INCOMPLETE dual-phrase pastes do not stamp |
| Kill switch | `touch /tmp/.cursor_push_audit_disable` |

**Live transcript fallback:** `beforeShellExecution` often supplies only `command` + `conversation_id` (no `transcript_path`). The gate **best-effort resolves** the transcript from payload path keys (`transcript_path`, `transcriptPath`, `agent_transcript_path`) or by searching `~/.cursor/projects/*/agent-transcripts/` for `{conversation_id}.jsonl`. When a transcript is found and it already has `Green: Y` and (when myaudit) dual-critic→confirm Task evidence, the gate **allows push and backfills** the OK stamp.

**Cloud Agents (`bc-*` conversation ids):** fail-open (allows push + logs `cloud_fail_open`) only when the **primary** `conversation_id` is `bc-*` and no local transcript resolves — private workers never sync transcripts. If `conversation_id` is local/non-`bc-*` and only a secondary id (e.g. `session_id`) is `bc-*`, push still **denies** (hybrid / smoke54). Still prefer an explicit `stamp_ok` after green (below) so WS_OK exists for hybrid local continue.

**stamp_ok (after Green: Y):** when stop may miss (cloud / no transcript), orchestrator runs before push. With a real `conversation_id`, `stamp_ok` requires a prior PENDING marker (`/myauditandfix` or `/verify-plan` already invoked); without PENDING it no-ops. Workspace-only stamp (no `conversation_id`) can still mint WS_OK without PENDING by design.

```bash
python3 "/Volumes/Cloud Storage/Claude/.cursor/hooks/audit_marker.py" stamp_ok <<EOF
{"conversation_id":"<id if known>","cwd":"$(pwd)","workspace_roots":["$(pwd)"]}
EOF
```

Do **not** invent bypass markers. Bypass phrases stamp when they appear in Matt's prompt (inline is enough — not own-line-only). Option-list / INCOMPLETE dual-phrase pastes still do not stamp.

**Preflight (before `git push`):** check for OK/bypass markers, cloud fail-open, or expect transcript fallback; if the previous push was denied, stop with **INCOMPLETE** — do not retry push in a loop, do not claim "committed and done."

Preferred recovery: end a turn with Loop summary `Green: Y` after `/audit` or `/myauditandfix`, run `stamp_ok` when cloud/no-transcript, then re-run `/commitprmerge` (or Matt says a bypass phrase anywhere in the prompt).

## What to ship (default scope)

**Only files created or modified in the conversation that invoked this command** — not all uncommitted repo dirt.

### Build the session file list (before staging)

1. **Conversation writes** — every path touched by `Write`, `StrReplace`, or `Delete` in **this thread only** (orchestrator + subagents).
2. **Session side effects** — paths known to have changed from shell in this thread (e.g. `khipu capture` / stop-hook → `/Volumes/Cloud Storage/Memory/conversations/episodes.jsonl` and any `topic_pages`).
3. **Intersect with git** — `git status --short`; stage only session-list paths that are modified, added, or untracked. Ignore other dirty files even if present.
4. **Never stage** even if in the session list: `API Keys/`, `**/.env`, `**/*.pem`, `**/*.key`, secrets, `_precompact_safety/`, `*.bak*`, build artifacts ignored by `.gitignore` but accidentally forced.
5. **`.cursor/mcp.json` carve-out:** may stage when in the session list **only if** `python3 .cursor/scripts/mcp_json_is_shippable.py .cursor/mcp.json` (or the Claude workspace copy of that script) exits 0 — URL / `${env:…}` / `<from …>` stubs only. **Abort** if the checker fails (live tokens). Never stage `~/.cursor/mcp.json`.

### Empty working tree but unpushed commits

If the session file list is empty **and** `git status` is clean **and** `HEAD` is ahead of `origin/main` (or has no upstream) with commits from **this conversation's ship already on the current branch** (re-invoked `/commitprmerge` after a mid-ship push deny, or Matt said continue/push):

- **Do not** stop at "Nothing to ship."
- Skip stage/commit; continue from **Push** through merge + sync main.
- Still show inventory: shipping = those unpushed commits; excluded = other WIP.

If the session file list is empty, tree clean, and HEAD is **not** ahead → stop: **"Nothing to ship from this conversation."**

**Inventory must show two lists:** (a) **shipping** — session files to stage (or unpushed commits continuing); (b) **excluded** — other `git status` paths left unstaged (label as pre-existing WIP / other sessions).

**Trailing text** after `/commitprmerge`:
- **Paths or globs** → explicit scope override (stage only those paths, still subject to the never-stage list).
- **Otherwise** → commit-message summary (kebab slug source); scope stays session-only.

To ship unrelated uncommitted WIP, Matt must name paths in trailing text or run `/commitprmerge` from the conversation that produced that work.

## Branch rules

**Never commit directly to `main` / default branch.**

```bash
git fetch origin
git checkout main   # or default branch from origin/HEAD
git pull origin main
git checkout -b <branch>
```

**Branch name:** `cursor/<slug>-ceaa` default. `<slug>` from trailing text or dominant change (kebab-case, ≤40 chars). App repos may use existing convention (`feat/…`, `chore/…`) when the repo’s recent branches clearly prefer it — still branch, never main.

When continuing unpushed commits already on a feature branch, **stay on that branch** — do not recreate from `main` and drop the commits.

## Workflow (execute in order)

1. **Inventory** — build session file list (above); `git status --short` + `git diff --stat` for **shipping** paths only. Confirm repo root and `origin` URL. State shipping vs excluded WIP before staging. If empty list + unpushed session commits → continue-from-push path.
2. **Branch** — fresh branch off updated `main` (step above), unless continuing an existing feature branch with unpushed commits.
3. **Stage + security gate** — stage in-scope files; `git diff --cached --name-only`; abort on secrets or unintended paths. (Skip if continue-from-push.)
4. **Commit** — `git commit -m "<type>: <summary>"` where `<type>` is `memory` for claude-os-workspace memory-only, else `feat`, `fix`, or `chore` inferred from the diff. Summary from trailing text or diff. (Skip if continue-from-push.)
5. **Push** — `git push -u origin HEAD`. On `push_audit_gate` deny → **INCOMPLETE** (commits local only); tell Matt to finish `/audit` or `/myauditandfix` green or bypass; **do not** claim shipped.
6. **Prepr evidence reuse (code ships)** — when shipping code files (py, ts, swift, etc.): **do not** run `prepr_audit.py`. Read the latest `/audit` or `/myauditandfix` prepr prepare evidence from **this conversation's transcript** (completed `--worktree --prepare` run, fingerprint, bundle attached to the solo Task or critics). This step is a **transcript read, not a mechanical marker check** — the mechanical gate is `push_audit_gate` on `Green: Y` at step 5. If the transcript has no current Green:Y with completed prepr prepare evidence for the code being shipped → **INCOMPLETE**; tell Matt to run `/audit` or `/myauditandfix` (prepr is that command's oracle, not commit/PR). Docs-only / no-code ships: prepr **N/A**.
7. **Open PR** — `gh pr create --base main --title "<type>: <summary>" --body "…"`. Cloud Agent fallback: `ManagePullRequest` `action: create_pr` when `gh pr create` fails with integration permissions.
8. **Merge gate (repo-specific)**
   - **iOS / native green lane (non-BugBot):** for iOS/macOS app ships, require session-scoped changes, a green local build oracle when the session touched native code, `/myauditandfix` prepr evidence from this transcript for code ships, and a human checkpoint before merge when the repo's ship norms require it. Do **not** open Memory BugBot runbooks from this command — BugBot stays opt-in only.
   - **BugBot default (all repos):** **skipped** unless Matt explicitly asked for BugBot in this thread or trailing text (e.g. "run bugbot", `/review-bugbot`). Do **not** post `bugbot run`, wait for BugBot, or require its review by default.
   - **No CI gate (HARD):** this workspace does **not** use GitHub Actions. Never wait on, poll, retry, or report a GitHub Actions check as a merge gate, and never add a workflow file. Verification comes from local oracles only (pytest, ruff, build/smoke scripts). Merge with `gh pr ready <N>` if draft, then `gh pr merge --merge --delete-branch`. BugBot ship line: **skipped (default)** or **N/A**.
   - **If a ruleset still blocks on a legacy status check:** report **INCOMPLETE** with the check name and tell Matt to remove that ruleset requirement. Do **not** satisfy it by adding a workflow, auto-commenting `bugbot run`, or polling BugBot. Do not use `gh pr merge --admin` (rulesets ignore it).
9. **Sync local main** — `git checkout main && git pull origin main`.

Run shell steps with evidence; tag claims **verified** / **unverified** / **failed** only from output seen this session.

## Ship summary (mandatory end)

**Complete ship** (all required):

- Branch + commit SHA
- PR URL + merge state (**MERGED** or open + why not merged)
- `git log -1 --oneline` on `main` after pull
- Prepr (from `/myauditandfix` transcript): **completed+adjudicated** / **N/A** (docs-only) / **failed** (→ INCOMPLETE)
- BugBot gate: skipped (default) / explicitly triggered / passed / waived / N/A

**INCOMPLETE ship** (push/PR/merge blocked) — lead with **INCOMPLETE**, list what finished (e.g. local commits), the exact blocker (audit gate / missing myaudit prepr / gh / network), and the next Matt action. Never imply GitHub is caught up.

## Anti-patterns

- Staging the full working tree (`git add -A` or all of `git status`) — scoops pre-existing WIP from other sessions
- Shipping only `/Volumes/Cloud Storage/Memory/conversations/` when the session changed app code elsewhere in the same repo
- Expecting the command when it was never installed to `~/.cursor/commands/`
- Claiming merged without `gh` + `git` output
- Claiming "committed" / "done" when push or PR/merge did not finish
- Stopping after `gh pr create` with the PR still **open** — that is INCOMPLETE
- `git push` while in-scope session files are still uncommitted when Matt said push/ship
- Stopping at "Nothing to ship" when this conversation already has unpushed commits on the feature branch
- Force-pushing `main` or merging without Matt naming explicit path overrides for out-of-scope files
- Re-running `prepr_audit.py` inside `/commitprmerge` (prepr belongs to `/myauditandfix` only)
- Claiming this command mechanically validates a prepr marker — it reuses transcript evidence; `push_audit_gate` on Green:Y is the mechanical gate
- Opening a code PR without current `/myauditandfix` Green:Y + completed prepr prepare evidence
- Auto-triggering BugBot (`bugbot run`, wait/poll, or default dispatch) — opt-in only via explicit Matt request or `/review-bugbot`
- Treating a GitHub Actions check as a gate, reporting its status as ship evidence, or adding a workflow file — this workspace does not use GitHub Actions

## See also

- `.cursor/rules/app-development.mdc` — app repo paths; prepr via `/myauditandfix`
- `.cursor/commands/myauditandfix.md` — session audit + prepr worktree oracle before ship
- `.cursor/hooks/push_audit_gate.py` / `audit_marker.py` — push gate + stamp UX
