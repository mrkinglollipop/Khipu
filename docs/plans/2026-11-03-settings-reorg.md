# Settings reorg: Accounts and routing, organized by what the owner is trying to do

Asked by Matt 2026-11-03: "impossible to read"; "I would want the settings to be the same ... on both" Macs. No build until approved. Mock: `docs/mockups/2026-11-03-settings/index.html`. Kept from `2026-10-15-settings-clarity.md`: no option moves to a terminal, no problem is hidden.

**Why "Telling conversations" didn't show.** The row is never removed: `GROUPS.map` draws all 11 (`RoutingSettings.tsx:5130`), `record` changes only a tag, list filters touch only their lists, and any build that says "Accounts and routing" has it (`109cd5e3`). It is off-screen. Real frontend, default 1100×720, fictional record: 1,959 px down when calm, 2,447 busy, because Accounts opens by default at about 1,430 px; 1,031 with every group closed; 6,570 after "Open all groups", which each Mac remembers. Changing section keeps the scroll (`App.tsx:1261`). Not confirmed against his screen.

## Top problems, ranked

1. **Nothing can be found.** Eleven look-alike folds named after the code, no search; the Mac copy switch is under "Sizes and times".
2. **Confirm is blind.** Needs you says "2 proposals from conversations to confirm", then Show. The setting band gives no old or new value, though the CLI sends `request.prior`; Home's Confirm (`RoutingCard.tsx:171`) doesn't say what changed. Bands aren't counted, so "Nothing here needs you." renders right under a Confirm button.
3. **Things sit where the code put them.** Rule limits in Telling, hold limits in Holds; Mac facts in three places; 17 tunables in one list, beside daily controls.
4. **Jargon, and Macs differ silently.** Telling, Pace caps and advice, lanes, Seeded, Crossed; see Settings sync.

## Proposed structure

Eight pages in an always-visible list with a Find box. Folds only on Rarely needed; pages open at their top; list rows count problems; deep links keep working.

| Page | Task | Holds |
|---|---|---|
| Needs you | What needs me? | Decide (before, now, who, if ignored; buttons name the result), Fix, Later |
| Your accounts | State of my accounts? | Who has room, when it resets, details |
| Who gets what work | Priority; change a rule | Order, note, rules, temporary instructions (Holds) |
| Your Macs | Do my Macs agree? | The sync switch, differences, what stays local |
| Conversations | Have they heard? | Who is behind; when they hear (Telling) |
| What changed | What happened? | History, who did it |
| Something's wrong | Something is broken | Job runners, job slots (lanes), broken rules, conflicts |
| Rarely needed (set apart) | Technical | Limits, timing, warnings, reset checks, runner setup, old file |

## Inventory

Who (my estimate, no usage data): **D** owner daily, **R** owner rarely, **N** nobody. Macs: **S** same on every Mac, **S+** same and copies today when the routing copy is on, **T** this Mac only, **–** local readout.

| Setting or readout | Now | Plain English | Who | New home | Macs |
|---|---|---|---|---|---|
| Needs you; setting, account bands | Top | What waits on you | D | Needs you | – |
| Open/Close all groups | Top | Folds everything | R | Removed | – |
| Account rows, detail (pause, words, caps) | Accounts | Who has room; controls | D | Accounts | S+, caps S |
| Pace, advice; next reset checks | Pace, Resets | Will it run out? | R | Accounts | – |
| Sort, filter; Check all now | Accounts | View; re-read plans | R | Accounts | T |
| Add account, find accounts | Accounts | Add one | R | Accounts | S+, login T |
| Accounts read, Not offered, auto-track | Accounts | Logins to read | R | Accounts | T |
| Login reads: Grok, Cursor, OpenCode | Accounts | Login to usage page | R | Rarely | T |
| Priority list, end, clear | Priority | Your order of accounts | D | Work | S+ |
| List by pace if no order; warn if unconfirmed | Priority | Fallback; reminder | N | Work; Rarely | S |
| Note; rules (add, edit, filter) | Rules | Your words | D | Work | S+ |
| Proposals: Confirm, Edit, Dismiss | Holds | A conversation's change | D | Needs you | S+ |
| Holds: list, add, end | Holds | Temporary instructions | R | Work | S+ |
| Hold length, words, reminder | Holds | Hold bounds | N | Rarely | S |
| Mismatches, conflicts, upkeep, file copies | Checks | What disagrees | R | Wrong | – |
| Model table | Checks | Your confirmed models | R | Rarely | – |
| Delivery health, live, who has it; what each hears, pace news, old off | Telling | Who heard what | R | Conversations | – |
| Other Macs and the cloud | Telling | Is each copy current? | R | Macs | – |
| Tell conversations; 3 moments | Telling | When they hear | R | Conversations | S |
| Dispatch scripts (names) | Telling | Your hand-off scripts | N | Rarely | S |
| Longest summary, update, helper block, line, rule, note; most rules, rules shown | Sizes | Length limits | N | Rarely | S |
| Resend-after, out-of-date, unstated limit | Sizes | Fact lifetimes | N | Rarely | S |
| Warn: slow, disagreement, not refreshed | Sizes | When to warn | N | Rarely | S |
| Prompt, start budgets; record removal | Sizes | This Mac's speed | N | Rarely | T |
| Proposal words; Copy routing, Retry cleanup | Sizes | What leaves this Mac | R | Macs | T |
| Reset check, wait, retries | Resets | Re-read at reset | N | Rarely | S |
| Wait while busy (3 fields) | Resets | This Mac's load limit | N | Rarely | T |
| Providers; lanes (list, search, map) | Providers | Slot to account | R | Rarely, Wrong | S+ |
| Runner folders; old routing file | Providers, Table | Local paths | R | Rarely, Wrong | T |
| Recent changes, filters, bring back | Changes | History | R | What changed | S+ |

## Settings sync

**Today.** Settings don't sync. Nothing copies unless `routing_replication_live` is on (per Mac, off by default). Then rules, note, priority, pauses, holds, definitions and limit reports go through the closed map `_REPLICATION_FIELDS` to the hub's `routing_events` every 30 s. All of `config.json` stays local (every `routing_*` setting, features, login switches), and so do caps (`cap_set` isn't in the map). The record is read with local settings, so two Macs can tell conversations different things.

**Proposed.** One switch, "Keep settings the same on my Macs", on Your Macs; recommended on. It folds in the routing-copy switch and keeps its gate; the old name still finds it. It copies everything marked S, caps included. **Never copied:** secrets, database and gateway addresses, paths, login folders, launchd jobs, machine limits, and switches that decide what leaves a Mac (this one, proposal words, login reads).

**Same route, same gates.** New events (`setting_set`, one snapshot at switch-on) use the existing log, map, `_sync` and `routing_events`; no second path. Only owner-gated writes (`config.routing_setting`, answered requests) emit one; a conversation's unanswered change never travels. The receiving Mac re-checks with `_coerce` and logs to `owner-actions.jsonl`, so Undo works.

**Conflicts.** The last owner change wins by its own time, hub order breaking ties; the other value stays on Your Macs until he chooses. At first switch-on nothing overwrites: he picks from a list of differences.

## Out of scope and build slices

Out: routing behaviour; the other 12 Settings sections' layout; answering Needs you in a conversation (`feat/needs-you-in-conversation`; share the wording). Each slice inspects the code first, gates on `npm run build` and vitest, and stops if the mock doesn't cover a state.

| Slice | One-line goal |
|---|---|
| A | Make every Needs you item readable, bands and Home's Confirm included; ships alone |
| B | Replace the folds with the eight pages, moving sections as they are, keeping deep links |
| C | Reword and slim Your accounts and Who gets what work |
| D | Split Telling and Checks into Conversations, What changed and Something's wrong |
| E | Build Rarely needed, Find a setting and the Macs tags; update Guides and What's new |
| F | Add settings events and caps to the routing copy; first prove an older Mac ignores them |
| G | Build Your Macs: the one switch and the differences |
| H | Carry out the decisions below |

## Open decisions for the owner

1. **Should Accounts and routing get its own place in the left rail, out of Settings?** Recommended: yes; you check it daily. The mock shows this.
2. **Should "Keep settings the same" cover all of Settings, not just this screen?** Recommended: yes, this screen first, always excluding secrets, paths and machine limits.
3. **Rules copied from your old file are in use either way; confirming only removes a label. Should they keep asking?** Recommended: no; list them once under "When you have time".
