# Owed and the ledger as the single source of truth

Asked by Matt 2026-10-16: "khipu is supposed to be the ssot of our ledger now... if that's not working, I
need to fix that." Two problems were reported from a busy orchestrated project (called **project A** here,
because this repo is public): its Owed list is flooded, and its ledger in Khipu is empty while the real
ledger is still the old tracker file. This page is the scope: findings with evidence, a design, single-session
slices with their oracles, and questions for Matt. Read `2026-10-15-work-view.md` first; this page builds on
it and changes none of its decisions except where it says so.

## Status

| Slice | What | Size | State |
|---|---|---|---|
| 0 | This scope | S | written, waiting for Matt |
| 1 | Owner and origin at capture | M | open |
| 2 | Auto-close by pull request and by decision | M | open |
| 3 | Paging, filters and bulk changes in `khipu_owed`, `khipu_owed_update` and `khipu owed` | M | open |
| 4 | The one-time clean-up: planner, preview, apply and undo | M | open |
| 5 | App: Owed by owner, paging, select and close many, the clean-up preview | M | open |
| 6 | App: Import a tracker (the import part of Work phase 5) | M | open |
| 7 | Run the clean-up and the import for project A | S | needs Matt's go |

## Findings

Measured 2026-10-16 on the live hub through `khipu owed` (read-only) and `khipu get` on a sample of 240
episodes. Nothing was written to the hub.

### The size of the flood

- Project A has **3,687 open commitments**: 2,126 questions, 1,099 follow-ups, 433 blockers, 29 promises.
  **3,583 (97%) resolve to owner "user"**, so all of them show as "needs the user".
- Opened per day (all statuses): 269 on Oct 8, 413, 545, 606, 685, **1,326 on Oct 13**, 850, 557, and 405 on
  Oct 16 so far. Before Oct 7 the project had 8 in total.
- 1,986 are closed: 778 by "session-ended", 525 by a text match, 5 by cosine, 367 with no reason recorded
  (closed by hand), the rest as duplicates by the hygiene pass. Only 4 are stale.
- 674 open items name a pull request ("PR #N", "#N"). 658 open items have been restated more than once
  (`seen_count` above 1).

### Root cause 1: a dispatched job's "user" is the orchestrator, and Khipu cannot tell

- The extractor asks the model for an owner that is only ever `"user"` or `"assistant"`
  (`packages/cli/khipu/extract.py:69-70`). In a dispatched job, the person typing the prompt is the
  orchestrator that wrote the brief, so the model correctly calls the orchestrator "the user". Khipu stores
  that as Matt.
- In the sample, **76% of open items (342 of 452) came from sessions with exactly one episode**, the shape of
  a dispatched job (167 of 171 sessions sampled had one episode; one long session had 56). Examples of job
  final-message lines that became Matt's items: "The orchestrator must authorise a legal-text resolution",
  "Rebase the PR #539 stack onto main and resolve the conflicts", "Correct the phone installer's renamed app
  path", "Check for a clash with migration 185 if the other job also reaches for it".
- The commitment writer never looks at lineage when deciding the owner (`commitments.py:817-821`), although
  the capture payload carries `parent_session_id` for a Claude Code child (`session_capture.py:216-245`,
  stored at `session_capture.py:2270-2271`). Codex and Aegis jobs carry no lineage at all unless their runner
  sets `KHIPU_PARENT_SESSION` (`session_capture.py:227-232`).
- There is no owner for a third party. "The orchestrator", "PR #234's author", "the reviewer", "the
  builder" are none of user or assistant, so they fall to whichever the rules pick.

### Root cause 2: the owner rules push almost everything to the user

Why each of the 3,583 user-owned open items is the user's, reproduced with Khipu's own functions:

| Rule that fired | Items | Where |
|---|---|---|
| kind is `question`: always the user's | 2,126 | `commitments.py:448-449` |
| no signal in the text, so the model's declared owner (`"user"`) wins | 871 | `commitments.py:472-475` |
| an alias of the user governs a verb ("Matt must", "Matt to decide", "owner to approve") | about 520 | `commitments.py:314-329` |
| a passive decision shape ("approve", "decide whether") | about 60 | `commitments.py:331-343` |

- A question is the user's whatever it says, so "Orchestrator to rule whether ..." is Matt's
  (`commitments.py:448`), and a question can never be closed by the session-end rule
  (`commitments.py:1163-1169`, `commitments.py:1211`).
- "owner" is one of the generic words that always mean the user (`commitments.py:46`), which is right for a
  conversation with Matt and wrong inside a job, where "the owner" is often the PR's author or the lane.
- User-owned items are never closed by the session-end rule (`commitments.py:1211`), and stale only after
  30 days for questions and blockers, 14 for follow-ups (`commitments.py:1079-1105`). The flood began on
  Oct 8, so nothing has aged out yet; 4 items are stale.

### Root cause 3: nothing closes an item when its pull request merges

- The only automatic closes are: a later capture's `closed_loops` or a `done:` decision matching the text in
  the same project (`commitments.py:988-1076`), the session-end rule for assistant items
  (`commitments.py:1172-1245`), staleness (`commitments.py:1108-1114`), and the hygiene pass's drops and
  duplicates (`hygiene.py:679-714`, `hygiene.py:775`). None of them knows a pull request number. A later
  episode saying "PR #366 merged" closes "Implement the two fixes before PR #366 can merge" only if the
  model happened to write a closed loop whose words cover the item's words.
- Khipu already holds two places that know a PR's fate and neither is consulted: a runner's
  `work-summary.json` `prs[]` with stage `merged` or `closed` (Work view part B), and ledger rows naming a
  PR in `refs.pr` with their state.
- A hand close records no reason and no author: `set_status` writes only `status` and `closed_at`
  (`commitments.py:1357-1378`), which is the 367 closed rows with no reason.

### Root cause 4: the reading and closing tools cannot handle thousands

- `khipu_owed` caps a page at 200 and has no offset, cursor, total or owner filter
  (`mcp_server.py:539-559`, `mcp_server.py:1528`). An agent cannot see items 201 to 3,687 at all.
- `khipu_owed_update` takes one `id` (`mcp_server.py:561-577`); closing 3,000 items is 3,000 calls.
- `khipu owed` defaults to 50, takes `--limit` but no offset, has no `--json` flag (it always prints JSON),
  and closes one `--close ID` at a time (`cli.py:2249-2316`, `cli.py:5160-5178`).
- The app reads at most 500 (`apps/desktop/src/App.tsx:471`, `App.tsx:1648-1653`) and closes one item at a
  time (`App.tsx:3464`).
- Work's Waiting on you reads every user-owned open commitment with no limit (`work.py:948-972`), so all
  3,583 land there and in the rail badge.
- The session-start slice shows 5 items marked "(needs the user)" and tells the session "do not act on them
  yourself" (`recall_rule.py:256-271`, chosen by `activity.py:327`). Inside project A's jobs that line tells a
  job not to do work that was in fact a job's.

### The ledger: import is not in the app, and the tracker's folder is refused

- **The app has no import.** Work phase 5 (Settings, Work, with Import a tracker...) is still open
  (`2026-10-15-work-view.md`, Status). The desktop has fixed commands for ledger list, get, removed, set,
  remove and restore (`apps/desktop/src-tauri/src/lib.rs:2244-2329`) and none for import. The Work test
  asserts no "Import a tracker" button exists (`apps/desktop/src/__tests__/Work.test.tsx:327`). No branch has
  one (checked `origin/feat/work-*`).
- **Only the owner may import.** `ledger import` has policy `owner`: the app, or Matt's yes typed on an
  interactive terminal (`ledger.py:2415-2418`). A dry run is a read and anyone may run it
  (`ledger.py:2436`). `khipu_ledger` has no import verb (`mcp_server.py:1954-1955`). So today the import
  can only happen in a terminal, which Matt's rules forbid.
- **The tracker's folder is refused.** Project A's tracker lives under `~/.claude/projects/<project>/`,
  inside Claude Code's login folder. The importer reads through `safe_read`, which refuses any path in a
  login folder (`safe_read.py:41-47`, `agent_view.py:191-213`), with "Not read: the file is inside a private
  folder or a login folder, or is reached through a link." (`ledger_import.py:122-127`). Verified: a dry run
  on a tracker at that place under a temporary home was refused with exactly that message.
- **The importer itself takes project A's shape.** The real file could not be read for this scope (the copy
  out of the login folder was denied, rightly). A synthetic tracker built to its known shape (1,068 rows;
  states DONE, IN FLIGHT, MERGED-UNVERIFIED, BUILT-UNMERGED, QUEUED, NEEDS MATT, DROPPED, VERIFIED, NOT
  STARTED; kinds goal, slice, review, loose-end, needs-matt; mixed id forms; a missing parent; multi-line
  notes) was imported into the hermetic test database: the dry run said "Ready to import 1068 rows and 2136
  history entries. Nothing was written."; the import said "Imported 1068 rows and 2136 history entries;
  every count matches and the round trip gives the file back."; a second import said "Nothing new". Every
  state mapped to a meaning (NEEDS MATT to needs, VERIFIED and DONE to done, DROPPED to dropped, the rest
  open) with no new words. **Unverified for the real file**: its other top-level keys, its history entry
  shapes and any key-shaped text. A dry run on a copy outside the login folder would verify it (slice 7).

## Design

Four rules from Matt hold throughout: no loss of memory quality (text is never dropped, only labelled,
closed with a reason, or moved out of Matt's lists), every limit liftable in the app, every owner action in
the app, and Khipu reports and never routes.

### (a) Capture only real commitments, with the right owner

**Two new facts on every commitment** (migration `0028_commitment_owner.sql`, additive):

- `origin`: `conversation` or `job`. A capture is a job when its payload has a `parent_session_id`, or its
  runner set `KHIPU_PARENT_SESSION` or a new `KHIPU_JOB=1`, or the harness reports a non-interactive run
  (Claude Code print mode and `codex exec`; slice 1 verifies which markers each harness really gives and
  uses only those). Unknown stays `conversation`, today's behaviour.
- `owner` gains a third value, `agent`, with `owner_label` (a short name: "orchestrator", "PR author",
  "reviewer", "builder", "Codex", "a job"). `user` and `assistant` keep their meaning.

**The rules, deterministic as today, in this order:**

1. The text names the user by alias as the actor (today's `actor_re`) or names a third party as the actor
   ("the orchestrator must", "PR #234's author to", "reviewer to", "the builder should"): that actor is the
   owner. A new `agent_actor_re` holds the third-party words; it is checked before the user words, so "the
   owner of PR #12 to rebase" is the PR author's.
2. From a **job**: a question or a decision ask with no named actor, and anything the model declared
   `"user"`, goes to `agent` with label "orchestrator", because a job's user is its dispatcher. Only text
   that names Matt (an alias, not "the user" or "owner") stays Matt's.
3. From a **conversation**: today's rules, except a question that names a non-user actor follows rule 1.
4. The extractor prompt asks for `owner` as one of `user`, `assistant` or `other`, plus `owner_label`, and
   is told whether the transcript is a job's. The deterministic result still wins on conflict.

**Nothing is dropped.** An item that is not Matt's is still stored, searchable through `khipu_search` and
`khipu owed --owner agent`, shown in Owed under its own heading, and closed by the same rules. What changes is
where it shows:

| Owner | Owed section | Waiting on you and the rail badge | Session-start slice |
|---|---|---|---|
| user | Needs you | yes | "(needs the user)" |
| assistant | Yours, the assistant's | no | "(yours)" in sessions of the same lineage |
| agent | For agents and jobs, with its label | no | "(for the orchestrator)" in conversation sessions of the project, at most 3 (a setting) |

A job's own session no longer sees another job's items as "needs the user, do not act on them".

**Duplicates across a job and its orchestrator.** A job's "Matt to decide X" is usually relayed by the
orchestrator in its own words. When an item from a conversation matches an open job item of the same
project (today's near-duplicate test, `commitments.py:750`), the job item is closed as `relayed-by <id>`
and linked, not left as a second "needs you".

### (b) Auto-close when the pull requests merge, or a later decision settles it

**PR references.** At insert, Khipu parses the PR numbers an item names (`PR #N`, `pull request N`, `#N`
next to "PR", and a GitHub pull link) into `prs int[]` on the row. Slice 2 backfills existing rows.

**Where a PR's fate comes from**, in order, all read-only and all already in Khipu's reach:

1. A registered runner's `work-summary.json`: a PR at stage `merged` or `closed`.
2. The ledger: a row whose `refs.pr` names the PR in a state that means done (merged, verified, done).
3. A later capture in the same project whose decisions, closed loops or summary say "PR #N merged" or
   "closed #N" (a narrow pattern; a match on "will merge" or "before #N merges" does not count).
4. Only with Matt's yes (question 1): a read-only `gh pr view` lookup, as a setting off by default, because
   the Work view scope says Khipu does not read a project's pull requests on GitHub.

**The rule.** An open item closes when **every** PR it names is merged or closed, with
`close_reason` "pull request #366 merged (from the runner's summary)" or similar, `closed_by` "Khipu, by
rule", and the evidence kept. An item naming one merged PR and one open PR stays open. A blocker phrased as
"before PR #N merges" closes on the merge too, since the window it guarded is gone.

**Settled by a decision.** A question or a "decide" item closes when a later decision in the same project
answers it: the decision's text matches the item at or above `commitment_close_similarity` by the existing
completion match, or the decision names the item's id. The reason records the decision's id. Anything
looser is shown as "Looks settled by decision 79627" with Close and Not settled in the app, and never closed
on its own.

**Every close is reversible.** Reopen (one item, or a whole rule run) restores the item as it was; a
reopened item is never closed again by the same rule and evidence (`close_rule_key` remembered). The
nightly runs the PR and decision rules once over all open items; each capture runs them for its project.

**Hand closes keep a reason too.** `set_status` gains `reason` and `by` ("you, in the app", "a
conversation", "Khipu, by rule"), so no close is silent again.

### (c) Paging and bulk changes for agents

- `khipu_owed`: adds `owner` (user, assistant, agent, all; default all), `kind`, `origin`, `query` (words
  in the text), `pr` (items naming a PR), `cursor` and returns `total`, `next_cursor` and `counts` by owner
  and kind. The page size stays 50 by default; the most per page becomes the setting `owed_page_max`
  (default 200, 0 for no limit), changeable in the app. The cursor is keyset (`opened_at`, `id`) so pages
  never skip or repeat while new items arrive.
- `khipu_owed_update`: accepts `ids` (a list) as well as `id`, with `action` close, reopen, snooze or
  `reassign` (`owner` plus `owner_label`), and a required `reason` for a list. One transaction, one result per
  id, through `owner_writes` so a queued write replays whole. The most ids per call is the setting
  `owed_bulk_max` (default 500, 0 for no limit). A conversation may close or reassign items it or its jobs
  own in bulk; **items owned by the user are closed in bulk only from the app** (question 6), single closes
  by id stay as today for "the user says it is done".
- `khipu owed`: the same filters, `--cursor`, `--close ID[,ID...]`, `--reassign`, `--reason`, and `--json`
  accepted as a no-op so callers that pass it keep working.

### (d) The one-time clean-up of the existing flood

A planner, `khipu owed tidy --project P`, applies (a) and (b) to every open item and writes a plan without
changing anything:

- per item: keep as is; **reassign** (to the orchestrator, a job, the assistant) with the rule that fired;
  **close** because every named PR merged, because a decision settled it, or as a duplicate of a kept item;
  each with its evidence.
- totals: how many stay Matt's, how many move, how many close and why, and 20 sample items per group.
- deterministic first; an optional model pass for the items no rule decides, capped by `owed_tidy_calls`
  (a setting, 0 for no limit), never on items that name Matt.

The app shows the plan (slice 5) with Apply and Not now. Apply writes every change in one batch with a
`tidy_batch` id and the reason per item. **Undo this clean-up** reopens and restores owners for every item in
that batch that has not been changed since. No row is deleted; closed items stay searchable and listed under
Closed with their reason.

Expected on project A from the measurements (to be confirmed by the planner's own run): of 3,583 items now
Matt's, roughly three quarters come from jobs, so the Needs you list should fall well under 1,000 before
any PR or decision closes, which then work on the 674 items naming PRs.

### (e) Importer changes and the steps for Matt

**Keep the login-folder refusal.** Reading inside a login folder is refused for good reasons, and a
narrow exception for "tracker files under `projects/`" would be a hole other files could use. Instead:

1. **A ledger inbox.** Khipu makes `<data dir>/ledger-inbox/`, and the app's Choose file opens there. Moving
   a tracker into it is the project's step, not Khipu's: at the switch moment, project A's orchestrator
   stops writing its tracker, copies `tracker.json` into the inbox, and runs `khipu ledger import <copy>
   --project <key> --dry-run --json` to report the counts to Matt. Khipu never writes, moves or deletes the
   original (Work view rule kept).
2. **Import a tracker in the app** (slice 6), as the Work view scope already describes, built now ahead of
   the rest of phase 5: Settings, Work, Ledger, **Import a tracker...**; Choose file; Project (a picker of
   known projects, the commitments' project keys first, or type a new one); **Check this file** runs the dry
   run and shows rows by state and kind, history entries, missing parents, ids already present, masked
   fields and other keys; **Import** runs the real import and shows the verification ("every count matches
   and the round trip gives the file back") or why it was rolled back. Fixed argv: `ledger import --json
   --from-app --project=<key> [--dry-run] -- <path>`.
3. **No catch-up for a copied file.** A copy never changes, so "Bring in the changes" is not used for a
   tracker that lived in a login folder; the switch is a hard cut (compatibility item 6 of the Work view).
   The app says so when the chosen file is in the inbox.
4. `khipu_ledger` stays without an import verb: import remains the owner's act.

**The steps for Matt, once slices 1 to 6 are merged and promoted** (all in the app, no terminal):

1. In project A's orchestrator conversation, say "switch the tracker to Khipu". It stops writing the old
   tracker, copies it into the ledger inbox and reports the dry-run counts.
2. Open Khipu, go to **Settings**, then **Work**, then **Ledger**, and click **Import a tracker...**.
3. Click **Choose file**; the inbox opens; pick the tracker. Pick project A in **Project**.
4. Click **Check this file**. Compare the counts with what the orchestrator reported.
5. Click **Import**. Wait for "every count matches and the round trip gives the file back".
6. Tell the orchestrator "the ledger is in Khipu"; it starts calling `khipu ledger`.
7. Open **Work**, then **Owed**, then **Tidy this list...** for project A; read the plan; click **Apply**
   (or **Undo this clean-up** later if anything looks wrong).

## Slices

Every slice reads this page and `2026-10-15-work-view.md` first. Oracle for Python slices: hermetic pytest
from `packages/cli` under a temp `HOME` with `KHIPU_KEYCHAIN=0` (the only allowed failure is
`test_real_python_shim_marks_after_the_package_import[True]`), and `ruff check --no-cache packages/cli` at no
more than today's 9. App slices add `npx vitest run`, `npm run build` and `cargo test --offline --lib`, then
the installed app through the promotion gate, seen rendering at 1280 and 960 wide (screenshots to files). At
most three runs of an oracle per change; still red means stop and report the last log. Fixtures are
synthetic; no slice reads a real tracker or writes the live hub.

### Slice 1: owner and origin at capture (M)

Inspect `commitments.py` (`resolve_owner`, `_user_signal`, `open_from_episode`, `close_session_plan`,
`finish_owed_rows`), `extract.py` (`PROMPT`, `_as_open_loops`) and how `session_capture.py` fills
`parent_session_id` before writing the migration and the rules.

- Build: migration 0028 (`origin`, `owner_label`, the `agent` owner); job detection (verify each harness's
  real non-interactive marker in a fake harness; if a marker cannot be confirmed, leave it out and say so);
  `agent_actor_re`; the job rules; the prompt change; the relay close; the slice and Waiting on you filters.
- Tests: the brief's examples ("The orchestrator must resolve the rebase conflict", "PR #234 author to
  renumber migration 115") are `agent`; a job's unnamed question is the orchestrator's; a job's "Matt to
  decide X" stays Matt's; a conversation's question stays Matt's; no item is dropped by the new rules (counts
  in equal counts out); a pre-migration hub still answers.
- Stop: if the near-duplicate relay test closes a conversation item, stop and ask.

### Slice 2: auto-close by pull request and by decision (M)

Inspect `auto_close`, `set_status`, `work.py`'s summary reader and `ledger.py`'s `refs` before writing.

- Build: `prs` parsing and backfill; the three PR sources; the all-PRs-done rule; decision settles with the
  strict match and "Looks settled" suggestions; `close_reason`, `closed_by`, `close_rule_key`; reopen
  restores and is remembered; nightly and per-capture runs.
- Tests: one merged of two named stays open; both merged closes with the reason; "before #N merges" closes
  on merge; "will merge #N" in a later capture does not count; a reopened item is not closed again by the
  same evidence; a hand close records reason and author.
- Stop: no GitHub call in this slice unless Matt said yes to question 1.

### Slice 3: paging, filters and bulk in the tools (M)

Inspect `_tool_owed`, `_tool_owed_update`, `_owed_update_on_hub`, `owner_writes` and `cmd_owed`.

- Build: the filters, keyset cursor, `total` and `counts`; `ids`, `reassign`, `reason`; the two settings;
  the CLI flags; the bulk refusal for user-owned items outside the app.
- Tests: 4,000 synthetic items page through with no skip or repeat while 50 more arrive; a bulk close of 500
  is one transaction with one result per id; a queued bulk write replays whole; 0 lifts each cap.

### Slice 4: the clean-up planner (M)

Inspect `hygiene.run_commitments_hygiene` (its order, its model cap, its never-delete rule) and reuse it.

- Build: `khipu owed tidy --project P [--plan-out FILE] [--apply PLAN] [--undo BATCH]`, `tidy_batch`, the
  plan document the app reads.
- Tests: on a synthetic flood shaped like project A's, the plan's counts add up to the open total; apply then
  undo returns every row to its prior state; an item changed after apply is left alone by undo and listed.

### Slice 5: the app's Owed (M)

Read the Work view mocks first. Inspect the Owed section in `WorkView.tsx` and `App.tsx`'s Owed reads and
writes.

- Build: Needs you, Yours and For agents and jobs headings with counts; paging past 500; select several and
  Close, Snooze or Move to...; Tidy this list... with the plan, Apply and Undo; Waiting on you and the badge
  count only Matt's items.
- Oracle as above with a fixture of 4,000 items, plus closing 50 at once in the installed app and seeing
  the badge fall.
- Stop: a control whose command is not built stays out.

### Slice 6: Import a tracker in the app (M)

Inspect `lib.rs`'s `ledger_*_argv` functions, `ledger.py`'s `_cmd_import` and the Work view's import
section before writing.

- Build: the ledger inbox folder; Settings, Work, Ledger, Import a tracker... with Choose file, Project,
  Check this file, Import, the verification and the copied-file note; the fixed argv with `--from-app`.
- Oracle as above plus, in the installed app, a synthetic 1,068-row tracker placed in the inbox imported
  with matching counts, and a file chosen from inside a login folder refused with its plain message.

### Slice 7: project A (S, Matt's go)

With Matt's go and after slices 1 to 6 are promoted: the orchestrator copies its tracker to the inbox and
reports the dry run; Matt imports in the app; Matt reviews and applies the tidy plan. Live check: `khipu
ledger check --project <key>` shows the tracker's row count, and Needs you for project A shows only Matt's
items within one refresh.

## Questions for Matt

1. **Pull request state from GitHub.** The Work view says Khipu does not read a project's pull requests on
   GitHub. May Khipu run a read-only `gh pr view` for PRs that open items name, as a setting off by default?
   Without it, items close only when a runner's summary, the ledger or a later capture says the PR merged.
   Recommended: yes, as an opt-in setting.
2. **Where job items show.** Should items for the orchestrator appear in the orchestrator's session-start
   slice (at most 3, a setting), or only in Owed under For agents and jobs? Recommended: in the slice.
3. **A job's "Matt to decide".** Keep it as yours (and close it when the orchestrator relays it), or send it
   to the orchestrator until the orchestrator asks you? Recommended: keep it yours when it names you.
4. **The clean-up.** Run the deterministic rules plus a capped model pass on items no rule decides, preview
   first? Recommended: yes, with the model pass off for items that name you.
5. **The tracker's move.** Is the ledger inbox (inside Khipu's data folder) the right place for the
   orchestrator to put the copy, and may the orchestrator do the copy at the switch moment?
6. **Bulk closes of your items.** Only from the app, or may a conversation bulk-close your items when you
   say so in chat? Recommended: only from the app; a conversation still closes one by id when you say it is
   done.
