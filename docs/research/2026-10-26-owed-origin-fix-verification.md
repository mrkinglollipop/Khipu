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
