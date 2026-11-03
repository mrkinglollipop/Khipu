# Routing observation retention review fixes — 2026-11-03

The five findings are fixed. Final broad gate: **3,489 passed, 30 skipped, 8 xfailed, zero failures and errors**, in 803.22 seconds. All three broad invocations have been used. No further broad run was made.

Runtime/test gate commit: `3e2283a04f310cf801867ea701698bfb7503a00a`. After this gate, one extra blank line at the end of the verification test was removed; complete AST equality was checked. The remaining post-gate change is this report.

## Fixes and regression evidence

| Concern | Final behavior and regression |
| --- | --- |
| Owner priority end | The stored owner end is preserved verbatim. The fold recognizes a confirmed reset only from cli/checked probe or check evidence with an explicit `reset_at`, or a cli/checked provider-dated limit marked `message_time` with a non-default end. Cleared or dismissed evidence is excluded. Default durations, inferred ends, and ordinary runner limits do not shorten it. `test_routing_priority_level_limit.py` reproduced the truncation before the fix and now passes. The existing stored-level test now requires the owner's original end, while retaining the confirmed-reset view assertions. |
| No stated level end | The existing stored schema requires `claim`, `as_of`, and a valid `until`. Omitted/null ends remain rejected; no lifetime is invented. Open-ended priority entries remain supported. |
| Crash before replacement | A single inert, timestamped generation marker is committed by the same atomic log replacement as its retained events. Archive headers link generations; SHA-256 checks validate retained and reconstructed content. Orphan archive existence and matching prefixes cannot authorize reconstruction. Both `A,B,C` and identical-prefix `A,B,B` crash probes reproduced duplication before their fixes. Sequence zero keeps metadata below every legacy upload cursor; a separate red/green regression verifies that a retained owner note reaches a replica when replication is enabled later. |
| Partial/corrupt gzip | Archives are written and fsynced under an undiscoverable temporary name, atomically published, and the directory fsynced. Failed and abandoned temporary files are cleaned under the writer lock. Corrupt gzip/header/content produces an unavailable-history warning while the current log and other reconstructable generations are returned. CRLF bytes reconstruct exactly. Red/green tests cover failure before publication, fresh leftover temporary files, corruption, and byte preservation. |
| Doctor severity | Absolute bounds remain yellow at 3,000 lines / 2 MiB and red at 4,000 lines / 3 MiB. Low compaction gain cannot downgrade red. Doctor counts the owner's protected lines and explains that retaining this history is not a malfunction. Line- and byte-bound regressions failed before the change and pass now. |
| Archive bytes | Archives have an absolute 64 MiB compressed-file budget. Oldest archives expire first, including recent ones when necessary. Retention expiry is recorded before deletion and survives later compactions; reconstruction reports unavailable older observations. The regression proves that owner lines never enter an archive and all remain in the log even when the byte budget removes every archive. |

## Requested verification

| Claim | Result |
| --- | --- |
| Compacted/full fold equality on fuzz seeds | Verified: 30 sample seeds (one empty fixture skips) and 17 observation seeds; every eligible sample compaction and all eligible observation profiles reconstruct their original bytes through `full_log()`. Existing strict fold-equality assertions remain. Seeds 224, 545 and 816 exposed a reset-retention mismatch after excluding guesses; retention now uses the fold's confirmed-reset predicate and all three pass. |
| Repeated-compaction order | Verified: the streaming multi-compaction test reconstructs every original event in exact order. Generation matching is independent of filename timestamp order. |
| Concurrent append and two compactors | Verified: two real compaction calls race after planning, a locked owner append survives exactly once, one replacement commits, and the stale second plan skips. |
| Historical reader and replicas | Verified: the tests load exactly `git show 1613a16d:packages/cli/khipu/routing.py`. The historical reader reports zero unreadable lines and equal folds on the compacted log. Both current and historical readers compare complete pure folds through the in-memory hub/disposable-Mac replication fixture; subsequent owner writes arrive. This is source-level historical-reader compatibility using current helper modules. |
| Day 120 figures | Verified using `tests/routing_compact_synth.py`, `Stream`, seed 7, two account keys, one runner, four lanes, 120 days and the default configured owner rate of 3/day. The fixture is compacted once after generating the full interval. See measurements below. |

## Day-120 measurements

- Original: **165,159 lines**, 107,127,544 bytes.
- Compacted: **1,864 lines**, 958,762 bytes, including one generation metadata line.
- Cold read, parse, inputs and fold: **55.28, 55.33, 54.69 ms** across three readings.
- Compressed archive: **5,723,766 bytes**.
- Owner lines actually generated: **382**, or **3.1833/day**. The generator's Bernoulli rate is 3/1440 per minute; it does not write exactly three lines every day. Thus the day-290 runway is a fixture-dependent projection, not a measured production runway. No live owner history was read.

## Oracle environment and attempts

Python 3.11.15, pytest 9.1.1, psycopg 3.3.5, with repository-vendored `.python_libs`. Every oracle had a disposable HOME, Khipu data/capture paths and Keychain disabled. The repository conftest clears credentials and identities and prohibits production PostgreSQL connections. The gateway fixtures use disposable PostgreSQL on loopback. Each broad run had a 1,200-second subprocess timeout.

1. Initial sandbox diagnostic: 3,320 passed, 18 failed, 147 errors, 30 skipped, 8 xfailed, one deselected replica probe. Loopback `socket.bind(("127.0.0.1", 0))` failed with `PermissionError: [Errno 1] Operation not permitted`; three fuzz failures also required the confirmed-reset retention fix. The replica probe's original view-revision comparison was replaced with a strict comparison of complete pure folds.
2. Authorized loopback environment, no exclusions: 3,488 passed, 30 skipped, 8 xfailed, zero failures/errors in 850.88 seconds. The later-added legacy cursor regression reproduced and fixed another marker consumer issue.
3. Final committed behavior, no exclusions: **3,489 passed, 30 skipped, 8 xfailed**, zero failures/errors in 803.22 seconds. Two warnings come from intentionally damaged gzip fixtures in the retention test. Neither named known-main failure required an exception.

Command: `python3.11 /tmp/khipu-log-oracle.py /tmp/kl-broad-3.log tests/test_routing*.py --junitxml=/tmp/kl-broad-3.xml -o junit_family=xunit1`, from `packages/cli`.

Evidence: `/tmp/kl-broad-1.log`, `/tmp/kl-broad-2.log`, `/tmp/kl-broad-3.log`, `/tmp/kl-broad-3.xml`, and the `/tmp/kl-*-red.log` / `/tmp/kl-*-green.log` focused regression logs. The environment wrapper remains `/tmp/khipu-log-oracle.py`.

All requested checks are verified within their synthetic/disposable scope. Live Khipu state, production replicas, installed applications, promotion, merge, tags and releases were not touched. No subagents were started.
