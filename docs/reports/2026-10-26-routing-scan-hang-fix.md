# Routing scan review repairs — 2026-10-26

Branch: `fix/routing-scan-hang`; reviewed baseline: `040de2a5`.
Review: `/Users/matthewschwartz/.claude/projects/-Volumes-Cloud-Storage-Code-Khipu/jobs/2026-10-26/scan-hang-sol/report.md`.

All four findings were reproduced before the corresponding production edits and repaired.

- **P1, bounded reader:** data-directory pointer resolution and home expansion now execute inside the same killable worker as the cached-report read. Synthetic pointer metadata and read stalls fail closed within the reader deadline.
- **P2, age:** stored snapshots remain current for less than 24 hours, matching nightly reconciliation. Exactly 24 hours, future dates, missing/invalid dates and dates without a timezone are stale. Reads age only the cache; retained hits and skip evidence remain available. The owner projection supplies its observation time.
- **P2, incomplete evidence:** the desktop retains skip reasons, displays the scan time with hits or no hits, and distinguishes stale, incomplete and never-checked snapshots. Oversized files and discovery omissions remain visible. Legacy checked reports carrying skip evidence also display as incomplete.
- **P2, discovery:** unavailable directories produce named skips, ordinary missing optional inputs do not, and the candidate limit produces an explicit incomplete result. Protected directories still receive no metadata reads. Cursor inventory uses the approved immediate project memory roots while retaining traversal failures.

## Verification

Every test invocation ran directly from `packages/cli` with:

```sh
env -u KHIPU_DATABASE_URL HOME=$(mktemp -d) KHIPU_KEYCHAIN=0 PYTHONPYCACHEPREFIX=$(mktemp -d) PYTHONPATH="$PWD:/Volumes/Cloud Storage/Code/Khipu/.python_libs" python3.11 -m pytest -q -p no:cacheprovider <files>
```

No command set `TMPDIR`. `tests/conftest.py` is unchanged; SHA-256 before and after: `cdeaf677cee6cc527acf6e57a6adb3ecfbdcfbdd095b0959a97333fc3374ae8a`.

- Before: `test_routing_scan_review_fixes.py`, `test_routing_scan_deadlines.py`, `test_routing_file_copies.py`: **17 failed, 36 passed**. Both pointer stalls failed with `filesystem stall escaped the refresh deadline`, using a killable outer guard.
- Before desktop fixes: five new mocked rendering cases failed and three existing copy cases passed. Failures demonstrated missing timestamps/skips and retained snapshots incorrectly rendered as never checked.
- After: the same three Python files: **53 passed**. The same desktop selection: **8 passed**, with 108 unrelated cases excluded. These ran through a disposable pytest bridge that copied desktop sources into pytest temporary storage, reused installed dependencies without installation, used a temporary Vite cache and mocked the Tauri bridge. No live application or service was used.
- Desktop TypeScript check and related `test_routing.py`, `test_routing_scanner.py`, `test_routing_scanner_round2.py`, `test_routing_scanner_review.py`: **294 passed** (one TypeScript wrapper plus 293 Python cases).
- Initial disposable harness setup failures omitted `tsconfig.node.json` for Vitest and `dev` fixtures for TypeScript. Both were corrected only in the temporary bridge; the actual checks then ran. No product assertion was weakened.
- Focused backend selector: two runs total; desktop wrapper: three invocations, of which the first collected zero frontend tests; TypeScript: two invocations. No fourth oracle run.
- Root inspected the complete changed-path list and production diff; `git diff --check` passed. Implementation worker ran no test/build oracle. The cached-file-change fixture now supplies its existing synthetic observation time explicitly.

No connected CUA browser was available, so native/pixel visual acceptance remains unverified. Mocked DOM rendering and TypeScript validation passed. No real services, live home/configuration, protected personal data, dependency installation, or push was used. This report records local verification, not deployment.
