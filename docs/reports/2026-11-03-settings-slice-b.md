# Settings reorganization — slice B verified handoff

Branch: `feat/settings-eight-pages`. Original presentation baseline: `603a16af`;
this completion starts at `779bf02e`. No routing command, write argument,
configuration key, routing behavior, or owner gate changed.

## Pages and measured heights

CSS pixels of `.route-page-content`, with the app's 216 px rail present.
Each cell is **normal / worst**. Worst includes 20 priority entries, long owner
words, an unbroken profile path, and long runner/lane names. These are actual
headless Chrome measurements, not test-budget constants.

| Page | Contents | 1100×720 | 760×720 |
|---|---|---:|---:|
| Needs you | Decisions, setting/account requests, fix/later lists, proposal details | 2128 / 4176 | 2405 / 5054 |
| Your accounts | Accounts, finder, details/pauses/caps, pace, next reset readouts | 1543 / 1543 | 1791 / 1791 |
| Who gets what work | Order, note, compact rules, holds; sticky section jumps | 1555 / 1829 | 1978 / 2287 |
| Your Macs | Routing-copy and proposal-word switches, cleanup, copy evidence | 476 / 476 | 521 / 521 |
| Conversations | Delivery, recipients, telling switches and moments | 1128 / 1128 | 1181 / 1181 |
| What changed | History, filters, bring back | 351 / 351 | 456 / 456 |
| Something's wrong | Conflicts, upkeep, copies, lanes, runner folders/settings | 883 / 1258 | 974 / 1337 |
| Rarely needed | Reset controls, limits/timing/scripts, models, providers, login reads, old file | 457 / 457 | 457 / 457 |

Rarely needed with Sizes open measures 1685 px wide / 2006 px narrow. Its
closed height is deliberately reported separately from that expanded state.
Normal Work meets the requested approximately 1600 px wide target, down from
2514 px. Narrow normal and both worst Work cases exceed 1600 px because the
same-size priority words, help, and action buttons wrap. The sticky Order,
Note, Rules, Temporary instructions row stays visible at both widths. Rule
previews occupy at most two lines (38 px at 18.85 px line height), use whole
words, retain 13 px text, and have a visible More button; More reveals the
complete original text, source and check details in place. No Work folds were
added. Needs you remains long in the busy/worst fixtures: the owner decision
preserves the complete before/after priority orders and proposal readouts.
That is a remaining short-page limitation; this report does not call it short.

## Runtime control parity and links

`RoutingRuntimeInventory.test.tsx` renders the actual `App`, with fictional
route/config/account/discovery/health records and the actual CSS. There is a
parameterized test for **each of the scope document's 30 inventory rows**,
plus an assertion that the probe count equals the parsed table count. Each
opens the specified new page and finds the visible control/readout; technical
controls open their Rarely needed fold. The deliberately removed Open/Close
all row instead proves both controls absent and all eight page buttons present.
The original inventory names/value/writer assertions remain in the older tests.

All 11 legacy group links select the owning page and focus the visible group
target, including opening a closed Rarely needed fold. The runtime suite also
checks every page resets scroll and that the Rules jump focuses and scrolls
the correct section. Runtime inventory suite: **46/46 passed**.

An opt-in Vitest-only transform (`KHIPU_WRITER_PROBE`) records actual executions
of each existing `w.run` call site before forwarding its identical arguments.
The full green run reached **all 30/30 writer sites**, with **105 calls**.
The [writer evidence](2026-11-03-settings-slice-b-writers.json) gives each site,
source line, command and observed count. No production Vite instrumentation
or real backend writes are involved. The Mac routing-copy switch is visibly
on Your Macs without opening a fold.

No surviving functional control is placed outside its scope-table home.
Proposal details were moved to Needs you; next reset/reason readouts were moved
to Your accounts. Provider definitions are Rarely needed; lane mapping and
runner folders are Something's wrong, as the table's split permits.

## Lexical site accounting: 150 → 156 → 157

The [source inventory](2026-11-03-settings-slice-b-control-inventory.json)
retains the 150-site baseline, the 156-site `779bf02e` snapshot and the final
157-site snapshot. A site is one static JSX button/input/select/textarea/
SwitchRow/LimitRow template; repetition and conditional runtime instances do
not inflate it. Shared imported factories are not expanded. This accounting
supports, rather than substitutes for, the runtime proof above.

The original +6 is exactly these **10 additions minus 4 removals**:

| Added site in 779bf02e | Purpose |
|---|---|
| RoutingSettings page-navigation button template | Eight destinations |
| RareFold button template | Technical-section disclosure |
| PrioritySection pager | More ordered accounts |
| RulesSection pager | More rules |
| RulesSection Add rule opener | Open existing form |
| RulesSection Add rule Close | Close form |
| HoldsSection Add hold opener | Open existing form |
| HoldsSection Add hold Close | Close form |
| NeedsYou decision pager | More decisions |
| NeedsYou PagedRows pager | More problems |

Removed: the old Group fold-button template; old SubFold fold-button template;
Open all groups button; Close all groups button.

This completion adds the RuleRow More/Less template and the Work jump-button
template, and removes one duplicate empty-runner Add a folder button. The folder
section's unique Add a folder button opens the same form and reaches the same
writer, proven by the original uniqueness/form test. Therefore **156 + 2 − 1 =
157**. SwitchRow sites remain 9, LimitRow sites 11, writer sites 30.

## Classification of every previous failure

All 20 unresolved failures, plus the baseline long-hold failure, are green.
No test/check was deleted or newly skipped. Names below identify each original
failure; moved controls are checked on their new page with the same values and
writer assertions.

| Original test (file prefix Routing) | Code or test wrong | Resolution / retained strength |
|---|---|---|
| Delivery: friendly names, Macs, last prompts, who has each change | Test | Render both split delivery components; retain all names, times, privacy and no-action checks. |
| Delivery: has-it unknown until doctor exposes it | Test | Check the split Mac component too; retain unknown-evidence assertions. |
| Delivery: no-age copy remains unknown despite green | Test | Same split; green-without-age still must be unknown. |
| Overview: every problem kind on group line and Needs you, once | Code | Legacy group target wrapped whole body and counted child tags; target now wraps only its header. All original counts remain. |
| Overview: runner absent from folders has own row and Show focuses it | Test | Expand visible Needs pagination before finding the same problem and asserting focus. |
| Overview: every old control and moved Plans controls exists | Test | Open new add forms and owning Rarely sections; retain the original inventory, supplemented by full-App row probes. |
| Overview: passing gap keeps Home's figures without needs | Code | Same group-header boundary fix; original figure/tag checks remain. |
| Overview: old open/closed-fold page heights | Test | Replace obsolete folds with explicit budgets for every one of eight pages at 1100 px, including Work ≤1600. No blanket budget increase; the original five-item Needs you assertion is retained after opening its pager. |
| Priority: account pace, ordering switch and warning | Test | Open Rarely Priority and hold limits for the same warning value; pace and ordering checks remain. |
| Settings: adds rule, never empty words | Test | Reopen the closed creation form and reacquire its current field; retain both exact writes and distinct operation ids. |
| Settings: hold word bounds, refusal, can raise limit | Test | Longest line moved to Rarely Sizes; retain refusal, exact limit write and enabled control checks. |
| Settings: provider plan-reading and owner's model facts | Test | Scope to Providers list in Rarely; retain all five names and existing values. |
| Settings: No job runners yet and Add a folder | Code | Two duplicate CTAs became exposed after unfolding; remove only the duplicate and retain unique lookup plus form-opening check. |
| Settings: every contradiction/upkeep flag in Needs you | Test | Expand visible pager; retain every original flag and no-action assertion. |
| Settings: rule-sourced contradiction names public id | Test | Expand visible pager; retain exact Rule 12 text on both pages. |
| Settings: confirmed model table has eleven work kinds | Test | Scope to its Rarely fold; retain all eleven dt/dd names and values. |
| Settings: intro/card spacing | Test | Check all eight pages and their groups with actual routing CSS; normal gaps ≥12 px, deliberately compact Work ≥8 px. |
| Settings: every routing setting changeable | Test | Open Rarely Sizes; retain every original name, value, option and writer check. |
| Settings: no false off while loading | Test | Open Rarely Reset controls before checking them; retain aria-busy, loading and real switch-state assertions. |
| Settings: CLI with no login settings has no login switch | Test | Open Rarely Reset controls for config-read readiness; retain absence of every login switch. |
| Settings: hold over 4000 chars and no-limit explanation (baseline failure) | Test | Stale expected phrase never matched existing copy. Assert exact existing “No limit on length; the app hands over up to 128 KB at once” after reopening the form; retain >4000 chars, unchanged exact submitted text and no-limit checks. |

The last failure was reproduced on untouched `603a16af` in the previous pass;
it was unrelated to page movement but its stale test expectation is corrected
here without changing production wording or limits. A stale rule preview found
by the existing undo test was also fixed: changing rule text resets the preview
even when an inactive page has zero layout width.

## Final gates and rendered evidence

- `npm ci` completed in the initial slice setup.
- `npx tsc --noEmit`: **passed**; `/tmp/khipu-slice-b-tsc-b2-verified.log`.
- `npx vitest run`: **1383 passed / 0 failed**, **75 passed files / 0 failed**,
  168.86 seconds. **Two of the three full runs** allowed for this completion were
  used; both passed. Final log: `/tmp/khipu-slice-b-full2.log`.
- Runtime writer log: `/tmp/khipu-slice-b-full2-writers.jsonl`.
- Final screenshots, capture scripts, measurements and review notes:
  `/tmp/khipu-slice-b-shots/README.md` and `b2-metrics.json`.

Every page's long-text screenshot was visually inspected at both requested
widths; lower portions of long pages, compact/expanded rules and open Sizes
were also inspected. No final text clipping, overlapping controls, or unintended
misalignment was found. Line wrapping of long paths is intentional. The switch
label and control remain visible on Your Macs. Earlier captures sometimes used
a stale composited scroll frame; final captures force a viewport repaint at the requested final dimensions, then wait after scroll reset. Their header pixels are identical across all 16 top-of-page captures at each width.

Not verified: installed/native webview rendering or real provider/hub delivery;
all backend data and writes in these checks are fictional. The existing preview
fixture has missing optional config values that render the app's real loading
state in some Rarely/runner rows; full values and enabled controls are covered
by the richer full-App runtime inventory fixture. No real Khipu configuration,
live slot, installed app, publication, PR, merge, tag, or release was touched.
