# Routing slice 5: fix round 3

Rebased `feat/routing-s5-precedence` onto `origin/main` at `fb98704` (#274 and #275). Main's provider summary rows are retained.

Rule edits and priority requests now require an explicit prepared `base` in their MCP schemas and writers. Receipts verify that the caller saw that revision; they never select a newer base. CLI requests take `--base`, and empty-priority reads expose their owner revision.

Replacement notices name the rule number or priority and reach only the author session. Successful MCP reads, hook delivery and printed CLI summaries acknowledge the notices actually shown. Optional MCP status workers and subagent snapshots carry no notices. Missing or departed author sessions cannot force full pushes into bystanders; notices expire after seven days.

Sessionless reads create no receipt files and take no receipt lock. Stop tracking ends the obsolete priority overlay before removing the account from the owner's priority. Older saved proposal views refold once, without rewriting the event log. The four requested agent identity variables are stripped by `tests/conftest.py`.

## Verification

Final full oracle, run 3 of 3: **2,090 passed, 5 failed, 7 skipped, 107 xfailed; 4 subtests passed** (235.46 seconds). All slice-5 regression cases passed. No app files changed; Cargo was not required.

Proving tests in `packages/cli/tests/test_routing_s5_fix3.py` cover omitted and forged bases, interleaved reads, author-only delivery and acknowledgement, session-start payload identity, sessionless CLI subprocesses, receipt-lock avoidance, bystander prompts, account stop and saved-view migration. Existing lifecycle tests now pass their explicitly read bases; the Stop tracking expectation follows the requested overlay-ending behavior.

The five failures are the same CLI argument-parsing cases identified as baselines in the supplied review, reproduced here:

- `test_rule_at_owner_request_is_live_without_terminal_confirmation_but_owner_actions_are_not`
- `test_a_removed_rule_comes_back_whole_with_restore`
- `test_the_apps_argv_keeps_a_dash_value_as_words_never_a_flag`
- `test_the_apps_view_marks_watched_rules_old_switches_and_who_changed_what`
- `test_recent_changes_name_the_owner_in_settings_for_every_change_made_in_the_app`

Real failure output includes `SystemExit: 2` and `khipu: error: unrecognized arguments: -- Never a -fast tier.` Their assertions remain intact.

`python3.11 -m khipu.routing_replay` exited **0**: 200 prompts each way, zero timeouts, `added_per_100: 0.0`, `stop: false`. Both sides returned 200 recall errors. This establishes the timeout gate, not successful recall delivery.

## Bystander cost

One observed 20,000-event sample (11 measured prompts per case):

| Notice state | Median per prompt | Text | Full pushes |
|---|---:|---:|---:|
| No notice | 2.941 ms | 0 characters | 0 |
| Notice for departed author | 3.800 ms | 0 characters | 0 |

The final `python3.11 -m tests.routing_s5_notice_cost` workload also exited 0. It verifies that the author's replacement notice exists, then asserts all 11 bystander prompts return `none` with zero text in both cases. Timings are observations, not timing assertions.

Logs: `/tmp/khipu-routing-s5/fix3/full-{1,2,3}.log`, `replay.json`, and `bystander-cost-final.json`.

`owner_direction.py` is byte-identical to rebased main (Git blob `4735708cdd5c50d87b862f580799e522e7e0dde6`); this fix makes no edits to it. No push.
