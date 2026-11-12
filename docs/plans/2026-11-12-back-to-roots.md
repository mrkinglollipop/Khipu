# Khipu: back to its roots

Status: proposal for Matt, 2026-11-12. Nothing in "What I propose" is built. It continues the 2026-11-05 simplify direction and replaces its remaining step list.

## What Matt said (2026-11-12)

- "We've turned Khipu into this garbage can that never is very organized."
- "I almost feel like the old system that wasn't Khipu was better."
- "I'm not sure if the memory systems built into Claude Code have gotten so much better that maybe we don't even really need Khipu anymore."
- On the 4,000 agent to-dos: "potentially something that should have never been done to begin with."
- The roots: a memory system in a wiki and a graph, with a nightly and monthly consolidation; one place for rules; one place for hooks; and usage for routing ("really big deal to me").
- "There's no way for me to choose what model is doing the work."

## What I measured today

| Question | Finding |
|---|---|
| Does Claude Code keep old transcripts? | Yes. 3,057 transcripts on the M5 main account back to 22 Aug (33 GB), 6,937 on the second account. A conversation can read its own transcript from before a compaction. I found no retention setting, so I cannot promise how long they stay. |
| Does Claude Code have its own memory? | Yes, per project: a notes folder with an index loaded into every conversation (96 projects have one). It is one harness, one account, one Mac. |
| What does Khipu hold that Claude Code does not? | 45,965 episodes: 26,492 Claude Code, 7,334 Codex, 2,926 Cursor, 1,250 Aegis, across both accounts and both Macs. Plus usage and routing for 8 accounts, and the Biblical library. |
| Do conversations use Khipu's memory? | Of 242 main-account sessions in 7 days: 239 received the automatic "prior work" note, 133 the routing page. Only 23 searched, 18 fetched a result, 4 read to-dos, 1 used the graph. |
| Agent to-dos | Automatic extraction was removed on 9 Nov (#519). 3,994 leftovers were open, 95% never seen a second time, and nothing listed them to any conversation except three per thread. None belonged to Matt. Of 20,603 ever closed, most were closed by a rule when the session ended. |
| Routing notes | The record had grown to 103 KB. Conversations added notes instead of replacing them. Merged today: Codex 6 notes to 1, Claude 6 to 3, Claude second 4 to 0. |
| Nightly | Runs at 02:05, exit 0 last night. It drains captures, consolidates, embeds, prunes caches and ledgers, sweeps storage, checks recall. It does not tidy routing notes or to-dos. Monthly consolidation last ran 1 Nov, exit 0. |
| Model choice and keys | Both exist in the installed app: Settings, "Who writes the summaries", seven choices (Claude Haiku by API key or by login, Gemini, OpenAI, OpenRouter, ChatGPT login, a local model), a free-text "Model id" box, and the keys further down the same page. Matt could find neither, so in practice they were missing. |
| Size | 143,426 lines of Python without tests, 17 tools. The 5 Nov review suggested about 45,000 lines and 8 tools. |

## Second look, after Matt asked why conversations rarely search (2026-11-12)

Matt: "conversations weren't really using Khipu to search ... I just want to make sure there's no underlying issue there." And: "a graph and a wiki ... those are non-negotiables."

Search itself is healthy where it matters. The earlier count mixed jobs with conversations. Split out, for 344 sessions in 7 days:

| Kind of session | Sessions | Searched | Got the automatic note |
|---|---|---|---|
| One-prompt jobs (builders, reviewers; they get a brief) | 219 | 11 | 218 |
| Short, 2 to 20 prompts | 108 | 12 | 107 |
| Long conversations, 21+ prompts | 17 | 14 | 16 |

Two real problems sit underneath:

1. **The wiki has stopped learning.** Of 626 wiki pages, none was revised in the last 7 days and 6 in the last 30, while about 6,000 conversations are captured each week. The nightly consolidation only writes a daily reflection and flags duplicates; it never updates a wiki page. Only the monthly job does, about 30 pages a run. The page for Khipu itself is 41 days old and still marked "seed". So a search returns piles of raw conversations instead of the distilled page, which is the opposite of what the wiki is for. This is also why the old system felt better: fewer conversations per day, so a monthly pass kept up.
2. **The graph is built but nobody walks it.** 138,000 nodes and 477,000 links, rebuilt daily, yet 1 session of 344 called the graph tool. The graph only reaches a conversation as a few "linked topics" in the automatic note.

## My read

1. Claude Code's own memory now covers two things Khipu was built for: a conversation's notes to itself, and reading back before a compaction. Khipu should stop duplicating those.
2. Claude Code does not cover what Matt actually leans on: memory across Claude, Codex, Cursor and both Macs and accounts; usage and routing; one place for hooks; the library. Those are the roots, and they are worth keeping.
3. The mess came from features that write on their own (to-do extraction, routing reminders, per-conversation routing notes) with nothing that replaces or expires what they wrote. The old system felt cleaner because only the consolidation wrote to it.
4. A cleaner that runs hourly, or a model that tidies afterwards, treats the symptom. The fix is at write time: fewer things may write, and a write replaces what it supersedes.

## What I propose

### Keep and make excellent

1. **Memory**: capture every conversation from every harness, search it, and consolidate nightly and monthly into the wiki and the graph. This is the original job. The wiki and the graph are fixed points (Matt: "non-negotiables").
   - **1a. The wiki learns every night.** The nightly updates the wiki pages that the day's conversations touched (and opens a page for a subject that has none), with the old text kept as a revision. The monthly pass stays for merging and retiring pages.
   - **1b. The graph comes to the conversation.** A search result and the automatic note carry the neighbours of what they found (related pages, decisions, files), so the graph is used without a conversation having to call a separate tool. The tool stays for deliberate walks.
   - **1c. Measure it.** The nightly reports how many pages it revised and how old the pages are that searches returned, so a stalled wiki is seen the next morning, not six weeks later.
2. **Usage and routing**: the readings, pace, and the one routing page.
3. **Hooks in one place**, and the rules file synced to every harness.
   - **3a. Finding (2026-11-12): this root is only half in Khipu.** Khipu installs its own hooks (recall, capture, delegation) into each connected app. It does not hold the rules, and it does not carry any other hook. The rules are one file, `~/.claude/CLAUDE.md`, and a shell script on the M5 (`~/.claude/scripts/sync-rules.sh`) copies it and four shared guard hooks to the M1 and builds Codex's and Antigravity's copies. Cursor's T3 threads load no rule file. The old Fable contract was folded into that rules file on 5 Nov; its own file sits unused on disk.
   - **Proposal, needs a short scope:** Khipu carries the rules file and a short list of the owner's own hooks to every connected app and Mac, shows in the app which app has which, and says plainly where an app cannot take one. This replaces the shell script; it adds no new kinds of hook.

### Change

4. **Routing notes replace, not stack.** One role line per account, plus dated exceptions that end on their own. A new note about the same subject replaces the old one at write time; the existing duplicate check is extended from exact repeats to same-subject. No conversation writes a routing note unless Matt said the words.
5. **The routing page shows each instruction in full** or not at all. Cut-off fragments are why conversations re-ask and re-write.
6. **Everything a user may choose is in Settings and findable** (Matt: "Everything that a user might need to choose or edit or modify needs to be able to be in settings"). The model choice and the API keys already exist but sat under a row named "Who writes the summaries", with the keys below the fold; Matt could find neither. First step, merged on 2026-11-12 (#594, not yet in the installed app): the row is renamed "Models and API keys" and shows the model in use. Next, after Matt said the whole Settings list is hard to navigate: a mock that regroups all thirteen rows into five pages with one-word names (General; Models; Storage; Connections; Help), after Matt asked for simpler names ("that should just be models", "we could call that connections"). Search switches sit under General, Macs under Connections, with a pick-list per job, keys as their own group under Models, and single switches on their rows instead of on pages of their own. Mock: `docs/plans/mocks/2026-11-12-settings-regrouped.html`. Nothing is built until Matt approves it.

### Added 2026-11-12, from Matt: think first, then a one-line confirm

Matt: "we need the models to just slow down for a second and think for a moment about what we're putting in Khipu, and then to maybe confirm that with me ... quick, easy for me to read and confirm."

Three lanes, by what is being written:

| What | Who decides | What Matt sees |
|---|---|---|
| The raw record: transcripts, usage readings | Automatic | Nothing |
| Something Matt said outright ("pause all Grok usage") | Saved at once, in his words | One line: what was saved and what it replaced |
| Something the model thinks should be kept (a rule, a routing note, a lasting fact) | Not saved until Matt says yes | One line with yes or no |

Rules for the third lane:

- **Think first.** Before proposing, the conversation looks up what Khipu already holds on that subject and says whether the new line replaces it or is new.
- **One line each.** "Save: <the sentence>. Replaces: <the old sentence, or nothing>. Until: <date, or until changed>."
- **At the end of the turn, never mid-work**, at most three per turn, answered with one tap or one word. In the app the same lines appear under Needs you.
- **No answer means not saved.** A proposal lapses after 7 days.
- **Anything about today's situation gets an end date** by default, so it leaves on its own.
- A decision model may pre-screen two things only: "is this already in Khipu?" and "is this lasting or just today?". It never decides for Matt.

This replaces item 8's reminders and is how item 4 stays true over time.

### Remove (each needs Matt's yes)

7. **Agent to-dos.** Keep only explicit asks to Matt ("needs you"). Remove the agent and assistant lists, their tools and their page. The leftovers are closed already.
8. **Routing-direction reminders** ("Routing said here, not in Khipu"). They produced the 8 false reminders fixed today. Matt's routing words go in by owner direction in the same turn, which the rules already require.
9. Withdrawn. The graph tool stays; see 1b.

### Not proposed

- An hourly cleanup. Nothing found today would have been prevented by one.
- A local or decision model as a janitor. A decision model has one sensible place here: answering "does this new note replace an existing one?" at write time. It would ship behind a switch and be measured first, and it never decides a permission.
- Dropping Khipu for Claude Code's memory. That would lose Codex, Cursor, the second account, the M1, usage and routing.

## Order

1. Item 1a (the wiki learns every night): the biggest gap between what Khipu is for and what it does. Needs a short scope of its own: which model writes, the cost ceiling, how a page is matched to a conversation, how a bad rewrite is undone.
2. Items 4 and 5 (routing notes and page) and the think-then-confirm rule: small, and they stop today's mess from growing back.
3. Item 1b (graph neighbours in search and the note), then 1c.
4. Item 6 (Models page): mock first, then build.
5. Items 7 and 8 (removals), one PR each, after Matt's yes to each.
6. Then return to the 5 Nov budget: lines, tools and settings go down, not up.

## Settled with Matt on 2026-11-12

- The wiki and the graph stay, always.
- Something Matt says outright is saved at once and echoed in one line; no confirm.
- Everything a user may choose is in Settings.
- Remove agent to-dos (item 7): "Remove the agent to-dos, yes."
- Remove routing-direction reminders (item 8): yes, "if we don't need them". They are not needed: the rules already require his routing words to go into Khipu in the same turn.
- Scope the nightly wiki update (item 1a): go ("we definitely need it").
- A "Proportion" rule was added to `~/.claude/CLAUDE.md` at his ask and synced to both Macs.
- The regrouped Settings mock (item 6) is approved: "Approve mock", after two rounds of changes (one-word page names; Model and Source as two separate choices instead of "on my Claude login"). It is being built from `docs/plans/mocks/2026-11-12-settings-regrouped.html`.
- Khipu should carry the rules and his own hooks to every app (item 3a): "Yes, scope it." It must work for a user with no Claude, across Macs, at setup, change and move, and it must read the rules and hooks other apps already have. Scope: `docs/plans/2026-11-12-rules-and-hooks-in-khipu.md` (being written).
- The nightly wiki update is scoped in `docs/plans/2026-11-12-nightly-wiki-update.md`.

## Questions for Matt

- **Usage on Today** (Matt, 2026-11-12: "on the today screen, there should be helpful information ... usage limits ... at a glance for the different accounts it is tracking"). Proposed mock: `docs/plans/mocks/2026-11-12-today-usage.html`. One tile per account Khipu reads, under the status sentence and above Needs you: what is left of the account's tightest limit as a bar, the reset day, the pace word the Accounts place already uses, a second limit on one small line, and when the reading is old, its age. Choosing a tile opens that account. "Choose accounts" has a switch per account (Matt, same day: he wants an account he does not care about out of sight, not deleted); off takes the account off Today only. Corrected by Matt later the same day: Muse and Kimi are still active until their plans end (Muse 13 Nov, Kimi 27 Nov); he is not renewing them, and nothing still usable is to be paused or hidden. So a tile whose plan has an end date says the day, the account stays in use until then, and after that day the tile leaves Today by itself and stays in Choose accounts. It reads what the Accounts place already reads; the only new stored value is which accounts are switched off. Approved by Matt on 2026-11-12 ("The mocks look good."); the build is branch `feat/today-usage`.

The two scope docs carry their own questions.
