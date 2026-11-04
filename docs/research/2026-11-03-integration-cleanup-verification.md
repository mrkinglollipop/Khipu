# Integration cleanup verification

Branch: `integrate/khipu-cleanup`, isolated worktree `/tmp/khipu-int`, starting head `7f8525c5`. All implementation and verification were performed directly, without subagents. No live Khipu state or harness controls were written; no keys or portfolio contents were read. Only a plain push of this branch is authorized.

## Disposition of each reported new failure

Each row names the original failing case and decides code versus test. Content pins, immutable held file snapshots, typed-no refusal, pending-request restoration and dismissed-rule behavior remain required.

| Original failing case | Decision and repair |
| --- | --- |
| `test_owed_origin_auth_regressions::test_invalid_prompt_cannot_confirm[stale]` | Test: the removed six-hour cutoff no longer invalidates a recorded owner turn; require successful correction, its durable inverse and applied request state. |
| `test_owed_origin_auth_regressions::test_invalid_prompt_cannot_confirm[wrong_hook]` | Test: the removed process/hook challenge no longer invalidates a recorded owner turn; retain correction and inverse/state checks. |
| `test_owed_origin_recheck::test_stale_session_cannot_extend_window_through_pty` | Test: an old typed turn needs no window extension; require the correction to apply while the agent-created pty still cannot change the window preference. |
| `test_owed_paging_bulk::test_a_bulk_close_of_the_owners_items_passes_with_the_app_or_a_typed_yes` | Test fixture: the simulated terminal inherited the agent process tree; model an owner shell and retain actual yes parsing, changed counts and app authorization checks. |
| `test_owed_paging_bulk::test_a_typed_no_refuses_and_changes_nothing` | Test fixture: use that owner-shell tree so the actual no is read; retain failure, the refusal wording and unchanged database assertions. No typed-no code was weakened. |
| `test_owner_needs_you::test_every_answering_action_is_offered_and_every_action_has_words` | Code: the merged `outbox.span_covered` owner action lacked words; add its description without changing authorization. |
| `test_routing_exceptions_review2::test_undo_of_a_confirm_returns_the_request_pending_over_the_stored_priority_it_replaced` | Code: a valid native answer was made into a new hold; apply its pinned request on that answer so the existing inverse restores the pending request and replaced priority. |
| `test_routing_instant_apply::test_a_handoff_or_job_marker_keeps_the_request_pending` | Code: restore use of the existing universal job-marker classification in the shared caller-session path; a machine context cannot borrow an owner's recorded turn. |
| `test_routing_instant_apply::test_a_t3_message_outside_the_window_holds` | Test: remove the obsolete expiry expectation; require application, changed state and a receipt after six hours. |
| `test_routing_instant_apply::test_a_t3_priority_request_applies_with_proof_and_stays_pending_without` | Test: an own request on a typed T3 owner message needs no prior question; require instant application and its actual undo, retaining the later-answer case. |
| `test_routing_undo_legacy_and_batches::test_a_legacy_rule_undo_over_a_pending_edit_is_refused` | Code: execute the valid pinned native answer so the actual legacy undo is reached; the existing provenance limitation remains and no pending edit is relabeled as owner state. |
| `test_routing_undo_provenance::test_undo_puts_a_pending_request_back_pending_and_the_conversations[route.priority.undo-args2]` | Code: remove the extra native hold; retain exact pending state, conversation author/words and the replaced owner priority on undo. |
| `test_routing_undo_provenance::test_undo_puts_a_pending_request_back_pending_and_the_conversations[route.priority.confirm-args3]` | Code: the same valid-answer repair reaches the existing inverse; retain all provenance/restoration assertions. |
| `test_routing_undo_provenance::test_undo_over_a_pending_rule_edit_puts_it_back_as_the_conversations[route.rule.undo-args0]` | Code: the same valid-answer repair reaches the existing inverse and restores the pending edit with its original author and words. |
| `test_routing_undo_provenance::test_undo_over_a_pending_rule_edit_puts_it_back_as_the_conversations[route.rule.dismiss-args3]` | Code: dismissing a pending edit is the displayed undo of that proposal; reuse that item's complete words and revision pin, then retain the existing restoration assertions. |
| `test_routing_undo_provenance::test_undo_never_brings_back_a_dismissed_proposed_rule_as_the_owners` | Code: let the typed dismissal apply; keep the existing inverse's refusal to revive a dismissed new proposal. |
| `test_routing_undo_round5::test_a_legacy_confirm_undo_is_refused` | Code: let the native confirmation apply, reaching the existing legacy inverse check without any new guard. |
| `test_work_structural::test_a_typed_yes_on_a_terminal_counts_as_routing_does` | Test fixture: explicitly model an owner terminal rather than inherit this agent's ancestry; retain real yes/no reads. |
| `test_work_structural::test_only_safe_read_parses_a_path_given_to_a_reader` | Test inventory: include the new immutable snapshot reader; apply the no-normalization assertion to its file-reading functions rather than unrelated executable-path diagnostics. |
| `test_routing_acceptance::test_4pa_cost_20000_events_with_18000_samples_stays_under_budget` | Load-sensitive: passed alone unchanged, 1 passed in 3.02 seconds; no threshold or workload was reduced. |

The native-answer repair is shared code in `owner_act.act`: after `owner_direction.check` succeeds, a pending-answer projection alone does not force a second question. The combined priority amendment still asks about the extra pending changes it would adopt. All offered content pins still reach the mutation's existing lock/version checks. Universal machine classification reuses `owner_turns.job_marker`; it adds no word, hash, expiry, quota, reservation or seal mechanism.

Additional owner tests were updated to the same approved behavior: `test_owner_needs_you::test_outside_t3_an_unshown_answer_is_held` now requires a native typed direction to apply its pinned answer; the four pending rule/priority/setting/accounts tests in `test_owner_direction_mcp` now require application and an owner-action id on the first answer. The former `test_a_t3_owner_reply_is_not_rejected_for_a_lane_marker` now requires a job lane to refuse, leave the request pending, and let the owner's original answer apply after leaving the job context without another typed answer.

## Wording-only desktop addition

The two Conversations unknown-state strings now read “Last heard: not known yet” and “Not known yet who has it”. The allowance-order switch has exactly the owner's requested sentence: “Khipu lists the account closest to wasting its allowance first; a priority you set always comes first.” No test required a fact unique to the removed paragraph, so no Rarely needed copy or layout was added. Test strings and the rendered switch-help assertion were updated; no layout/style markup changed.

## Environment and oracle record

The CLI environment is `/tmp/khipu-int-venv`, created with Python 3.11 and `pip install -r packages/cli/requirements.txt pytest pytest-subtests`: psycopg 3.3.5, typing_extensions 4.16.0, packaging 26.3, cryptography 50.0.1 and PyYAML 6.0.3. Desktop dependencies were installed with `npm ci --ignore-scripts` using this branch's lockfile.

All pytest runs use `env -u KHIPU_DATABASE_URL -u CLAUDE_DISPATCHED_LANE HOME="$(mktemp -d)" KHIPU_KEYCHAIN=0 PYTHONPYCACHEPREFIX="$(mktemp -d)" /tmp/khipu-int-venv/bin/python -m pytest -q -p no:cacheprovider`, from `packages/cli`. The test suite's conftest independently isolates Khipu state, Keychain and providers. PostgreSQL fixtures are disposable and require sandbox escalation. Full runs select all `tests`, without exclusions or reduced benchmark rounds, and have an explicit 7200-second subprocess timeout. Full-test XML and logs live under `/tmp/khipu-int-evidence`.

- Standalone routing performance case: **1 passed**, 3.02 seconds (`performance-alone.txt`).
- Initial listed-path/baseline reproduction: **19 failed, 322 passed**, interrupted with SIGINT during the 50,000-fact/200-round baseline copy benchmark at 650.53 seconds. It reproduced the 19 owner-path failures; the separately passing performance case makes the reported 20th. The unchanged copy benchmark is included in the full oracle; SIGINT allowed normal pytest fixture teardown (`reproduction.txt`).
- Undo repair: **36 passed, 1 failed**, the remaining failure identifying the pending-edit dismissal projection; that projection is now fixed (`undo-after.txt`).
- Listed owner-path rerun: **306 passed, 3 failed**; two were newly added receipt assertions on non-routing correction actions (corrected to existing auth/inverse assertions), and one retained the withdrawn native shown-read policy (`targeted-after.txt`).
- Expanded owner-path rerun: **1054 passed, 5 failed**; one still asserted that a job-lane context could borrow a T3 owner message, and four still expected the extra native yes. Updated focused acceptance: **36 passed**, 1.59 seconds (`targeted-native-pass.txt`). One editing command used a worktree-root-relative filename from `packages/cli` and failed before writing; its immediately launched tests still used the old four expectations. The edit was retried with an absolute worktree path before the passing run.
- TypeScript: `./node_modules/.bin/tsc --noEmit`, exit 0 (`frontend-tsc.txt`).
- Initial frontend Vitest: **1385 passed, 5 failed**, 75 files; all five failures were Chrome SIGABRT launch failures in sandboxed layout fixtures, not failed layout assertions (`frontend-vitest.txt`).
- Authorized frontend Vitest: **1390 passed, 75 files passed**, exit 0, 85.17 seconds, including headless Chrome rendering checks (`frontend-vitest-authorized.txt`).
- Reported baseline files excluding the long copy benchmark: **81 passed**, 1.51 seconds (`baseline-fast.txt`); this includes the formerly failing graph recovery/audit setup cases.
- Full CLI run **1 of at most 3**: **6 failed, 15110 passed, 167 skipped, 11 xfailed, 2 xpassed, 4 warnings, 4119 subtests passed; 0 errors**, exit 1, 3114.64 seconds (`full-1.txt`, `full-1.xml`). All tests under `packages/cli/tests` were selected; no workload, threshold or skip was changed. The six remaining failures are classified below. No source or test edits followed this full run. No second full run was needed because only the explicitly permitted baseline/environment outcomes remain.
- The full run's timing failure was rerun alone after completion: **1 passed**, 2.85 seconds (`performance-alone-after-full.txt`). Its unchanged assertion requires the changed push to take less than 1.0 second; it passed both before and after the full run. The full-run measured push was 1.268 seconds. This is treated as the owner's already-identified load-sensitive environment outcome, not a green full-suite timing result.

## Baseline/environment status

The owner's M1 baseline comprised `test_bundle_seal`, doctor cases in `test_cli_recall_quality`, `test_graph_summary_temp_copy`, `test_graph_triage_q2`, `test_provenance_copy_bench`, plus 24 setup errors in `test_embed_crash_recovery` and `test_nightly_audit_graph`. This pass installs the declared PyYAML dependency rather than suppressing any test. The graph recovery/audit setup errors are gone. All 81 tests in the six reported baseline files other than the long copy benchmark passed in isolation, but four doctor cases remain order-sensitive in the full run.

Every remaining failure is listed here, with its baseline status. The four doctor cases and the copy benchmark are explicitly in the owner's main-baseline list; their underlying code-versus-test attribution remains unresolved and their old assertions/behavior were retained. No health check was suppressed and no performance bound was relaxed.

| Remaining full-run failure | Baseline status and observed output |
| --- | --- |
| `test_bundle_seal.py::DoctorFoldsBundleSealTest::test_clean_signature_keeps_doctor_green` | Known main baseline; `rc == 0` failed with `2 != 0`. Passes in the isolated 81-test baseline group, fails in full-suite order. |
| `test_bundle_seal.py::DoctorFoldsBundleSealTest::test_na_off_a_maintainer_checkout_keeps_doctor_green` | Known main baseline; `rc == 0` failed with `2 != 0`. Passes in the isolated baseline group, fails in full-suite order. |
| `test_cli_recall_quality.py::DoctorAggregateOkTest::test_everything_green_including_probe_and_snapshot_is_ok` | Known main baseline; `rc == 0` failed with `2 != 0`. Passes in the isolated baseline group, fails in full-suite order. |
| `test_cli_recall_quality.py::DoctorAggregateOkTest::test_ratio_metrics_alone_never_fail_doctor` | Known main baseline; `rc == 0` failed with `2 != 0`. Passes in the isolated baseline group, fails in full-suite order. |
| `test_provenance_copy_bench.py::test_the_copy_under_a_sustained_workload_meets_its_bounds` | Known main baseline; the first failed assertion is `assert not wrote`: one purportedly unchanged pull changed the replica file. All 200 rounds ran unchanged. This is an observed write-invariance failure, not merely a timing failure. |
| `test_routing_acceptance.py::test_4pa_cost_20000_events_with_18000_samples_stays_under_budget` | Reported new case, explicitly identified by the owner as load-sensitive; full-run push took 1.268 seconds against 1.0. Passes alone before and after the full run, unchanged, so classified as the permitted environment/load outcome. No threshold increase or skip was introduced. |

Full-run baseline-file totals: `test_bundle_seal` 11 cases / 2 failures; `test_cli_recall_quality` 14 / 2 failures; `test_graph_summary_temp_copy` 1 / 0 failures; `test_graph_triage_q2` 6 / 0 failures; `test_embed_crash_recovery` 7 / 0 failures or errors; `test_nightly_audit_graph` 42 / 0 failures or errors. There are no remaining setup errors.

The copy benchmark reported 50,000 facts, 20,000 vectors, 200 rounds, 50 ms round-trip delay, one unchanged pull that wrote, unchanged-pull maximum 1.297 s / median 0.449 s / p99 1.230 s, changed-pull maximum 2.879 s, publication gate maximum 1.353 s, and incremental peak RSS 58.2 MiB. Its first failed assertion prevented later bounds from being evaluated; these are reported measurements rather than claims that the remaining assertions ran.

The full run's watchdog traces identified normal sustained copy publication and the 120-day routing compaction stress case; neither was stopped or reduced. The full run completed and exited 1 normally. The accepted final state is **no remaining owner-path failures, five known main-baseline failures, and the isolated-passing load-sensitive timing case**. This matches the owner's explicit condition for a plain branch push; it is not described as an entirely green CLI suite.

## Limits and publication

The previously approved limits remain: Khipu does not interpret the owner's words; own requests trust the agent's interpretation and can use any typed owner message; same-user code that forges local records or reparents a terminal is outside the gate. No additional owner security mechanism was introduced. Known machine/job markers still identify machine turns, and an actual typed no still changes nothing. Only `integrate/khipu-cleanup` will be pushed after acceptance; no main merge, tags, release, promotion or live write is in scope.

Final verification also passed `git diff --check`. Publication uses only a plain branch push; the final commit SHA and counts are reported in the handoff. No PR, main merge, tag, release, promotion or live Khipu write was performed.
