# Routing as a first-class feature (2026-10-20)

Asked by Matt 2026-10-20: "A routing system that can change dynamically needs to be a first class feature. We
depend on this." Status: DRAFT, revised after Codex review round 1 (six P1s, one P2; see Review responses).
Nothing here is built until Matt approves it, and no UI is built until its mocks are approved.

Read first: `2026-10-14-routing-truth.md` (the record, its sources, the push, decisions 1 to 21) and
`2026-10-20-routing-delivery.md` (today's fixes A to F, slice 1 here). This page adds to them and never
restates them. Where it changes a decision, it says so. It replaces phase 7 of the routing-truth plan with
slices 11a to 12 below, because phase 7 copied the record in one direction only and replicated no
observations.

## The heart of it

Khipu's record is right, and conversations still act on stale routing. Each failure below happened in real
work on 2026-10-14..20. Every one comes from one of three gaps:

1. **Delivery is version-based, not change-based.** A session's marker is a single version number, so "shown"
   and "delivered" are the same thing, and anything a push left out is lost (`recall_prompt.py`
   `mark_routing_seen`, `routing.py` `_fit_push`, `prompt_push`).
2. **The record mixes kinds of truth.** Owner words carry account levels that go stale, a priority can
   contradict a hold, and nothing types which is which.
3. **The record is one Mac's.** Its event sequence, rule numbers and account ids are local (`routing.py`
   `_stamp_locked`, `_next_rules`, `_id_salt`). Its observations are folded in from local sources and never
   persisted (`_fold_body`, `routing_sources.collect`). Memory files and CLAUDE.md keep copies of it.

| Failure seen | Gap | Fixed in |
|---|---|---|
| A pause stayed live for a day after Matt released it: the release was written as a priority change, and the hold (`h125d608f`, ended by hand 2026-10-20 10:40Z) was never ended | 2 | 1 (B, F), 4 |
| Unconfirmed or long changes collapsed to "too long to show here"; this session's own start block was the pointer alone | 1 | 1 (A), 6 |
| Changes left out of a prompt update were marked delivered | 1 | 1 (C), 6 |
| One conversation's unconfirmed change blocked every other conversation (`request_priority`'s pending check) | 2 | 1 (D), 5 |
| "Codex is low" beside a reading of 96% left | 2 | 1 (E), 4 |
| Nothing compared a dispatch's model with rule 12; every fix round ran on Opus | 2 | 10 |
| GLM and DeepSeek runner lanes match no account unless mapped by hand (live: church-harness lane `glm`, unmatched) | 2 | 9a |
| The record lives on one Mac | 3 | 3, 11a, 11b, 12 |
| Memory files and CLAUDE.md held their own routing state, and it contradicted Khipu | 3 | 13 |

Khipu still reports and never routes (routing-truth decision 1, standing rule 7). Every check here is
advisory. No routing warning or advisory mark ever feeds the promotion gate (`khipu promote`) or a dispatch
decision. A red health check means Khipu's delivery failed; it is a health fact, never a gate on work or on
content. An unmatched lane, for example, is red as health and changes nothing about what may run.

## 1. Identity: one record across Macs

Built first (slice 3), because the ledger, replication and the app all key on it.

- **Accounts get stable ids,** `<provider>:<label-slug>` (for example `claude:second`). Each Mac keeps a local
  binding from that id to its login home in config. Login homes never leave the Mac. The same label on two
  Macs is the same account: a login used on both is one quota. Two accounts of one provider must have
  different labels, and the write refuses a duplicate. A rename is an owner event that leaves the old id as a
  forward (the view's `forwards` already exists). A rename that crosses a concurrent rename on another Mac
  resolves by event order, and the loser is shown as a forward.
- **Migration.** The per-install salted ids (`acct-<hmac>`, `account_id`) stay accepted as aliases by
  `resolve_account` on the Mac that issued them, for 90 days. New answers name the stable id. The old
  `<harness>|<home>` keys in the local history are mapped through the binding at fold time, never rewritten.
- **Events and entities get global ids.** Each event carries `id` (a UUIDv7), `mac` (a random id made at
  install, kept in the routing folder) and `seq` (per Mac). Rules, holds and proposals get entity ids
  (`rule-<uuid>`). A rule's display number is given by the fold in first-confirmed order, and it never
  changes once given. Two rules added at once on two Macs therefore both survive.
- **Order is causal.** Each event carries a hybrid logical clock stamp (`hlc`: wall time, counter, mac) and,
  when it changes an entity, the revision it was written against (`base`). The fold orders by `hlc`. Today's
  clock settling (`CLOCK_TOLERANCE`, `clock_at`) becomes the HLC's skew bound. An event that arrives before
  the one it ends (a `hold_end` before its `hold`, possible across Macs) is kept as a tombstone and applies
  when its target arrives. Today's fold drops it (`fold`, the `hold_end` branch).
- **Retries never double.** A write carries a client `op` id. The same `op` with the same body returns the
  first result, and with a different body it is refused.

## 2. The model: three kinds of entry

Every entry is exactly one kind. The kind decides who may write it, where it may travel and how it ends.

| Kind | What it is | Scope | Who writes it | How it ends |
|---|---|---|---|---|
| **Observation** | What a source saw: a probe reading, a runner limit, a reset check, a conversation report, a source's heartbeat | **Account quota** (shared by every Mac: limited, open, the used share) or **machine availability** (this Mac only: CLI not installed, not signed in, runner silent) | Khipu's sources and conversations, author-stamped | It carries `expires_at`, computed when written (a limit's end, or witness time plus `routing_stale_after`), so every Mac expires it the same way by the clock alone |
| **Structured fact** | A typed value: pauses, holds (account, machine or all), the priority order and its entries' roles, providers, account definitions, lane maps, the rule 12 tier table, Clear and Dismiss | Shared, except machine holds and lane maps, which are keyed by Mac | The owner. Conversations write holds and *proposals* | At its `until`, or when the owner ends it. Conversation holds keep the 24-hour default and 7-day cap |
| **Owner rule** | The owner's own words: standing rules, the note, a priority entry's role words | Shared | The owner. Conversations propose (decision 21) | Rules never expire. Home asks for reconfirmation after 90 days |

What follows from the table:

- **Observations become events.** Today usage and runner evidence is recomputed on every fold and never
  stored (`_fold_body` passes it as `extra`). Slice 4 writes an `observation` event whenever a source's
  folded state for an account changes: on a transition, never on each poll. Sources also write a `source_seen`
  heartbeat at most every 5 minutes, which is what makes "silent" decidable on another Mac.
- **Machine availability never makes an account globally down.** "Grok CLI not installed on Mac B" closes Grok
  on Mac B only, and other Macs still see the account's quota state. Machine holds ("load over 75") carry
  their Mac. The push shows them only on that Mac, and the app shows them labelled with the Mac.
- **Definitions are facts.** Providers, account definitions (label, provider, family) and lane maps move from
  per-Mac config into owner facts. Config keeps only the local binding (login home, runner folder).
- **Owner words carry no state.** A level ("low", "N% left") is an observation or a structured `level` claim
  on a priority entry (`{claim, as_of, until}`, ending at its `until` or the account's next reset). The app
  offers to move a level found in the text into the field. Khipu never edits the words.
- **A write that touches a held or paused account must say end or keep.** A priority, rule or role naming such
  an account is refused without a `holds: {<id>: "end" | "keep"}` choice for each one, and the refusal lists
  them. Khipu never reads a release from prose. This is the structural fix for the day-long Grok pause.

## 3. Owner precedence and proposals

The path exists today: `khipu_owner_act`, verified by `owner_direction.check` against Khipu's own record of
Matt's typed turn. The owner-only set (`_OWNER_ONLY`) and the hash-chained log are unchanged.

- **The effective value of an entity** is its newest owner revision, overlaid by at most one live proposal
  whose `base` is that same revision.
- **Proposal to proposal.** A newer proposal on the same base replaces the older one, live at once (decision
  21). The older one ends as *replaced by <id>*, which is terminal, and its author is told. Nothing locks: the
  pending-author refusal in `request_priority` and `request_rule` goes away.
- **A proposal crossing a newer owner revision never takes effect.** A proposal whose `base` is older than the
  current owner revision is written as *crossed*. A delayed retry, a queued MCP call and a sync arriving late
  all end the same way. It is shown to the owner as a stale request and told to its author. It cannot replace
  a newer confirmed owner write. The `expect` check stays inside the write lock, and across Macs `base`
  decides.
- **Concurrent owner writes** (two Macs, same base) resolve by `hlc`. The later one wins, and the earlier is
  recorded as *overtaken*, shown in the app and named in the next push to both Macs.
- **Ending a proposal** (expiry after 7 days, Dismiss, Undo) removes the overlay and leaves the owner revision
  underneath. A replaced or crossed proposal never comes back, so an obsolete direction cannot return.
  Confirming a proposal makes it the new owner revision.
- **Attribution stays.** A proposal keeps its `owner_request` words and its Khipu-stamped author. A dispatched
  job is still refused owner direction (`owner_turns.note_brief`).
- **Every refusal names a way forward:** the author and age of what blocked it, the current revision, and the
  exact `khipu_owner_act` action or app place that works (slice 1, D, extended to every write path).

## 4. Delivery guarantees

A conversation is **live** when it has had a prompt in the last 12 hours (a setting).

1. **Nothing is silently dropped.** Each delivery identity (section 5) has a ledger of change ids: *owed*, then
   *in flight* (sent in batch `b`, with a 60-second lease), then *acked*. A hook acks with its existing `op:
   ack` or `finish_delivery`. An MCP client acks per the protocol in section 5. A lease that runs out returns
   the change to owed. A change is delivered only in a form that names its kind, its accounts and what it does
   (slice 1, A). Standing rules and the note are delivered whole, split into pages when they do not fit. A
   page carries `{entity, revision, page n/m}`. If the entity changes mid-pagination, the old revision's
   remaining pages are cancelled and the new revision starts at page 1 with "rule 11 changed while being
   shown". A session start that exceeds the 10,000-character hook limit owes its pages to the next deliveries
   instead of sending only the pointer.
2. **Superseded changes are not delivered.** Only an entity's current state is sent.
3. **Delivery is visible.** `khipu_route` returns the caller's `delivery: {owed, in_flight, acked_through}`.
   The app lists each live identity with its owed count. A pushed block ends with a short tail only when
   something was owed or paged.
4. **When.** This is bounded by the harness matrix below. The honest general bound: **before the next model
   turn that follows a delivery point** (start, prompt, tool result, Khipu tool call), with the local fold
   current within `routing_poll_interval` (10 s). Across Macs, add 60 s while both are online (section 7).
   Nothing interrupts a running turn.

### Harness matrix

The replays inspect the context the model received (the transcript's hook attachments, Codex's developer
messages, the MCP result body), never the hook's stdout.

| Client | Start / compaction | Each prompt | During a turn | Before a dispatch goes out | Achievable boundary |
|---|---|---|---|---|---|
| Claude Code parent | full summary, paged | owed changes | PreToolUse context arrives **with the tool's result, after the call ran** (`routing_delegation.py:20-23`) | from the last prompt's push only | the next model step after any prompt or tool result |
| Claude Code subagent | SubagentStart gives the full summary before its first step (phase 0 fact 3) | none (no prompts) | its PreToolUse is skipped (`:461-462`) | fresh at its start | its start. Slice 8 adds a per-agent ledger so a long subagent hears changes on its own tool results |
| Codex parent (CLI, `codex exec`) | SessionStart, including compaction and resume (phase 0 fact 4) | owed changes | PreToolUse with the result | from the last prompt | as for the Claude Code parent |
| Codex native child (`spawn_agent`) | **none: its rollout carries no hook context** | none | none | none from Khipu | only through its brief. Client integration: Khipu's `spawn_agent` PreToolUse writes nothing into the child, so the parent's brief must carry `khipu route --brief` (filed in the Codex instructions; verified in slice 8) |
| T3 `delegate_task` / provider switch | the child provider's own SessionStart, if Khipu's hooks are installed for it | its own | its own | the parent's PreToolUse context arrives after `delegate_task` returned | the child's start. Client integration: Khipu's T3 integration sets `KHIPU_SESSION_ID=t3:<thread>` for every provider, so a provider switch keeps the thread's ledger instead of starting cold (slice 8) |
| Job runners' `claude -p` / `codex exec` | their own SessionStart | one prompt | as for the parents | the runner's brief | its start. The runner adds `khipu route --brief` to each brief (runner task, routing-truth) |
| Grok Build | hook output discarded | discarded | only Khipu tool results | none | its next Khipu tool call ([[grok-build-shares-the-aegis-home-and-has-no-push-recall]]) |
| Aegis | native pack, no hooks from Khipu | its client pulls `khipu_status` / `khipu_route` | Khipu tool results | none | its next Khipu tool call |
| Cloud clients through the gateway | MCP instructions line | none | Khipu tool results | none | its next Khipu tool call, as of the hub copy |

Pre-dispatch freshness for the parent stays a per-prompt guarantee. A change landing mid-turn reaches the
parent only after its dispatch has gone out. Holding the dispatch back would make Khipu a gate (rule 7). The
child of that dispatch gets fresh state at its start where the harness allows it (Claude Code SubagentStart, a
child's own hooks); where it does not, it relies on the brief integration named in the matrix.

## 5. Pull delivery: identity and acks

`khipu_route` takes only `since` and `account` today. Authors come from the environment
(`conversation_author`), and the gateway is stateless (`gateway.py:14`). Slice 7 adds the following.

- **Delivery identity** = `(transport principal, session)`. Locally the principal is the harness the
  environment names, and the session is the first of `KHIPU_SESSION_ID`, `CLAUDE_CODE_SESSION_ID`,
  `CODEX_THREAD_ID`. On the gateway the principal is the token's label (`KHIPU_GATEWAY_TOKENS`), and the
  session is a client-supplied `session_id` argument, bound to that label: the same `session_id` under two
  labels is two identities.
- **With no session, nothing is consumed.** A caller without a session gets the current state, the full
  summary and any open pages, and its result says `delivery: "untracked: pass session_id"`. Nothing is marked
  delivered for anyone. Two callers sharing a token therefore never consume each other's owed changes.
- **Batch and ack.** Each result that carries routing has `routing_changes: {batch, items[{change, entity,
  revision, page?}]}`. The client acks by passing `ack: <batch>` on any later Khipu call. Calling again
  without an ack re-sends the same in-flight items (a dropped response is re-sent). A duplicate ack is a
  no-op. Acking an unknown or expired batch is answered, never an error.
- **Concurrent hook and MCP delivery** to one local identity share its ledger under one lock. Each reserves
  what it sends as in flight, so the same change is never sent twice at once. A change in flight from the hook
  is never re-sent through MCP until its lease runs out.
- **Scrubbing stays.** Routing additions on memory-tool results go through `agent_view`'s routing scrub, and so
  do their error responses (`_AGENT_SCRUBBED`). The memory content itself is never scrubbed, as today.

## 6. Privacy boundary

Four layers, each a projection of the one before. Slices 11a and 12 test each field of each layer.

| Layer | Holds | Never holds |
|---|---|---|
| **Local history** (`events.jsonl`) | everything, as today | unchanged; nothing in it is rewritten |
| **Replication payload** (Mac to hub) | ids, kinds, structured fields, stable account ids, `hlc`, `base`, author as `{kind, harness, mac}`, the owner's confirmed rule and note words, owner hold words | login homes and any nested `<harness>\|<home>` key (mapped to stable ids); conversation free text (`owner_request`, a conversation hold's words, evidence lines, self-given names); project and session names (replaced by keyed hashes); `prior_text` of an unconfirmed rule |
| **Hub storage** (migration 0041, its own tables) | the payload, append-only, keyed by event id; a per-Mac source status row | anything the payload doesn't hold |
| **Gateway projection** | labels, states with times and basis, holds (structured; owner hold words under opt-in), rules and the note under opt-in, the caller's own delivery state | paths of any kind (cut to "a path" even inside owner words; local owner words untouched), other identities' delivery state, Mac ids |

- **The old cloud opt-ins are amended:** "copy for cloud agents" now names three switches: account state (on
  when the copy is on), owner hold words, and rules and note. The rules-and-note switch now also covers other
  Macs (decision 6).
- **Historical free text with paths** stays as written locally. The payload carries owner words as written
  between Matt's own Macs. Only the gateway projection cuts paths, and only in the projection
  (`agent_view.keep_owner_words` keeps its local exception and gains a cloud-side counterpart).
- **The key floor (slice 9b, the `http-quota` adapter):** HTTPS only, to the exact host the owner confirmed on
  the provider fact. Redirects are refused, not followed. Bounded time and response size. The key is read
  through `keychain` into a variable and sent only in the request header to that host, never in argv, env,
  a URL or a file. Errors carry the status code and a closed error code, never the response body or headers.
  The proof is a test with a sentinel key asserting its absence from stdout, stderr, the hook log, view.json,
  events, the replication payload, exception text and every tool result. A log grep is not proof.

## 7. Replication

Slice 11b follows `hub_snapshot`'s contract (`pull_changes`): a per-Mac cursor on the hub's own clock, read
with an overlap of `FRESHNESS_OVERLAP_S`, idempotent inserts keyed by event id, and a caught-up stamp only
when a pull read up to the hub's now minus the overlap.

- **Upload** sends unshipped local events in `seq` order with an upload cursor. A duplicate upload is ignored
  by event id, and an interrupted upload resumes from the cursor.
- **Download** pulls other Macs' events past the cursor into a local `remote.jsonl`. The fold reads local and
  remote together. Each Mac's source status rows say how fresh its sources are.
- **Restore or reinstall** gives a new `mac` id. The Mac rebuilds its remote set from the hub, and its old id's
  events stay valid history. A restored local log whose events are already on the hub is deduplicated by id.
- **Offline.** A Mac that cannot reach the hub keeps working on its own log, and every push says "routing from
  other Macs is N min old". Delivery health (section 9) turns red after 10 minutes.
- **The bound** is 60 s from a write on one Mac to the other Mac's fold while both are online (the upload
  debounce plus the poll). Every observation's `expires_at` makes clock-only expiry the same on every Mac.

## 8. Lanes, accounts and providers

- **Providers become owner facts** (slice 9a): `{id, label, family, login: folder|none, probe: <adapter>,
  host?, lane_names: [...]}`. `family` (Anthropic, OpenAI, xAI, Meta, Zhipu, DeepSeek) is what rule 2's
  "different model family" is checked against. Adapters come from a closed set in code: the four CLI probes,
  `http-quota` (slice 9b, section 6) and `none` (tracked from runner lines and reports only). Adding DeepSeek
  is a provider fact written from the app or by owner direction, with no code change, as long as an adapter
  fits.
- **Lanes match by name** (decision 3). A lane named after a provider id or one of its `lane_names` matches
  that provider's account when the provider has exactly one, marked "matched by name". **Ambiguity:** a
  provider with two or more accounts, or a name claimed by two providers, never matches. The lane stays
  unmatched and is listed with its candidates, so one owner direction can map it.
- The live deploy gap (decision 91270: live-b's `usage.py` predates GLM) is a promotion, done when Matt says.

## 9. Advisory checks and health

**Advisory checks** (rule 7) put a marker on a line and a doctor warning. They never feed a gate.

- **Rule 12 tiers** (slice 10). Rule 12 gets a structured tier table (task class to tier) that Matt confirms
  once. The task classes are `scope`, `mock`, `ruling`, `hard-security`, `failed-fix`, `build`, `fix-round`,
  `tests`, `docs`, `synthesis`, `mechanical`. At delegation, `routing_delegation.py` reads the model (Agent
  `model`, `--model`, `-m`, `CLAUDE_CONFIG_DIR`) and the class: a `task:` line in the brief first, otherwise a
  keyword guess marked as a guess. On a mismatch, one line arrives with the tool result (matrix above): "Rule
  12: fix rounds go to Sonnet; this dispatch is Opus. Say why if intended." Brief text never leaves the Mac or
  goes to a model.
- **Contradictions:** a priority or rule naming a held or paused account; two structured directions opposite
  for one account; a priority entry for an account that is down or untracked.
- **Stale level claims:** slice 1 (E) for owner words, and the `level` field's own expiry.

**Delivery health** is a red `routing_delivery` check in `khipu doctor`, the app's health row and attention
items. It is measured as **stalled delivery against a budget-aware bound**: for each live identity, the
pushes a change needs is the owed characters ahead of it, plus its own, divided by the per-push budget,
rounded up. It is red when a change is still owed after that many delivery points plus 2, or when it
expires from in flight twice. It is also red when:

- an ack was recorded for a change never shown (the failure-3 invariant);
- more than 1 in 100 prompts today skipped routing (the existing rate check);
- the cross-Mac copy is more than 10 minutes behind, or the gateway copy is past its stale bound;
- an unmatched lane's limit stays unmatched for an hour. This is a health fact only: it changes nothing about
  what may run.

The evidence is the ledgers, the hook outcome log, the replication cursors and the gateway's delivery rows,
never a health probe ([[audit-lessons-2026-08-17]]).

## 10. One source of truth

Routing state lives only in Khipu's record. Files point at it.

- **May stay in files:** durable capability notes (the global CLAUDE.md model table) and how each lane is
  reached. **May not:** whether an account is open, limited, paused, held or "tight", priorities and levels.
- **The scanner** (slice 13) reads global and per-project CLAUDE.md and AGENTS.md files and the memory notes
  Khipu imports. It flags a routing-state claim (an account next to a state word plus a date or level) with
  its file and line. Recall marks such a note "routing state: superseded by Khipu's record" in the prior-work
  block. **False positives it must pass clean:** capability rows ("Weak at", "Good for"), evidence dates in
  those tables, quoted history ("on 2026-10-14 Matt paused…" inside a past-tense record or a quote block), and
  lane how-to text. Live examples it must flag: "Main account tight until Mon 2026-10-19" and "Delegation
  order: second Claude account first".
- **The one-time cleanup** of Matt's own files is a separate step with his go (decision 7), with the
  hand-written lines kept verbatim and a backup first ([[index-rewrites-need-verbatim-lines-and-backups]]).

## 11. The app

Each group needs an approved Opus mock first (slice 14). If a mock is missing, stop and ask. The mocks extend
`docs/mockups/2026-10-14-routing/` and use Better Design. Every owner action stays doable from chat through
`khipu_owner_act` ([[owner-direction-through-the-agent]]).

- **Delivery and health:** live identities (harness, Mac, last prompt, owed), recent changes and who has them,
  the red check in plain words, other Macs' freshness.
- **Proposals and holds:** open, crossed and overtaken proposals side by side; the end-or-keep choice.
- **Providers and lanes:** providers, accounts and their Macs, lane names, "matched by name", ambiguous lanes
  and their candidates.
- **Advisory and copies:** the day's tier mismatches, contradictions, stale claims, and copies found in files.

## 12. Acceptance: replay the real failures end to end

`tests/test_routing_acceptance.py` (slice 2). **Fixtures are sanitized:** a script turns the private log into
a fixture with the same shapes, ids, times and lengths, with synthetic words and no paths. The private log is
never tracked. The replays drive the real hooks and MCP tools and assert on the context the model received
(section 4). Each case starts `xfail` and is flipped by the slice named.

| # | Case | Slice |
|---|---|---|
| 1 | The Grok hold and the 10-19 priority: the contradiction is shown everywhere; an owner write is refused until it says end or keep; "end" removes the hold from every next delivery | 4 |
| 2 | A 3,000-character pending rule, a 10,500-character session start, and a rule edited mid-pagination: whole across pages, restarted on the edit, never pointer-only | 6 |
| 3 | Eight changes against a 400-character budget: all shown within the budget-aware bound; no ack without a show | 6 |
| 4 | Proposals: two conversations take turns, the first is told it was replaced, neither is refused; a delayed retry after an owner write is crossed; expiry and Undo leave the owner revision and never revive a replaced proposal; a dispatched job's owner act is refused | 5 |
| 5 | "Codex is low" against a 96% reading is marked; a structured claim expires at the reset | 4 |
| 6 | Pull protocol: a shared token with no session consumes nothing; a dropped response is re-sent; a duplicate ack is a no-op; hook and MCP deliveries at once never double-send; routing additions on error results are scrubbed | 7 |
| 7 | Harness matrix: each row's boundary holds in received context, including a Codex child's brief and a T3 provider switch keeping its ledger | 8 |
| 8 | Providers: `glm` and `deepseek` added as facts match by name; a two-account provider and a name claimed twice stay unmatched with candidates | 9a |
| 9 | The `http-quota` adapter: a redirect is refused; the sentinel key is absent from every channel | 9b |
| 10 | An Opus dispatch with `task: fix-round` gets the rule 12 line with its result; a Sonnet one gets nothing; the hook never denies | 10 |
| 11 | Identity and migration: an old salted id still resolves; a rename forwards; two Macs adding a rule at once keep both; an old local log folds unchanged | 3 |
| 12 | Two Macs (two HOMEs, test hub): events in opposite arrival order, a skewed clock, a duplicate upload, an interrupted sync, a restore with a new Mac id; probe limits, runner limits, reopening, source silence and clock-only expiry fold the same on both; "not installed on Mac B" leaves the account open on Mac A | 11a, 11b |
| 13 | Gateway: the projection holds only its listed fields (every nested and history field checked); another token's delivery state is invisible; owner writes are refused | 12 |
| 14 | Scanner: flags the two live notes; passes capability rows, evidence dates and quoted history | 13 |
| 15 | Health: each red condition turns red and back to green on its own evidence | 16 |

## Slices

Each slice fits one session and leads with "Read this scope first; inspect {named code} before writing". One
PR each. Every slice reaches this Mac only through `khipu promote` and its live gate. The builder and the
reviewer always come from different families.

**Oracle for every slice:** hermetic pytest from `packages/cli` (temp HOME, `KHIPU_KEYCHAIN=0`; the baseline is
the named failures), `ruff check --no-cache packages/cli`, and the slice's acceptance cases flipped to pass with
no other case regressed. At most 3 oracle runs per change; if it is still red, stop and report the last log.
Slices on the per-prompt path also run `python -m khipu.routing_replay` (no new timeouts).

| # | Slice | Depends on | Builds | Reviews | Cases, stop-conditions |
|---|---|---|---|---|---|
| 1 | Today's A to F | none | Codex (in flight) | Sonnet | its own acceptance |
| 2 | Acceptance harness, sanitizer, all cases `xfail` | 1 | Codex Sol, high | Sonnet | every case fails for its stated reason, not on a harness error; no private text in the fixture |
| 3 | Identity contract: stable account ids, bindings, renames and duplicates, alias migration, global event and entity ids, HLC, `base`, `op` dedup (local only) | 2 | Codex Sol, high | Opus | case 11; the existing log folds to the same view |
| 4 | Entry model: kinds, observations as events with scope and `expires_at`, definitions as facts, end-or-keep, `level` claims | 3 | Codex Sol, high | Opus | cases 1, 5; owner words byte-identical |
| 5 | Owner precedence and proposals | 3, 4 | Codex Sol, high | Opus | case 4; `owner_direction.check` unchanged |
| 6 | Delivery ledger and pagination for hooks: owed, in flight, acked, leases, pages with revisions | 3, 4 | Codex Sol, high | Opus | cases 2, 3; `routing_replay` holds; today's budget caps |
| 7 | Pull delivery: identities, batches, acks, `routing_changes` on tool results, scrubbing incl. errors | 6 | Codex Sol, high | Opus | case 6 |
| 8 | Harness matrix integrations: per-agent ledger, Codex child briefs, T3 `KHIPU_SESSION_ID` | 6, 7 | Codex Sol, medium | Sonnet | case 7; stop and report any matrix row the received context disproves |
| 9a | Provider catalog as facts, match by name, ambiguity | 4 | Codex Sol, medium | Sonnet | case 8 |
| 9b | `http-quota` adapter | 9a | Codex Sol, high | Opus (credential path) | case 9 |
| 10 | Advisory: rule 12 tier table and check, structured contradictions | 4, 9a | Grok, against tests Codex writes first (10t, Sol medium) | Codex re-check (rule 2), then Sonnet | case 10 |
| 11a | Hub schema, migration 0041, replication payload projection | 3, 4 | Codex Sol, high | Opus (what leaves the Mac) | case 12's payload-field checks; inspect `test_audit1010_hub*` for the test hub first |
| 11b | Replication and recovery: cursors, idempotent upload, caught-up, offline, restore | 11a, 6 | Codex Astra, high (a miss here is costly) | Opus | case 12 |
| 12 | Gateway projection and pull delivery through the gateway | 7, 11b | Codex Sol, medium | Opus (gateway exposure) | case 13; the deploy is its own step with Matt's go ([[merged-is-not-deployed]]), then a live `khipu_route` through the gateway |
| 13 | Scanner and recall's superseded mark | 2 | Grok, against tests Codex writes first (13t) | Codex re-check, then Sonnet | case 14; Matt's files are only read |
| 14 | Mocks for the four app groups | 4, 5, 6 | Claude Opus | Matt approves | no app work before approval |
| 15a | App: delivery and health | 14, 6, 16 | Codex Sol, high | Sonnet | `npm run build && tsc`, desktop tests, then the rendered result (at most 3 visual passes, screenshot as proof) |
| 15b | App: proposals and holds | 14, 5 | Codex Sol, high | Opus (owner actions) | as 15a |
| 15c | App: providers and lanes | 14, 9a | Codex Sol, medium | Sonnet | as 15a |
| 15d | App: advisory and copies | 14, 10, 13 | Codex Sol, medium | Sonnet | as 15a |
| 16 | Delivery health: the budget-aware bound and red conditions, with evidence from hooks, MCP, replication and the gateway | 6, 7, 11b, 12 | Codex Sol, medium | Sonnet | case 15 |

Slices 9a, 10 and 13 can run beside 5 to 8, and 11a beside 6.

## Matt's decisions (each with a recommendation)

1. **Delivery failures turn doctor red** (amends routing-truth decision 10 for delivery only). Routing content
   and advisory marks stay warnings and never feed a gate. *Recommend yes.*
2. **Conversation requests become proposals.** They are live at once on the current owner revision, a newer
   proposal on the same revision replaces an older one, a proposal written against an older revision never
   takes effect, and nothing locks. *Recommend yes.* (Revised: "newest wins" now applies only between
   proposals on the same base.)
3. **Lanes match by exact name when the provider has exactly one account,** marked and overridable; ambiguous
   names stay unmatched with candidates. *Recommend yes.*
4. **Owner writes that touch a held or paused account must say end or keep.** *Recommend yes.*
5. **Rule 12 gets a structured tier table you confirm once;** the class comes from a `task:` line, else a
   marked guess; brief text never goes to a model. *Recommend yes.*
6. **What crosses Macs and the cloud** (revised). *Recommend:* between your Macs, the replication payload in
   section 6 (structured facts, observations, your confirmed words, no login homes and no conversation
   words). To the cloud, the gateway projection, with paths cut, under three switches: account state on;
   owner hold words on; rules and note on, since Grok Bot needs them to follow them.
7. **A one-time cleanup of routing state in your CLAUDE.md files and memory notes,** by a Sonnet job with a
   backup, after slice 13 shows you what it found. *Recommend yes.*
8. **Bounds** (revised): 12 hours for "live"; red at the budget-aware bound plus 2 delivery points; a 60-second
   cross-Mac target and a 10-minute red; a 60-second in-flight lease; 90 days for old account-id aliases. Each
   is a setting. *Recommend these defaults.*
9. **Each Mac binds accounts by label** (new): the same label on two Macs is one account and one quota.
   *Recommend yes;* it matches how your logins work. The alternative is a manual pairing step per Mac.

## Review responses (Codex round 1)

All seven points are accepted and answered in place: 1 in section 4's matrix, 2 in section 5, 3 in sections 2
and 7, 4 in sections 1 and 7, 5 in section 6, 6 in section 3, 7 in the slice table. The notes are folded into
the heart of it, section 9, section 10 and section 12. Two places go slightly differently from the request,
with reasons:

- **Point 1, pre-dispatch freshness for the parent.** No hook can deliver it, and holding a dispatch until
  routing is read would make Khipu a gate (rule 7). The scope keeps the parent's guarantee at "the last
  prompt's push" and puts fresh state into the child (SubagentStart, the brief integration) rather than adding
  a pre-dispatch client step.
- **Point 5, owner words containing paths.** Between Matt's own Macs they replicate as written, because
  cutting them would change his words to his own agents. Only the gateway projection cuts paths. This is
  decision 6's to confirm.

## Known limits

- A running turn learns nothing until its next delivery point. No harness offers an interrupt.
- A Codex native child hears routing only through its brief.
- A keyword-guessed task class can be wrong. It is always shown as a guess.
- "Owner only" stays a convention (routing-truth decision 6). Owner direction is as strong as
  `owner_direction.check`.
