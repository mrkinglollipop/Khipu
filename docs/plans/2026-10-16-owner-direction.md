# Owner direction through the agent (scope, 2026-10-16)

Status: section 2 is the decided design (Matt, 2026-10-17); it replaces the word-matching design of 2026-10-16 and the code-reply proposal of 2026-10-17. Section 1 is the inventory it applies to. Owner requirement (Matt, 2026-10-16):

> "I need to be able to do any changes in Khipu through you with my direction. I can't play games trying to manually confirm something when I'm not in front of my computer."

He directs agents from his phone. Today every owner action needs the Khipu app or a `yes` typed on an interactive terminal, so from the phone he can only ask, and the change waits (or never happens) until he is back at the Mac.

What the gates exist for, and what this design must keep: **an agent must never take an owner action on its own**: a background job, a dispatched job, or a conversation nobody is typing in. A conversation the owner is directing must be enough, with nothing for him to remember or type beyond the request itself.

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

## 2. Design (decided, Matt, 2026-10-17)

> "What is the point here? I'm never going to remember this code. Why do I need a code to make a change? We're not dealing with nuclear secrets here."

The simplest design that meets the requirement. It replaces both the word-matching design (the owner's words quoted, looked up in the recorded turn and parsed for the action and every argument) and the code-reply proposal (a single-use code the owner sends back). Neither exists any more.

### 2.1 How a change is made

An agent makes an owner change on Matt's direction directly: it submits the structured action through the MCP tool `khipu_owner_act` (`{action, args}`) or with `--owner-direction` on the owner command (rows 1 to 20 of section 1), and it applies at once, through the same function the app calls. There is no code, no confirmation turn, and no matching of his words. Confirming a pending request (rows 17 to 20) is an ordinary action of the same kind.

### 2.2 The one check: a conversation the owner typed in recently

The request must come from a session where Matt has typed recently: Khipu's own prompt hook recorded an eligible owner turn in that session within the window (`latest_window_h`, default 6 hours, a setting in the app and by direction). That blocks background jobs, dispatched jobs and unattended sessions from making owner changes on their own.

- **The session** is read from the calling process's environment (`KHIPU_SESSION_ID`, `CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID`), never from the arguments. A call whose environment names two different sessions (a job that kept its parent's id beside its own, one front end hosting two conversations) is refused (`no_recorded_turn`).
- **Eligible turns** are the ones the prompt hook recorded from a trusted front end (`claude_code:cli` with Remote Control, `claude_code:t3`, the Codex front ends, `codex:t3`, `aegis:aegis`), holding typed words, and not a machine message, a wrapper away from its place, a T3 helper session, or a dispatched job's brief (the delegation hook notes every brief it hands on). `claude -p` and `codex exec` are not trusted front ends, so a dispatched job has no eligible turn and is refused (`no_recorded_turn`).
- **Fresh**: the session's latest eligible turn is inside the window, else `stale`. The refusal says the change works again once the owner sends a message in that conversation.
- **From this conversation's hook**: the turn names the process that ran the hook (its issuer). The recorder's parent must be the prompt shim, and the shim's parent the harness, with nothing between but a harness's own `sh -c <shim>`; a recorder or shim run from an agent's Bash tool finds the agent's shell there and records a refused issuer. The issuer must then be the nearest harness above the call (the first ancestor that is not a shell, an interpreter or a launcher such as `env`), else `not_from_the_hook`. A dispatched job (`claude -p`, `codex exec`, a T3 delegate) runs its own harness process, so it never acts on its parent's turn even with the parent's session id, and a session id set by hand in another process does not pass.
- **Aegis and Grok Build** keep the weaker session check Matt allowed: their hook output is discarded, and the turn is matched by session with the same issuer test. The switch `session_binding` turns this off; the log records the binding (`session` or `hook`).
- No model takes part (the user's standing rule for permission decisions).

### 2.3 What the prompt hook keeps

Only what the check needs: one line per turn with its time, harness, entrypoint, whether it held typed words, whether it was a machine message, a wrapper, a T3 helper or a dispatched brief, and the hook's issuer. **The text is not kept**, and neither is a delegation brief's: a brief is kept as a hash of its words and a hash of its first 200 characters (so a turn that holds it after other words is still known), with its parent session; a dispatched job's turn names the brief's hash, and forgetting the job's session removes that brief. Files stay local, 0600 in a 0700 folder, never synced, captured, searched or backed up, and lines go after 24 hours (on the next write or read, and on the replica-refresh timer). Forgetting an episode deletes its session's turn files and briefs and scrubs any words left in owner-actions lines written under the old design (each keeps its hash, so the chain holds). The turn token line ("Owner turn: ...") is no longer pushed.

### 2.4 After the change: log, show, undo, report

- **Owner-actions log**: every owner change, by direction, in the app or on a terminal, is a line in `<data dir>/owner-actions.jsonl` with `before` (enough to undo), `auth`, and for direction the harness, session, turn id and binding. The log is hash-chained and append-only; a break (a changed line, lines cut from its end) raises a persistent tamper alarm kept in its own file until the owner acknowledges it (an owner action, logged, with an Undo that brings the alarm back). A direction is applied only when its line, with its Undo, could be written first.
- **The app**: Settings, Owner actions lists every change with how it was made, when, and Undo per line.
- **The next routing block** names every owner change once, however it was made. A change the routing record holds is named from its own line; any other (an owner-direction setting such as the window, a ledger or Work view change, Update anyway, an Undo) writes one `owner_change` line to the routing record with fixed words, never its target.
- **Undo survives a lost finish**: everything Undo needs is on the line written before the change. A rule or an owner hold whose number or id was never recorded is found as the one that was not there before; a purge by direction carries its day on that line, so it stays cancellable until then and runs on its day.
- **An import** by direction has an Undo: it removes (restorably, children first) the rows that import added and marks the import undone. A catch-up import changes rows already there, so it has no Undo and is refused by direction (`not_by_direction`).
- **Purge** (row 15) keeps its 24-hour delay: by direction it is scheduled a day out and Undo cancels it until then; from the deadline on, Undo refuses.
- **Undo by chat** is the action `direction.undo` with the line's id, through the same check.

### 2.5 Changing the check itself

Widening it (trusting a new front end, lengthening the window, turning session-only binding on) and turning owner direction on are ordinary owner changes: one request from a conversation that passes the check, logged with an Undo. Turning it off or tightening it is the same. While it is off, only `direction.on` goes by direction; the hook keeps recording turn times so that request can pass.

### 2.6 Known boundary: the same Mac user

Khipu and the agents run as one Mac user. A program running as that user can write the owner-turn files or the log, or run the hook's shim with `exec` from a bash shell so that the shim's parent looks like a harness. File modes and process checks cannot separate two programs of the same user. Matt accepted this limit on 2026-10-16: every owner action is tamper-evident (the hash chain and alarm) and visible (Owner actions, the routing block), with Undo.

### 2.7 Removed

The word parser and the per-action verb and name tables, argument coverage as an authoriser, the turn token, the earlier-turn window, the two-turn rule and bare-yes widening, the waiting-widenings file (an old one is deleted by the timer and by forget), the parent-session link for dispatched jobs (`--owner-session`), and the code reply. Sections 3 to 5 below are the history of the earlier design, kept for the record; where they differ, this section wins.

## 3. Slices

Each slice is one session. The oracle for every slice is the hermetic pytest run from `packages/cli` under a temp HOME with `KHIPU_KEYCHAIN=0` (only the named known failure allowed), and `ruff check` unchanged; slices that touch the app add `npx vitest run` and `npm run build` in `apps/desktop`, and Rust changes add `cargo test --offline --lib`. Read-to-act: inspect the files named in section 1 before writing.

1. **Record the turns.** Recorder in the prompt shim before the warm and cold branch; cleaning shared with capture (one stripping path, not two); trusted-entrypoint list; T3 helper and delegated-thread exclusion; turn token in the push for Claude Code and Codex; Aegis recorder-only entry; retention; doctor row. Find out, and pin in a test, which harnesses pass the session id to the MCP server's environment. Oracle: tests feeding recorded hook payloads for each harness and wrapper (`<system-reminder>`, `<pasted_content>`, cross-session message, task notification, T3 hand-over, `sdk-cli` entrypoint) and asserting what is recorded and what is eligible; a timing test that the recorder adds under 5 ms. Stop if a harness's payload shape cannot be confirmed from fixtures already in the repo: ask, do not guess.
2. **The check and the log.** `owner_direction.check()` (steps 1 to 6), the action table, the `"direction"` branch in `_owner_via` and `ledger.require`, `--owner-words` and `--owner-turn` on every owner command, the owner-actions log with `before` and `after`. Update the enumerating tests so every row of section 1 is reachable by direction and each refusal code is tested. Oracle: a table-driven test with one passing and one refused case per action, and every failure mode of 2.6 as a named test.
3. **MCP and dispatched jobs.** `khipu_owner_act`; `--owner-session` with the parent link recorded by the delegation hook; confirming pending requests by direction; tool description that tells agents to quote, never paraphrase. Oracle: tests through `mcp_server` with a fake environment per harness, including a forged session id and a parent that never typed the words.
4. **The app and the push.** Settings, Owner actions: the list, Undo per line, the off and tighten switches, the windows. The routing block line. Oracle: vitest and build, cargo tests, and a rendered check of the new section (a second-account job, screenshot to a file) per the UI rule.
5. **Turn it on.** The switch defaults off until slice 4 is verified, then on. Oracle: an end-to-end test in a temp HOME: a recorded fake Claude Code turn "pause Codex until 5pm", an agent call with those words, the record paused, the log line, the routing block line, an Undo, the record lifted.

## 4. Decisions (Matt, 2026-10-16; superseded by section 2 on 2026-10-17)

Matt approved the earlier design and answered its questions. Decisions 1 and 4 (two turns for a widening, the 30-minute earlier window) are superseded by section 2; 3 (purge delay) and 5 (Aegis and Grok Build) stand as section 2 restates them.

1. **Changing the check itself works by chat; nothing needs the app.** Turning owner direction off, or tightening it (shorter windows, removing an entrypoint, turning off session-only binding), works by direction in one message. Widening it (for example trusting a new front end as the owner, lengthening a window, turning session-only binding on) also works by direction, but needs a second turn of the same session whose words confirm it ("yes"), the same two-turn rule as step 6.
2. **Front ends that count as the owner**: Remote Control and T3 Code, as the doc assumed, alongside an ordinary Claude Code or Codex session the owner types in. Dispatched jobs stay ineligible.
3. **Purge is one message, undoable for 24 hours.** A purge by direction needs one turn. It does not run at once: it is scheduled to run a day later, and Undo (in the app or by chat) cancels it until then. The replica-refresh job's timer runs it once its day has passed. From that deadline on Undo refuses, saying the deadline passed (or that the purge already ran), whether or not the timer has run it yet. Step 6's two-turn rule applies only to widening the check (decision 1).
4. **Windows stand**: 6 hours for the session's latest owner turn, 30 minutes for an earlier one. Both are changeable in the app and by chat (shortening is a tightening, lengthening a widening).
5. **Aegis and Grok Build are allowed every owner action** on the weaker session-only binding, the same as other harnesses. The owner-actions log records which binding was used (`token` or `session`).

## 5. Rules added after the third review (2026-10-17; superseded by section 2)

Rules 1, 3 and 4 belonged to the word-matching design and are gone. Rule 2 (the tamper alarm stays until acknowledged) stands.

The third cross-family review found seven gaps of one shape: words that named part of an action were taken to authorise all of it. These rules close the class.

1. **Argument coverage.** An owner direction authorises a structured action only when every argument of that action is stated in the owner's own words, in the clause that asks for it: the action (its verb), what it acts on (an account, a rule, a setting, a file, a row and the row's project, a front end as its harness and its entry point), the field it changes, the value it gives, the end a timed change carries, the order of a list and the side a switch is turned to. Each is matched exactly or by the synonyms documented in `owner_direction.py` (the verbs in `ACTIONS`, the names in `_names`, `FIELD_NAMES`, `_FRONT_END_NAMES`). An argument the words do not state is refused with a plain message naming it ("the owner's words do not say which project (atlas)"). One function lists the arguments of every owner action (`arguments`) and one checks them (`_read`); a test drops each argument from every action's passing words and expects a refusal. A verbless clause carries arguments for the verb before it when arguments are all it holds ("make the latest window 12 hours and the earlier window 60 minutes"); a value belongs to the clause that names what it changes.
2. **The tamper alarm stays until the owner acknowledges it.** A break found in the owner-actions log (a changed line, lines cut from its end) is kept in a file of its own beside the log. A later append, which rewrites the head, never clears it. The owner acknowledges it in the app (Settings, Owner actions, Acknowledge) or by chat ("acknowledge the tamper alarm"); acknowledging is an owner action, logged, with an Undo that brings the alarm back.
3. **Turning owner direction on is an owner action.** It is logged with its Undo however it is made. By chat it is a widening, so it takes decision 1's two turns. While owner direction is off, the prompt hook records only the turns that could turn it back on (one that names owner direction, then a short yes soon after); everything else the owner types is not recorded, and no other action goes by direction. Delegation briefs are noted while it is off too, so a brief never passes for the owner's turn.
4. **Naming one of several waiting widenings.** With more than one widening waiting from the same turn, a confirmation that names one ("yes to trusting T3", "yes, make the earlier window 60 minutes") selects it; a bare "yes" is refused and asks which. Any number the confirmation gives must be the waiting widening's.

