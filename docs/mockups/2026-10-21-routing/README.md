# Routing as a first-class feature: app mocks (slice 14, 2026-10-21, waiting for Matt's approval)

Static mocks for the five app groups of `docs/plans/2026-10-20-routing-first-class.md` (section 11), with Pace
carrying section 13's caps, advice and unknown evidence. They extend the approved
`docs/mockups/2026-10-14-routing/` and `docs/mockups/2026-10-15-settings-clarity/` mocks and sit inside today's
Settings → Accounts and routing page: the intro, Needs you, and groups that fold. No app code changes until
these are approved (slice 14's stop-condition).

Open any `.html` in a browser. Each screen has a light and a dark file. `python3 gen.py` rebuilds every screen,
plus `App-light.css` and `App-dark.css` from the app's own stylesheets (`apps/desktop/src/App.css`,
`routingOverview.css`, `accounts.css`), so the mocks use the app's tokens and components. `mock.css` holds only
the new layout these groups need. `python3 render.py` writes full-page PNGs to `png/` with headless Chrome
(`render.py 960` renders at the app's smallest window instead of 1280).

All accounts, projects, Macs, words and times are fictional: accounts Claude, Claude second, Codex, Grok, GLM,
DeepSeek and Muse; projects project-a to project-d; Macs "This Mac", MacBook Air and Office mini. "Now" is
Wednesday 2026-10-21 at about 2:40 PM. The numbers agree with each other and with section 2a's formulas (for
example, Claude's week: 31% used, 0.82% an hour, 0.59% an hour needed, runs out Sun 2:46 AM, 32 h before the
Mon 11:00 AM reset; its even pace to 65% plus 5 points allows 25% by now).

## Screens

| File | Group | Plan section and case | What it shows |
|---|---|---|---|
| `overview` | All | 11; 9 (attention items) | The page with every group closed: the two new groups (Pace, caps and advice; Checks and copies), Holds renamed Proposals and holds, Job runners and lanes renamed Providers and lanes. Needs you carries the new attention items |
| `delivery` | Telling conversations (delivery and health) | 11 "Delivery and health"; 9 delivery health; 4.3; 5; 7; case 15 | The red check in plain words (a conversation 2 changes behind past its bound; a Mac 2 h behind). Live conversations with Mac, last prompt and what each has, including pull-only ones (Grok Build, Aegis), a T3 thread that switched provider, and Grok Bot through the cloud copy. Recent changes and who has them. Other Macs' and the cloud copy's freshness. The existing settings stay in two folds |
| `delivery-states` | same | 4, 5 ("untracked"), 7 (offline) | Empty (no live conversation); unknown (a caller with no conversation name; a conversation on a Mac that hasn't synced); error (the delivery read failed, with what still works); this Mac offline |
| `proposals` | Proposals and holds | 11; 3; case 4 | Two proposals in use beside your words, with the change marked; one crossed (written against an older version, never took effect); one overtaken (two Macs at once). Holds, one Mac-only. Ended proposals collapsed |
| `proposals-states` | same | 2 (end or keep, decision 4); 3 and decision 6 (proposal words stay on their Mac) | Empty; a proposal whose words are on another Mac; a priority change that names two held accounts, asking end or keep for each, with Save waiting for both; error |
| `lanes` | Providers and lanes | 11; 8; 13 B and C; decision 3; case 8 | Providers with maker, how the plan is read, allowed models and use (Muse "On request"). Accounts and the Macs each is on. Lanes with how each matched: by name, declared and confirmed, declared and only claimed, your map, an ambiguous name with its candidates, and one with no account naming the field to add |
| `lanes-states` | same | 8 (adding DeepSeek from the app); 13 B (conflicts never match) | Adding a provider; no job runner folders (empty); a lane whose declared account disagrees with its login (unknown); error |
| `checks` | Checks and copies (advisory and copies) | 11; 9; 10; 13 D and E; cases 10, 14, 17c | Today's model mismatches (from the brief's task line, or marked as a guess); the model table folded with its confirm date; contradictions; a stale level in your words with "Set a level instead"; rule upkeep (a dated rule, an overlap, a 31 h pending edit); routing copied into files, a memory note and a runner's settings; a direction said in a conversation that never reached Khipu |
| `checks-states` | same | 9 (decision 5) | The model table waiting for its one confirmation; nothing found (empty); files never looked at (unknown, kept apart from "nothing found"); error |
| `pace` | Pace, caps and advice | 2a; 13 A; cases 16, 17b | Claude open (it needs attention): each window's use, rate, the rate that lands on the limit or cap, the verdict, a 24 h (or 1 h) trend with the needed rate and the cap drawn; its four caps (a five-hour share, a weekly share, an even pace to 65% plus 5 points, 13% a day for jobs) and where each stands; Khipu's advice for jobs and for lead conversations with the evidence's age. The other six accounts as one line each |
| `pace-all` | same | same | Every account open: Codex on pace for its plan but ahead of its own even-pace cap; Grok outpacing its 76% share with no five-hour pace; Claude second underpacing; GLM at 1 of 1 job; DeepSeek's spend unknown because of a runner gap; Muse closed |
| `pace-states` | same | 2a (reasons for "unknown"; caps from words); 13 A (expired running count) | Not enough evidence: a possible reset, a reset time that disagrees, readings covering 9 of 24 h, an expired running count. Each still advises go with the evidence marked unknown. A cap found in your words with one-click confirm, and the Add a cap form; no readings yet (empty); error |

PNGs: `png/<screen>-light.png` and `png/<screen>-dark.png`, rendered at 1280 wide.

## What the mocks decide

- Nothing on these screens starts, stops or blocks work (rule 7). Advice says so where it shows, and each check
  says "None of them stops any work".
- "Can't tell" never looks like "nothing wrong". Failed reads, unknown evidence and empty states each have their
  own words, and an error says what still works.
- Owner actions stay one click from the thing they fix: Confirm or Dismiss a proposal, End now on a hold, Pick
  one or Map on a lane, Make it a cap, Set a level instead. Every one is also reachable from chat through
  `khipu_owner_act`; the app never edits your words.
- Caps show as "confirmed by you" once per account. A cap found in your words counts only after you confirm it.
- Forecasts say what they assume ("at the last 24 hours' rate"). Rates are written "0.82% an hour".

## Better Design review

- Principles loaded before drawing (`get-ui-principle`, `get-ux-principle`); `get-review-rules` checked after.
  Fixed from the rules: several numbers that disagreed (the count of accounts holding back, Claude's five-hour
  needed rate, Codex's five-hour verdict, when Codex's hold-back clears), repeated identical badges
  ("Confirmed Oct 20" on every cap, "Superseded by Khipu" on every file), and jargon ("hooked", "MCP client",
  "session", "sync").
- `check-comprehension` on each group. Fixed: one 28-word sentence; Pace folded so only accounts that need
  attention open (448 words before). **Not fixed, on purpose:** every group still reads as "over 60 words and more
  than 3 actions" (200 to 270 words). These are owner settings lists where each action belongs to its own row,
  like the approved Accounts group. Cutting further would hide what section 11 says each group must show.
- `inspect-spacing` on the open Claude pace block at 960 and 1440 wide: score 90, no critical or serious
  findings. Two moderate findings: a 1 px gap of mine (fixed to 0) and the 6 px gap inside the app's own `.tag`
  (left as the app has it). Not measured at 390 or 1728: this is a desktop app whose smallest window is 960.
  The rest of the page was checked by eye in the renders, not measured.
- Visual passes: every screen was looked at in its PNG, at most three passes each.

## Approved

Matt approved these mocks on 2026-10-21 ("Proceed as you rec"), taking every recommendation below, and confirmed Grok's cap at 76%. App slices 15a-15e build from these screens.

## Open questions for Matt (each with a recommendation)

1. **Where Pace lives.** Today one group is "Priority and pace". *Recommend* splitting it: "Priority" keeps your
   order and roles, and a new "Pace, caps and advice" group sits right under Accounts (as mocked). Pace now
   carries caps and advice, which is too much to tuck under Priority.
2. **Pace on Home.** Section 2a shows pace in the routing block and the app's Pace group. *Recommend* one line on
   Home's Routing card only while an account is outpacing or a cap is less than 6 h away (for example "Claude
   reaches your five-hour 60% at 4:40 PM"). Not drawn yet; it gets a mock if you want it.
3. **Which Pace accounts open by themselves.** *Recommend* open when an account is outpacing, ahead of its even
   pace, or within 6 h of a cap; everything else one line (as mocked: only Claude open).
4. **The word for orchestration.** Advice differs for jobs and for the conversation you type in. *Recommend*
   "Lead conversations". Alternatives: "Your conversations", "Orchestrators".
5. **Unknown evidence says "Go".** Decision 16 and section 13 A answer `go` with the evidence marked unknown,
   and the mocks show "Go" plus "evidence unknown: …". *Recommend* keeping that, since the runner decides. The
   alternative is a third word, "Can't tell", in place of Go.
6. **Grok's share cap: 76% or 80%?** The plan (decision 13, section 2a) says 80%. Today's routing record (rule 1)
   says 76%, so the mocks use 76%. *Recommend* confirming 76% as the cap fact, since it is the newer owner
   direction.
7. **Delivery's group name.** *Recommend* keeping "Telling conversations", the name you already know, with the
   live list and health inside it and the size settings folded below.
8. **Recent changes in two places.** Delivery shows the last three changes and who has each. *Recommend* the
   Recent changes group also gains a "who has it" column, so the two never disagree.
9. **The cleanup of routing copied into files** (decision 7). *Recommend* that "Review a cleanup…" shows every
   change and makes a backup first, and appears only after the first look at your files (as mocked).
10. **Where "Share proposal words between your Macs" lives** (decision 6). *Recommend* Telling conversations →
    Sizes and times, beside the other cross-Mac settings. The proposals-states screen points there.
11. **The narrow window.** At 960 wide each pace window takes two lines, with the trend under the bar.
    *Recommend* accepting that rather than dropping the trend.

Approving this page approves the five groups' layout, words and states. Slices 15a to 15e build them in the
app.
