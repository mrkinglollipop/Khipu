# Routing slice 4: fix round 2

All four requested items are addressed. All new fix-round tests pass in the final full run. Its additional session-start timing failure passes alone under the original limits. Production code did not change after the successful final cost comparison. No push was made.

## Items and proving tests

| Item | Result and location | Proof |
| --- | --- | --- |
| 1. Undo while held | Fixed. `khipu/owner_direction.py:1639` accepts explicit choices; `owner_act.py` exposes them for `direction.undo`. Recorded choices are copied, explicit choices override them, and `routing.py:4813` defaults newly relevant holds and pauses to keep under the append lock. Priority roles also count when identifying affected accounts. | The six Undo tests below pass. |
| 2. Changed-log cost | Fixed. `khipu/routing.py:4230` reuses the parsed/raw and settled-order snapshots, including source persistence and identity projection. Appended source events are merged into the known order. `routing.py:2671` reuses this fold's operations salt without changing legacy hashes. `routing.py:1838` reuses parsed rows only for an exact, newline-complete byte prefix of the same file/mode; changed prefixes and incomplete tails are reparsed. | `test_changed_log_prompt_with_source_append_reads_and_orders_once[False]` and `[True]` each require one log-read call and one full write-order pass for an actual 20,000-event append-then-push, with a source renewal appended under lock. The variants cover absent/present account definitions. Both also require a fresh full-or-changes push result, the new renewal witness, and preserved sequence order. |
| 2. `source_seen` growth | Changed to **write only on availability-state change**, not on a time throttle. The durable state consists of `silent`, `unreadable`, and declared lanes. A heartbeat timestamp advancing by itself never appends another line; the live source view still uses the current heartbeat. An old line lacking this state causes one initial state capture. | `tests/test_routing_s4_entries.py::test_source_heartbeat_writes_on_availability_change_and_never_renews_account` advances beyond an hour with no new source line, checks the live heartbeat, then checks an unreadable transition appends one. |
| 3. `khipu_route_rule` holds | Fixed. `khipu/mcp_server.py` adds the schema, validates the map and passes it through `routing.request_rule`. Existing owner-only, non-pending processing governs when end choices take effect. | `test_route_rule_mcp_schema_and_write_accept_hold_choices`. |
| 4. Settings UI | Carried to slice 15b, as requested. No Settings UI was built. | `docs/plans/2026-10-20-routing-first-class.md:920` explicitly includes Settings end-or-keep choices for held or paused accounts. |

Names without a path in this report belong to `packages/cli/tests/test_routing_s4_fix2.py`. Its Undo cases are:

- `test_direction_undo_keeps_a_held_account_the_action_never_named` — u1: Grok held, put Codex first, then Undo without a choice.
- `test_owner_act_undo_keeps_a_hold_added_after_the_action_without_an_explicit_choice` — u2: the hold appears after the action; Undo needs no explicit choice.
- `test_direction_undo_accepts_an_explicit_end_choice` — explicit end is applied and recorded on the Undo action.
- `test_owner_act_undo_accepts_an_explicit_keep_for_a_hold_added_after_the_action` — owner-act accepts explicit keep.
- `test_undo_defaults_a_hold_named_only_in_the_restored_priority_role`.
- `test_undo_of_a_restored_rule_uses_its_two_argument_inverse`.

The cache tests `test_parsed_line_cache_parses_only_complete_appended_lines`, `test_parsed_line_cache_reparses_a_replaced_prefix`, and `test_parsed_line_cache_reparses_an_incomplete_final_line_without_rewriting_it` pass. `test_legacy_revision_reusing_this_fold_salt_is_byte_identical` and the existing old-format effective-view/unchanged-bytes test also pass. No existing log lines are rewritten or reordered. No strict xfail markers or acceptance assertions were relaxed in this round.

## Back-to-back cost comparison

The same repo-owned runner, `packages/cli/tests/routing_s4_cost_workload.py`, ran serially against baseline `4ee7942`, then the repaired worktree. Each invocation used a fresh temporary HOME/data directory. The workload has 20,000 old-format events, including 400 standing rules, three accounts, a usage reading with a fixed reset, and five raw append / advance one second / `prompt_push(..., budget=1.0)` iterations. Timings are measurements, never regression assertions.

| Median, milliseconds | Base | Final fix |
| --- | ---: | ---: |
| Cached read | 4.3 | 4.5 |
| Steady prompt push | 56.7 | 38.1 |
| Push after one appended line | **744.1** | **573.1** |

Changed-push samples were base `[767.0, 731.2, 695.2, 785.8, 744.1]` and fix `[587.0, 553.0, 511.5, 573.1, 593.3]`. All five calls on each checkout returned `kind: full`, version 2 after a version-1 warm view. The repaired median is about 23% below the measured base and below the unchanged 1,000 ms budget.

The review's original figures were 842 ms base and 3,129 ms fix. Two earlier comparisons during this round measured 880.8/965.3 ms and 774.4/815.3 ms; neither met the below-base requirement. Profiling then identified per-row salt-path lookup overhead, and the final repair also avoids re-parsing unchanged complete lines. Three cost comparisons were used; no fourth was run.

## Oracle results

The exact requested hermetic full selector ran three times; no edits were made while a run was active, and no timeouts were increased.

| Run | Passed | Failed | Errors | Skipped | Strict xfails | Subtests | Seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 1927 | 7 | 6 | 7 | 117 | 4 | 252.79 |
| 2 | 1933 | 7 | 0 | 7 | 117 | 4 | 270.48 |
| 3, final | 1934 | 6 | 0 | 7 | 117 | 4 | 290.68 |

All five excluded baseline failures remain in `tests/test_routing.py`:

- `test_rule_at_owner_request_is_live_without_terminal_confirmation_but_owner_actions_are_not`
- `test_a_removed_rule_comes_back_whole_with_restore`
- `test_the_apps_argv_keeps_a_dash_value_as_words_never_a_flag`
- `test_the_apps_view_marks_watched_rules_old_switches_and_who_changed_what`
- `test_recent_changes_name_the_owner_in_settings_for_every_change_made_in_the_app`

Every other full-run failure/error is listed here:

| Test id | Earlier problem | Final result |
| --- | --- | --- |
| `tests/test_routing_s4_fix2.py::test_direction_undo_keeps_a_held_account_the_action_never_named` | Run 1: missing `direction_world` fixture alias | passed |
| `tests/test_routing_s4_fix2.py::test_direction_undo_accepts_an_explicit_end_choice` | Run 1: missing fixture alias | passed |
| `tests/test_routing_s4_fix2.py::test_owner_act_undo_keeps_a_hold_added_after_the_action_without_an_explicit_choice` | Run 1: missing fixture alias | passed |
| `tests/test_routing_s4_fix2.py::test_owner_act_undo_accepts_an_explicit_keep_for_a_hold_added_after_the_action` | Run 1: missing fixture alias | passed |
| `tests/test_routing_s4_fix2.py::test_undo_defaults_a_hold_named_only_in_the_restored_priority_role` | Run 1: missing fixture alias | passed |
| `tests/test_routing_s4_fix2.py::test_undo_of_a_restored_rule_uses_its_two_argument_inverse` | Run 1: missing fixture alias | passed |
| `tests/test_routing_s4_fix2.py::test_changed_log_prompt_with_source_append_reads_and_orders_once[False]` | Run 1: empty delivery marker read as versioned; run 2: moving reset meant a new observation, not a renewal | passed |
| `tests/test_routing_s4_fix2.py::test_changed_log_prompt_with_source_append_reads_and_orders_once[True]` | Same fixture setup problems | passed |
| `tests/test_routing_delivery.py::test_review_omitted_pending_changes_are_counted_and_delivered_later[start]` | Run 3: bounded session-start read returned no text | **passed alone**, unchanged limits: 1 passed in 1.93 s |

The count assertions already passed in runs 1 and 2. Corrections supplied the existing owner-direction fixture, a real warm-view delivery marker, and a fixed quota reset so the unchanged renewal assertion tests one quota period. No production changes followed the successful final benchmark. The known burst-of-prompts load test passed in the full runs and did not need an isolated rerun.

`python3.11 -m khipu.routing_replay`: exit 0; 200 prompts; **zero timeouts on and off**; routing-on median 28.5 ms, p95 42.0 ms; `added_per_100: 0.0`, `stop: false`. Both search paths reported errors and 185 routing results were unsent, so this verifies the replay timing gate, not successful recall or delivery.

Ruff on touched production/tests: all checks passed. CLI-wide Ruff remains at the 24-error baseline. `git diff --check` passed. Raw root oracle logs and the diagnostic profile are in `/tmp/khipu-routing-s4/fix2/`.

## Current branch comparison

This round changes ten files relative to `c4c4d6d`. The shared `origin/main` comparison below, at `88cbfb3`, also includes upstream divergence. No fetch or rebase was performed.

`git diff --name-only origin/main`, including this report:

```text
apps/desktop/src-tauri/src/lib.rs
docs/plans/2026-10-20-routing-first-class.md
docs/reports/2026-10-20-routing-s4-fix1.md
docs/reports/2026-10-20-routing-s4-fix2.md
docs/reports/2026-10-20-routing-s4.md
packages/cli/khipu/accounts.py
packages/cli/khipu/cli.py
packages/cli/khipu/embed.py
packages/cli/khipu/jobs.py
packages/cli/khipu/library.py
packages/cli/khipu/mcp_server.py
packages/cli/khipu/owner_act.py
packages/cli/khipu/owner_direction.py
packages/cli/khipu/promote.py
packages/cli/khipu/routing.py
packages/cli/khipu/routing_entries.py
packages/cli/khipu/routing_events.py
packages/cli/khipu/routing_identity.py
packages/cli/khipu/routing_sources.py
packages/cli/scripts/routing_fixture_sanitize.py
packages/cli/tests/fixtures/entry_legacy_effective.json
packages/cli/tests/routing_s4_cost_workload.py
packages/cli/tests/test_accounts.py
packages/cli/tests/test_embed.py
packages/cli/tests/test_jobs.py
packages/cli/tests/test_library.py
packages/cli/tests/test_owner_act_argv.py
packages/cli/tests/test_owner_direction_mcp.py
packages/cli/tests/test_owner_undo.py
packages/cli/tests/test_promote_offline.py
packages/cli/tests/test_routing.py
packages/cli/tests/test_routing_acceptance.py
packages/cli/tests/test_routing_delivery.py
packages/cli/tests/test_routing_push_prompt.py
packages/cli/tests/test_routing_push_start.py
packages/cli/tests/test_routing_s4_entries.py
packages/cli/tests/test_routing_s4_fix2.py
```
