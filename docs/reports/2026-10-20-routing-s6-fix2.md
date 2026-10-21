# Routing slice 6 — fix round 2

Rebased onto slice 4's current `626d4ad` tip (the same slice 4 patch as `73e9f6d`, rebased onto newer main). No conflicts, delegation, or push. The five settled review leads were not reopened.

The new P2 is fixed: the helper builds a complete scrub inventory before readiness and the first fork, with a 250 ms startup deadline. Children inherit its last good table; a 5 ms prompt refresh timeout retains those aliases and rebuilds current owner exemptions. A cold timeout raises `InventoryUnavailable`, so the existing child error boundary sends no routing and advances no delivery marker. The one-shot child also prepares once before announcing readiness. Children inherit a failed preparation too, so a stalled helper does not repeat the startup wait per prompt. Round 1's prompt deadline and alarm restoration remain intact.

## Proving tests

`packages/cli/tests/test_routing_s6_scrub.py::test_forked_prompt_never_shows_resolved_login_alias` runs real helper/prompt forks from an empty table, using a configured symlink to `SecretAliasZZZ`. All four cases pass:

- `slow-signature`: a 15 ms signature forces the 5 ms refresh to expire; the first and next prompt still deliver useful routing without the alias.
- `stalled-volume`: `/Volumes` stalls after helper readiness; both prompt children retain the resolved alias scrub and return within the bound.
- `cold-stalled-volume`: the volume stalls before inventory preparation; both prompts withhold routing and leave delivery markers untouched.
- `one-shot`: the daemon is absent; the directly forked child delivers scrubbed routing despite the slow signature.

The original three helper cases leaked the alias before this fix (`alias-red.log`). The one-shot case separately reproduced `error-InventoryUnavailable` before its startup preparation was added (`one-shot-red.log`). The existing stalled-volume availability test now prepares the complete inventory first, matching helper startup; the new cold-stall case explicitly verifies withholding instead of accepting an incomplete scrub.

## Verification

- **179 passed** in the final scrub/deadline selector: `test_agent_view.py`, `test_routing_s6_scrub.py`, and `test_routing_s6_delivery.py`.
- Two full requested oracle runs, with no edits during either run. Final: **1,970 passed, 5 failed, 7 skipped, 113 xfailed, 4 subtests passed**. All five failures reproduced on an untouched archive of `626d4ad` (`baseline-five.log`). They are the five CLI failures already listed in the round 1 report.
- Run 1's additional cold default-budget test failed with `routing=skipped:error-InventoryUnavailable`, then **passed alone** before further edits (`cold-alone.log`). The bounded one-shot preparation addresses that availability gap; it passes in the final full run.
- Final replay: **200 prompts per mode, zero timeouts, zero added timeouts, `stop=false`**. All 400 real-search attempts returned errors, with 200 routing responses unsent; this establishes the timeout condition, not live recall delivery.
- `git diff --check` passes. Production changes for this round are confined to `agent_view.py` and `recall_prompt.py`.

Logs: `/tmp/khipu-routing-s6/fix2/`, especially `PYTEST-2.log`, `replay-2.log`, `final-scrub.log`, and `baseline-five.log`.
