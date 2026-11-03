# Conversation overrides and the routing walk-through

Status: proposed 2026-11-03, not approved. Design only. Builds on `2026-10-26-routing-exceptions-expire.md`.

## What it does

> **You:** Let this conversation run ahead of pace on the second Claude account for 3 hours.
> **Agent:** It may run ahead of pace on Second Claude until 19:40Z, stopping when the account reaches 60% of its week. Others stay on normal pace. Yes?
> **You:** Yes.

Its advice now reads `go (override until 19:40Z)`. Others get their usual answer and hear nothing. At 19:40Z it ends on its own.

> **You:** Let Healthy You run ahead of pace on Grok.
> **Agent:** For how long? For no end, say "indefinitely", and I'll raise it with you every 7 days.

> **You:** Walk me through my routing.
> **Agent:** Second Claude: about 1% an hour until Sat 19:00Z, one override. Duplicate: Codex "re-check of PRs #3 and #11", four times. Conflict: the Grok override is on an account that's down. Waiting for your yes: 2. Keep one Codex entry and end the Grok override?
> **You:** Yes.

## Why exceptions can't do this

An exception's `share_pct` applies at the fold to the whole account, highest wins, so it raises the share for everyone. It never touches a `pace_target` cap, so "ahead of pace" words change no verdict. Nothing compares words on write, so a retry with a new op adds another, up to 16.

## Data shape

An override is its own log record, not part of the priority: `subject` (T3 `thread`, `session`, or `project`, plus the Mac), `account`, `effect` (`ahead_of_pace`, optional `ceiling_pct` of a window), `until` or `indefinite` plus `review_at` (one required), `children`, optional `reason` and `ref` (a PR or owed id), and `by`, `at`, `request`.

- **Advice.** The shared view stays conversation-free. `advise` matches at read time: it drops pace-target verdicts and pace-based crossings, then checks the ceiling. It never lifts holds, pause, limits, down, spend, concurrency or eligibility. Usage isn't split by conversation, so the ceiling measures the whole account.
- **Visibility.** The named conversation gets one fixed-words line in its routing block (no writer-chosen text in pushes, decision 17). `khipu_route` shows a conversation only its own. The walk-through and app show all. None go in the cloud copy.
- **Ending.** Readers ignore a passed `until`, with no write. Indefinite ones never lapse; at `review_at` they join the "needs you" read until kept or ended.
- **Children.** A T3 delegated child has its own thread. With `children` on it matches through T3's parent link while the parent's override is live, and can't renew (this needs a new parent-id read). Native subagents carry the parent's session id and match by it; `is_dispatched` excludes them when `children` is off, where its marker reaches the server.
- **Existing exceptions: leave them.** Each has an end and lapses; prose isn't parsed. The walk-through offers to convert one that names a conversation. Exceptions remain for directions to every conversation.

## Trust model

- **Only the owner grants**, through `khipu_owner_act` with the usual routing proof. Any conversation can confirm a held request, so the subject is frozen when asked and named in the ask.
- **Identity comes from the server's environment and T3's record, never tool arguments.** `conversation_author` gives harness, project and session, no T3 thread; the thread comes from `session_thread_ids_result`, which must return exactly one. Anything unknown gets normal pace, and the reason says so.
- **Spoofing is accepted, not prevented.** A process setting another's `KHIPU_SESSION_ID` gets that override's advice, undetected. Advice is advisory, anyone can already ignore `hold_back`, and the harm is burn bounded by the end and ceiling. If advice ever becomes a gate, redo this.
- **Project subjects are weakest:** a folder's base name. The ask says "every conversation in folder X".

## The walk-through

A read with no model and no writes: in force, duplicates, conflicts, ending within 6 hours. One ask names every proposed change; one yes applies them. Checks cover live items:

| Code | Reported when |
|---|---|
| D1 Duplicate | Same account, same words after folding case, spacing, punctuation |
| D2 Near duplicate | Same account, word overlap ≥ 0.8 (tune on the real log), identical numbers and PR refs |
| D3 Twin | Same words on different accounts: asked about, not an error |
| C1 Stacked | Two overrides, same subject and account, overlapping time |
| C2 Share clash | Two exceptions on an account with different `share_pct` |
| C3 Closed account | Override or exception on an account paused, held, down, or limited past its end |
| C4 Over a cap | `ceiling_pct` above the stored share cap |
| C5 Lapsed reason | `ref` PR merged or closed, or owed item closed; unreadable says "couldn't check" |
| C6 Gone subject | T3 no longer shows the thread |
| W1 Worth a look | Rule, role or exception names the account with fixed opposing words (never, stop against ahead, over), or a role holds a past date |

Rules are free text, so W1 says "look at these together", never "conflict".

**Stopping duplicates.** Under the routing lock, on both exception write paths (`_amended_priority`, `_priority_entries`) and against waiting requests: D1 writes nothing and returns the existing id (a later end needs a yes); D2 is refused, naming the match, unless the owner confirms it differs. A second override on one subject and account replaces the first on a yes.

**"Needs you" read** (`feat/needs-you-in-conversation`, no commits past main yet, so its shape is assumed). It owns everything waiting on a yes, including held overrides and indefinite reviews. The walk-through shows its items, never reads pending requests itself, and owns only what's in force.

**T3 load balancing.** Khipu's code never reads these settings, so nothing connects them to Rule 24. Both aim at placing work across the Macs; whether they overlap in effect can't be told without T3's source, and Rule 24's text isn't readable here. The settings hold one weight, keyed to this Mac's own environment id, reading 50 rather than 100 (file changed 21:11Z today). This doc would need to know what T3 balances, whether weights are relative, what a missing environment means, whether T3 reads load, and whether an agent can see the placement. Overrides depend on neither.

## Edge cases

- After an override ends the account may be ahead, so everyone, former holder included, gets `hold_back` until pace recovers. The ask says so.
- A thread moved to the other Mac, or a non-T3 resume with a new session, stops matching and falls back to normal pace.
- Gateway callers have no `advise`, so no overrides.

## Slices

1. **Duplicate guard** on exceptions and waiting requests. Verify with a four-retry test.
2. **Override record:** log, fold, expiry, owner action. No effect on advice.
3. **Advice:** matching, children. Others' answers must stay byte-identical. Stop if a T3 lookup breaks the advice budget.
4. **Visibility:** the line and filtered reads. Stop and ask for a mock before any app UI.
5. **Walk-through read.** If "needs you" hasn't landed, show a pointer.
6. **Settling:** one yes for many changes; exception conversion.

## Open decisions

1. **Do delegated children share an override by default?** Recommended: yes, while the parent's is live, since an orchestrator's work mostly runs in children.
2. **With no ceiling given, where does an override stop?** Recommended: the stored share cap if there is one, otherwise the plan limit; the ask states which.
3. **Second-Mac conversations read a separate record with no priority or rules (`routing_replication_live` is unset). Should overrides wait for shared records?** Recommended: no. Ship on the main Mac, refuse plainly on the second, and decide replication separately.

## Owner's decisions (2026-11-03)

1. Helpers a conversation starts share its override while the override lasts.
2. With no limit given, the override stops at whatever the settings already say: the stored share cap if there is one, otherwise the plan limit. The ask states which.
3. Both Macs behave the same. Overrides rely on routing copy being on, and routing copy gets turned on and proven first. While a Mac has no copy of the routing record, it refuses plainly instead of guessing.
