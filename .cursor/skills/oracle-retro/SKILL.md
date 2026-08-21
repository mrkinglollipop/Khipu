---
name: oracle-retro
description: >-
  End-of-session verify/oracle harness retro for the invoking thread only.
  Audits build/test/visual verify friction, applies cure-not-symptom action bar,
  encodes fixes to runbooks/agents/repo rules when warranted, ends with mandatory
  Closure Summary. Use for /oracle-retro, oracle retro. Does NOT use audit/SKILL.md
  §4 report format. Authorizes harness-only Phase 2 writes per command contract.
---

# Oracle retro

**Mode:** read-only Phase 1; Phase 2 authorized only by **`/oracle-retro`** (or trailing `audit-only` skips Phase 2).

**Not** `/myauditandfix` — narrower (verify/oracle only), no 5-round loop, higher encode bar, **Closure Summary** replaces Action summary / Plan completion.

## 0. Bootstrap (every dispatch)

```bash
# shellcheck source=cursor_root.sh
source "/Volumes/Cloud Storage/Claude/.cursor/scripts/cursor_root.sh"
export_cursor_root
# If CURSOR_ROOT empty: STOP — cannot encode fleet SSOT

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
```

Read `/Volumes/Cloud Storage/Memory/conversations/topics/ios-ship-runbook.md` before proposing iOS verify encodes.

## 1. Scope

**Default:** verify/oracle slice of **this thread only** — build/test/visual claims, shell commands, dispatches (`ios-oracle`, etc.), re-derivation friction.

| In scope | Out of scope |
|----------|--------------|
| Verify pass/fail claims from this thread | Pre-session WIP, repo-wide git diff |
| Shell/MCP oracle commands run or skipped | Features, refactors, non-verify findings |
| Dispatch/oracle routing mistakes | App source (except test-as-oracle) |
| Ad-hoc verify commands never encoded | Tier 2 new agents (defer to custom-agents-plan) |

**Trailing text:** `audit-only` → Phase 1 + Phase 3 only; repo name → narrow `$REPO_ROOT`.

## 2. Write authorization (Phase 2 only)

Authorized targets when `/oracle-retro` invoked (not `audit-only`):

1. `/Volumes/Cloud Storage/Memory/conversations/topics/*`
2. `$CURSOR_ROOT/.cursor-plugin/**`
3. `$CURSOR_ROOT/.cursor/commands/**`, `ios-build-verify.sh`, `hooks/**`, `scripts/smoke_harness_skills.sh`
4. `$REPO_ROOT/.cursor/**`, `$REPO_ROOT/AGENTS.md` when thread touched that repo
5. `$REPO_ROOT/scripts/ship.sh` verify subsection only

`~/Projects/*` harness files OK when (4) applies — command is explicit in-thread auth per `master.mdc`.

## 3. Action bar (Phase 2 requires ≥1 evidence gate)

If **none** apply → skip Phase 2; Phase 3 still runs with "No harness encode warranted."

1. **Verify failure** — oracle exit ≠ 0 with log tail in thread
2. **Model-oracle violation** — pass/fail claimed without shell/MCP evidence
3. **Re-derivation** — same verify steps improvised twice, or runbook ignored
4. **Wrong tier** — UI pass from build-only; mixed-stack half-verified
5. **Encode gap** — ad-hoc command string never written to SSOT

Each **encode** row needs: root-cause class + **Future session runs:** `` `command` `` **instead of:** what went wrong.

## 4. Routing matrix (one primary target per finding)

**SSOT hierarchy:** runbook command > agent procedure > rule doctrine > skill checklist.

### Tier A — cross-repo / plugin

| Target | When |
|--------|------|
| `/Volumes/Cloud Storage/Memory/conversations/topics/ios-ship-runbook.md` | Per-app verify_command (SSOT) |
| `/Volumes/Cloud Storage/Memory/conversations/topics/cursor-web-tools.md` | Gateway selftest |
| `/Volumes/Cloud Storage/Memory/conversations/topics/<app>.md` | Gotcha too long for runbook row |
| `.cursor-plugin/rules/orchestration.mdc` | Dispatch/routing wrong |
| `.cursor-plugin/rules/app-development.mdc` | Fleet-wide doctrine |
| `.cursor-plugin/rules/code-hygiene.mdc` | Missing stack in gate list |
| `.cursor-plugin/agents/ios-oracle.md` | iOS procedure (defer commands to runbook) |
| `.cursor-plugin/agents/app-repo-scout.md` | Repo table wrong |
| `.cursor/ios-build-verify.sh` | Multi-repo XcodeGen wrapper gap |
| `.cursor/hooks/finish_guard.py` | Repeated unverified claims (2+), after encode exists |
| `.cursor-plugin/skills/<domain>/SKILL.md` | Domain verify checklist |

### Tier B — repo-local

| Target | When |
|--------|------|
| `$REPO/.cursor/rules/01-verify-oracle.mdc` | Oracle differs from fleet |
| `$REPO/.cursor/ios-build-verify.sh` | App-unique scheme/sim |
| `$REPO/.cursor/skills/*` | Repo overlay (e.g. circletown-app) |
| `$REPO/AGENTS.md` | cwd, verify_command |
| `$REPO/scripts/ship.sh` | verify subsection only |

### Tier C — reject

Duplicate commands across tiers, hooks before runbook encode, one-off symptom patches.

## 5. Stack → default oracle

| Stack | Oracle | SSOT |
|-------|--------|------|
| iOS / SwiftUI | `bash "$CURSOR_ROOT/.cursor/ios-build-verify.sh" REPO` | runbook + ios-oracle |
| Flutter | `flutter analyze` / sim build | runbook + repo rule |
| Python | `PYTHONPATH=.python_libs python3 -m pytest` (repo path) | repo rule / topic |
| Node/TS | `npm run build && tsc` | AGENTS.md / topic |
| Harness | `build_graph.py`, hook smokes | `$CURSOR_ROOT/.cursor/hooks/smoke_harness_full.sh` |
| Web-tools | `infra/web-tools/selftest.py` | cursor-web-tools.md |

Cloud VM: document Mac-only gaps; never claim sim/screenshot pass.

## 6. Clutter gates (before every write)

1. Read full target file
2. Search for duplicate commands — merge, don't append
3. Delete stale contradictory lines in same target
4. Rules/agents: ≤5 lines added per finding; else use memory topic
5. Post-edit: `rg` duplicates across touched files

## 7. Phase 1 — Read-only report

Do **not** edit until Phase 1 complete.

1. **Scope block** — target, in/out, depth
2. **Verify ledger** — each claim: verified / unverified / model-oracle
3. **Friction log** — re-derived steps, wrong cwd, 3-run cap, Mac/cloud limits
4. **Findings** — evidence, root-cause class, Tier A/B target, future command
5. **Encode decision** — finding → encode / defer / reject (symptom)

## 8. Phase 2 — Implement (encode rows only)

Skip if encode table empty or `audit-only`.

1. One primary file per finding; link secondaries only
2. Hooks/scripts: prefer product Auto / Task when useful; orchestrator reviews + oracle. No LOC force-dispatch.
3. Run changed oracle (3-run cap); report exit code + tail
4. If `$CURSOR_ROOT/.cursor/**` touched: `bash "$CURSOR_ROOT/.cursor/hooks/smoke_harness_full.sh"` or targeted smoke
5. If plugin/commands/rules changed: `bash "$CURSOR_ROOT/.cursor/scripts/sync-harness.sh"`
6. `/commitprmerge` per affected git root
7. Local Mac: do not pipe `capture_v2`; `khipu-stop-hook` owns the write. Cloud: `khipu_capture`.

**Removable gap (max once):** if Phase 3 lists in-scope fixable gap (forgot re-run, encode skipped), fix once and re-emit Phase 3.

## 9. Phase 3 — Closure Summary (mandatory — last block, no prose after)

Re-read Phase 1 encode table + Phase 2 outcomes before writing.

```markdown
## Oracle retro — closure summary

### Resolved (encoded + verified this run)
| Finding | Target file | Future command | Verify evidence |
|---------|-------------|----------------|-----------------|
| … or **None** | | | exit code + tail / N/A |

### Correctly not addressed (with reason)
| Item | Why not encoded | Valid? |
|------|-----------------|--------|
| … or **None** | symptom / out of scope / action bar / Tier 2 | Y |

### Should have addressed but did not
| Gap | Why skipped | Remediation (so next run can fix) | Blocked on |
|-----|-------------|-----------------------------------|------------|
| … or **None — no gaps identified** | | | |

### Closure verdict
- **Worth resolving:** N found → M resolved
- **Harness clean for this thread:** YES | NO
- **Re-run needed:** NO | YES — after `<blocker>` | Matt action: `<one line>`
```

**Rules:**

- Every `defer`/`reject` → "Correctly not addressed"
- Unverified encodes → "Should have addressed" or honest defer
- Blocked gaps stay listed; do not pretend resolved

## 10. Anti-patterns

- Loading `audit/SKILL.md` §4 instead of this skill for `/oracle-retro`
- Ending without Closure Summary four subsections
- Inventing encodes when no evidence gate fired
- Duplicating verify commands in runbook + agent + rule
- `bash .cursor/hooks/...` from app repo cwd without `$CURSOR_ROOT` prefix
- Bare `python3 cursor_root.py` for bootstrap (prints nothing)
