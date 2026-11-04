# Routing log retention follow-up

Source gate head: `0deeaa1b92be64407cb892045e5f84e534374bd7`.

All five requested findings are repaired and verified. Generation metadata is excluded from clock and identity history while remaining readable by the older parser. Remote observations use the same retention selection and fold agreement; a committed order fence prevents refetch resurrection while sync advances its cursor. Doctor includes both active files, reports actionable removable growth red, and reports protected owner history yellow with nothing to do. Thresholds are unchanged; the byte-red branch is reachable below the line bound.

Item 5 reproduced before repair. A stale retention flag warned even with a complete generation chain. Complete reconstruction now clears that flag. The synthetic legacy full-copy archive upgrade, two compactions, exact full reconstruction, and doctor checks pass.

The owner explicitly approved archiving expired unsupported observations from the sender log and deferred index. The regression verifies archival and deferred cleanup, exact full-log reconstruction, preserved owner rows, and preserved supported unsent rows. Dated conversation resets, archive recompression lock behavior, and author classification were left unchanged.

The strict existing one-second performance assertion remains unchanged. Historical clock settlement is now computed only when its existing skew condition needs it.

## Verification

Hermetic disposable HOME, XDG config, Khipu state and PostgreSQL; no live Khipu state writes. Python 3.11 with the repository vendored dependencies.

Broad selector: `tests/test_routing*.py` (three runs total).

- Run 1: 4 failed, 3493 passed, 30 skipped, 8 xfailed. Fixed the named regressions/fixtures and investigated the unchanged performance deadline.
- Run 2: 1 failed, 3497 passed, 30 skipped, 8 xfailed. Only the strict performance deadline failed; profiled and removed unnecessary normal-clock settlement.
- Run 3: **3498 passed, 30 skipped, 8 xfailed; exit 0**, 1103.22 seconds. XML has zero errors and failures. No fourth broad run.

Final logs: `/tmp/kl2-broad-3.log`, `/tmp/kl2-broad-3.xml`. Focused red-to-green regressions also ran. The broad selection includes fold fuzz equality, repeated-compaction reconstruction, concurrent append/compactors, old reader and replica compatibility. All passed. Scope diff and whitespace checks passed.

## Day 120

Seed 7, two accounts, one runner, four lanes, production-shaped UUIDv7 identifiers. Replication projects all 112999 supported rows and initially defers 52160 unsupported usage samples. The bulk post-sync fixture measures fold/retention; pair tests separately exercise real paginated sync and refetch. Both modes retain exact original sender history through full_log(). Timings include cold read, parse, input and fold; three samples, not an isolated fold microbenchmark.

| Replication | Before physical lines | After local + remote | Unique fold events | Active bytes | Archive bytes | Cold milliseconds |
|---|---:|---:|---:|---:|---:|---|
| Off | 165159 | 1864 + 0 | 1863 | 966406 | 6491150 | 40.23, 41.07, 41.92 |
| On | 278158 | 1864 + 1327 | 2285 | 1680276 | 6491151 | 64.83, 65.39, 161.84 |

Measured owner rate: 382 lines / 120 days = 3.1833 per day (configured mean 3). With replication, 959 recent unsupported observations remain deferred. The 3191 physical lines include duplicates across sender and receiver files; the actual merged fold is 2285 events.
