# Routing truth: one live record of which accounts are open, pushed to every conversation

Asked by Matt 2026-10-14 ("Yes, scope it"; "Khipu does not route, it reports. It's just the single source of
truth that the conversations can use, and it just makes it easier for everybody to know what's up";
"Dynamic and live routing is certainly the point"; the background check at reset times approved). This page
is the scope; a build session reads it first. It builds on plan usage (`2026-10-14-plan-usage.md`: the
probes and the cache), and every phase reaches this Mac only through the promotion gate
(`2026-10-14-promotion-gate.md`). It was revised the same day after a review of its claims, of the owner's
rules, of how live the result is and of the mocks; the corrections are folded in below.

The assessment behind it (2026-10-14: five read-only readers, three designs, three judges) scored the
account-state design best (8, 8 and 5). This scope is that design with the judges' grafts. The other two
designs were set aside: moving a project's job runner into Khipu would make Khipu route, and pushing
the hand-written table as it is would keep an unlocked, hand-edited file as the only truth.

**What Matt gets, in plain words.** Every conversation on this Mac that Khipu can reach learns that an
account has hit its limit, has reset, has been paused by him, is down, or that Khipu does not know, before it
next decides where to send work. Today it learns by failing, by restarting, or by Matt typing it into each
window. His standing rules and his own note on where work should go travel first and whole, in his words. He
can see and change all of it in the app. Khipu never decides where work goes.

## Status

| Phase | What | Size | State |
|---|---|---|---|
| 0 | This scope; the mocks of the Home Routing card and Settings → Routing; a throwaway-hook check of five hook facts | S | done 2026-10-14 (scope and mocks approved; hook check: five of five yes, see Phase 0 results) |
| 1a | The record and its fold: event log, versioned view with clock-driven refolds, rules, Matt's note, holds, owner clear; `khipu route`; the doctor block | M | done 2026-10-14 (one known limit, see Known limits; its fix is in phase 1b) |
| 1b | The sources: read-only runner adapter, lane map, usage-cache source, table pointer | S–M | done 2026-10-14 (PRs #186 and #187) |
| 2 | MCP `khipu_route` and `khipu_route_report`; `routing` in `khipu_status`; `khipu route wait` | S | done 2026-10-14 (PR #188) |
| 3a | The full summary at session start and after compaction; kill switches; the `routing_push` feature and its app parity | S | done 2026-10-14 (PR #189) |
| 3b | Changes on every prompt inside their own time budget, with confirmed delivery | M | done 2026-10-15 (PR #190) |
| 3c | The daemon refolds on change and on the clock; the promotion gate's replay covers routing | S | done 2026-10-15 (PR #191) |
| 4 | App: Home Routing card, Settings → Routing for what phases 1a to 3c built, health row, attention items | M–L | done 2026-10-15 (PR #192) |
| 4b | The owner's priority and the spend pace, with their part of the app | S–M | built, in review (branch feat/routing-priority) |
| 5 | The background check at each known reset time, with its part of the app | S | built, in review (branch feat/routing-truth-5) |
| 6 | Push at the moment of delegation, with its part of the app | S–M | built, in review (branch feat/routing-truth-6) |
| 7 | Copy for cloud agents through the hub, read by cloud harnesses and a second Mac, with its part of the app | M | open |
| 8 | Cutover: the NOW block of `ROUTING-NOW.md` becomes a generated view; the old SessionStart hook is narrowed or retired | S per step | needs Matt's go |

One PR per phase, in order. Phases 1a to 3c are the fix Matt asked for; each later phase stands on its own.

## The defect, in one paragraph

The account state every orchestrator routes on lives in one hand-edited markdown file, `ROUTING-NOW.md` in
the owner's routing folder, and reaches conversations only through a Claude Code SessionStart hook
(`~/.claude/hooks/routing_now_inject.py`) that runs once per session, ignores whether the session is new,
resumed or compacted, and keeps the first 6,000 characters (`:31`). On 2026-10-14 the NOW block measured
9,278 characters, so the cut fell inside the disk hold and every session lost the rest: the Grok builder
verdict, the line "Never a -fast tier; never the paid xAI API (GROK_DISABLE_API_KEY_AUTH=1); nothing through
Cursor" (it starts at character 6,321), the machine-load hold, the Muse hold and the bottleneck line. Codex
and Grok Build get a static pointer to the table in the instruction files they load, not its contents; what
Muse reads has not been checked. The best live evidence often never reaches the part of the file that is
injected: a job runner can mark an account limited without the table ever hearing of it, and when an
orchestrator does write a limit into the table's change log, the NOW block, which is what the hook injects,
can go on saying the account is open. Reset times are typed by hand into throwaway relaunch scripts that
sleep until a literal time, which cannot move when a reset moves; one had to be stopped by PID when a weekly
pause was found. A running conversation learns a limit by hitting it, by restarting, or by Matt retyping it.

Dated evidence lives in the private ops notes.

## Source facts (2026-10-14; Khipu paths are under `packages/cli/`)

- **Delivery today.** `routing_now_inject.py` is registered only under SessionStart in
  `~/.claude/settings.json`, which the second Claude home shares through a symlink. Across 649 injected
  sessions since 2026-10-13 the `updated:` stamp was a median 1.5 hours old when the session started (p90 5.1
  hours), and 122 of those sessions lived longer than an hour; the longest-lived orchestrators carried
  snapshots up to about 17 hours old. Codex's `~/.codex/AGENTS.md`
  names the table in one line. Grok Build loads the owner's user-global instructions file as its home AGENTS
  file (a dispatched Grok session's `prompt_context.json` under `~/.grok/sessions/` lists it
  under `agents_md_files` with source `home`), and that file's "Live routing table" section names the table.
  Both get the pointer, not the contents. Codex's SessionStart list has no routing hook, and 6 of 414 Codex
  rollouts read the file. Grok Build 1.0.52 discards SessionStart and UserPromptSubmit output
  (`docs/plans/2026-10-13-grok-build-pack.md`, hook facts).
- **Churn.** The change log has 53 entries between 2026-10-13 01:52Z and 2026-10-14 14:34Z, a median of about
  12 minutes apart, written by four orchestrators. Matt changed his routing directive 15 times in five days.
  On 2026-10-13 he typed one routing direction into two running windows by hand, because a conversation
  that is already running never receives a change to the table. The NOW block's priority lines are an
  orchestrator's summary of his direction, not his words.
- **Writers.** `routing_now.py` `stamp` rewrites the `updated:` line with an author (`updated: <time> by
  <who>`, `:56`); `log` prepends a change-log line with no author field (`:67`). Both rewrite the whole file
  with no lock (`:57`, `:67`). The only staleness warning fires past 24 hours, and only in `show`. Hand-typed
  times run ahead of the clock.
- **The runners hold the truth privately.** A project's job runner (`lanes.py` in project A, 2,453 lines)
  records limits per lane in `state/global.json` (`limited: {lane: {until, reason}}`) and in `events.log`
  (`<iso> <pipeline> LIMITED lane=<lane> until=<iso> job=<job> (...)`), and writes `state/heartbeat.json`
  (`{pid, time, at, interval, error}`) every 20-second pass, all under `<runner state dir>`. It never reads
  or writes the table. Project B runs its own copy against the same second Claude account. On 2026-10-14
  project B's runner learned of that account's limit 34 minutes after project A's runner had, when a job it
  had just launched failed, although project B's orchestrator had written the limit into the table's change
  log a minute after project A's runner saw it: no runner reads the table.
- **Reset times are guessed.** `lanes.py` believes only the form "resets 8pm (America/New_York)" (`:179`; plus
  two minutes, clamped between 5 minutes and 12 hours); anything else rests the lane 30 minutes (`:36`). Muse
  says "resets at <ISO time>", so a runner rests a Muse lane about 30 minutes on each limit and writes
  several LIMITED lines for one limit. Codex is excluded
  from text limit detection (`:227`); Matt said Codex was nearly out at 10:30Z, and a Khipu probe read 91% of
  its week at about 12:00Z.
- **Some accounts cannot be measured.** The second Claude home answers `rate_limits: null` until a `/login`
  there (plan usage). Muse reports figures only as of its last turn; its weekly allowance, 95% used, was found
  by Matt on its website at 14:29Z.
- **Khipu's own reach.** The per-prompt hook already runs on every Claude Code and Codex prompt (harness
  timeout 3 s, `khipu/integrations.py:320`; search budget 1.2 s, `khipu/recall_prompt.py:61`; block 600
  characters, `:86`) through a warm daemon whose socket round trip is a median 0.17 ms. Over 12 hours the
  search took a median 504 ms and a p99 1,202 ms, with 1.1% timeouts. The hook prints nothing when the search
  is empty, gated, deduplicated or timed out (`:1554-1555`), so anything riding on the search result would
  vanish in exactly those prompts. Delivery is confirmed: the client sends `op: ack` only after its stdout is
  written (`bin/khipu-recall-client.py`), and the cold path calls `finish_delivery` (`recall_prompt.py:1506`).
  For subagents, Khipu installs only SubagentStop today (`khipu/integrations.py`, docstring lines 10-50 and
  `_CLAUDE_HOOK_EVENTS` at `:564`). Claude Code 2.1.296 also has a SubagentStart hook event whose
  `additionalContext` is added to the subagent's context (seen in the installed binary); whether it reaches
  the subagent's model in practice is a phase 0 check. Codex subagent rollouts on this Mac carry no hook
  context. Codex records SessionStart and UserPromptSubmit hook context in its rollouts as `response_item`
  messages with role `developer` (the Khipu rule and the "## Prior work on this topic" block appear that way
  in the `~/.codex/sessions` exec rollouts checked). Capture reads Codex messages only for roles `user` and
  `assistant` (`khipu/session_capture.py:417-434`), and `_CODEX_INJECTED` applies to user parts only, so
  that context never becomes an episode. The MCP server sends no notifications and its `instructions` are
  fixed at initialize (`khipu/mcp_server.py:1643`). `khipu_usage` is local only (`:1471`), and `usage._key`
  keys an account as harness plus login home (`khipu/usage.py:752`).
- **Khipu once billed the wrong account.** A Stop hook fired in a session on the second Claude account
  carried that account's `CLAUDE_CONFIG_DIR` into the summariser drain it spawned, so Haiku was billed to the
  second account and hit its five-hour limit on 2026-10-14 while the main account was idle (#177, commit
  5e02d58). The lesson for this feature: an account is the login home a process actually runs under, not the
  account a message names.
- **Hygiene.** The repository is public, and `tests/test_repo_hygiene.py` fails any tracked file that holds
  the owner's private paths. Every source path in this feature is therefore configuration.

## What Khipu does and does not do

Matt, 2026-10-14: "Khipu does not route, it reports." Khipu records, reconciles and pushes account state. It
never chooses a lane and never returns a pick or a ranking. It never refuses, launches, delays or reorders
work. It never writes a runner's files or edits a runner's config. Orchestrators and runners decide. Matt's
own note on where work should go is shown in his words, word for word; Khipu never turns it into a pick.
Doctor warnings about a broken standing rule never block anything, so the table's own rule stands: "THIS
TABLE IS GUIDANCE, NOT A GATE" (Matt 2026-10-13: "I don't want to hinder the orchestrator. Flexibility is a
big deal"). Each project's job runner stays in that project, and its bugs go back there as tasks (see the
section near the end).

## Design

### The record

The record is kept **per account, not per lane**. An account is a login: the harness plus its login home,
which is the key the usage cache already uses (`usage._key`: `<harness>|<absolute login home>`, with an empty
home for the harness's default). For example, the second Claude account is `claude|` followed by the
absolute path of `~/.claude-t3-second`. Project A's `grok` and `grokcheck` lanes run `~/.grok/bin/grok`
with no `GROK_HOME`, in their launch script and in the running processes, so they use the default home and
map to `grok|`; `~/.grok-dispatch` is a separate login home used by another launcher. A limit seen on one
lane closes every lane on that account: `grok` and `grokcheck` close together, and project B's `secondary`
lanes and project A's `secondary` lane are one Claude account. A
configured lane map (`routing_lane_map`: per runner, lane name to account) does the translation. A lane that
is not in the map is shown as unmatched in Settings and in doctor, and a limit from it is pushed as one line
("project A lane opus-batch reached a limit; that lane is not matched to an account"); it is never
guessed.

Each account is in one of five states. The words in quotes are the ones the app and the push use.

- **Open**, always with its witness and age ("Open; read from Codex 4 min ago").
- **Limited until** a time ("Limit reached, back at 2:52 PM"). A limit from a model-scoped window closes only
  that model ("Opus limit reached, back Thu 9:00 AM; other models open").
- **Paused by owner** until a time or until Matt lifts it ("Paused by you until Sat 8:00 PM").
- **Down**, with a reason: not signed in, not installed, or reported out of service.
- **Unknown**: no evidence yet; or the newest open or down evidence is older than `routing_stale_after` (12
  hours, a setting); or the only source is a runner whose heartbeat is older than five of its intervals. It
  shows the last state seen and its age ("Unknown: last seen open 14 h ago"). A limit does not go stale
  before its own time.

Two readings follow a limit whose time has passed with nothing confirming the reset: "Probably open: its
limit was due to end at 1:52 PM, not checked yet", until a check or later evidence settles it; and, for an
account that cannot be probed, "Its reset time passed; this account can't be checked without using a turn".

Every entry carries these fields:

- `written_by`, never empty, set by Khipu and never taken from the writer's say-so: "project A lanes
  (read by Khipu)", "Khipu usage probe", "Khipu reset check", "owner (app)", or for a conversation its
  harness, project and session id. A conversation may give a name for itself, which is shown beside that
  author and never in place of it.
- `written_at`: Khipu's clock at the moment of the write, never a typed time.
- `until` and `until_basis`, which says where the time came from. **cli**: a probe's `resets_at`, a limit
  message Khipu parsed itself, or a runner's time in the shape a parsed message gives (see Sources).
  **estimated**: a runner's 30-minute default, a clamped time, or a conversation's report. **checked**:
  Khipu's reset check saw the account answer with headroom. **owner**: set by Matt. A guess is always shown
  as a guess.
- `scope`: all, or a model name when the window is model-scoped (the Claude probe already returns windows
  with a `model`, `usage.parse_claude`).
- `window` (five-hour, weekly or none) and `used_pct` when a probe gave them; `evidence`, one line Khipu words from
  parsed fields (a runner's lane, end and pipeline; the probe's error code), never a source's own text; and `source` (a runner's name, probe,
  check, report or owner). The routing table is never a source of account state.

**Holds** are short notes about one account, the machine, or all work. Each has a scope, a reason from a
closed list (machine busy, disk low, keep for one purpose, outage, other), an author, an end, and words of at
most 300 characters (for example "hold new launches when load1 is over 75"). A conversation's hold lasts 24
hours by default and at most 7 days (both settings). Matt's holds end when he chooses, including "until I
clear it"; Home reminds him of his own holds after 90 days (a setting). An expired hold leaves the view and
stays in the history.

**Matt's note to every conversation** carries his direction on where work should go. Today it lives in the
NOW block's priority lines, and he changed it 15 times in five days. Only he writes it, from the app or from
the CLI with a typed confirmation. It is dated, shown and pushed word for word after the standing rules, and
never truncated. Khipu never ranks accounts from it, turns it into a pick, or checks work against it.
Earlier notes stay in the history, and he can end the current one at any time.

**Standing rules** are a first-class part of the record. They are never truncated anywhere and are always
delivered first. The public product ships with no rules. A conversation may add or change a rule when it records
the owner's request in its own words; the change is live immediately, marked with Khipu's author for that
conversation and its prior wording when changed, and remains pending until the owner confirms it. Only the owner,
from the app or after a typed terminal confirmation, can confirm it, undo a pending change or dismiss a pending
addition. On this Mac, phase 1a imports them once from a
local, untracked file named by the setting `routing_rules_seed`, and each imported rule is marked "seeded;
confirm in Settings → Routing" until Matt confirms it; that mark is the only claim of ownership until he
does. The seed is taken from the table and from Matt's earlier recorded words, not from a statement of his
on 2026-10-14:

| # | Rule as seeded | Source and date |
|---|---|---|
| 1 | Grok never builds or fixes privacy or safety code. | NOW block hold "Grok builder verdict IN (2026-10-13 02:25Z)" and its GROK NO line, written by project A's orchestrator |
| 2 | Every Grok result that is acted on gets a re-check from a different model family. | NOW block, lane 2: "EVERY Grok result acted on gets a Claude or Codex pass that looks WIDER than Grok's own list", added with the GROK YES and NO list (change log 2026-10-13 01:57Z) |
| 3 | Never a -fast tier. | NOW block hold "Never a -fast tier; never the paid xAI API …" (the line carries no date); Matt's hard wall on -fast tiers in his user-global instructions (2026-07-26) |
| 4 | Never the paid xAI API. | the same NOW block hold (no date on the line) |
| 5 | Khipu runs in Aegis only through Aegis's own pack, never through a Claude or other vendor import. | Matt 2026-08-17: "I don't want Khipu running in Aegis through anything Claude. Aegis is its own harness and it's our home built harness." (Khipu project memory) |
| 6 | Never send what is in API Keys or My Portfolio to Codex or Grok. | the Codex lane floors in Matt's user-global instructions (2026-10-07) |
| 7 | Khipu's routing record is guidance, not a gate: an orchestrator with better live information may route differently, and says why. | NOW block: "THIS TABLE IS GUIDANCE, NOT A GATE (Matt 2026-10-13: "I don't want to hinder the orchestrator. Flexibility is a big deal")" |

Matt's standing rules for Khipu itself bind how this feature is built and are not pushed as text: no loss of
memory quality or function; every limit Khipu enforces can be lifted on the spot; every option is visible and
usable in the desktop app; Khipu holds no credential; plain language for a non-technical owner. If he wants
any of them in front of conversations, he adds it as a rule in Settings.

**Storage.** The record lives in Khipu's data directory beside the usage cache: `state/routing/events.jsonl`
is append-only, written under a file lock, and is the full history; `state/routing/view.json` is the folded
view, replaced atomically. The view carries `version`, which only goes up and moves only when the effective
view changes, with the plain change lines for each version; `folded_at`; and `valid_until`, the time of the
next change the clock alone will cause (a limit's end, a hold's or a pause's end, an entry going stale, a
scheduled check). The effective view is each account's state, until, scope, basis and any disagreement, plus
the rules, the note and the holds. Who wrote an entry and when are shown but are not part of it, so the same
report from many conversations does not move the version. Each conversation's last-seen version and the
time of its last full summary are kept in that session's existing per-prompt state (`recall--<session>.json`),
not in a new file per session.

**"Owner only" is a convention, not a lock.** Khipu holds no credential, so any process with a shell can
write. Every write is stamped with its author and shown in the app. Rules, the note, owner pauses and clears
are written from the app; the CLI asks for a typed confirmation on a terminal and refuses without one; the
MCP tool refuses them. Doctor warns when any of them was written by anything other than the app.

### Sources

1. **Khipu's usage probes** (`khipu usage`, cache `state/usage.json`). A window at 100% or more makes the
   account limited until that window's `resets_at` (basis cli), for that window's scope. A probe written
   after a limited entry that shows headroom (every window under 100%) reopens the account (basis checked);
   a window at 100% with no reset time ahead is never headroom and gives no evidence for its scope. `not_signed_in` and
   `not_installed` make it down. Muse rows are last-observed and labelled so. The second Claude home answers
   nothing until its `/login`.
2. **Runners' files, read and never written.** Each registered runner folder (setting `routing_sources`, a
   list of `{name, kind: "lanes", path}`) is read for `state/global.json` `limited`; the `ADDED`, `LIMITED`,
   `LAUNCH` and `HOLD` lines of `events.log` (a bounded tail: the last 512 KB, keeping the newest 200 LIMITED
   lines, both settings); `state/heartbeat.json`; and, for doctor's rule
   checks only, these keys of `config.json` and nothing else: each lane's `kind`, `model`, `launch` argument
   list and `confirm_lane`, and `defaults.check_lane` and `defaults.fix_lanes`. Nothing else in `config.json`
   is read or kept. The `launch` arguments are never kept: only what the checks need is kept from them (whether
   a model ending in -fast is named, whether the model is one of them, and the launch script's file name). Runner and table text is never stored or
   shown: Khipu keeps only parsed fields (times, the line kind, numbers) and names (lane, pipeline, model, kind)
   that pass a strict shape check and do not look like a key, with Khipu's existing secret redaction
   (`khipu.redact.redact_secrets`) as a second layer; usage cache rows are read only for configured accounts. Khipu never takes a runner's lock and never writes:
   `lanes.py` saves `global.json` at the end of a pass without its own mutex (`lanes.py:1860`), so a second
   writer would race it. Until runners write the basis themselves, a runner's `until` counts as **cli** only
   when its seconds are zero and it is not exactly 30 minutes after the LIMITED line (the shape `limit_until`
   gives a parsed message plus two minutes); everything else is **estimated**. A file that fails to parse is
   reported with its last good read, never guessed around. A `global.json` limit keeps the time of the
   newest LIMITED line Khipu saw for it (same lane, same end) after that line leaves the tail, matched against
   every line of the tail, including lines the LIMITED line cap leaves out; a limit with no line anywhere in
   the tail is dated at the oldest line the tail held when Khipu first found it. An owner clear
   is weighed against that time, so a limit the runner wrote after the clear stands once its line has aged
   out. A registered path under a credential location (`API Keys/`, `My Portfolio/`, any `auth.json`,
   keychain exports) is refused.
3. **Conversations reporting through a tool**: MCP `khipu_route_report` and `khipu route report`, for what a
   conversation learns first (a job ended on "You've hit your session limit"; Matt said Codex is nearly out).
   A report can mark an account limited, open or down, and can add holds. Its basis is **estimated** unless
   it passes the limit message itself and Khipu parses a reset time out of it (the forms "resets 8pm
   (America/New_York)" and "resets at <ISO time>"), which makes it **cli**; a conversation cannot declare a
   basis. "resets 8pm (zone)" names no day, so it is **cli** only with `--seen`, when the message was shown
   (the next 8 pm after that); without it the day is guessed from the report's time and the report is
   **estimated**. Its `until` is clamped to the length of the window it names (five hours or seven days)
   and is 30 minutes when omitted. Its author is set by Khipu, as above. A report cannot write rules, the
   note, owner pauses or clears.
4. **The owner in the app**: pauses and lifts, clears and dismissals, holds, the rules and his note.
5. **Until the cutover only: the table, read only, and never as account state.** Setting `routing_table`
   names the file. Khipu reads the time the NOW block's `updated:` line gives and a hash of the block. When the
   hash changes, the version moves with one line: "The routing table's NOW block changed at HH:MMZ (stamped
   HH:MMZ)." Khipu does not push the table's prose, which is free text from a file any process rewrites
   without a lock, and it never writes the file before phase 8.

### Folding the sources into one state

Evidence written before an owner clear of an account is ignored for that account, and a dismissed report is
ignored on its own. Open and down evidence older than `routing_stale_after` counts only toward Unknown's
"last seen" line. Of what remains, an account's effective state is the first row that applies:

| Order | Evidence | State shown | What ends it before its time |
|---|---|---|---|
| 1 | An owner pause that has not ended or been lifted | Paused by you | Matt lifts it |
| 2 | Down, from a probe or a report | Down | a later probe or check that answers, a later report, or an owner clear |
| 3 | An unexpired limit with basis cli, checked or owner | Limit reached | a later probe or reset check with headroom, or an owner clear; never a conversation's open report |
| 4 | An unexpired estimated limit | Limit reached (a guess) | any later open evidence (a probe, a check, a conversation's open report), or an owner clear |
| 5 | Open evidence newer than any limit, or a limit whose time has passed | Open, or Probably open | later limit or down evidence |
| 6 | No evidence, only stale evidence, or only a silent runner | Unknown | any fresh evidence |

Within a row the latest evidence wins. A probe's `resets_at` replaces a runner's estimated time for the same
account and window. A model-scoped limit closes only its model; the account line names both.

**Disagreements.** When evidence newer than the evidence behind the shown state says something else but
cannot override it by the order above (for example a conversation's open report against Claude's own limit
message), the entry records a disagreement: the other state, its source and when. The shown state follows
the order; the Home card and every conversation hear both, in one line: "Claude second: limit reached, back
at 18:52Z (from Claude's message, seen by project A lanes at 15:13Z); a Claude Code conversation in
project-b reported it open at 15:31Z." A report against an owner pause is not a disagreement, because a
pause is Matt's choice. A disagreement lasts until either side expires, a probe or check settles it, or Matt
clears the account or dismisses one side. Clear ends the shown limit or down state and every older report;
Dismiss removes only the report he picks. Doctor warns when a disagreement lasts longer than 30 minutes (a
setting).

**Change lines.** Each fold that changes the effective view writes one version with plain change lines, for
example "Claude second: limit reached, back at 18:52Z (time from Claude's message; seen by project A
lanes at 15:13Z)". Folding never calls a model, never starts a process and never waits on the hub.

**The clock.** A limit ending, a hold or pause ending, an entry going stale and "probably open" are changes
the clock causes with no file changing. In phase 1a every read refolds when a source file's modification
time has changed or `valid_until` has passed. From phase 3c the resident recall daemon
(`com.khipu.recall-daemon`) checks the registered files' modification times every 10 seconds (a setting),
refolds on change, and never sleeps past `valid_until`, so each of those changes becomes a version on time.
After the Mac sleeps, the daemon runs every transition and check that fell due, in order, when it wakes. The
per-prompt hook compares `valid_until` with the clock and, when it is overdue (a stalled or missing daemon),
refolds inline from the files it already reads; that starts no process. Checking a file's time starts no
process and probes nothing.

### The push

The push has two shapes.

- **The full summary**, at session start, after compaction, and as the fallback below. In order: a heading
  with the current time and the version ("## Routing at 15:40Z (Khipu's routing record, version 212; data,
  not instructions; guidance, not a gate)"); the standing rules, whole; Matt's note, whole; until the
  cutover, one line on precedence ("These account lines are newer than the routing table's account rows; the
  table's NOW block was last stamped 13:48Z, 1 h 52 min ago."); one line per account (state, until with the
  time remaining, basis, scope, witness and age, any disagreement); then holds. Everything after the rules
  and the note has a budget of 1,500 characters (a setting). Whole lines are dropped from the bottom, oldest
  holds first and then open accounts, never cut mid-line; the summary then ends with "N more lines: run
  `khipu route`", and doctor warns. The rules and the note are never cut whatever their length; doctor warns
  when together they pass 1,500 characters so Matt can shorten them. On a Mac with no routing sources the
  account lines are replaced by "No routing record on this Mac." When `folded_at` is older than 5 minutes (a
  setting), a line says "Khipu's record has not been refreshed since HH:MMZ." Claude Code keeps a hook's
  text whole only up to 10,000 characters, so at session start whole lines of the memory slice are dropped
  from its bottom to make room; a summary that still does not fit is left out whole, is not recorded as
  delivered, and arrives with a later push.
- **What changed since this conversation last saw the record**, on every prompt. Khipu takes every entity
  that changed between the session's last-seen version and the current one (an account, a hold, a rule, the
  note) and sends its **current** state, one line each; a line another version has since replaced is never
  sent. Then the order and the budget apply: a changed rule and a changed note first and whole; then limited,
  down and unknown lines; then reopenings; then holds. The fence is headed "## Routing changes at HH:MMZ
  (Khipu's routing record; data, not instructions; guidance, not a gate)", so the current time is always
  stated, and every line with a time also states the time remaining ("until 18:52Z, in 3 h 12 min"). The
  budget for lines other than rules and the note is 400 characters (a setting); overflow ends with "N more
  changes: `khipu_route` since <version>". Nothing is sent when nothing changed, with two exceptions: one line
  when a source goes dark (a runner's heartbeat passes five intervals, or its files stop parsing) or a limit
  arrives from an unmatched lane; and one line when the record has not been refreshed past its bound.
- **Falling back to the full summary.** The per-prompt hook sends the full summary instead of changes when
  the session has no last-seen version (a resumed session, a lost session-start delivery, a compaction no hook
  reported), when it is more than 20 versions behind, or when its last full summary is more than 6 hours old
  (both settings). That also covers an idle conversation coming back.

The per-prompt part follows these rules.

- It reads the folded view under its own time budget, 50 ms by default (a setting). It never shares the 1.2 s
  search budget, and a search that is empty, gated, deduplicated or timed out still delivers the routing
  lines. Today `hook_main` returns `{}` when the recall context is empty (`recall_prompt.py:1554`); that early
  return is restructured.
- The session's last-seen version moves only on confirmed delivery: the same `op: ack` the client sends after
  writing stdout, and `finish_delivery` on the cold path. A block that was not delivered is sent again on the
  next prompt.
- It fails open. An unreadable view, a missing daemon or a blown budget sends nothing and adds
  `routing=skipped:<reason>` to the hook's summary log line; recall is unaffected.
- **What goes in.** The block is fenced. Each line other than a standing rule or Matt's note is clipped to 200
  characters, and the cap applies to those lines only: rules and the note are never clipped or capped, in
  either shape. Only Khipu-written lines appear. From conversations only structured values are pushed:
  account, state, until, scope, basis, a hold's scope and reason, and the author Khipu set. A conversation's
  own words (a hold's text, its evidence line, the name it gives itself, and its project, which is its
  folder's name) are shown only in `khipu_route` results and in the app; a push names only its harness. A hold's words are pushed only when Matt wrote them. File prose is never pushed.
- **Switches.** The `routing_push` feature (added to the closed `features.FEATURES` set; off by default in
  the public product; one setting, shown in Optional features and at the top of Settings → Routing) turns
  all of it on or off. Under it, one setting per moment: at session start and after compaction
  (`routing_push_start`), on each prompt (`routing_push_prompt`) and at delegation calls
  (`routing_push_delegation`, phase 6). There are no per-harness switches; Settings shows what each harness
  hears. The existing `ROUTING_NOW_OFF=1` and `/tmp/.routing_now_disable` are honoured too, so Matt's old
  bypass turns off both.
- **Memory quality is protected.** Claude Code hook context arrives in `<system-reminder>` blocks, which
  capture strips (`khipu/session_capture.py:297`). Codex records hook context as `developer` messages, which
  capture never reads (`:417-434`). A regression test on both harnesses' transcript shapes proves that a
  routing block never becomes part of an episode.

Where each harness gets it, and how fresh it really is:

| Harness | At start and after compaction | During the conversation | Honest freshness |
|---|---|---|---|
| Claude Code on both accounts: every orchestrator, interactive sessions and `claude -p` jobs | the full summary, through `khipu-recall-hook` | changes on every prompt, through `khipu-prompt-recall`; at each delegation call from phase 6 | next prompt; inside a long autonomous turn only from phase 6 |
| Codex, interactive and `codex exec` | the same, through `~/.codex/hooks.json` (whether it fires after compaction and resume is a phase 0 check) | the same | next prompt; phase 6 only if Codex's PreToolUse carries context |
| Grok Build | a static pointer to the table in its home AGENTS file, and one line in the MCP instructions telling it to call `khipu_route` before it delegates; nothing is pushed, because it discards SessionStart and prompt output | `khipu_route` when it asks; a PreToolUse push only after Matt's go (open decision 4 of the Grok Build pack scope), and it must refuse inside Aegis | when it asks; after the go, its next tool call |
| Aegis product agent | Aegis is native-only, so nothing Claude-shaped is installed there; Khipu exposes `routing` in `khipu_status` and `khipu_route` | Aegis's own client pulls each turn (an Aegis task; its `status()` sends empty arguments today) | when it calls |
| Cloud harnesses through the hub (Grok Bot through the HTTPS gateway) | the MCP instructions line | `khipu_route` on the gateway reads the cloud copy (phase 7) | as of the last copy, with a stale mark past 10 minutes; only when it calls |
| A second Mac | "No routing record on this Mac" until phase 7; then its `khipu route` and hooks read the cloud copy, read only, with its age | the same | as of the cloud copy |
| Jobs a runner launches (`claude -p`, `codex exec`, `grok -p`, `muse exec`) | the brief carries `khipu route --brief` (a runner change, filed with the runner's project); Claude Code and Codex jobs also get the hooks above | a headless job has one prompt; phase 6 reaches its delegation calls | current at launch; Grok and Muse jobs learn nothing mid-run |
| Runners (`lanes.py` loops) | optionally read `view.json` on each pass (filed with the runner's project) | | within one pass, 20 seconds |
| Dispatched subagents | Claude Code: a SubagentStart hook can give a subagent the summary (phase 6, once the phase 0 check confirms it reaches the subagent; Khipu installs only SubagentStop today). Codex: its subagent rollouts carry no hook context | phase 6, if PreToolUse fires on a subagent's tool calls | their brief at launch; from phase 6, the summary at start where SubagentStart works |
| Cursor (retired as a lane) | the summary through its sessionStart hook | none; Cursor has no per-prompt event | session start |

An idle conversation learns nothing until its next prompt, and nothing interrupts a turn that is running.

### The background check at reset times

Matt approved this on 2026-10-14. It changes plan usage's decision "Cache with a short max age rather than a
daemon" for this one case, and phase 5 records the change in that page's Decisions.

For each account the view holds limited, or probably open, until a known time, the resident recall daemon
schedules a check 2 minutes after that time (the wait, a setting). Accounts that are open are not checked: a
reset changes nothing conversations are told about them. This reads Matt's "each known reset time" as each
reset that ends a limit; his approval of the mock on 2026-10-14 confirmed that reading. The check is a
single `khipu usage` probe of that one account: no credential, no model call, never a spent turn. It shares
the usage refresh lock, so it never runs beside another probe. If the probe shows headroom, the account reopens with
basis **checked**. If it still shows the limit with the same reset time, the check tries again 10 and 30
minutes after the reset (a setting); if it shows a later reset time, the account stays limited until that
time and the next check moves there. If the account cannot be read without a turn (Muse, or the second
Claude home before its `/login`), the entry reads "Its reset time passed; this account can't be checked
without using a turn". When the Mac is busy (load1 over 75, a setting), the check waits 5 minutes, at most
three times, and then records "not checked: the Mac was busy". Every outcome is written to the record by
"Khipu reset check". A daemon restart recomputes the schedule from the view, and after the Mac sleeps every
check that fell due runs once when it wakes. At most one probe process runs at a time, and an account gets
at most three checks per reset: the first and two retries.
Only a check at or after the reset time plus the wait counts toward those three; a Check now before then (or on an account with no reset time) records its outcome and leaves the reset's checks, waits and next check as they were. Each check gets its own busy-Mac waits. An account the record holds but plan usage no longer reads (removed in Settings → Models) gets no check and no Check now (phase 5 fix round 1). Only a live read counts as a check's reading: Codex's last-turn figures (its fallback when the live read fails) record the failure, never open or a limit, and a signed-out read records "can't be checked". A wait that nothing will retry (reset checks off, or the reset's three checks spent) says "try again later" and schedules nothing, and a wait never rolls back a check written meanwhile. With no `usage_accounts`, a CLI Khipu cannot find is recorded as not installed. A sign-in failure on a Check now before the reset leaves the reset's check in place (phase 5 fix round 2).

### Tools

- `khipu route [--json] [--since V] [--summary] [--brief] [--account A]` prints the view. `--summary` is
  exactly the session-start text, `--brief` is what a runner pastes into a brief, and `--since` prints the
  current state of everything that changed after a version.
- `khipu route report (--account A | --runner R --lane L) --state limited|open|down [--until T] [--window
  five-hour|weekly] [--model M] [--message "<the limit message>"] [--seen T] [--name "<what the conversation
  calls itself>"] [--evidence "<line>"]` records what a conversation learned. Khipu sets the author and the
  basis. The fields that are pushed take no free text: `--model` is a model's name (not with `--state down`,
  which is about the whole account), a conversation's `--runner`/`--lane` must be a lane the lane map names,
  and a harness Khipu doesn't know is recorded as unknown.
- `khipu route hold "<text>" --reason R --until T [--account A | --machine | --all]` adds a hold, and `khipu
  route hold end <id>` ends one.
- The owner's commands: `khipu route pause <account> [--until T]`, `khipu route lift <account>`, `khipu
  route clear <account> [--report ID]` (clear the account, or dismiss one report), `khipu route note
  set|end`, and `khipu route rule add|edit|retire|confirm`. They ask for a typed confirmation on a terminal
  and refuse without one.
- `khipu route wait <account> [--confirmed] [--timeout S]` exits 0 when the account is open (with
  `--confirmed`, only once a check has confirmed it: a usage probe or reset check that read it open after its
  newest limit or down report, from any source, and is not stale, even when a newer open report from a conversation is now the witness). An
  orchestrator's relaunch script waits on the record instead of a typed time that cannot move. It waits; it
  launches nothing.
- `khipu route sources` lists each registered runner folder with its last good read and any parse error.
- MCP `khipu_route` reads the view, with optional `since` and `account`, and includes the words of holds and
  reports that the push leaves out. Its description ends "Khipu reports; routing stays with you", and it
  never returns a pick or a ranking. MCP `khipu_route_report` writes reports and holds only, takes its author
  from the session, and refuses rules, the note, pauses and clears. Its answer and the light status's
  `routing` field follow one policy: each is built from a projection that copies only named fields and never
  holds an account key, a login folder, a runner folder or the table's path. An account is named by its label
  and a stable opaque id (a short hash of its key), which both tools take back with the label; a since
  entity names an account by that id; Khipu's own sentences use labels only; an error is a fixed message,
  never an exception's text or a path. The owner's rules, note and hold words are shown word for word (a
  path in them included); nothing is scrubbed out of an answer after it is built. A conversation's words
  have each configured path cut to its last name once, before they are stored. A
  conversation's words inside a line are shown in printable ASCII only: quotation marks become a straight
  single quote, any other character is written as its escape (`\u02ee`), and "you", "owner" and "standing
  rule" are marked as theirs ("[you]"), so no lookalike can close Khipu's quote or speak for the owner. A
  conversation's project is its folder's name, and none when that folder is the home folder, one that holds
  it, the root or a scratch folder.
- `khipu_status`'s light payload gains a `routing` field holding the summary, so a harness without hooks
  gets it with a call it already makes. The recall rule and the MCP `instructions` gain one line, and
  `features.contract()` gains a `routing` capability.

### App

The mock comes first (`docs/mockups/2026-10-14-routing/`); Matt approved it on 2026-10-14. Every text is
in plain words and every time is local. The record's terms appear in the app as:

| In the record | In the app |
|---|---|
| limited until, basis cli | "Limit reached, back at 2:52 PM", with "Read from Claude" or "From Claude's own message, seen by your Project A job runner" |
| limited until, basis estimated | "Limit reached, back about 12:10 PM (a guess)" |
| a limit passed, not checked | "Probably open: its limit was due to end at 1:52 PM, not checked yet" |
| cannot be checked | "Its reset time passed; this account can't be checked without using a turn" |
| paused by owner | "Paused by you until Sat 8:00 PM"; the button is "Mark paused until…", with "This doesn't stop anything that is running; it tells conversations you want this account left alone." |
| down | "Down: not signed in" (or "not installed", or "out of service") |
| unknown | "Unknown: no report yet" or "Unknown: last seen open 14 h ago" |
| owner clear, dismissal | "Clear" ("treat as open until something newer reports on it"), "Dismiss this report" |
| `routing_lane_map` | "Which account each job lane uses" |
| `routing_sources` | "Job runner folders" |
| `routing_table` | "Routing table file" |
| load guard | "Wait while the Mac is busy" |
| grace and retries | "Wait after the reset time", "Try again after" |
| hub mirror | "Copy for cloud agents" |
| summary size, per-prompt size, time budget | "Longest summary at the start of a conversation", "Longest update before a prompt", "Time allowed before a prompt" |
| `routing_stale_after` | "Treat a report as out of date after" |

- **Home → Routing card**, beside Plans. One row per account with its state in the words above, who said so
  and how long ago, and a Check now button on each account that is limited, probably open or unknown and can
  be probed. Active holds show under the accounts with their end and who set them; Matt's note shows in his
  words with its date; "Rules (7)" is folded. Attention items: a standing rule may be broken, a job runner
  has stopped reporting, sources have disagreed for a while, or a limit came from a lane that is not matched
  to an account.
- **Settings → Routing** shows every option and makes each one usable, and every limit on it can be raised
  or removed on the spot:
  - Accounts: Check now, Mark paused until…, Lift, Clear, and in a disagreement Dismiss one side, with a line
    saying what each does. The account list itself is plan usage's (`usage_accounts`, shown in Settings →
    Models); this page does not keep a second list.
  - Your note to every conversation: write, edit, end; earlier notes in Recent changes.
  - Standing rules: each with its date, its source, a "Seeded, confirm" mark until confirmed, and whether
    Khipu watches it (the rules with doctor checks say "Watched"; the rest say "Not watched by Khipu");
    confirm, edit, remove and add, each asking to confirm, with Undo after a removal.
  - Holds: the list with scope, author and end; add (words, scope, reason, end including "Until I clear it");
    end now; how long a conversation's hold lasts (24 hours, at most 7 days); when to remind Matt about his
    own (90 days).
  - Checking for resets: on or off; wait after the reset time (2 minutes); try again after (10 and 30
    minutes); wait while the Mac is busy (load over 75, 5 minutes, up to three times); the next checks; which
    accounts are not checked and why.
  - Telling conversations: the master switch; at the start, before each prompt, before handing work to
    another agent; what each harness hears; the longest summary (1,500 characters), the longest update (400
    characters), the time allowed (50 ms), when to send everything again (20 changes or 6 hours), when a
    report is out of date (12 hours), when to warn about a disagreement (30 minutes), when the record counts
    as not refreshed (5 minutes); and whether either old off switch is set.
  - Job runner folders: add, remove, last good read, errors, stopped reporting.
  - Which account each job lane uses, with unmatched lanes.
  - Routing table file: read only until the switch-over, then the generated part.
  - Copy for cloud agents: on or off; include hold words (off by default); include the rules and the note
    (off by default, a separate choice); when the copy counts as out of date (10 minutes).
  - Recent changes, newest first, one list per day.
  The page uses the same fixed-argv command pattern as the Plans card.
- **Health report**: a "Routing" row with the last change, any broken standing rule and any silent runner.

### Doctor warnings

These go into the top-level `warnings` list that plan usage phase 2 adds to `khipu doctor` (on main,
b1a1cdf, there is none; the only `warnings` key is inside the unknown-harness block, `khipu/cli.py:697`), and
into the app's attention items. They never gate `ok`. The texts Matt reads:

- "A standing rule may be broken: <runner>'s settings name a model ending in -fast for its <lane> lane." This
  reads `config.json` only: a lane's `model` and its `launch` arguments. A LAUNCH line carries no model
  (`lanes.py:1717`, `:1799`), and a model chosen inside a launch script (project A's Grok launch script
  passes `-m grok-4.7`) is reported as "can't be checked: the model is set inside <script>", never as
  passing.
- "A standing rule may be broken: a Grok lane is set to work on <pipeline>, which is marked private." (from
  the pipeline's ADDED line; when it names no check lane or no fix lanes, the runner's `defaults.check_lane`
  or `defaults.fix_lanes` stand in for them)
- "A standing rule may be broken: <runner>'s Grok check lane has no confirming lane from another model
  family." (`confirm_lane`: none set, a lane the settings lack, or a Grok lane.) When the confirming lane's
  `kind` is not set, it "can't be checked", never reported as passing.
- Whether a Grok lane uses the paid xAI API "can't be checked: the launch environment is set inside
  <script>", never reported as passing.
- "A job was started on <account> while you had it paused", or "…after it had reached its limit." (LAUNCH
  lines, through the lane map)
- "Your <runner> job runner stopped reporting at HH:MM", or "Khipu can't read <runner>'s files since HH:MM."
- "<account> is marked limited only on a guess."
- "A limit came from <runner>'s <lane> lane, which isn't matched to an account."
- "Sources have disagreed about <account> for more than 30 minutes."
- "The summary had to leave out N lines", or "Your rules and note are longer than the summary size."
- "A rule, a pause, a clear or your note was written by something other than the app."
- "A job runner folder was refused because it is inside a private folder."
- "Reading the record before a prompt took too long on more than 1 in 100 prompts today."
- "There is no routing record on this Mac." (only when routing is on)

### Cutover (phase 8, Matt's go)

1. One day of dry run first. Khipu's view is compared with `lanes.py status --json` for each registered
   runner (projects A and B) and with the NOW block, and the differences are listed for Matt, together with
   every hand-written line that will stay outside the generated part.
2. Before the NOW block changes, Matt's priority lines move into his note: Khipu offers the current lines
   word for word, and he confirms or edits them in the app, so nothing he wrote is lost or rewritten by
   Khipu. Then the NOW block's account state and holds become a generated view. Khipu writes only between its
   own fence lines, atomically, and the first line says where to make changes. If that part was edited by
   hand since Khipu last wrote it, the hand edit is saved aside and a doctor warning names the copy before
   the part is replaced. The GROK YES and NO list, the bottleneck line, the capability rows, the "how the
   lanes are reached" section and the change log's reasoning stay hand-written outside the fence; the
   cutover must not remove or truncate any of them.
3. The old hook. At phase 3c's live check Matt is asked for a go either to retire `routing_now_inject.py` or
   to narrow it to the hand-written lines, dropping the account state Khipu now pushes. At this step it is
   narrowed or retired as he chose, and `routing_now.py stamp|log` is repointed to `khipu route`. Both live in
   Matt's own workspace, not in this repository.
4. Changes go one project at a time, and any runner change waits until its current jobs drain.

Ancillary, for Matt and not part of this scope: the static lane sections in `~/.claude/CLAUDE.md` and
`~/.codex/AGENTS.md` still contradict the live state (Cursor as the "SOLE non-trivial code lane", Codex as
"prepaid, plentiful"), and every session receives them. The same file's "Live routing table" section tells
every conversation to edit the NOW block and says the NOW block wins; after phase 3 it should point
conversations to `khipu route report` and Khipu's summary instead. Both edits are outside this repository.

## What this will not fix

- Limits nobody sees: a limit that no runner detects, no probe can read and no conversation reports. The
  second Claude account stays unreadable by probe until a `/login` in its home, and Muse stays last-observed.
  Such an account shows as Unknown once its evidence is stale, never as open.
- Work already running. A headless job learns only at launch, or at its next delegation call from phase 6;
  Grok and Muse jobs learn nothing mid-run.
- Idle conversations, until their next prompt (they then get the full summary if they are far behind); and a
  long turn in a harness whose PreToolUse hooks carry no context.
- Codex subagents, whose rollouts carry no hook context; they learn only from their brief.
- Grok Build, until Matt approves its PreToolUse push; and the Aegis agent, until Aegis's own client pulls
  each turn.
- The rush at a reset. Every orchestrator learns at the same moment that an account reopened. Khipu shows
  the headroom beside the state but staggers nothing, because launching is the orchestrators' job.
- Wrong or stale reports and holds. They are only as true as their authors. The author Khipu sets, the
  expiry, the clamp on a conversation's time, the order that keeps a conversation's report from overriding
  Claude's own message, Matt's clear and the app's who-and-when limit the damage; they do not make a report
  correct.
- A second writer. A second Mac reads the cloud copy (phase 7) and writes no record of its own; nothing
  merges two writers.
- Runner mechanics (below) and the static instruction files named above.

## Phases

### Phase 0: scope, mocks and the hook check (S)

This page and the mocks in `docs/mockups/2026-10-14-routing/`, both approved by Matt on 2026-10-14 (decision
20).
Then a throwaway-hook check of five facts that shape phases 3 and 6, run on Claude Code 2.1.296 and the
installed Codex in a scratch configuration home that leaves the live settings untouched:

1. Does a PreToolUse hook's `additionalContext` reach the model?
2. Does PreToolUse fire on a subagent's tool calls?
3. Does SubagentStart's `additionalContext` reach the subagent's model?
4. Does Codex's SessionStart fire after compaction and on resume, as Claude Code's does (`source: compact`)?
5. Does a desktop task-notification wake count as a prompt for UserPromptSubmit?

Each answer is written into this page with its evidence. Stop-condition: if a check would need the live hook
configuration, it stays in Unverified and the check moves on.

#### Phase 0 results

Done 2026-10-14. All five facts answered yes, on Claude Code 2.1.296 and codex-cli 0.162.0-alpha.17.2, in a
scratch configuration home; no live hook configuration was read or changed. Claude Code ran once, headless
(`claude -p --model haiku --setting-sources "" --settings <scratch> --strict-mcp-config
--no-session-persistence`, one `--agents` probe subagent limited to Bash, recorded with `--output-format
stream-json --include-hook-events --forward-subagent-text`); the stream showed only the scratch hooks firing.
Codex ran four times with `codex exec --ignore-user-config --dangerously-bypass-hook-trust` and the hooks
passed as `-c hooks.<Event>=[...]`. Every hook logged its payload's field names and returned a distinct
sentinel in `additionalContext`.

| # | Fact | Answer | How it was shown |
|---|---|---|---|
| 1 | A PreToolUse hook's `additionalContext` reaches the model | Yes, on Claude Code and Codex | Claude Code: the stream shows the PreToolUse hook's output and the model's final text repeats the sentinel, noting it arrived after its Bash call and again after its Agent call. Codex: the agent reported the sentinel "in a developer message". In both, the context arrives with the tool result, after the call has gone out. |
| 2 | PreToolUse fires on a subagent's tool calls | Yes, on Claude Code | The hook log has a PreToolUse payload carrying `agent_id` and `agent_type` (`probe`), which the main session's calls do not carry. The subagent reported seeing its own sentinel; the parent did not. |
| 3 | SubagentStart's `additionalContext` reaches the subagent's model | Yes, on Claude Code | The log has the SubagentStart event for the probe, and the stream shows its hook output. The subagent said the sentinel was in its context before its first tool call; the parent never saw it. |
| 4 | Codex's SessionStart fires after compaction and on resume | Yes, both | Compaction (`-c model_auto_compact_token_limit=2000`): the log runs `SessionStart source=startup`, `UserPromptSubmit`, `PreCompact`, `PostCompact`, `SessionStart source=compact`, so it fires after a compaction partway through a turn. Resume (`codex exec resume`): `SessionStart source=resume`, then `UserPromptSubmit`. The binary's schema lists the sources startup, resume, clear, compact and fork. |
| 5 | A desktop task-notification wake counts as a prompt for UserPromptSubmit | Yes, reproduced headless and backed by desktop transcripts | Headless: the probe subagent ran in the background; the first turn ended, a `task_notification` arrived, then UserPromptSubmit fired (second log entry, with a `prompt` key) and its sentinel reached the model for a second turn. Desktop: transcripts from the last two days, read for structural fields only and never content, show wakes (`origin.kind` task-notification) followed by a UserPromptSubmit hook-context attachment in 48 sessions on versions 2.1.266 to 2.1.295. About a third of wakes, and a similar share of typed prompts, have no attachment; that fits a hook that fired and printed nothing, but is not verified. |

What this means for the later phases:

- Phase 6 can be built in full on both harnesses. PreToolUse context reaches the model in Claude Code and in
  Codex, and SubagentStart context reaches a Claude Code subagent, so the full summary at subagent start can
  go in as planned.
- The PreToolUse push arrives after the call it fires on. It reaches the orchestrator with the tool result,
  once the delegation has gone out, so it can only change the next step (the hook never denies). Giving the
  current state to the child of the call in flight is SubagentStart's job.
- The phase 6 hook must key on `agent_id`. PreToolUse also fires inside subagents; without the check (skip
  them, or key the seen marker per agent) every subagent tool call would repeat the push and spend the budget.
- Phase 3a's session-start summary covers compacted and resumed Codex sessions the same way it covers Claude
  Code's, including a compaction partway through a turn.
- In phase 3b every background-task wake runs the per-prompt hook with the notification as the prompt, many
  times in a desktop session, and each run also triggers the recall search. The "nothing changed, send nothing"
  rule and the seen marker matter there, and a wake must not trigger the full summary. A wake is also the
  natural moment to tell a long orchestration about a limit that changed mid-turn.
- A new Codex hook does not run until it is trusted: Codex keeps that in `hooks.state.<key>.trusted_hash`
  and shows "1 hook needs review before it can run". The scratch hooks ran only because of
  `--dangerously-bypass-hook-trust`, so any hook phase 3 or phase 6 adds has to plan for the trust step.

### Phase 1a: the record and `khipu route` (M, one session)

Needs plan usage phase 2 merged, because it adds doctor's top-level `warnings` list; if it has not merged,
stop and ask. Inspect `khipu/usage.py` (`_key`, `configured_accounts`, `report`, `parse_claude` and its
model-scoped windows, the cache and its locks), `khipu/config.py`, `khipu/redact.py` and the doctor
`warnings` path in `khipu/cli.py` before writing `khipu/routing.py`.

- Build the event log; the fold with its order, disagreements, owner clear and dismissal; the five states and
  the scope; `valid_until` and the refold on read; the versioned view with change lines; holds with reason and
  expiry; Matt's note; the rules with their one-time import from `routing_rules_seed`; `khipu route` with
  read, report, hold, pause, lift, clear, note and rule; config keys for every size, time and setting named on
  this page; a `routing` block in doctor.
- Tests, hermetic: the fold order row by row; an open report cannot end a cli limit but does end an
  estimated one; a clear holds until newer evidence arrives; dismissing one side of a disagreement; a
  model-scoped limit closes only its model; stale open evidence becomes Unknown and a limit does not go stale
  before its time; a limit's end and a hold's end become a new version with no file changing; the same report
  from two sessions does not move the version; the version moves only when the view changes; a conversation's
  until is clamped and is 30 minutes when omitted; a pasted limit message gives basis cli and a bare report
  gives estimated; the public default has no rules; the seed imports once.
- Stop-conditions: if the fold would need a runner's lock, a model call or a process, stop and ask. If a test
  or a fixture would need a real private path, that path is configuration instead.

Useful alone: Matt and any conversation can record and read account state, his rules and his note in one
place.

### Phase 1b: the sources (S–M, one session)

Inspect a real `global.json`, `events.log`, `heartbeat.json` and `config.json` from a lanes runner (read
only; copied into fixtures by shape, with names changed) and the usage cache before writing the adapters.

- Build the read-only runner adapter (only the named `config.json` keys; only parsed fields and checked names stored), the lane
  map with unmatched lanes, the usage-cache source with model-scoped windows, the silent-runner and
  unreadable-file entries, and the table pointer.
- Tests: fixtures shaped like the real runner files, including a Muse LIMITED line with a 30-minute guess and
  a second-account line with a parsed time; a probe at 100% replaces a runner's guess; a later probe with
  headroom reopens; two lanes on one account close together; a limit from an unmatched lane is reported and
  never guessed; a runner file that fails to parse is reported with its last good read; a cleared runner limit stays cleared
  until the runner writes a newer LIMITED line; a stale heartbeat
  makes an account Unknown when the runner is its only source; a source path under a credential folder is
  refused; every runner file is opened read-only (assert that no write handle is opened); a `config.json` key
  outside the named list never reaches the record.
- Settle write order when a line is written, not when the log is read: the known limit below. The writer
  holds the log lock and sees the last line, so it can record a sequence number with each line and the fold
  can order by that instead of guessing from times. Test the known limit's case: `[T0, T0+1m, three lines
  dated 2001, N correct lines]` for N = 0, 1 and 10; the history keeps its times once N >= 1, and doctor names
  the 2001 lines, not the T0 lines.
- Stop-condition: if an adapter would need to write or lock a runner file, stop and ask.

Useful alone: one command answers which accounts are open right now, according to whom, and since when.

### Phase 2: the tools (S, one session)

Inspect `_tool_usage`, `_tool_status_light`, `TOOLS` and `handle_message` in `khipu/mcp_server.py`,
`features.contract()`, and `recall_rule.RULE_MD` with `tests/test_recall_rule.py` (which pins what the rule
carries) before writing the tools.

- Build `khipu_route`, `khipu_route_report`, the `routing` field in the light status payload, the capability,
  the instructions line and `khipu route wait`. On the gateway both tools answer `available: false` until
  phase 7.
- Tests in the `test_mcp_server` style: no answer contains a pick or a ranking; rules, the note, pauses and
  clears are refused through MCP; the author is always set by Khipu; `khipu_route` returns the words the push
  leaves out; `wait` returns on a state change in a fixture and on its timeout.

Useful alone: every harness with Khipu's MCP server can read and report, Grok Build and Aegis included.

### Phase 3a: the session-start summary (S, one session)

Inspect `recall_rule.session_start_context` and `session_start_main`, `features.FEATURES` with the app's
`FEATURE_NAMES` parity test (`apps/desktop/src-tauri/src/lib.rs:515`, `:2929`), and the Claude Code and Codex
transcript shapes that capture reads, before writing it.

- Build the full summary in `khipu-recall-hook` (it resets the session's last-seen version and records the
  time of the full summary), with the precedence line about the table and the no-record and not-refreshed
  lines; the kill switches; the `routing_push` feature and `routing_push_start`, with the parity test in the
  same release.
- Tests: the rules and the note arrive whole whatever their length, including a rule longer than 200
  characters; the budget drops whole lines; a Claude Code and a Codex transcript that carry the summary
  produce no routing text in the episode.

Useful alone: every new or compacted Claude Code, Codex and Cursor session gets the rules, the note and the
account state whole, with no daemon or hot path involved.

### Phase 3b: changes on every prompt (M, one session)

Inspect `bin/khipu-prompt-recall`, `bin/khipu-recall-client.py` (the `op: ack`), `recall_daemon._recall` and
`_warm_caches`, `recall_prompt.hook_main` and `finish_delivery`, the dedup state (`recall_prompt.py:148-226`),
and the hook log regex shared with `khipu/promote.py` before writing it.

- Build the per-prompt changes on both the daemon path and the cold path, under their own budget; the
  current-state lines; the fallback to the full summary; the seen marker that moves only on confirmed
  delivery; the restructure of `hook_main`'s early return; the inline refold when `valid_until` is overdue;
  `routing=` appended at the end of the hook's summary log line (after `degraded=`, so the existing log
  regexes still parse it); `routing_push_prompt`.
- Tests: a search timeout, a gated prompt and a deduplicated prompt still deliver the routing lines; an
  undelivered block is sent again; a replaced line is never sent (an account limited and then reopened
  between two prompts sends only the reopening); a session with no last-seen version, more than 20 versions
  behind, or past 6 hours since its last full summary gets the full summary; an unreadable view fails open
  with recall unchanged; the seen marker works on both paths; nothing is sent when nothing changed; a changed
  rule longer than the budget is sent whole.

Useful alone: this is the fix Matt asked for in Claude Code and Codex.

### Phase 3c: the daemon and the gate (S, one session)

Inspect `recall_daemon.run` and its restart on code change, and `gate_recall_replay` in `khipu/promote.py`,
before writing it.

- Build the daemon's poll and refold, its sleep that never passes `valid_until`, and the run of everything
  that fell due after the Mac wakes. Extend the gate's recall replay so a candidate fails the step when its
  routing read exceeds the budget or its search answers change with routing on. The replay also measures the
  search's milliseconds with routing on against routing off, on a daemon whose vector matrix is warm (the
  daemon never forks itself for routing: its routing helper, spawned once, forks each prompt's child; a fork
  of the daemon cost about 18 ms and slowed its search about four times while the child lived). Put the
  per-prompt budget rate in doctor.
- Stop-condition: if, over at least 200 replayed prompts, timeouts with routing on exceed those with routing
  off by more than 2 in 100 prompts, switch push off and stop.
- Live check after promotion: one Claude Code conversation and one Codex conversation each see a state change
  between two of their prompts, including a limit's end with no file changing. Then Matt switches the feature
  on (the app's optional-features list shows it once it is in `FEATURE_NAMES`), or says go, and he is asked
  for a go to retire or narrow the old SessionStart hook.

Useful alone: changes the clock causes reach conversations as reliably as changes in a file.

#### Phase 3c results (2026-10-15)

The stop-condition replay (`python -m khipu.routing_replay --live-replica --prompts 200`), run on this Mac
after the nightly finished, against the live replica with the real search, 200 prompts each way in blocks of
20 with the order alternating and the routing helper stopped for the off blocks: 0 timeouts with routing on
and 0 with it off (0.0 added per 100, the limit is 2.0). Median 252 ms on and 261 ms off; 95th percentile
427 ms on and 369 ms off. With routing on, 10 conversations got the full summary, 10 got a change (a limit
that ended partway through the run, with no file changing), and 180 got nothing. The stop-condition is not
met.

### Phase 4: the app (M–L, likely two sessions)

Read the approved mock first; if it is not approved, stop and ask. Plan usage phase 2 must be merged first.
Inspect `PlansCard.tsx`, the fixed-argv commands in `lib.rs` (`khipu_usage_status`, `khipu_usage_refresh`),
`healthRows.tsx`, `doctorAttention.ts` and `OptionalFeatures.tsx` before writing the card.

- Build the Home card, Settings → Routing, the health row and the attention items, through fixed-argv
  commands only, for what phases 1a to 3c built: accounts (pause, lift, clear, dismiss), the note, the rules,
  holds, the master switch and the start and each-prompt switches with their sizes and budgets, job runner
  folders, the lane map, the table file and the old off switches. Check now and the reset-check settings
  arrive with phase 5, the delegation switch with phase 6 and the cloud copy with phase 7, each in its own PR.
- Verify with the app build and its tests, then the running app at the mock's widths, with screenshots
  written to files rather than read inline.
- Stop-condition: a control in the mock whose feature is not built yet stays out of this PR; the app never
  ships a control that does nothing.

Useful alone: Matt sees and changes all of it without a terminal.

### Phase 4b: the owner's priority (S–M, one session)

The owner asked on 2026-10-15, after upgrading the second Claude account: "Does Khipu have the ability to
prioritize an account? Say, like, this account's the priority at the moment?" and then "Think about it.
Make sure you've thought it through with what we need and what I'm trying to accomplish, and do it." What
he is trying to accomplish: spend each plan's allowance where it is worth most before it resets (this week:
use the second Claude account hard before its Saturday 3 pm Eastern reset, keep this account for
orchestration), and have every conversation in every project follow that without him editing rules or
typing in a terminal.

This stays inside "Khipu reports, it does not route". The priority is the owner's stated choice, reported
word for word like his note and rules; Khipu never computes, reorders or enforces it, and a conversation
with better live information may still route differently and say why.

The record gains one owner-held item, **the priority**: an ordered list of accounts (from the accounts plan
usage lists), each with an optional role in his words ("all Claude work first", "orchestration only",
"cross-family reviews") and an optional end time, plus an optional end time for the whole list. Writes:
- From the app (Settings → Routing → Accounts → "Priority"): add an account to the order, move it up or
  down, remove it, set its role and its end, clear the whole priority. Owner writes, marked via=app.
- From a conversation at the owner's request (the same mechanism as standing rules phase "rule edits"):
  written at once and live, marked "set by <conversation> at the owner's request; the owner has not
  confirmed it yet", with the priority it replaced shown beside it until he confirms in the app with one
  tap, or undoes it. Confirming, undoing and clearing stay owner-only.
- The CLI with a typed yes, as today for owner commands.

What conversations see, first in every summary and in every change push, before the account lines:
"Your priority (your words; guidance, not a gate): 1. Claude second, until Sat 3:00 PM: all Claude work
first; 2. Codex: cross-family reviews; 3. Claude: orchestration only." Each entry also carries the facts
Khipu already has about that account, never a judgement: its state (open, limited until …), and, when plan
usage can read it, how much of its window is used and how long until it resets ("12% of its week used,
resets in 2 d 4 h"). If the first account is limited, down or paused the line says so beside it; Khipu does
not promote the next one. When an entry's end time passes it drops out on its own (a clock-driven refold,
as limits do) and conversations hear "Priority on Claude second ended"; when the last entry ends, "No
priority set". A priority naming an account that is not in the accounts list is kept and shown as
"not in Settings → Models".

Tools: `khipu_route` returns the priority as data (labels, roles, ends, confirmed or not);
`khipu_route_priority` sets it at the owner's request (a conversation passes the full ordered list and the
owner's words for what he asked); `khipu route priority` shows it, and with a typed yes sets or clears it.

App: the Home Routing card shows the priority at the top ("Priority: Claude second until Sat 3:00 PM, then
Codex"), with the unconfirmed mark and Confirm/Undo when a conversation set it. Settings → Routing →
Accounts gets the Priority controls above the account list. Every control is a fixed-argv command with
--from-app, argv from validated fields only.

Doctor: warns when the priority is unconfirmed for more than a day, when its first account is limited or
paused, and when an entry names an unknown account.

Tests: order, roles and ends survive a refold and a restart; an end drops the entry and tells
conversations; set at the owner's request is live, marked and shows the replaced priority; a conversation
cannot confirm, undo or clear; the summary puts the priority first and never truncates it; the app's
controls send the right argv and a value can never become an option; Khipu never reorders the list.

Stop-condition: if a piece of this would make Khipu pick, reorder or enforce, it stays out and the build
reports it.

**Spend pace (the owner, the same day):** "it would be nice if the model in conversations is like, okay,
there's a lot of usage there left, but very little time. Whereas like ChatGPT, I've got over ninety percent
but a week to use it, so there's not as much of a rush." So for every account plan usage can read, Khipu
reports a fact beside it: what is left of its longest window and the time until that window resets, and the
pace that would use it up by then ("Claude second: 88% of its week left, resets Sat 3:00 PM (2 d 4 h): about
1.7% an hour to use it all"; "Codex: 92% of its week left, resets Thu (6 d 20 h): about 0.6% an hour"), in
plain words a model can act on ("a lot left, little time" / "plenty of time"). These are arithmetic on
figures Khipu already reads, never a choice. When the owner has set no priority, the summary lists accounts
in order of that pace, fastest first, under the heading "Accounts by how soon their allowance runs out
unused (Khipu's arithmetic, not your priority)"; the owner can switch that ordering off in the app (a
setting), and a priority he sets always comes first and is never reordered. An account whose usage cannot
be read says so ("plan figures can't be read: sign in again") and gets no pace, never a guess. Tests: the
pace arithmetic on fixed figures and a fixed clock; a window that resets before the next one; 0% and 100%
left; an unreadable account; the ordering switch; the owner's priority never reordered.

### Phase 5: the check at reset times (S, one session)

Inspect `recall_daemon.run`, `usage.probe_account`, `_try_refresh_lock`, and the phase 1a fold before writing
the scheduler.

- Build the schedule, the single-account probe, the busy-Mac wait, the recorded outcomes and Check now, and
  this phase's part of the app: the reset-check settings, the Check now buttons and the "checks again at"
  text. Add a line to plan usage's Decisions pointing here.
- Tests with a fake clock and fake probes: one probe per reset; retries bounded; a later reset time moves the
  next check; the busy-Mac wait defers and then records; Muse and an unreadable home record "can't be checked
  without using a turn"; no probe starts while another holds the lock; a restart reschedules; checks that
  fell due during sleep run once on wake.
- Stop-condition: if an account's check would need a model turn or a credential, it records "can't be
  checked" instead; if that seems wrong for an account, stop and ask.

Useful alone: a reset stops being a guess.

### Phase 6: push at the moment of delegation (S–M, one session)

Read the phase 0 hook check first. If neither harness delivers PreToolUse context, build only what the check
confirmed (SubagentStart context for Claude Code subagents, if that works) and report the rest. Then inspect
the hook installers in `khipu/integrations.py` (`:650-672`, `:1555-1575`) before writing the hooks.

- Build a PreToolUse hook matched only to delegation calls: the Agent tool, `delegate_task`, and shell
  commands that start `claude -p`, `codex exec`, `grok`, `muse exec` or a configured dispatch script. It sends
  the same current-state lines as the per-prompt push, under the same seen marker and budget, and it never
  denies anything. Where the check confirmed it, a SubagentStart hook gives a Claude Code subagent the full
  summary. This phase's part of the app is the "before handing work to another agent" switch.
- The Grok Build version waits for Matt's go and must refuse inside Aegis, as Khipu's hook scripts already do.

Useful alone: an orchestrator in the middle of a long turn learns of a limit before it delegates.

### Phase 7: the copy for cloud agents (M, one session plus a gateway deploy)

Inspect `ops/migrations/` (the latest is `0026`), the handlers in `khipu/gateway.py`, `_via_https_gateway`,
and how the Mac writes to the hub today before writing the migration.

- Add migration 0027 with a small routing view table. The daemon writes the view on each version change with
  a trailing debounce of 30 seconds, so the last change in a burst is always written; it retries with backoff
  while the hub or gateway is down and records failures in doctor. The copy carries account labels only (no
  login homes), hold words only when Matt opts in, and the rules and his note only under their own opt-in.
  `khipu_route` on the gateway answers from it with `as_of`, and with `stale: true` and "this Mac has not
  reported since HH:MMZ" once `as_of` is older than 10 minutes (a setting).
- A second Mac's `khipu route` and hooks read this copy, read only, when that Mac has no routing sources of
  its own, with its age and the stale mark.
- This phase's part of the app: the "Copy for cloud agents" settings.
- Deploy the gateway as its own step ([[merged-is-not-deployed]]).
- Stop-condition: anything beyond account state, the opted-in hold words and the opted-in rules and note
  stays on the Mac.

Useful alone: Grok Bot, any cloud agent and a second Mac can ask what is open.

### Phase 8: cutover (S per step, Matt's go)

The cutover steps above, in order, with the dry run first. This work is outside this repository, and none
of it is done without Matt's go.

## Job runner tasks (filed with the runner's project, not Khipu work)

- The runner has no Codex limit path: its text limit detection skips Codex (`lanes.py:227`), so a Codex weekly
  limit is never recognised.
- A Grok check that is killed or limited is filed as an unclear verdict and flags the pipeline
  (in the runner's Grok check launch script).
- Settings are read once at start (`lanes.py:2441`), so every cap change is a stop, an edit and a restart (57
  such stops in about 15 hours).
- Muse's reset time is misread: only "resets 8pm (zone)" parses (`lanes.py:179`), so Muse's form "resets at
  <ISO time>" falls back to 30 minutes.
- Wanted from the runner, so Khipu stops inferring: write the basis of each `until` on the LIMITED line and
  in `limited`; call `khipu route report` from `note_limits`; put `khipu route --brief` into each brief and
  into the Muse and Grok launchers; optionally read `view.json` in `lane_ctx`; replace the hand-typed relaunch
  scripts with `khipu route wait`. The same applies to project B's copy.

## Oracle

Hermetic pytest from `packages/cli` (temp `HOME`, `KHIPU_KEYCHAIN=0`; the baseline is the named failures, not
green); `ruff check --no-cache packages/cli`; for phase 4 the desktop build and its tests. At most three oracle
runs per change. Every phase merges and then reaches this Mac only through `khipu promote` and its live gate;
until the gate's migration has run on this Mac, a merged phase waits rather than being pulled into the main
checkout. The builder and the re-checker of each phase come from different model families. Grok may run
read-only surveys but builds none of this feature's safety parts (the source-path refusal, the injection
fence, the author stamping, the redaction of runner lines). No -fast tier; no paid xAI API.

## Decisions

1. Khipu reports; it does not route (Matt 2026-10-14, quoted above). No tool, command or app control returns
   a pick, or refuses, launches or reorders work.
2. Live is the point (Matt 2026-10-14: "Dynamic and live routing is certainly the point"). A running
   conversation must learn of a limit, reset, hold or outage before its next decision about where to send
   work; the per-prompt push, the clock-driven refold and the delegation-time push exist for that.
3. The record is per account, not per lane, keyed as the usage cache keys accounts.
4. Every reset time says where it came from (cli, estimated, checked, owner); a guess is never shown as fact,
   and a conversation cannot declare its own report to be more than a guess.
5. Standing rules are first-class, never truncated and always first. Matt's note follows them, word for word.
6. "Owner only" is a convention and is written down as one; writes are author-stamped by Khipu, and holds from
   conversations expire.
7. The background check at each known reset time was approved on 2026-10-14. It changes plan usage's decision
   "Cache with a short max age rather than a daemon" for this one case, and phase 5 records that in plan
   usage's Decisions. It checks accounts that are limited until a known time, not open ones; Matt's approval
   of the mock confirmed that reading (decision 20).
8. Reconciling runner files means checking their modification times in the resident recall daemon every 10
   seconds and waking for the next clock-driven change; it starts no process. Probing stays on demand, plus
   decision 7.
9. Runners' files are read and never written, and only the named keys of a runner's settings are read. The
   lanes runner stays in its own project, and its bugs go back there.
10. Doctor warns and never blocks; the record stays guidance, not a gate.
11. Until the cutover the table is read only and is never a source of account state; only the fact that its
    NOW block changed is pushed, never its prose. Its holds are re-entered as records, and Matt's priority
    lines move into his note at the cutover.
12. Push ships off in the public product. On this Mac Matt switches it on after phase 3c's live check, and is
    asked then whether to retire or narrow the old hook. Generating the NOW block needs his go.
13. Every size, budget, interval and expiry here is a setting Matt can raise or remove in Settings → Routing.
14. Nothing new leaves the Mac except the phase 7 copy, which carries labels rather than login homes, hold
    words only when he opts in, and the rules and his note only under their own opt-in. Khipu holds and reads
    no credential.
15. Matt can end any limit or down report on the spot: Clear treats the account as open until newer evidence
    arrives, and Dismiss drops one report.
16. "Open" always names its witness and age. With no evidence, or only stale evidence, the account is Unknown,
    never open.
17. A conversation's own words never reach another conversation by push; only structured values and the
    author Khipu set do.
18. Matt's direction on where work should go lives in his note, in his words; Khipu shows it and never turns
    it into a pick.
19. The public product ships with no standing rules. This Mac's are imported once from a local file and stay
    marked "seeded" until Matt confirms them.
20. Matt approved this scope and its mocks on 2026-10-14 ("Looks good. Proceed."). The mock README asked him
    to confirm three things on approval, and the approval confirms them: the reset check covers only accounts
    at their limit (decision 7); the seven seeded rules and their wording, which still arrive marked
    "seeded" until he confirms each in Settings → Routing (decision 19); and his note as the place for his
    direction on where work should go (decision 18).
21. The owner approved conversation-requested live standing-rule changes with later owner reconfirmation: "you should be able to update that rule when I ask you to, and then I go to reconfirm it. I understand that I need to manually confirm. I'm good with that."

## Known limits

- **A record that starts ahead of the clock (phase 1b).** When the record's first two or more lines came from
  a Mac whose clock read ahead, a Mac whose clock reads before all of them takes the newest line's time,
  keeping its own reading beside it (`clock_at`), until its clock passes the record's first line; doctor then
  names that Mac's lines as the ones with the wrong date, and also warns that the record holds times ahead of
  this Mac's clock. It is the mirror of the fixed limit below, and times alone cannot tell the two apart. A
  clock behind only part of the record (some lines written before it) is not affected: those lines keep the
  phase 1a handling. A single line written ahead after a correct history is not this case: a line that copied
  its time is never a witness to that time, so the ahead line is pulled back once a correct line follows.
- **A clock that reads low, but not before the record began (phase 1a handling kept).** A Mac whose clock is
  more than `CLOCK_TOLERANCE` behind writes its own time when the record holds lines written before that time.
  If such a run is longer than the lines written in the stretch it is behind, the fold still keeps the low
  run, as in phase 1a. The case the phase 1a limit describes (a clock reading 2001, behind every line) is fixed
  for a record that holds two lines or more; a record of one line is the next limit.
- **A record of one line (phase 1b).** A writer takes the record's time only when the record holds two lines or
  more. With one line, a run of lines from a clock reading before it (a clock reading 2001) keeps its own
  times, and once that run is longer than the history, the fold keeps the low run as in phase 1a: the one
  line reads as old (an open report shows as Unknown), and doctor names that line as the one with the wrong
  date. Stamping a record of one line would let a single line written ahead of the clock pin every later
  line (the mirror case above), so it is left. It cannot start once the record holds two lines, and the log
  keeps every line as written. Shown by `test_a_low_clock_run_after_a_one_line_record_keeps_the_phase_1a_handling`.
- **A routing folder that can never be saved (phase 3b).** When the view cannot be saved (a read-only data
  folder), a change is never taken for no change: each prompt logs `routing=skipped:unsaved` and leaves the
  seen marker where it was, so the change arrives at the first prompt after a save succeeds. On a folder that
  stays read-only, a conversation that already has a marker gets no per-prompt changes until then; new and
  compacted conversations still get the full summary at session start.

- **The daemon's poll runs only while its routing helper runs (phase 3c).** The refold between prompts runs in
  a child of the recall daemon's routing helper, and the daemon starts the helper only when the per-prompt push
  is on (at its start, or at the first prompt with the push on), then keeps it running while the push stays
  on. With the push off, nothing polls: the helper reads the switches (the feature, the old hook's bypass,
  `routing_push_prompt`) and `routing_poll_interval` before every poll and every 5 seconds and forks no poll
  child while the push is off, and the daemon stops an idle helper within 2 seconds of the push going off.
  With the push off and reset checks on (phase 5), the helper keeps running for the checks alone and still never polls: it has no poll interval, never wakes at `valid_until`, and wakes only at the next reset check, after a wake from sleep, or when view.json changed; that child reads the saved view and refolds only through a check that is due (fix round 1). It also wakes when an owner's pause over a limit with a reset time ends, and that child refolds once so the check the pause hid is seen (fix round 2).
  `khipu route`, the tools and doctor refold when they read, as before. A poll interval set to 0 and back is
  used within 5 seconds, not at the view's `valid_until`. The helper also looks at view.json every 5 seconds
  and polls once when it changed, so the `valid_until` a write saved there is waited for: a limit written after its last poll
  still refolds at its end with the poll interval at 0 or long. One fold at a time: a prompt's fold, the poll's, the
  session start's and a reader's (`khipu route`, the tools, doctor) each take their turn under
  `prompt-fold.lock`, wait for a running fold within their own budget and answer the view it saved; a reader
  whose budget runs out answers from its own fold, unsaved, and a prompt skips routing for that prompt
  (`routing=skipped:fold-busy`); its marker stays, so the change arrives with the next prompt. A write folds
  under the routing lock, since its new line makes any fold beside it unsavable. Doctor's per-prompt rate
  counts every prompt that went without its routing lines for lack of time: `skipped:budget`,
  `skipped:fold-busy` and `skipped:busy`, each also shown apart.
- **The gate's replay runs the one-shot hook, not a warm daemon (phase 3c).** The promotion gate replays each
  search through the candidate's hook with routing off and on and fails it when the routing read is skipped
  for any reason but a benign one (`fold-busy`, `unsaved`): it ran out of its budget, failed, or its reader
  died before it answered (`skipped:no-answer`); when the candidate's own code says its push is on and its
  hook logs no routing status; when its search with routing on times out, fails or runs past the 1.2 s
  budget (the same test as the run with routing off); or when routing changes the search's answer. A failing pair is replayed once
  more before the step fails (a read that refolded once because a runner's heartbeat changed mid-replay is
  timing, not the candidate). Every process the gate starts runs in an empty folder of its own, so a gate run
  from a checkout never imports that checkout's code in place of the candidate's. It measures the search's milliseconds both ways on the
  one-shot path only: the warm service runs the live code, not the candidate's. The warm-daemon measure is
  `python -m khipu.routing_replay` (the stop-condition: at least 200 prompts each way through a warm recall
  service with its routing helper, timeouts per 100 for each). Its helper runs with the push on, and the
  replay stops before the first prompt unless the helper refolds a seeded limit's end with no prompt asking,
  so the poll's cost is in the measure.

## Fixed limits

- **A Mac clock that reads low for a while (phase 1a, accepted by Matt 2026-10-14; fixed in phase 1b).**
  Fixed by settling each line when it is written: the writer holds the log lock, sees the record, and records
  a sequence number (`seq`) with its line, which the fold orders by; when the writer's clock reads more than
  `CLOCK_TOLERANCE` before every line of a record of two lines or more, its line takes the newest time the fold
  settles for the record and keeps the clock's reading in `clock_at`, and doctor names it from that reading.
  The fold counts such a line from the line written before it and never as a witness to its copied time. Lines without `seq`
  (written by phase 1a) keep their handling. Proved by `test_a_run_of_lines_from_a_clock_reading_2001_never_ages_the_history`
  (a history of two lines; N = 0, 1 and 10: the history keeps its times for every N, and doctor names the
  2001 lines, never the T0 lines). A history of one line is not covered (Known limits). The text as accepted: The fold decides which
  out-of-order line has the wrong time from the log alone. When a Mac writes a run of lines while its clock is
  more than `CLOCK_TOLERANCE` (5 minutes) behind, and that run is longer than the history written before it,
  the fold keeps the low run and reads the earlier history as old: reports show as Unknown, an owner hold
  comes due for its reminder early, and doctor names the correct lines as the ones with the wrong date. More
  correct lines afterwards do not undo it. Nothing is lost: the log keeps every line as written, so the
  phase 1b fix re-reads it correctly. Three fix rounds each closed one clock case and opened another, because
  times alone cannot always say which clock was wrong; the phase 1b fix orders lines when they are written.

- **A key made only of letters, used as a lane, model or pipeline name (phase 1b).** Khipu keeps no text
  from runner files or the table, only parsed fields and names. A name is withheld when it fails the shape
  check, matches a known key prefix, is UUID-shaped, or holds a run of 16 or more letters and digits mixing
  both. A secret made of letters only, or short enough to look like a word, cannot be told from a name and is
  shown as one. Runner names are written by the owner's own tools, so this needs a key pasted in as a name.

## Unverified

- Two edges of the phase 0 hook check. The desktop wakes and typed prompts that left no hook-context
  attachment (about a third): probably a hook that printed nothing, not shown. And whether Codex's live user
  hooks also ran during the scratch runs, since `--ignore-user-config` was used.
- Checked 2026-10-14 (phase 1b): the runner-basis rule holds for project B's copy of `lanes.py` (the copy with
  the newest heartbeat). Its `limit_until`, `LIMIT_SECONDS`, `LIMIT_MAX_REST`, `iso` and LIMITED line format
  are the same as project A's; the two differ only in verdict hedging and the `confirm_lane` handling. One
  correction to the rule as written: both copies take the 30-minute rest from one clock reading and stamp the
  LIMITED line from a later one, both cut to whole seconds, so the guess lands up to a few seconds short of
  30 minutes (a real line: 03:09:03 with until 03:39:02). Khipu reads a gap within 5 seconds of 30 minutes as
  the guess. Project B's folder keeps the same layout (`events.log`, `config.json`, `state/global.json`,
  `state/heartbeat.json`), with a 30-second heartbeat interval against project A's 20.
- Which standing rules doctor watches is read from the rules' own words (phase 1b): "-fast", "xAI", Grok with
  privacy or safety, and Grok with a re-check or another model family. A rule worded otherwise is not watched;
  the app's "Watched" mark (phase 4) should show this to Matt.
- Whether Grok Build's PreToolUse context reaches its model in headless runs.
- What Muse reads at start, and whether it sees the table at all.
- Whether `~/.grok` and `~/.grok-dispatch` are two logins on one Grok plan. If they are, they share one limit,
  and the lane map maps both to one account.
