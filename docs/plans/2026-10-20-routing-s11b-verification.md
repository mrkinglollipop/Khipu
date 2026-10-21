# Routing slice 11b verification

Scope: [routing first-class plan](2026-10-20-routing-first-class.md), slice 11b,
cases 12 and 4's Mac portion. Base: `55bc203e72519c109162a7c92684d24d956a20ec`
(the merged slice 11a). All work stayed on `feat/routing-s11b` in its assigned
worktree. No delegation, push, deployment or live routing configuration change.

## Outcome

- Case 12: two temporary HOMEs exchange events through a fake hub. Hub-order
  numbers, opposite order, duplicate upload, lost upload replies, paged and
  interrupted downloads, overlap, offline age, clock skew, restore with a new
  Mac identity, observations, renewals, source silence, machine availability,
  and clock-only expiry pass. Local history bytes remain unchanged by sync and
  restore. Missing/rejected IDs never upload. Poll integration is exercised.
- Case 4, Mac portion: withheld proposals show placeholders; confirmation on
  the source Mac carries the confirmed rule/priority words. Confirmation of
  unseen words is refused on the receiving Mac. A concurrent proposal cannot
  be confirmed using its predecessor's words.
- The previous acceptance xfails for these cases are now ordinary passing
  tests. The old observation fixtures use slice 4's actual `scope="all"`
  and explicit fold Mac identity; their state assertions remain strict.
- The 11a projector was dropping generated UUIDs and Mac identities through
  its free-text secret heuristic. Exact typed generated identity shapes now
  survive, including derived entity IDs and legacy revision hashes. Path IDs
  still drop. Confirmation kinds explicitly carry confirmed owner words.
  The existing field-walk tests retain their closed-field checks.

## Verification

Tests were written before implementation. Initial red evidence: 17 failures
plus four expected-failure case assertions in the replication selector, and two
additional red proposal tests. Later edge tests reproduced a null-text push
failure and the confirmation/replacement race before those fixes.

The supplied hermetic full oracle ran exactly three times, from `packages/cli`,
with its environment removals, temporary HOME, disabled Keychain, external
Python library path, selectors and pytest flags unchanged. A subprocess timeout
bounded each run (600 seconds first, 900 seconds thereafter).

| Run | Passed | Failed | Result |
| --- | ---: | ---: | --- |
| 1 | 3215 | 28 | Found cache fingerprint serialization, projection/schema, and test-fixture issues. |
| 2 | 3241 | 6 | Five allowed baselines plus the new CLI/MCP withheld-flag mismatch. |
| 3 | 3244 | 5 | Only the five allowed baselines remain. |

Final pytest output:

```text
5 failed, 3244 passed, 36 skipped, 19 xfailed, 3124 subtests passed in 284.20s (0:04:44)
```

Pytest exit code is **1**, because the permitted baseline failures remain:

- `test_rule_at_owner_request_is_live_without_terminal_confirmation_but_owner_actions_are_not`
- `test_a_removed_rule_comes_back_whole_with_restore`
- `test_the_apps_argv_keeps_a_dash_value_as_words_never_a_flag`
- `test_the_apps_view_marks_watched_rules_old_switches_and_who_changed_what`
- `test_recent_changes_name_the_owner_in_settings_for_every_change_made_in_the_app`

All are in `test_routing.py`. All 24 tests in
`test_routing_s11b_replication.py` pass. The final run includes all code changes;
only this evidence document was added afterwards. Logs for this session are
`/tmp/khipu-s11b-{red,proposal-red,focused,oracle-1,oracle-2,oracle-3}.log`
and `/tmp/khipu-s11b-{final-edges-red,race-red}.log`.

## Boundary and deferred work

`routing_replication_live` remains default off. Replication and test traffic
stayed on fakes. No real transcript, routing log, Keychain value or API Keys
folder was read. No live hook, launch agent or configuration was modified. PostgreSQL
transport statements are exercised with a fake cursor; actual PostgreSQL and
live two-Mac propagation are **unverified** and require a separately authorized
integration/deployment gate.

The closed 11a projection does not cover newer pace/cap/tier event kinds.
Unsupported kinds are retained as `deferred_kinds` and retried after a projection
upgrade; supported events continue. Gateway exposure remains slice 12. The
60-second propagation budget assumes the existing routing helper is running;
its enabled poll interval is capped at 30 seconds. A stopped helper requires
manual sync. No live configuration was enabled by this work.
