# Routing slice 6 — fix round 4

Requested regressions pass. The full gate remains red for five unchanged CLI failures independently reproduced on untouched `origin/main` (`fb98704`). Rebased onto that base, including #274 and #275, choosing main for the import conflict; retained its provider imports and restored slice 6's delivery preload after the import test identified it. Worked directly, without delegation, stash, or push.

The helper owns freshness: it rebuilds the complete scrub table within **250 ms** on idle ticks and after requests, publishing its login signature and successful build times. The maximum age is **2 seconds**, two production helper ticks of 1 second each. Wall and monotonic age checks cover sleep and reject backwards clock changes. A failed refresh never renews freshness.

Each prompt probes its own login signature for **5 ms**. A completed differing signature immediately withholds routing. An unfinished probe uses a fresh helper table; it cannot renew the table. **A login added after the last build can therefore be exposed only within that 2-second window if its prompt probe does not finish.** Expired tables withhold routing even when discovery stalls. Current owner exemptions remain separate from inherited aliases.

The daemon-poll failure was missing helper preparation in a direct-renderer setup. Its setup now prepares inventory; its content assertion is unchanged. Other direct-renderer setups do likewise, and each hermetic runtime gets an isolated helper cache. Read-cost assertions exclude helper preparation. Tests cover unchanged slow discovery, configured and discovered mid-session logins, early signature rejection, expiry, stalled-start recovery, unchanged-input rebuilding, failed refreshes, sleep, and clock changes.

Exactly three full supplied oracle runs, with no edits during a run:

| Run | Passed | Failed |
| --- | ---: | ---: |
| 1 | 2,069 | 52 |
| 2 | 2,115 | 5 baseline |
| 3, final | 2,117 | 5 baseline |

Final additional results: 7 skipped, 109 xfailed, 14 subtests passed. Final replay: 200 prompts per mode, zero timeouts, zero added timeouts, `stop=false`, routing-on p95 18.6 ms. All 400 searches errored and all 200 routing responses were unsent; live delivery is unverified. `git diff --check` passes.

Evidence: `/tmp/khipu-routing-s6/fix4/` (`full-1.log`, `full-2.log`, `full-3.log`, `baseline-five.log`, `failures-check.log`).
