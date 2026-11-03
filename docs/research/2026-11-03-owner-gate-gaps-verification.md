# Owner-gate repairs: verification record

Status: both blockers are closed. The final hermetic broad gate passed: **1665 passed, 7 skipped; 34 subtests passed**, exit 0. The initial handoff below records `5a5b3e1b`; the completion pass at the end supersedes its two blockers.

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
