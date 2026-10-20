# Routing as a first-class feature (2026-10-20)

Asked by Matt 2026-10-20: "A routing system that can change dynamically needs to be a first class feature. We
depend on this." Status: DRAFT for Matt's review. Nothing here is built until he approves it, and no UI is built
until its mocks are approved.

Read first: `2026-10-14-routing-truth.md` (the record, its sources, the push, decisions 1 to 21) and
`2026-10-20-routing-delivery.md` (today's fixes A to F, slice 1 here). This page adds to them and never
restates them. Where it changes a decision, it says so. It replaces phase 7 of the routing-truth plan with
slices 10 and 11 below, because phase 7 copied the record in one direction only.

## The heart of it

Today Khipu's record is right, and conversations still act on stale routing. Each failure below happened in
real work on 2026-10-14..20. Every one comes from one of three gaps:

1. **Delivery is version-based, not change-based.** A session's marker is a single version number, so "shown"
   and "delivered" are the same thing, and anything a push left out is lost (`recall_prompt.py`
   `mark_routing_seen`, `routing.py` `_fit_push`, `prompt_push`).
2. **The record mixes kinds of truth.** Owner words carry account levels that go stale, a priority can
   contradict a hold, and nothing types which is which (`routing.py` `_priority_entries`, `rule_line`).
3. **The record lives on one Mac, and copies live everywhere else.** The record is
   `~/.config/khipu/state/routing/` on this Mac only. Memory files and CLAUDE.md hold hand-written routing state
   that recall and the harness push back into conversations.

| Failure seen | Gap | Fixed in |
|---|---|---|
| A pause stayed live for a day after Matt released it: the release was written as a priority change, and the hold (`h125d608f`, ended by hand 2026-10-20 10:40Z) was never ended | 2 | slice 1 (B, F), slice 4 |
| Unconfirmed or long changes collapsed to "too long to show here"; this session's own start block (10:55Z) was the summary pointer alone | 1 | slice 1 (A), slice 3 |
| Changes left out of a prompt update were marked delivered | 1 | slice 1 (C), slice 3 |
| One conversation's unconfirmed change blocked every other conversation | 2 | slice 1 (D), slice 5 |
| "Codex is low" beside a reading of 96% left | 2 | slice 1 (E), slice 4 |
| Nothing compared a dispatch's model with rule 12; every fix round ran on Opus | 2 | slice 7 |
| GLM and DeepSeek runner lanes match no account unless mapped by hand (live: church-harness lane `glm`, unmatched) | 2 | slice 6 |
| The record lives on one Mac | 3 | slices 10, 11 |
| Memory files and CLAUDE.md held their own routing state, and it contradicted Khipu | 3 | slice 12 |

Khipu still reports and never routes (routing-truth decision 1, standing rule 7). Every check in this page is
advisory. The only things that turn red are Khipu's own delivery failures, never a routing choice.

## 1. The model: three kinds of entry

Every entry in the record is exactly one of three kinds. The kind decides who may write it, how it is shown and
how it expires. Inspect `routing.py` `_checked`, `fold` and `_fold_priority` before changing any of them.

| Kind | What it is | Examples today | Who writes it | How it ends |
|---|---|---|---|---|
| **Observation** | Something a source saw, with a witness and a time | probe readings, runner LIMITED lines, conversation reports, reset checks | Khipu's sources and conversations, author-stamped | It goes stale after `routing_stale_after`, or at a limit's end; it is never confirmed |
| **Structured fact** | A typed value the owner set or confirmed: a field, not prose | pauses, holds, the priority order and each entry's tier role, the lane map, providers, the rule 12 tier table, Clear and Dismiss | the owner (app, terminal, owner direction); conversations may write holds and *proposals* | At its `until`, or when the owner ends it. Conversation holds keep today's 24-hour default and 7-day cap. A proposal ends when it is confirmed, replaced or dismissed, or after 7 days |
| **Owner rule** | The owner's own words, shown whole and never edited | standing rules, the note, a priority entry's role words | the owner; a conversation may propose a rule (decision 21) | Rules never expire. Home asks for reconfirmation after 90 days, as it does for holds |

Three changes follow.

- **Owner words carry no state.** An account level ("low", "nearly used up", "N% left", "paused until") is an
  observation or a structured fact, never part of owner words. Slice 1 (E) marks a stale claim. Slice 4 adds an
  optional structured `level` claim to a priority entry (`{claim, as_of, until}`, ending at its `until` or the
  account's next reset), and the app offers to move a level found in the text into that field. Khipu never edits
  the words.
- **A write that touches an account says what it does to that account's holds and pauses.** A priority entry,
  rule or role that names an account with an active hold or pause cannot be written without a `holds:
  {<id>: "end" | "keep"}` choice for each one. The tool's refusal lists the holds and their ids. Khipu never
  reads a release from prose (slice 1, B). This is the structural fix for the day-long Grok pause.
- **Each entry carries a change id.** Every write and every clock-driven change (a limit ending, a hold
  expiring, a reading going stale) produces a change with a stable id. Versions stay as they are, and delivery
  counts changes, not versions (section 2).

## 2. Delivery guarantees

A conversation is **live** when it has had a prompt in the last 12 hours (a setting), counted from its
per-session state (`recall--<session>.json`). The guarantees hold for every live conversation that Khipu
reaches.

1. **When.** On this Mac, a write is in the folded view at once (writes refold under the lock). A clock-driven
   change is in it within `routing_poll_interval` (10 s). A conversation sees each change **before its next
   prompt, delegation call or Khipu tool call**, whichever comes first. Nothing interrupts a running turn, and
   this page states that limit instead of hiding it. Across Macs (slice 10), the bound is 60 s plus the
   receiving Mac's poll interval while both are online.
2. **Nothing is silently dropped.** Each session keeps a delivery ledger of change ids: *owed*, then *shown*
   (with its form: whole, short or page *n/m*), then *acked* once the hook's `op: ack` or `finish_delivery`
   confirms the write. Owed changes go first in the next push. A change counts as delivered only in a form that
   names its kind, its accounts and what it does (slice 1, A). Standing rules and the note are only ever
   delivered whole. When they do not fit, they are split into pages over the following pushes, and each page is
   tracked. A session-start summary that does not fit the 10,000-character hook limit is no longer replaced by a
   pointer: its pages are owed to the first prompts. This replaces the "left out whole, arrives with a later
   push" text in routing-truth's push section, which in practice arrived only as the pointer.
3. **Delivery is visible.** `khipu_route` tells the caller what it has been shown and what it still owes
   (`delivery: {owed, shown_at}`). The app lists each live conversation with its owed count (slice 9). Every
   pushed block ends with a short tail ("3 changes, ids r1a2…; all shown") only when something was owed or
   paged, never on a quiet prompt.
4. **Superseded changes are not delivered.** A change that a later change on the same entity replaced is
   marked *superseded* in the ledger. Only the current state is sent, as routing-truth's push section already
   requires.

Pull-only harnesses (Grok Build, Aegis, cloud agents through the gateway) get the same ledger, keyed by the
session id they pass. **Every Khipu MCP tool result** carries the caller's owed routing changes in a fenced
`routing_changes` field, so a harness that never calls `khipu_route` still hears changes the next time it uses
memory. Inspect `mcp_server.py` `_AGENT_SCRUBBED` and the tool dispatch before adding the field.

## 3. How a change in one conversation reaches the others

- **Same Mac:** the write goes to the event log. Every other live conversation owes it and receives it under
  section 2. Nothing changes from routing-truth beyond the ledger.
- **Another Mac (slice 10):** the event log is replicated through the hub, in both directions. Each Mac appends
  locally and ships its new events. The hub keeps the union, keyed by event id. Each Mac folds the union, with
  events ordered by `(written_at, mac_id, seq)` and today's clock settling (`_stamp_locked`, `CLOCK_TOLERANCE`)
  applied per Mac. Account keys are Mac-specific today (`claude|/Users/…/.claude-t3-second`), so slice 10 first
  gives each account a stable id (`provider:label`), mapped per Mac to its login home. Login homes never leave
  the Mac. A Mac that cannot reach the hub keeps working on its own log. Every push it sends says "routing from
  other Macs is N min old", and doctor turns red after 10 minutes.
- **Cloud harnesses (slice 11):** the gateway answers `khipu_route` from the hub copy, carries the
  `routing_changes` field on its tool results, and takes conversation reports and holds (author-stamped as the
  gateway's harness). Owner writes are refused there, because owner direction needs a local owner turn
  (`owner_direction.check`). What the cloud copy may hold is Matt's decision 6.

## 4. Owner direction from chat, without dead ends

The path exists today: `khipu_owner_act`, verified by `owner_direction.check` against Khipu's own record of
Matt's typed turn, and logged in the hash chain. These changes make it the normal way and remove the dead ends.

- **Pending changes never lock.** A conversation request (an unverified write, `via: mcp`) becomes a
  **proposal** with an id. A newer proposal on the same entity takes effect live, as decision 21 allows, and
  the older one is marked *replaced by <id>*. That outcome is owed back to the older proposal's conversation
  through section 2. The exclusion in `request_priority` and `request_rule` (`PriorityMoved`, the
  same-author check) goes away for proposals. `expect` (optimistic versioning) stays for owner writes.
- **The owner's word wins, with nothing to resolve by hand.** An owner write (app, terminal or direction)
  replaces every open proposal on that entity, and each replaced author is told. Two conversations whose
  proposals disagree both appear in the app's Proposals list, side by side. One tap, or "use the second one"
  through owner direction, settles it.
- **Every refusal names a way forward.** It gives the author and age of what blocked it, the current version,
  and the exact `khipu_owner_act` action (or the app place) that works (slice 1, D, extended to every write
  path).
- **A dispatched job never acts as the owner.** This is unchanged: `owner_turns.note_brief` already keeps a
  brief from passing for Matt's turn.

## 5. Lanes, accounts and new providers

Today a provider is code: `usage.HARNESSES` and its probes. A lane joins an account only through a hand-written
`routing_lane_map` entry, and the live GLM limit is unmatched.

- **Providers become data** (slice 6). A config list `routing_providers` holds entries of the form `{id, label,
  family, login: folder|none, probe: <adapter>, lane_names: [...]}`. `family` (Anthropic, OpenAI, xAI, Meta,
  Zhipu, DeepSeek) is what rule 2's "different model family" is checked against. Probe adapters come from a
  closed set in code: the four CLI probes, `http-quota` (a URL and a JSON-path map, with the key read through
  `keychain` into a variable and never printed, like `_glm_key`), and `none` (an account Khipu tracks only from
  runner lines and reports). Adding DeepSeek is then a config entry from the app, with no code change, as long
  as an existing adapter fits. A new adapter shape is a code change, and doctor names the gap.
- **Lanes match by name** (Matt's decision 3). A runner lane whose name equals a provider id or one of its
  `lane_names` matches that provider's single account, marked "matched by name" in Settings and doctor, and the
  owner can override it. A provider with two or more accounts never matches by name. Those lanes stay
  unmatched and are listed, as today, and an unmatched lane's limit line names the likely account so a
  one-step owner direction can map it.
- The live deploy gap (decision 91270: live-b's `usage.py` predates GLM) is a promotion, not a code change. It
  goes through `khipu promote` when Matt says so.

## 6. Advisory checks (rule 7: guidance, never a gate)

Each check produces a marker on the line it concerns, in the push, `khipu_route` and the app, plus a doctor
warning. None of them refuses, reorders or delays work.

- **Model tier (rule 12).** Rule 12 gets a structured companion: a tier table of task class to tier, confirmed
  by Matt once. The task classes are `scope`, `mock`, `ruling`, `hard-security`, `failed-fix`, `build`,
  `fix-round`, `tests`, `docs`, `synthesis`, `mechanical`. At delegation, `routing_delegation.py` already parses
  the call. It reads the model (Agent `model`, `claude -p --model`, `codex exec -m`, `CLAUDE_CONFIG_DIR`) and
  the task class: a declared `task:` line in the brief first, otherwise a keyword guess marked as a guess. On a
  mismatch, the delegation push carries one line: "Rule 12: fix rounds go to Sonnet; this dispatch is Opus. Say
  why if intended." Daily mismatch counts go to doctor and the app. Brief text never leaves the Mac and is never
  sent to a model (decision 5).
- **Contradictions.** These cover a priority or rule naming an account with an active hold or pause (slice 1,
  B); two rules, or a rule and a priority entry, giving opposite directions for one account (a structured
  check only, on the providers and tiers each one names); and a priority entry for an account that is down or
  untracked.
- **Stale level claims.** Slice 1 (E) covers owner words. Slice 4 adds the structured `level` claim, which
  expires by itself, and a doctor warning for any level claim more than one reset old.
- **Copies elsewhere.** Section 7.

## 7. One source of truth

Routing state lives only in Khipu's record. Instruction and memory files point at it.

- **What may stay in files:** durable capability notes (the model table in the global CLAUDE.md: what each
  model is good for) and how each lane is reached. **What may not:** whether an account is open, limited,
  paused, held or "tight", priorities, and level claims. Those come from Khipu.
- **Slice 12** adds a scanner, run by doctor and by the nightly. It reads the global and per-project CLAUDE.md
  and AGENTS.md files and the memory directories Khipu already imports (`note:` topics). It flags any
  routing-state claim (an account name next to a state word, a date or a level) and names the file and line.
  Recall marks such a note "routing state: superseded by Khipu's record" in the prior-work block instead of
  pushing it as-is. Live example: the memory index still carries "Main account tight until Mon 2026-10-19" and
  "Delegation order: second Claude account first", and both contradict today's priority.
- **The one-time cleanup of Matt's own files** (rewriting those memory notes into pointers, trimming CLAUDE.md
  state lines) is a separate step, done with his go (decision 7). Memory rewrites keep the hand-written lines
  verbatim, with a backup first ([[index-rewrites-need-verbatim-lines-and-backups]]).

## 8. Health: a broken delivery is red

The doctor warnings for routing content stay warnings (routing-truth decision 10). Delivery failures become a
red `routing_delivery` check in `khipu doctor`, in the app's health row and as an attention item (Matt's
decision 1).

- A change owed to a live conversation for more than 3 of its prompts.
- A session whose ledger shows a change *acked* that was never *shown*. This invariant catches today's
  failure 3 if it ever comes back.
- A session start that delivered only a pointer while pages were still owed past its third prompt.
- More than 1 in 100 prompts today skipped routing (`skipped:budget`, `fold-busy`, `busy`). This is the
  existing rate check, which turns red.
- The cross-Mac copy is more than 10 minutes behind, or the gateway copy is past its stale bound.
- A limit from an unmatched lane that stays unmatched for more than 1 hour.

Doctor reads its evidence from the ledgers and the hook outcome log, not from a health probe
([[audit-lessons-2026-08-17]]: a check is green only when its evidence arrived. "Not checked" is not clean).

## 9. The app

Each item needs an approved Opus mock before any build (slice 8). If a mock is missing, stop and ask. The mocks
extend `docs/mockups/2026-10-14-routing/` and use Better Design (`get-ui-principle`, `get-ux-principle`,
`get-review-rules`).

- **Settings → Routing → Delivery:** live conversations with harness, Mac, last prompt and owed count, plus
  each recent change with how many live conversations have it.
- **Proposals:** open conversation proposals by entity, side by side when they conflict, with Use, Dismiss and
  "Replaced by".
- **Hold choice:** the "end or keep" choice wherever an owner write meets an active hold.
- **Providers:** add, edit and remove a provider and its lane names, "matched by name" marks, and overrides.
- **Advisory marks:** the tier mismatches of the day, contradictions, stale claims, and copies found in files,
  each with a plain-words line and where it came from.
- **Health row:** the red `routing_delivery` check in plain words.

Every owner action stays doable from chat through `khipu_owner_act` ([[owner-direction-through-the-agent]],
[[khipu-everything-in-the-app-no-terminal]]).

## 10. Acceptance: replay the real failures end to end

`tests/test_routing_acceptance.py` (slice 2) replays a copy of the real `events.jsonl` (2026-10-14..20, in a
temporary HOME) together with scripted conversations that run the real hooks (`khipu-recall-hook`,
`khipu-prompt-recall`, the delegation hook) and the MCP tools. One case per failure in the table at the top.
Each case starts as `xfail` and is flipped by the slice that fixes it. The feature is done when every case
passes.

1. The Grok hold and the 10-19 priority: every live session is shown the contradiction. An owner-direction
   priority write is refused until it says end or keep, and "end" removes the hold from every session's next
   push.
2. A 3,000-character pending rule and a 10,500-character session-start context: the rule reaches each session
   whole across pages, and no session receives only a pointer.
3. Eight changes against a 400-character budget: all eight are shown within two prompts, and the ledger never
   acks an unshown change.
4. Two conversations propose different priorities: both are live in turn, the first author is told it was
   replaced, and neither is refused.
5. "Codex is low" against a 96% reading: the marker appears. A structured claim expires at the reset.
6. An Opus Agent dispatch with a brief declaring `task: fix-round`: the delegation push carries the rule 12
   line. A Sonnet dispatch gets nothing.
7. A runner lane `glm` and a `deepseek` provider added as config only: both match by name, with no code change.
8. Two HOMEs against a test hub: a hold written on Mac A is in Mac B's next push within the bound, and taking
   the hub down turns doctor red at 10 minutes.
9. A memory note "Main account tight until Mon": the scanner flags it, and recall marks it superseded.

## Slices

Each slice fits one session and leads with "Read this scope first; inspect {named code} before writing". One PR
per slice, in order unless noted. Every slice reaches this Mac only through `khipu promote` and its live gate
([[khipu-live-launchers-follow-main-checkout]]). The builder and the reviewer always come from different
families.

**Oracle for every slice:** hermetic pytest from `packages/cli` (temp HOME, `KHIPU_KEYCHAIN=0`; the baseline is
the named failures, not green), `ruff check --no-cache packages/cli`, and the slice's acceptance cases flipped
to pass. At most 3 oracle runs per change; if it is still red, stop and report the last log. Slices that touch
the per-prompt path also run `python -m khipu.routing_replay` (200 prompts each way through a warm daemon, with
no new timeouts).

| # | Slice | Builds | Reviews | Extra oracle and stop-conditions |
|---|---|---|---|---|
| 1 | Today's A to F (`2026-10-20-routing-delivery.md`) | Codex (in flight) | Sonnet | Its own acceptance. Not redesigned here |
| 2 | Acceptance replay harness: all 9 cases, `xfail` | Codex Sol, high effort | Sonnet | Every case fails for the stated reason (checked in the review), not on a harness error |
| 3 | Delivery ledger: change ids, owed/shown/acked, pages for rules, the note and the session start, the `delivery` field, `routing_changes` on every MCP result | Codex Sol, high effort | Opus (this is the delivery path every conversation depends on) | Cases 2 and 3 pass. `routing_replay` holds. Budgets stay at today's caps |
| 4 | The model: entry kinds, the end-or-keep hold choice on owner writes, structured `level` claims | Codex Sol, high effort | Opus (owner-action path) | Cases 1 and 5 pass. Owner words byte-identical before and after |
| 5 | Proposals instead of locks; the owner write replaces proposals; refusals name a way forward | Codex Sol, high effort | Opus (permission and owner path) | Case 4 passes. `owner_direction.check` is unchanged; a dispatched job is still refused |
| 6 | Providers as data, match by name, family field, adapters (`http-quota`, `none`) | Codex Sol, medium effort | Sonnet | Case 7 passes. No key value in any log or output (grep the test logs) |
| 7 | Advisory checks: rule 12 tier table and delegation check, the structured contradiction check | Grok builds against Codex-written tests (tests are 7a, Codex Sol medium); not privacy code | Codex re-check (rule 2), then Sonnet | Case 6 passes. The check never denies a call (assert that `additionalContext` is the only key) |
| 8 | Mocks for section 9 | Claude Opus | Matt approves | Stop: no slice 9 work before approval |
| 9 | App build of the approved mocks | Codex Sol, high effort | Sonnet; Opus for the hold choice and Proposals | `npm run build && tsc` and the desktop tests, then the rendered result: build, run, capture, compare with the mock, at most 3 visual passes, screenshot as proof |
| 10 | Cross-Mac: stable account ids, two-way event replication through the hub (migration 0041), and staleness | Codex Sol, high effort (Astra if Matt prefers, since a miss here is costly) | Opus (what leaves the Mac) | Case 8 passes against the test hub. Login homes never in a hub row (asserted). Inspect how `test_audit1010_hub*` stand up a hub before writing |
| 11 | Gateway and cloud: `khipu_route` from the hub copy, `routing_changes`, conversation reports, owner writes refused | Codex Sol, medium effort | Opus (gateway exposure) | Gateway tests, then a deploy as its own step ([[merged-is-not-deployed]]) and a live `khipu_route` through the gateway. Matt's go before the deploy |
| 12 | One source of truth: the scanner, recall's superseded mark, doctor | Grok builds against Codex-written tests (12a); it reads only Matt's own files, writes nothing | Codex re-check, then Sonnet | Case 9 passes. The cleanup of Matt's files is not in this slice (decision 7) |
| 13 | Health: the red `routing_delivery` check, the app health row, attention items | Codex Sol, medium effort | Sonnet | Each red condition in section 8 has a test that turns it red and back to green |

Slices 6, 7 and 12 can run in parallel with 3 to 5. Slice 8 can start once slice 4's model is merged. Slice 13
needs slice 3.

## Matt's decisions (each with a recommendation)

1. **Delivery failures turn doctor red.** This amends routing-truth decision 10 for delivery only; routing
   content stays a warning. *Recommend yes.* Quiet delivery failures are how the Grok pause lasted a day.
2. **Conversation requests become proposals: live at once, never locking, newest wins, the replaced author
   told.** This keeps decision 21's "live immediately" and removes the lock. *Recommend yes.* The alternative,
   holding a proposal until you confirm it, reintroduces the dead end.
3. **Lanes match a provider by exact name, marked "matched by name", overridable.** This amends routing-truth's
   "never guessed" for exact single-account names only. *Recommend yes.* An exact name with one account is not
   a guess, and hand mapping is why GLM is unmatched today.
4. **Owner writes that touch an account with a hold or pause must say end or keep.** *Recommend yes.* It is the
   only fix that does not depend on reading your prose.
5. **Rule 12 gets a structured tier table that you confirm once; the task class comes from a `task:` line in the
   brief, otherwise a marked guess; brief text never goes to a model (no Jev on briefs).** *Recommend yes.*
   Briefs can carry private material.
6. **What crosses Macs and what the cloud copy holds.** *Recommend:* the full event log between your own Macs,
   with stable account ids and no login homes. The cloud copy holds labels, states, holds, rules and the note,
   because Grok Bot needs the rules to follow them. Conversation words stay on the Macs.
7. **A one-time cleanup of routing state in your CLAUDE.md files and memory notes, turned into pointers, by a
   Sonnet job with a backup.** *Recommend yes, after slice 12 lists what it found,* so you see the list before
   anything changes.
8. **Bounds:** 12 hours for "live", 3 prompts before an owed change turns red, a 60-second cross-Mac target and
   a 10-minute red. Each is a setting. *Recommend these defaults.*

## Known limits

- An idle conversation, or a turn already running, learns nothing until its next prompt, delegation or Khipu
  tool call. No harness offers a way to interrupt.
- Grok Build discards hook output, so it hears changes only through Khipu tool results
  ([[grok-build-shares-the-aegis-home-and-has-no-push-recall]]).
- A task class guessed from keywords can be wrong. It is always shown as a guess.
- "Owner only" stays a convention (routing-truth decision 6). Owner direction is as strong as
  `owner_direction.check`.
