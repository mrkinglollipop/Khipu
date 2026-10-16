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
| 8 | What's new after an update (dialog and Home card), and its history in Settings → Updates | S | open |
| 9 | Finding and adding accounts in the app (addendum, see "Phase 9" at the end) | M–L | approved by Matt 2026-10-15 |
| 9A | Discovery engine and CLI (`packages/cli`) | M | built, in review (branch feat/accounts-9a) |

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

## Phase 9: finding and adding accounts (addendum, APPROVED by Matt 2026-10-15)

Asked by Matt 2026-10-15: "we need to verify that users can add accounts for Khipu to track, because right now
I don't know how that would be", then "There should be a feature that automatically finds them", then
"thoroughness from cradle to grave". Phases 1 to 8 above are unchanged; where phase 9 changes something they
built, it says so here and the change is built in a phase 9 slice. It builds on phase 3 (Accounts and routing
→ Accounts), phase 7 (setup's Accounts step, branch `feat/clarity-setup`) and phase 8 (What's new, branch
`feat/clarity-whatsnew`).

Mocks: `add-account.html` (the Add an account dialog and its states), `add-account-found.html` (Home's Found
card, Found rows, Not offered again, automatic tracking, the setup step), `add-account-manage.html` (rename,
Stop tracking, two accounts on one login), `add-account-states.html` (the lifecycle states added after the
review: can't run the CLI, nothing installed, sign-in codes, couldn't check, folder moved or missing, signed
out, the last account, rename with references).

### Phase 9 decisions

Matt, 2026-10-15, approving the mocks:

1. **Grok Build and Muse sign-in codes are shown in the app.** Khipu starts their device-code sign-in and reads
   only the line that carries the link and code from the CLI's output, with a strict pattern; that line is
   shown in the dialog and never stored, cached or logged, and nothing else the CLI prints is read.
2. **New login folders go next to the default:** `~/.claude-<name>`, `~/.codex-<name>`, `~/.grok-<name>`
   (Muse has one login, so no folder).
3. **The "Found an account" notice is a dismissible Home card**, never a pop-up, dialog or badge.

Taken in this scope (conventional defaults, reversible; say if any should change):

4. Everything phase 9 adds is **per Mac**; nothing syncs through the hub (see Two Macs).
5. A new account is **not placed in the priority list**; it shows "not ranked" until the owner places it.
6. **Default logins stay home-less.** A folder equal to the CLI's default login (by real identity) is always
   stored with no `home`, so its key stays `claude|`, `codex|`, `grok|`, and writing the default list out never
   changes a key.
7. **The account key never changes on rename.** The key is the CLI and the login folder; the name is a label.
   The lane map moves from labels to keys (see Rename) so a rename breaks nothing structured.
8. **A failed check is never "not signed in".** It is "couldn't check" with its reason, never offered, never
   turned down, retried later.
9. **Agents get a public projection, never raw rows.** `khipu_usage`, `khipu_route` and any listing of accounts
   go through one projection that keeps the name, account id, plan, windows, state and a plain note, and drops
   `home` and every path (Agents' view).
10. **The CLI and its Node folder can be set in the app.** Two new per-Mac settings, `cli_path.<cli>` and
    `node_folder`, read by `synth_cli.binary` after `KHIPU_<NAME>_CLI` and added to the probe PATH by
    `usage._probe_path`, so a CLI the app can't start is repaired without a terminal (Section 1).
11. **Moving a login folder moves everything keyed to it,** reports and evidence included, and leaves a
    forwarding entry so nothing is counted twice (Section 7).


Matt, 2026-10-15 (about 16:55 ET), answering open questions 1 to 8 with the recommendations:

12. **Quiet for what already existed.** After the update, and for a login left unticked in setup, found folders
    show only in Settings (no Home card) until a conversation uses them.
13. **Rename keeps free text.** Rules, the note and agents' scripts keep the old name; Khipu does not rewrite
    them and keeps no hidden alias; the rename dialog lists each rule and the note that mention the old name
    with an Edit link.
14. **Stopping the last account** gives an explicit "Read no accounts" state after a confirmation, never a
    fallback to every installed CLI.
15. **The account list has an owner gate.** `khipu config --set usage_accounts` gets the same typed
    confirmation (or `--at-owner-request`) as priority and rules; no MCP tool adds, renames or removes an
    account.
16. **Stop tracking defaults:** remove from priority, end its pause and remove it from lanes are on by default;
    holds are listed and kept unless ticked.
17. **The same person in two folders** can't be detected without reading credentials; the dialog says so and
    shows no guessed hint from matching plan figures.
18. **Accounts are per Mac.** Syncing later only with an identity that doesn't depend on a path.
19. **Live proof is read-only;** one live Track on this Mac only on Matt's go, undone at once.

Matt, 2026-10-15, after the 9A guardrail run (Rules for phase 9 and Guardrails):

20. **A status check that writes waits for the owner on folders found only by name.** The guardrail run found
    that both status commands write into an empty login folder:

    | CLI (version) | Check | Answer in an empty folder | What it wrote there |
    |---|---|---|---|
    | Claude Code 2.1.296 | `claude auth status --json` | exit 1, `"loggedIn": false` | `.claude.json` and `backups/.claude.json.backup.<time>` |
    | Codex 0.162.1 | `codex login status` | exit 1, "Not logged in" | `tmp/arg0/codex-arg0<random>/` with `.lock`, `apply_patch`, `applypatch`, `codex-execve-wrapper` |

    Both ran with HOME and the login folder set to scratch folders; the default logins' own folders were not
    changed. Grok's plan read was not run; Grok is treated as writing too until a run shows otherwise. So
    discovery checks a Claude, Codex or Grok folder only when a conversation ran on it, T3 names it, it is the
    CLI's default login, or the owner presses **Check**. A folder found only by its name shows as **Found, not
    checked** with Check and Not this one; pressing Check is the owner's consent to that write, and the button's
    help line says so: "Check asks Codex whether this login is signed in. Codex may create a settings file in
    that folder when it is asked." Check runs the same one-shot check (`khipu usage --discover --check
    <folder>`), which ignores the daily wait. A not-checked folder is never offered on Home, is not counted in
    the 12-folder cap, and is never tracked automatically.

Review history: a twelve-stage review (`dd33e77`) and an independent Codex review of it (11 findings, all
confirmed against the code on 2026-10-15) are folded into the text below.

### Today

The only way to add an account is the "Accounts read" form (`PlansCard.tsx`, `UsageAccountsField`, about line
1010): Name, CLI and a free-text "Login folder". It works only for someone who already made a second login in
a terminal and knows its path. With no list saved, Khipu reads every installed CLI's default login
(`usage.configured_accounts`), computed from the PATH of whoever asks. The CLI refuses a second entry for the
same login (`login_taken`) or name (`name_taken`), but `login_taken` compares `usage._key` strings, not real
folders, so a symlink, a trailing slash or `~` against a full path slip through. The app can start a sign-in
again only for an account already in the list (`khipu_account_sign_in`, `lib.rs:1471`; `sign_in_argv` refuses
any other folder; `usage.sign_in` resolves through `configured_accounts`); it is one blocking call that
returns when the login ends and reads none of its output. Routing keys holds, pauses, clears and priority by
`harness|home`, but `routing_lane_map` targets and the owner's rules and note name accounts by label. An
account removed from the list stays in the routing record while any owner record names it, marked "isn't in
Settings → Models". Saving an empty list unsets it, which brings back every installed CLI.
`khipu config --set usage_accounts` has no owner gate.

### What each CLI supports (checked on this Mac 2026-10-15)

| CLI (version here) | More than one login | How the login folder is chosen | Signed-in check with no turn | Sign-in from the app |
|---|---|---|---|---|
| Claude Code 2.1.296 | Yes, one per folder | `CLAUDE_CONFIG_DIR`, default `~/.claude` (docs: code.claude.com/docs/en/settings, "To keep the home-directory files somewhere else, set CLAUDE_CONFIG_DIR"; not in `--help`) | `claude auth status --json` (`claude auth status --help`) | Yes: `claude auth login --claudeai` opens the browser (`claude auth login --help`; Khipu already runs it) |
| Codex 0.162.1 | Yes, one per folder | `CODEX_HOME`, default `~/.codex` (docs: learn.chatgpt.com config-advanced, "Codex stores its local state under CODEX_HOME"; `codex --help` names `$CODEX_HOME`) | `codex login status` (`codex login --help`) | Yes: `codex login` opens the browser and returns to a local callback (docs: learn.chatgpt.com auth) |
| Grok Build 1.0.52 | Yes, by folder | `GROK_HOME`, default `~/.grok` (not in `--help`; `grok du` calls `~/.grok` "the grok home"; Khipu's plan reads already use it, `usage.py:63`) | No status command; the plan read (`probe_grok`, which passes `--no-leader`) answers "not signed in" | By code (decision 1): `grok login --device-auth`, once proven not to touch the default login or the leader socket (guardrail) |
| Muse 1.4.6 | No: one login per Mac | None in `muse --help`; Khipu treats Muse as one sign-in (`usage.py:818`) | The plan read (`usage/read`) | By code (decision 1): `muse login` ("approve a code in your browser"); `META_API_KEY` overrides it. Re-signs the one Muse login only |

No Grok or Muse docs beyond `--help` were checked. A device-code sign-in prints a link and a code; per decision
1 Khipu reads only that line (see Signing in).

### Rules for phase 9 (in addition to rules 1 to 7)

- **Rule 6 is amended for phase 9:** besides what's new after an update, the app may show the Found card and
  the automatic-tracking card on Home. A find is not a rule-1 problem: no rail or Settings tab badge and no
  Needs you line; "2 found" is a neutral tag on the Accounts group's closed line only.
- **Never read inside a login folder.** Discovery, `--try` and sign-in list names in the home folder, stat a
  folder itself, and ask the CLI. Nothing they run opens, lists or reads a file inside a login folder. Two reads
  inside login folders exist today and are named, not hidden: the capture hook reads the transcripts it saves
  (its job), and Codex's plan read falls back to the last `rate_limits` in its newest session files
  (`usage.py:691`, `codex_rollout`, which scans `<CODEX_HOME>/sessions` and reads the tail of up to five
  files). That fallback is switched off for discovery and `--try` (fix now, 9A); whether tracked accounts keep
  it is open question 9.
  Khipu's own capture folder (`~/.grok/khipu` by default, `session_capture.khipu_home`) is Khipu's data, not the
  Grok login's: discovery reads its session state files (source 2) and nothing else under `~/.grok` (tested
  with the default capture home, 9A).
- **Never prompt.** No discovery or check can start a login or any command that waits for a person (see
  Discovery, "How a prompt is made impossible").
- **Login-folder paths stay with the owner.** They show in the app; they are never returned by `khipu_get`,
  `khipu_search`, `khipu_status` or `khipu_route`, and default names derived from a folder pass the same
  redaction as `resolve_account` (`credential_location`, `redact_secrets(force=True)`); a folder that fails
  gets no default name until the owner names it.

### The life of an account

#### 1. A new user: setup's Accounts step

Setup's step (phase 7, `SetupAccounts.tsx` on `feat/clarity-setup`) lists what discovery found when the step
opens: each CLI's default login and every other signed-in folder, as in `add-account-found.html`.

- A ticked login plus Next is the owner's answer and counts as Track (the "never add silently" test covers
  automatic discovery only). Signed-in logins start ticked; a login that is not signed in or couldn't be
  checked starts unticked. A login left unticked is neither tracked nor turned down: it stays a Found row in
  Settings and never reaches Home (decision 12 covers this quiet rule).
- **Nothing installed:** the callout keeps Check again and gains Add another account… The dialog's step 1 then
  lists all four CLIs as not installed with Next off.
- **Installed, but Khipu can't run it:** a third state beside installed and not installed, decided by running
  `<cli> --version` with `probe_environment`'s environment and PATH (the same call that supplies the version).
  It shows where Khipu found the CLI and the in-app fixes, never a terminal step (decision 10):
  - **Set the Node folder…** when the CLI is a Node script whose `node` Khipu can't find (the usual cause under
    launchd's bare PATH): a folder picker that opens at the version manager's folder and accepts only a folder
    holding an executable `node`; saved as `node_folder` and added to the probe PATH.
  - **Choose the CLI…** when Khipu found the wrong file or none: a file picker that accepts only an executable
    file; saved as `cli_path.<cli>`.
  - **Check again** re-runs `<cli> --version` with the new settings and says what changed.
  Both settings show in Settings → Accounts and routing → Accounts under the CLI's name, with Clear. A CLI
  whose own install is broken (its files are missing) is the one case Khipu can't repair: it says "Codex's
  install looks incomplete; reinstall it from its maker" and links the maker's page when one is verified (open
  question 10). Never "not signed in", never a generic read failure.
- **CLIs under a Node version manager.** The app starts under launchd's bare PATH, so finding the CLI depends
  on `synth_cli.binary`'s list. The build adds the install places it can verify that the list misses: fnm's
  `~/Library/Application Support/fnm/node-versions/*/installation/bin` and `~/.fnm/node-versions/*/installation/bin`,
  and Claude Code's `~/.claude/local/claude`. nvm resolves to the newest Node folder holding the CLI, not
  `nvm alias default`; the scope accepts that, and Set the Node folder… overrides it. For any install still
  missed, step 1 offers **Choose the CLI…** (above). Today only the `KHIPU_<NAME>_CLI` environment variable
  can name a CLI (`synth_cli.py:137`), which the app can't set for itself; the two settings close that.
- **Download links:** the app names the CLI and links only to a download page verified at build time and
  checked into the code (Claude Code and Codex); for Grok Build and Muse it names the CLI and shows no link
  until a page is verified.
- **Check again** re-runs the installed check (`synth_cli.binary` through `khipu usage`) and a discovery run.
- **Signed-out default login:** a tracked Claude or Codex row that is not signed in gets **Sign in now**
  (`khipu_account_sign_in`, which accepts default-list accounts) with the dialog's waiting and Stop states;
  Grok Build and Muse rows get the code display of decision 1.
- **Writing the list:** the first save that changes the list writes the default accounts out (Tracking, below).
  From then on a CLI installed later is not tracked by itself; it reaches the owner through discovery, which
  also offers any installed CLI whose default login is neither tracked nor turned down (Muse included, which
  has no folder for the name search to see).
- **Run setup again** on a configured Mac shows the automatic-tracking switch at its saved value and writes it
  only if changed; Found rows list only undecided folders; Skip for now and Back never track or turn anything
  down; nothing saved earlier is reset.

#### 2. An existing user updating

- Accounts entered by hand keep their names and folders: the update writes nothing to `usage_accounts`.
- **No save drops an entry.** Entries that are invalid today (relative folder, unknown CLI, Muse with a
  folder) stay in the stored list, shown with their reason and Fix… or Stop tracking…; the build lets
  `parse_usage_accounts` keep them as they are rather than refuse the whole list. Test: a hand-edited relative
  folder survives an Add.
- **The first discovery run after the update** is quiet for a curated list: folders that already existed show
  only as Found rows in Settings; a folder reaches Home only after a conversation uses it after the update
  (decision 12). Conversation counts start at the update; sessions saved before it count only where their
  `transcript_path` maps to a folder (below), otherwise "not counted yet", never 0.
- Priority, holds, pauses, clears, rule checks and route reports made before the update keep resolving,
  because no key changes (decision 6). Test: after the first Add, everything made on `claude|` still names
  the same account.
- **What's new:** one mention in the release that ships phase 9. If that is the release now numbered 0.4.16
  on `feat/clarity-whatsnew` (`postUpdateNotices.ts:137`), it is one sentence added to the "Accounts and
  routing" item ("It also finds your other logins and lets you add one."), no new item; if later, one item in
  that release with Show me to Accounts and routing → Accounts.

#### 3. Discovery

**Sources, exactly:**

1. **Folder names in the home folder:** direct children of `$HOME` only (`os.scandir`, never recursing) whose
   name matches `^\.(claude|codex|grok)(-[A-Za-z0-9._-]+)?$`, minus names ending in `-backup`, `-backups`,
   `-logs` or starting `.bak` after the CLI word. Directories only (a symlink is resolved, then
   de-duplicated). Permission errors are skipped silently. Muse is never found by folder.
2. **The login folder each conversation ran under.** New: the capture hook records the folder path only (the
   value of `CLAUDE_CONFIG_DIR`, `CODEX_HOME` or `GROK_HOME` in the hook's own environment; no other value) in
   the session's state next to `transcript_path` (`session_capture.py:1868`). For sessions without it: a Claude
   transcript's folder is the parent of its `projects` folder (new code; `claude_homes.transcript_home` only
   matches known homes); a Codex transcript's is the folder above `sessions/`; Grok's is the folder above
   `sessions/`. A recorded path counts only if it exists and is a directory; outside `$HOME` it shows as its
   full path. Old state files without the field load fine and count as unknown.
3. **T3's settings:** `providerInstances.*` entries whose `driver` is the Claude or Codex driver, reading only
   `driver`, `displayName` and `config.homePath` through the same key allowlist as `t3.claude_instances`.
4. **Installed CLIs whose default login is neither tracked nor turned down** (this covers Muse and `~/.grok`).

Khipu's own process environment is not a source, so the app and the CLI give the same answer. Every candidate
is de-duplicated by real identity (`claude_homes.path_identity`: the inode when it exists, the resolved path
otherwise) against tracked accounts, turned-down folders and the default logins; a home-less entry stands for
its CLI's default folder.

**The check:** a new one-shot runner with a frozen table, `STATUS_ARGV` (Claude `auth status --json`, Codex
`login status`), separate from `SIGN_IN_ARGS` and the device-code argv. Grok's check is `probe_grok`'s plan read
(it passes `--no-leader`). Muse is checked only as its one default login, by its plan read. The runner keeps
the exit code and a fixed signed-in, not-signed-in or couldn't-check result; everything the CLI prints is
dropped, as `_classify` does. A signed-in Claude or Codex folder then gets one plan read for its plan name. A folder found only by
its name is not checked until the owner presses Check (decision 20). No
check uses a turn or a paid call; checks reach the network the same way today's plan reads do.

**How a prompt is made impossible** (the class behind a past `muse login` started by accident while listing
folders): discovery builds an argv only by lookup in `STATUS_ARGV` or the existing probe argv, never from text;
every child gets `stdin=DEVNULL`, `start_new_session=True` (no controlling terminal), the `probe_environment`
allowlist, a fresh temp cwd and a kill of its whole process group at its deadline. Tests assert that every
discovery argv equals one exact allowed tuple (`(claude, "auth", "status", "--json")`, `(codex, "login",
"status")`, `probe_grok`'s and `probe_muse`'s fixed argv), so `codex login status` passes and a bare
`codex login`, `muse login` or any added argument fails, and a fake CLI that opens `/dev/tty` or reads stdin is
killed and classified, never left waiting.

**When it runs and what it costs:** at app start, every 6 hours while the app is open, when Accounts and routing
or setup's step opens, and from Look for accounts now. Never while a sign-in is running. One run at a time
across processes (its own lock, `usage.discover.lock`, `flock LOCK_NB`; a second trigger gets the last result
marked "Looking…"); discovery and the plan refresh do not wait on each other. At most 12 folders a run, at most
2 children at a time, 5 seconds per child, under an overall deadline as `probe_all` has (timeout plus
`OVERALL_GRACE_S`, `kill_all`). Order: folders used by conversations first, then newest first seen, then least recently
checked; the rest wait for the next run, and the run says how many were left. A folder already tracked or
turned down is never checked. An undecided folder that was signed in at its last check is not checked again for
24 hours (its Found row keeps that result and its time); one that is not signed in or couldn't be checked is
checked again at most once a day; Look for accounts now ignores both waits. So every candidate is checked
within ceil(candidates / 12) runs, and no group of twelve can take every run. Test: with 30 undecided signed-in
folders, every folder is checked within three runs, and a folder first seen after them is checked by the run
after it appears. Discovery never writes the usage cache for an unsaved folder.

**What it keeps** (per Mac, in Khipu's own data, never the routing record): each candidate's CLI, path, path
identity, first seen, last checked, last result and conversations in the last 7 days (counted from
`seen_ts`, capped); and the last run (time, folders checked, how many could not be checked, timed out or were
left for later). Accounts and routing shows it beside Look for accounts now: "Last looked 3:40 PM; 1 folder
could not be checked". Doctor and the health report do not show discovery: a find is never a health problem,
and a failed check is already on its Found row.

#### 4. Showing what was found

- **Home:** one Found card above the attention callouts, below the What's new card when both show (order: What's
  new, Found or automatic-tracking card, attention, health line). One card, listing every find a conversation
  used, each with Track… and Not this one, and a **Close** control. Close hides the card on this Mac until a
  different folder is found; it turns nothing down, and the Found rows in Settings stay.
- **Accounts and routing → Accounts:** a Found row per undecided signed-in folder, a "Found, not checked"
  row with Check and Not this one per folder found only by its name (decision 20), "N found" as a neutral tag on
  the closed line, and while a run is going "Looking…", never "0 found". A folder that couldn't be checked
  shows "Couldn't check ~/.claude-x: <plain reason>" with Check again; it is never offered.
- **Not this one** and Stop tracking record the folder in a per-Mac list (CLI, path, path identity, why: "you
  chose Not this one" or "you stopped tracking it", when), kept apart from `usage_accounts` so a config edit
  never clears it, matched by path identity. **Offer again** deletes the entry. An entry whose folder is gone
  shows as gone.
- **Repeat cases:**

| Case | Home | Found rows |
|---|---|---|
| Same folder seen again, still undecided | Only if the card was not closed since it was first shown | Yes |
| Undecided folder signs out | Removed from the card | Moves to "Not signed in" in the dialog's list only |
| Folder renamed on disk | A new find (new path); the old one is pruned when gone | Yes, as a new find, or "moved?" when a tracked folder disappeared (Living, below) |
| After Stop tracking | Never (it is in Not offered again) | Never, until Offer again |
| After Not this one | Never | Never, until Offer again |

- **Track new accounts automatically**, a per-Mac setting, off by default. When on, a signed-in find that a
  conversation used is tracked through the same path as Track (same checks, unique default name, first read,
  default list written out), and Home says once "Khipu started tracking Claude client" with Undo, Rename… and
  Close. Undo is Stop tracking plus Not offered again, so it is not tracked again on the next run. After Close,
  Stop tracking… in Accounts and routing undoes it. A folder no conversation used still waits for the owner.

#### 5. Tracking

**Naming rules,** enforced in `parse_usage_accounts` with fixed codes the app words: 1 to 64 characters, one line,
no `|`; unique ignoring case and surrounding space; may not equal a CLI's bare name (claude, codex, grok, muse)
or another account's id, except the CLI's own default login, which keeps its plain name ("Claude"), because
conversations' bare CLI word resolves to it. Default names: a folder with a suffix gives "<CLI> <suffix>"
(`~/.claude-client` → "Claude client"), a default folder gives "<CLI>", a picked folder elsewhere gives "<CLI>
<folder name>"; a taken name gets " 2", " 3".

**Saving** (Track…, the dialog's Save, automatic tracking and setup's Next all use one path):

1. If no list is saved, write the default accounts out exactly as the Plans card showed them when the dialog
   opened (`accounts_status().accounts`, not a fresh PATH lookup): labels unchanged, no `home`.
2. Add the new account with one canonical home: home-less when it is the CLI's default folder, else `~/...`
   when under the home folder, else the absolute path, symlinks and `..` resolved first.
3. `parse_usage_accounts` refuses a second entry for the same real folder (path identity, not string) and a
   taken name. Steps 1 to 3 are one config write.
4. Write the dialog's step-3 read as the account's first cache row (or start one read at once), so Plans,
   `khipu_usage` and `khipu_route` have figures straight away. Until then agents see it as unknown, never open.
5. The routing record gets a new version with the change line "Claude client: newly tracked, not read yet".

**Priority:** a new account is not ranked (decision 5); the priority editor and setup's Move up and Move down
place it.

**Who may do it:** see decision 15 and Agents, below.

#### 6. Signing in a new account

**Folder creation** (decision 2): the slug comes from the name: lowercase, a leading CLI word dropped ("Claude
client" → "client"), only `[a-z0-9-]`, repeated dashes collapsed, 1 to 32 characters, no leading or trailing
dash; refused when empty, "default", or a name discovery skips (backup, backups, logs, bak...). Khipu creates it
with an exclusive `os.mkdir(path, 0o700)`, never `makedirs`. "Taken" means anything at that path (folder, file,
symlink); then `-2`, `-3` are tried. Khipu records each folder it made in its own state. A folder chosen with
Choose another folder… is never created or changed.

**A new command, not a loosened one.** 9D adds a new-folder sign-in (`usage --sign-in-new --harness H --folder
F`): it accepts only a folder Khipu itself created for this attempt, under the home folder, matching
`~/.<cli>-<slug>`. Rust builds the argv from the CLI enum and the folder Khipu returned at creation, never from
webview text. The existing Sign in again path and its refusal tests stay as they are.

**Start, status, cancel.** Sign-in becomes three calls: start returns at once with an attempt id; status reads
it; cancel ends it. One sign-in at a time, owned atomically: start takes an exclusive lock
(`usage.sign_in.lock`, `flock LOCK_NB`) and writes the attempt record with a temp file and rename, so two
starts can never both own an attempt. The record holds the attempt id, folder, pid, process group, the
process's own start time and its argv as the system reports them (`ps -o lstart=,command= -p <pid>` or
`proc_pidinfo`), and Khipu's own start time. **Before any kill** (cancel, Start again, the reaper), Khipu reads
the live process's start time and argv again and kills the group only if both match the record and the argv is
one of the fixed sign-in tuples; otherwise the record is stale (the process ended and its number may now
belong to something else), and Khipu deletes the record without killing anything. The reaper runs on the next
start and on quit; until then the login may still finish and write into the folder, which the next check then
sees. "Open the sign-in page again" is replaced by **Start again** (kill, then start), since
nothing reads a Claude or Codex login's output.

**Claude and Codex:** the browser opens; the dialog waits up to 15 minutes ("Waiting for you to finish signing in
in your browser"), with Stop and cancel and Start again.

**Grok Build and Muse** (decision 1): stdout and stderr are piped and read line by line with a per-line and a
total byte cap; one anchored pattern per CLI matches an https link on a fixed host list plus a code of a fixed
shape; only the matched link and code are held in memory and passed to the dialog, never to logs, cache or
state; the rest is drained unread. No match within 20 seconds: "Grok Build didn't show a sign-in code" with
Start again. Muse's sign-in only re-signs its one login.

**After it ends:** success → step 3 (name and read). Timeout, cancel or failure → nothing is saved; a folder Khipu
made that never signed in is kept and reused on Start again or on a new attempt with the same name (no `-2`);
it is removed only if Khipu made it and it is still empty when the owner chooses Cancel. "Use a login I already
have" shows a leftover as "Not signed in". Khipu never deletes a folder that has signed in.

**The same account twice.** Khipu detects only the same folder (path identity). It cannot tell that one person
is signed in to two folders without reading credentials; decision 17.

**Codex in the Keychain:** before Sign in to another account ships for Codex, 9D proves with two scratch folders
that a second `CODEX_HOME` keeps its own sign-in; if not, Codex gets only Use a login I already have.

#### 7. Living with an account

- **Signed out later** (section 2's table gains a row): "Signed out, earlier figures kept": the meter dimmed,
  the percent, "Signed out; figures from 2 days ago" with Sign in again (Claude, Codex) or the code sign-in
  (Grok Build, Muse). The not-signed-in phrase wins over "Can't read it now". Needs you (section 3) lists
  "Claude second: signed out". A signed-out account stays tracked and keeps its priority place.
- **Signed in again as someone else:** after any Sign in again, Khipu compares the plan before and after; if it
  changed it says "Signed in again; its plan is now Pro" so the owner can rename it or adjust priority.
  The refusal at `lib.rs:1389`, which names "Settings → Capture & models → Plans", names Settings → Accounts and routing
  → Accounts instead.
- **Login folder not found:** checked before every read by a directory test on the folder itself (nothing
  inside it read). The row and Plans block say "Khipu can't find the login folder ~/.claude-x" instead of "Not
  signed in", the read is skipped, and the choices are Choose the folder…, Sign in here again (Claude and
  Codex; recreates the folder, after asking) and Stop tracking…. Test: a missing folder never yields the
  sign-in note.
- **Folder moved or renamed:** when a tracked account's folder is gone and discovery finds an untracked folder
  for the same CLI, its Found row asks "Is this Claude work, moved?". Yes re-keys the account to the new folder
  in one owner write that carries everything keyed to the old key: pause, clear, holds, priority entry,
  lane-map entries, and also the conversation reports and limit evidence (`routing.py` filters evidence by
  `_evidence_account(...) == key`, about line 3466, and folds reports per key). The record keeps a forwarding
  entry `old key → new key`, so reports and evidence written later under the old key (a conversation still
  running from the old path) fold into the new account instead of making a second one; Recent changes keeps
  its old lines and adds "Claude work's login folder moved to ~/.claude-office". The usage cache row moves too.
  No tracks it as new and says the old account's records stay until removed. Test: move an account that is at
  its limit with a pause and a report; after the move it is still at its limit and paused, one account shows
  in the view and the push, and a report under the old key lands on the new account.
- **Paused accounts** keep having their plans read (phase 2 decision).
- **Rename everywhere:** structured places (Plans, account rows, a hold's scope label, the priority list, the
  routing block, `khipu_route`) show the new name from the next fold. `routing_lane_map` stores the account
  key from 9B on, so lanes keep working. The one-time conversion of existing label values does not use today's
  first-match resolver (`routing.resolve_account`, `routing.py:879`, returns the first account whose label
  matches, and a hand-edited list can hold two accounts with the same label): a label that matches exactly one
  account is converted; a label that matches two or more, or none, is left as it is and shown in Job runners and
  lanes as "Lane review-2 names Claude, which two accounts are called; choose one" with a chooser, and counts as
  a rule-1 problem until chosen. Test: two accounts labelled "Same", a lane naming "Same": nothing converted,
  the repair shows, evidence for that lane attaches to neither until chosen; the rename dialog says how many lanes point at it. Free text (standing rules, the note, hold
  reasons, conversation reports, past Recent changes lines) keeps the old name and is not rewritten; the
  rename dialog lists the rules and note that mention the old name with a link to edit each (decision 13).
  The rename is one Recent changes line and the push's change line "Claude client is now called Claude work".
  Renaming an account on the default list first writes the list out (Tracking, step 1).

#### 8. Two Macs

Everything phase 9 adds is per Mac (decision 4): tracked accounts (`usage_accounts` in the Mac's own
`config.json`), plan reads (`state/usage.json`), the routing record (holds, pauses, priority, the lane map),
Not offered again, the automatic-tracking switch and Home's Found card. A join kit carries no accounts
(`join.py` `export_kit`), so a joined Mac's setup step and discovery start from its own folders with an empty
Not offered again list. The same login signed in on two Macs is two accounts in Khipu, each with its own
routing state; names are unique only within a Mac. The Add and Stop tracking dialogs say "on this Mac". An
account whose folder is missing on this Mac shows "Login folder not found on this Mac" (Living). Syncing is a
separate future decision that needs an identity that does not depend on a path (decision 18).

#### 9. Stop tracking and after

The Stop tracking dialog (`add-account-states.html`) lists, for this account:

- **Removed:** the account from `usage_accounts`, Plans and Accounts and routing; the usage cache row for its key.
- **Priority:** "Also remove it from your priority list" (on by default; an owner priority write shown in Recent
  changes). Left off, the entry stays marked "no longer tracked".
- **Pause or clear:** "End its pause now" (on by default). Left off, it stays until its end time.
- **Holds:** each listed with its end time and "End this hold" (off by default).
- **Lanes:** each lane pointing at it, with "Remove from these lanes" (on by default).
- **Not changed:** standing rules and the note that name it (listed), the login folder and its sign-in,
  conversations that run on it (still saved), past history.
- **Not offered again** as a found account, until Offer again.

While anything is kept, agents see it in `khipu_route` as "no longer tracked" with `configured: false`;
`routing.py`'s "isn't in Settings → Models" (lines 1184, 2507, 4517) becomes "isn't tracked in Accounts and
routing". Test: stopping an account with a priority entry and a pause, with the defaults, leaves no
row for its key in the view, the push or doctor.

**Default mode:** Stop tracking while no list is saved first writes the defaults out (home-less) minus that
account.

**The last account:** saving an empty list today unsets it and brings every installed CLI back. Phase 9 adds an
explicit "Read no accounts" state stored apart from an unset list; Plans, Home and routing then say "No
accounts tracked" with Add an account…. Stop tracking the last account asks first (decision 14).

**Tracking it again:** step 3 suggests its last name; its key is unchanged, so holds and history that were kept
attach again; the priority place comes back only if the entry was kept; Recent changes says "tracked again".

#### 10. Uninstall or reset

What Khipu keeps for phase 9, and what removes it. None of these removals ever touches a login folder or a
sign-in; folders Khipu created stay.

| Kept | Where | Removed by |
|---|---|---|
| Tracked accounts | `usage_accounts` in `config.json` | Stop tracking; deleting Khipu's config |
| Not offered again | its own file in Khipu's data | Offer again; deleting Khipu's data |
| Automatic-tracking switch, Found card closed | `config.json` / Khipu's data, per Mac | the switch; deleting Khipu's data |
| Candidates, counts, last run | Khipu's data | pruned when a folder is gone; deleting Khipu's data |
| Login path per session | session state files (`session_capture`; never pruned today, `session_capture.py:119`) | the new retention below |
| Plan read cache | `state/usage.json`, keyed `harness|home` | Stop tracking (that row); deleting Khipu's data |
| Folders Khipu made | Khipu's data (the list), the folder itself on disk | never removed once signed in |

**Login paths in session state:** session state files are never pruned today, so the path would be kept
forever. 9A adds retention for the new field only: the path is cleared from a session's state 30 days after its
`seen_ts` (the count window is 7 days, so nothing that counts is lost) by the same pass that writes it, and
`khipu usage --forget-login-paths`, reached in the app as "Forget which logins conversations used" in Accounts
and routing, clears it from every state file at once. Reset and "delete Khipu's data" remove the files as
today. Tests: a path older than 30 days is cleared on the next capture; Forget clears every file and leaves
the rest of each state untouched; a state file without the field is never rewritten.

Wiping Khipu's state: `config.json` goes, so Khipu reads only the default logins again, a second account stops
being read until found and tracked again, and declined folders are offered again. `khipu integrations
uninstall` removes Khipu-owned hook entries only (`cli.py:5225`) and leaves all of this.

#### 11. The agents' view

- Tracking, renaming, stopping, Not this one, Offer again and the switch are owner actions in the app; no MCP
  tool is added for them, the same as pauses and clears. Conversations only read accounts and report limits on
  accounts Khipu knows (`routing.py:874-877`).
- `khipu usage --discover --json` and `--try` are CLI reads for the app; no MCP tool starts them.
- What agents see change: a new account appears in `khipu_route` and `khipu route --summary` as "newly tracked,
  not read yet" (unknown, never open) until its first read; a rename as "<old> is now called <new>"; a stopped
  account as "no longer tracked" while kept records name it.
- **No agent-facing output carries a login-folder path (fix now, 9A).** Today `khipu_usage`
  (`mcp_server.py:1702`) returns `usage.report()` as it is, and every row carries `home` (`usage._blank_row`
  and the cached and pending rows in `report`), so agents see login-folder paths now. The fix is one explicit
  public projection used by `khipu_usage` (and by `khipu_route` wherever it embeds plan rows): an allowlist of
  fields (name, account id, CLI, plan, windows, state, ok, error code, plain note, read times), never `home`
  or any path, applied to fresh, cached, pending, failed and invalid rows alike; notes already say "this login
  folder" rather than a path (`usage._note`). The app keeps the full rows through its own command. Tests: one
  per row kind asserting no field or nested value contains the home folder, `~/`, or the account's folder
  name.
- Whether `khipu config --set usage_accounts` gets an owner gate is decision 15.

### Adding an account by hand (the dialog)

Settings → Accounts and routing → Accounts has **Add an account…** and **Look for accounts now** at the top of
the group; setup has **Add another account…** (also in its nothing-found state). One dialog, three steps; its
footer says "on this Mac".

1. **Service:** Claude, Codex, Grok Build, Muse, each installed (with version), installed but can't run, or not
   installed (Section 1's states); Muse already tracked is shown and off.
2. **Which login:** Use a login I already have (every login discovery knows for that CLI, each signed in or not,
   couldn't check, its plan when readable, conversations this week, "Tracked as …" off; Choose another
   folder…), or Sign in to another account (Section 6; Grok Build and Muse by code; Muse only re-signs).
3. **Name and read once:** the name (rules in Section 5) and one read of the unsaved account: Live with figures;
   signed in but unreadable with the plain reason and Read again now (can still be saved); not signed in with
   Sign in now or Save without signing in; login folder not found.
   **Sign in now for an existing folder** is a defined transition, since neither command allows it today
   (`khipu_account_sign_in` needs a saved account; the new-folder command needs a folder Khipu made): Sign in
   now first saves the account (the one save path, Section 5), with the change line "Claude old: tracked, not
   signed in", then calls the existing `khipu_account_sign_in` for it, unchanged. If the owner cancels or the
   sign-in fails, the account stays tracked and shows "Not signed in" with Sign in again, and the dialog says
   so before it starts ("Khipu tracks Claude old first, then opens the sign-in page"). The existing command and
   its refusal tests are not loosened. Test through Rust (`cargo test`) and the Tauri command, not only the
   rendered props: Sign in now on an untracked folder saves then signs in; with the save refused (name taken),
   no sign-in starts. Refusals before saving: same real folder
   already tracked (with Show it), name taken (with a suggestion).

### Guardrails for the build (stop-conditions)

- **No status check may write into a folder.** Before discovery checks folders found only by name, run each
  status command and Grok's plan read against a scratch empty folder and diff its listing, and assert nothing
  under the real default login changes; record the result per CLI version in the PR. A CLI that writes is
  checked only for folders tied to conversations or picked by the owner. Result (2026-10-15): Claude Code
  2.1.296 and Codex 0.162.1 both write; decision 20 applies this.
- **Grok device code:** prove `grok login --device-auth` with a scratch `GROK_HOME` (no real sign-in: stop at
  the code line) does not touch the default login or `~/.grok/leader.sock`; otherwise Grok gets only Use a
  login I already have.
- **Codex Keychain:** as in Section 6.
- **Never read inside a login folder:** a 9A pytest runs discovery, `--try` and sign-in over a temp tree of fake
  login folders that each hold a file named like a credential, patches `open`, `os.scandir` and `os.listdir`,
  and fails on any access inside them (listing `$HOME` and a stat of the folder itself are allowed).
- **Never a real sign-in in tests;** sign-in is faked as today (`popen=`), and installed-app checks use fake
  CLIs (below).
- **Order:** 9C and 9E start only after phases 3, 7 and 8 are merged and promoted; otherwise stop and ask.
- If anything in routing is found keyed by label alone besides the lane map, stop and ask.

#### Owner proofs still to run (9D)

Until each proof below passes, its entry in `signin.PROVEN` stays False and the app does not offer Sign in to
another account for that CLI (it offers only Use a login I already have). Claude Code is the only CLI turned on.

- [ ] **Codex Keychain, with two scratch folders.** Make two empty scratch folders. Run `CODEX_HOME=<scratch A>
  codex login` and sign in to account A, then `CODEX_HOME=<scratch B> codex login` and sign in to account B.
  Then run `codex login status` under each scratch folder and under the default login. Pass: each scratch
  folder reports its own account and the default login is unchanged. Record the Codex version. On a pass, set
  `signin.PROVEN["codex"]` to True and add codex to the Rust `SIGN_IN_NEW_OFFERED` list. On a fail, Codex keeps
  only Use a login I already have.
- [ ] **Grok Build device code** (after the Grok pause lifts). With a scratch `GROK_HOME`, run `grok login
  --device-auth` and stop at the code line (no real sign-in). Confirm `~/.grok` and `~/.grok/leader.sock` are
  unchanged, using a stat of those two entries only. Record the real link host and the shape of the code, so
  `signin.CODE_HOSTS` and the code shape can be confirmed before `signin.PROVEN["grok"]` turns on.
- [ ] **Muse code sign-in.** The same shape: run its login, stop at the code line, confirm the link host and the
  shape of the code, and confirm it asks nothing on the terminal (Khipu starts it with no terminal and no
  input). Only then does `signin.PROVEN["muse"]` turn on.

### Proof on this Mac

- **Read-only live run:** in the installed app, open Accounts, press Look for accounts now, and save screenshots
  of the real Found rows at 1280 and 960. Snapshot `usage_accounts`, the priority list and Not offered again
  before and after; they must be identical.
- **Private evidence, public artefacts.** Live screenshots and snapshots show this Mac's real folder names and
  account names, and the repo is public. They are written only to a private folder outside every repo (the
  session's scratch folder), never committed, attached to a PR or pasted into a public comment. What goes in
  the PR is a sanitized record: counts ("3 found, 1 couldn't check"), pass or fail per check, and screenshots
  from the fake-CLI seam with fictional names. A publication check runs before every commit and PR body in
  phase 9: it fails if staged files or the PR text contain the owner's home path, a real login-folder name from
  the live run, or an account name that appears only in the live config.
- **Every write path** (Track, Not this one, Offer again, Rename, Stop tracking, the switch, the first save that
  writes the defaults out, sign-in) runs in the installed app only under a throwaway HOME and Khipu home, with
  `KHIPU_CLAUDE_CLI` and `KHIPU_CODEX_CLI` (`synth_cli.py:137`) pointing at fake CLIs: exit 0, exit non-zero,
  hang, ignore SIGTERM, print a device code. A test-only short `SIGN_IN_WAIT_S` is honoured only under a test
  home. Stop and cancel is proven by a process listing before and after showing no leftover login process.
- **A live Track on this Mac** happens only on Matt's go, with Undo shown working (decision 19).
- Every mock state is reproduced through the fake-CLI seam and saved as a screenshot file per state at 1280 and
  960, compared with the mock.

### Slices

Oracles as for every phase above: hermetic pytest from `packages/cli` under a temp `HOME` with
`KHIPU_KEYCHAIN=0` and `ruff check --no-cache packages/cli`; `npx vitest run` and `npm run build` in
`apps/desktop`; `cargo test` in `src-tauri` when Rust changes. At most three runs per change.

**Fix now, before the rest of phase 9 (packages/cli; part of 9A, shippable on its own).** Two gaps are live
today, not only in this plan, and go first, in their own PR, independent of discovery:
1. `khipu_usage` returns login-folder paths to agents (`mcp_server.py:1702` passes `usage.report()` through;
   rows carry `home`). Add the public projection (Agents' view) and its per-row-kind leak tests.
2. Codex's plan read falls back to reading session files inside the login folder (`usage.py:691`,
   `codex_rollout`). Add a `rollout=False` switch to `probe_codex` that discovery and `--try` will use, test
   that with it off a failed live read returns its failure code and opens nothing under the folder, and test the
   failure branch with it on. Tracked accounts keep the fallback until Matt answers open question 9.

**9A, discovery engine and CLI (packages/cli; one session).** Read this section, `usage.py`, `claude_homes.py`,
`t3.py`, `session_capture.py` and `synth_cli.py` first. Build: the login path recorded per session; sources 1 to
4; `STATUS_ARGV` and the one-shot runner; the discovery lock, caps, order and once-a-day recheck; what it
keeps and the last run; `khipu usage --discover --json` and `--try`; the install-place additions and the
can't-run state; the privacy redaction of names and paths. Tests: the name filter and skips, non-directories,
symlinks, permission errors, the 12 cap and order, a hanging CLI killed at 5 s, a fake CLI that reads stdin or
opens `/dev/tty`, no argv outside the table, the never-read-inside test, old state files, the count window,
Claude and Codex with and without their variables, a corrupt state file never failing the hook, only the path
recorded, a second run refused while one runs, paths absent from MCP output, exact argv tuples (`codex login
status` allowed, `codex login` refused), the rotation test (30 folders all checked within three runs), the
30-day path retention and Forget, and `cli_path.<cli>` and `node_folder` honoured by `synth_cli.binary` and the
probe PATH (a scratch nvm-style shim under launchd's bare PATH starts once the Node folder is set). A status check
that writes into a scratch folder is handled by decision 20 (Claude Code and Codex do): folders found only
by name are not checked until the owner's Check.

**9B, tracking and managing (packages/cli, routing, Rust; one session).** Read Sections 5, 7 and 9 and
`routing.py` (`routing_lane_map`, priority, holds) first. Build: naming rules and path-identity duplicates in
`parse_usage_accounts`; invalid entries kept; the one save path (default write-out home-less, canonical home,
first cache row, change line); Not offered again; the switch; Rename with the lane map moved to keys; Stop
tracking's writes and the "Read no accounts" state (decision 14); re-key for a moved
folder; folder-not-found before reads; the routing wording; the owner gate (decision 15). Tests: every key
unchanged by the write-out and by stop then track again on `~/.claude`; a symlink, full path or trailing slash
refused as the same login; names that equal a CLI word refused; a mapped lane keeps its evidence after rename;
stop with defaults leaves no row; re-key moves every owner record; a missing folder never yields the sign-in
note; a hand-edited invalid entry survives an Add; a move carries reports and evidence and the forwarding
entry folds a late report under the old key into the new account; an ambiguous lane label is left for repair,
never converted to the first match.

**9C, app surfaces (apps/desktop; one session).** Read `add-account*.html` and `PlansCard.tsx` first. Build: the
dialog (steps 1 and 3, every state but sign-in), Found rows, "Found, not checked" rows with Check and its
help line (decision 20), Looking…, couldn't check, last run line, Home's
Found and automatic-tracking cards with Close and their order, Not offered again, the switch, Rename and Stop
tracking dialogs, the folder-not-found and moved rows, the signed-out row, the "Read no accounts" state, setup's
step (found list, nothing found with Add, Sign in now, Run setup again rules), the What's new sentence. Tests:
props-driven vitest per mock state; a not-checked row runs no check until Check is pressed; Close never turns down; automatic tracking off saves nothing; setup's
unticked login reaches no Home card; focus trapped in the dialog and returned after the folder picker; Set
the Node folder… and Choose the CLI… save their settings and Check again re-runs (through the Tauri command,
not props alone); Sign in now on an untracked folder saves first and then calls `khipu_account_sign_in`
(`cargo test` plus a command-level test).

**9D, sign-in (packages/cli, Rust, apps/desktop; one session).** Read Section 6 and `usage.sign_in`,
`lib.rs` `sign_in_argv` first. Build: folder creation, the new-folder command, start, status and cancel, the
reaper on start and quit, Start again, Claude and Codex browser sign-in, the Grok and Muse code line, the
Codex Keychain and Grok guardrails first. Tests (cargo): the argv builder accepts only `~/.claude-<slug>` and
`~/.codex-<slug>` (and Grok once its guardrail passes) that Khipu created, and refuses `..`, control characters,
symlink escapes and Muse folders; cancel stops only its own group. (pytest, `popen` fake): only that folder's
variable is set; failure, timeout and cancel save nothing; the code pattern keeps only the link and code, caps
bytes, and nothing reaches logs; Escape during sign-in asks the same as Stop and cancel; two starts at once
leave exactly one owner; a stale record whose pid now belongs to another process (a fake process with a
different start time or argv) is deleted and nothing is killed; after a simulated crash the next start reaps
only a matching process. Stop if either
guardrail fails, and ship the rest.

**9E, proof and promotion (one session).** After 9A to 9D are merged: the Proof on this Mac list through the
promotion gate, screenshots per state at 1280 and 960, the snapshots identical before and after, live
evidence kept private and only the sanitized record published, the publication check green. Stop and report
on any difference.

### Open questions for Matt

Questions 1 to 8 were answered on 2026-10-15 and are decisions 12 to 19. Two stay open:

9. **Codex's session-file fallback for tracked accounts.** When Codex's live plan read fails, Khipu reads the
   last `rate_limits` from the tail of its five newest session files, inside the login folder. Those are the
   same transcripts Khipu's capture already reads, and only the figures are kept. Recommended: keep it for
   tracked accounts (removing it loses figures when Codex's live read fails, against "no loss of
   functionality"), never for discovery or `--try`, and say so in Settings next to Codex's figures ("from its
   last conversation").
10. **A CLI whose install is broken.** Khipu can set a CLI's path and its Node folder in the app, but it can't
    repair or reinstall another maker's program. Recommended: accept that as the one boundary; Khipu says what
    is wrong and links the maker's verified install page, and never runs an installer for you.
