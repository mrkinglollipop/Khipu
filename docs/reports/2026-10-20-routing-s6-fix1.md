# Routing slice 6 — fix round 1

Rebased `feat/routing-s6-delivery` cleanly onto slice 4's `c4c4d6d`; the rebased slice 6 base is `4270fb8`. No delegation or push.

## Review leads

All test names below are in `packages/cli/tests/test_routing_s6_delivery.py` unless qualified. Red evidence precedes each production fix under `/tmp/khipu-routing-s6/fix1/`.

| Lead | Result | Tests |
| --- | --- | --- |
| 1. Stale rendered rule and unlocked ack race | Proven and fixed. Rule/priority revisions include rendered dependencies; reservations are checked under the ack lock. New holds precede long rule pages. Account age wording retains stable revisions. | `test_new_hold_invalidates_rule_pages_and_arrives_on_next_prompt[False]`, `[True]`; `test_level_suffix_invalidates_live_rule_page`; `test_prompt_offer_serializes_with_ack_instead_of_reusing_unlocked_snapshot` |
| 2. Deleted rule or cleared note stays owed as old words | Proven and fixed. Full plans carry current entity identities and removal messages. Both prompt and SessionStart reservations replace old debt with removal debt. | `test_full_summary_replaces_deleted_entity_debt` (note/rule, live/expired lease); `test_session_start_plan_reconciles_deleted_debt_without_the_view[note]`, `[rule]` |
| 3. Unbounded prompt discovery | Proven and fixed. Discovery refresh has a 5 ms deadline, preserving the caller's alarm. Timeout uses configured/saved names and cached aliases without directory walks or link resolution. Retired owner exemptions are discarded. | `test_stalled_volume_refresh_keeps_prompt_routing_and_restores_alarm`; `test_stalled_refresh_keeps_known_aliases_without_retired_owner_exemptions`; `test_projection_deadline_cannot_escape_during_alarm_restoration` |
| 4. Ack history grows forever | Proven and fixed. Retain 128 recent batch ids; recent duplicate acks succeed, evicted ids fail closed. | `test_ack_history_is_bounded_and_recent_ack_is_idempotent` (140 acknowledgements with debt still outstanding) |
| 5. Final page header while that entity still has a slice owed | Disproven; no page-count algorithm change. The invariant test passes on the original implementation, including repeated smaller refits. Remaining pages are counted from the actual cut end, so remaining text necessarily makes `m > n`. Estimates can change as later room changes. | `test_refit_page_total_never_claims_completion_with_text_still_owed` |

Strengthened `test_expired_ack_without_a_retry_cannot_consume_debt` and `test_revision_change_cancels_unacknowledged_batch`: inspect saved unacked state and offsets, re-offer the debt, then acknowledge it and verify progress. They no longer rely only on a false ack and empty marker.

## Verification

The requested hermetic selector ran exactly three times, with no edits during a full run. No test assertions or routing timeout budgets were relaxed.

- Final full run: **1,953 passed, 113 xfailed, 7 skipped, 4 subtests passed; only the five existing CLI failures below**. Log: `/tmp/khipu-routing-s6/fix1/PYTEST-3.log`.
- Slice 4's `tests/test_routing_s4_entries.py::test_cached_definition_inputs_borrow_without_whole_log_work` passes, retaining the 20,000-event zero-parse/fold/order assertions.
- Both previously unverified pointer/page tests pass in the final full run: `tests/test_routing_push_start.py::test_a_large_start_pages_the_note_and_keeps_mandatory_memory` and `tests/test_routing_push_prompt.py::test_a_block_too_long_for_the_harness_limit_arrives_as_pages`.
- Final focused privacy/deadline run: **149 passed** (148 existing `test_agent_view.py` cases plus the deterministic late-alarm regression). Log: `alarm-green-and-privacy.log` in the evidence directory.
- Replay: **200 prompts per mode, zero timeouts in either mode, zero added timeouts, stop=false**. Log: `replay.log`. The isolated real-search path returned errors for all 400 prompts; routing reported 186 unsent responses. This verifies the timeout condition, not successful live recall delivery.
- Scope and whitespace: `git diff --check` passes. Production changes are limited to `agent_view.py`, `routing.py`, and `routing_delivery.py`; tests and this report are the other changed files.

Remaining baseline failures:

```text
tests/test_routing.py::test_rule_at_owner_request_is_live_without_terminal_confirmation_but_owner_actions_are_not
tests/test_routing.py::test_a_removed_rule_comes_back_whole_with_restore
tests/test_routing.py::test_the_apps_argv_keeps_a_dash_value_as_words_never_a_flag
tests/test_routing.py::test_the_apps_view_marks_watched_rules_old_switches_and_who_changed_what
tests/test_routing.py::test_recent_changes_name_the_owner_in_settings_for_every_change_made_in_the_app
```

Intermediate full-run failures, all corrected before the final run:

- Run 1: `tests/test_routing_acceptance.py::test_3_unacknowledged_hook_batch_expires_to_owed`; `tests/test_routing_push_prompt.py::test_a_paged_block_does_not_ack_its_undelivered_remainder[changes]`; `tests/test_routing_push_start.py::test_a_large_start_pages_the_note_and_keeps_mandatory_memory`; `tests/test_routing_s6_delivery.py::test_expired_ack_without_a_retry_cannot_consume_debt`. Relative account text was over-invalidating revisions, and adding dependency entries before full-summary entries changed the established order. The SessionStart test passes on untouched rebased `4270fb8`; a before/after ledger trace identified the ordering regression.
- Run 2: `tests/test_routing_s6_delivery.py::test_ack_history_is_bounded_and_recent_ack_is_idempotent` raised `_InventoryDeadline` during alarm restoration. It passed when rerun separately; the deterministic `test_projection_deadline_cannot_escape_during_alarm_restoration` then reproduced the race, failed before the fix, and passed afterward. A pending one-shot alarm becomes harmless before handler restoration.

Additional red logs: `regression-red.log`, `revision-red.log` (corrected rule-entry setup), `start-red.log`, `fallback-red.log`, and `alarm-red.log`. Diagnostic traces and the untouched-baseline export remain local in the evidence directory.
