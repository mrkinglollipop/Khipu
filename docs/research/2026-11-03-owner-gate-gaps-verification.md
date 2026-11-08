# Owner-gate repairs: verification record

## Current scope: the owner's simpler yes flow

This section supersedes the security requirements and completion claims in the historical record below. The owner explicitly withdrew F2 and F4, rejected protection against same-user code or agent interpretation, and required conversational operation with one ordinary answer. This pass started at `5609823ba95a0c9a2b9a514902bc416887018be1` in `/tmp/khipu-gate-fix`, branch `fix/owner-gate-gaps`. No subagents were used.

Current verification: final broad run 3 passed **1638 tests, 7 skipped; 34 subtests passed**, exit 0, 79.10 seconds. The budget is exhausted at 3 of 3 broad runs; no fourth run was made. Targeted acceptance also passed 273 tests, followed by 66 focused tests including an actual undo after the removed six-hour cutoff. Source and tests were not edited after the final broad run; only this evidence was finalized. No blockers remain. Publication is limited to the authorized plain push of `fix/owner-gate-gaps`.

### What was removed

- T3 hook/message hash equality, provider/issuer/process ancestry challenges, nearest-harness and conflicting environment-session checks. A typed T3 owner-message row in the conversation supplies the answer; the hook text need not match it. Hashes retained in receipts are diagnostic, not a confirmation challenge.
- Global message-use accounting, creation-turn tokens, turn reservations, pending-question embargoes and receipt/event ownership challenges. The normal hold still applies once under its request lock. There is no global quota on an owner's typed message.
- Versioned hold review seals and the per-thread delivery/disclosure contract. Legacy non-file holds are no longer refused merely for lacking the new schema. Content-version checks and saved file bytes remain where replaying something different would otherwise be possible.
- Antigravity repeated-word, late-copy and occurrence challenges; request-boundary/message-count and question/file-length caps; the six-hour authorization expiry; expiry of the shown-question row. The collector's ordinary short-lived native turn storage is unchanged: an old question can receive a freshly typed answer at any later date.
- The word-for-word/hash/imperative-phrase challenge for undoing over newer state. The agent can explain the loss and use `over_newer` on the owner's ordinary answer; `owner_words` remains accepted for compatibility and is ignored.
- The conversational ban on tracker catch-up. It now reaches the existing importer and its business prerequisites. Catch-up still reports its existing lack of automatic undo; this does not affect the four own-request routing paths checked below.
- F2's proposed shown-read requirement outside T3 and F4's proposed `ANSWER_ACTIONS` additions were never implemented in this pass. Existing native read boundaries remain; no second front-end-specific policy was added. F5 adds `home` wording only, including an old and new home for an existing label; it adds no refusal.

### What was kept or repaired

- F6: an identical re-read keeps its boundary and known shown signatures, including a full-key read or a subset. A changed version starts the boundary for the changed content. Shown-question rows do not expire at 30 days. Saved file snapshots remain immutable.
- F7: an unconfirmable legacy file request does not block direct directions. Its list and refusal say to decline or ask for a reissue with the full content. Declining and a new direction may share one typed message: declining changes only the queue, so only the new direction changes owner state. The existing owner-actions log and routing receipt record the decline, its originating conversation, the confirming conversation and message; this also makes another conversation's decline visible. No additional audit mechanism was introduced.
- F8: a false-valued terminal refusal carries its reason and prints it. Routing, ledger, project, bulk Owed, preference merge and CLI callers surface that reason. A terminal inside a known harness says to answer in the conversation or app. No reparenting defense was added.
- F1 report-only check: rule add, rule edit, priority set and priority amendment all produce an owner-action receipt and an undo reference/inverse. No receipt or undo was missing in those four paths. The owner deliberately permits an own request on any typed owner message and trusts the agent's free-text interpretation; no hold was added.
- Existing T3 web/mobile one-answer cases remain selected (six combinations). New synthetic cases also cover Claude Code, Codex, T3 with multiple providers, Aegis, Cursor, Antigravity and the shared Aegis/Grok Build typed-record path. This is fixture evidence, not a claim of live UI acceptance. Desktop RPC signatures did not change.

### Remaining refusal inventory

Each line states the mechanism, when an actual typed message can still be refused, and the next action. These are content/state constraints, deliberate preferences or operational failures; none asks for a code or prescribed wording.

| Mechanism | When it refuses; what happens next |
| --- | --- |
| Supported action and argument schema | The requested action, account, flag or value is invalid; the agent corrects the structured call using the owner's existing direction. |
| Owner direction preference | The owner deliberately disabled conversational changes; use `direction.on` in the conversation to enable them. |
| Native front-end preferences | The owner disabled that front end or session-only binding; enable it through an already enabled conversation or Settings. Defaults for the supported fronts are unchanged. |
| Missing typed-message record | No recorded typed owner turn is available, or a native producer classified it as a machine/job delivery; type in the supported conversation or repair its recording integration. No front-end-specific challenge was added. |
| Conversation identity | The caller has no session or T3 maps it to zero/multiple threads; repair that mapping and retry the same answer. |
| T3 read failure | T3's database/message rows cannot be read after bounded retries; restore readability and retry. It does not instruct an endless ask-again loop. |
| Reply after the ask | The available message precedes the held question or the read that asked it; show the whole question and answer once. An old question has no answering deadline. |
| Native read boundary | A settle is attempted in the same turn that first showed its list; the next typed answer applies. An identical re-read preserves the previous boundary. |
| T3 answer boundary | A normal answer lacks a read showing that answer or a message after the ask; show the item and answer once. Own requests keep the instant exemption. |
| Changed content on re-read | The operative pinned arguments changed; show the changed version and get its answer. The old answer does not adopt new content. |
| Clipped display | The item is shortened; read its full key before the initial answer. Identical full re-reads do not consume an existing answer. |
| Priority content pin | Pending entries/exceptions or the priority they replace changed; show the current priority and answer that version. |
| Rule content/revision pin | The pending rule or displayed owner revision changed, including a seeded-rule retirement; show the current words before adopting/retiring them. |
| Proposal content pin | The pending proposal/version changed; show and answer the current proposal. |
| Setting request identity/value/prior-state pin | Another request or prior value replaced the one shown; show the current request and answer it. Comparison stays under the mutation lock. |
| Tier proposal and owner revision pin | The proposal event or owner table changed, even if proposed values repeat; show the current table before changing it. |
| Account-list version pin | The account request changed; show the current account diff, now including homes, and answer it. |
| Unwordable priority/proposal | Existing projection cannot disclose the operative content; the agent supplies a fully wordable request rather than asking the owner to accept hidden changes. No new F5 refusal was added. |
| Legacy file hold without snapshot | The old tracker/Owed hold has no reviewed bytes; decline it or reissue it with the full input and answer that question once. It does not block another direction. |
| Invalid or unreadable file snapshot | Saved bytes cannot be read or fail their content digest, or the source violates existing safe-file restrictions; repair/reissue the full input. No mutable path is silently re-read as the reviewed file. |
| Settled/applying request | The request already applied, was declined, failed or is applying; inspect its existing receipt/outcome. It cannot apply twice. |
| Request forgotten during admission | Forget removed the source while it was being held; submit the direction again if it is still wanted. |
| Missing undo snapshot/log write | The database or durable action log cannot supply/save the before-state and receipt; restore it and retry the same direction. |
| Undo over newer owner state | A later change would be overwritten; the agent explains the loss and can use `over_newer` after one ordinary answer. No word/hash challenge remains. |
| Domain prerequisites | Import conflicts, invalid rows, permissions or already removed/purged data make the action inapplicable; resolve the reported business state. These checks were not expanded. |
| App/terminal transport | A caller falsely uses app-only transport, stdin is not interactive, or a terminal is inside a known harness; use the normal conversation action. F8 preserves the explicit explanation. |

### Accepted limits

Khipu does not parse the owner's words. “Thanks” can count after a shown question, and the agent decides what the owner meant. An agent's own free-text request may apply on any typed owner message; several own requests can therefore apply from that message. Each held request still applies only once. This is the owner's explicit tradeoff, not a provenance guarantee.

Same-user code that forges local records, hides/reparents its process tree or drives the owner's app is outside the gate. The separate review reported that the two-fork terminal reparenting probe still reaches apply (`auth: terminal`); that result is recorded as an accepted limit, not rerun or repaired in this pass. No Touch ID, codes or word-matching were added.

### Verification and boundaries

The initial unchanged-scope tests failed because they asserted the now-removed protections. The compatibility run was **100 failed, 479 passed**; after policy-test updates it was **503 passed**. New flow acceptance initially had two fixture errors (a nonexistent undo field and a missing Owed `reason` argument), corrected without weakening the owner-state assertions.

Broad run 1: **4 failed, 1642 passed, 7 skipped; 34 subtests passed**, exit 1, 142.29 seconds (`/tmp/khipu-gate-evidence/simple-broad-1.txt`). Failures were the old tracker catch-up app-only expectation, two T3 fixtures depending on removed dispatched-lane checks, and the bulk test's missing `reason` argument. The importer test now requires its real `no_import` prerequisite refusal. The T3 read-failure test now produces a real unreadable mapping and retains the strict `t3_unreadable` assertion.

The first undo/help targeted run was **1 failed, 202 passed** because help text still expected the removed “ask again” instruction. A later targeted invocation in the sandbox was **3 failed, 230 passed, 38 errors**: scratch `initdb` setup was blocked, and its partial tests left pending action state for subsequent tests. The same non-database order with a pending-context trace was **212 passed** with no pending-context leaks. Running the authorized scratch-DB fixtures outside the sandbox then passed **273 tests**, exit 0, 40.11 seconds (`/tmp/khipu-gate-evidence/simple-targeted-authorized.txt`). No source workaround for the sandbox or test-order issue was added.

Broad run 2: **1 failed, 1637 passed, 7 skipped; 34 subtests passed**, exit 1, 103.05 seconds (`/tmp/khipu-gate-evidence/simple-broad-2.txt`). Its only failure was `test_a_direct_action_outside_the_window_is_still_refused_as_today`, which asserted the superseded six-hour authorization cutoff. Its replacement requires successful application, a receipt and a successful actual undo on the seven-hour-old recorded turn. Focused acceptance after that test update: **66 passed**, exit 0, 6.33 seconds (`/tmp/khipu-gate-evidence/simple-last-targeted.txt`).

Final broad run 3: **1638 passed, 7 skipped; 34 subtests passed**, exit 0, 79.10 seconds (`/tmp/khipu-gate-evidence/simple-broad-3.txt`), across 44 selected files. The exact broad selector is the requested owner/T3/needs-you/MCP/priority/s19a/replication set, plus tracker import, Owed tidy, ledger owner and project-group files touched by this gate. Known unrelated `test_routing_received` and `test_routing_s5_fix3` were not selected. Each run uses `env -u KHIPU_DATABASE_URL -u KHIPU_LIVE_TESTS -u CLAUDE_DISPATCHED_LANE`, a fresh temporary HOME and pycache, `KHIPU_KEYCHAIN=0`, Python 3.11, `pytest -q -p no:cacheprovider`, from `packages/cli`, with a 600-second subprocess timeout. PostgreSQL state is disposable fixture state; the shared checkout supplies dependencies read-only.

Before the owner changed scope to skip the live hash measurement, the earlier read-only measurement had already completed: 276 hook rows, 83 mapped threads, 89 owner messages, 88 matched pairs, 87 matching hashes, 1 mismatch, 188 unmatched hooks, 0 ambiguous matches and 0 attachment pairs. The mismatched pair did not normalize under the existing owner-text canonicalization. Only counts and hashes were emitted; no message contents were output. No further live measurement was performed. Removing the equality challenge makes this difference irrelevant to authorization. The mismatch-shaped regression uses synthetic differing hook/message text.

No live Khipu state, configuration, hub, slot or other harness controls were written. No keys or portfolio contents were read. The historical statement below about no live reads applies to the preceding completion pass, not that already-completed, explicitly requested read-only T3 measurement.

### Test changes required by the simpler policy

The tests for content pins, immutable files, per-request single application, receipts, undo, unreadable T3 state and six T3 web/mobile one-answer combinations remain. There are 19 new flow test functions (38 parameterized cases), and the undo suite now accepts ordinary answers without owner-word matching. The following removed functions enforced superseded security policies; they were retired rather than renamed to disguise changed expectations. Updated functions are listed separately. This appendix records exact test names so the policy change is reviewable.

Removed functions (85):

- `test_ledger_import.py`: `test_a_catch_up_import_is_not_made_by_direction`.
- `test_owner_accepted_limit.py`: `test_an_ordinary_job_that_kept_the_owners_session_id_is_refused`.
- `test_owner_change_losses.py`: `test_a_real_dispatched_job_still_says_dispatched_job`.
- `test_owner_direction.py`: `test_a_call_from_any_dispatch_marker_is_refused`, `test_a_call_naming_two_conversations_is_refused`, `test_a_dispatched_job_that_kept_the_conversations_session_id_is_refused`, `test_a_non_t3_session_with_the_sdk_child_markers_is_still_refused`, `test_a_session_id_from_another_conversation_is_refused`, `test_a_t3_conversation_on_a_dispatched_lane_is_refused`, `test_a_t3_delegated_child_threads_turn_is_not_eligible`, `test_a_t3_delegated_child_with_the_sdk_child_markers_is_refused`, `test_a_t3_lookup_failure_with_the_sdk_child_markers_is_refused`, `test_a_t3_top_level_thread_with_a_job_marker_is_refused`, `test_an_unmarked_t3_child_turn_is_refused_at_the_check`, `test_refusal_stale`.
- `test_owner_direction_mcp.py`: `test_a_stale_turn_is_refused`.
- `test_owner_gate_completion.py`: `test_changed_request_cannot_use_an_older_receipt_from_the_same_turn`, `test_creation_of_own_request_reserves_turn_before_old_question_can_claim_it`, `test_damaged_review_contract_cannot_be_repaired_by_listing`, `test_missing_content_pin_or_snapshot_in_sealed_hold_refuses`, `test_native_own_request_cannot_also_answer_the_old_question`, `test_old_own_request_cannot_borrow_a_later_typed_turn`, `test_own_request_spends_only_its_creation_turn_and_leaves_older_question_pending`, `test_turn_switch_at_mutation_gate_cannot_retarget_own_request`.
- `test_owner_gate_gaps.py`: `test_confirm_message_cannot_authorize_unrelated_instant_writes`, `test_one_fresh_turn_allows_one_instant_change_without_a_yes_prompt`, `test_t3_explicit_confirmation_rejects_a_different_hook_prompt`, `test_t3_reply_cannot_be_reused_after_native_session_switch`, `test_two_held_changes_cannot_share_one_reply`.
- `test_owner_needs_you.py`: `test_a_dispatched_job_reads_but_cannot_settle`, `test_a_dispatched_read_records_nothing`, `test_a_held_request_refused_as_not_from_the_hook_confirms_from_another_conversation`.
- `test_owner_provenance.py`: `test_a_forged_issuer_for_a_process_never_seen_is_refused`, `test_a_forged_issuer_start_for_a_known_replaced_process_is_refused`, `test_a_forged_record_cannot_make_a_job_read_as_a_shell`, `test_a_forged_record_through_a_symlink_cannot_make_a_job_read_as_a_shell`, `test_a_known_process_that_runs_something_else_now_is_not_known`, `test_a_matching_name_with_the_wrong_ancestry_is_refused`, `test_a_process_above_the_harness_with_no_path_is_not_the_conversations_to_restart`, `test_a_process_never_seen_with_a_readable_path_is_refused_and_told_to_restart`, `test_a_refused_issuer_never_carries_the_owner`, `test_a_replaced_job_with_its_own_record_is_its_own_harness`, `test_a_reused_process_number_with_a_matching_name_is_refused`, `test_a_turn_whose_issuer_is_not_above_the_caller_is_refused`, `test_running_the_shim_by_hand_cannot_make_a_turn_that_passes`, `test_the_agents_shell_under_a_known_replaced_process_stays_refused`, `test_the_recorder_run_from_an_agents_bash_tool_is_not_the_hook`, `test_the_refusal_is_true_for_a_conversation_and_for_a_job`.
- `test_owner_review_gaps.py`: `test_clipped_held_request_cannot_be_confirmed`, `test_hidden_held_request_cannot_borrow_filtered_read`.
- `test_owner_undo_newer.py`: `test_direction_over_newer_loose_wording_is_refused`, `test_direction_over_newer_refusal_names_the_phrase_to_type`, `test_direction_over_newer_when_the_owner_only_asked_for_an_undo_is_refused`, `test_direction_over_newer_with_words_that_are_not_the_latest_message_is_refused`, `test_direction_over_newer_without_the_owners_words_is_refused`.
- `test_t3_owner_confirm.py`: `test_a_boundary_that_would_drop_a_live_exclusion_refuses`, `test_a_copy_sharing_an_excluded_turns_occurrence_never_confirms`, `test_a_delegated_or_child_thread_with_an_owner_row_is_refused`, `test_a_direct_action_outside_the_window_is_still_refused_as_today`, `test_a_dispatched_call_cannot_hold_or_confirm`, `test_a_foreign_sdk_process_from_a_non_t3_parent_is_refused_in_t3`, `test_a_machine_hook_turn_in_t3_never_borrows_the_owners_message`, `test_a_replaced_t3_claude_never_seen_is_refused_and_told_to_restart`, `test_a_t3_confirm_in_a_conversation_t3_runs_on_cursor_or_antigravity_fails_closed`, `test_a_t3_confirm_on_a_two_day_old_request_needs_the_replys_hook_turn`, `test_an_antigravity_flushed_copy_from_a_later_preinvocation_never_confirms`, `test_an_identical_antigravity_yes_for_a_second_request_is_refused_with_the_new_message`, `test_an_older_eligible_turn_never_confirms_when_the_current_turn_is_a_late_copy`, `test_review2_p1_a_duplicate_after_the_prune_never_confirms`, `test_review2_p1_an_antigravity_postinvocation_flush_copy_never_confirms`, `test_review2_p2_a_t3_confirm_from_a_process_inheriting_the_session_is_refused`, `test_review2_p2_a_t3_confirm_needs_t3_trusted_now`, `test_review3_p1a_a_stepless_fallback_read_again_from_the_transcript_never_confirms`, `test_review3_p1b_a_fallback_step_read_again_after_the_prune_never_confirms`, `test_review3_p2_a_foreign_headless_process_recording_its_own_hook_turn_in_t3_is_refused`, `test_review3_p2_a_snapshot_that_cannot_keep_its_tied_ids_refuses`, `test_review4_a_copy_after_a_later_row_never_stands_in`, `test_review4_p1_a_boundary_saved_before_the_words_were_kept_is_refused_and_asked_again`, `test_review4_p1_a_machine_turn_never_borrows_an_earlier_owner_reply`, `test_review4_p1_renumbered_steps_never_let_pre_ask_words_confirm`, `test_review4_p2_a_flushed_copy_of_a_fresh_antigravity_reply_stands_in_for_it`, `test_review4_p2_a_foreign_process_with_unknown_headless_is_refused_in_t3`, `test_review5_a_late_copy_of_an_earlier_antigravity_row_stays_refused`, `test_review_p1_a_delayed_duplicate_of_the_held_turn_never_confirms`, `test_the_owner_replies_after_an_ask_on_a_40_day_old_request_in_t3`.

Updated functions (8):

- `test_owner_change_losses.py`: `test_a_failed_t3_lookup_on_confirm_says_unreadable_not_dispatched_job`.
- `test_owner_direction.py`: `test_every_action_passes_from_a_recent_owner_turn_and_is_refused_without_one`, `test_the_window_is_a_setting`.
- `test_owner_direction_mcp.py`: `test_the_tool_takes_only_an_action_and_its_arguments`.
- `test_owner_gate_completion.py`: `test_legacy_hold_never_applies_after_a_full_read_and_later_yes`, `test_legacy_refusal_can_be_replaced_only_by_a_freshly_shown_hold_and_reply`.
- `test_owner_needs_you.py`: `test_how_to_settle_does_not_send_a_held_request_to_the_app`, `test_what_to_do_when_a_turn_is_unproven_is_in_the_answer`.

## Historical implementation and completion record


Status: both blockers are closed. The final hermetic broad gate passed: **1665 passed, 7 skipped; 34 subtests passed**, exit 0. The initial handoff below records `5a5b3e1b`; the completion pass at the end supersedes its two blockers.

Branch: `fix/owner-gate-gaps`, based on `origin/feat/needs-you-in-conversation` (`d0f066dc`), merged with `origin/fix/owner-proof-replaced-binary` (`178ccc79`). The merge was clean. No live Khipu state, hub, live slot, private keys or portfolio files were accessed. No PR was opened.

## Implemented behavior

1. `needs_you.prepare_answer` expands direct priority confirm/undo, rule confirm/undo/dismiss, account-request answers, setting answers and tier confirmation to the complete matching item. Missing, withheld, stale, unwordable or oversized disclosures refuse. Direct priority answers carry `pending_priority_pin` in `expect`, compared under the routing lock. The list returns the same pins and ordinary web/mobile flows retain one yes.
2. `owner_direction._review_files` snapshots the two actions consuming file contents: `ledger.import.file` and `historical clean-up plan`. It shows the entire reviewed JSON and digest, refuses private/symlink paths, secrets and files longer than 12,000 characters, and replays the exact snapshot. Directory arguments identifying accounts are not file-content imports. Snapshot fields participate in Forget scrubbing.
3. Setting requests receive a fresh random identity. The held answer automatically carries the pin of identity, value and previous state; mutation compares it under the config/routing locks.
4. `_claim_message` atomically spends a typed message once before mutation. T3 consumption survives native-session switches. Explicit T3 proof also matches the actual owner-message hash to the prompt-hook hash. Instant writes do not consume the turn during preflight; the mutation gate consumes it. A pending question blocks unrelated instant changes.
5. `typed_yes` checks the complete readable ancestry for known harnesses and dispatched markers, including through nested ptys. Kernel names can deny when executable paths disappear; they never establish app authorization. The ordinary owner-terminal positive test remains covered.
6. Each pending held request records complete disclosure separately per thread/session. Filtered or clipped reads revoke that thread's eligibility. Only fully shown held items grow that request's ask boundary. Read signatures describe the latest read, not a cumulative union.
7. Tier pins include the proposal event and prior owner-table event; stale identical reproposals cannot restore old owner tables over newer ones.
8. Seed-rule retirement carries and validates the displayed revision under the write lock. Rule undo/dismiss now compare and mutate under that lock too.
9. Cap wording includes purpose, slack, tier, sensitivity, expiry and revision, alongside its limit/window.

## Verification

All pytest commands ran from `packages/cli`, unsetting `KHIPU_DATABASE_URL`, `KHIPU_LIVE_TESTS`, and `CLAUDE_DISPATCHED_LANE`, with new temporary HOME and bytecode cache folders, `KHIPU_KEYCHAIN=0`, and `-q -p no:cacheprovider`. Python: `/opt/homebrew/bin/python3.11`. Dependencies were read from the starting checkout's `.python_libs`; each broad subprocess had a 600-second timeout.

Selection: `tests/test_owner_*.py tests/test_t3_owner_confirm.py tests/test_needs_you*.py tests/test_mcp_*.py tests/test_routing_priority*.py tests/test_routing_s19a*.py tests/test_routing_replication_switch.py tests/test_ledger_import*.py tests/test_archived_owed_cleanup*.py`.

- Original exploit probes on the merged baseline: **3 passed, 1 failed**. These probes assert vulnerable behavior. The repeated-message, mutable-file and setting-retarget probes reproduced. The priority-hidden-text probe failed because the merged feature already disclosed the exception; the content pin was still missing. Baseline log: `/tmp/khipu-gate-evidence/baseline.txt`.
- Additional review probes before their repairs: **5 failed, 8 passed**. Failures: `test_clipped_held_request_cannot_be_confirmed`, `test_hidden_held_request_cannot_borrow_filtered_read`, `test_old_tier_undo_cannot_overwrite_new_owner_table_after_same_proposal_returns`, `test_cap_words_show_scope_and_slack`, and `test_seeded_rule_undo_cannot_retire_newer_owner_words`. Six web/mobile one-yes cases passed. Log: `/tmp/khipu-gate-evidence/recheck-baseline.txt`.
- Broad run 1: **39 failed, 1506 passed, 7 skipped, 74 errors; 34 subtests passed**. Scratch PostgreSQL initialization/socket operations were blocked by the sandbox. Old multi-write and cross-thread undisclosed-confirm expectations also failed.
- Broad run 2, with permission for disposable PostgreSQL fixtures: **19 failed, 1604 passed, 7 skipped; 34 subtests passed**. Remaining failures concerned adapting old fixtures/assertions to one-message consumption, added read rows and earlier stale-pin refusals.
- Broad run 3: **1 failed, 1626 passed, 7 skipped; 34 subtests passed**, exit 1, 117.70 seconds. No fourth broad run was made. Log: `/tmp/khipu-gate-evidence/suite-3.txt`.

Final failure (assertion retained): `tests/test_owner_needs_you.py::test_a_conversations_own_request_still_applies_on_the_owners_turn`, line 1102: `assert done.get("applied") is True`; actual `None`. The pending-question restriction blocks the conversation's own-request exemption, contrary to the task's required behavior. The three-run cap is exhausted; implementation stopped.

The two supplied probe files are retained as repository regression tests, with exploit-positive assertions inverted in `test_owner_gate_gaps.py`. Added coverage includes two held actions sharing one reply, one prompt-free instant write followed by refusal of a second, explicit T3 hook/message mismatch, native-session switching, identical setting resubmissions, both file snapshots, terminal ancestry, and the supplied six web/mobile flows. Existing behavioral assertions were retained; separate writes now use separate synthetic owner turns, and direction-matching alternatives use independent parameterized cases.

Desktop call sites were source-inspected: config buttons still use `--from-app --answer-request KEY ACTION`, priority buttons keep their existing `expect`, and rule retirement's new `expect` argument is optional. No desktop signature changed. Native UI and live transport were not exercised.

## Initial handoff: blockers and limits (superseded below)

- **Required behavior regression:** the conversation's own request must still apply on its typed turn when another held question exists. The current guard is too restrictive. Do not weaken the failing assertion.
- **Source-inspected, unprobed legacy-hold concern:** old held rows lack snapshots and pins. A full read can add disclosure eligibility to such a row; file replay falls back to the current path when no snapshot exists. Legacy unpinned setting holds may also remain confirmable. Add an explicit legacy refusal/re-hold path and regression coverage before approval.
- The gate deliberately does not interpret owner words: after full disclosure, “thanks” may be treated as consent. Own-request/instant exemptions still rely on the agent interpreting the owner's intent, bounded to one change per typed message.
- Same-user automation that hides/reparents/renames its ancestry, edits local records, or drives the owner's app remains outside process-provenance protection. This is an accepted same-user limit, not proof that a pty is human.
- No post-suite source edits were made. Known unrelated `test_routing_received` and `test_routing_s5_fix3` were not selected.


## Completion pass from `5a5b3e1b`

Scope: finish the two remaining blockers in `/tmp/khipu-gate-fix`, without subagents or live state. A fresh budget of at most three broad runs was authorized. The only authorized outward action is a plain push of `fix/owner-gate-gaps`, after completion and a green suite.

### Legacy reproduction and resolution

The probe was written before source changes, using real hold/proof/list/replay code and the pre-change persisted shape: no review contract, no disclosure record, and the old pinless arguments where applicable. Tracker and Owed mutation boundaries used fake sinks; all routing/config state was synthetic. Reading the whole held item and then typing a new yes reproduced unauthorized application in **six cases**: rule undo, setting confirmation, tier confirmation, seeded-rule undo, tracker import and Owed plan. Five other legacy cases already refused. Together with the unchanged own-request failure, the baseline was **7 failed, 5 passed**. Exact output: `/tmp/khipu-gate-evidence/completion-baseline.txt`.

New holds now contain a versioned review contract binding their action, complete arguments, disclosed ask and file snapshots. `reviewed_hold` is checked under the requests lock before confirmation; listing cannot add disclosure eligibility to an old or damaged contract. The list offers no confirm for those rows and directs the conversation to request the change again. The old row remains available to decline. Reissuing creates a fresh full ask and a later typed reply is required; the answer to the old question cannot confirm the new hold.

Regression coverage includes priority confirm/undo, rule confirm/undo, account-list proposal, setting, tier, seeded-rule undo, tracker, Owed plan and cap proposal. A second matrix removes a modern content pin or snapshot while leaving its review contract present; damaged arguments, asks, file metadata and missing contracts are also refused. No legacy file path is silently upgraded by rereading its current bytes.

### Why the own-request test was correct

`test_a_conversations_own_request_still_applies_on_the_owners_turn` matches the owner's decision: a new direction and its own request in the same proved typed turn need no question. The blanket pending-question guard was wrong. The original test and assertion remain unchanged.

The repair preserves the guard and makes its exemption specific. Request writers capture the current typed-message fingerprint before writing. A successful proposal reserves that fingerprint for its exact event before returning its receipt. Application compares the creation turn and current event, then the mutation gate checks the reservation token and atomically consumes it. An older held question or unrelated instant write cannot take the reserved message first or reuse it afterward. A stale receipt, replacement event, native-session switch or typed-turn change at the mutation boundary cannot retarget the authorization.

An additional ordering probe proved that merely binding the receipt was insufficient: the older question could spend the message between request creation and application. That probe failed before reservation (`/tmp/khipu-gate-evidence/completion-ordering-probe.txt`) and is retained as a regression. Another probe showed an own direction could still be blocked when the agent read the list during the same turn. Own-request proof now uses the current typed message independently of ask boundaries; ordinary confirmations retain their read/ask boundaries. Its regression keeps the older question pending and asserts its precise `not_confirmed` refusal when the read itself is the latest boundary. Output before repair: `/tmp/khipu-gate-evidence/completion-same-turn-read-probe.txt`.

These cases cover T3, first-turn requests, Claude Code and Codex native fronts, an older question claiming before and after application, stale receipts, altered pending events, and a turn switch at the mutation gate. Multiple proposals in one typed turn cannot transfer the first request's reservation to a replacement request.

### Completion verification

The hermetic environment, Python, dependencies, 600-second timeout and broad selectors are the same as above. The new `tests/test_owner_gate_completion.py` is included by `tests/test_owner_*.py`. Scratch PostgreSQL fixtures required sandbox escalation; all HOME/config/database fixtures remained disposable.

- Targeted initial repair: **46 passed**, followed by **48 passed** with mutation-boundary and re-hold coverage.
- Expanded targeted run: **59 passed, 2 failed** because the two new native fixtures omitted the helper's required `ago` argument; corrected native fixtures: **2 passed**.
- Reservation targeted run: **61 passed, 1 failed** on preserving the existing refusal-message wording; corrected run: **62 passed**.
- Same-turn-read targeted run after source repair: **62 passed, 1 failed** because the new test expected `message_used` rather than the earlier correct `not_confirmed` boundary refusal. The assertion now requires `not_confirmed`, successful own application and the older row still pending; no behavioral assertion was weakened.
- Final targeted gate: **63 passed**, exit 0, 3.90 seconds. Log: `/tmp/khipu-gate-evidence/completion-targeted-acceptance.txt`.
- Broad run 1, before the final reservation/ordering additions: **1663 passed, 7 skipped; 34 subtests passed**, exit 0, 101.38 seconds. Log: `/tmp/khipu-gate-evidence/completion-broad-1.txt`.
- Broad run 2: **1664 passed, 1 failed, 7 skipped; 34 subtests passed**, exit 1, 68.57 seconds. Only the new same-turn-read test's refusal-code expectation failed. Log: `/tmp/khipu-gate-evidence/completion-broad-2.txt`.
- Final broad run 3: **1665 passed, 7 skipped; 34 subtests passed**, exit 0, 81.31 seconds. No fourth broad run was made. Log: `/tmp/khipu-gate-evidence/completion-broad-3.txt`.

One targeted invocation was launched from the worktree root instead of `packages/cli`; collection exited 4 without running tests. It was rerun from the required directory. It was not a broad run.

### Limits and operational consequences

No new security bypass is accepted. Legacy or damaged holds must be reissued; they cannot be automatically adopted on an old answer. A message reserved for one request cannot authorize a replacement request. The existing limits remain: Khipu hashes but does not interpret words, so agent interpretation still determines whether an own request reflects the owner's direction and whether “thanks” answers a shown question. Same-user code that rewrites local records, hides/reparents ancestry or drives the app remains outside process-provenance protection. The review contract is a content-integrity check, not protection against same-user code recomputing local hashes.

No live hub, live slot, real Khipu config, private keys or portfolio contents were accessed. No subagents, tags, release, PR, merge or promote operation was used. Desktop signatures did not change. Acceptance is verified. No blockers remain. No source or test edits followed the final broad gate. Publication is limited to the authorized plain branch push; its head is reported at handoff.
