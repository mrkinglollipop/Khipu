# Explicit owner asks and the one-time reset

Capture no longer creates user-owned commitments from extracted open loops. Agent/assistant commitments, decisions and the work ledger keep their existing paths. Nightly no longer runs commitment close rules or hygiene; mark-stale and other nightly steps remain. Session-start slices omit only the Open commitments block.

File an ask through the existing `khipu_owed_update` tool:

```json
{"action":"ask","requester":"Builder","yes":"use the revised design","no":"keep the current design","episode_id":123}
```

Use a live source episode ID available through capture/search/get. Its project and forgetting lineage are preserved. The resulting sentence is a user-owned question, visible through Owed and Needs you, with no confirm/apply operation. Describing yes/no outcomes does not authorize either outcome. Sign-in and other Khipu-owned needs-you paths are unchanged.

## Reset (not executed by this implementation)

Preview without writing database rows or files:

```sh
khipu owed reset --dry-run
```

Export all readable open owner items, including full original rows, plus an adjacent SHA-256 file (existing files are not overwritten):

```sh
khipu owed reset --export /absolute/path/owner-items.json
```

The output separates proposed closures, explicit asks, and items with positive recent owner-ask evidence. Recent means an owner provenance claim or explicit ownership assignment in the last 30 days; summary text alone cannot prove a real owner request. All explicit asks are retained, including those filed before the reset. New items after export are outside the reviewed set. Forgotten items are excluded by existing fences.

After reviewing the archive, use the existing owner action `owed.tidy` with project `<all-owner-items>` and the archive as `plan`, or the existing terminal/app gate:

```sh
khipu owed tidy --project '<all-owner-items>' --apply /absolute/path/owner-items.json
khipu owed tidy --project '<all-owner-items>' --undo BATCH_FROM_APPLY
```

Apply rechecks owner/ask evidence and fingerprints under row locks. Closures across projects form one transaction and one existing tidy batch. Undo uses the existing tidy behavior: it restores unchanged members and explicitly lists any member edited or forgotten after apply. The archive remains available regardless of later edits. No automatic invocation is installed.

## Readers affected

Owed and its owner counts, Work's Waiting on you list/badges, activity project slices, and prompt recall of open commitments will receive fewer newly inferred owner rows. Existing historical rows stay until an owner-approved reset. The desktop Needs you view can show deliberately filed asks; Owed and Work will show fewer owner rows after reset. No app files or screens were changed or visually tested.

## Implementation validation (M1 Max, 2026-11-05)

No unresolved failures in the final affected-file run. Earlier runs failed: the first found a removed nightly result variable, obsolete owner-capture expectations and a test transaction mistake; the second found four remaining fixture/count expectations. These were corrected before the third run. No further test runs were made.

All commands ran from `/Users/matthewschwartz/.t3/worktrees/Khipu/t3-dc030fe9`, outside `/tmp`. `/tmp/khipu-oracle.sh` was read first; it isolates HOME, disables Keychain and supplies PYTHONPYCACHEPREFIX. `df -h /` preceded every run and showed 74 GiB available each time.

Run 1 (exit 1; 19 failed, 198 passed, 86 subtests passed):

```sh
mkdir -p /tmp/owed-explicit-tests
ROOT="$PWD" PYTHONPATH=/tmp/khipu-yaml TMPDIR=/tmp/owed-explicit-tests python3 -c 'import subprocess; r=subprocess.run(["bash", "/tmp/khipu-oracle.sh", "tests/test_explicit_asks.py", "tests/test_commitments.py", "tests/test_recall_rule.py", "tests/test_jobs.py", "tests/test_owed_tidy.py", "tests/test_mcp_server_owed.py"], timeout=300); raise SystemExit(r.returncode)'
```

Runs 2 and 3 used the same exact command below. Run 2: exit 1; 4 failed, 329 passed, 86 subtests passed, 23.21 seconds. Run 3: exit 0; 333 passed, 87 subtests passed, 20.23 seconds.

```sh
ROOT="$PWD" PYTHONPATH=/tmp/khipu-yaml python3 -c 'import subprocess; r=subprocess.run(["bash", "-x", "/tmp/khipu-oracle.sh", "tests/test_explicit_asks.py", "tests/test_commitments.py", "tests/test_recall_rule.py", "tests/test_jobs.py", "tests/test_owed_tidy.py", "tests/test_mcp_server_owed.py", "tests/test_owner_needs_you.py", "tests/test_nightly_jobs_bounds.py"], timeout=300); raise SystemExit(r.returncode)'
```

Changed tests:

- `test_commitments.py`: owner-shaped summaries now expect no insertion; dedup tests use assistant work so they continue exercising dedup rather than the removed owner-capture behavior.
- `test_jobs.py`: removed two step expectations and two calls; hygiene is now asserted absent, including its backup/model calls.
- `test_recall_rule.py`: the slice must omit the heading and commitment text while retaining the other sections.
- New `test_explicit_asks.py`: owner-shaped extraction creates no owner row; a filed ask appears as user-owned in Owed and in Needs you with no settlement controls; authority arguments are rejected; dry-run changes neither rows nor files; export SHA matches; ungated apply is refused; a protected recent ask and explicit asks stay open; a late ask stays outside the exported set; one cross-project batch closes the reviewed historical rows and undo restores their original fields.

Existing tidy, MCP Owed, Needs you and nightly bounds tests were also run. `git diff --check` passed.

Not checked: full suite, desktop rendering, live hub behavior, real-data counts, real archive/reset execution, terminal/app interactive confirmation, or a full nightly wall-clock benchmark. The reset/export command was exercised only against throwaway PostgreSQL fixtures. Recent-ask detection is deliberately limited to stored positive evidence; it cannot recover unrecorded owner intent from historic summaries. The removed close-rules pass also hosted its provenance/backfill preparation; that nested preparation no longer runs through this nightly pass. Standalone helpers remain available.
