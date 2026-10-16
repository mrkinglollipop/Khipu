# Owner direction through the agent (scope, 2026-10-16)

Status: scope for review. No code changed. Owner requirement (Matt, 2026-10-16):

> "I need to be able to do any changes in Khipu through you with my direction. I can't play games trying to manually confirm something when I'm not in front of my computer."

He directs agents from his phone. Today every owner action needs the Khipu app or a `yes` typed on an interactive terminal, so from the phone he can only ask, and the change waits (or never happens) until he is back at the Mac.

What the gates exist for, and what this design must keep: **an agent must never take an owner action on its own, or because of text it read** (a web page, a file, tool output, pasted content, another session's message). The owner's own words, typed in a real conversation turn, must be enough.

Evidence read for this doc: the code at main `3997db9` in a worktree. No live record, hub, hook log or login folder was read. Line numbers are at that commit.

## 1. Inventory of owner gates

There are three gate mechanisms. Everything else is a caller of one of them.

### Mechanism A: `routing._owner_via` (the app, or a typed yes)

- `packages/cli/khipu/routing.py:6365` `_owner_via(args)`: with `--from-app`, accepted only when `_caller()` (`routing.py:6331`) walks the process tree to the installed Khipu app's own binary; otherwise `typed_yes` (`routing.py:6387`), which accepts only `yes` read from `sys.stdin` when it is a TTY. A piped answer never counts. The refusal text is `_REFUSED` (`routing.py:6406`). The way it was authorised is stamped on the record line (`_OWNER_AUTH`, `_owner_stamp`).
- Satisfied today by: the app (Settings, Routing; Settings, Accounts; the Update anyway button), or a typed yes in a terminal.

Callers:

| # | Owner action | Gate call | CLI | MCP |
|---|---|---|---|---|
| 1 | Pause, lift, clear an account | `routing.py:6665` (`run_cli`), then `pause` `:4030`, `lift` `:4039`, `clear` `:4047` | `khipu route pause/lift/clear` | none |
| 2 | Owner's note set and end | `routing.py:6665`, `set_note` `:4081` | `khipu route note set/end` | none |
| 3 | Rules: add, edit, retire, restore, confirm, undo or dismiss a request | `routing.py:6665`, `add_rule` `:4105`, `confirm_rule` `:4194`, `undo_rule_request` `:4199`, `dismiss_rule_request` `:4206` | `khipu route rule ...` | none for the owner's own; see C |
| 4 | Owner holds and ending one as owner | `routing.py:6633`, `add_hold` `:3916`, `end_hold` `:3959` | `khipu route hold --owner` | none (`khipu_route_report` writes conversation holds, which are not owner actions) |
| 5 | Priority: set, confirm, undo, clear | `routing.py:6545` (`_priority_cli`), `set_priority` `:4299`, `confirm_priority` `:4355` | `khipu route priority` | none for the owner's own; see C |
| 6 | Accounts: track, rename, stop, move, auto-track on/off, decline, offer again, answer a conversation's request | `accounts.py:1072` (`run_cli`) | `khipu accounts ...` | none |
| 7 | The account list as one save | `cli.py:3201` | `khipu config --set usage_accounts` | none |
| 8 | Every routing setting (lane map, runner folders, push switches, sizes and times; `routing.SETTINGS`) | `cli.py:3334` via `_owner_or_request` `cli.py:3065` and `_owner_only` `cli.py:3082` | `khipu config --set routing_*` | none |
| 9 | Answer a conversation's change to a routing setting | `cli.py:3315` | `khipu config --answer-request KEY confirm/undo` | none |
| 10 | The one-time "Update anyway" (skip named red checks on the next promotion) | `cli.py:3481` `_config_gate_override`, then `promote.set_gate_ignore` (`promote.py:48`, `:598`) | `khipu config --set promote_gate_ignore` | none |

### Mechanism B: `ledger.require` (the ledger and Work view)

- `packages/cli/khipu/ledger.py:2428` `require(command)`, driven by `WRITE_POLICY` (`ledger.py:2415`): "writer" verbs are open to any writer; "owner" verbs go through the app or `routing.typed_yes` (`ledger.py:2452`); "app" verbs (`_APP_ONLY`, `ledger.py:2422`) need the app with no terminal alternative. `--from-app` is checked by `_check_from_app` (`ledger.py:2458`).

| # | Owner action | Policy | Reached by |
|---|---|---|---|
| 11 | `ledger import` (bring a tracker file in) | owner | `khipu ledger import` (`ledger.py:2330`); not an MCP verb (`mcp_server.py:2005`) |
| 12 | `work hide`, `work unhide` | owner | `khipu work hide/unhide` (`work.py:1510`) |
| 13 | `work same`, `work not-same` (merge or split work items) | owner | `khipu work same/not-same` (`work.py:1510`) |
| 14 | `ledger restore` (bring a removed row back) | app only | the app (Settings, Work, Removed rows) |
| 15 | `ledger purge` (remove a row for good) | app only | the app |
| 16 | Any write "as you, in the app" (`--from-app` on add, set, remove) | app | the app; MCP `khipu_ledger` (`mcp_server.py:2021`) only ever writes as the conversation |

### Mechanism C: pending until confirmed (a conversation asks, the owner confirms)

| # | Request | Where it is made | How it is confirmed today |
|---|---|---|---|
| 17 | A rule (add or edit) at the owner's request | MCP `khipu_route_rule` (`mcp_server.py:825`, `:1904`) and `khipu route rule add --at-owner-request` (`routing.py:6649`), both into `request_rule` (`routing.py:4125`) | `khipu route rule confirm N` (mechanism A) or the app. The rule is live while pending, marked as the conversation's |
| 18 | Priority at the owner's request | MCP `khipu_route_priority` (`mcp_server.py:847`, `:1927`, into `conversation_priority` `routing.py:5694`) and `khipu route priority set --at-owner-request` (`routing.py:6537`, into `request_priority` `:4312`) | `khipu route priority confirm` (A) or the app |
| 19 | A routing setting at the owner's request | `khipu config --set routing_* --at-owner-request` (`cli.py:3065`, `routing.set_setting` `routing.py:473`) | `--answer-request` (row 9) or the app |
| 20 | An account-list change at the owner's request | `khipu config --set usage_accounts --at-owner-request` (`cli.py:3192`) | `khipu accounts request confirm` (row 6) or the app |

The `owner_request` words a conversation passes today are free text it writes itself: nothing checks them against anything the owner said. That is why they stay pending.

### Not gated today (no change needed, listed so nobody adds a gate by mistake)

Work-view settings (`work.set_setting`, `cli.py:3286`), ledger settings (`cli.py:3273`), `khipu budget lift` (`cli.py:3541`), owed and decision updates (`khipu_owed_update`, `khipu_decisions_update`), `khipu_forget`, and conversation holds and reports. Any conversation can make these now. The join-kit passphrase prompt (`cli.py:3730`) reads a secret from a TTY; it is not an owner gate and is out of scope.

### Tests that pin the gates

- `tests/test_work_structural.py:298-335`: every verb of the ledger and work parsers is classified in exactly one of `WRITE_POLICY` or `READ_VERBS`, and each write calls `require` first. `:353` `test_an_owner_verb_refuses_a_conversation_and_a_piped_yes`, `:374` `test_a_typed_yes_on_a_terminal_counts_as_routing_does`, `:385` `test_a_from_app_claim_from_a_shell_is_refused_even_with_a_typed_yes`, `:395` `test_same_answer_is_owner_only`.
- `tests/test_routing.py:968` `test_owner_commands_refuse_without_a_typed_confirmation`, `:993` `test_rule_at_owner_request_is_live_without_terminal_confirmation_but_owner_actions_are_not`, `:1171` `test_an_owner_clear_is_timed_after_its_confirmation`, `:2166` `test_owner_requested_rules_are_live_pending_and_owner_can_confirm_undo_or_dismiss`, `:2267` `test_recent_changes_name_the_owner_in_settings_for_every_change_made_in_the_app`, `:2322` `test_every_routing_setting_is_in_the_app`.
- `tests/test_routing_priority.py:236` `test_the_cli_sets_from_the_app_and_a_conversation_sets_pending`, `:515` `test_the_apps_argv_parses_as_the_app_means_it`, `:561` `test_from_app_is_refused_unless_the_app_itself_started_the_command`, `:586` `test_an_owner_line_records_how_it_was_authorised`, `:599` `test_an_owner_write_that_skipped_the_check_shows_as_unchecked`, `:804`, `:820` `test_a_reused_process_number_is_not_the_app`.
- `tests/test_accounts.py:422` `test_owner_writes_need_the_app_or_a_typed_yes`, `:435`, `:685`, `:705` (a conversation's request waits for the owner).
- `tests/test_ledger.py:451`, `:2191`, `:2218` `test_from_app_is_refused_when_the_app_did_not_start_the_call`; `tests/test_ledger_import.py:848` `test_import_is_refused_when_the_app_did_not_start_it_even_without_from_app`.
- `tests/test_promote_offline.py:95-115` (the Update anyway override).

**Count: 16 owner actions behind mechanisms A and B (rows 1 to 16), plus 4 pending-request paths (rows 17 to 20) that exist only because A and B cannot be reached from a conversation. 3 gate functions (`_owner_via`, `typed_yes`, `require`) cover all of them.**

## 2. Design

### The idea in one paragraph

A third way to satisfy the owner check, beside the app and the typed yes: **the owner's own words, recorded by Khipu's own prompt hook from a real user turn**. The agent submits the action together with the owner's words quoted verbatim and its session. Khipu looks those words up in what its UserPromptSubmit hook recorded for that session, from turns the person typed (never from wrapped material), within a recent window, and checks that the words actually ask for this action on this target. If they do, the action is applied at once, stamped `auth: "direction"`, written to an owner-actions log, shown in the app with one-tap Undo, and named in the next routing block. If they do not, nothing changes and the refusal says why in a way the agent can relay ("say it again in your own words").

The protection holds because an agent cannot write into the hook's record. Injected text from a page, a file, a tool or another session never passes through UserPromptSubmit for this session as a typed turn, so it can never supply the words.

### 2.1 What the prompt hook records (new)

Today the hook (`bin/khipu-prompt-recall`, `recall_prompt.prompt_recall_main` `recall_prompt.py:3285`) receives the harness payload (`prompt`, `session_id`, `cwd`, `transcript_path`) and keeps only per-session recall state (`recall--<session>.json`: recent result batches and the routing marker, `recall_prompt.py:151-313`). It keeps no prompt text. The warm path (`bin/khipu-recall-client.py`) answers without ever starting the cold Python path, so recording must happen before the shim branches.

New: a small stdlib-only recorder runs first in the shim (both paths) and appends one line per turn to `<data dir>/owner-turns/<session>.jsonl` (file mode 0600, folder 0700, never synced to the hub, never captured, never searchable):

- `at` (the hook's own clock), `harness`, `session`, `entrypoint` (from the hook process's environment, for example `CLAUDE_CODE_ENTRYPOINT`), `cwd`, a per-turn `turn` id, and `text`.
- `text` is the person's part only, cleaned by the same rules capture already uses: drop `<system-reminder>` and the other `INJECTED_CONTEXT_TAGS` (`session_capture.py:312`), the T3 hand-over (`t3.strip_handoff`, `t3.py:276`), and add `<pasted_content>` and tool-result wrappers to that list. A turn that is a machine message (`MACHINE_USER_TAGS`, `session_capture.py:347`: task notifications, cross-session messages and deliveries, local-command and slash-command wrappers) is recorded as `machine: true` with no text and is never eligible.
- `eligible: false` for a T3 helper session (`t3.is_helper_session`) and for any entrypoint not on the trusted list (2.4).
- Retention: lines older than the longest window (default 24 hours) are dropped on the next write; a session file older than that is deleted by the existing storage-hygiene sweep.

The recorder must never fail or slow a prompt: any error writes nothing and the hook carries on. Budget: under 5 ms, measured in the slice oracle.

**Turn token.** Where the harness lets the hook add context (Claude Code, Codex), the hook adds one short line to its output: `Owner turn: <token>` (random, 8 characters, recorded with the turn). The agent passes the token back with the action. This binds the action to a turn of *this* conversation without trusting the session id (2.6). Where the hook's output is discarded (Aegis, Grok Build), there is no token and the session id alone is the lookup key, with the weaker binding said plainly in the record.

### 2.2 How an agent submits

One new MCP tool, `khipu_owner_act`, and the same thing on the CLI as a flag every owner command accepts: `--owner-words "<verbatim quote>" [--owner-turn <token>]`. The MCP tool takes `{action, args, words, turn}` where `action` is one of the inventory's owner actions (rows 1 to 16) or a confirmation of a pending request (rows 17 to 20).

The session is never taken from the tool's arguments. It is read the way `routing.conversation_author` (`routing.py:1066`) already does, from the calling process's environment (`CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID`, `KHIPU_SESSION_ID`), and for MCP from the server process's environment, which the harness sets when it starts the server. (Slice 1 confirms each harness really passes it to the MCP server; where one does not, the token carries the binding.)

`_owner_via` gains a third branch: no `--from-app`, no TTY, but `--owner-words` present, so the direction check runs. It returns `"direction"` on success and stamps `{"auth": "direction", "harness", "session", "turn", "words"}` through the existing `_OWNER_AUTH`. `ledger.require` gets the same branch for its "owner" verbs. Nothing else in the callers changes: they already pass `via` through to the record.

### 2.3 The check, in order

1. **A recorded turn exists.** The session (and token, when given) names a recorded, eligible turn. Else refuse: `no_recorded_turn`.
2. **Fresh.** The turn is the session's latest eligible turn (any age up to 6 hours, since a long task may act on the turn it is answering), or an earlier eligible turn of the same session no more than 30 minutes old. Else refuse: `stale`. Both numbers are owner settings, liftable in the app and by direction.
3. **Verbatim.** The submitted words, after normalising case, whitespace, straight and curly quotes and trailing punctuation, are a contiguous part of that turn's recorded text, at least 3 words long. No fuzzy matching: a paraphrase is refused with `not_the_owners_words`, and the refusal tells the agent to quote him exactly or ask him to say it again.
4. **The words ask for this action.** A fixed, deterministic table per action (in code, unit-tested), never a model call:
   - a verb word for the action ("pause", "stop using", "hold off" for pause; "lift", "unpause", "resume", "back on" for lift; "rule" plus "add", "new", "make it a rule" for a rule; "priority", "first", "before" for priority; "track", "rename", "stop tracking"; "update anyway", "skip the check"; "hide", "same", "merge", "not the same"; "import"; "restore"; "purge", "delete for good"; "confirm", "yes", "approve" for a pending request), and
   - the target named in the words: the account's label or one of its known aliases, the rule's number, the row id or title words, the check's name, "all" or "everything" when the target is all.
   - For words the action stores (a rule's text, the note, a hold's text), the stored words must be the quoted words or a contiguous part of them. The owner's rule is his sentence, not the agent's rewrite.
   Else refuse: `words_do_not_ask_for_this` with the missing part named ("the words do not name Codex").
5. **Not already used.** Each `(turn, action, target)` is used once. A repeat is answered with the first result, not applied again (`already_done`).
6. **Irreversible actions need two turns.** Only `ledger purge` (row 15) cannot be undone. It needs the words from one turn and an eligible later turn of the same session whose words confirm it ("yes, purge it"). The agent asks; he answers from the phone. No app step.

Jev or any model may never take part in steps 1 to 6: this is a permission decision (the user's standing rule).

### 2.4 Which turns count, per harness

| Harness | Prompt hook today | Can inject the token | Plan |
|---|---|---|---|
| Claude Code (CLI, Remote Control from the phone) | yes, UserPromptSubmit (`integrations.py:724`) | yes | full design |
| Claude Code driven by T3 Code | yes (same hooks, second account included) | yes | full design; T3's hand-over stripped; helper sessions ineligible; threads T3 started by `delegate_task` ineligible (slice 1 finds how T3 marks them) |
| Codex | yes, `hooks.json` UserPromptSubmit (`integrations.py:1637`) | yes, the recall push already rides it | full design; Codex's own prepended context (AGENTS.md, environment) stripped as capture does |
| Aegis | UserPromptSubmit is an observe gate: output discarded (`recall_rule.py`), the shim exits at once for Aegis | no | a recorder-only Aegis hook entry (no output); session id binding only; marked "weaker binding" in the log |
| Grok Build | same as Aegis; Grok is paused | no | same as Aegis, built but not turned on while Grok is paused |
| Grok Bot (cloud, gateway only) | none | no | no direction: its owner actions stay pending requests (rows 17 to 20) |
| A dispatched job (`claude -p`, `codex exec`, a subagent, a workflow agent) | the hook fires, but the "user" turn is another agent's brief | n/a | ineligible: trusted entrypoints are an allow-list (`cli`, the desktop app, Remote Control and the T3 front end); `sdk-cli` and `exec` are not on it |

The last row matters most. A dispatched job's first prompt is written by an orchestrator, often quoting the owner (this very scope's brief does). An orchestrator that wants a dispatched job to take an owner action passes the owner's words, and the check runs against the **orchestrator's** session, where the owner typed them: the job submits `--owner-words` plus `--owner-session` naming its parent, and the parent's record must hold the words. That keeps "the owner said it" true without trusting any brief.

### 2.5 After the check: apply, log, show, undo, report

- **Apply at once**, through the same function the app calls. No pending state.
- **Owner-actions log**: `<data dir>/owner-actions.jsonl`, one line per action: `at`, `action`, `target`, `before` and `after` (enough to undo), `auth` (`app`, `terminal`, `direction`), and for direction the `words`, `harness`, `session`, `turn`, the binding strength (token or session only). Routing changes are also lines in the routing record, as now, stamped `auth: direction`.
- **The app**: a "Done by your direction" list (Settings, Owner actions) with the words, which conversation, when, and one-tap **Undo** per line. Undo applies the inverse from `before`. An undo is itself logged.
- **Undo by chat**: "undo that" or "put Codex back" is an owner direction like any other (the inverse action with its own words check).
- **The next routing block** names it: "Paused Codex by your direction in a Claude Code conversation, 14:02Z (undo in Settings)". `author_words` (`routing.py:1100`) keeps the push free of the session id and project, as now.
- **Pending requests (rows 17 to 20)** stay for conversations that cannot pass the check (Grok Bot, a session with no hook). A direction can confirm one ("yes, keep that rule").

### 2.6 Failure modes

- **A forged session id.** The session comes from the process environment, not the arguments, but a shell call can set `KHIPU_SESSION_ID` itself. With a token, a forged id fails step 1 (the token belongs to another session's turn, and the token is only ever in that session's context). Without a token (Aegis), a forged id can only point at another session's real turn, so the action still needs words the owner really typed there, it is used once (step 5), and the log names both sessions. The residual case: the owner asked for X in session B, and an agent in session A, steered by injected text, applies X first. X is what he asked for; B's agent is told it is done. Acceptable, and stated.
- **A stale prompt.** Step 2. An hour-old "pause Codex" cannot be replayed after he said "lift it" later, because the later turn is the latest and the older one is past 30 minutes.
- **The agent paraphrasing.** Step 3 refuses; the agent quotes him exactly or asks him to repeat. No fuzzy match, so injected text cannot be "close enough".
- **"Do X and Y".** One turn, two actions: the agent submits each with the part of the quote that asks for it (or the whole sentence); step 4 runs per action and step 5 keys on the action and target, so both go through once each. "Pause Codex and Grok" names both targets; "pause everything" needs "all" or "everything".
- **A prompt in another session.** Without `--owner-session`, words from another session's turn fail step 1. With it (2.4, dispatched jobs), the named session must be the caller's recorded parent; slice 3 records the parent link at dispatch time (the delegation hook already sees every delegation call).
- **Pasted text that is not wrapped.** A plain paste into Claude Code reaches the hook as part of the typed prompt, with no wrapper. If the owner pastes a page that contains "pause Codex", the words are in his turn. Steps 3 and 4 still need the action and target, Undo is one tap, and the routing block says what happened. This is the one injection path the hook cannot see; it is named here, not hidden.
- **The hook did not run** (budget, crash, harness update). No record, so refusal `no_recorded_turn`, and doctor gets a row: "owner direction: N turns recorded in the last day per harness", red when a harness with the hook installed recorded none.
- **Prompt text on disk.** The owner-turns files hold what he typed for 24 hours. They are local, 0600, excluded from capture, sync, search, backups and every agent surface (`agent_view`), and named in the storage report.

### 2.7 What stays app-only

Argued, since the owner's default is nothing:

- **Nothing an owner does stays app-only.** Restore (row 14) becomes undoable by direction. Purge (row 15) becomes a two-turn direction (2.3 step 6), still no app step.
- **One exception, recommended: weakening this check.** The trusted-entrypoint list, the wrapper list and the "session-only binding" switch for Aegis decide what counts as his words for every other action. If a direction could widen them, one unwrapped paste could open the door for everything after it. Recommendation: by direction he can turn owner direction **off** or **tighten** it at once; widening it (adding an entrypoint, turning on session-only binding, lengthening the windows past their defaults) is done in the app. This is a recommendation, his call (question 1).

## 3. Slices

Each slice is one session. The oracle for every slice is the hermetic pytest run from `packages/cli` under a temp HOME with `KHIPU_KEYCHAIN=0` (only the named known failure allowed), and `ruff check` unchanged; slices that touch the app add `npx vitest run` and `npm run build` in `apps/desktop`, and Rust changes add `cargo test --offline --lib`. Read-to-act: inspect the files named in section 1 before writing.

1. **Record the turns.** Recorder in the prompt shim before the warm and cold branch; cleaning shared with capture (one stripping path, not two); trusted-entrypoint list; T3 helper and delegated-thread exclusion; turn token in the push for Claude Code and Codex; Aegis recorder-only entry; retention; doctor row. Find out, and pin in a test, which harnesses pass the session id to the MCP server's environment. Oracle: tests feeding recorded hook payloads for each harness and wrapper (`<system-reminder>`, `<pasted_content>`, cross-session message, task notification, T3 hand-over, `sdk-cli` entrypoint) and asserting what is recorded and what is eligible; a timing test that the recorder adds under 5 ms. Stop if a harness's payload shape cannot be confirmed from fixtures already in the repo: ask, do not guess.
2. **The check and the log.** `owner_direction.check()` (steps 1 to 6), the action table, the `"direction"` branch in `_owner_via` and `ledger.require`, `--owner-words` and `--owner-turn` on every owner command, the owner-actions log with `before` and `after`. Update the enumerating tests so every row of section 1 is reachable by direction and each refusal code is tested. Oracle: a table-driven test with one passing and one refused case per action, and every failure mode of 2.6 as a named test.
3. **MCP and dispatched jobs.** `khipu_owner_act`; `--owner-session` with the parent link recorded by the delegation hook; confirming pending requests by direction; tool description that tells agents to quote, never paraphrase. Oracle: tests through `mcp_server` with a fake environment per harness, including a forged session id and a parent that never typed the words.
4. **The app and the push.** Settings, Owner actions: the list, Undo per line, the off and tighten switches, the windows. The routing block line. Oracle: vitest and build, cargo tests, and a rendered check of the new section (a second-account job, screenshot to a file) per the UI rule.
5. **Turn it on.** The switch defaults off until slice 4 is verified, then on. Oracle: an end-to-end test in a temp HOME: a recorded fake Claude Code turn "pause Codex until 5pm", an agent call with those words, the record paused, the log line, the routing block line, an Undo, the record lifted.

## 4. Decisions (Matt, 2026-10-16)

Matt approved the design and answered the questions. These replace section 2.7's recommendation and step 6 of 2.3 where they differ.

1. **Changing the check itself works by chat; nothing needs the app.** Turning owner direction off, or tightening it (shorter windows, removing an entrypoint, turning off session-only binding), works by direction in one message. Widening it (for example trusting a new front end as the owner, lengthening a window, turning session-only binding on) also works by direction, but needs a second turn of the same session whose words confirm it ("yes"), the same two-turn rule as step 6.
2. **Front ends that count as the owner**: Remote Control and T3 Code, as the doc assumed, alongside an ordinary Claude Code or Codex session the owner types in. Dispatched jobs stay ineligible.
3. **Purge is one message, undoable for 24 hours.** A purge by direction needs one turn. It does not run at once: it is scheduled to run a day later, and Undo (in the app or by chat) cancels it until then. Step 6's two-turn rule applies only to widening the check (decision 1).
4. **Windows stand**: 6 hours for the session's latest owner turn, 30 minutes for an earlier one. Both are changeable in the app and by chat (shortening is a tightening, lengthening a widening).
5. **Aegis and Grok Build are allowed every owner action** on the weaker session-only binding, the same as other harnesses. The owner-actions log records which binding was used (`token` or `session`).
