# Job-origin correction fix verification

Branch: `feat/owed-job-origin-correction`. Baseline: `a27fb24e`.

All seven findings in the first Astra review were repaired. Every production
repair had a regression observed failing before its implementation. Tests used
synthetic data; Python used the existing hermetic conftest and disposable
PostgreSQL. The conftest remains byte-for-byte equal to the restored baseline,
including its TMPDIR assignment. No command supplied a TMPDIR override.

## Findings and regression evidence

| Finding | Result and coverage |
| --- | --- |
| Terminal authorization | A real synthetic PTY cannot confirm. Only verified app authorization or the existing recorded-prompt direction gate can apply the exact logged request. Missing, stale, job, foreign-session and wrong-hook evidence is refused. |
| Historical ownership | Pre-0035 rows, including an explicit pre-metadata reassignment, are quarantined. Eligibility requires ownership metadata coverage at opening and a historical opening no later than migration 0049. Old stored previews are revalidated. |
| Witness lineage | Conflicting peer lineage vetoes both opening and secondary witness sessions. |
| Chat-directed confirmation | A real recorded synthetic owner prompt authorizes confirmation through the existing gate; the action records its request and prompt identity. |
| Individual undo | CLI and owner actions accept selected IDs. Unselected items stay corrected, later edits stay protected, and correction redo remains refused. Each logged undo retains its actual successful inverse scope before commit. Tests cover skipped selections, separate earlier undos, lost finish, zero restores and logging-failure rollback. |
| Desktop quiet counts | Real App tests cover all-quiet, mixed and linked-ledger cases. Badge totals and Work counts retain quiet work without active Blocking treatment. A separate contrast regression failed at 3.94:1 in dark mode; the final token passes both themes. |
| Pure preview | Success and failure preserve reachability memos. Setup avoids legacy secret migration, directory creation and account recovery. Full parser and cold alias resolution use scoped config snapshots without losing stored aliases or paths; ordinary recovery resumes outside the scope. |

Before/after logs are retained under `/tmp/.codex_owed-origin-*`:

- Auth/evidence: `auth-red.log` and `auth-green.log`; the additional secret,
  Keychain, preview-path and preview-alias logs carry their corresponding names.
- Selective undo and exact inverse scope:
  `/tmp/.codex_owed-origin-undo-evidence/`.
- Desktop behavior and contrast:
  `/tmp/.codex_owed-origin-desktop-evidence/`.

New regression selectors used red and green worker runs plus one root
integration run. Exhausted selectors were not repeated in later gates.

## Root checks

Root logs and exact Python selector lists are in
`/tmp/.codex_owed-origin-evidence/`.

- Broad hermetic Python gate: **567 passed, 1 failed**. The failure was an old
  mocked lost-finish fixture that omitted the newly required successful undo
  scope. The fixture now records that scope; its strict result assertion is
  unchanged.
- Final hermetic follow-up: **217 passed, 22 subtests passed**, including the
  corrected lost-finish case, the full parser/preview regression, config,
  accounts, connection defaults and owner-direction seams. These counts
  overlap the broad gate and must not be added as distinct test totals.
- Final `npm run build`: **passed**.
- `npx vitest run`: **1,110 passed, 4 failed**. All new quiet-count cases passed.
  The four failures were headless Chrome command failures in unchanged
  RoutingCard and RoutingOverview tests. The untouched `a27fb24e` baseline
  reproduced all four (**69 passed, 4 failed** in those two files).
- After the final contrast repair, its root selector passed **2 tests** and
  the desktop was rebuilt successfully. Full Vitest was not repeated.
- `cargo check`: **passed** after staging the documented generated CLI
  resources. The initial attempt failed because that generated folder was
  missing. Generated resources and the dependency symlink are uncommitted.
- `git diff --check`: **passed**.

Rendered verification remains **unverified**. CUA reported no available
browser or native apps and a native service startup failure. Source-level
contrast calculations and jsdom tests do not establish rendered appearance.
No public preview, release, push or deployment was performed.


## Astra second recheck repairs (2026-10-26)

Worker worktree: `/tmp/.codex_owed-origin-fix-worker`; baseline: `2551a5b1`.
The four open findings were reproduced and repaired:

- Machine entrypoints are ineligible even if configuration explicitly trusts
  them. Changing authorization settings (`on`, `off`, `set`) or undoing those
  changes now requires the verified app or a recent eligible recorded prompt.
  A fabricated PTY cannot grant trust, extend a stale window, or undo a prior
  narrowing of the authorization settings.
- Migration times do not establish ownership-writer coverage. Historical rows
  have no supported positive per-row proof of untouched ownership, so all are
  quarantined. The cached-pre-0035 writer regression is excluded. Old stored
  previews are revalidated. Lifecycle fixtures now explicitly inject synthetic
  per-row evidence through a test-only seam; no production migration or policy
  fabricates evidence to retain candidate counts.
- Batches carrying `origin_correction_stamp` require verified app or
  prompt-bound direction for Undo; `terminal`, `cli`, and unverified callers
  are refused. The ordinary batch Undo behavior remains covered unchanged.
- Correction exceptions resolve the durable request before marking the action
  failed. A lost acknowledgement retains exact batch IDs and logged Undo;
  confirmation retry returns those IDs without reapplying. If reconciliation
  is unavailable, the action stays unconfirmed and Undo refuses until the
  durable outcome is available. A rolled-back transaction resolves to failed.
  Tests execute the recovered logged Undo and verify exact rows across two
  projects, not just the absence of a refusal.

Hermetic verification evidence is in
`/tmp/.codex_owed-origin-round2-evidence/`. Exact commands and selectors are in
`red-command.txt` and `green-command.txt`. Both run from `packages/cli`, unset
`KHIPU_DATABASE_URL`, use disposable HOME and bytecode-cache paths with
`KHIPU_KEYCHAIN=0`, and use the disposable PostgreSQL fixture. Each shell
invocation is wrapped in a Python subprocess with a 600-second timeout. No
TMPDIR override was supplied. The conftest blob remains
`c0d94190f0bd708671c42028b3f40d037fdad13a`.

- RED: **21 failed, 251 passed**, 33.67 seconds (`red.log`). Every case in
  `test_owed_origin_recheck.py` failed before production edits. The excluded
  empty-project probe was not added.
- GREEN: **491 passed**, 67.67 seconds (`green.log`). This covers the recheck,
  additional recovery boundaries, all origin lifecycle/preview/selective Undo
  cases, owner turns, owner-direction unit/seams/e2e/MCP, owner Undo/audit
  recovery, ordinary tidy and owner guards.
- The seven RED selectors used their second worker attempt for GREEN; the ten
  added GREEN selectors used one worker attempt. No selector exceeded two.
- After GREEN, the historical/stored-preview regression was tightened to
  restore the captured real production ownership policy rather than a constant
  false substitute. Root verified this final test change in the integration
  gate below. Production code was unchanged after GREEN. The independent
  cached-schema regression also exercised the real policy in GREEN.

### Root integration result

Root inspected the worker delta and integrated exactly the nine assigned files
into `feat/owed-job-origin-correction`. The final hermetic gate ran 59 related
Python test files, including all worker selectors and their authorization,
routing, provenance and recovery consumers. `root-command.txt`,
`root-selectors.json` and `root.log` retain the exact invocation and results:

```text
3 failed, 2006 passed, 11 xfailed, 10 errors, 8 subtests passed in 199.39s (0:03:19)
```

All 21 failing-before recheck regressions and the recovery boundary tests passed
in this final gate. The broad gate is not fully green: all three failures and
ten errors come from `test_provenance_review7.py` using the unchanged fixture at
`tests/test_provenance_forget.py:74`. Its strict migration assertion expects
`["0047_fact_provenance"]`, while migration 0049 already exists on the baseline
and also runs. No assertion was weakened and neither fixture nor migration was
changed in this round.

An untouched detached `2551a5b1` worktree reproduced exactly the same thirteen
failed/error node IDs with the same assertion. Its hermetic invocation and log
are `baseline-command.txt` and `baseline.log`:

```text
3 failed, 2 passed, 3 xfailed, 10 errors in 6.38s
```

The seven original RED selectors reached their three-run cap (worker RED,
worker GREEN, root integration) and were not repeated. The ten additional
worker GREEN selectors used two runs; remaining integration selectors used one,
with only the unrelated baseline-failing file run once more on untouched code.
Final source and scope review passed, `git diff --check` passed, and the
protected conftest still has blob `c0d94190f0bd708671c42028b3f40d037fdad13a`.
No command set TMPDIR. No live database or service, real Khipu configuration,
real Documents directory, API Keys or portfolio data was accessed. No rendered
verification, push, release or deployment was performed in this fix round.
