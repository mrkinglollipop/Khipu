---
name: iterate
description: >-
  Deep iterate: parent-owned parallel finders, serial fixer, lenses
  working/bugs/gaps/polish/product, per-lens budgets, tool utilization.
  Use for /iterate. Not session-audit (claims/correctness). Model-agnostic.
---

# Iterate

**Mode:** fix authorized by default when `/iterate` invoked for **existing surface**
plus novel run-log paths under the contract_guard allowlist. Trailing `find-only` /
`audit-only` = find+log only (no fix writes except run log).

**vs session-audit:** Iterate = constructive improve on existing surface. Session audit
= claims/lies/process correctness.

## 0. Parse trailing text

| Token | Meaning |
|-------|---------|
| (lens omitted) | Default lens set = **`working` only** |
| `lens=<name>` or bare `polish` / `bugs` / `gaps` / `working` / `product` | Single lens |
| `lenses=all` | `working` → `bugs` → `gaps` → `polish` → `product` |
| `lenses=a,b` | Explicit list, **left-to-right CSV order** |
| `find-only` / `audit-only` | Find-only mode |
| `resume=<path\|run-id>` | Resume run log |
| `scope=<subpath>` | Narrow to an **existing directory** subtree (repo-relative;
  default `scope=.` / repository root). File-valued scope is invalid. |
| path / repo name | Force `$REPO_ROOT` / target |

On resume, the run log's recorded `Mode:` wins; if trailing text conflicts (for
example `find-only` against a fix-mode log), **stop before dispatch** and ask.

**Scope selection ordering (HARD):** see §2 — do **not** resolve `$SCOPE_ROOT`,
perform scoped cheap-reads, create/append the run log, seed TodoWrite, or dispatch
until run-mode and the normalized repo-relative scope subpath are selected per §2.
On resume, recorded `Scope:` wins over trailing `scope=`; conflicting trailing
`scope=` → **stop and ask**. Legacy `schema_version: 2` logs lacking `Scope:` →
interpret as `.` (repo root); if trailing `scope=` is non-`.` and conflicts with
that interpretation, **stop** before reads/log/dispatch — do not silently broaden
or narrow. Record legacy interpretation on next parent write when proceeding.

## 1. Resolve host + `$REPO_ROOT`

1. Host from tools present (Task → cursor; spawn_subagent → grok; else claude). Read
   `.cursor/dispatch-settings.yaml` → `hosts.<host>.iterate_finder` /
   `iterate_fixer`.
2. `$REPO_ROOT` = `git rev-parse --show-toplevel` for the **target**.
3. Scope floor: that root only — never whole Cloud Storage.

### 1b. Resolve and validate `$SCOPE_ROOT` (HARD)

Apply **only after** §2 steps 1–3 (run-mode + normalized scope subpath selected).
Do **not** resolve `$SCOPE_ROOT` or perform scoped reads before that selection.

1. Normalize subpath: repo-relative, non-empty; reject absolute paths and bare
   `..` segments before resolution.
2. `$SCOPE_ROOT = realpath($REPO_ROOT/<subpath>)` — path must **already exist**;
   realpath result must be a **directory** (file-valued scope is invalid).
3. **Containment (HARD):** `$SCOPE_ROOT` must equal `$REPO_ROOT` or be a strict
   subdirectory of it (path-component-aware ancestry check after realpath; never a raw
   string-prefix check). Reject missing paths, non-directory targets,
   symlink/`..` escapes outside `$REPO_ROOT`, or empty subpath.
4. On invalid scope: **stop before scoped cheap-read, run-log creation, TodoWrite
   seed, or any dispatch**; report the reason (missing, not-a-directory, escape,
   absolute, empty). `scope=.` is valid (`$SCOPE_ROOT` = `$REPO_ROOT`).

**Scope narrows (HARD):** target inventory, cheap-read primary workflow,
finder coverage/exercise, fixer edits, `touched_paths`, and scoped
verification/Presmoke surfaces — all bounded to `$SCOPE_ROOT`. **`$REPO_ROOT`
remains** the git/security boundary, run-log location parent, and finalize
`git status --porcelain` cwd. Dirty paths outside `$SCOPE_ROOT` are reported as
pre-existing/out-of-scope; parent never edits them.

**TodoWrite (host mapping):** the contract term "TodoWrite" means the host's actual
todo tool (Cursor todo tool; grok equivalent). Same semantics on both hosts in use.

## 2. Parent before dispatch (HARD)

**Parent owns the mission.** Parent seeds run log, fans out finders, merges, fixes,
rechecks, and finalizes in **one turn** (Parent fan-out / multi-wave one turn).
**Parent updates TodoWrite live between phases.** Hired finders/fixers never mutate
parent TodoWrite or the run log (**durable-write ban** on finder; fixer returns
facts only).

**Mission ceilings:**

- `schema_version: 2`
- max finder dispatches = `2 × N_requested_lenses` in fix mode (Wave A find + Wave B
  recheck per lens); **find-only** max = `N_requested_lenses` (Wave A only)
- max fixer batches = **2**
- max oracle runs = **3 per fix batch**
- Per-lens max **2** passes (find + recheck) in fix mode; find-only = **1** pass/lens

**Resume safety (HARD):** Resume only when explicit `resume=` resolves to a log, or
when **exactly one** matching `status: in-progress` log exists for `repo_root:`.
Resume requires `schema_version: 2` in the candidate log. Missing or lower version →
treat as unreadable (stop, surface path, ask for explicit disposition). Never silently
upgrade an old log.
Match counts:

- **0** matching in-progress → `run_mode=fresh`.
- **1** matching in-progress → implicit `run_mode=resume` that log.
- **2+** matching in-progress → **stop before dispatch**; ask Matt to choose with
  explicit `resume=<path|id>` and list candidate IDs/paths.

Never reopen or mutate a `status: done` run implicitly.
Do not fall back to latest-mtime completed logs.

On `run_mode=resume`, parent reconstructs consumed finder/fixer budget from Phase log
rows before dispatching. **Finder consumption (unique):** each finder hire —
successful, failed, denied, or incomplete/partial — consumes **1** finder dispatch
toward the ceiling. Wave A with N lenses = **up to N** consumed if all lenses were
hired (including partials); "Wave A find = N_lenses dispatched" means the **count of
finder dispatches logged for that wave** (≤N), not a flat N when some lenses were
never dispatched. Wave B recheck = count of recheck finder dispatches logged. Fixer
batches: Phase B/D block present counts. **Partial phase (definition):** a phase/hire
is partial when a Phase log block exists but the hire returned incomplete schema, was
denied, or parent recorded recovery without a normal completion count — that hire
still counts as **1**. Remaining budget = ceiling minus consumed.

**`lenses=all` / per-lens budget honesty (HARD):** `lenses=all` requests five lenses
(`working` → `bugs` → `gaps` → `polish` → `product`) with **per-lens max 2 passes**
(find + recheck). State plainly in the initial Mission plan. A lens that exhausts its
second pass marks the plan step `complete` with evidence `per-lens budget reached` even
if H/M remain. Mission may end in `budget-stop` when finder/fix budgets exhaust —
name skipped lenses and reason; **not** Green:Y.

**Branch before dispatch (HARD — ordering):**

1. Parse lenses + mode + resume token + trailing `scope=` (do **not** resolve
   `$SCOPE_ROOT` yet). Resolve `$REPO_ROOT` only (§1).
2. Decide `run_mode` **without scoped reads or dispatch** (Resume safety above):
   - **0** matching in-progress → `run_mode=fresh`.
   - **1** matching in-progress → `run_mode=resume` that log.
   - **2+** matching in-progress → **stop and ask** Matt (no scope resolution).
3. **Select normalized repo-relative scope subpath** (still no `$SCOPE_ROOT`
   resolution, no scoped cheap-read, no run-log write, no TodoWrite seed, no
   dispatch):
   - **`run_mode=fresh`:** trailing `scope=` if present, else `.`.
   - **`run_mode=resume`:** read recorded `Scope:` from the candidate log first —
     recorded Scope **wins**. Trailing `scope=` that conflicts → **stop and ask**.
     Legacy logs without `Scope:` → interpret as `.`; trailing non-`.` `scope=`
     that conflicts → **stop** (no silent broaden/narrow). Record legacy
     interpretation on next parent write when proceeding.
4. **Resolve and validate** the selected subpath into `$SCOPE_ROOT` (§1b). On
   invalid → stop before scoped cheap-read, run-log, TodoWrite, or dispatch.
5. **Only after successful validation:** scoped cheap-read primary workflow within
   `$SCOPE_ROOT`; then:
   - **`run_mode=fresh`** — create plan shell (`schema_version: 2`), one row per
     requested lens + Presmoke (always seed in fix mode; omit Presmoke row only in
     `find-only`) + Psynth; seed durable run log; parent TodoWrite; print
     `## Mission plan`.
   - **`run_mode=resume`** — read EXISTING Mission plan; reconstruct TodoWrite and
     consumed budgets; do not overwrite plan rows. Run-log `Mode:` wins over trailing
     text conflicts (stop and ask if conflict). Scope conflicts resolved in step 3.
6. Proceed to Wave A dispatch (run log path known; fresh log seeded in step 5).

**Incomplete / crashed hire recovery (HARD):** If a finder/fixer fails, is denied, or
returns incomplete schema, parent inspects the known run-log path.

- **Readable:** surface partial plan/phases + `resume=<path|id>`; leave
  `status: in-progress`.
- **Unreadable:** unverified failure; do not fabricate Green or findings.

## 3. Target class detect

**Repo-level signal detection (HARD):** class signals below are detected at
`$REPO_ROOT` (project files may sit outside a narrow `$SCOPE_ROOT`). Inventory,
exercise, fixer edits, `touched_paths`, and Presmoke surfaces remain bounded to
`$SCOPE_ROOT` (§1b Scope narrows). Do not expand inventory to the whole repo
solely because class signals were found outside scope.

| Class | Signals | Verify lane |
|-------|---------|-------------|
| iOS | xcodeproj / xcworkspace / project.yml | xc-mcp / ios-oracle |
| Mac | macOS scheme / Mac target | Mac run destination |
| Electron | electron in package.json | electron-oracle / CDP |
| Web | next/vite/react scripts | browser / Playwright |
| Aegis | Code/grok-build-harness | smokes + desktop consent |
| Loom/harness | loom*, .cursor-plugin hooks | smokes + desktop consent |
| Biblical | Code/biblical-system | domain smoke + UI |
| Manna | Code/Manna | domain smoke + UI |
| Generic | else | README/CLAUDE.md + workflow |

Mis-detect: re-read signals once on immediate smoke fail; else log assumed class.

## 4. Run log

Template: `references/run-log-template.md` (`schema_version: 2`).

Fresh runs record **`Scope: <repo-relative> (<absolute scope_root>)`** in the log
header (default `.` / `$REPO_ROOT`). Run-log path stays under `$REPO_ROOT`, never
inside `$SCOPE_ROOT` alone.

**Path order** (`mkdir -p` first):

1. `$REPO_ROOT/.cursor/state/iterate/<run-id>.md`
2. `$REPO_ROOT/outputs/iterate/<run-id>.md`
3. Claude Mac: `/Volumes/Cloud Storage/Claude/outputs/iterate/<run-id>.md`
4. Cloud/unknown: `/tmp/iterate/<run-id>.md`

`run-id` = `YYYYMMDDTHHMMSSZ-<short>` UTC.

Parent appends after each **phase**; mission summary prints log path.

### Mission plan lifecycle (HARD)

Mission plan = lens order + gates (not speculative fixes). Run-log `## Mission plan`
is SSOT. Parent TodoWrite mirrors plan rows only.

**Plan statuses:** `pending`|`in_progress`|`complete`|`blocked`|`skipped` — never
`capped`.

**Finding statuses:** `open`|`logged`|`fixed`|`deferred`|`deferred_overflow`|`blocked-on-Matt`|`cancelled`.
`blocked-on-Matt` is never a plan-step status.

**Two separate vocabularies (HARD — never mix):** plan-step statuses apply only to
`## Mission plan` rows; finding statuses apply only to backlog / detail blocks.
`blocked-on-Matt` is a **finding** status and is never a plan-step status.

**Plan status → parent TodoWrite mapping (exact):**

| Plan status | TodoWrite status |
|-------------|------------------|
| `pending` | `pending` |
| `in_progress` | `in_progress` |
| `complete` | `completed` |
| `blocked` | `cancelled` |
| `skipped` | `cancelled` |

### Finding TodoWrite protocol (HARD)

Parent Todo list = plan steps **plus** HIGH/MEDIUM finding items. Finding items are **never** `in_progress`. The sole `in_progress` TodoWrite item is the **current
phase** plan step. Batch plan-step handoff and finding updates atomically in **one** TodoWrite call.

| Finding status | TodoWrite status |
|----------------|------------------|
| `open` | `pending` |
| `fixed` | `completed` |
| `blocked-on-Matt` / `deferred` / `deferred_overflow` / `cancelled` / `logged` | `cancelled` |

**Plan finalization (HARD — exit path, before Mission summary):** after the final
Phase report, **terminalize every plan step by stop reason** — no plan step remains `pending` or `in_progress`. Then set run-log top-level `status: done`.

Never invent plan status `capped`. Use `per-lens budget reached` evidence on
`complete` when a lens hits its second pass with open H/M.

## 5. Lens loop

Alias for the parent multi-wave pipeline (finders are no longer inline).

## 5b. Parent multi-wave pipeline (one turn)

```text
Wave A: parallel iterate-finder (one dispatch per requested lens, pass=find)
  → parent merge + dedupe → Phase report A
Fix batch 1: iterate-fixer (merged backlog) → Phase report B
Wave B: parallel iterate-finder (pass=recheck, touched lenses only)
  → merge → Phase report C
Fix batch 2 (optional, budget remaining): iterate-fixer → Phase report D
Finalize: Presmoke → Psynth → Mission summary
```

**Empty backlog (HARD):** if the merged Wave A backlog has zero open H/M in fix mode,
skip fixer and Wave B; if Presmoke row was seeded (fix mode) and Paths edited is empty,
terminalize Presmoke as `skipped` with evidence `empty-backlog` / `zero-edit` before
finalize; Green evaluation unchanged (coverage floors still gate).

**Find-only mode (HARD):** skip fixer and Wave B recheck/fix; findings Status=`logged`
(not `open`); no fix writes. Run log still written. Finder budget = `N_requested_lenses`
only. Never seed a Presmoke row in find-only. **Green in find-only:** zero Status=`open`
H/M is automatic when all findings are `logged` **only for Green rule #1**; still require
every requested lens plan step terminal — Mission summary must state `find-only` (audit
surface) so Green:Y is not mistaken for a clean fix mission. **HARD — below-floor still blocks:**
if any requested-lens finder return has Coverage vs floor = `below-floor` (or `partial` on a
required floor), final Mission summary **must** be `Green: N` naming that coverage miss —
even when findings are Status=`logged`. Logged below-floor findings do **not** satisfy Green.

### Finder structured return schema (HARD)

Each finder returns the block defined in `.cursor/agents/iterate-finder.md` (plugin
SSOT: `.cursor-plugin/agents/iterate-finder.md`). Parent validates coverage inventory
fields, **Tools used**, **Tools missing**, findings table, recheck table (Wave B).

**Dispatch scope (HARD):** every finder/fixer hire passes **`repo_root`** (`$REPO_ROOT`)
and **`scope_root`** (`$SCOPE_ROOT`). Finders inventory/exercise only inside
`scope_root`. Fixers may edit only paths inside `scope_root`; defer any required
outside-scope edit. Inline read-only blast-radius caller discovery may inspect
outside scope but cannot expand edit scope.

**Task enum escape (HARD):** if host Task rejects `subagent_type: iterate-finder` /
`iterate-fixer`, dispatch `escape_type` from `dispatch-settings.yaml`
(`generalPurpose` on cursor) with the agent md as ROLE brief — do not abort Wave A.

### Fixer structured return schema (HARD)

Each fixer returns the block defined in `.cursor/agents/iterate-fixer.md`. Parent
copies paths/IDs/oracle runs into the Phase report.

### Merge dedupe rules

See `references/finder-common.md`:

1. Same path + same defect → one finding; highest severity wins.
2. Same title, different paths → separate IDs.
3. Cross-lens overlap → keep both when lenses differ; link in detail.
4. Parent assigns durable merged IDs `<lens>-<sequence>` at merge (e.g. `working-1`).
5. **Primary path:** first absolute path in the finding's Paths / Finding detail What
   cite; if none, first path in Wave A finder return `paths` column for that ID. Parent
   records it in Finding detail at merge if missing.

**Mission merged open H/M cap (HARD):** parent fills at most **12** keep-slots of
merged Status=`open` HIGH/MEDIUM findings (tie-break: severity HIGH before MEDIUM, then
requested-lens order, then durable merged ID `<lens>-<n>` ascending). Overflow → status
`deferred_overflow` under run-log `## Deferred (over budget)` (see `finder-common.md`).
Cap does not apply to product `logged` proposals. **HARD — slot accounting:** any
finding whose id matches `*presmoke-failed` sits **outside** the 12-slot keep count
(does not compete for keep-slots), never `deferred_overflow`, stays Status=`open` until
Presmoke re-pass or Matt cancel — Green rule #1 still fails via open H/M. After Wave B
merge of recheck returns (including new regression IDs), parent must re-apply the same
12-cap + tie-break + outside-slot exemption.

**Below-floor coverage escalation (HARD):** if any requested-lens finder returns
Coverage vs floor = `below-floor` (or `partial` on a required floor), parent must
(a) ensure a MEDIUM+ `below-coverage-floor` finding is in the merged backlog **and**
(b) refuse Green — final Mission summary **`Green: N`** with that coverage miss named.
Never silently Green past a below-floor return. In **find-only**, (a) yields
Status=`logged` (not `open`); (b) still applies — logged findings do **not** clear
the below-floor Green block.

### Fixer batches (HARD)

Parent hires `iterate-fixer` serially (max **2** batches). Each batch receives merged
`open` HIGH/MEDIUM backlog (LOW if budget). Fixer returns paths edited, IDs fixed,
oracle runs, remaining open — parent logs to Phase report. **product** findings are
never sent to fixer (**product lens never blocks Green**).

A finding may be marked `fixed` only with passing oracle evidence (exit 0 or
class-appropriate pass) or an explicit `unverified — would verify by X` label.
**Wave B recheck downgrades unverified-fixed batch-1 findings to `open`** when evidence
is still absent; batch-2 findings follow batch-2 labeling (label only, never reopen).

**Touched lenses / `touched_paths`:** `touched_paths` = union of absolute paths in
fixer batch-1 return `Paths edited` (paths under `$SCOPE_ROOT` only). Touched lens =
the lens of any finding whose status changed in fix batch 1, plus any lens whose
coverage-floor surfaces intersect those `touched_paths`. Wave B rechecks touched
lenses only.

### Phase report (mandatory, every phase)

```markdown
### Phase <label> — <wave|fix> · lenses=<list>
- **Found / Fixed / Recheck:** <counts + ids>
- **Coverage:** <floor summary>
- **Tools used:** <list>
- **Tools missing:** <list>
- **Evidence:** <oracle / smoke / a11y>
- **Open after phase:** <h>H / <m>M
- **Plan update:** completed=<ids> · current=<id> · next=<id>
- **Next:** <next phase> | green | budget-stop | stop-early
```

### Tool utilization contract

Every finder pass must attempt class-appropriate tools (`references/finder-common.md`
tool matrix) before file-only findings. Unavailable tools → **Tools missing** +
explicit degrade — never fake VISUAL PASS.

### Per-lens budget

Each requested lens gets at most **2** finder passes in fix mode (find + recheck). After
recheck on pass 2, mark lens plan step `complete` with `per-lens budget reached` if
H/M remain (`open` in fix mode; stay `logged` in find-only). Advance to next lens or
`budget-stop` when finder budget is exhausted (`2 × N` fix mode; `N` find-only).

### Presmoke terminalization (HARD)

**Fix mode: always seed** a Presmoke plan row. Omission of the row at plan creation is
allowed **only** in `find-only` mode (never seed). **Do not** use `find-only` or
`not applicable` as finalize skip evidence.

At finalize: if zero Paths edited across all fixer batches (or empty-backlog with no
edits), mark Presmoke `skipped` with evidence `zero-edit` / `empty-backlog` — do **not**
omit the row after seed.

Presmoke **runs** when `mode=fix` AND any fixer batch returned non-empty `Paths edited`.

**Presmoke procedure AC** (defines “surfaces exercised by Presmoke”):

- When `working` was among Lenses requested: re-run the working journey checklist from
  the Wave A `working` finder coverage inventory (`journey_step: true` rows), using the
  class verify lane from §3 / finder-common tool matrix. Record exercised surfaces in
  Presmoke Evidence.
- **Missing Wave A working inventory (HARD):** When `working` was requested but Wave A
  produced no `journey_step: true` rows (denied hire, budget-stop, incomplete, or empty
  inventory): treat Presmoke as **fail** — mark Presmoke `complete` with fail evidence
  `working-inventory-missing`, AND emit/keep Status=`open` MEDIUM finding per the
  `presmoke-failed` schema. Do **not** vacuous-pass.
- When `working` was **not** requested: re-run the class verify lane for the cheap-read
  primary workflow (§2) within `$SCOPE_ROOT` only — no journey checklist. Record
  command + exit + surfaces.
- Pass = class-appropriate success (exit 0 / VISUAL PASS with a11y evidence / smoke
  pass). Fail = non-zero / BLOCKED without degrade reason / journey step fail /
  `working-inventory-missing`.

When Presmoke runs and fails: mark plan step `complete` with fail evidence (command +
exit + tail, or `working-inventory-missing`), AND emit/keep Status=`open` MEDIUM
finding per the `presmoke-failed` schema. Do **not** use `skipped` for a failed run.

**`presmoke-failed` Finding detail (minimum schema):** id = `presmoke-failed`, or
`working-presmoke-failed` if `working` was requested, else
`<first-requested-lens>-presmoke-failed`; severity `MEDIUM`; lens = `working` if working
was requested, else first requested lens; status `open`; What: Presmoke failed; Why:
independent re-smoke of primary workflow failed (or `working-inventory-missing`);
Evidence: command + exit + tail (or token `working-inventory-missing`); Paths: primary
workflow entry path if known, else `$REPO_ROOT`. **HARD — slot accounting:** id matching
`*presmoke-failed` sits **outside** the 12-slot keep count — never `deferred_overflow`,
remains Status=`open` until Presmoke re-pass or Matt cancel. Green #1 still fails via
open H/M.

**Batch-2 labeling (HARD):** at finalize after Presmoke (or after skipping for
`zero-edit` / `empty-backlog`), any finding marked `fixed` in fix batch 2 that lacks
independent Presmoke coverage evidence gets Finding detail Evidence + Mission summary
Evidence label `fixed (unverified — fixer oracle only)` — even when a passing
fixer-oracle was recorded. Status stays `fixed`; no reopen. Label is applied by parent
at finalize, **never** at batch-2 mark time.

**Independent Presmoke coverage evidence (AC at finalize):** a batch-2 finding is
Presmoke-covered only when (1) Presmoke status=`complete` with pass evidence AND (2)
the finding's lens is `working` **or** the finding's primary path sits on the surfaces
recorded as exercised by Presmoke under the Presmoke procedure AC. All other batch-2
`fixed` findings get the Evidence label. Wave B may still downgrade unverified-fixed
**batch-1** findings to `open` when evidence is absent.

### Psynth sequence (HARD — Green, budget-stop, and stop-early)

Explicitly transition `Psynth` from `pending` **or** `in_progress` → set `in_progress`
while constructing final synthesis → `complete` immediately before **Plan outcome**.
Then set run-log `status: done`, Updated, Green / stop reason.

### Finding detail completeness (HARD)

Every backlog row needs `## Finding detail — <ID>` with What / Why / How (planned) /
How (done) / **Resolution / blocker** / Evidence / Status.

## 6. Green / stop

**Green requires ALL of:**

1. Zero Status=`open` HIGH/MEDIUM (product `logged` items excluded —
   **product lens never blocks Green**). Find-only: findings are `logged`, so #1 is
   satisfied by mode; still label Green reason `find-only` (not a fix clean).
2. Every requested lens plan step terminal (`complete`|`skipped`|`blocked`).
3. No fixable work remains within budgets (find-only: no fixer hired ⇒ #3 holds).
4. **No requested-lens below-floor coverage miss** — if any requested-lens finder
   return had Coverage vs floor = `below-floor` (or `partial` on a required floor),
   **`Green: N`** naming the miss. Applies in fix mode **and** find-only (logged
   `below-coverage-floor` findings do **not** satisfy this rule).

**budget-stop:** finder or fixer ceiling hit; name skipped lenses; Green:N.

**Stop-early:** remaining items are Matt-only blockers.
**stop-early due Matt-only blockers always yields `Green: N`** — never Green:Y.

**Green line rules (HARD):** stop-early due Matt-only blockers always yields `Green: N`.
budget-stop with skipped lenses = N. Below-floor on any requested lens = N (fix or
find-only). Plan `status: done` does not imply Green:Y.

## 7. Mission summary (parent, once)

After final Phase report + plan finalization:

```markdown
## Iterate mission summary
- **Target:** <repo> · class=<class> · **Scope:** <repo-relative> (<absolute scope_root>)
- **Phases:** <list> · **finder dispatches:** <n> / <2×N> · **fixer batches:** <n> / 2
- **Lenses run:** <list> · **skipped:** <list or none>
- **Green:** Y|N — <reason>
- **Fixed:** <count> — <ids>
- **Product proposals (logged):** <count> — <ids>
- **Evidence:** <oracles / smokes / unverified>
- **Dirty paths (in scope):** <git status --porcelain paths under $SCOPE_ROOT, or none>
- **Dirty paths (out of scope / pre-existing):** <paths under $REPO_ROOT outside
  $SCOPE_ROOT, or none> — never edited by this mission
- **Run log:** <absolute path>

## Findings summary
| ID | Severity | Lens | Status | Finding | Resolution / blocker | Evidence |

## Plan outcome
| Step | Lens | Status | Finding IDs | Evidence |
```

Include **every** finding. Product proposals appear with Status=`logged`.

Fixer never commits, branches, or pushes. At finalize parent runs
`git status --porcelain` under `$REPO_ROOT`, partitions dirty paths into in-scope vs
out-of-scope/pre-existing, and lists both in the Mission summary; defers shipping
to the existing ship flow.

## FAQ

- Parent hires `iterate-finder` (readonly) and `iterate-fixer` (writable) per
  `dispatch-settings.yaml`; legacy `iterate` agent is thin redirect to fixer if
  escape-hired — parent still owns loop.
- `iterate-finder`: **durable-write ban** — no run log, no TodoWrite, no nested Task.
- Contract_guard: `/iterate` authorizes existing-surface fixes + novel run-log paths.
- Hire failure: report tool, type, deny reason; parent may solo finder/fixer roles.
- **`scope=<subpath>` (#14) evidence honesty:** Scope validation is a **parent-executed
  prompt contract** (§0/§1b/§2 + finder/fixer HARD rules). `smoke_harness_skills.sh`
  asserts **content markers** only — not behavioral parser/hook execution of invalid
  scope rejects. Marker smoke PASS ≠ runtime/behavioral scope verification. A
  historical find-only run log does **not** verify `#14` Scope behavior.
