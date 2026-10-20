# Routing slice 6 — fix round 3 (partial; verification red)

Rebased cleanly onto `4f19b39` (origin/main at the start, including #273). Worked directly, without stash or push. This is not a merge-ready result: the three-full-run budget is exhausted with two additional availability failures still reproducible alone.

The helper now refreshes its complete table on idle ticks and after requests, with a 250 ms full-refresh bound, and retries failed startup preparation. Prompt refresh retains its 5 ms bound; fallback has a separate 5 ms bound. Fallback checks source stamps, login-shaped home entries, environment, covered account paths and known symlink paths, and withholds routing when inventory changes cannot be confirmed. Current owner exemptions remain separate from inherited aliases.

## Proving tests

`test_routing_s6_scrub.py::test_forked_prompt_never_shows_resolved_login_alias` uses real helper/socket/prompt forks. In the final full run:

- Both configured symlink and newly discovered `.codex-<name>` logins pass with a slowed refresh and with `/Volumes` stalled after readiness: four cases. Initial prompts withhold routing and leave markers untouched; helper refresh restores useful scrubbed output.
- Stalled startup followed by recovery passes, with prompt signatures deliberately slowed so recovery depends on the helper's retried preparation.
- Eight of nine cases pass. The unchanged-login `slow-signature-None` case fails availability, returning `error-InventoryUnavailable`; it also fails alone. No alias was exposed.

Three direct-renderer test setups now prepare inventory as the real helper does. Their content, paging, acknowledgement and import assertions remain intact. Final separate privacy suite: **148 passed**.

## Final verification and blocker

Exactly three full requested oracle runs; no edits during a run. Final result:

```text
7 failed, 2024 passed, 7 skipped, 113 xfailed, 14 subtests passed
```

Five CLI failures reproduce on an untouched archive of the actual rebase base, `4f19b39`. The two additional failures also fail separately:

```text
test_routing_daemon_poll.py::test_a_limits_end_with_no_file_changing_reaches_the_next_prompt_as_a_change
    khipu.agent_view.InventoryUnavailable

test_routing_s6_scrub.py::test_forked_prompt_never_shows_resolved_login_alias[slow-signature-None]
    AssertionError: {'skip': 'error-InventoryUnavailable', ...}
```

These remain unresolved; they are not classified as timing flakes. Further source changes require a renewed verification budget.

Final replay: 200 prompts per mode, zero timeouts, zero added timeouts, `stop=false`; routing-on p95 22.6 ms. All 400 searches errored and 200 routing responses were unsent, so this does not verify live delivery. `git diff --check` passes.

Evidence: `/tmp/khipu-routing-s6/fix3/`, particularly `PYTEST-3.log`, `final-clock-alone.log`, `final-slow-alone.log`, `final-privacy.log`, `baseline-five.log`, and `replay-3.log`.
