---
name: iterate
description: Deep iterate — parent parallel finders, serial fixer, lenses working/bugs/gaps/polish/product, per-lens budgets, tool utilization
---

# Iterate

**Authoritative contract:** `.cursor/skills/iterate/SKILL.md`. This command **authorizes
the fix phase** (unless trailing `find-only` / `audit-only`).

Iterate = constructive improve on existing surface. Command docs are injected into the
user turn — this file never spells sibling slash names.

## Where this command lives

1. `~/.cursor/commands/` (global — after sync `--local`)
2. `<workspace-root>/.cursor/commands/` (project / Cloud)

```bash
bash "/Volumes/Cloud Storage/Claude/.cursor/scripts/sync-harness.sh"
```

## Trailing text

| Token | Effect |
|-------|--------|
| (none) | Lens = `working` only |
| `lenses=all` | working → bugs → gaps → polish → product (per-lens max **2** passes: find + recheck; honesty in Mission plan) |
| `lenses=a,b` | Explicit list, left-to-right CSV order |
| `lens=<name>` / bare `polish` / `bugs` / `gaps` / `working` / `product` | Single lens |
| `find-only` / `audit-only` | No fix writes (run log still written) |
| `resume=<path\|id>` | Resume run log (required when 2+ in-progress runs match) |
| `scope=<subpath>` | Narrow to an **existing directory** subtree (repo-relative;
  default `scope=.` / repository root). File-valued scope is invalid. |
| path / repo | Target `$REPO_ROOT` |

## Pipeline (parent-owned)

1. Resolve host + `$REPO_ROOT` (git top-level of target) — **no** `$SCOPE_ROOT`
   resolution yet.
2. **Parent before dispatch (ordering HARD):** parse lenses + mode + resume +
   trailing `scope=`; decide fresh vs resume **without scoped reads or dispatch**.
   **Fresh:** trailing `scope=` or default `.`. **Resume:** recorded `Scope:` wins;
   trailing conflict → stop; legacy missing `Scope:` → `.`; trailing non-`.` conflict
   → stop. **Then** resolve/validate selected subpath into `$SCOPE_ROOT` (§1b —
   directory-only; containment). **Only after validation:** scoped cheap-read,
   run-log seed/append, TodoWrite, dispatch. Implicit resume only if **exactly one**
   matching `status: in-progress` for `repo_root:` with `schema_version: 2`; zero →
   fresh; **2+ → stop and ask** Matt with explicit `resume=<path|id>` (list
   candidates; never latest-mtime). `run_mode=fresh` → seed `schema_version: 2` run
   log + print `## Mission plan` + parent TodoWrite after validation.
   `run_mode=resume` → read EXISTING Mission plan + reconstruct TodoWrite and
   consumed budgets (do not overwrite plan rows); run-log `Mode:` wins over
   conflicting trailing text; Scope per ordering above.
3. **Wave A:** parallel `iterate-finder` per requested lens (`pass=find`) — dispatch
   **`repo_root` + `scope_root`** → merge +
   dedupe → **Phase report** A (Tools used / Tools missing / coverage).
   If host Task enum lacks `iterate-finder`, escape: `generalPurpose` +
   `.cursor/agents/iterate-finder.md` (same for fixer → `iterate-fixer.md`).
4. **Fix batch 1:** `iterate-fixer` on merged backlog — **skip in find-only** or empty
   open H/M backlog.
5. **Wave B:** parallel `iterate-finder` recheck on touched lenses → Phase report C —
   **skip in find-only** (no recheck/fix waves).
6. **Fix batch 2** (optional, budget remaining): `iterate-fixer` → Phase report D —
   **skip in find-only**.
7. Finalize Presmoke/Psynth; Mission summary + Findings summary + Plan outcome.
   **Parent updates TodoWrite live between phases.**

**Ceilings:** fix mode max finder dispatches = `2 × N_requested_lenses`; **find-only**
max finder dispatches = `N_requested_lenses` (Wave A only). Max fixer batches = **2**;
max oracle runs = **3 per fix batch**. product lens never blocks Green.

**Incomplete / crashed hire recovery (HARD):** If a finder/fixer fails, is denied, or
returns incomplete schema, parent inspects the known run-log path.

- **Readable:** surface partial plan/phases + `resume=<path|id>`; leave
  `status: in-progress`.
- **Unreadable:** unverified failure; do not fabricate Green or findings.

**You always receive (no ask):** initial `## Mission plan` before Wave A; Phase reports
per wave/batch; final summary tables; run-log path.

## Host dispatch

| Host | Tool | Finder | Fixer | Model |
|------|------|--------|-------|-------|
| cursor | Task | `iterate-finder` | `iterate-fixer` | omit / inherit |
| grok | spawn_subagent | general-purpose + finder md | general-purpose + fixer md | omit |
| claude | Agent | general-purpose + finder md | general-purpose + fixer md | omit |

Legacy `iterate` entry = thin redirect to fixer (parent owns loop).

## Green / stop

See skill. Per-lens max 2 passes (find + recheck). budget-stop when ceilings hit.
**stop-early due Matt-only blockers always yields `Green: N`** (never Green:Y).
**Below-floor on any requested lens always yields `Green: N`** (fix and find-only;
logged findings do not clear a below-floor miss).
Presmoke: **always seed** in fix mode; at finalize `skipped` with evidence `zero-edit` /
`empty-backlog` only (never omit after seed; omission at creation only in find-only);
when run → `complete` with evidence (fail → also open MEDIUM `*presmoke-failed`).
Psynth: from `pending` or `in_progress` → `in_progress` while synthesizing → `complete`
before Plan outcome.
