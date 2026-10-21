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
- **Conversation reports.** An agent that hits a limit can report it.
- **You, in the app.**
- **The routing table file** (setting `routing_table`). Read only, as a pointer. It is never treated as account state.

Provider definitions are owner facts (`definition_set`, `definition: provider`),
written with `khipu route definition set provider --value='<JSON>'` (terminal
confirmation), `--owner-direction` in an eligible owner conversation, or
`khipu_owner_act` action `route.definition.set` with
`{"definition": "provider", "value": {"id": "glm", "label": "GLM", "family": "Zhipu"}}`.
Use the same action with `definition: account` and
`{"id": "glm:main", "label": "GLM", "provider": "glm"}` to track an account.
Both writes are logged and support Undo, which appends a restoration or removal
fact. Settings → Accounts and routing → Add provider account creates GLM or
DeepSeek provider facts and an account; existing provider restrictions are kept. Fields are `id`,
`label`, `family`, `login` (`folder` or `none`), `probe` (`claude`, `codex`,
`grok`, `muse`, `glm`, `http-quota`, or `none`), optional hostname `host`,
`lane_names`, optional exact model ids in `models_allowed` and `models_never`,
and `use` (`normal` or `on_request`). With `probe: http-quota` and a `host`,
the periodic usage refresh reads `https://<host>/` for each account of the
provider: HTTPS only to that confirmed host, redirects refused, and the key
stored as `<provider>_api_key` read from the Keychain only at request time
and sent only in the request header. Only the parsed quota windows are
kept; failures carry a fixed code (a redirect is `redirect_refused`), never
a response body or header.
Account definitions name the provider; login folders remain local bindings.

The rule 12 tier table is a structured fact (`tier_table_set`, `tiers` naming
every task class: `scope`, `mock`, `ruling`, `hard-security`, `failed-fix`,
`build`, `fix-round`, `tests`, `docs`, `synthesis`, `mechanical`, each with a
tier of `opus`, `sonnet` or `haiku`), written with
`khipu route tier-table set --tiers='<JSON>'` (terminal confirmation),
`--owner-direction` in an eligible owner conversation, the app's owner path
(`--from-app`), or `khipu_owner_act` action `route.tier_table.set` with
`{"tiers": {...}}`. `khipu route tier-table confirm` confirms a table a
conversation proposed (`set --at-owner-request '...'` writes a proposal that
never governs on its own), and `clear` removes the table. Only a confirmed,
owner-written table governs the advisory dispatch check. Each owner write is
logged, and Undo restores the prior table (or clears it when there was none).
No adapter or provider fact launches a probe by itself. Model and use checks
are advisory. DeepSeek's rule 13 model restriction is `models_allowed: [deepseek-flash]`,
not merely a ban on one Pro model. Spend and concurrency remain with slices
17a/17b; rule 13 stays standing until those facts are represented and verified.
An undeclared model causes no model-violation warning. `use: on_request` is a
neutral reminder: the dispatch hook does not establish whether the owner asked.

Runner identity contract:

- A lane's `config.json` entry may declare `account`, `budget_account`,
  `provider` (or existing `kind`), and `login_home` (or `home`). All supplied
  explicit identity fields must agree with the model's known provider.
  A known model family outranks transport `kind` (for example `kind: deepseek`,
  `model: glm-5.3` bills GLM). Claude transport can bill GLM or DeepSeek.
  Cursor always bills the unified Grok account; Auto's model family remains
  unknown for cross-family checks. A tier name such as `main-sonnet` needs a declaration.
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

Runner budget declarations (slice 19b). A runner's `config.json` may carry a
`budget` object mapping an account (its key, a `provider|` prefix or a
label) to a list of cap declarations. Each declaration is the same shape as
an owner cap fact: `{"kind": "share"|"per_day"|"pace_target", "window":
"weekly"|"five_hour", "pct", "slack"}`, `{"kind": "spend", "usd", "days"}` or
`{"kind": "concurrency", "n"}`. Khipu compares each declaration with the
owner-confirmed cap facts and reports the ones that have no matching fact or
disagree with the fact they have; a runner value never becomes a fact, and
the runner's files are never written. A lane's own `cap` is local capacity
on that Mac, not a quota, and is not compared. `khipu route --json` and
`khipu doctor` carry the report as `runner_caps`.

Caps by purpose. A cap fact may carry `purpose: "job"` (the default for
old facts too) or `purpose: "check"`. Only `pace_target` accepts check purpose; share,
per-day, spend and concurrency caps reject it. A check `pace_target` is a separate
allowance for that account and window. Without one, checks use the job
pace target. Setting, removing and undoing a check cap leave the job cap
alone. The existing owner direction, app and typed-terminal gates apply.
`khipu route --json` includes the purpose on every `cap_statuses` row and
separate `advice` and `lead_advice` objects, plus `check_advice` only when a
check cap exists. Settings shows the check allowance beside the job allowance,
with its window and qualifiers.

The owner's check exceptions can be written with these commands (a terminal
asks for typed confirmation):

```sh
khipu route cap set Codex --window weekly --kind pace_target --pct 100 --slack 5 --purpose check
khipu route cap set Claude --window weekly --kind pace_target --pct 100 --slack 20 --purpose check --tier sonnet --sensitive-only
```

These add check allowances to the existing job caps; they do not set job
caps or share ceilings. Codex's job cap is even weekly pace with zero
slack and a 95% share ceiling. Main Claude's job cap is even plus 5 with
a 91% share ceiling. The Claude check allowance is for Sonnet checks of
sensitive code. Check caps accept optional `tier` (`opus`, `sonnet` or `haiku`)
and `sensitive_only` (boolean). A qualified allowance applies only when the
call matches every qualifier; otherwise checks use the job cap for that window.
An unqualified check cap, such as Codex's, applies to checks on any code.
`--sensitive` is declared by the caller, not detected or verified by Khipu;
it is separate from `--privacy`, which retains its account-eligibility meaning.
The caller names that work explicitly:

```sh
khipu route advise --lane codex-check --purpose check --kind check --json
khipu route advise Claude --purpose check --kind check --tier sonnet --sensitive --privacy --json
khipu route cap remove Codex --window weekly --kind pace_target --purpose check
```

Runners use the same `purpose: "check"` on `khipu_route_advise` and their
budget declarations. MCP advice also takes caller-declared `tier` and
`sensitive`; runner budget declarations carry the cap's optional `tier` and
`sensitive_only`. A tier inferred from the task table does not count as a caller
declaration for a qualified check allowance. Purpose selects the pace allowance;
it does not grant model or privacy eligibility. Share ceilings close every
purpose at the ceiling. A reached spend ceiling closes orchestration and holds
jobs and checks back. Full concurrency holds jobs and checks back; orchestration
never pauses for pace, daily pacing or concurrency. Owner holds, account pauses
and provider limits still close an account. Advice lines and
pushes identify their purpose, for example `Advice for checks: Codex: go`.

Runner telemetry contract (slice 17t). A runner's `state/heartbeat.json` may
also carry three typed, timestamped records. Khipu reads them through the
same bounded parser as the rest of the heartbeat: only parsed fields
survive, names pass the same shape check, and no other byte of the file is
kept.

- `active`: a snapshot list of `{"lane", "account", "jobs", "unit", "at"}`.
  `unit` is `job` today: a GLM job's requests go in turn, so one job is one
  request at a time. The newest snapshot for a `(runner, lane)` replaces the
  earlier one, and every snapshot expires at the runner's silent deadline.
  A heartbeat with none of the telemetry keys also expires the snapshot.
  When a lane is idle the runner sends `jobs: 0` for that lane: idle reads
  as a known zero only from such a record, never from a missing one, so a
  lane the runner stops mentioning reads unknown, not zero.
- `spend`: a list of increments, never a running total:
  `{"event", "job", "attempt", "provider", "account", "usd", "at"}`.
  `event` is a uuid the runner mints; records are deduplicated on it, so a
  heartbeat may republish its recent spend. Each retry is its own `attempt`
  and its own event, so retries count. A correction is a new event with
  `corrects: <event>` and the signed difference. Khipu accumulates the
  events across heartbeats (bounded; what it cannot keep marks the
  accumulation incomplete).
- `spend_coverage`: a list of `{"account", "from", "to"}`: the interval for
  which this runner vouches it reported every spend for that account.
  Khipu records when it started accumulating each runner's telemetry;
  that start is kept in the saved view and resets whenever the kept
  accumulation is discarded or the view is lost, and a period that starts
  before it reads unknown, never a known understated total. A correction
  whose target Khipu cannot find in the same stream and period (it names
  no account, or its target lies outside the period) is dropped from the
  figure and marks the affected stream, or the unattributed bucket,
  incomplete, so the total reads unknown instead of silently omitting it.

Each record is attributed to an account through the same lane resolver as
limits and reports. A record no resolution can name is unattributed, never
guessed. An account's spend total for a period is known only when every
runner that took part in the period covers its own participation interval;
one runner's gap makes the total unknown even when another covers the whole
period, two accounts on one provider never pool, and a silent runner's
records expire to unknown. Missing or partial coverage is unknown, never
zero. Reference emitter (the runner's side, kept minimal):

```python
import json, time, uuid

def heartbeat(path, active, spend, coverage, interval=20):
    doc = {"time": time.time(), "interval": interval,
           "active": [{"lane": lane, "account": account, "jobs": jobs,
                       "unit": "job", "at": time.time()} for lane, account, jobs in active],
           "spend": [{"event": str(uuid.uuid4()), "job": job, "attempt": attempt,
                      "provider": provider, "account": account, "usd": usd,
                      "at": time.time()} for job, attempt, provider, account, usd in spend],
           "spend_coverage": [{"account": account, "from": start, "to": time.time()}
                              for account, start in coverage]}
    path.write_text(json.dumps(doc))
```

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

Rule edits and priority requests must carry `base`, the owner revision returned by the conversation's earlier `khipu_route` read. A missing or unread base is refused; later reads never fill it in. The CLI takes the same revision as `--base` with `--at-owner-request`. Replacement notices name the rule number or priority and go only to their author's session, once delivered; undelivered notices expire after seven days.

Sessionless CLI reads, including the app's polling command, create no receipt file and take no routing lock to record a receipt. Sessionless MCP callers can propose against revisions read on the same connection: receipts stay in that server process's memory and never pass to another connection or process. Replacement notices match either the recorded canonical session id (including `t3:<thread>`) or its provider session id.

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

Five tools. Locally all five work as below. Through the HTTPS gateway, `khipu_route` answers only the closed gateway projection (account labels and states, structured holds, and — under their switches — owner hold words, the confirmed rules and note, and the confirmed priority; paths are cut even inside owner words, and pending proposals never appear), and only when the owner has turned on `routing_cloud_copy`. The other routing tools and `khipu_owner_act` answer `available: false` there, and every owner write is refused. A gateway caller's identity is its token label plus its `session_id`: it sees only its own delivery state, and an ack for another identity's batch is refused (`ack_not_yours`). `since` is not available through the gateway. See [MCP tools](mcp-tools.md) for the full list.

The cloud copy is **published by the Mac**, not folded in the gateway container.
Migration `0044_routing_gateway_copy.sql` adds `routing_gateway_copies`, one
current row per salted publisher identity, plus `routing_gateway_delivery` for
receipts. Apply the migration to the hub before deploying the gateway. This
change does not apply a migration or deploy anything itself. The Mac must also
run the updated daemon and have its hub connection configured.

With `routing_cloud_copy` on, each daemon routing poll considers publication,
including polls that do not refold. Changes use a trailing
`routing_cloud_copy_debounce` (30 seconds by default); failures retry with
bounded exponential backoff. An unchanged copy gets a heartbeat at most every
60 seconds (or half the stale bound when shorter). `khipu route copy publish`
forces an immediate attempt, bypassing debounce/backoff. Lock contention skips
that tick without changing health or starting backoff; actual publication and
revocation failures remain retryable errors.

A salted publisher id change (including Mac-id restore) first deletes all
retired ids known to this Mac before adopting the replacement. The full set of
used ids is kept in both publication state and an independent durable
`gateway-publishers.json` journal. Turning the switch off deletes every recorded
id plus the id recomputed from the persisted salt and Mac id, even if
`gateway-copy.json` is missing or corrupt. Deletion failures preserve the ids
for retry; another Mac's row is never included. Completed revocations retain
the history and skip redundant daemon deletes. A Mac without a persisted
routing identity or recorded publisher stays offline when the switch is off;
an existing identity with lost publication state gets one speculative cleanup
only when a hub is configured. Its attempt marker is saved before the delete;
a failure is recorded but never retried on idle ticks, even after its retry time.
These ticks write no files and contact no hub. Publishing again clears the
speculative-cleanup marker. In contrast, recorded publisher ids remain pending
until revocation succeeds, and delete retries honor `retry_at` before taking a
lock or rewriting state. A forced manual publish bypasses that backoff for
recorded publishers. Without a configured hub, recomputed-id cleanup does not
create files or contact the hub.

Only `gateway_projection` computed with the **Mac's** switches and then strictly
scrubbed is uploaded. `routing_cloud_copy_rules` controls confirmed rules, note
and priority; `routing_cloud_copy_hold_words` controls owner hold words.
Conversation hold words, proposals, login paths, session/project names, raw Mac
ids and private history never enter that payload. The hub receives a salted
publisher hash as bookkeeping; it is never returned through the gateway.

The gateway reads the newest hub row (hub publication time, then publisher hash
for ties), never its local routing log or switches. It returns `as_of` from the
hub publication time, plus `stale: true` and a last-report message once that time
exceeds the publishing Mac's `routing_cloud_copy_stale_after`. No row returns
`available: false` with the existing cloud-copy reason. The hub copy is a
snapshot: a disconnected Mac cannot update time-driven account changes, so
clients must honor its stale mark.

Pull delivery persists in `routing_gateway_delivery` in the hub, keyed by a
hash of the authenticated token label and session id. No receipt depends on a
container filesystem or volume. One batch covers the full selected projection;
its digest includes the published epoch/revision, contents and account filter.
A resend keeps the batch id until acknowledged or its 60-second lease expires.
Foreign acknowledgements are refused; duplicates are harmless; unknown/expired
acks consume nothing. Receipt changes commit before a response reports them.
A missing table, database outage or failed receipt commit returns
`available: false` with a fixed durable-ledger-unavailable reason. It never
accepts an ack in memory and silently loses it on restart. Owner writes remain
refused. Receipt rows inactive for more than seven days are pruned in batches
of at most 1,000 during gateway delivery and health refreshes. A live in-flight
lease is retained. At the retention boundary receipts remain; after pruning,
an old ack is unknown and consumes nothing, and the full current copy is offered
again.

The Mac refreshes receipt health at most once a minute during normal publishing;
a forced manual publish refreshes it immediately. SQL returns health fields
only, restricted to the configured live-delivery window (at most seven days)
and at most 512 rows. A probe for one additional row detects truncation, which
makes health unknown with a named missing-evidence reason. Batch and ack history
never travel back to the Mac. Health display itself never contacts the hub.


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

**Directions said in a conversation.** If you type a routing direction in a conversation ("use GLM only one job at a time") and no matching owner change reaches Khipu, capture opens an owed item, "Routing said here, not in Khipu", quoting your words; only you see that quote. A direction counts only when it names an account, provider, lane or model Khipu's record knows; ordinary prompts that merely start with a word like "use" or "keep" open nothing. The item appears in `khipu_owed` (kind `routing_direction`), in `khipu owed` on the command line, and in the pushed slice at session start. It closes when the matching owner change is made from that same prompt (setting the cap to a different value leaves it open), when an owner change cites it, or when you dismiss it with `khipu owed --close <id>` (the owed list's close control, also `khipu owed --dismiss <id>`); a dismissal survives later captures. Questions, quotations and job prompts open nothing. If the prompt recorder missed the turn, the item stays open marked unjoined rather than being matched by guesswork.

## Limits of this feature

- Routing stays local by default. Opt-in Mac replication is described below; the HTTPS gateway projection is gated by `routing_cloud_copy` (with `routing_cloud_copy_rules` and `routing_cloud_copy_hold_words`), not by the replication switch, and deploying it to the live gateway is a separate step.
- Replacing the routing table file with a view generated by Khipu is planned. Today the file is only read.
- A push for stock Grok Build is planned, not built.
- Related work that is planned and not described here: a Work view and work ledger.


## Replication between Macs (slice 11b)

`routing_replication_live` is the owner's switch for network replication
between Macs. It defaults off. Turn it on or off in the app at
Settings → Routing, beside the other Mac-to-Mac switch ("Copy routing
between your Macs through the hub"), or with
`khipu config --set routing_replication_live on --owner-direction` (or
`--from-app` from the app, or a typed `yes` on a terminal). Like the other
routing switches it goes only through that owner gate and is recorded as
an owner action; a conversation's change at the owner's request waits for
him to confirm or undo it. Do not hand-edit `config.json` for it. The app
stores the JSON boolean, and the live check still requires that boolean:
a hand-edited string or number in the file never turns replication on.
The switch's status line in the app ("Replication: caught up", "stale", or
"never synced") comes from `khipu route replication status` (below). The
hub needs migration 0042 and 0043. Enabling it or deploying a running
build is a separate operational step.

- `khipu route replication status` reads local cursors, caught-up time, age,
  source rows and deferred event kinds. More than ten minutes old is stale.
- `khipu route replication sync` runs one exchange through the closed 11a
  projection. With the switch off, it does not construct a transport.
- `khipu route replication restore` assigns a fresh Mac identity and clears only
  the downloaded set and cursors. It preserves `events.jsonl` byte for byte and
  does not connect. The next enabled sync rebuilds the downloaded set; old local
  event IDs are retried idempotently. Run this after restoring a copied routing
  folder to another Mac; a fresh install creates its own identity automatically.
  If a sync holds the lock, restore returns `busy` without changing files.

The daemon's existing routing poll uploads in local sequence order, then pulls
hub-timestamp pages with the snapshot overlap. Polls are capped at 30 seconds
when replication is enabled, within the 60-second two-Mac propagation budget
while the routing helper is running. A stopped helper needs an explicit sync.
The local routing lock is never held over a network operation. Durable receipts
precede upload cursor advancement; durable pages precede download cursor
advancement. Only a completed pull through its snapshot can mark caught-up.
Existing `n`/`local_order` display numbers never change. Previously unnumbered
rules receive the next rule number in hub acceptance order, independent of gaps
or other event kinds in `hub_order`. Assignments persist in receipts and survive
retries and restore. Offline additions keep UUID targets and show “new rule
(number pending)”. Each Mac reserves its existing number range before uploading,
including a numeric high-water mark for id-less legacy rules; those rows never
upload. A Mac's private numbers cannot be known elsewhere before its first sync.
Conflicting historical numbers are preserved, and ambiguous numeric operations
refuse with the matching UUIDs so the owner can target the intended rule.

Unconfirmed words omitted by `routing_proposal_text_between_macs` become a
placeholder on the other Mac. Confirm on the source Mac, where the words are
visible; the confirmation carries those now-confirmed words. Owner words and
local history are not rewritten. Missing or rejected event IDs never upload.

The closed 11a projection does not yet cover newer pace samples, caps and tier
facts. Unsupported kinds are reported as `deferred_kinds`, retained locally and
retried after a projection upgrade; they do not block supported events. No
live hub, live configuration, hook or launch agent was changed for this slice.

## Delivery health

`khipu doctor --json` exposes `routing_delivery` (also inside `routing`) and
`routing_delivery_ok`. The check reads existing delivery ledgers, today's stamped
hook outcomes, replication cursors and gateway delivery rows; it never probes or
changes routing, owner actions, dispatch policy or the promotion gate. Desktop
clients can consume the same check; the app delivery view is slice 15a.

For each identity active within `routing_delivery_live_after` (12 hours), the
current attachment selects `main` for hooked clients and `mcp` for pull-only
clients. Agent components count separately. Child receipts never clear parent
debt. Each revision records its first owed delivery point and the ceiling of
characters ahead plus its own characters divided by the available payload
budget (page framing and the owed tail consume part of the configured cap). Still owed
at that bound plus `routing_delivery_slack` (2) further delivery points is red. The
bound belongs to that revision: a replacement revision starts a new age and budget.
An entity replaced on every delivery point continually restarts this bound;
health does not accumulate stall age across its successive revisions.
Two expired leases for the same revision are also red. An acknowledgement past
its shown receipt is red; old ledgers without receipt proof are unknown.

Other red reasons are more than 1% of today's recorded prompts skipping routing,
a cross-Mac caught-up cursor more than 10 minutes behind, a gateway delivered
projection still behind after `routing_cloud_copy_stale_after` seconds since the
current local revision's `folded_at`, and a limited lane still
unmatched for an hour of one continuous unmatched run. UTC timestamps select today's hook outcomes from the
existing bounded log tail. Copy checks are not applicable when their switches
are off. Enabled copies without timestamps, unreadable ledgers, absent live
identities and missing hook outcomes say `unknown` with named missing evidence,
never green. A gateway identity outside the 12-hour delivery liveness window no
longer supplies copy evidence. A gateway response carrying the current local
revision proves delivery recovery. An unchanged record stays fresh while its
Mac keeps publishing heartbeats, even if callers are idle. Publication age
past the stale bound is independently red, including when clients are inactive. A newly folded revision starts the gateway staleness clock even when a
client last pulled much earlier. Simply calling health
cannot refresh evidence.

Each result carries `status`, `reasons`, `missing`, per-component `identities`,
`prompts`, `replication` and `gateway`. Identity names are ledger hashes; health
rows carry the attachment's harness name (`unknown` for missing or unrecognized
names) and each component's `last_delivery_point`, with no session ids, routing
words or login paths. Green requires affirmative evidence
for every applicable check. Recovery requires the corresponding receipt, current
outcome log, caught-up cursor, delivered projection or resolved lane mapping.
