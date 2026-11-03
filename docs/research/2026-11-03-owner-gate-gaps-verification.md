# Owner-gate repairs: partial implementation, not merge-ready

Branch: `fix/owner-gate-gaps`, based on `origin/feat/needs-you-in-conversation` (`d0f066dc`), merged with `origin/fix/owner-proof-replaced-binary` (`178ccc79`). The merge was clean. No live Khipu state, hub, live slot, private keys or portfolio files were accessed. No PR was opened.

## Implemented behavior

1. `needs_you.prepare_answer` expands direct priority confirm/undo, rule confirm/undo/dismiss, account-request answers, setting answers and tier confirmation to the complete matching item. Missing, withheld, stale, unwordable or oversized disclosures refuse. Direct priority answers carry `pending_priority_pin` in `expect`, compared under the routing lock. The list returns the same pins and ordinary web/mobile flows retain one yes.
2. `owner_direction._review_files` snapshots the two actions consuming file contents: `ledger.import.file` and `owed.tidy.plan`. It shows the entire reviewed JSON and digest, refuses private/symlink paths, secrets and files longer than 12,000 characters, and replays the exact snapshot. Directory arguments identifying accounts are not file-content imports. Snapshot fields participate in Forget scrubbing.
3. Setting requests receive a fresh random identity. The held answer automatically carries the pin of identity, value and previous state; mutation compares it under the config/routing locks.
4. `_claim_message` atomically spends a typed message once before mutation. T3 consumption survives native-session switches. Explicit T3 proof also matches the actual owner-message hash to the prompt-hook hash. Instant writes do not consume the turn during preflight; the mutation gate consumes it. A pending question blocks unrelated instant changes.
5. `typed_yes` checks the complete readable ancestry for known harnesses and dispatched markers, including through nested ptys. Kernel names can deny when executable paths disappear; they never establish app authorization. The ordinary owner-terminal positive test remains covered.
6. Each pending held request records complete disclosure separately per thread/session. Filtered or clipped reads revoke that thread's eligibility. Only fully shown held items grow that request's ask boundary. Read signatures describe the latest read, not a cumulative union.
7. Tier pins include the proposal event and prior owner-table event; stale identical reproposals cannot restore old owner tables over newer ones.
8. Seed-rule retirement carries and validates the displayed revision under the write lock. Rule undo/dismiss now compare and mutate under that lock too.
9. Cap wording includes purpose, slack, tier, sensitivity, expiry and revision, alongside its limit/window.

## Verification

All pytest commands ran from `packages/cli`, unsetting `KHIPU_DATABASE_URL`, `KHIPU_LIVE_TESTS`, and `CLAUDE_DISPATCHED_LANE`, with new temporary HOME and bytecode cache folders, `KHIPU_KEYCHAIN=0`, and `-q -p no:cacheprovider`. Python: `/opt/homebrew/bin/python3.11`. Dependencies were read from the starting checkout's `.python_libs`; each broad subprocess had a 600-second timeout.

Selection: `tests/test_owner_*.py tests/test_t3_owner_confirm.py tests/test_needs_you*.py tests/test_mcp_*.py tests/test_routing_priority*.py tests/test_routing_s19a*.py tests/test_routing_replication_switch.py tests/test_ledger_import*.py tests/test_owed_tidy*.py`.

- Original exploit probes on the merged baseline: **3 passed, 1 failed**. These probes assert vulnerable behavior. The repeated-message, mutable-file and setting-retarget probes reproduced. The priority-hidden-text probe failed because the merged feature already disclosed the exception; the content pin was still missing. Baseline log: `/tmp/khipu-gate-evidence/baseline.txt`.
- Additional review probes before their repairs: **5 failed, 8 passed**. Failures: `test_clipped_held_request_cannot_be_confirmed`, `test_hidden_held_request_cannot_borrow_filtered_read`, `test_old_tier_undo_cannot_overwrite_new_owner_table_after_same_proposal_returns`, `test_cap_words_show_scope_and_slack`, and `test_seeded_rule_undo_cannot_retire_newer_owner_words`. Six web/mobile one-yes cases passed. Log: `/tmp/khipu-gate-evidence/recheck-baseline.txt`.
- Broad run 1: **39 failed, 1506 passed, 7 skipped, 74 errors; 34 subtests passed**. Scratch PostgreSQL initialization/socket operations were blocked by the sandbox. Old multi-write and cross-thread undisclosed-confirm expectations also failed.
- Broad run 2, with permission for disposable PostgreSQL fixtures: **19 failed, 1604 passed, 7 skipped; 34 subtests passed**. Remaining failures concerned adapting old fixtures/assertions to one-message consumption, added read rows and earlier stale-pin refusals.
- Broad run 3: **1 failed, 1626 passed, 7 skipped; 34 subtests passed**, exit 1, 117.70 seconds. No fourth broad run was made. Log: `/tmp/khipu-gate-evidence/suite-3.txt`.

Final failure (assertion retained): `tests/test_owner_needs_you.py::test_a_conversations_own_request_still_applies_on_the_owners_turn`, line 1102: `assert done.get("applied") is True`; actual `None`. The pending-question restriction blocks the conversation's own-request exemption, contrary to the task's required behavior. The three-run cap is exhausted; implementation stopped.

The two supplied probe files are retained as repository regression tests, with exploit-positive assertions inverted in `test_owner_gate_gaps.py`. Added coverage includes two held actions sharing one reply, one prompt-free instant write followed by refusal of a second, explicit T3 hook/message mismatch, native-session switching, identical setting resubmissions, both file snapshots, terminal ancestry, and the supplied six web/mobile flows. Existing behavioral assertions were retained; separate writes now use separate synthetic owner turns, and direction-matching alternatives use independent parameterized cases.

Desktop call sites were source-inspected: config buttons still use `--from-app --answer-request KEY ACTION`, priority buttons keep their existing `expect`, and rule retirement's new `expect` argument is optional. No desktop signature changed. Native UI and live transport were not exercised.

## Remaining blockers and limits

- **Required behavior regression:** the conversation's own request must still apply on its typed turn when another held question exists. The current guard is too restrictive. Do not weaken the failing assertion.
- **Source-inspected, unprobed legacy-hold concern:** old held rows lack snapshots and pins. A full read can add disclosure eligibility to such a row; file replay falls back to the current path when no snapshot exists. Legacy unpinned setting holds may also remain confirmable. Add an explicit legacy refusal/re-hold path and regression coverage before approval.
- The gate deliberately does not interpret owner words: after full disclosure, “thanks” may be treated as consent. Own-request/instant exemptions still rely on the agent interpreting the owner's intent, bounded to one change per typed message.
- Same-user automation that hides/reparents/renames its ancestry, edits local records, or drives the owner's app remains outside process-provenance protection. This is an accepted same-user limit, not proof that a pty is human.
- No post-suite source edits were made. Known unrelated `test_routing_received` and `test_routing_s5_fix3` were not selected.
