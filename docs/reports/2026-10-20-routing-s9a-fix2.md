# Routing slice 9a — fix round 2

The named code repairs are verified. **The strict push-after-append latency target
(at or below fresh base) remains unmet.** Work was direct on the existing
`feat/routing-s9a-providers` branch at `ed955e8`, based on `4f19b39` (`origin/main`).
No delegation, stash, push, live routing change, or app edit.

## Repairs and proving tests

- **P2-1:** persisted observations drop `declaration.login_account`; their account
  remains a stable id. `test_login_home_never_persisted_in_observation_or_conversation`
  proves a LIMITED line attributes the secondary login correctly without its path
  in observations or the conversation view. The established local
  `account_migration` binding records are excluded, as in Opus's probe.
- **P2-2:** the fold calls `resolve_lane` only for evidence with a runner.
  `test_refold_resolves_only_runner_evidence` folds 20,000 events and proves zero
  calls without runner evidence, and exactly one with a runner evidence item.
- **Doctor:** both configured and owner-written lane maps reach provider checks.
  `test_doctor_uses_owner_lane_map[config|definition]` proves the mapped provider's
  model restriction is checked. The existing
  `test_provider_checks_flag_real_runner_model_settings` now requires no broken
  check for the on-request lane, while retaining the disallowed-model assertion.
  Dispatch still receives the neutral on-request reminder.
- **Labels:** `test_owner_provider_label_refuses_url_or_token` covers a URL and
  three token shapes through the owner MCP writer, with `bad_args` and no routing
  log append. Validation reuses the existing source redaction/token detectors.
- **Docs:** the provider/identity block follows the complete state-source list.
  Out-of-order Undo is unchanged.

New regressions live in `packages/cli/tests/test_routing_s9a_fix2.py`.
The focused new/existing provider suite passed **30 tests**. The exact requested
Python oracle ran **once**, exiting 1:

```text
5 failed, 2004 passed, 7 skipped, 113 xfailed, 4 subtests passed in 238.87s
```

The same five failures were independently reproduced on untouched `4f19b39`
(`base-five.log`, 5 failed in 1.55 s):

- `test_rule_at_owner_request_is_live_without_terminal_confirmation_but_owner_actions_are_not`
- `test_a_removed_rule_comes_back_whole_with_restore`
- `test_the_apps_argv_keeps_a_dash_value_as_words_never_a_flag`
- `test_the_apps_view_marks_watched_rules_old_switches_and_who_changed_what`
- `test_recent_changes_name_the_owner_in_settings_for_every_change_made_in_the_app`

No new or timing-only failure occurred. Ruff and `git diff --check` passed.
No app files changed; the conditional Cargo/Vitest gate was not invoked.

## Back-to-back cost measurements

Each row ran base `4f19b39` immediately followed by fix, using the same workload
and hermetic environment. Values are median milliseconds for push after one raw
appended line and a one-second advance, with 20,000 seed events. Pairs 1 and 2
use five samples per arm; pair 3 uses fifteen. The provider/runner variant adds
four definition events and a synthetic runner. Every changed push returned full.

| Workload | Pair | Base | Fix |
|---|---:|---:|---:|
| Slice 4 | 1 | 537.6 | 570.5 |
| Slice 4 | 2 | 600.3 | 613.8 |
| Slice 4 | 3 | 641.3 | 708.6 |
| Providers + runner | 1 | 570.9 | 561.4 |
| Providers + runner | 2 | 610.5 | 627.6 |
| Providers + runner | 3 | 675.5 | 680.3 |

These measurements do **not** establish at-or-below-base latency. Only one pair
met that target. Host load was 31.71/32.61/34.41 near the last pair; this does not
prove the cause of the remaining differences. No timing assertion or production
budget was relaxed. No additional timing attempt was used to select a better result.

The reusable variant is committed as
`packages/cli/tests/routing_s9a_cost_workload.py`; it imports nothing from `/tmp`.
Raw logs, samples, profile, and comparison scripts are under
`/tmp/khipu-routing-s9a/fix2/` (`cost.json`, `final-cost.json`, `full-1.log`,
`base-five.log`, `focused-final.log`).
