# Routing slice 9a — fix round 1

Rebased `feat/routing-s9a-providers` onto slice 4 fix-round commit `c4c4d6d`,
preserving its entry cache, warm-read operation limits, and observation deduplication.
Case 1 then passed all four variants. Work was direct, with no delegation or push.

**Upstream changed during verification:** `feat/routing-s4-entries` advanced to
`a2ecd7e` at 17:09 EDT. That later commit is not integrated. The three-full-run
budget is exhausted; the verified base of this repair remains `c4c4d6d`.

## Findings and tests

The tests below are in `packages/cli/tests/test_routing_s9a_fix1.py` unless named otherwise.

| Lead | Result | Regression evidence |
|---|---|---|
| P1.1: transport treated as payer | Proven and fixed. Known model provider outranks transport; explicit contradictions are refused. Claude transport can bill GLM/DeepSeek. | `test_transport_model_and_explicit_payer`, `test_claude_transport_login_can_bind_glm`; real declaration shapes in `test_real_church_lane_shapes_and_owner_secondary_override` |
| P1.2: Cursor Auto/Grok billing | Rejected under Matt's rule 1. Every Cursor lane retains Grok billing; Auto's family stays unknown. | Cursor variants of `test_transport_model_and_explicit_payer`; existing `tests/test_routing_cursor.py` witness/attribution tests |
| Missing definition owner writer | Proven and fixed. CLI `route definition set … --owner-direction`, MCP `route.definition.set`, and Settings **Add provider account** write provider/account definitions. Each definition write has append-only Undo. | `test_owner_definition_writer_creates_matching_real_lane_shapes`, `test_app_definition_argv_reaches_the_same_owner_writer`, `test_definition_owner_undo_restores_prior_fact_and_removes_new_fact`; Settings component tests and Rust fixed-argv test |
| P2.1: DeepSeek Flash-only weakened to denylist | Proven and fixed in fixtures, Settings defaults, and scope case 8: `models_allowed: [deepseek-flash]`. | `test_deepseek_flash_only_fact_reaches_dispatch[deepseek-reasoner]` and `[deepseek-chat]` failed before the change |
| P2.2: spend/concurrency missing from 9a | Scope boundary, not a 9a defect. Spend and concurrency remain with **17a/17b**. **Rule 13 stays standing**; no owner words or live facts were migrated. | `test_rule13_budget_and_concurrency_remain_outside_provider_schema` |
| P2.3: cached declarations | “Never re-resolves” is disproven for a readable unchanged config: the reader rereads it. The narrower retained-config failure when its file becomes unreadable was proven against rebased pre-fix code and fixed by preserving safe unresolved references. Bare `claude` remains ambiguous across accounts; an owner's unique label override still works. | `test_cached_declaration_resolves_after_owner_creates_account` passed on pre-fix code; `test_retained_config_declaration_resolves_after_owner_creates_account` failed there; `test_bare_provider_word_stays_ambiguous_after_cache` covers both main labels |
| P2.4: family optional | Proven and fixed: provider facts require `family`. Invalid-field fixtures now include a valid family so their schema assertions remain meaningful. | `test_provider_family_is_required`; `tests/test_routing_s9a_providers.py::test_closed_provider_schema` |
| P3: dead request parameter / false absence claim | Proven and fixed by removing the unused parameter and making `on_request` a neutral policy reminder. The hook does not infer whether Matt requested a dispatch. | `test_requested_use_reminder_does_not_claim_owner_request_is_absent` |
| P3: missing model falsely violates allowlist | Proven and fixed; an undeclared model is unknown, without a model-violation warning. | `test_missing_model_is_unknown_not_a_policy_violation` |
| P3: name normalization | Proven and fixed: catalog ids and account harnesses use the same normalization. | `test_name_step_normalizes_provider_and_account` |

The scope's slice 9a row now points to the definition-writer documentation in
`docs/routing.md`. The older Cursor assertion rejecting Claude transport with a
Grok payer became an explicit positive assertion under the approved transport/payer
contract; contradictory declarations remain negative assertions.

A malformed definition kind in the new writer also reproduced an uncaught
`TypeError`. The enum is now checked before its dependent value schema, regardless
of JSON key order: `test_definition_action_rejects_invalid_kind_before_value`.

## Case 1 start probe

`tests/test_routing_acceptance.py::test_1_hold_priority_contradiction_and_end_route_already_arrive[start]`
passed after rebase and in all three full runs at its default one-second budget.
An ordered whole-file probe raised `routing_start_budget_ms` to 8000: forked text
returned in 41.9 ms, and `start_push(1.0)` called in-process at that same test point
returned all required words in 40.8 ms, without an exception. Load averages were
39.07/39.58/39.37. The whole file passed: 135 passed, 113 expected failures.

The historical `None` did not reproduce. Its cause remains unproven; these probes
establish neither a persistent 9a logic failure nor a retrospective timing cause.
No production start budget was changed. Probe source and results are in
`/tmp/khipu-routing-s9a/fix1/start-probe.py`, `start-probe.log`, and `start-probe.jsonl`.

## Verification

The requested hermetic Python oracle ran exactly three times, with no edits during a run:

| Run | Passed | Failed | Skipped | Expected failures | Duration |
|---|---:|---:|---:|---:|---:|
| 1 | 1972 | 9 | 7 | 113 | 268.54 s |
| 2 | 1979 | 5 | 7 | 113 | 266.68 s |
| 3, final | 1982 | 5 | 7 | 113 | 253.15 s |

Every run also passed four subtests. Final exit status is 1, solely for these
inherited `tests/test_routing.py` failures:

- `tests/test_routing.py::test_rule_at_owner_request_is_live_without_terminal_confirmation_but_owner_actions_are_not`
- `tests/test_routing.py::test_a_removed_rule_comes_back_whole_with_restore`
- `tests/test_routing.py::test_the_apps_argv_keeps_a_dash_value_as_words_never_a_flag`
- `tests/test_routing.py::test_the_apps_view_marks_watched_rules_old_switches_and_who_changed_what`
- `tests/test_routing.py::test_recent_changes_name_the_owner_in_settings_for_every_change_made_in_the_app`

Run 1 additionally failed these four, all passing in runs 2 and 3:

- `tests/test_routing_cursor.py::test_declaration_conflicts_overrides_and_tier_keys`
- `tests/test_routing_sources.py::test_a_limit_from_an_unmatched_lane_is_reported_and_never_guessed`
- `tests/test_routing_sources.py::test_a_pasted_secret_in_a_runner_line_is_redacted`
- `tests/test_routing_sources.py::test_a_launch_while_paused_is_named`

The three source failures exposed an implementation regression: applying declaration
ambiguity to owner-map labels. Owner maps retain unique-label resolution; only a
provider word in a declaration requires a unique provider account.

Other final checks:

- Settings component suite: **88 passed**; TypeScript/Vite build passed.
- Desktop `cargo test`: **171 passed**. Attempt 1 stopped before tests because
  `../khipu-resources` was absent; the documented `bundle_cli.sh` staged it and
  attempt 2 passed. No Rust edit followed it.
- Focused Python repairs: 81 passed, then 27 passed for retained-cache, ambiguity,
  Cursor, and the source regressions. Final full runs cover subsequent changes.
- Ruff passed for the changed routing/owner modules and both 9a test files checked;
  `git diff --check` passed.
- Final `python3.11 -m khipu.routing_replay`: exit 0, 200 prompts per arm,
  zero timeouts, `added_per_100=0.0`, `stop=false`, `live_replica=false`.
  Routing-on median/p95: 26.4/42.7 ms; off: 0.9/1.9 ms. Both arms returned
  `reason=error` for all 200 recall searches. This verifies the timeout stop
  condition, not successful recall. Routing delivery counts were full=1,
  none=13, unsent=186.

Rendered UI remains **unverified**: the in-app browser was unavailable and the
computer-use browser inventory was empty. Component behavior, fixed app argv,
and the CLI app-origin boundary were verified; no native/live UI acceptance is claimed.
No live runner configuration, provider credentials, or live routing state was changed.

All logs, the pre-fix cache snapshot, and the complete per-run failing-test-id
inventory are under `/tmp/khipu-routing-s9a/fix1/`; see `failure-inventory.md`.
No fourth full run was performed. No production edit followed the final full oracle
or replay.
