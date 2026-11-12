# Khipu carries the rules and the owner's hooks to every app

Status: scope for Matt, 2026-11-12 (back-to-roots item 3a, `docs/plans/2026-11-12-back-to-roots.md`). Nothing here is built. Decisions that are Matt's own are listed at the end, each answerable in one word.

Matt: "we needed a place to keep our rules, and a central place for hooks ... It would have to go through Khipu." And: "How does this work for people who don't have Claude?"

## The shape in three sentences

Khipu holds the rules as its own record and a short list of the owner's hooks, both in the hub every Mac already reads, with every version kept. Each Mac's Khipu writes them into each connected app through the same install path that writes Khipu's own hooks today (`integrations.install`), into a marked block or a Khipu-owned file, never over what the owner or the app wrote. `~/.claude/scripts/sync-rules.sh` retires; `~/.claude/CLAUDE.md` becomes one more copy Khipu writes.

## 1. What Khipu holds, and where

- **The rules**: one text, versioned. Stored in the hub in one new table (`owner_config`: kind `rules` or `hook`, name, version, body, settings, who changed it and why, time). Every Mac of the owner reads the hub already, so the rules follow the owner with no ssh and no second Mac needed. A one-Mac user's hub is that Mac.
- **Per-app extras**: text for one app only, appended inside that app's block (today's 7-line Codex addendum becomes Codex's extra).
- **The owner's hooks**: a short list. Each has a name, the script's text, the event (in Claude Code's names, mapped per app the way `_CURSOR_IMPORT_EVENTS` already maps them), an optional matcher, which apps, and which Macs ("all" or named). Each Mac writes the script to `~/.config/khipu/hooks/<name>` and wires it into each chosen app.
- **A local copy** of the current rules at `~/.config/khipu/rules.md` on every Mac: the target for any app that can only be pointed at a file, and what the check in 5a compares against.
- **Not carried**: a project's own hooks and rules. They live in the repo and travel by git (each repo owns its conventions).
- `~/.claude/CLAUDE.md` (and each other Claude home, such as `~/.claude-t3-second/CLAUDE.md`, which Claude Code loads for that account: seen in this session's own context) becomes a file Khipu writes a block into, like the others.

## 2. A user with no Claude

Nothing above names Claude. The source is the hub record; Claude Code is one app among six. Someone with only Codex gets the rules in `~/.codex/AGENTS.md`; only Cursor, by session-start text; any mix, each by its own channel. No step needs T3 Code, ssh, a Claude file or a second Mac. Another person runs their own Khipu with their own hub and rules; shared team rules are not proposed.

## 3. Three moments

**Setting up, rules already present.** On connecting each app, Khipu reads its rule files and hook list and shows them (section 4): "Claude Code: CLAUDE.md, 160 lines. Codex: AGENTS.md, 12 lines, 2 hooks of its own." Khipu writes nothing until the owner picks: "use Claude Code's rules everywhere", "combine them" (both texts, each under its app's name), or "leave each app as it is" (Khipu then holds no rules). The same choice works by telling any agent ("use my Claude rules everywhere"), which records it through owner direction. Hooks found in an app are offered one by one: "carry this to your other apps and Macs?"

**Setting up, no rules anywhere.** Khipu shows an empty rules page with one line: "Rules you write here reach every app Khipu is connected to." Nothing is written to any app while the rules are empty.

**Changing.** In the app: edit the text, save. By chat: the owner tells any agent; the agent calls `khipu_owner_act` with `rules.set` (a whole text or one replaced paragraph), on his typed words, which is how routing changes work today. Either way: a new version in the hub, then the Mac it was made on writes every app at once; every other Mac writes within 5 minutes on the existing `com.khipu.queue-drain` tick (it already runs every 5 minutes on every Mac; no new job). The echo line says what changed and which apps have it. Hooks change the same way (`hooks.add`, `hooks.edit`, `hooks.remove`).

**Moving.** A new Mac joins through the existing "Add another Mac" flow. Its first tick finds rules and hooks in the hub, reads what its apps already hold (outside Khipu's blocks, untouched), writes, and shows one line: "Your rules and 5 hooks are now in Claude Code and Codex on this Mac." Codex still asks its owner to review new hooks on that Mac. An offline Mac catches up on its first tick back. If a Khipu block was edited by hand meanwhile, Khipu does not overwrite it: "CLAUDE.md on M1 was edited outside Khipu. Make that edit the rules everywhere, or replace it with Khipu's?"

## 4. Other apps' own rules and hooks

- Khipu reads, never deletes, and never overwrites what it did not write. It knows its own writing by the block markers and by the hash of what it last wrote to each file on each Mac.
- **Side by side**: where an app loads a folder of rule files, Khipu writes its own file (Antigravity: the `~/.gemini/config/rules/khipu.md` file Khipu already writes gains the rules). Where an app loads one file, Khipu writes one marked block (`<!-- khipu: rules v12, edit in Khipu -->` ... `<!-- khipu: end -->`) and leaves everything outside it alone. The block helper is today's Grok Build one (`_replace_grok_build_agents`), made general, not a second one.
- **The same rule in two apps**: Khipu compares paragraphs by exact text (whitespace ignored, no model). A paragraph found in two apps' own text is offered once: "This is in Claude Code and Codex. Use it everywhere?" Yes adds it to the rules and offers to remove the copies outside the blocks; nothing is removed without that yes. Near-matches are not detected, and the page says so.
- **Hooks**: each app's own hooks are listed under "Not Khipu's" with their event and command (today: the four guards, the Olivewood guard and the Aegis toolchain lease in Claude Code; `oracle_cap_guard.py` and `ruff_postwrite.py` in Codex). Adopting one makes it an owner hook; the entry is repointed to Khipu's copy of the script, with a backup.
- **Migrating the script's own copies**: `~/.codex/AGENTS.md` and `~/.gemini/GEMINI.md` were written wholesale by `sync-rules.sh`. A file whose text is exactly the script's last build (the rules, plus the addendum or the script's header line) is replaced by Khipu's block or emptied into Khipu's rule file, with a backup. Anything else in it is the owner's and stays.

## 5. Per app

| App | Rule file it loads (format) | Hook events | Khipu can write | Khipu cannot, and what the app says |
|---|---|---|---|---|
| Claude Code | `<home>/CLAUDE.md` per Claude home, markdown, at session start (checked: this session) | SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, SubagentStart/Stop, Stop, PreCompact, SessionEnd (as `integrations.py` uses them) | Marked block in each home's `CLAUDE.md`; hooks in each home's `settings.json` | Nothing blocked. |
| Codex | `~/.codex/AGENTS.md`, markdown (Codex docs; used here since 5 Nov) | The same names in `~/.codex/hooks.json` (Khipu's code and the M5's file) | Marked block in `AGENTS.md`, Codex's extra inside it; hooks appended at the end of each event list, never reordered | Trust: "Codex runs a new or changed hook only after you review it in Codex." (`CODEX_REVIEW_STEP`; state read by `codex_hook_trust.ask`). `config.toml` is never touched. |
| Cursor | No global file: User Rules live in Cursor's own settings (`_cursor_install` comment); project `.cursor/rules/*.mdc` and `AGENTS.md` are per repo | sessionStart, beforeSubmitPrompt, pre/postToolUse, subagentStart/Stop, stop, preCompact, sessionEnd and more (`~/.cursor/hooks.json`). Cursor also runs the hooks in `~/.claude/settings.json` (its Claude import, on by default; the CLI cannot opt out) | Session-start text through Khipu's recall hook; hooks in `~/.cursor/hooks.json` only on a Mac where Claude Code does not already carry them (else they run twice, as recall did on 27 Oct) | User Rules: "Cursor keeps its global rules in its own settings. Khipu can't write them." Cap: 10,000 characters of session-start text. T3 threads: unchecked (5 Nov test said no start text reaches them; 27 Oct logs show the hook runs). |
| Antigravity | `~/.gemini/GEMINI.md`, `~/.gemini/config/rules/*.md` (`trigger: always_on`), truncated past 24,000 bytes (docs, per the 27 Oct scope) | SessionStart, PreInvocation, PostInvocation, Stop, PreToolUse, PostToolUse | The existing `rules/khipu.md`; hooks under Khipu's key in `config/hooks.json` | No tool-blocking hooks: "Khipu doesn't add tool-blocking hooks to Antigravity: registering one turns off its own permission prompt." T3's Antigravity keeps a private profile: not reached (lane unused). Nothing has been run by Antigravity itself yet (slice H). |
| Grok Build | Project `AGENTS.md` (Khipu writes a marked block there today). A global file: unchecked | Stop, PreCompact, SessionEnd in `~/.grok/config.toml` (others unchecked) | Owner hooks in its managed TOML block | Rules: "Grok Build has no global rule file Khipu knows of; Khipu won't write your rules into project files." (They would land in git.) |
| Aegis | Unchecked | Stop, PreCompact, SessionEnd, UserPromptSubmit (output discarded); hooks run sandboxed | Hooks in its managed block, only scripts that work in its sandbox | Rules: "not reached" until Aegis names a rule file. Aegis stays native-only. |

### 5a. How the rules reach the model

Matt asked whether a rule in a file is even read. It is: Claude Code, Codex and Antigravity put the file into the model's context at session start, the same place a hook's injected text lands. Injected text is no more binding than a file. So each app gets exactly one channel, from the one record:

| App | Channel | A running session picks up a change |
|---|---|---|
| Claude Code | `CLAUDE.md` block | Next session. After a compaction or mid-session: unchecked. Hooks: at once. |
| Codex | `AGENTS.md` block | Next session. Hooks: after the owner's review in Codex. |
| Antigravity | `rules/khipu.md` | Next session (unchecked). |
| Cursor | Session-start text from Khipu's existing recall hook, rules first, the memory note in what is left | Next session. If the rules pass the 10,000-character cap (today's file is roughly 15,000 by eye; slice 1 measures), Cursor gets one line pointing at `~/.config/khipu/rules.md` instead, and the check says so. If T3's Cursor threads prove to get no start text, the app shows a one-line User Rule to paste once into Cursor's settings: the one exception, because nothing else reaches them. |
| Grok Build, Aegis | None yet | Shown as "not reached". |

The Fable contract needs no hook of its own: its text is in the rules, and the rules already reach every app above by one channel.

**"Does this app have the current rules?"** The block's first line carries the version and date ("Rules v12, 12 Nov"). For a file channel Khipu compares the block with the hub on each tick; for Cursor the recall hook records which version it injected. The app shows per app: current, older (v11), edited by hand, missing, or not reached. `khipu_status` returns the current version, so any agent can compare it with the line in its own context.

**Reading is not obeying.** A rule that would do real harm when missed earns a blocking hook, as the four guards do today (a subagent names its model, a workflow names its model, no waiting on a `pgrep -f` match, no `npm audit` upload). Three more are candidates, each for Matt's yes: no release or publish command without his ask (public harm, the hardest line), no reading a file under `API Keys/` (a leaked key), no edit of `~/.codex/config.toml` (breaks Codex's hook trust). Everything else stays as text.

## 6. Per-app and per-Mac extras

- Text for one app: an extra kept beside the rules and written inside that app's block only (Codex today).
- Claude-only files the script also copies (`~/.claude/agents/*.md`, `~/.claude/commands/*.md`, the T3 guide) ride as Claude Code extras: plain files copied to each Claude home on each Mac.
- A hook for one Mac: its Macs field names it (the Olivewood guard and the Aegis toolchain lease are M5-only today). A hook for one project stays in that repo.

## 7. Undo

Every write keeps the previous file next to it (`_backup`, `<file>.bak-khipu-<time>`), keeping the last ten per file. Every version of the rules and of each hook stays in the hub. One action, "Undo last change" in the app or `rules.undo` / `hooks.undo` by chat, restores the previous version, and every app and Mac follows on the next tick. "Put this file back as it was before Khipu" restores the oldest backup of one file.

## 8. What the app shows

On the regrouped Settings' **Connections** page (mock `docs/plans/mocks/2026-11-12-settings-regrouped.html`), a group "Rules and hooks" with two rows: **Rules** ("v12, in 4 of 6 apps", Open) and **Hooks** ("6 hooks", Open). Each app row on the same page gains one word for its rules state (current, older, by hand, not reached). Two sheets: the rules sheet (the text, the per-app extras, versions with Undo, and what was found in each app with "use this everywhere?"), and the hooks sheet (each hook with its apps and Macs ticked, Codex's review state, and "Not Khipu's" hooks with "carry this"). Nothing else. A mock comes first and needs Matt's yes.

## 9. What replaces what

Retired: `sync-rules.sh`, `codex-addendum.md` (now Codex's extra), `scripts/generated/`, the ssh copy to the M1, the bootstrap copy of `CLAUDE.md`, and hand-wiring the four guards on each Mac. The script is moved to an archive folder, not deleted. Added: one hub table, one small module for the record (`owner_config.py`), a rules-and-hooks step inside each app's existing install function, a step on the existing queue-drain tick, and the owner-direction actions `rules.set`, `rules.adopt`, `rules.undo`, `hooks.add`, `hooks.edit`, `hooks.remove`, `hooks.adopt`, `hooks.undo` (named apart from the routing `route.rule.*` actions). No second install path, no new job, no new MCP tool, no signing or approval steps beyond Codex's own.

## 10. Slices

Test command for every Python slice, from `packages/cli` (never the real home): `S=$(mktemp -d) && HOME="$S" KHIPU_KEYCHAIN=0 env -u KHIPU_DATABASE_URL -u ALZY_DATABASE_URL -u GEMINI_API_KEY PYTHONDONTWRITEBYTECODE=1 python3.11 -m pytest -q -p no:cacheprovider tests/<files>; rm -rf "$S"`, with the known baseline failures named, not new ones. Desktop slices add `npx vitest run && npx tsc --noEmit && npx vite build` in `apps/desktop`. Every slice: stop and report if it would write a file outside Khipu's markers, touch `~/.codex/config.toml`, or need a second install path.

1. **Read and show (read-only).** Per app, a reader beside each `_status` in `integrations.py`: rule files found, their size, Khipu's block if any; hooks listed as Khipu's or not. Shown by `khipu doctor` and `khipu rules show`, and in `status_all` for the app. Measures today's rules against each cap. Stop if any reader would need to write, or if a file can't be parsed (show "can't read", never crash).
2. **The record.** `owner_config` table and migration, `owner_config.py`, the owner-direction actions for rules (`set`, `adopt`, `undo`), versions. No app writes yet. Stop if the hub migration touches any existing table.
3. **Rules into file apps.** The general marked-block helper; Claude Code (every home), Codex (with its extra), Antigravity's `khipu.md`; hand-edit detection; backups kept to ten; the queue-drain tick step; the local `rules.md`. Stop if a fixture's text outside the markers changes by one byte.
4. **Rules into Cursor, and the check.** Rules first in the recall hook's start text with the cap rule; the version record; per-app current/older/by hand/missing/not reached. One real Cursor run in T3 with a canary rule to settle whether T3 threads get start text; read the log, report. Stop if the memory note would be dropped entirely for apps that already get it today.
5. **Owner hooks.** Scripts to `~/.config/khipu/hooks/`; wiring per app; Cursor skipped where Claude's import already runs it; Codex append-only with the review state shown; no tool-blocking hooks in Antigravity; `hooks.*` actions; adopting an app's own hook. Stop if a Codex write would reorder existing entries.
6. **Move Matt's setup over (needs his yes).** Adopt `~/.claude/CLAUDE.md`, the addendum, the four guards (all Macs), the Olivewood and Aegis hooks (M5 only), and the Claude extras; replace the script's built copies; let the M1 catch up; confirm both Macs show "current" everywhere; archive the script. Stop if any app shows "by hand" or "older" after the M1's tick.
7. **The app (after the mock is approved).** The Connections group and the two sheets. Proof is the rendered app, not a green build.

## 11. Open questions for Matt

1. Build this, replacing `sync-rules.sh`? (yes/no)
2. Make today's M5 `~/.claude/CLAUDE.md` the rules, and its first paragraph say "edit the rules in Khipu, in the app or by telling any agent"? (yes/no)
3. Add the three guards in 5a (releases, key files, Codex config)? (all/none/name them)
4. Trim the rules under 10,000 characters so Cursor gets the whole text, rather than a pointer? (yes/no)
