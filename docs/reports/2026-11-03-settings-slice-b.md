# Settings reorganization — slice B control inventory

Source baseline: `603a16af`. This report is a presentation-only mapping: no
CLI command, configuration key, or routing behavior changed.

| Before group / control | After page | Handler or key | Count |
|---|---|---|---:|
| Needs you decision cards and problem Show | Needs you | existing `khipu_route_*`, `khipu_config_request`, `onShow` | 1 factory |
| Accounts, detail sheets, finder, plan refresh | Your accounts | existing account/detail callbacks and `plansState.refresh` | 1 section |
| Pace, caps and advice | Your accounts | read-only pace model | 1 section |
| Priority order and pace-order switch | Who gets what work | `khipu_route_priority`; `routing_pace_order` | 1 section + 1 switch |
| Note and standing rules | Who gets what work | `khipu_route_note_*`; `khipu_route_rule` | 2 sections |
| Proposals and holds | Who gets what work | `khipu_route_hold_*` | 1 section |
| Priority confirmation warning | Rarely needed | `routing_priority_unconfirmed_warn_after` | 1 LimitRow |
| Hold length, word cap, reminder | Rarely needed | `routing_conversation_hold`, `routing_conversation_hold_max`, `routing_hold_chars`, `routing_owner_hold_reminder` | 3 LimitRows |
| Routing copy, proposal words, cleanup retry | Your Macs | `routing_replication_live`, `routing_proposal_text_between_macs`, `khipu_gateway_copy_cleanup` | 2 switches + 1 retry |
| Other Macs and cloud delivery evidence | Your Macs | read-only doctor/route evidence | 1 section |
| Conversation delivery and three telling moments | Conversations | `routing_push`, `routing_push_start`, `routing_push_prompt`, `routing_push_delegation` | 4 switches |
| Dispatch scripts and sizes/times | Rarely needed | `routing_dispatch_scripts`; existing `SIZES` keys | 1 row + 7 LimitRows |
| Recent changes | What changed | existing restore handler | 1 section |
| Checks, contradictions, copies, upkeep | Something's wrong | read-only checks model | 1 section |
| Lanes and job-runner folders | Something's wrong | existing lane map/folder handlers | 1 section |
| Provider definitions and add account | Rarely needed | existing `khipu_route_definition` | 1 section |
| Confirmed model table | Rarely needed | read-only checks model | 1 section |
| Reset checks, old routing file, login-usage switches | Rarely needed | existing config handlers | 3 sections |

## Count evidence

The companion [lexical control-site inventory](2026-11-03-settings-slice-b-control-inventory.json)
defines a site as one static JSX `button`, `input`, `select`, `textarea`,
`SwitchRow`, or `LimitRow` template (repeated rows and conditional branches
remain one listed source site; unchanged shared factories remain represented).
It records each site's component identity, source location and excerpt before
`603a16af` and after this slice, alongside the section-to-page mapping above.
This is a source inventory, not a semantic proof of runtime control parity.

| Signature | Baseline | After |
|---|---:|---:|
| Legacy top-level `GROUPS` identities | 11 | 11 retained |
| Permanent page destinations | 0 | 8 |
| `SwitchRow` call sites | 9 | 9 |
| `LimitRow` call sites | 11 | 11 |
| `w.run(` call sites | 30 | 30 |
| `Open all groups` / `Close all groups` source residuals | 2 controls | 0 |

The final lexical inventory totals are **150 before / 156 after**. These
are static source sites, not a verified count of runtime controls. The JSON
includes source excerpts, line locations, component identities and page mapping;
shared unchanged imported components are not expanded. All 30 `w.run` sites
and the 13 named writer-command signatures remain represented. Open/Close all
and legacy fold controls are intentionally replaced by page navigation.

## Partial handoff — acceptance is not complete

Final `npx tsc --noEmit`: passed on the third compiler invocation. The second
invocation found two unused destructured props after the section moves; only
those unused bindings were removed before the passing check.

Final `npx vitest run`: **21 failed / 1316 passed (1337 tests)**, **4 failed /
70 passed files (74)**, 62.11 seconds. Full-suite invocations: two. The two
page tests also had one worker focused run, exhausting their three-run budget;
no fourth execution or additional full suite was run.

- RoutingDelivery: 3 failed.
- RoutingOverview: 5 failed, including control inventory, runner focus and the
  old open/closed page-height assertion. The height test migration is incomplete.
- RoutingPriority: 1 failed.
- RoutingSettings: 12 failed, including hidden/moved controls, creation forms,
  old spacing assertions and loading states.
- The long-hold explanatory-text failure was reproduced on an untouched
  archive of starting commit `603a16af`: 1 failed / 121 skipped (122), 1.39s.
  The other 20 failures remain unresolved; they are not classified as baseline.

Evidence: `/tmp/khipu-slice-b-vitest-final.log`,
`/tmp/khipu-slice-b-tsc-3.log`, and
`/tmp/khipu-slice-b-baseline-long-hold.log`.

Every page was inspected with the isolated fake backend at CSS 1100×720 and
760×720, the latter with long text. Final screenshots and page measurements
are listed in `/tmp/khipu-slice-b-shots/README.md` and `metrics.json`.
The copy switch is visible on Your Macs. Measured page changes reset the main
scroll container to zero. A lane badge overlap found at narrow width was fixed
and inspected again. This does not establish full visual acceptance: Work is
still about 2514px in the normal wide fixture and 5380px with worst-case narrow
text; Needs you is 2754px in that worst case. The short-page requirement remains
open. Full runtime control parity, all deep links, and final page-height budgets
are not verified. No backend, live configuration, hub write, installed-app,
release or PR operations were performed.
