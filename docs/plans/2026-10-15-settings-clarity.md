# Settings clarity: Capture & models, Plans, Routing, standing rules, and how users learn what is new

Asked by Matt 2026-10-15, after using the app: Capture & models should be cleaner, especially the models part;
a Plans section "looks like it's not attached to anything", seemingly Grok's, "doesn't really show that it's
from Grok, and it shows a bunch of errors"; Settings → Routing is "way, way, way too long" and should be
collapsed, expandable, sortable and maybe filterable; nobody can know what "Watched" and "Not watched by
Khipu" mean; and new and updating users need to be able to set all of this up and learn what the new
features do. This page is the scope, **APPROVED by Matt 2026-10-15** with his four answers folded in (Decisions,
below), with the mocks in `docs/mockups/2026-10-15-settings-clarity/`. A build session reads it first. It changes how existing data is
shown and adds one setup step and a better what's-new; it adds no new routing behaviour. It builds on routing
truth (`2026-10-14-routing-truth.md`) and plan usage (`2026-10-14-plan-usage.md`), and it leaves room for Work
(`2026-10-15-work-view.md`), where the Owed tab folds in. Every phase reaches this Mac only through the
promotion gate (`2026-10-14-promotion-gate.md`).

**What Matt gets, in plain words.** Capture & models reads as three short questions (when is a conversation
saved, how do commitments close, which model writes the summaries) with each key in one place. Every account
in Plans is one block with its name on it, and when an account can't be read it says so once, in words, next
to the name, including that he paused it. Settings → Routing, renamed Accounts and routing, opens as a one-screen overview: a short "Needs
you" list at the top, then one line per group that he can open, with accounts he can sort and filter. A rule
says "Checked by Khipu" and what the check found, or that Khipu can't check it. A new user meets accounts and
routing in setup; an existing user sees what is new after an update twice over, once as a dialog and once as
a card on Home that stays until he closes it, each item with a Show me button.

## Status

| Phase | What | Size | State |
|---|---|---|---|
| 0 | This scope and the mocks | S | approved by Matt 2026-10-15 |
| 1 | Three fixes: no false "off" while loading, no "Live" beside "no turn yet", no negative ages | S | open |
| 2 | Plans: one block per account, one failure line, paused shown, on Home and in Settings | S–M | open |
| 3 | Routing overview: groups that fold, the Needs you list, Plans moves into Accounts | M | open |
| 4 | Routing lists: sort and filter accounts, filter and search rules, filter recent changes | S–M | open |
| 5 | Standing rules: "Checked by Khipu" with the check's result | S | open |
| 6 | Capture & models: three cards and one Keys list | M | open |
| 7 | Setup: an Accounts step | S–M | open |
| 8 | What's new after an update (dialog and Home card), and its history in Settings → Updates | S | built, in review (branch feat/clarity-whatsnew) |

One PR per phase. Phase 1 goes first because it fixes things that are wrong today. Phases 2 to 6 can go in any
order after 3, except that 4 and 5 need 3. Phases 7 and 8 go last, because they introduce what the earlier
phases made readable.

## Source facts (2026-10-15, `apps/desktop/src` unless named)

- **Capture & models** is `settingsSection === "capture"` in `App.tsx` (about 5314 to 5491). It shows six
  cards in this order: Capture (read-only cadence and secrets), How captures are stored (`CaptureTuningCard`,
  `SettingsControls.tsx`: where a capture is written, two similarity thresholds, your names), Models
  (`SynthProviderFields`, `SynthProvider.tsx`: seven providers in a select, four fixed paragraphs of quality
  notes shown for every provider, endpoint and model id, key or login status, the fallback, Save models),
  Summariser calls per day, **Plans** (`PlansSettingsCard`, `PlansCard.tsx:651`), and Keys (Gemini key, local
  key, three key checks, "Currently stored"). The Keychain sentence appears three times; key presence shows
  in Models and again in Keys; the Gemini key's three uses are explained in four places; the key checks show
  only under Keys, away from the provider they test, and only after a save. "Close an open commitment at" and
  "Your names" (commitment settings) sit in the card about where captures are stored.
- **Plans** is a Home card (`PlansCard`, `App.tsx:3670`, directly above the Routing card) and a Settings card
  in Capture & models (`App.tsx:5394`, between the daily calls card and Keys, nothing to do with either). Both
  read `khipu usage --json` (`packages/cli/khipu/usage.py`). A failed read keeps the last good figures and sets
  `ok: false` and a short code; only four codes have a note (`_note()`, `usage.py:113`), so Grok's usual
  failures (`timeout`, `cli_error`, `bad_reply`, `no_answer`) have none. In one Grok row the same failure can
  show three times in three wordings: the `plan-none` text, a warn tag, and the reach tag "Can't be read just
  now (reported an error)" followed by "Sign in from Grok Build" (`signInOffer` offers sign-in for any
  unreadable Grok row, `PlansCard.tsx:430`). In Settings an account is a `rows plan-account` group whose rows
  are separated like any other rows; only the first row names the account, so the reach row and the failure
  row below it read as loose rows. A paused account is unknown to Plans; the pause lives in the routing record.
  On Home, when a later read fails, a footer row "The last read failed; these figures are from the read before
  it." names no account (`PlansCard.tsx:625`).
- **What Matt most likely saw (point 2).** He was reviewing Settings → Capture & models. Its fifth card is
  Plans, which has nothing to do with capture or models (the "not attached to anything"), and in it the Grok
  group: the name and plan on the first row, then separate rows with a warn tag, "Can't be read just now
  (reported an error)", "Sign in from Grok Build" and the failure phrase again, none of them naming Grok, and
  nothing saying Grok is paused by him. The design below fixes both readings (the card's place and the
  group's rows), and the Home card too, which shares the same row logic. Not confirmed with a screenshot.
- **Muse.** `parse_muse` returns the note "no turn observed yet" with `ok: true` and no windows when Muse has
  not run a turn; `liveWords` (`PlansCard.tsx:411`) looks only at `ok`, so the row also shows a green "Live:
  read 17s ago". The tests lock this in (`PlansCard.test.tsx:85`).
- **Settings → Routing** is `RoutingSettings.tsx` (2918 to 2985): eleven cards, none of which fold. With a
  realistic record (5 accounts, 11 rules, 2 holds, 2 job runners) it is about 200 rows: Accounts with
  Priority and pace (about 30), your note, Standing rules (about 30), Holds, Checking for resets, Telling
  conversations (about 30, including 11 sizes and times), Job runner folders, Which account each job lane
  uses (10 to 15), Routing table file, and Recent changes (up to 100 changes, grouped by day). Problems are
  flagged only on their own rows; no card says it holds one.
- **Watched.** `routing_sources.watched` (`packages/cli/khipu/routing_sources.py:893`) marks a rule watched
  when its words match one of four checks Khipu has: a `-fast` model, the paid xAI API, Grok on privacy or
  safety code, and a different family re-checking Grok. `rule_checks` reads each job runner's settings and
  added-work lines and reports `broken` or `cannot_check`; it never reports a pass, it blocks nothing, and a
  broken result is a warning on Home and in the health report only, not on the rule's own row. With no job
  runner folder added, a watched rule can never be checked. `khipu route --json` already carries
  `rule_checks` and `watched_rules` (`routing.py:4607`, `4615`).
- **Loading.** While `khipu_config_show` and `khipu_features_show` have not answered, every switch in Telling
  conversations and Checking for resets renders unchecked and disabled (`checked={... === true}`,
  `RoutingSettings.tsx:2032`, `582`), and Job runner folders and lanes render their empty-state words, because
  those cards are drawn before the routing record arrives (`2966` to `2983`).
- **Ages.** `fmtAgeSince` (`time.ts`) subtracts a hub timestamp from the Mac's clock with no clamp, so a hub
  clock 40 seconds ahead shows "-40s ago" (`App.tsx:3803`, Home Needs you; `3349`, Owed). The app records when
  it read the list (`owedFetchedAt`, `App.tsx:1679`) but does not use it for ages.
- **Setup** (`Welcome.tsx`, `STEPS`): Welcome, Database, Model, Graph, Agents, Finish. No step mentions
  accounts, plans, routing, priority or pace. Settings → Advanced → "Run setup again" reopens it; it can open
  at a given step.
- **What's new** (`postUpdateNotices.ts`, `PostUpdateNoticeDialog.tsx`): one notice per version, and after an
  update only the newest eligible notice shows; a user who skips three versions sees one of three. A notice
  can link to Harnesses, Home, or Settings on Optional features or Embeddings only. A fresh install shows
  nothing. Routing has no notice. Once shown, a notice cannot be found again anywhere.

## What Khipu does and does not do

Unchanged by this scope: Khipu reports, it never routes. Nothing here sends, stops, refuses or reorders work.
Sorting and filtering change only what the page shows; Khipu's own priority order is changed only by the
Priority controls, as today. Every option that exists today stays in the app and usable (some move, some fold;
none goes to a terminal), and every hard limit stays liftable with its Change… or Lift button.

## Rules for every phase

1. **Folding never hides a problem.** A group, block or list that is folded shows, on its closed line, the
   count and tone of every problem inside it (red stays red, a needs-you item stays named). A problem is
   anything the app already marks warn or err, or asks the owner to act on: an account at its limit or down,
   sources that disagree, a rule to confirm, a rule that may be broken, a job runner that stopped reporting or
   can't be read, a lane with no account, a read that failed.
2. **Never a false state.** A switch, a count or an empty-state sentence that depends on data not yet read
   shows "Reading…", never off, zero or "none yet". A failed read says it failed, where the data would be.
3. **One account, one name on every line.** Anything about an account sits inside that account's block, under
   its name.
4. **Say it once.** One failure, one message: the plain cause, what Khipu still shows, and the one thing to do
   if there is one. The short code is kept for the health report and a "Details" disclosure, never as the
   message.
5. **Plain words, no em-dashes, local times, fictional data in mocks.**
6. **Remembered per Mac, never forced.** Which groups are open is remembered on this Mac. The app never opens,
   pops up or badges anything to get attention except what's new after an update (one dialog and one Home
   card, phase 8) and the problem counts in rule 1.
7. **Every status has words.** A dot's colour always sits next to words that say the same thing; nothing is
   told by colour alone.

## Design

### 1. Capture & models (Matt's point 1)

Mocks: `capture-models-before.html`, `capture-models.html`.

A one-line intro: "Khipu saves a short summary of each conversation (a capture) and keeps a list of what
conversations say is still owed (commitments). Here you choose when it saves, which model writes the
summaries, and how commitments close."

Then three cards and one list, in this order:

- **When Khipu saves a conversation.** One read-only line: "After 5 turns or 20 minutes, before a conversation
  is compacted, and when it ends. The same on every harness." The secrets line, shortened to "Keys, tokens
  and passwords are masked before anything is summarised or stored." Then "Where captures are written" (the
  three choices, the Locked tag when the environment sets it) and "Merge near-duplicate captures at" with its
  default.
- **Commitments.** "A commitment is something a conversation said is still owed: a follow-up, a blocker, a
  question or a promise. They are listed on Owed." Two fields moved here from the storage card: "Your names"
  ("Names that mean you when Khipu decides who owns a commitment") and "Close a commitment when a later capture
  matches it at", each with its default. When Work replaces Owed, "Owed" becomes "Work" in this sentence.
- **Session summaries.** The provider becomes a list of choices, one row each, the chosen one marked: its name,
  one short line on what it is like (from the measured notes: "Best memory, about 11 s a capture"; "Accurate,
  thinner on open loops, about 8 s"; "Fastest, about 3 s, least accurate"; for login choices "Uses the login
  you already have; counts against that plan"; for OpenRouter "One key, any model"; for local "A model on
  this Mac or your network"), and a status tag from the access check ("Key works", "No key", "Signed in",
  "Not found on this Mac", "Not checked"). Only the chosen row opens, showing what that provider needs: its
  model id, the endpoint for local only, and a pointer to its key in Keys below. Under the list: "If the
  provider fails" (the two choices, with "Fallback uses the Gemini key, about $0.002 a conversation" only when
  it applies), and "Calls today: 12 of 5,000, about $0.03" with Change… and Lift for today. The four quality
  paragraphs become the per-row lines plus one muted line "Measured on 15 real conversations, Oct 13". Save
  models stays the one save for provider and model id; the fallback still saves on change, as today.
- **Keys on this Mac.** One row per key, in one place: name, what uses it ("Summaries, the fallback and search
  by meaning" for Gemini), status ("Works, checked 2 min ago", "Stored, not checked", "Not stored", "Set by
  GEMINI_API_KEY in the environment; that one is used"), and Add…, Replace… and Check now. The key checks run
  when the page opens if a key has never been checked, and from Check now, not only after a save. One line
  under the list: "Keys are kept in this Mac's login Keychain, never in a file." The Gemini, Claude API,
  OpenAI, OpenRouter and local endpoint keys all live here; the provider rows only show status.
- **Plans leaves this page** and moves to Settings → Accounts and routing → Accounts (section 3), where the same accounts
  already are. Its accounts list ("which accounts Khipu reads") moves with it.

Embeddings stays its own section; the summaries card says in one line that search by meaning is set there.

### 2. Plans (Matt's point 2, and polish item b)

Mocks: `plans-before.html`, `plans.html`.

**Home card.** One row per account, as today, but the row's last column holds exactly one status phrase:

| Account state | Row shows |
|---|---|
| Read now | the meter, percent and reset, nothing else |
| Read failed, earlier figures kept | the meter dimmed, the percent, and "Can't read it now; figures from 2 days ago" |
| Read failed, nothing kept | "Can't read its plan: Grok Build didn't answer" (the cause in words, from a fuller code map) |
| Not signed in | "Not signed in" with Sign in again (Claude, Codex) or "Sign in from Grok Build" |
| Paused by you | a neutral "Paused by you until Wed, Oct 21, 9:26 PM" tag before anything else, taken from the routing record |
| Muse with no turn yet | "No figures yet. Muse reports its plan after its first turn." No "Live" tag |
| Muse with figures | the meter and "as of its last turn, 2 h ago" |

The unattached footer goes; when the whole last read failed, the card head says "Last read failed 2 min ago;
showing earlier figures" next to Refresh. "Live: read Ns ago" is shown only when there are figures from that
read (fixes polish item b); a successful read with no figures says when it was checked, in grey.

**Settings.** Each account is its own bordered block with its name and plan as the block's heading; every line
about it (each window, the read times, the failure, sign-in, Check now, Details) sits inside the block, so
nothing floats. A paused account's block says so first. A failure is one line, with "Details" opening the
short code, the last attempt and the last good read. The block list sits in Accounts and routing → Accounts (section 3);
Home's card and the Routing accounts list use the same account order and names.

**Backend.** `_note()` gets plain notes for every code it can return (`timeout`: "didn't answer in time";
`cli_error`: "reported an error when asked"; `bad_reply`: "answered in a way Khipu can't read"; `no_answer`:
"didn't answer"), so the app never invents its own wording, and a test fails if a code has no note.

A paused account's plan keeps being read (Matt, 2026-10-15): a read costs no turn and shows when the pause can
end.

### 3. Settings → Accounts and routing: an overview that folds (Matt's point 3)

The section is renamed from "Routing" to "Accounts and routing" (Matt, 2026-10-15), since Plans now lives
here. Every link, deep link and what's-new item uses the new name.

Mocks: `routing-before.html`, `routing.html`, `routing-open.html`, `routing-states.html`.

The page opens as an overview about one and a half screens tall at 1280 by 800 (today's page is about ten), with Accounts open:

- The intro, shortened to one line: "Khipu tells your conversations which accounts have room, what you
  paused and what your rules are. Your agents decide where work goes."
- **Needs you**, only when something does: a warn callout listing each problem in one line with a button that
  opens its group and moves focus to the item ("Claude second: two sources disagree", "2 standing rules to
  confirm", "The job runner in project-b stopped reporting 3 h ago", "Lane review-2 has no account"). With
  nothing to do, one grey line: "Nothing here needs you."
- **Groups**, each a folding card with a one-line summary and its problem tags on the closed line (rule 1),
  and Open all / Close all at the top right:

| Group | Closed line says | Open by default |
|---|---|---|
| Accounts | "5 accounts: 2 open, 2 at their limit, 1 paused" | yes |
| Priority and pace | "Claude second first, then Codex, then Claude main" | no |
| Your note and standing rules | "Note from Oct 13 · 11 rules · 2 to confirm" | no |
| Holds | "2 holds, the next ends Wed, Oct 21, 9:26 PM" or "None" | no |
| Telling conversations | "On: at the start and before each prompt" | no |
| Checking for resets | "On: up to 3 checks after each reset" | no |
| Job runners and lanes | "2 folders, 9 lanes" plus "1 stopped reporting" | no |
| Old routing table file | "ROUTING-NOW.md, read only" | no |
| Recent changes | "14 changes today" | no |

- **Accounts, open.** One line per account: name, state in words with its dot, what is left of the week, and
  under it the spend pace as the app says it today ("About 1.8% an hour to use it all (a lot left, little
  time)"); an account Khipu can't read says why it has no pace. The line is a grid: a long state such as
  "Limit reached for five hours, back at 6:10 PM" wraps in its own column and never runs into the week figure;
  in a narrow window the week and pace drop under the state. Opening a line shows what today's row shows (source, next check, the pace sentence,
  Check now, which also reads the plan again, Mark paused until…, Lift, Clear, the disagreement panel) and
  the account's Plans block from section 2, so plan figures and routing state for one account are in one place. Above the lines: a "Sort by"
  menu (Your priority, Back soonest, Most left, Spend pace fastest first, Name; default Your priority) and filter chips with counts (All,
  Needs a look, Open, At the limit, Paused). A filter that hides a problem still counts it on the chip; "Needs
  a look" is every account with a problem. "Which accounts Khipu reads" (today's accounts field under Plans)
  is the last line of the group.
- **Telling conversations, open.** The four switches as today; "What each kind of conversation hears" and
  "Sizes and times" fold inside it, the latter's closed line saying "11 settings, 2 changed from the default".
- **Job runners and lanes** joins today's two cards: each folder with its state and, under it, its lanes.
- **Recent changes, open.** Today's changes, with "Show earlier days" below; filters by who (You,
  Conversations, Job runners, Khipu) and by kind (Accounts, Rules and note, Holds, Settings).
- Long lists inside a group (more than 8 rules, more than 8 lanes) get a search box.
- **Old routing table file** stays, because the app still reads `ROUTING-NOW.md` (setting `routing_table`,
  `routing_sources.py` `read_table`). Its help says plainly that it is an old file kept by hand before Khipu had
  its own record, that Khipu reads it but never writes to it and never takes an account's state from it.
  "NOW block" becomes "current-state section".

Which groups are open, the sort and the filters are remembered on this Mac. A link from elsewhere (Home's
"Routing settings", an attention item, a what's-new item) opens Settings → Accounts and routing with that group open and
scrolled into view.

Tabs were the other design considered (Accounts, Rules, Telling, Job runners, History). Folding groups won:
everything stays on one page, the Needs you list can point into any group, and a problem in a closed group is
still on screen; with tabs a problem in another tab is out of sight.

### 4. Standing rules: "Watched" (Matt's point 4)

Mock: inside `routing-open.html`.

What it means today: Khipu has four automatic checks. Each reads your job runners' settings for one kind of
mistake (a `-fast` model, the paid xAI API, Grok on privacy or safety work, Grok work with no re-check by
another family). A rule whose words match a check is "Watched"; any other rule is passed to conversations as
written and Khipu has no way to check it. A watched rule that looks broken gives a warning on Home; nothing is
ever stopped.

New words:

- The group's help line: "Every conversation sees these rules first, in full. Khipu can check 4 of them by
  reading your job runners' settings; if one looks broken you get a warning. Khipu never stops any work."
- A rule Khipu checks shows a line under it with the check's result, never just a label:
  - "Checked by Khipu in your job runners' settings: nothing wrong found, 4 min ago."
  - "Checked by Khipu: may be broken. The lane fast-review in project-a uses a model ending in -fast." (warn
    tone, on the rule's own row as well as on Home)
  - "Checked by Khipu, but not yet: add a job runner folder so it has something to read." (no folder
    added), "Checked by Khipu, but not yet: none of your job runners has a lane this rule is about.", or
    "Checked by Khipu, but not here: the model is set inside the launch script, which Khipu doesn't read."
- A rule Khipu can't check shows, in grey: "Passed to every conversation as written. Khipu has no way to check
  this one."
- A filter row above the rules: All, To confirm, Checked by Khipu, May be broken.

`rule_checks` already reports broken and cannot-check per rule. The build adds what a pass needs: the time of
the last check and the rules it covered, so "nothing wrong found" is said only when a check actually ran.

### 5. New and updating users (Matt's point 5)

Mocks: `setup-before.html`, `setup-accounts.html`, `whats-new-before.html`, `whats-new.html`.

**Fresh install: an Accounts step in setup**, after Agents and before Finish (setup becomes seven steps; the
Welcome page's list gains the line). Title "Your accounts". Text: "Khipu can see how much of each plan is left
and tell your agents which accounts have room. It never sends or stops work; your agents decide."

- The accounts found on this Mac, one line each, with what Khipu could read: "Claude Code: signed in, Max,
  62% of its week left"; "Codex: signed in, Pro"; "Grok Build: not signed in. Sign in from Grok Build, then
  Check again"; "Muse: found; it reports its plan after its first turn". A checkbox per account, on by default,
  is "Read this account's plan" (today's accounts field). Not found accounts are listed once in grey.
- "Which should your agents try first? (optional)": the found accounts in a list with Move up and Move down;
  leaving it as is sets no priority. One line on pace: "Khipu also works out how fast each week is being used,
  so your agents can spend what would otherwise expire."
- The master switch "Tell my conversations about this", on (the current default).
- "Standing rules and holds can be added later in Settings → Accounts and routing." Next continues; nothing on this step is
  required, and Skip for now leaves everything at today's defaults.
- "Run setup again" can open straight at this step. When Work ships, its phase adds its own line to this step
  ("Work: what each project is doing"), not a new step.

**After an update: What's new, as a dialog and a Home card** (Matt, 2026-10-15: both, so closing one by
accident still leaves the other).

- On the first launch after an update, one dialog lists everything new since the version this Mac last showed,
  newest first, each item one line with a Show me button that opens the exact place (Settings → Accounts and routing with
  a group open, Settings → Capture & models, setup at the Accounts step, Home). At most five items, then "and
  3 more in Settings → Updates". One button closes it: Got it. Show me closes it too. The dialog says the
  same list stays on Home.
- The same items also appear as a "New in 0.5.0" card at the top of Home: the first three with Show me, "5 more
  since 0.4.12" and a link to Settings → Updates, and a Close button. It stays until closed there; closing the
  dialog does not close the card, and closing the card does not bring the dialog back.
- An item can be marked "set it up": for routing, when this Mac has no routing record or no account read yet,
  the item's button is "Set up accounts" and opens setup at the Accounts step; otherwise it is Show me.
- Nothing else: no badge, no dot, no reminder, no second dialog. Closing the dialog and the card is final for
  that version.
- Settings → Updates gains "What's new", every notice by version, each with its Show me, so it can be found
  again any time.
- A fresh install shows no dialog: setup covers it, and finishing setup records the version.
- The notice list moves from one notice per version to items per version; `settingsSection` widens to every
  section and takes an optional group (`routing#accounts`).

### 6. Polish

Mock: `polish.html`.

- (a) **Loading.** The switches in Telling conversations and Checking for resets show a "Reading…" tag in place
  of the switch (with `aria-busy`) until their settings are read, then the real switch. Job runner folders and
  lanes say "Reading…" until the record is read. If the settings read fails, each affected switch says "Can't
  read this setting" in place, with Try again, instead of an off switch and an error at the bottom of the page.
- (b) **Muse.** Section 2.
- (c) **Ages.** `fmtAge` never shows a negative: anything under 0 reads "just now". Commitment ages are counted
  from the time the Mac read the list: `khipu owed --json` gains the hub's own time at the read (or an age per
  row), the app shows that age plus the seconds since the Mac read it, so a hub clock that runs ahead or
  behind no longer moves the age. Applies to Home's Needs you, Owed, and Work when it ships.

## What this will not do

- Change what Khipu records or tells conversations, or how routing works.
- Remove any option. Options move (Plans, commitment settings, keys) or fold, and every one stays usable.
- Add a new kind of check for standing rules. Only the result of the four existing checks becomes visible.
- Nag. One dialog and one closable Home card per update, nothing else.

## Phases

Oracle for every phase: from `apps/desktop`, `npx vitest run` and `npm run build` (with `tsc`); `cargo test`
in `apps/desktop/src-tauri` when a phase touches Rust; for phases that touch `packages/cli`, hermetic pytest
from `packages/cli` under a temp `HOME` with `KHIPU_KEYCHAIN=0` and `ruff check --no-cache packages/cli`.
Then the installed app through the promotion gate, seen rendering at 1280 and 960 wide next to the mock
(screenshots written to files, not read inline). At most three runs of an oracle per change; still red means
stop and report with the last log.

### Phase 1: three fixes (S, one session)

Read this page and `polish.html` first. Inspect `RoutingSettings.tsx` (`TellingSection`,
`ResetChecksSection`, `FoldersSection`, `LanesSection`), `settingsCli.ts`, `time.ts`, `PlansCard.tsx`
(`liveWords`, `StatusTags`) and the `owed` read in `App.tsx` before changing them.

- Build: the Reading… and Can't read states of 6(a); `fmtAge` floors at "just now"; owed ages from the read
  time with the hub's time from `khipu owed --json`; Muse with no figures never shows "Live".
- Tests: each switch renders neither checked nor unchecked before its read, and the real value after; a failed
  read shows Can't read in place; folders and lanes say Reading… before the record; a timestamp 40 s in the
  future reads "just now"; an owed age uses the hub time; the Muse fixture with no turn shows no Live tag
  (update `PlansCard.test.tsx:85`).

### Phase 2: Plans (S–M, one session)

Read this page and `plans.html` first. Inspect `PlansCard.tsx`, its tests, `usage.py` `_note()` and the routing
record's holds (`routing.ts`) before writing.

- Build: the one-phrase Home row and the head line; the Settings account blocks, still in Capture & models for
  now; paused from the routing record; the plain notes for every code in `usage.py`; Sign in only for
  `not_signed_in` and `no_plan_limits`.
- Tests: each row of the table in section 2 renders exactly one status phrase; a paused account shows the pause
  first; a code with no note fails the test; no row outside an account block in Settings.
- Stop-condition: if the routing record can't be read on Home, Plans shows no pause rather than guessing.

### Phase 3: Routing overview (M, one session)

Read this page and `routing.html`, `routing-states.html` first. Inspect `RoutingSettings.tsx` top to bottom,
`ui.tsx` `Disclosure`, `doctorAttention.ts` (`routingAttention`) and `RoutingCard.tsx` before restructuring.

- Build: the groups with closed-line summaries and problem tags; the Needs you list from the same problem list
  `routingAttention` uses; Open all and Close all; open state remembered; deep links to a group; Job runners and
  lanes joined; Telling's two inner folds; the Plans blocks and the accounts field move into Accounts (out of
  Capture & models).
- Tests: every problem kind in rule 1 shows on a closed group's line and in Needs you; a deep link opens its
  group; nothing that was on the page before is missing (a test lists every control by its label before and
  after); with Accounts open, 5 accounts and 5 problems the page is under 1,400 px tall at 1280 wide, and with
  every group closed it fits one screen.
- Stop-condition: if a problem has no single source the list can read, stop and ask rather than counting it
  twice.

### Phase 4: sort, filter and search (S–M, one session)

Read this page and `routing-open.html` first. Inspect what phase 3 built.

- Build: account sort and filter chips with counts; rule filters and search over 8; Recent changes by who and
  kind with Show earlier days; lane search over 8; all remembered on this Mac.
- Tests: Your priority sort follows the priority list and never changes it; a filter that hides a problem still
  counts it; Recent changes shows today first and loads earlier days on request.

### Phase 5: standing rules (S, one session; touches `packages/cli`)

Read this page and the rules part of `routing-open.html` first. Inspect `routing_sources.py` (`watched`,
`rule_checks`) and the route JSON (`routing.py:4600` on).

- Build: the check's time and covered rules in the route JSON; the result line per rule; the grey line for
  unchecked rules; the group help line; the filter.
- Tests: "nothing wrong found" only after a check that ran; with no job runner folder the line says it can't
  check yet; a broken check shows on the rule's row and on Home.

### Phase 6: Capture & models (M, one session)

Read this page and `capture-models.html` first. Inspect `App.tsx` (the `capture` section), `SettingsControls.tsx`,
`SynthProvider.tsx` and `modelVerify.tsx` before moving anything.

- Build: the three cards and Keys on this Mac; provider choices as rows with status; key checks on open for a
  never-checked key and from Check now; commitment settings moved; the Plans card already gone (phase 3).
- Tests: every setting saves exactly as before (same CLI calls); each provider shows only what it needs; an
  environment key shows as the one used; no sentence about the Keychain appears twice.

### Phase 7: setup's Accounts step (S–M, one session)

Read this page and `setup-accounts.html` first. Inspect `Welcome.tsx` (`STEPS`, `initialStep`), `PlansCard.tsx`
(`UsageAccountsField`) and the priority writer in `RoutingSettings.tsx`.

- Build: the step, its place in the rail and the Welcome list; the plan read when the step opens; priority from
  the list only if changed; Run setup again at this step.
- Tests: Skip for now writes nothing; Next with the list unchanged writes no priority; a not signed in account
  shows its sign-in words; the rail shows seven steps.

### Phase 8: what's new (S, one session)

Read this page and `whats-new.html` first. Inspect `postUpdateNotices.ts`, `PostUpdateNoticeDialog.tsx` and the
Updates section in `App.tsx`.

- Build: items per version; every item since the last shown version, at most five; the Home card with its
  own closed state, stored apart from the dialog's; Show me to any section,
  group or setup step; Set up accounts when routing is not set up; the What's new list in Settings → Updates;
  this release's items (Routing, Priority and pace, the clearer Settings, the Accounts step).
- Tests: skipping three versions lists all their items; a fresh install shows none and finishing setup records
  the version; Got it never brings the dialog back and leaves the card; closing the card leaves no dialog;
  each Show me lands on its place.

## Decisions (Matt, 2026-10-15)

1. The Settings section "Routing" is renamed "Accounts and routing".
2. A rule Khipu checks is labelled "Checked by Khipu", not "Khipu checks this".
3. Khipu keeps reading a paused account's plan.
4. What's new is both a one-time dialog on the first launch after an update and a closable card on Home, so
   closing one by accident still leaves the other; the full history stays in Settings → Updates.

## Review (2026-10-15, before approval)

Orchestrator review, fixed in the mocks: Claude main's long state overlapped its week figure in the folded
Accounts list (now a grid that wraps); every readable account shows its spend pace, and "Spend pace, fastest
first" is a sort choice; the routing table section stays because the app still reads the file, and says it is
an old, read-only file.

Better Design rules, every critical and serious finding fixed in the mocks:

- rule/no-color-only-meaning: every dot sits next to words (account states, tags, rule results, job runners).
- rule/icon-button-needs-name, rule/form-input-needs-label: the sort is a labelled "Sort by" menu; each filter
  chip group has a label ("Show accounts", "Show rules", "Changes by", "Changes to") and each counted chip a
  name with its count ("Paused, 1 account"); Open all groups and Close all groups say what they do.
- rule/no-click-on-non-interactive: every fold is a button with `aria-expanded` and `aria-controls`, not a
  `details` summary; the mocks' small `folds.js` makes them work by mouse and keyboard.
- rule/focus-needs-visible-indicator: fold buttons get the app's focus ring (inset, so it is not clipped).
- rule/touch-target-min-44px: group and account fold rows are at least 44 px tall; filter chips and the sort
  menu are 32 px. The app's 26 px small buttons stay, as a desktop pointer size.
- rule/no-skipped-heading-levels: Plans account headings (h4), the standing rules and day headings and the
  Updates version headings (h3) sit directly under the page's h1 and are now h2.
- Content: no em-dashes; counts are pluralised by number ("1 rule", "2 rules"); empty states name the next
  action; no line only repeats its label.

Terms replaced for someone outside the team: "NOW block" became "current-state section"; "Routing table file"
became "Old routing table file"; "No window with a reset time ahead" became "Its plan shows no limit that
resets"; "Khipu checks this" became "Checked by Khipu"; "Routing" became "Accounts and routing".
