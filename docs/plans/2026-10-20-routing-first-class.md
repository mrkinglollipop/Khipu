# Routing as a first-class feature (2026-10-20)

Asked by Matt 2026-10-20: "A routing system that can change dynamically needs to be a first class feature. We
depend on this." Status: APPROVED by Matt 2026-10-20 ("Yes proceed as you recommend"): all 14 decisions
as recommended, after Codex review rounds 1 to 8. Slice 1 shipped as PR #260. No UI is built until its
mocks are approved.

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
| The record lives on one Mac | 3 | 3a, 3b, 11a, 11b, 12 |
| Memory files and CLAUDE.md held their own routing state, and it contradicted Khipu | 3 | 13 |

Khipu still reports and never routes (routing-truth decision 1, standing rule 7). Every check here is
advisory. Khipu itself never enforces a routing warning or advisory mark: none feeds the promotion gate
(`khipu promote`), and none stops a dispatch. A runner or conversation may choose to act on `advise`
(section 13); that is its own launch policy. A red health check means Khipu's delivery failed; it is a health fact, never a gate on work or on
content. An unmatched lane, for example, is red as health and changes nothing about what may run.

## 1. Identity: one record across Macs

Built first (slices 3a and 3b), because the ledger, replication and the app all key on it.

- **Accounts get stable ids,** `<provider>:<label-slug>` (for example `claude:second`). Each Mac keeps a local
  binding from that id to its login home in config. Login homes never leave the Mac. The same label on two
  Macs is the same account: a login used on both is one quota. Two accounts of one provider must have
  different labels, and the write refuses a duplicate. A rename is an owner event that leaves the old id as a
  forward (the view's `forwards` already exists). A rename that crosses a concurrent rename on another Mac
  resolves by event order, and the loser is shown as a forward.
- **Migration (slice 3a).** The per-install salted ids (`acct-<hmac>`, `account_id`) stay accepted as aliases
  by `resolve_account` on the Mac that issued them, for 90 days. New answers name the stable id.
  - The 90 days start at the `account_migration` event slice 3a writes into the local log. A restore of that
    log keeps the same start. A reinstall with no log has no aliases, because it never issued any.
  - After expiry, an old id fails explicitly: "this account id expired on <date>; use <stable id>". It is
    never silently rebound.
  - The fold's mapping of historical `<harness>|<home>` keys through the binding is permanent. The local
    history is never rewritten.
- **Events and entities get global ids (slice 3b).** Each event carries `id` (a UUIDv7), `mac` (a random id
  made at install, kept in the routing folder) and `seq` (per Mac). Rules, holds and proposals get entity ids
  (`rule-<uuid>`).
- **Every write addresses entities by uuid.** A numeric target (`rule: 11`, an edit by number) is resolved to
  its uuid at the writer's read, and the event stores the uuid. The fold keys rules by uuid, replacing the
  integer key in `fold`'s `rule_add` branch and `_next_rules`' local allocation.
- **Rule numbers are provisional until the hub has them.** A rule's display number is assigned by the hub's
  acceptance order: the hub's insert sequence of each rule's first `rule_add`. Once given, it never changes.
  - A Mac that is not caught up shows a new rule as "new rule (number pending)". A numeric target that names a
    provisional number is refused, and the refusal gives the uuid. Two disconnected Macs therefore never give
    out the same number.
  - With no hub configured, the local `seq` order is the hub order, as today.
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

- **Observations become events, and they are renewed.** Today usage and runner evidence is recomputed on every
  fold and never stored (`_fold_body` passes it as `extra`).
  - Slice 4 writes an `observation` event (with its own id) on each state transition for an
    `(account, window or scope)`.
  - While the state holds, it writes an `observation_renew` event naming that observation id and carrying the
    new witness time. It does this at most every `routing_stale_after / 4` per `(account, window)`.
  - The witness time is the source's own reading time for that account and window: the usage row's
    `observed_at` or `probed_at` (`routing_sources.py:925`), or a runner line's time. It is never the fold's
    clock.
  - A renewal is written only when the witness time is strictly newer than the observation's last witness. So
    re-reading old cached evidence (an unchanged usage row, a `last_good_at` row kept after a failed probe, a
    runner tail read again) can never refresh it.
  - The fold's staleness test (`_fold_account`, `routing.py:1986`) and `expires_at` use the newest witness of
    the observation.
  - `source_seen` heartbeats (at most every 5 minutes) only say that a source is alive. They never renew an
    account's reading.
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

## 2a. Pace: are we outpacing a limit?

Matt, 2026-10-20: "a way for the conversation to see a pace for how much the usage limit is getting used.
Knowing if we are outpacing our limit for something or underpacing is very helpful."

Today's pace (phase 4b, `routing.py` `plan_fact`, `pace_words`, ~4788-4915) gives only the even rate that
would use what is left by the reset. It never measures the real burn rate. This section adds that as an
advisory: it never gates anything (rule 7), and Khipu still returns no pick. Every forecast is labelled "at
the trailing average".

### Samples

- **What a sample is.** A `usage_sample` observation (slice 4Pa) holds `{account, window, cycle, witness_at,
  used_pct, mac, source}`. The rules of section 2 apply: a sample needs a witness strictly newer than the last
  one from the same Mac and source. A cached row or a `last_good_at` row never makes a sample.
- **Sampling per window** (decision 12).
  - The daemon reads plan figures, and writes a sample from each fresh reading, every **10 minutes** for a
    five-hour window and every **30 minutes** for a weekly window. It does so only while the window's usage
    moved in the last hour, or at least once an hour otherwise.
  - A move of 0.5 points or more is sampled at once, but not more often than every 2 minutes.
  - This meets the floor below: 3 readings within 30 minutes for a five-hour window, and within 12 hours for
    a week.
  - An account whose probe is costly (Grok's starts a whole agent) samples at the weekly interval for every
    window. Its five-hour verdict is then usually "unknown", and the line says so.
- **Cycle identity is a canonical partition of reset times, independent of the witness buckets.**
  - **With a provider cycle id** (a probe that reports one; none does today), the cycle is that id, but only
    if the id is unique to one reset cycle. A reusable window id such as "weekly" or "five_hour" names the
    window, not the cycle, and never merges successive resets. Such ids are ignored for cycle identity, and
    the estimates are grouped as below.
  - **When ids are mixed,** some readings carrying a unique cycle id and some carrying none, the id-bearing
    readings define their cycles. An id-less reading joins a cycle only if its reset estimate falls within
    that cycle's single-linkage component. Otherwise it is grouped with the other id-less readings, as
    below.
  - **Otherwise cycles are connected components of the reset estimates under single linkage.**
    - Sort one account window's reset estimates. Consecutive estimates no more than **G = 1 hour** apart
      join, and a cycle is a resulting connected component.
    - That depends only on the set of estimates, so it is order-independent and transitive by construction.
      A late estimate only joins or links components, exactly as if it had arrived on time.
  - **The span bound.** A component whose span (latest estimate minus earliest) exceeds **S = 1 hour** is
    **ambiguous**. Pace for that window is `unknown`, and the line says "reset time spread over more than an
    hour". It stays so until the stale end ages out of the trailing window, or the estimates settle into a
    narrower component.
  - **Why it never merges two real resets.** Genuine resets of five-hour and weekly windows are at least 5
    hours apart, which is more than G.
    - Two real resets therefore join only if estimates chain across the whole 5-hour gap.
    - Such a component spans more than S, so it reads `unknown` and is never one silent cycle.
    - The supported jitter is up to 1 hour of spread in one cycle's estimates.
  - Codex's examples, each in any arrival order:
    - 14:59 and 30:01 form one cycle, with no middle reading needed.
    - 14:59, 15:00, 30:00, 45:00 and 60:01 form one cycle spanning 45 min 02 s, which is not ambiguous.
    - R, R + 4 min and R + 8 min form one cycle, and so do 14:59, 29:59 and 30:01.
  - **The representative reset time** of a cycle, used for T, is the `resets_at` of its newest fresh reading,
    ties going to the earlier value.
  - **The current cycle** is the cycle of the newest fresh reading, so a provider's earlier replacement reset
    wins over stale metadata. Stale metadata never makes a sample and never decides a cycle. When the newest
    witness bucket holds fresh readings of two different cycles, pace is `unknown` ("reset time disagrees
    between readings") until a fresh reading in a later witness bucket settles it.
  - A cycle change is also proven by usage evidence: a confirmed possible reset (below).
- **Possible resets come from the raw readings, before any merging.** In corrected-time order within a cycle,
  a reading more than 5 points below an earlier one is a **possible reset**.
  - It is **confirmed** when the next fresh reading belongs to a different cycle, or when the next two readings
    stay more than 5 points below the earlier high.
  - It is **refuted**, and the low reading is then a correction, when a later reading returns to the earlier
    high or above.
  - The raw disagreement is kept on the possible reset, so aggregation never hides it. For example, 95% then
    1% within 150 s of each other is a possible reset, not a 95% bucket.
  - Until it is settled, the pace for that window is `unknown`: "possible reset; waiting for the next reading".
- **Merging Macs uses a canonical partition.** Quota is per account (decision 9). The result is a pure
  function of the set of samples. It is recomputed from that whole set on each fold, so a late reading only
  changes the answer it would have changed had it arrived on time, and arrival order never matters.
  1. Each Mac's `witness_at` is corrected by that Mac's offset from hub time, measured at each sync, as
     `hub_snapshot` takes the hub's clock.
  2. Within each settled cycle segment (split at confirmed resets), readings fall into **fixed buckets
     anchored to the Unix epoch**: `floor(corrected_witness / B)`, where B is 5 minutes for a five-hour window
     and 15 minutes for a weekly one. Buckets don't chain: two-minute samples fill consecutive buckets, never
     one endless cluster.
  3. A bucket's value is its highest reading, since usage only rises within a settled segment. Its time is
     its latest corrected witness. It also keeps its lowest reading, so that its spread is visible.
  4. Between adjacent buckets, a negative gain of 2 points or less, between readings from different Macs, is
     a skew inversion: that interval is excluded as invalid. A larger one is a possible reset (above).
  5. Ties sort by `(segment, bucket, used_pct, mac, source)`.

  Replays of one set of samples in opposite arrival orders give identical buckets, segments and results.

### Burn rate

- **Everything is clipped to the trailing window** (decision 12: **24 hours** for a weekly window, **60
  minutes** for a five-hour window). Consecutive buckets form intervals with a gain and a duration. An
  interval that straddles the window start contributes only its part inside the window: its gain is
  interpolated linearly by the fraction of its duration inside, with that duration. Gain from before the
  window is never counted.
- **Rate = sum of valid in-window gains / sum of the same in-window durations.** A gain and its duration are
  kept or dropped together, and per-cycle rates are never summed. Codex's example (0 h, 10%), (6 h, 10%),
  (18 h, 34%), (24 h, 34%) at now = 24 h gives 24 / 24 = **1 point an hour**.
- **Invalid intervals** are excluded, gain and duration both: one that crosses a confirmed reset, a
  correction, a skew inversion, or one that touches an unsettled possible reset.
- **Long gaps lower confidence; they don't move the average.** An interval longer than the window's gap
  limit (15 minutes for five hours, 2 hours for a week), or an interpolated boundary part of such an interval,
  counts as **gap time**. `observed_pct` = (valid in-window duration − gap time) / the trailing window.
- **When a forecast is allowed.** All of these must hold:
  - at least 3 buckets in the window;
  - valid in-window durations covering at least half the trailing window;
  - `observed_pct` of at least 50%;
  - a last bucket no older than twice the window's sampling interval (20 minutes for five hours, 1 hour for
    a week);
  - a settled current cycle whose `resets_at` is still ahead.

  With gap time over 25% of the window, the line adds "(gappy readings)". Otherwise the verdict is `unknown`
  with its reason: "only 2 readings in the last 24 h", "readings disagree for 9 of 24 h", "readings cover
  only 9 of the last 24 h", "last reading 3 h old", "possible reset; waiting for the next reading", "reset
  passed; waiting for a new reading", or "this account can't be probed".

### Verdicts

Each target is computed separately. The schema carries each one apart.

- **Plan limit** (always). Headroom H = 100 − used; time T = `resets_at` − now.
  - T ≤ 0: `unknown`, "reset passed".
  - H ≤ 0: `limit_reached`, with no forecast.
  - Rate r below 0.01 points an hour: `underpacing`, with about H unused at the reset, no run-out, and "no use
    in the last 24 h".
  - Otherwise needed n = H / T. **Outpacing** when r > 1.15 n (decision 14): runs out at now + H / r, which
    is T − H / r before the reset. **Underpacing** when r < 0.85 n: about H − r·T left unused at the reset.
    **On pace** otherwise. Every verdict states n, the rate that lands on the limit.
  - **Verdicts have hysteresis.** Outpacing starts above 1.15 n and ends below 1.10 n. Underpacing starts
    below 0.85 n and ends above 0.90 n. A rate near a threshold can't flip the verdict back and forth.
- **Share cap** (`share c`: a ceiling on the window, such as rule 1's Grok Bot headroom, held as 80% of Grok's week (Matt 2026-10-19: "I don't want to use anymore than 80%")). This is the same
  calculation with H = c − used.
  - H ≤ 0: `cap_reached` ("at 92% against your 90%"), with no forecast.
- **Daily cap** (`per_day d`: points of the window per rolling 24 hours, such as the priority's 13% a day for
  Claude jobs; decision 13).
  - **G**, the rolling-day gain, is the sum of valid gains inside [now − 24 h, now], with the boundary interval
    interpolated as above. Gain from before the day is never counted.
    - Codex's example, readings (−6 h, 0%), (6 h, 6%), (24 h, 15%) at now = 24 h, gives 3 + 9 = 12, not 15.
      But both of its intervals are longer than the 2-hour gap limit, so every in-window hour is gap time,
      `observed_pct` is 0%, and the daily total is **`unknown`**: "readings cover only 0 of the last 24 h".
    - Readings every 30 minutes at 0.5 an hour give G = 12 with `observed_pct` 100%.
  - A daily total needs valid durations covering at least 20 of the 24 hours and `observed_pct` of at least
    50%. Otherwise it is `unknown`: "the last 24 h are covered for only 14 h".
  - **Over** when G > d. **Near** when G ≥ 0.85 d. **Under** otherwise.
  - **The forecast is of the rolling sum, aging out included, solved exactly.** At the trailing average r,
    the sum Δ from now is S(Δ) = G + r·Δ − A(Δ), where A(Δ) is the in-window gain that leaves the window by
    then.
    - The history is piecewise linear between its buckets, with the boundary interval interpolated, so A, and
      therefore S, is piecewise linear in Δ. **Across an invalid interval** (a confirmed or unsettled reset, a
      correction, a skew inversion), the history contributes **zero**: nothing is interpolated across it, the
      usage drop is never spread over time as negative gain, and no gain ages out over that span. Its breakpoints are the times at which each history point ages
      out: t_i − (now − 24 h), for each bucket time t_i in the day.
    - `reaches_cap_at` is found by walking the segments between breakpoints, over (0, 24 h], in order. If
      S(a) ≥ d at a segment's start a, the crossing is a. If S(a) < d ≤ S(b) at its end b, the crossing is a
      + (d − S(a)) / (S(b) − S(a)) · (b − a). The first crossing found is the answer. No sampling at bucket
      width is involved, so a crossing between scan points can't be missed.
    - Codex's case shows why. With G = 12.99 and r = 0.54125 an hour, no gain over the oldest 4 minutes, 0.1
      over the next 2, then 12.89 spread evenly: S crosses 13 at 1.109 minutes and peaks at 13.026 at 4
      minutes, where every 15-minute scan reads under 13. The exact walk returns 1.109 minutes.
    - The output also carries `peak_pct` and `peak_at`, the highest S over the next 24 hours, so a brief
      crossing is visible as one.
  - If no crossing exists in 24 hours, there is no time, and the line says "stays under at the trailing
    average". For example, a steady 0.5 an hour keeps S at 12 against 13: near, with no time.
  - The output also carries `settles_at_pct` = 24 · r, where the rolling day ends up if r holds.
- **Both caps at once.** Each is computed independently. The **headline** is the most urgent: cap reached or
  over; then the earliest crossing among the plan limit's run-out, the share cap's run-out and the daily cap's
  `reaches_cap_at`; then near; then the rest. A daily cap 2 hours from its crossing beats a plan run-out 120
  hours away.
- **Schema** (`khipu_route`, per account and window):

  ```
  pace: {window, cycle_resets_at, trailing_h, buckets, valid_pct, observed_pct, gappy, rate_pct_per_h, why?,
         plan:      {verdict, needed_pct_per_h, runs_out_at?, before_reset_s?, unused_at_reset_pct?},
         share_cap: {revision, pct, verdict, needed_pct_per_h, runs_out_at?, before_reset_s?, unused_at_reset_pct?}?,
         day_cap:   {revision, pct, verdict, used_24h_pct, observed_pct, settles_at_pct, peak_pct, peak_at, reaches_cap_at?}?,
         headline:  "plan" | "share_cap" | "day_cap"}
  ```

- **Owner caps are structured facts, never read from prose.** Matt confirms each cap once, as an owner action.
  The app offers to create a cap from words it finds, as it does for level claims, and never changes the
  words.

### Where it shows, and when it pushes

- **The routing block:** one line of at most 160 characters per account, on the headline of the window that
  needs attention first. For example: "Claude weekly: outpacing at the trailing average (1.1%/h); runs out Fri
  02:00Z, 1 d 13 h before reset; 0.5%/h lands on 100%".
- **The full summary and `khipu_route`** always carry the current numbers.
- **A per-prompt push** sends a pace line when one of these differs from the **last acknowledged forecast** of
  that component (the ledger keeps it):
  - **Always:** the verdict of the headline target; the limiting window; the cycle; the target or a cap's
    revision; availability (known to unknown and back).
  - **Urgency bands:** the time to run-out or to reaching a cap crosses 24 h, 6 h, 1 h or 15 min on its way
    down. A band fires once, and **re-arms only after the time goes back above the band plus 25%** (30 h,
    7 h 30 min, 1 h 15 min, 18 min 45 s). Readings that alternate between 59 min 59 s and 60 min 01 s push
    once.
  - **Movement:** the projected time moved, against the acknowledged one, by more than max(15 min, 25% of the
    time that remained in the acknowledged forecast).

  So a five-hour window going from 1 h to 10 min left pushes, at both the band and the movement. Small moves
  can't add up unseen, because each one is measured from the last forecast the conversation acknowledged, not
  from the last computed one.
- **The app** shows it after its mock is approved (slice 14, the Pace group).
- **What it replaces.** Phase 4b's "fastest first" arithmetic list keeps its place and its heading. It gains
  the measured rate beside the even rate.

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
- **Proposals across Macs (decision 6).** A proposal's text (a rule's words, the note, a priority entry's
  role words, and its `owner_request`) replicates to Matt's own Macs under the "proposal text between your
  Macs" switch, on by default. It never goes to the gateway projection.
  - **With the switch on,** every Mac converges on the same effective value, the proposal included, once it
    is caught up.
  - **With the switch off,** a text proposal stays on its own Mac until it is confirmed. Its structured parts
    still replicate and take effect everywhere: the entity id, kind, accounts named, `base`, author and
    holds choice. Other Macs show "a pending rule proposal from Mac A; confirm it there", and their pushes
    name it with no words.
  - So with the switch off, structured state converges everywhere, and text converges at confirmation, when
    the text becomes owner words.

## 4. Delivery guarantees

A conversation is **live** when it has had a prompt in the last 12 hours (a setting).

1. **Nothing is silently dropped.** Each delivery component (section 5) has a ledger of change ids: *owed*, then
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

| Client | Start / compaction | Each prompt | During a turn | Achievable boundary |
|---|---|---|---|---|
| Claude Code parent | the paged start block | owed changes | PreToolUse context arrives **with the tool's result, after the call ran** (`routing_delegation.py:20-23`) | the next model step after any prompt or tool result. Before a dispatch: as of its last prompt |
| Codex parent (CLI, `codex exec`) | SessionStart, including compaction and resume (phase 0 fact 4) | owed changes | PreToolUse with the result | as for the Claude Code parent |
| Job runners' `claude -p` / `codex exec` | their own SessionStart | one prompt | as for the parents | its start |
| Grok Build | hook output discarded | discarded | only Khipu tool results | its next Khipu tool call ([[grok-build-shares-the-aegis-home-and-has-no-push-recall]]) |
| Aegis | native pack, no hooks from Khipu | its client pulls `khipu_status` / `khipu_route` | Khipu tool results | its next Khipu tool call |
| Cloud clients through the gateway | MCP instructions line | none | Khipu tool results | its next Khipu tool call, as of the hub copy |

**Children, per harness.** For each one: what is guaranteed before its first step, what is deferred, and what
happens when its brief carries no routing.

| Child | Guaranteed before the first step | Deferred | Brief without routing |
|---|---|---|---|
| Claude Code subagent | A bounded **child block** from SubagentStart, at most `routing_child_chars` (4,000; a setting). In order: account states and holds, then each standing rule and the note whole if they fit, otherwise each one's number, kind, accounts and opening. Today an oversized summary returns nothing (`routing_delegation.py:481`); slice 8a replaces that with the child block | The rest of the rules and the note as pages on the subagent's own tool results, through its per-agent ledger (slice 8a; its PreToolUse is skipped today, `:461-462`) | Irrelevant: SubagentStart doesn't depend on the brief |
| Codex native child (`spawn_agent`) | **Nothing.** Its rollout carries no hook context | Changes only on its own Khipu tool calls, under its own `CODEX_THREAD_ID` identity | It runs with no routing. Slice 8b reads the brief at the parent's `spawn_agent` PreToolUse (`briefs_of`). When the brief has no routing block, the parent's result carries one advisory line: "This child got no routing: add `khipu route --brief` to its brief." Nothing is added automatically |
| T3 `delegate_task` child | The child provider's own paged start block, if Khipu's hooks are installed for that provider. Otherwise nothing, and doctor names the provider | Pages on its own prompts and tool results | As for a Codex child: the advisory line on the parent's result (slice 8c) |
| Job-runner job | Its SessionStart start block | Its prompts | The runner task (routing-truth) adds `khipu route --brief`. Until it does, the job relies on its SessionStart |

**Parent freshness before a dispatch is an explicit limitation, and a scope choice.** A change that lands
mid-turn reaches the parent only after its dispatch has gone out. A bounded, fail-open read before dispatch
would not by itself make routing a gate. This scope doesn't take it on because no hook in Claude Code or
Codex delivers context before the call runs. It would need a client-side step in every harness for a window
of one turn. For children that get a start block (Claude Code subagents, and T3 and job-runner children whose
provider has Khipu's hooks), that block covers the freshness that matters for the delegated work. A Codex
native child, or a child whose provider lacks the hooks, gets no block and relies on its brief. Matt can
reopen it as its own slice.

## 5. Pull delivery: identity and acks

`khipu_route` takes only `since` and `account` today. Authors come from the environment
(`conversation_author`), and the gateway is stateless (`gateway.py:14`). Slice 7 adds the following.

One canonical identity mapping covers hooks, MCP, provider switches and children (slices 7 and 8).

- **A conversation** is `(mac, root)`. The harness is an attribute of the conversation, never part of its key.
  The root is decided in this order:
  1. `KHIPU_SESSION_ID`, set by Khipu's T3 integration to `t3:<thread>` for every provider. A switch from
     Claude Code to Codex in one T3 thread therefore stays one conversation with one ledger.
  2. Otherwise the harness's native id, prefixed: `claude:<CLAUDE_CODE_SESSION_ID>`, `codex:<CODEX_THREAD_ID>`.

  `conversation_author` (`routing.py:1066`) keeps its harness field for attribution. Delivery stops reading
  identity from the harness.
- **A delivery component** is `(conversation, component)`. Each component has its own ledger:
  - `main`: the parent's hooks (start, prompt, and the parent's PreToolUse, `routing_delegation.py:470`).
  - `agent:<agent_id>`: a Claude Code subagent's SubagentStart and its own tool results. The `agent_id` comes
    from the hook payload.
  - `mcp`: Khipu MCP tool results within that conversation, from any agent in it. The MCP payload carries no
    agent id, so MCP deliveries can't be told apart by agent.

  A Codex native child, or a T3 child, is its own conversation under its own root, and is never a component
  of the parent.
- **Who owns an ack.** A batch belongs to the component it was issued to, and only that component's ack moves
  it.
  - Hooks ack through their existing `op: ack` / `finish_delivery`, carrying the component.
  - MCP clients ack by passing `ack: <batch>`. An ack naming a batch issued to another component or identity
    is refused (`ack_not_yours`), and that batch stays where it was.
  - A duplicate ack is a no-op. An unknown or expired batch is answered, never an error.
- **Duplicates across components are allowed and named.** The same change may reach `main` and `mcp`, because
  a subagent's MCP call is not the parent's view. Duplicates are never allowed within one component: within
  a component, hook and MCP deliveries reserve what they send as in flight under one lock.
- **Which component's ack counts for delivery health** (section 9). This depends on what the client can do:
  - **Capability comes from the current attachment, never from history.** Each delivery records the
    harness of the conversation's attachment at that moment: the provider now serving it, from the hook
    payload or the MCP caller's environment. Health judges a change by the capability of the attachment
    current when the change was owed and delivered.
  - **Hooked attachments** (Claude Code, Codex, job-runner jobs). `main` counts, and `mcp` acks don't,
    because a subagent may have made the call.
  - **Pull-only attachments** (Aegis, Grok Build, gateway clients). `mcp` counts.
  - So after a T3 switch from a hooked provider to a pull-only one, valid MCP acks clear stalled health, even
    though the conversation's ledger once had hook deliveries. A switch back makes `main` count again.
  - **A child's ack never satisfies its parent.** `agent:<id>` acks count only for that subagent. A Codex or
    T3 child is its own conversation. On a pull-only client, a child must pass its own session id
    `<parent>/<child>` (the MCP instructions say so). A session id containing `/` is a child, and its ack
    never moves its parent's delivery. A pull-only child that reuses its parent's id is indistinguishable
    from it: this is a stated limit.
- **Batches and pages.** Each result that carries routing has `routing_changes: {batch, component, items[{change,
  entity, revision, page?}]}`. Calling again without an ack re-sends the same in-flight items, so a dropped
  response is re-sent.
- **With no identity, nothing is consumed.** A local MCP caller whose environment names no session, or a
  gateway caller with no `session_id`, gets the current state and any open pages. Its result says
  `delivery: "untracked: pass session_id"`, and nothing is marked delivered for anyone.
- **The gateway** identity is `(token label, session_id)`. Clients that share a token must use session ids
  unique among them, of the form `<client>:<stable id>`. The MCP instructions say so. The gateway refuses an
  ack whose batch belongs to another identity under the same label. Two clients that share a token and
  wrongly reuse one `session_id` are indistinguishable to Khipu: this is a stated limit, and the instructions
  name the format.
- **Scrubbing stays.** Routing additions on memory-tool results go through `agent_view`'s routing scrub, and so
  do their error responses (`_AGENT_SCRUBBED`). The memory content itself is never scrubbed, as today.

## 6. Privacy boundary

Four layers, each a projection of the one before. Slices 11a and 12 test each field of each layer.

| Layer | Holds | Never holds |
|---|---|---|
| **Local history** (`events.jsonl`) | everything, as today | unchanged; nothing in it is rewritten |
| **Replication payload** (Mac to hub) | ids, kinds, structured fields, stable account ids, `hlc`, `base`, author as `{kind, harness, mac}`, the owner's confirmed rule and note words as he wrote them (paths included), owner hold words, and, under the "proposal text between your Macs" switch, a proposal's text and its `owner_request` | login homes and any nested `<harness>\|<home>` key (mapped to stable ids); other conversation free text (a conversation hold's words, evidence lines, self-given names); project and session names (replaced by keyed hashes); proposal text when its switch is off |
| **Hub storage** (the next free migration, 0042 today: PR #257 holds 0041; its own tables) | the payload, append-only, keyed by event id, with the owner's original words (paths included) stored as written; a per-Mac source status row | anything the payload doesn't hold |
| **Gateway projection** | labels, states with times and basis, holds (structured; owner hold words under opt-in), rules and the note under opt-in, the caller's own delivery state | paths of any kind (cut to "a path" even inside owner words; local owner words untouched), other identities' delivery state, Mac ids |

- **The old cloud opt-ins are amended:** "copy for cloud agents" now names three switches: account state (on
  when the copy is on), owner hold words, and rules and note. Between Macs, a fourth switch, "proposal text
  between your Macs", covers proposal text and its `owner_request`. It never reaches the gateway (decision 6).
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
  host?, lane_names: [...], models_allowed?: [...], models_never?: [...], use: normal|on_request}`. The
  model fields and `use` replace standing rule 13's model lines and Antigravity's exclusion; the spend and
  concurrency lines become 17a caps. Rule 13 retires only after cases 8 and 17b show each of its lines held
  as a fact. `family` (Anthropic, OpenAI, xAI, Meta, Zhipu, DeepSeek) is what rule 1's
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
  Khipu imports. It flags a routing-state claim with its file and line. A line is a claim when it names an
  account, provider or lane, and either:
  - pairs it with a state word (open, limited, paused, held, tight, low, down) plus a date or a level; or
  - makes a **priority or ordering claim** about it, with or without a date or level: "first", "second",
    "before", "after", "order", "priority", "prefer", "main", "only", "not used", or a numbered ranking of
    accounts.

  "Delegation order: second Claude account first" has no date or level, and still matches as an ordering
  claim. Recall marks such a note "routing state: superseded by Khipu's record" in the prior-work
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
- **Pace:** each account's windows with burn rate, the rate needed, the verdict and its target (plan or cap), a
  small trend of the trailing window, and the caps with their confirm state. Section 13 adds the
  `pace_target` allowance and whether the account is ahead, spend against its cap with its coverage,
  running jobs against `concurrency`, the current advice per purpose, and an unknown-evidence state.

## 12. Acceptance: replay the real failures end to end

Two layers, built in slice 2 and kept apart.

- **Hermetic replay** (`tests/test_routing_acceptance.py`, slice 2a) drives the fold, the ledgers, the hook
  entry points and the MCP tools in-process under a temporary HOME. It runs in the normal oracle.
- **Received-context acceptance** (`tests/acceptance/routing_harness/`, slice 2b) runs the real harnesses
  headless (`claude -p`, `codex exec`, a Claude Code subagent, a Codex `spawn_agent` child, a T3 provider
  switch). It asserts on what the model received: the transcript's hook attachments, Codex's developer
  messages, the MCP result body. It never asserts on hook stdout. It needs signed-in harnesses, so it runs
  on this Mac, outside the hermetic oracle, at most once per slice that touches the matrix.

**Fixtures are sanitized.** A script turns the private log into a fixture with the same shapes, ids, times
and lengths, with synthetic words and no paths. The private log is never tracked. Each case starts `xfail`
and is flipped by the slice named.

**The pace fixture has its own closed schema,** `pace_fixture_v1`. It holds only rows of the form `{acct: "a1",
win: "w1", t: <int seconds>, t_err: <int seconds>, used: <number>, reset: <int seconds>}`, with synthetic
account and window ids, numeric times shifted to a fixed origin, and nothing else: no text, bodies, tool
arguments, paths, labels or session ids. A loader rejects any other key.

- **The extractor** (`tools/pace_fixture_extract.py`, slice 4Pa) is local and deterministic. It uses no model
  and no network. It reads transcripts and keeps only the account lines inside fenced "Routing" blocks,
  matched by a fixed pattern.
- **Sentinel tests.** Sentinel strings are planted in surrounding message text, tool arguments, tool results
  and paths. The tests prove that none reaches the fixture, the extractor's stdout or stderr, or its error
  messages.
- **A block's delivery time is not a reading's time.** The witness is the block time minus the stated age
  ("read from Codex 4 min ago"). `t_err` carries the rounding of that age and of the block's clock.
- **Rows that are not fresh samples.** A row whose age is relative with no minute precision ("2 h ago"),
  whose `t_err` exceeds the sampling interval, or that repeats the same value with a witness within `t_err`
  of an earlier row is dropped or merged. Repeated cached readings never become fresh samples.
- Verdict tests on this fixture allow for `t_err`. Exact hand calculations use the synthetic cases.

| # | Case | Layer | Slice |
|---|---|---|---|
| 1 | The Grok hold and the 10-19 priority: the contradiction is shown everywhere; an owner write is refused until it says end or keep; "end" removes the hold from every next delivery | hermetic | 4 |
| 2 | A 3,000-character pending rule, a 10,500-character session start, and a rule edited mid-pagination: whole across pages, restarted on the edit, never pointer-only | hermetic | 6 |
| 3 | Eight changes against a 400-character budget: all shown within the budget-aware bound; no ack without a show | hermetic | 6 |
| 4 | Proposals: two conversations take turns, the first is told it was replaced, neither is refused; a delayed retry after an owner write is crossed; expiry and Undo leave the owner revision and never revive a replaced proposal; a dispatched job's owner act is refused; with proposal text off, Mac B shows the placeholder and converges at confirmation | hermetic | 5 (Mac part: 11b) |
| 5 | "Codex is low" against a 96% reading is marked; a structured claim expires at the reset | hermetic | 4 |
| 6 | Pull protocol: no identity consumes nothing; a dropped response is re-sent; a duplicate ack is a no-op; an ack for another component or identity is refused; hook and MCP in one component never double-send; `main` and `mcp` duplicates are named; routing additions on error results are scrubbed; after a provider switch from a hooked attachment to a pull-only one, valid MCP acks clear stalled health | hermetic | 7 |
| 7a | Claude Code subagent: the child block arrives before its first step with an oversized summary; deferred pages arrive on its tool results | received context | 8a |
| 7b | Codex `spawn_agent` child: no routing reaches it; a brief without routing gets the advisory line on the parent's result | received context | 8b |
| 7c | T3: a Claude Code to Codex switch in one thread keeps one ledger; a T3 child without Khipu's hooks is named in doctor | received context | 8c |
| 8 | Providers: `glm` and `deepseek` added as facts match by name; a two-account provider and a name claimed twice stay unmatched with candidates; DeepSeek with `models_never: [v4-pro]`, Muse with `models_allowed: [muse-wonder-full-1.0]` and Antigravity with `use: on_request` show in `khipu_route` and the advisory check flags a dispatch that breaks them | hermetic | 9a |
| 9 | The `http-quota` adapter: a redirect is refused; the sentinel key is absent from every channel | hermetic | 9b |
| 10 | An Opus dispatch with `task: fix-round` gets the rule 12 line with its result; a Sonnet one gets nothing; the hook never denies. Rule hygiene: a rule saying "until Saturday" gets the move-to-role flag, a passed one the expired flag; two overlapping rules and a role restating a rule are flagged; a 25-hour-old pending edit is listed with its owner direction; no owner word changes | hermetic | 10 |
| 11a | Account migration: an old salted id resolves within 90 days and fails explicitly after; a restored log keeps the start; a rename forwards; a duplicate label is refused; an old local log folds unchanged | hermetic | 3a |
| 11b | Event identity: two Macs adding a rule at once keep both, numbers pending until the hub orders them; a numeric target on a pending number is refused with the uuid; an `op` retry returns the first result; a `hold_end` before its `hold` applies on arrival | hermetic | 3b |
| 12 | Two Macs (two HOMEs, test hub): opposite arrival order, a skewed clock, a duplicate upload, an interrupted sync, a restore with a new Mac id; probe limits, runner limits, reopening, renewals, source silence and clock-only expiry fold the same on both; an unchanged cached reading never renews; "not installed on Mac B" leaves the account open on Mac A | hermetic | 11a, 11b |
| 13 | Gateway: the projection holds only its listed fields (every nested and history field checked); another identity's delivery state is invisible and its acks are refused; owner writes are refused | hermetic | 12 |
| 14 | Scanner: flags the two live notes; passes capability rows, evidence dates and quoted history | hermetic | 13 |
| 15 | Health: each red condition turns red and back to green on its own evidence | hermetic | 16 |
| 16a | Pace inputs: the extractor's sentinel tests; `pace_fixture_v1` rejects any other key; relative, rounded and repeated readings never become fresh samples; two Macs' samples with the same time and different values, a skewed clock, and different reset times merge to identical buckets in opposite arrival orders; two-minute samples over 24 h make consecutive buckets, not one cluster; a late reading changes only what it would have changed on time; 95% then 1% within 150 s is a possible reset, settled by the next readings either way; an earlier replacement `resets_at` carried by fresh readings wins over stale metadata | hermetic | 4Pa |
| 16b | Pace results: Codex's gap example gives 1 point an hour; (−6 h, 0%), (6 h, 6%), (24 h, 15%) computes 12, not 15, but is `unknown` with `observed_pct` 0%; readings every 30 minutes at 0.5 an hour give 12 with 100%; Codex's aging-out case crosses 13 at 1.109 minutes (exact walk) with `peak_pct` 13.026 at 4 minutes, where a 15-minute scan would miss it; cycle grouping, each case in every arrival order: 14:59 and 30:01 with no middle reading form one cycle; 14:59, 15:00, 30:00, 45:00 and 60:01 (a 45 min 02 s span) form one cycle and are not ambiguous; R, R + 4 min and R + 8 min form one cycle; 14:59, 29:59 and 30:01 form one cycle; a component spanning more than 1 h gives `unknown` with "reset time spread over more than an hour"; two resets 5 h apart are two cycles; a reusable provider window id ("weekly") on two successive resets gives two cycles, and a unique provider cycle id groups its readings; mixed readings with and without ids group as specified; a daily cap 2 h from its crossing is the headline over a plan run-out 120 h away; a reset inside the day contributes zero across its interval and no interpolated drop; a steady 0.5 an hour against a daily cap of 13 gives near with no time; a rising rate gives a `reaches_cap_at` that accounts for usage aging out; long gaps lower `observed_pct` and mark the line gappy; synthetic outpace, underpace and on-pace; a reset mid-window; a correction drop; a possible-reset drop later confirmed and later refuted; coverage below the floor gives `unknown` with its reason; zero burn; limit and cap already reached; a reset time passed; a share cap and a daily cap together, carried apart in the schema; the sanitized real history within its `t_err` | hermetic | 4Pb |
| 16c | Pace pushes: a five-hour run-out moving from 1 h to 10 min while still outpacing pushes; readings alternating at 59 min 59 s and 60 min 01 s push once; a rate hovering at 1.15 n doesn't flip the verdict; a change of limiting window, cycle, cap revision or availability pushes; small moves measured against the last acknowledged forecast push once they add up; numbers alone don't push | hermetic | 4Pa |
| 17a | Discovery: a Claude Code home pointing at api.z.ai is offered as GLM; a settings file with a sentinel auth token never shows it anywhere; a folder holding only `worktrees` is not offered; a Keychain-backed Claude home with no auth file is offered when its CLI reports signed in, and a GLM home with a settings token is offered with the token never read out; three Grok homes are one account only when the CLI's account status agrees, otherwise separate candidates an owner can merge; auto-track off survives a restart. Lanes: `budget_account` matches with no `routing_lane_map`; contradictory declarations stay unmatched; a lane report on a declared lane is accepted; a declaration is `claimed` until a reading confirms it | hermetic | 9a, 9c |
| 17b | Advise: a `pace_target` of 65% with 5 slack at 18% used 11 h into the week (allowance 9.26%) answers `hold_back` for `job` and `go` for `orchestration` from an undispatched attachment, and `hold_back` for a dispatched job claiming `orchestration`; zero current burn is still `hold_back` while ahead; recovery needs 1 point under the allowance; a `spend` of $35 at $35.10 and a full `concurrency` 1 answer `hold_back`; expired telemetry answers `go` with `evidence: unknown`; a pause answers `closed` for both purposes; `orchestration` at a 91% `share` or a reached `spend` answers `hold_back`, and at a full `concurrency` answers `go` | hermetic | 17a, 17c |
| 17d | Owner direction end to end: an eligible conversation in Claude Code, Codex and Grok Build applies a cap, a rule edit and a priority through `khipu_owner_act`; each is live without a terminal, logged with its undo, and refused from a dispatched job | hermetic | 5, 17a |
| 17e | Telemetry: duplicate spend events count once and two attempts of one job count twice; a correction nets out; a period where one of two runners on an account has a gap totals `unknown` even though the other covers the week; two accounts on one provider never pool; a new `active` snapshot replaces the old; two runners' active jobs sum; a silent runner's records expire to `unknown`; a lane's spend with no resolvable account is unattributed, never guessed | hermetic | 17t |
| 17c | Directions: "use GLM only one job at a time" typed by the owner with no matching owner action opens one owed item naming the words; an unrelated routing write leaves it open; the matching cap closes it, and setting that cap to 4 instead leaves it open; a question, a quote and a job prompt open nothing; two directions with one written leave one open; a dismissal survives the next capture; the same prompt typed twice opens two directions, and each closes only on its own occurrence's action (when only the second acts, the first stays open); a recorder that missed a turn leaves the direction `unjoined`; a conversation resumed after the 24-hour turn prune keeps its occurrence numbers and its directions still join; a runner cap with no Khipu fact is flagged | hermetic | 19a, 19b |

## 13. Amendment, 2026-10-20 afternoon: routing that runs the day

Asked by Matt 2026-10-20 after the Church Harness conversation's routing never reached Khipu: "we're trying
to dynamically adjust our usage to not run out early. That's what the other conversation I think is trying
to do. That should be being done inside Khipu", and "this needs to be something that works across all
computers automatically". Then: "make sure nothing else was missed".

What went wrong that day:
- The Church Harness orchestrator kept Matt's routing directions in its own memory and its job runner's lanes
  config: a pace guard for the main account (an even pace toward 65% of the week, 5 points of slack, 91% at
  most, the five-hour window at 60%), DeepSeek Flash only at $35 a rolling week, GLM one request at a time,
  Muse's limits. Khipu held none of it, so every other conversation routed without it.
- Five of that runner's lanes stayed unmatched (`main-sonnet`, `main-haiku`, `secondary-haiku`, `glm`,
  `deepseek`) because matching is by lane name or a hand-written per-Mac map (`routing_lane_map`,
  `routing.py:1711`). The runner already declares `budget_account: "claude|"` on its main lanes, and Khipu
  drops that field while parsing (`routing_sources.py:467`).
- The app's "7 found" were name-based suggestions: two Claude Code homes pointed at GLM and DeepSeek, two
  folders that were never logins (`~/.codex-workspaces`, `~/.claude-server-commander`), two more homes of the
  one Grok subscription (`~/.grok-dispatch`, `~/.grok-drive`), and one real login (`~/.claude-pro`).
- The rules had grown to twelve, three of them repeating each other and two describing one week's account
  state. Matt's consolidation (owner direction, 2026-10-20) left seven: 1, 3, 5, 6, 7, 9 and 12. Rule 1 now
  carries the old 2 and 10, and rule 3 the old 4. Rules 8 and 11 were retired; their content lives in the
  priority roles. Rule 11 had sat pending all day because a conversation wrote it through the proposing tool.
- A sweep of the last ten days (Codex, read-only) found directions Khipu lacked or held differently: Grok's
  ceiling (80%, not 90%), DeepSeek (Flash only, $35 a rolling week, never privacy or money code, checked by
  another family), GLM (one request at a time, the same floor), Muse's model, Antigravity unused, Opus for
  documents third parties read, workflows always encouraged, and main's 13% a day. They were written into
  Khipu by owner direction the same day: rule 1 now covers Grok, GLM and DeepSeek; rule 12 gained the
  documents and workflows lines; a new rule 13 holds the per-provider model and budget facts until slices 9a
  and 17a make them account facts, when rule 13 retires.
- Slice 1's fixes merged as #260 but were not live: this Mac ran #243 until the next promote, so the push
  still said "too long to show here".

**A. Usage budgets and launch advice** (slices 17t, 17a, 17c, 17b).

*Telemetry first* (slice 17t). Khipu can only judge what it sees, so the runner heartbeat contract gains two
typed, timestamped records, each attributed to an account through the resolver in B:
- `active {runner, lane, account, jobs, unit, at}`: a snapshot. The key `(runner, lane)` replaces the earlier
  record each heartbeat, and the snapshot expires at the runner's silent deadline. `unit` is `job` today:
  GLM's runner sends a job's requests in turn, so one job is one request at a time.
- `spend {event, runner, job, attempt, provider, account, usd, at}`: an increment, never a running total.
  - `event` is a uuid the runner mints, and records are deduplicated on it. Each retry is its own `attempt`
    and its own event, so retries count.
  - A correction is a new event with `corrects: <event>` and the signed difference.
  - Totals sum across runners and Macs (section 7).
- `spend_coverage {runner, account, from, to}`: the interval for which that runner vouches it reported every
  spend for that account. Each runner that launched work on an account during a period is a stream.
  - **Known:** an account's total for a period is known only when every stream that took part in the period
    covers its own participation interval.
  - **Unknown:** one runner with a gap makes the total `unknown`, even when another runner covers the whole
    week.
  - **Accounts never pool:** two accounts on one provider are separate.

The producer side is new work. Today's heartbeat (`lanes.py:2308`) publishes no counts. Slice 17t ships the
contract and a reference emitter in the runner doc, and slice 17b's first step makes the Church Harness runner
emit it.
Missing or expired coverage is `unknown`, never zero.

*Cap kinds* (slice 17a). Section 2a's caps gain three kinds. Each is an owner fact, confirmed once and never
read from prose.
- **`pace_target {pct, slack}`**, a limit on how far ahead of an even pace an account may run.
  - **Allowance:** at a time e into a window of length W, the allowance is `pct · e / W + slack`. At 11 hours
    into the week, a target of 65% with 5 points of slack allows 65·11/168 + 5 ≈ 9.26%.
  - **Ahead:** the account is `ahead` when used > allowance, whatever its current burn. It recovers once
    used ≤ allowance − 1 (one point of hysteresis).
  - **Unknown:** with no reset time or no reading, the verdict is `unknown`.
  - **Separate from the forecast:** being ahead is reported apart from section 2a's forecast verdict, and
    both carry their own tests.
- **`spend {usd, days}`**: the 17t spend total over the rolling period against `usd`. If the period isn't
  covered, it is `unknown`.
- **`concurrency {n}`**: 17t's active jobs against `n`. If the record has expired, it is `unknown`.

The window share (91% of the week, 60% of the five hours) is section 2a's `share`.

*Advice* (slice 17c). `khipu route advise` and an `advice` object per account in `khipu_route`.
- **The request** names a lane, an account, or a tier and kind: `--tier opus|sonnet|haiku`, `--privacy`,
  `--kind build|fix|check`, and `--purpose job|orchestration`.
  - `purpose` defaults to `job`.
  - `orchestration` is accepted only from an attachment that is not a dispatched job. Capture lineage
    already marks dispatched jobs (`CLAUDE_DISPATCHED_LANE`, the delegation hooks' child identities), and a
    job that claims `orchestration` is answered as a `job`.
- **The answer:** `go`, `hold_back` or `closed`, each with its reason, its numbers, and an `evidence` field
  (`fresh`, `stale`, `unknown`).
  - **`closed`:** a limit, a pause or a hold, for every purpose.
  - **`hold_back`**, per cap kind and purpose:

    | Cap | `job` | `orchestration` |
    |---|---|---|
    | `pace_target`, `per_day` | applies | exempt |
    | `concurrency` | applies | exempt, because orchestration is not a runner job |
    | `share`, `spend` | applies | applies |

    It also applies to `job` when any cap crosses within the hour at the trailing average.
  - **Unknown evidence:** the answer is `go` with that evidence status, so the consumer can apply its own
    policy.
- **Ranking:** `advise` ranks what is open in the owner's priority order and says why. It replaces section
  2a's "Khipu still returns no pick" with advice that nothing is obliged to follow.
- **Pushes:** a change of advice for an account in the conversation's priority is pushed under section 2a's
  push rules.

*The Church Harness runner* (slice 17b, in church-harness, with Matt's go). Its numbers are converted, not
copied:
- the main account's 91% becomes a `share` of 91% on the week;
- its even pace toward 65% with 5 points of slack becomes a `pace_target`;
- its five-hour 60% becomes a `share` on that window.

Each is written to Khipu by owner direction. The runner keeps its local lane `cap` as capacity on this Mac,
which is not a quota. Every budget consumer (`lane_status`, `lane_report`, `budget_check`) calls `advise`
instead of its own arithmetic. The runner keeps its own fail-closed policy for missing, stale, timed-out or
malformed advice.

**B. Runner identity, so lanes match on any Mac** (slice 9a, extended).
- **One resolver** serves evidence, `report()` (which today refuses any lane missing from the hand-written
  map, `routing.py:3913`), and health. It resolves in this order:
  1. the owner's override (`routing_lane_map`);
  2. the runner's declaration: `account` and/or `budget_account`, when every field it gives agrees with the
     others and with the provider and login-home binding of slice 3a;
  3. the provider and login home alone;
  4. the name (decision 3).
- **Conflicts never match.** Contradictory fields, a label two accounts share, or a stale binding leave the
  lane unmatched, with candidates.
- **Claimed, then confirmed.** A declaration stays `claimed` until **identity-bearing** evidence ties the lane
  to the account's launcher binding. Two cases count:
  - Khipu captures a job launched from that lane, and the job's own attachment resolves, through slice 3a's
    bindings, to that account's login home.
  - The heartbeat names the lane's login home, and the binding resolves it.

  A limit or usage reading that merely agrees never confirms: two accounts can read alike. Both states show
  in health.
- **Tier lanes** (`main-sonnet`) match by declaration, not by name.
- The heartbeat contract documents these fields. An unmatched lane's health row names the field to add.

**C. Discovery offers only real logins, and one subscription is one account** (slice 9c).
- **Provider-pointed homes.**
  - **One permitted read:** a bounded local parser may open a Claude Code home's `settings.json`. Its only
    output is a validated hostname from `env.ANTHROPIC_BASE_URL`: no userinfo, path or query, from an
    allow-list of provider hosts.
  - **Nothing else survives:** every other byte is dropped in that function. A parse failure is recorded as
    `unreadable`, never with its error text. No log, cache, model or replication ever sees the raw settings.
    Today's rule that discovery opens no files inside a login home (`discover.py:27`) gains this one named
    exception.
  - **Tested with sentinels:** a settings file holding a sentinel token in `ANTHROPIC_AUTH_TOKEN` must never
    show that token in any output, log or cache.
  - **Auth files** are checked with `stat` for existence only, never opened.
- **Login evidence required**, never the folder name alone. Any of these counts:
  - an auth file present (checked with `stat`);
  - the CLI's own signed-in status for that home, which covers Keychain-backed Claude logins
    (`usage.py:655`);
  - a provider-pointed home whose settings hold a token. The bounded parser reports only that one is present,
    and its value is dropped in the parser.
- **One subscription is one account.** Several homes count as one subscription only when a supported source
  names the same account: a CLI's own account-status output, parsed for its account id only. A provider
  with no such source leaves the homes as separate candidates. The owner can merge them with one owner
  action ("same subscription"), and Khipu never guesses. Merging reconciles through slice 3a's bindings.
- **Auto-tracking.**
  - An explicit "off" persists as `false`; today turning it off deletes the setting (`accounts.py:996`).
  - "On by default" (decision 15) applies only to a new install with no setting.
  - This Mac was switched on by owner direction on 2026-10-20.

**D. Rules stay few** (slice 10, extended; case 10 gains these). These are advisory flags. Khipu never edits
the owner's words.
- **Wording that belongs in a role:** a standing rule worded for a time ("until", "this week", a weekday, a
  date ahead) gets a suggestion to move it into the account's priority role with an `until`.
- **Expired rules:** a rule whose date or named reset has passed.
- **Overlaps:** two rules naming the same account and topic; a priority role that restates a rule.
- **Long-pending edits:** a conversation's pending edit older than 24 hours is listed as waiting on the owner,
  with the one-line owner direction that makes it live or drops it.

**E. Directions that never reach Khipu** (slices 19a, 19b).
- **19a, capture.**
  - **What counts:** a `routing_direction` is recorded only from the owner's own typed turn (the provenance
    `owner_direction.check` already uses), with the exact span and an actionable intent. Questions,
    quotations, hypotheticals, discussion and dispatched jobs' prompts are abstained on.
  - **The join** gives each owner turn an occurrence identity.
    - **At recording:** when `owner_turns` records an owner turn, it stores `prompt_sha256` of the canonical
      prompt text (one shared function, used by capture too: Unicode NFC, trimmed, whitespace collapsed). It
      stores that, never the text, beside the turn's existing random id.
    - **At the action:** `owner_direction.check` already picks the eligible turn. It copies that turn's id
      and `prompt_sha256` into its stamp, and the owner-actions log's field allowlist (`owner_direction.py:805`)
      gains both fields.
    - **Occurrence:** a durable per-session counter numbers each hash: the first time a prompt is typed it is
      1, an identical later prompt is 2, and so on. The counter lives in its own file per session
      (`owner_turns/occurrences/<session>.json`). It is kept 30 days after its last update, apart from the
      turn records that are pruned at 24 hours (`owner_turns.py:528`), so a conversation resumed the next day
      keeps counting.
    - **At the action:** the owner-actions log entry, which is append-only and never pruned, carries the
      triple `(session, prompt_sha256, occurrence)` itself, copied from the turn when the action is made.
      Capture joins on the log's triple, so pruned turn records don't matter.
    - **At capture:** capture counts the same occurrence numbers from the transcript's owner turns. A
      direction is keyed `(session, prompt_sha256, occurrence)`, and it joins only the actions whose stamp
      carries exactly that triple.
    - **Repeated prompts:** each occurrence joins only its own actions. If only the second of two identical
      prompts acted, the first stays open.
    - **Counts that disagree:** if the transcript and the recorder count occurrences differently (a missed
      hook, or a counter expired after 30 idle days), the direction is `unjoined`. It stays open with that reason, and nothing is
      guessed.
  - **How it closes:** the direction carries a structured target where one can be read (entity, kind, value:
    "GLM, concurrency, 1"). It closes only when an applied owner action joined to its prompt matches that
    target and value at the resulting revision; setting the cap to 4 leaves it open. A direction with no
    structured value closes only when an owner action cites it (`--direction <id>`) or the owner dismisses it.
  - **The owed item** ("Routing said here, not in Khipu: '<words>'") opens only when no matching action is
    found. Its stable key is the session, the prompt and the span, and a dismissal persists.
  - **Tests** include negative cases (a question, a quote, a job prompt), several directions in one prompt,
    and a partial write.
- **19b, runner configs.** The scanner (slice 13) compares a runner's caps and budget fields with Khipu's cap
  facts (17a) and flags any value with no matching fact.
- **For conversations themselves:** the global CLAUDE.md says so, since 2026-10-20.

**F. Merged is not live.** A slice is done when `khipu promote` has run and a live `khipu_route` (or the
pushed block) shows its change. The PR merging is not enough ([[merged-is-not-deployed]]).

## Slices

Each slice fits one session and leads with "Read this scope first; inspect {named code} before writing". One
PR each. Every slice reaches this Mac only through `khipu promote` and its live gate. The builder and the
reviewer always come from different families.

**Oracle for every slice:** hermetic pytest from `packages/cli` (temp HOME, `KHIPU_KEYCHAIN=0`; the baseline is
the named failures), `ruff check --no-cache packages/cli`, and the slice's hermetic cases flipped to pass with
no other case regressed. Slices 8a to 8c also run their received-context cases. At most 3 oracle runs per
change; if it is still red, stop and report the last log. Slices on the per-prompt path also run
`python -m khipu.routing_replay` (no new timeouts).

| # | Slice | Depends on | Builds | Reviews | Cases, stop-conditions |
|---|---|---|---|---|---|
| 1 | Today's A to F | none | Codex (in flight) | Sonnet | its own acceptance |
| 2a | Hermetic replay harness, sanitizer, hermetic cases `xfail` | 1 | Codex Sol, high | Sonnet | each case fails for its stated reason, not on a harness error; no private text in the fixture |
| 2b | Received-context harness: headless runs and transcript readers, cases 7a to 7c `xfail` | 2a | Codex Sol, medium | Sonnet | the rig reads a known sentinel from each harness's received context before any case is trusted |
| 3a | Account migration: stable ids, bindings, renames and duplicates, aliases with the 90-day window and explicit failure | 2a | Codex Sol, high | Opus | case 11a; the existing log folds to the same view |
| 3b | Event identity and ordering: global event and entity ids, uuid targets, provisional rule numbers, HLC, `base`, `op` dedup, tombstones | 3a | Codex Sol, high | Opus | case 11b |
| 4 | Entry model: kinds, observations and renewals with scope and `expires_at`, definitions as facts, end-or-keep, `level` claims | 3b | Codex Sol, high | Opus | cases 1, 5; owner words byte-identical |
| 4Pa | Pace plumbing: sampling per window, sample storage and Mac merge, cap facts and the owner action that confirms them, the extractor and `pace_fixture_v1`, the push rules, the privacy and projection integration, and the tests for 4Pb (4Pt) | 4, 6 | Codex Sol, high | Opus (owner action on caps) | cases 16a, 16c; `routing_replay` holds |
| 4Pb | Pace calculation and formatting only: a pure function from sanitized samples, caps and now to the `pace` objects and the block line | 4Pa | Grok, against 4Pt, on sanitized inputs only | Codex re-check (a different family, which satisfies rule 1's re-check), then Sonnet | case 16b |
| 5 | Owner precedence and proposals, with the proposal-text switch | 3b, 4 | Codex Sol, high | Opus | case 4 (local part); `owner_direction.check` unchanged |
| 6 | Delivery ledger and pagination for hooks: components, owed, in flight, acked, leases, pages with revisions | 3b, 4 | Codex Sol, high | Opus | cases 2, 3; `routing_replay` holds; today's budget caps |
| 7 | Pull delivery: canonical identity, components, batches, ack ownership, `routing_changes` on tool results, scrubbing incl. errors | 6 | Codex Sol, high | Opus | case 6 |
| 8a | Claude Code child: the bounded child block, the per-agent ledger and deferred pages | 7, 2b | Codex Sol, medium | Sonnet | case 7a |
| 8b | Codex native child: brief inspection and the advisory line | 7, 2b | Codex Sol, medium | Sonnet | case 7b |
| 8c | T3: `KHIPU_SESSION_ID=t3:<thread>` in Khipu's T3 integration; doctor names providers without hooks | 7, 2b | Codex Sol, medium | Sonnet | case 7c; stop and report any matrix row the received context disproves |
| 9a | Provider catalog as facts; lane matching by declared identity, then login home, then name; ambiguity | 4 | Codex Sol, medium | Sonnet | case 8 |
| 9b | `http-quota` adapter | 9a | Codex Sol, high | Opus (credential path) | case 9 |
| 10 | Advisory: rule 12 tier table and check, structured contradictions, rule hygiene (section 13 D) | 4, 9a | Grok, against tests Codex writes first (10t, Sol medium) | Codex re-check (rule 1), then Sonnet | case 10 |
| 11a | Hub schema (the next free migration, 0042 today), replication payload projection | 3b, 4, 5 | Codex Sol, high | Opus (what leaves the Mac) | case 12's payload-field checks; inspect `test_audit1010_hub*` for the test hub first |
| 11b | Replication and recovery: cursors, idempotent upload, caught-up, offline, restore, rule numbering by hub order | 11a, 6 | Codex Astra, high (a miss here is costly) | Opus | cases 12 and 4's Mac part |
| 12 | Gateway projection and pull delivery through the gateway | 7, 11b | Codex Sol, medium | Opus (gateway exposure) | case 13; the deploy is its own step with Matt's go ([[merged-is-not-deployed]]), then a live `khipu_route` through the gateway |
| 13 | Scanner and recall's superseded mark | 2a | Grok, against tests Codex writes first (13t) | Codex re-check, then Sonnet | case 14; Matt's files are only read |
| 14 | Mocks for the five app groups, Pace including section 13's caps, advice and unknown evidence | 4, 4Pa, 5, 6 | Claude Opus | Matt approves | no app work before approval |
| 15a | App: delivery and health | 14, 16 | Codex Sol, high | Sonnet | `npm run build && tsc`, desktop tests, then the rendered result (at most 3 visual passes, screenshot as proof) |
| 15b | App: proposals and holds | 14, 5 | Codex Sol, high | Opus (owner actions) | as 15a |
| 15c | App: providers and lanes | 14, 9a | Codex Sol, medium | Sonnet | as 15a |
| 15d | App: advisory and copies | 14, 10, 13 | Codex Sol, medium | Sonnet | as 15a |
| 15e | App: pace, caps and advice | 14, 4Pb, 17a, 17c | Codex Sol, medium | Sonnet; Opus for the cap confirm (owner action) | as 15a |
| 16 | Delivery health: the budget-aware bound and red conditions, with evidence from hooks, MCP, children, replication and the gateway | 6, 7, 8a, 11b, 12 | Codex Sol, medium | Sonnet | case 15 |
| 9c | Discovery: the bounded settings parser, login evidence, subscription identity sources and the owner merge, auto-track's persistent off and new-install default | 3a, 9a | Codex Sol, high | Opus (reads inside login homes) | case 17a; sentinel tests |
| 17t | Runner telemetry: `active` and `spend` records, attribution, dedup, coverage and expiry | 9a | Codex Sol, high | Sonnet | case 17e |
| 17a | Cap kinds `pace_target`, `spend`, `concurrency`: facts, owner confirm, calculations | 4Pb, 17t | Codex Sol, high | Opus (owner caps) | case 17b's cap parts |
| 17c | `khipu route advise`, the `advice` field, `purpose`, ranking, pushes | 17a, 6 | Codex Sol, high | Opus | case 17b; advisory only |
| 17b | Church Harness runner: converted caps by owner direction; every budget consumer calls `advise`; its fail-closed policy kept | 17c | Codex Sol, medium (in church-harness) | Sonnet | that repo's oracle; Matt's go |
| 19a | Directions that never reach Khipu: owner-turn `routing_direction`, correlation with applied owner actions, the owed item | 4, 5 | Codex Sol, high | Opus (reads owner prompts) | case 17c |
| 19b | Runner-config comparison against Khipu's cap facts | 13, 17a | Codex Sol, medium | Sonnet | case 17c's runner part |

Slices 4Pb, 9a, 9c, 10, 13 and 17t can run beside 5 to 8c, and 11a beside 6.

## Matt's decisions (each with a recommendation)

Approved 2026-10-20: all 14 as recommended. Decisions 15 to 17 added the same afternoon (section 13), approved with Matt's "proceed as you recommend".

1. **Delivery failures turn doctor red** (amends routing-truth decision 10 for delivery only). Routing content
   and advisory marks stay warnings and never feed a gate. *Recommend yes.*
2. **Conversation requests become proposals.** They are live at once on the current owner revision, a newer
   proposal on the same revision replaces an older one, a proposal written against an older revision never
   takes effect, and nothing locks. *Recommend yes.*
3. **Lanes match by exact name when the provider has exactly one account,** marked and overridable; ambiguous
   names stay unmatched with candidates. *Recommend yes.*
4. **Owner writes that touch a held or paused account must say end or keep.** *Recommend yes.*
5. **Rule 12 gets a structured tier table you confirm once;** the class comes from a `task:` line, else a
   marked guess; brief text never goes to a model. *Recommend yes.*
6. **What crosses Macs and the cloud** (revised again). *Recommend:*
   - **Between your Macs:** the replication payload in section 6. That is structured facts, observations, and
     your confirmed words, with no login homes. **The hub stores your original words as written, paths
     included.**
   - **Proposal text between your Macs:** a switch, **on**. Every Mac then converges on a pending proposal
     and you can confirm it anywhere. With it off, text proposals stay on their own Mac until confirmed, and
     other Macs see a placeholder.
   - **To the cloud:** the gateway projection, with paths cut, under three switches: account state on, owner
     hold words on, and rules and note on, since Grok Bot needs them to follow them. Proposal text never goes
     to the cloud.
7. **A one-time cleanup of routing state in your CLAUDE.md files and memory notes,** by a Sonnet job with a
   backup, after slice 13 shows you what it found. *Recommend yes.*
8. **Bounds:** *Recommend these defaults.* Each is a setting.
   - 12 hours for a conversation to count as "live".
   - Red at the budget-aware bound plus 2 delivery points.
   - A 60-second cross-Mac target, red at 10 minutes.
   - A 60-second in-flight lease.
   - A 4,000-character child block.
   - Renewals at most every quarter of `routing_stale_after`.
   - 90 days for old account-id aliases, counted from the migration event.
9. **Each Mac binds accounts by label:** the same label on two Macs is one account and one quota. *Recommend
   yes;* it matches how your logins work. The alternative is a manual pairing step per Mac.
10. **Rule numbers are given in hub order and are provisional while a Mac is offline** (new). *Recommend
    yes.* The alternatives were Mac-namespaced numbers ("A-12"), which change how you refer to rules, or
    numbers only from a live hub, which blocks adding a rule offline.
11. **Parent freshness before a dispatch stays "as of its last prompt"** (new). This is a scope choice. A
    bounded read before dispatch would need a client step in every harness. *Recommend keeping it out of
    scope;* it can be its own slice later.
12. **The pace window and sampling** (revised). *Recommend:*
    - Trailing windows of 24 hours (weekly) and 60 minutes (five-hour).
    - Sampling every 30 minutes (weekly) and 10 minutes (five-hour) while in use.
    - A forecast only with at least 3 buckets in the window, valid durations covering half the window,
      `observed_pct` (valid time minus gap time, over the window) of at least 50%, and a fresh last reading.
    - A daily total only with 20 of its 24 hours valid and `observed_pct` of at least 50%.

    Grok, with its costly probe, samples at the weekly interval, so its five-hour pace is usually unknown.
13. **What a verdict measures against** (revised). *Recommend:* the plan limit, always, plus each cap you set,
    computed and carried separately. The line shows the most urgent. Caps are structured facts you confirm
    once: rule 1's Grok Bot headroom (80%) as a share cap, and the 13%-a-day line as a daily cap over a rolling 24 hours (no time
    zone). A calendar day would reset at a midnight you'd have to choose.
14. **The on-pace band** (revised). *Recommend ±15%* of the rate needed to enter outpacing or underpacing,
    and ±10% to leave it. A narrower band flips the verdict on
    ordinary noise.

15. **Auto-tracking for new installs.** *Recommend on by default.* It tracks a login a conversation actually
    used, and the owner can turn it off. Off by default leaves a new user's second account unseen.
16. **Khipu advises go or hold back** (amends section 2a's "no pick"). *Recommend yes, advisory only:*
    runners and conversations ask, and Khipu answers with its reasons. It launches nothing and blocks
    nothing (rule 7).
17. **Job runners keep only how a lane launches.** *Recommend:* caps, budgets and pacing live in Khipu, and
    a runner reads them through `advise`. Its own fail-closed choice stays in its config.

## Review responses

**Round 1.** All seven points were accepted and answered in place. Two places went differently, and round 2
accepted both:

- **Pre-dispatch freshness for the parent.** Corrected in round 2: a bounded, fail-open read before dispatch
  would not by itself make routing a gate. Leaving it out is a scope choice (section 4; decision 11), not a
  rule 7 requirement.
- **Owner words containing paths.** These replicate as written between Matt's Macs, and the hub stores the
  original words (decision 6 now says so). Only the gateway projection cuts paths.

**Round 2.** All five P1s were accepted and answered:

1. **Identity:** the canonical mapping, components and ack ownership (section 5).
2. **Rule numbering:** provisional numbers given in hub order, with uuid addressing everywhere (section 1;
   decision 10).
3. **Observation freshness:** renewals tied to the observation by witness time, never by re-reading the cache
   (section 2).
4. **Proposals:** an explicitly authorized projection of proposal text between Matt's Macs, with the
   switch-off fallback and its weaker convergence stated (section 3; decision 6).
5. **Children:** a per-harness table of what arrives before the first step, what is deferred, and what
   happens with no routing in the brief (section 4).

The non-blocking points are folded in: migration 0042, the alias window, the splits of slices 2, 3 and 8, and
gateway session uniqueness with refused foreign acks. No disagreements this round.

**Round 3.** All seven P1s and the P2 were accepted and answered:

1. **Which ack counts, by client capability,** and a child's ack never satisfies its parent (section 5).
2. **Paired-interval burn rate,** corrections versus resets, and when a forecast is allowed (section 2a, Burn
   rate).
3. **Cycle identity, clusters, skew correction and deterministic merge** (section 2a, Samples).
4. **Separate plan, share-cap and daily-cap contracts,** with their edge cases and a headline (section 2a,
   Verdicts; decision 13).
5. **Pushes on urgency bands and movement,** measured against the last acknowledged forecast (section 2a).
6. **The closed `pace_fixture_v1` schema,** the local extractor, sentinel tests, and witness time versus
   delivery time (section 12).
7. **Slice 4P is split:** Codex owns 4Pa. Grok builds only the pure calculation and formatting in 4Pb, and
   Codex's re-check is rule 1's re-check.
8. **Sampling per window** that can meet the floor (section 2a; decision 12).

The notes are folded in: forecasts are labelled "at the trailing average", and section 4's child freshness is
qualified. No disagreements this round.

**Round 4.** All five pace items were accepted and answered in section 2a:

- **a.** A canonical partition into fixed, epoch-anchored buckets, recomputed from the whole sample set so
  late readings don't depend on arrival order, and `observed_pct` as the long-gap confidence penalty.
- **b.** Gains clipped to the window with an interpolated boundary, and a coverage floor for daily totals.
- **c.** A rolling-sum forecast that includes usage aging out, or no time.
- **d.** Possible resets judged on raw readings, and the current cycle taken from the newest fresh evidence,
  so an earlier replacement reset can win.
- **e.** Band re-arming with a 25% margin, and verdict hysteresis.

No disagreements this round.

**Round 5.** Both P1s were accepted and answered in section 2a:

- **a.** Cycles are a canonical partition of reset times: a provider cycle id where there is one, otherwise a
  15-minute grid anchored to the epoch, with adjacent cells paired in one ascending pass. It is independent of
  the witness buckets.
- **b.** The rolling-cap forecast is solved exactly over the piecewise-linear segments between aging-out
  breakpoints, with the peak reported.

The daily example, case 16b and decision 12's coverage wording are corrected: Codex's example is `unknown` at
0% observed. No disagreements this round.

**Round 6.** The P1 was accepted: a cycle is a maximal run of adjacent occupied 15-minute cells. A run of more
than 4 cells is ambiguous and gives `unknown`, and the supported jitter is 1 hour (section 2a). The P2 was
accepted: the history contributes zero across an invalid interval and never interpolates a usage drop.
No disagreements this round.

**Round 7.** The grid approach kept producing edge cases, so it is replaced, on the orchestrator's ruling.
Cycles are now connected components of the sorted reset estimates under single linkage (G = 1 h). A
component spanning more than S = 1 h is ambiguous and reads `unknown`. The cell wording and the 4-cell bound
are gone. The rounds 5 and 6 entries above describe the rules they replaced. Case 16b gains Codex's six
grouping cases, each in every arrival order. The scanner's predicate now includes priority and ordering
claims. No disagreements this round.

**Round 8.** Codex called the scope ready. Its three P2s are folded in: a provider cycle id counts only when it
is unique to one reset cycle, with mixed ids handled; the headline's earliest crossing includes the daily cap;
and delivery health uses the current attachment's capability, so a switch to a pull-only provider clears with
MCP acks. No disagreements this round.

## Known limits

- A running turn learns nothing until its next delivery point. No harness offers an interrupt.
- A parent's view before a dispatch is as of its last prompt (decision 11).
- A Codex native child hears routing only through its brief or its own Khipu tool calls.
- Two gateway clients that share a token and reuse one `session_id` look the same to Khipu.
- A keyword-guessed task class can be wrong. It is always shown as a guess.
- Pace is as good as its readings. Accounts that can't be probed (Muse today) get no verdict, and a burst
  inside the trailing window shows only as an average.
- A pull-only child that reuses its parent's session id can't be told apart from its parent.
- "Owner only" stays a convention (routing-truth decision 6). Owner direction is as strong as
  `owner_direction.check`.
