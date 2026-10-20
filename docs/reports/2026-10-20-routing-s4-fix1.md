# Routing slice 4: fix round 1

All review findings are addressed. The final requested Python oracle has only the five explicitly excluded baseline failures. No source edits followed that final run. The branch is `feat/routing-s4-entries`; no push was made.

## Findings and proof

All Python test names below passed in full run 3 unless another check is named.

| Finding | Change and location | Proof |
| --- | --- | --- |
| P1-A: per-prompt cost | `packages/cli/khipu/routing.py:922` caches definitions and a borrowed event snapshot by path, stat and Mac; account overlays also account for bindings and configured accounts. `_inputs` reuses that snapshot. `routing.py:4266` avoids the initial source-backed fold before the locked refold. | `tests/test_routing_s4_entries.py::test_cached_definition_inputs_borrow_without_whole_log_work` builds 20,000 events, requires zero parses/folds/order walks on warm read, push and input calls, repeats with an account definition and changed bindings, and requires exactly one fold on a source-backed refresh. |
| P1-B: end-or-keep through owner act, app and Undo | `packages/cli/khipu/owner_act.py:28` accepts and serializes `holds` for rule add and priority set. `apps/desktop/src-tauri/src/lib.rs:1911` forwards the map as one `--holds` argument. `packages/cli/khipu/routing.py:8095` records rule choices in owner-action facts. `packages/cli/khipu/owner_direction.py:1150`, `:1328`, `:1343` and `:1379` reuse them for priority and rule Undo. | `tests/test_owner_act_argv.py::test_priority_and_rule_add_forward_each_hold_choice_as_json`; `tests/test_owner_direction_mcp.py::test_owner_act_accepts_hold_choices_for_priority_and_rule_add`; the four Undo tests listed below; Rust `the_priority_travels_as_one_joined_json_argument_in_the_owners_order`. |
| P2-C: a newer reading inside the renewal throttle | `packages/cli/khipu/routing.py:4221` suppresses the already-persisted source reading, rather than every reading in its observation slot. A fresh same-state reading can update the view without appending a renewal. | `tests/test_routing_s4_entries.py::test_same_state_newer_used_share_is_live_while_renewal_stays_throttled` requires 96% to replace 40% after four minutes while the log still contains only the original observation. |
| P3-D: renewal scope maximum | `packages/cli/khipu/routing_entries.py:59` caps renewed expiry by the base observation's lifetime and preserves a LIMITED observation's original limit end. | `tests/test_routing_s4_entries.py::test_renewal_expiry_cannot_outlive_the_base_lifetime`; `tests/test_routing_s4_entries.py::test_limited_renewal_cannot_slide_past_its_original_limit_end`. |
| P3-E: local source identifiers | `packages/cli/khipu/routing_entries.py:120` uses HMAC with the existing routing id-salt for newly persisted source ids. Legacy source dismissals remain recognized. `docs/plans/2026-10-20-routing-first-class.md:914` assigns omission of local source ids and evidence text to slice 11a's replication projection. | `tests/test_routing_s4_entries.py::test_persisted_source_id_is_salted_and_depends_on_the_local_salt`, plus the existing old-source dismissal regression. |
| P3-F: stop tracking | `packages/cli/khipu/accounts.py:967` preserves surviving priority entries' `level`. `packages/cli/khipu/routing.py:4754` applies end-or-keep validation at account-transaction append under the routing lock. | `tests/test_routing_s4_entries.py::test_stop_tracking_keeps_a_priority_level_and_checks_its_remaining_entries`. |
| P3-G: definitions have no owner writer | Deferred as requested, rather than building a writer here. The slice 9a row explicitly includes an owner-reachable definition writer for CLI, owner direction and app. | Scope inspection: `docs/plans/2026-10-20-routing-first-class.md:911`. |
| P3-H: new Ruff errors | Removed the fixture import/redefinition and unused names; no blanket ignores were added. | `ruff check --no-cache packages/cli/tests/test_routing_s4_entries.py packages/cli/tests/test_owner_undo.py`: all checks passed. CLI-wide Ruff reports the 24-error baseline, with zero new errors. |
| Case 12: cached reading cannot renew | Removed this test's strict xfail and advanced its clock four hours, beyond the renewal throttle. | `tests/test_routing_acceptance.py::test_12_cached_reading_never_renews` at line 1207. |

The four Undo regressions are:

- `tests/test_owner_undo.py::test_priority_undo_reuses_its_original_hold_choice`
- `tests/test_owner_undo.py::test_rule_edit_undo_reuses_its_original_hold_choice`
- `tests/test_owner_undo.py::test_rule_confirm_undo_reuses_its_original_hold_choice`
- `tests/test_owner_undo.py::test_rule_retire_undo_reuses_its_original_hold_choice`

Cases 1 and 5 remain passing in the final run. Case 12 is the only xfail removed in this round; every remaining marker stays strict. `test_entry_kinds_and_owner_words_survive_append_and_fold_byte_identically` and `test_old_format_log_matches_pre_slice_effective_snapshot_without_rewriting` pass. Existing event lines are not rewritten or reordered.

## Verification

Exactly three full Python runs used the requested hermetic command, without changing any timeout:

| Run | Passed | Failed | Skipped | Xfailed | Subtests | Duration |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 1917 | 9 | 7 | 117 | 4 | 426.51 s |
| 2 | 1919 | 9 | 7 | 116 | 4 | 1143.70 s |
| 3, final | 1922 | 5 | 7 | 117 | 4 | 247.18 s |

Run 1 preceded the additional rule-retire Undo regression and the single-fold assertion. Runs 2 and 3 checked identical production code and assertions. The final exit code is 1 because of these five excluded baseline failures, all in `tests/test_routing.py`:

- `test_rule_at_owner_request_is_live_without_terminal_confirmation_but_owner_actions_are_not`
- `test_a_removed_rule_comes_back_whole_with_restore`
- `test_the_apps_argv_keeps_a_dash_value_as_words_never_a_flag`
- `test_the_apps_view_marks_watched_rules_old_switches_and_who_changed_what`
- `test_recent_changes_name_the_owner_in_settings_for_every_change_made_in_the_app`

Every other failure from earlier full runs is named here:

| Test id | Failed in | Final run |
| --- | --- | --- |
| `tests/test_routing_acceptance.py::test_1_hold_priority_contradiction_and_end_route_already_arrive[start]` | 2 | passed |
| `tests/test_routing_acceptance.py::test_2_oversized_session_start_has_a_first_page` | 2 | strict xfail retained |
| `tests/test_routing_delegation.py::test_the_launcher_delivers_and_never_fails_the_call` | 1, 2 | passed |
| `tests/test_routing_delivery.py::test_review_omitted_pending_changes_are_counted_and_delivered_later[start]` | 2 | passed |
| `tests/test_routing_push_prompt.py::test_the_fallback_to_the_full_summary[behind]` | 1 | passed |
| `tests/test_routing_push_prompt.py::test_the_routing_clock_starts_when_the_read_starts` | 1 | passed |
| `tests/test_routing_push_prompt.py::test_a_fold_longer_than_the_childs_bound_still_saves_the_view` | 1 | passed |

Run 2 coincided with observed host load 58.93, 21 GB compressed memory and 256 MB free. Its extra failures returned no routing output; the final unchanged run cleared them. The host measurements support contention as a contributing factor, rather than proving a root cause for every earlier failure.

Other checks:

- `cargo test`: 170 passed, zero failed. The first attempt stopped before crate compilation because generated `khipu-resources` was absent. The documented `apps/desktop/scripts/bundle_cli.sh` staged it; attempt 2 passed.
- Informational synthetic 20,000-event measurement: cached-read median 5.21 ms; prompt-push median 58.8 ms over ten calls each. The regression gate uses counts, never wall-clock assertions.
- `python3.11 -m khipu.routing_replay`, attempt 2: exit 0, 200 prompts, zero timeouts on and off, routing-on median 38.5 ms, p95 195.1 ms, `added_per_100: 0.0`, `stop: false`. Attempt 1 overlapped Cargo and reported one timeout; no timeout setting changed. Both attempts reported real-search errors, so this verifies the replay timing gate, not successful recall or delivery. The second attempt reported 119 routing results as unsent.
- `git diff --check`: passed.

Raw local evidence is under `/tmp/khipu-routing-s4/fix1/`: `PYTEST-root1.log`, `PYTEST-root2.log`, `PYTEST-root3.log`, `CARGO-root1.log`, `CARGO-root2.log`, `RESOURCES-root.log`, `REPLAY-root1.log`, `REPLAY-root2.log`, `PERF-root.json`, and `RUFF-root-final.log`.

## Branch comparison

The slice base remains `4ee7942`. Another process advanced the shared `origin/main` ref during verification, most recently to `88cbfb3` (merge of #272). No fetch, rebase or integration of those unrelated changes was performed here. Consequently, the current requested comparison includes six unrelated upstream library/job paths. They are not fix-round edits.

`git diff --name-only origin/main`, including this report:

```text
apps/desktop/src-tauri/src/lib.rs
docs/plans/2026-10-20-routing-first-class.md
docs/reports/2026-10-20-routing-s4-fix1.md
docs/reports/2026-10-20-routing-s4.md
packages/cli/khipu/accounts.py
packages/cli/khipu/cli.py
packages/cli/khipu/embed.py
packages/cli/khipu/jobs.py
packages/cli/khipu/library.py
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
```
