# Routing slice 7 — fix round 3

Rebased `feat/routing-s7-pull` with `git rebase --onto origin/main 1d26288` onto `db79cc4` (#276, including #274/#275). The one conflict retained main's public account candidates and added the complete-inventory gate. Main's helper rebuild and two-second freshness policy are unchanged. Worked directly, without delegation, stash, or push.

Implemented P2 and P3-1 through P3-6:

- An environment-identified caller may select its own root or a strict descendant; unrelated roots, empty path segments, traversal segments and NUL are refused. T3, Claude and Codex children get separate ledgers and cannot acknowledge their parent (case 6).
- Lane coverage comes from the active complete scrub table, including empty/default-only inventories. Failed inventory still withholds the answer before acknowledgement.
- Account-key syntax is checked before owner masking, and an exemption must cover the whole syntax match.
- The response, delivery reservation and final dispatch scrub share one folded view and its current owner words.
- Owner credentials are removed as tokens, preserving surrounding words; plain, percent-encoded and HTML-encoded notes page and acknowledge completely. Pending credentials remain withheld. Note revisions include safe rendered text, invalidating old unsanitized reservations, including partly acknowledged pages, before retransmission.
- Unknown-ack classification examines at most 32 directory entries and eight candidate ledgers, each at most 1 MiB, under nonblocking shared locks. It checks a 50 ms budget between candidates. Busy or out-of-budget candidates yield a non-consuming unknown result.
- `conftest.py` strips all four requested Claude/Codex identity variables; a separate poisoned-environment subprocess verified this.

The old empty/default-only always-hidden tests were replaced by complete-inventory visibility assertions; discovery-failure and actual-login privacy assertions remain strict. Opus's probes were copied into `tests/test_routing_s7_fix3.py`; no tests import from `/tmp`.

## Post-rebase re-checks

| Item | Check and test evidence |
| --- | --- |
| Active table and completeness | Inspected `_table`/`_PROJECTION.current`; pull tables require complete construction and cannot use a previous-table fallback. `test_C_innocuous_lane_visibility`, `test_complete_table_failure_with_existing_inventory_consumes_nothing`, `test_new_fold_account_alias_is_in_the_pull_table`, and the helper expiry/sleep tests pass. |
| Final scrub and owner provenance | Opus F2, both path and key-syntax variants, passes. `test_stale_owner_exemption_cannot_authorize_pending_page` and `test_response_and_delivery_share_one_folded_view` pass. |
| Strict hook push and paging | Ran both push suites and both slice-6 delivery/scrub suites. All slice-7 owner-page and secret-split regressions pass. Final daemon paging had one no-progress failure; its isolated rerun passed unchanged. See limitations below. |
| Canonical helper protocol | Both `test_helper_ipc_reserves_and_hook_acks_mcp_identity` variants pass, proving canonical shared/native identities survive IPC and hook/MCP use one ledger. |
| Clean-environment full oracle | Ran the supplied file selection three times with the supplied environment removals, temporary HOME/cache, Keychain disabled and a 600-second command timeout. Results below. |

## Verification

| Full run | Passed | Failed |
| --- | ---: | --- |
| 1 | 2,210 | Five main CLI failures; configured-alias assertion |
| 2 | 2,216 | Five main CLI failures |
| 3, final | 2,217 | Five main CLI failures; daemon paging no progress |

Final output:

```text
6 failed, 2217 passed, 7 skipped, 100 xfailed, 14 subtests passed in 292.21s (0:04:52)
```

The five CLI failures reproduce on untouched `origin/main` (`db79cc4`): owner-request rule confirmation, removed-rule restore, dash-valued app argv, app-view provenance and recent app changes. The configured-alias assertion from run 1 also fails on untouched main; its branch-only rerun passed. Its real assertion was `assert word.encode() not in wire` with synthetic `SecretNewYYY` present. This remains an observed inherited freshness-window risk; no assertion or helper policy was weakened.

The final additional failure was `test_paged_delivery_continues_on_the_daemon_path`: `assert end > offset` evaluated as `9222 > 9222`. Its unchanged isolated rerun passed (1 passed in 2.85s). The cause is unproven; the full gate remains red, and the three-run budget is exhausted.

Additional focused checks: 153 agent-view/new-owner checks passed; both legacy-note upgrade regressions passed. `git diff --check` passed.

Replay exited 0: 200 prompts per mode, zero timeouts, zero added timeouts, `stop=false`; routing-on p95 31.0 ms. All 400 searches errored; routing-on recorded 185 unsent, one budget skip, 13 none and one full response. This verifies the timeout gate, not live delivery.

## Open question

P3-7 remains open: real Claude Code `/clear` and `--resume` environment-versus-hook-payload identity retention was not exercised. Hermetic identity tests cannot establish the real client's lifecycle. No live lifecycle claim is made.

Evidence: `/tmp/khipu-routing-s7/fix3/`: `full1.log`, `full2.log`, `full3.log`, `main-five.log`, `main-alias.log`, `alias-alone.log`, `paging-alone.log`, `focused2.log`, `legacy-note-probe2.log`, `legacy-note-fix.log`, `conftest-env.log`, `replay.log`.
