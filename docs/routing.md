# Routing record and plan usage

Khipu keeps a record of which of your agent accounts can take work right now. It also reads how much of each subscription plan is left.

**Khipu reports. Agents route.** Khipu never picks an account, ranks work, refuses a job, launches anything or reorders anything. Every answer is data, not instructions, and guidance, not a gate. An orchestrator with better live information can route differently. The owner decides the rules. The agent decides the route.

This page covers what the record holds, how agents read it, and how you change it.

Related pages: [How it works](how-it-works.md), [MCP tools](mcp-tools.md), [CLI reference](cli.md), [Desktop app](desktop-app.md), [Integrations](integrations.md), [Operations](operations.md).

## What Khipu reports

### Account states

Each account has one state:

| State | Meaning |
| --- | --- |
| open | It can take work. |
| limited | It hit a limit. A reset time is shown when known. |
| paused | You marked it paused. |
| down | Not signed in, not installed, or out of service. |
| unknown | Khipu has no reliable reading. |
| probably open | The limit time has passed, but nothing confirmed it. |

Accounts are listed in this order: limited, paused, down, unknown, probably open, open.

Each limit time carries a basis, so you can tell how much to trust it:

- **cli**: parsed from the CLI's own limit message or probe.
- **checked**: Khipu checked and saw headroom.
- **owner**: you said so.
- **estimated**: a guess.

The first three are strong. An estimate is not.

### Where the state comes from

- **Plan usage cache.** An account at 100% of a window counts as limited. Headroom after that reopens it as checked. "Not signed in" and "not installed" count as down.
- **Job runner folders.** Folders you point Khipu at (setting `routing_sources`). Khipu only reads them, never writes. It reads the runner's limited flag, the tail of its event log, its heartbeat and a few config keys. Folders inside credential locations are refused.

Provider definitions are owner facts (`definition_set`, `definition: provider`),
written through the existing owner `routing.set_definition` API. Fields are `id`,
`label`, `family`, `login` (`folder` or `none`), `probe` (`claude`, `codex`,
`grok`, `muse`, `glm`, `http-quota`, or `none`), optional hostname `host`,
`lane_names`, optional exact model ids in `models_allowed` and `models_never`,
and `use` (`normal` or `on_request`). `http-quota` is reserved for slice 9b.
Account definitions name the provider; login folders remain local bindings.
No adapter or provider fact launches a probe by itself. Model and use checks
are advisory. Rule 13 remains until its budget and concurrency lines also have facts.

Runner identity contract:

- A lane's `config.json` entry may declare `account`, `budget_account`,
  `provider` (or existing `kind`), and `login_home` (or `home`). All supplied
  fields must agree. A tier name such as `main-sonnet` needs a declaration.
- One resolver serves health, evidence, and reports: the owner's lane map,
  declared identity, provider plus local login binding, then provider id or
  `lane_names`. A provider with several accounts or a name claimed by several
  providers stays unmatched, with candidates and `budget_account` as the repair field.
- Declarations are `claimed`. An ordinary usage or limit reading never confirms
  one. A fresh `state/heartbeat.json` can confirm it with
  `{"time": 1792512000, "interval": 20, "lanes": {"main-sonnet":
  {"provider": "claude", "login_home": "/local/login"}}}`. Raw home fields
  are resolved immediately into local account references and discarded.
- Alternatively, a `LAUNCH` line can carry `lane=main-sonnet
  harness=claude_code session=<job-session-id>`. Khipu joins that reference to
  the job's own capture state (`origin: job` and its hook-recorded `login_home`).
  The line alone cannot confirm identity. Only a hash of the attachment
  reference survives runner parsing; capture retention and Forget apply.
- Health lists `identity_state: claimed|confirmed`, match origin, candidates,
  and repair guidance. Stale or contradictory bindings never match. A failed
  or silent heartbeat cannot confirm identity.
- **Conversation reports.** An agent that hits a limit can report it.
- **You, in the app.**
- **The routing table file** (setting `routing_table`). Read only, as a pointer. It is never treated as account state.

A runner's lane maps to an account through the lane map (`routing_lane_map`). A lane with no match is shown and pushed as one line. Khipu does not guess.

### Holds

A hold is a short note that work should wait: "machine busy", "disk low", "keep", "outage" or "other". A hold can cover one account, one machine, or everything.

These are the defaults. Each is a setting you can change or switch off.

- Hold text is capped at 300 characters.
- A hold set by a conversation lasts 24 hours unless it says otherwise, and never more than 7 days.
- An owner hold is set by you and asks for typed confirmation. A reminder comes after 90 days.

### Owner rules, note and pauses

These are yours:

- **Standing rules.** Plain-language rules such as "never use this account for that kind of work". Khipu shows them and folds them into every summary. It does not enforce them.
- **Note.** One note to every conversation, shown word for word.
- **Pause.** Mark an account paused, optionally until a time. Lift it later.
- **Clear.** Clear an account's state, or dismiss one report.

A conversation can record a rule or a priority only when you asked it to. Khipu keeps it pending until you confirm it in the app. A conversation cannot confirm, retire, dismiss or undo anything.

### Priority and pace

You can set an order for your accounts, with your own words for each role and an optional end date. An order set by a conversation stays unconfirmed until you confirm or undo it. A warning appears if it stays unconfirmed for more than 24 hours.

Pace comes from the plan usage cache. For each account's longest window still in force, Khipu shows:

- how much is left,
- time until reset,
- the even pace needed to use it all (percent per hour).

It says "a lot left, little time" when the needed pace is at least 1.5 times the window's even pace. Otherwise it says "plenty of time". With no priority set and `routing_pace_order` on (the default), accounts are listed by how soon their allowance runs out unused. An unreadable account says why and gets no pace.

This order is arithmetic. It is not your priority and not an instruction.

### Plan usage

Khipu reads the headroom of each subscription CLI without using a model turn: Claude, Codex, Grok Build and Muse. Errors show as short codes. Muse figures are as of its last turn. The cache is `state/usage.json`.

Plan usage shows on the Home screen (a Plans card) and under Settings > Capture & models. You can add and remove accounts there (up to 32). `khipu doctor` carries a cache-only plan usage block that lists a warning per window at 90% or more. Warnings never turn doctor red.

### Sign-in again

If an account is signed out, you can sign it in again:

- In the app: the **Sign in again** button on the Plans card, for Claude and Codex accounts.
- On the command line: `khipu usage --account LABEL --sign-in`.

Either one starts that CLI's own login in its own login folder. A browser opens. Khipu waits up to 15 minutes, then reads the account again. Grok and Muse accounts are not offered this. Sign in from their own CLI.

### Reset checks

When an account's known limit time passes, Khipu can check whether it really reopened. The check is on by default. By default it waits 2 minutes after the reset, then retries at 10 and 30 minutes. If the machine is busy (load above 75), it waits up to 5 minutes, up to 3 times. All of these are settings. A check costs no model turn. Muse and unreadable accounts say "This account can't be checked without using a turn".

Settings > Routing has a switch for this and a **Check now** button per account. On the command line: `khipu route check ACCOUNT`.

## How agents read it

### MCP tools

Five tools, all local only. Through the HTTPS gateway each answers `available: false`. See [MCP tools](mcp-tools.md) for the full list.

| Tool | What it does |
| --- | --- |
| `khipu_route` | Reads the record: account states, rules, holds. Takes `since` (only what changed after a version) and `account`. Read-only. |
| `khipu_usage` | Reads plan headroom per account. Takes `fresh` and `max_age`. Read-only. |
| `khipu_route_report` | Records that an account hit a limit, is open, or is down. Also starts or ends a hold. It refuses rules, notes, pauses and clears. |
| `khipu_route_rule` | Records a rule you asked for. Pending until you confirm it. |
| `khipu_route_priority` | Records an order you asked for. Unconfirmed until you confirm it. |

`khipu_status` also carries a short routing field in its light payload.

Every `khipu_route` answer ends with a reminder that Khipu reports and routing stays with the agent.

### Pushed into the prompt

Agents do not have to ask. When the `routing_push` feature is on, Khipu puts routing into their context at three moments. Each has its own switch, and all three are on once the master switch is on:

| Switch | Moment |
| --- | --- |
| `routing_push_start` | Session start, and after compaction. |
| `routing_push_prompt` | Before each prompt. |
| `routing_push_delegation` | Before handing work to another agent. |

`routing_push` itself is off until you turn it on (Settings > Optional features, or `khipu features --set NAME VALUE`). Setting `ROUTING_NOW_OFF=1` in the environment, or creating the file `/tmp/.routing_now_disable`, also bypasses the push.

What the agent sees:

- **Session start**: a block headed "Routing at HH:MMZ" with the full summary (rules, accounts, holds, pace). It has a time budget of 1 second by default.
- **Each prompt**: only what changed since that session last saw the record, headed "Routing changes at HH:MMZ". The prompt update is capped at 400 characters and a 50 ms budget. The full summary is sent again when the session is 20 versions behind or 6 hours old. A session only counts a change as seen once it was really delivered.
- **Delegation**: a hook watches the moment an agent hands work to another one (agent and task calls, `delegate_task`, and shell commands that start `claude -p`, `codex exec`, `grok` or `muse exec`, plus any dispatch scripts you name in settings). It adds the same changes block. Calls made inside a subagent get nothing. A subagent gets the full summary when it starts (Claude Code).

The hooks never deny or alter a call. The delegation context arrives alongside the tool result, after the call. They are installed by `khipu integrations install` for Claude Code and Codex (Codex runs them only after you approve Khipu's hooks in Codex). Cursor hears routing at session start only: it gets no per-prompt or delegation hook. Aegis refuses these hooks; it, Grok Build and cloud agents hear routing only when they ask. See [Integrations](integrations.md).

A background helper in the warm recall service polls the sources every 10 seconds and refreshes the record on change and on the clock. It stops when the push is switched off.

### Command line

`khipu route` prints the record. Useful flags: `--json`, `--summary` (the text a session gets at start), `--brief` (text for a job runner brief), `--account A`, and `--since V` (only what changed after version V).

Agents can also record what they learned:

```
khipu route report --state limited --account A --until T --message "..."
khipu route hold "words" --reason machine-busy --until T
khipu route hold end ID
khipu route wait ACCOUNT --timeout S
```

`route wait` exits 0 when the account is open and 1 at the timeout. It waits and launches nothing. With `--confirmed` it counts only Khipu's own read (a probe or a reset check).

## How the owner changes it

**In the app first.** Settings > Routing holds everything, in this order:

1. **Accounts.** State, source and age per account. Priority and pace switch, Check now, Mark paused until, Lift, Clear, and dismissing one side of a disagreement.
2. **Note.**
3. **Standing rules.** Confirm, edit, remove, add, undo.
4. **Holds.**
5. **Reset checks.**
6. **Telling conversations.** The master switch, the start, prompt and delegation moments, what each harness hears, sizes and times, and dispatch scripts.
7. **Job runner folders.**
8. **Which account each job lane uses.**
9. **Routing table file.**
10. **Recent changes.**

The Home screen has a Routing card with one row per account, holds, your note, a priority headline, and attention items (a broken rule, a silent runner, two sources that disagree). It also has a Plans card for usage.

**CLI equivalent.** Owner commands ask for typed confirmation in a terminal and refuse without one:

| Command | Does |
| --- | --- |
| `khipu route pause ACCOUNT [--until T]` | Pause an account. |
| `khipu route lift ACCOUNT` | Lift a pause. |
| `khipu route clear ACCOUNT [--report ID]` | Clear an account, or dismiss one report. |
| `khipu route note set TEXT` / `note end` | Set or end the note. |
| `khipu route hold WORDS --owner ...` | Set an owner hold. |
| `khipu route rule add\|edit\|retire\|restore\|confirm\|undo\|dismiss` | Manage standing rules. |
| `khipu route priority show\|set\|confirm\|undo\|clear` | Manage the priority order. |

`khipu route sources` lists each job runner folder with its last good read and any error. Run `khipu route --help` and each subcommand's `--help` for every flag. See the [CLI reference](cli.md).

## Limits of this feature

- The routing record is local. The HTTPS gateway does not serve it yet, so cloud agents cannot read it. Sharing it with cloud agents through the hub is planned.
- Replacing the routing table file with a view generated by Khipu is planned. Today the file is only read.
- A push for stock Grok Build is planned, not built.
- Related work that is planned and not described here: a Work view and work ledger.
