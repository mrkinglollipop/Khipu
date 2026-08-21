# Iterate run <run-id>

- schema_version: 2
- Started: <ISO-8601 UTC>
- Updated: <ISO-8601 UTC>
- repo_root: <absolute path>
- Scope: <repo-relative normalized subpath> (<absolute scope_root>)
- status: in-progress|done
  (top-level run status uses hyphen `in-progress`; plan-step statuses use underscore
  `in_progress` — never mix when matching resume)
- Target: <path> + class (ios|mac|electron|web|harness|aegis|loom|biblical|manna|generic)
- Host: cursor|grok|claude
- Mode: fix|find-only
- Lenses requested: <list>
- Standing drive auth: yes|no
- Primary workflow: <one sentence>
- Green / stop reason: <text or pending>

## Mission plan

SSOT for mission execution steps (lens order + gates). Parent TodoWrite mirrors this
table. Parent updates durable log **between phases**; finders/fixers do not write this file
(**durable-write ban** on hired finders).

Plan status vocabulary: `pending`|`in_progress`|`complete`|`blocked`|`skipped`.
Never invent plan status `capped`. At a lens's second pass after recheck, mark the lens
step `complete` with evidence `per-lens budget reached` (remaining H/M stay `open` in
fix mode unless validly deferred; in **find-only** stay `logged`).

`blocked-on-Matt` is a finding status only — never a plan-step status.

**Presmoke terminalization:** **always seed** a Presmoke row in fix mode. At finalize
mark `skipped` with evidence `zero-edit` / `empty-backlog` only — never omit the row
after seed; omission at plan creation only in `find-only`; **never** use `find-only` or
`not applicable` as finalize skip evidence. When Presmoke runs → `complete` with
evidence (pass) or `complete` with fail evidence + open MEDIUM `*presmoke-failed`.

**Psynth sequence:** template seeds `Psynth` as `pending`; transition from `pending`
**or** `in_progress` → `in_progress` while synthesizing → `complete` immediately before
Plan outcome; then `status: done`, Updated, Green / stop reason.

Resume: requires `schema_version: 2` in the candidate log (missing or lower → unreadable;
stop, surface path, ask disposition — never silently upgrade). exactly one `status: in-progress`
for `repo_root:` (**exactly one** matching); **2+** matching in-progress → parent stops for
explicit `resume=`. Never reopen `status: done`. Do not fall back to latest-mtime completed
logs. Run-log `Mode:` wins over conflicting trailing text (stop and ask).

**Resume scope ordering (HARD):** determine fresh vs resume and select normalized scope
**before** resolving `$SCOPE_ROOT` or any scoped read/log/dispatch. Recorded `Scope:`
wins on resume; trailing `scope=` conflict → stop and ask. Legacy logs lacking `Scope:` →
interpret as `.` (repo root); trailing non-`.` `scope=` that conflicts → stop (no silent
broaden/narrow); record interpretation on next parent write when proceeding. Scope subpath
must resolve to an existing **directory** (file-valued scope invalid — stop before
reads/log/dispatch).

| Step | Lens | Objective / gate | Status | Finding IDs | Evidence / next |
|------|------|------------------|--------|-------------|-----------------|
| P1 | <requested-lens-1> | find + recheck gate | pending | | |
| P2 | <requested-lens-2> | find + recheck gate | pending | | |
| … | … | … | pending | | |
| Presmoke | — | final working re-smoke (always seed in fix mode; skip only zero-edit/empty-backlog) | pending | | |
| Psynth | — | final synthesis | pending | | |

## Coverage summary

| Lens | Pass | Surfaces enumerated | Exercised | Floor |
|------|------|---------------------|-----------|-------|
| <lens> | find|recheck | <n> | <n> | met|partial|below |

## Tools summary

Omit Fix / Wave B rows when Mode=`find-only`.

| Phase | Tools used | Tools missing |
|-------|------------|---------------|
| Wave A find | <list> | <list> |
| Fix batch 1 | <list> | <list> |
| Wave B recheck | <list> | <list> |
| Fix batch 2 | <list> | <list> |

## Findings backlog

| id | severity | lens | status | title |
|----|----------|------|--------|-------|
| <id> | <severity> | <lens> | <status> | <title> |

## Finding detail — <id>

Exactly one detail block per backlog row (required).

- **What:** <what>
- **Why:** <why>
- **How (planned):** <how-planned>
- **How (done):** <how-done>
- **Resolution / blocker:** <resolution-or-blocker>
- **Evidence:** <evidence> (or `fixed (unverified — fixer oracle only)` for batch-2
  fixed findings lacking independent Presmoke coverage; or unverified)
- **Status:** open|logged|fixed|deferred|deferred_overflow|blocked-on-Matt|cancelled
- **Primary path:** <absolute path> (first Paths/What cite; recorded at merge if missing)

**`presmoke-failed` minimum schema (when used):** id = `presmoke-failed` or
`working-presmoke-failed` or `<first-requested-lens>-presmoke-failed`; severity MEDIUM;
status `open`; sits **outside** the 12-slot keep count (never `deferred_overflow`).

## Phase log

One block per phase (not Round). Parent emits **Phase report** after each phase.

### Phase A — parallel find · lenses=<list>

- **Found:** <h>H / <m>M / <l>L — <ids>
- **Coverage:** <per-lens floor summary>
- **Tools used:** <aggregate>
- **Tools missing:** <aggregate>
- **Merge:** <dedupe notes>
- **Plan update:** <step ids touched>

### Phase B — fix batch 1

- **Fixed:** <id — one line each; `none`>
- **Deferred / blocked:** <id — reason; `none`>
- **Evidence:** <oracle tails>
- **Open after batch:** <h>H / <m>M
- **Plan update:** …

### Phase C — parallel recheck · lenses=<touched>

- **Recheck:** <id status table>
- **New findings:** <ids or none>
- **Plan update:** …

### Phase D — fix batch 2 (optional)

- Same fields as Phase B when budget allows.

### Phase final — presmoke / psynth / stop

- **Next:** green | budget-stop | stop-early (Matt blockers)
- **Skipped lenses:** <list + reason on budget-stop>
- **Green:** Y|N — <reason>
- **HARD:** stop-early due Matt-only blockers always yields `Green: N` (never Green:Y).
  budget-stop with skipped lenses = `Green: N`. Below-floor on any requested lens =
  `Green: N` (fix and find-only; logged findings do not clear). Plan `status: done`
  does not imply Green:Y.

## Deferred (over budget)

- id — one-line reason parked

## Blockers for Matt

- …
