# Fact provenance: who owns each piece of wording (scope, 2026-10-23)

Status: approved by Matt. Phases 1 to 4 are built on branch `feat/fact-provenance` (not merged; main merged in after #356). It replaces the fact-support attempt (`fix/audit-astra2`, commits `5e9b1f4a..389199c0`, migrations 0045/0046), which failed three reviews.

Evidence read for this doc: main at `629d49bb`; the original audit (`jobs/2026-10-23/audit-astra/rep/report.md`, findings P2 "forgetting one source loses facts", P2 "nightly closes using forgotten evidence", P2 "queued owner writes follow a replacement hub", P2 "incremental pull revives scrubbed text"); the attempt; the reviews `jobs/2026-10-23/astra-mig/report.md` (7 findings), `jobs/2026-10-23/review-fix-astra-mig/report.md` (3 P2, 1 P3), `jobs/2026-10-23/astra-mig2/report.md` (2 P1, 2 P2, 1 P3). No database, hub, Keychain or real HOME was touched.

## 1. The problem in one paragraph

Capture deduplicates. A second episode that restates an open commitment does not get its own row: it overwrites `commitments.latest_text`, unions its PR numbers into `prs` and `pr_check.refs`, and bumps `seen_count` (`commitments.py:1158-1191`). A repeated decision or deliverable is skipped outright (`decisions.py:323`, `deliverables.py:66`), and so is an owner supersession whose words repeat one already on file (`decisions.create_decision`). Nothing records who supplied which value. Forget then has to act on the fact row by its anchor episode alone (`forget.py:213-237`): it closes, retracts or deletes facts that a kept episode also supports, and it never touches wording a non-anchor episode wrote. The attempt added a support table but kept the question "which episode supplied this value?" open at forget and cleanup time, answering it by re-reading raw captures with a second set of matching rules. Those rules disagreed with capture's (kind, mutable owner), so forgotten wording stayed readable in some schedules and live wording was deleted in others (third review, P1 #2). Every later fix patched one disagreement and exposed another.

## 2. Invariants (each is a test)

I1. **Forgotten wording is never readable.** After `forget_episode(E)` commits, no reader returns a value whose only claimants (section 3) are forgotten episodes. Readers: hub `list_owed`, `list_decisions`, `recent_deliverables`, search, get, briefs, the MCP tools, gateway mode; and each replica's readers once its next `pull_changes()` returns ok. Test: for every claim value `v` held by E, assert `v` is absent from every reader's output unless a live episode or the owner also claims exactly `v`.

I2. **Live supported wording is never deleted.** If a live episode or the owner holds a claim on fact F, then no forget, backfill or cleanup retires F, and each projected field of F is one of its live claims' values (by the projection rules in 3.2). Test: generated schedules over captures A/B/C with mixed kinds and owners, owner reassign, an old-code writer (refused, 5.2), and forgets of every subset. Assert after each commit.

I3. **Forget never overwrites owner edits.** Owner fields (3.3) are byte-identical before and after any forget, backfill or cleanup. The one exception is the retirement status of a fact left with no live or owner claim, and even then the owner fields stay on the row. Owner claims are removed only by the owner. Test: snapshot owner fields, forget, compare.

I4. **No capture silently loses a fact.** Every capture job ends in exactly one of three states: committed, with a claim for every fact item it derived; still queued in the outbox, not buried, with a named deferral reason; or committed with a `fact_gaps` row that doctor reports and the nightly re-runs. Test: inject `DeadlockDetected`, `SerializationFailure`, `LockNotAvailable`, `QueryCanceled` (statement timeout), a gate refusal and a plain bug into each fact step. Assert one of the three states each time, and assert the outbox job was never acknowledged without one.

I5. **No owner write is applied twice or to the wrong hub.** A queued owner write applies at most once, and only to the hub whose identity it was bound to when it was queued. An entry with no binding is never applied until the owner binds it explicitly. This is a separate change (section 7). It is listed here because the same review cycle broke it.

I6. **Wording changes only through the projector** (the mechanism behind I1 and I2). In any transaction that changes a fact's wording columns, the projector must have recorded that fact in `fact_changes` in the same transaction, or the commit fails. This holds whatever code version is writing. Test: a direct SQL `UPDATE commitments SET latest_text = ...`, an old-code dedup and an old-code forget each fail at COMMIT.

## 3. The model: claims, one projector

**Chosen: every stored wording value has exactly one claimant, recorded when it is written.** The claimant is either a capture episode or the owner, and the record is a row in `fact_claims`. A fact's visible wording is a pure function of its live claims, computed by one function, `khipu/provenance.py: project(cur, kind, fact_id)`. Capture dedup, forget, legacy backfill and any future cleanup change wording only by adding or deleting claims and then calling `project()`. No code path reads raw captures to work out after the fact who said what. Dedup keeps its current rules (exact hash, paraphrase match, owner separation): they decide only *which fact* a new claim attaches to.

**Rejected: a support set beside the fact row, re-derived from raw on forget and cleanup (the attempt).** It needs two equivalence rules, the writer's dedup and the forget/cleanup witness classifier, and they must agree forever. They already disagreed on kind, on an owner that `reassign` can change, and on support rows that existed but were ignored. The two rules lived in different modules, so the drift was invisible until a review hit it.

**Also rejected: no cross-episode dedup in storage** (one row per occurrence, grouped at read time). Forget would become a plain cascade, but every reader, the close rules, owner edits (which would target a group, not a row), replica schema and paging would all change. That is a much bigger change for the same invariants.

### 3.1 Tables (one migration, 0047)

- `fact_claims(kind, fact_id, field, value, claimant, episode_id NULL, xid xid8 DEFAULT pg_current_xact_id(), created_at)`. `claimant` is one of `episode`, `owner` or `legacy_unproven`. PostgreSQL refuses a primary key on expressions, so the claim's key is a unique index on `(kind, fact_id, field, md5(value), coalesce(episode_id, 0), claimant)` (the `ON CONFLICT` target), with a surrogate `id` primary key; a replayed capture writes nothing new. `field` is one of: commitment `text`, `restatement`, `pr`, `pr_ref`; decision `text`, `rationale`, `evidence`; deliverable `title`, `support`.
- `fact_changes(seq bigserial, xid xid8 DEFAULT pg_current_xact_id(), kind, fact_id, via, at)`. `project()` writes `via = 'project'` rows, the only ones the commit gate (I6) accepts. A plain trigger on `decisions` and `deliverables` writes a `via = 'row'` row for any other insert, update or delete (owner supersede, retract, confirm), so the replica stream (section 6) sees lifecycle changes too. `at` drives the 30-day prune; `fact_changes_floor(pruned_through xid8)` records how far it has removed rows.
- `fact_gaps(episode_id, step, error, at, resolved_at)`. Used for I4.
- `provenance_projects(project, backfilled_at)`. Legacy witness progress (section 4).
- Triggers, all `DEFERRABLE INITIALLY DEFERRED` constraint triggers with `WHEN (OLD.col IS DISTINCT FROM NEW.col)`:
  - Fact-table gate (I6). Fires on INSERT into `commitments`, `decisions` or `deliverables`, and on UPDATE of a wording or anchor column: commitments `text`, `latest_text`, `prs`, `pr_check->'refs'`, `opened_episode`; decisions `text`, `rationale`, `evidence`, `episode_id`; deliverables `title`, `episode_id`. It requires a `fact_changes` row for `(kind, NEW.id)` with `xid = pg_current_xact_id()`.
  - Forget gate. Fires on UPDATE of `episodes.deleted_at`. It requires that no `fact_claims` row names that episode.
  - Claim gate. An immediate (not deferred) `BEFORE INSERT` trigger on `fact_claims`: an episode claim takes `FOR SHARE` on the episode row, before any fact lock (the lock order in 3.4), and refuses a deleted or missing one. This serialises it with forget's `FOR UPDATE` (`forget.py:116`). It refuses with `check_violation`, not `08K01`: an old writer never inserts claims, so this refusal is always a new writer's bug and must not read as "upgrade it" or as hub-away.
  - The fact-table and forget gates refuse with SQLSTATE `08K01` and the message "khipu: this writer predates fact provenance; upgrade it". The reason is in 5.2.

### 3.2 Projection rules (`project()`, the only writer of these columns)

| Fact | Field | Projected value |
|---|---|---|
| commitment | `opened_episode` | the earliest live `text` or `restatement` claimant. A forgotten anchor is kept only when the fact retires |
| commitment | `text` | the anchor's own claim value |
| commitment | `latest_text` | the newest live `restatement` value that differs from `text`, else NULL |
| commitment | `prs`, `pr_check.refs` | the union of live `pr` / `pr_ref` claims and owner claims; `prs` sorted, `refs` in the order each was first claimed (the order the pre-provenance writer appended them). A `legacy_unproven` PR value that no live or owner claim also holds sets `pr_check.needs_owner`, and close rules then never auto-close the fact (the owner closes it; phase 3, see 8) |
| decision | `episode_id`, `text` | the earliest live claimant and its own words. An owner claim with no live episode leaves `episode_id` NULL |
| decision | `rationale`, `evidence` | the anchor's claim, if it has one, else NULL |
| deliverable | `episode_id`, `title` | the earliest live claimant and its own title |
| any | retirement | no live and no owner claim: close (`forgotten`), retract (`forgotten`) or delete exactly as `forget.py:213-237` does today, with the anchor left on the forgotten episode so the existing reader fences keep hiding it |

When `text` or `latest_text` changes, `project()` also deletes the fact's `memory_embeddings` row (the nightly re-embeds it). `content_hash` is identity, not wording, and is never recomputed. This removes the owner-separated hash collision class (first review #6).

### 3.3 Owner fields (never written by forget, backfill or cleanup)

Commitments: `status` (except retirement), `owner`, `owner_label`, the reassign lineage columns, `due_after`, snooze, `linked_ids`, `reopened_rule_keys`, the `pr_check` keys other than `refs` and `needs_owner`. Decisions: supersede and retract state, `decision_links`, confirm and reject. Owner-authored wording is an `owner` claim. That covers `create_decision`, including when it deduplicates into an existing row, and any future owner text edit. On main today no owner API edits commitment, decision or deliverable wording.

### 3.4 Locks and errors

- There is no global mutex. The attempt's advisory lock caused the second review's deadlock class and the third review's statement-timeout loss.
- Lock order for every writer: episode row(s), then fact rows in `(kind, id)` order.
- Forget: `FOR UPDATE` on E, select E's claimed facts, lock them in order, delete E's claims, `project()` each, then the existing tombstone steps.
- Capture dedup: lock the target with `FOR UPDATE ... WHERE status = 'open'` and re-check. If the target has retired in the meantime, take the insert path.
- These four errors in a fact step make the whole capture transaction roll back as `FactStepDeferred`, which the outbox retries and never buries (like `ClaimDeferred`, `outbox.py:495`): `DeadlockDetected`, `SerializationFailure`, `LockNotAvailable` and `QueryCanceled`. So does SQLSTATE `08K01`, after a readiness refresh.
- Any other fact-step error keeps today's savepoint rollback (`capture.py:1207-1270`, merge path `:669-750`), and also writes a `fact_gaps` row in the same transaction.

## 4. Legacy data (unknown provenance: never guessed)

Migration 0047 attributes a value only when the code proves who wrote it.

- **Proven by the writer inventory.** Each of these columns has exactly one writer, the insert by the anchor episode: commitment `text`, `prs` equal to `parse_prs(text)`, decision `text`/`rationale`/`evidence`, deliverable `title`. A commitment whose `prs` is NULL (never parsed, from before 0034) keeps NULL through the migration (the projection keeps NULL while the fact has no `pr` claim), unless its text contains no `pr` or `pull` at all, which no reading can turn into a PR, and is settled as `{}`. Its one writer, `close_rules.backfill_prs`, then records `parse_prs(text)` (the exact parser, not the stricter SQL reading) as claims of the text's claimant and projects; a capture that dedups into such a row claims those numbers first (built in phase 3; phase 2 had the migration prove them with the SQL reading, which can miss a number). Each gets an `episode` claim for its anchor. A decision that is the `new_id` of an owner-made supersede link gets an `owner` claim. `tests/test_episode_write_gate_inventory.py` (or a sibling) pins the single-writer list, so a new writer breaks the build.
- **Unproven.** Commitment `latest_text`, any `prs` or `pr_check.refs` beyond what `text` yields, and any value the inventory cannot explain become `legacy_unproven` claims. `project()` never shows them, so the migration's own projection sets `latest_text` to NULL and turns on `needs_owner` where it applies. The values stay in the hub-only claims table, on the same terms as a forgotten episode's raw, and are never sent to a replica.
- **Witness backfill** (`khipu provenance backfill`, nightly until `provenance_projects` is complete). For each project, it streams live episodes (keyset pages by id, so the nightly can commit between batches) selecting only the open loops, decisions and deliverables of `raw` and of `raw->'merged_from'[*].payload`, in batches of 500. For each item it computes the value the writer would have stored, by calling the writer's own value function (`_normalize_open_loop(...)["text"]`; `_redacted_value(str.strip())` with `normalize_text` for decisions; the deliverable dedup key). If that value exactly equals a stored or unproven value on a fact in the same scope (the episode's `project`; facts retired as forgotten excluded), and that episode does not already hold that value on a fact (it opened its own, such as an owner-separated twin, or a capture after 0047 claimed it), the episode gets an `episode` claim and the fact is re-projected. Commitment PR numbers and refs (`pr_requirement_labels` with the project's repository, so a plain "PR #41" ref is proven too) are claimed when the episode's words name a value already on the fact; this also clears `needs_owner`. Decisions match on the stored text byte for byte, not on `normalize_text`. Kind and current owner are never compared. Memory is bounded by the batch, not the project. This answers the third review's #5 and the second review's P3.
- **Before a project is backfilled,** forget runs that project's backfill first. It is the same function, streamed, under the same locks, so there is only one code path. Forget locks the episode, then its facts in `(kind, id)` order, then backfills (whose claim gate takes `FOR SHARE` on other episodes): two forgets that both run one project's first backfill can deadlock, and the refused one changed nothing. `forget_everywhere` retries a deadlock or serialization failure itself, 4 attempts in all with a short growing pause (`forget.FORGET_DEADLOCK_ATTEMPTS`, phase 4). The nightly runs the backfill inside its close-rules step, before `backfill_prs` and the rules.
- **Accepted limit.** A legacy restater whose words were later overwritten left no stored value, so it cannot be proven. If every provable claimant of such a fact is forgotten, the fact retires as it does on main today. Doctor counts these: retired legacy facts with `seen_count` greater than their proven claimants. No fuzzy matching is used to rescue them.

Why exact equality is safe for I1: if a live episode's capture produces exactly `v`, then `v` is not wording that only forgotten episodes hold.

## 5. Rollout on a live hub (enforced in code, not by deploy order)

### 5.1 Before the migration: new code on the old schema

New writers check for `fact_claims` with `to_regclass` at the start of each transaction and cache only a positive answer. That fixes the first review's #3. Without the table they behave exactly as main does. They always write claims and call `project()` when the table exists. A writer whose check went stale is caught by the gate (I6) at COMMIT, retried as `FactStepDeferred`, and its next attempt sees the table. That fixes the first review's #1, and the database enforces it, not code discipline.

### 5.2 Old-code writers after the migration

This covers other Macs' live slots, the gateway at `/opt/alzy/bin`, and anything still on main.

- **Old captures.** Every wording write fails the deferred gate at COMMIT, and that rolls back the whole transaction, savepoints included. The old fail-open steps cannot commit an episode without its claims. The error is class `08`, which old `hub_errors.unreachable_error` treats as "hub away" (`hub_errors.py:52`). The old outbox stops, keeps the job queued and never buries it (`outbox.py:514`). Captures land after that Mac upgrades. Phase 1 verifies this mapping against the pinned psycopg (stop condition).
- **Old forget.** It fails the forget gate, so nothing is tombstoned and the caller sees an error. Its local fence is published before the hub step (`forget.py:136`), so a refused old forget leaves a local tombstone with no hub tombstone. That is an existing split, and the same as an unreachable hub. The new forget heals it on retry.
- **Old owner writes.** Lifecycle writes (close, reopen, snooze, reassign, retract) touch no wording and keep working. An old supersede inserts a decision, fails the gate, and is reported to the owner as refused or waiting, never as applied.

This fixes the second review's P2-3 and the third review's #2 old-binary cases by refusal, not by later repair.

### 5.3 The migration itself (0047, one file, one transaction)

- Number 0047. The runner refuses to run it if `schema_migrations` records 0045 or 0046 (a disposable hub from the attempt).
- It takes `ACCESS EXCLUSIVE` on `episodes`, `commitments`, `decisions` and `deliverables`, in writer lock order, before anything else. Writers in flight finish first, so the backfill sees them. Writers that arrive later wait, then meet the gate.
- In the same transaction it creates the tables, then the proven and unproven claims, then the initial projection with `fact_changes` rows, then the triggers. Because it is a single file there is no intermediate state like the attempt's 0045-only window (first review #7).
- Exhausting the lock retries (`migrate.py:333`) rolls the whole file back. Idle-in-transaction readers are the known cause (third review, 0046 section).
- Run it in a quiet window, after `khipu promote` on this Mac, the other Macs and the gateway (`merged-is-not-deployed`).
- **Rollback.** Code rollback after 0047 means every writer is refused. Undoing it is migration 0048, which drops the four triggers and keeps the tables. It is an owner-directed action through the agent, per `2026-10-16-owner-direction.md`, and needs no terminal.

## 6. Replica change stream (out-of-order commits)

Commitments already arrive as the whole table on every pull (`hub_snapshot.py:1705`) and stay that way. Decisions and deliverables move from the timestamp watermark (`sync_decision_changes`, `hub_snapshot.py:1115`) to `fact_changes`:

- The cursor is the hub snapshot's `pg_snapshot_xmin(pg_current_snapshot())`, read inside the pull's existing REPEATABLE READ snapshot. It is stored in the replica's own `snapshot_meta` (`fact_stream_xid`), written in the same SQLite transaction as the rows, so a dump (which records its own snapshot's xmin) brings its cursor with it. A read with a lower xmin than the one held is an earlier snapshot and is skipped whole.
- Each pull reads `fact_changes WHERE xid >= cursor` that are visible in its snapshot, re-fetches those facts' current rows by id (an absent row means delete locally), applies them idempotently, and stores the new xmin.
- Every transaction below the xmin has finished, so a slow transaction with an earlier stamp can never fall behind the cursor. No time bound or overlap guess is needed (first review #4).
- Re-reads are bounded by the transactions in flight.
- Rows older than 30 days are pruned by the nightly (inside the close-rules step, after the backfill). A replica whose cursor is at or below `fact_changes_floor.pruned_through` asks for a full dump. So does one with no cursor at all: a replica dumped before 0047 (the first review's #7 analog; 0047 being one file, the 0045-only state itself cannot exist).
- On a hub without 0047 the pull and the capture drain keep the timestamp watermark (`sync_decision_changes`, section 5.1). On a 0047 hub the watermark is retired: `sync_decision_changes` reads the stream instead (`sync_fact_changes`).
- Tombstones and fact changes come from the same snapshot, so a replica never holds E's tombstone without the matching re-projection.
- `forget_everywhere` applies the changed decision and deliverable ids it gets back to the local replica straight away, after the local tombstone and without moving the cursor, so this Mac does not wait for its next pull. Commitments keep their whole-table pull and freshness rule: one the forget moved to another episode is hidden locally until that pull, never shown with forgotten wording.
- Derivative imports check the durable erasure registry, so a scrubbed source's commitments and decisions are not recopied. This is the original audit's replica P2, landed on main by #356 (`_rejected_episode_ids`); the stream's decision and deliverable writes use the same fence, and the stream treats a row anchored to a forgotten hub episode as gone.

## 7. Owner-write destination binding: a separate change

Owner-write destination binding shares no tables, locks or code with fact provenance, so it ships separately. Coupling the two on one branch produced the second review's P2-2 and the third review's #1 and #4, which were judged together with unrelated fact defects. The rules for that change:

- The hub gets a random `hub_identity` uuid (its own migration). An endpoint fingerprint cannot tell a recreated database at the same address.
- New entries bind that uuid, and replay checks it on the connection that applies the write.
- An entry with no binding is never applied. It moves out of the ordered queue into `unbound/`, so it does not hold up other writes. A later write to the same record id waits behind it. The app lists it with two owner actions: "send to this hub" (an explicit bind; the receipt is checked first) and "discard".
- Clear takes the entry claim before reading, then re-reads. An entry already applied or missing is reported as such.

## 8. Phases (one session each; phases 1-4 on one branch, merged once after phase 4)

The oracle everywhere is hermetic pytest from `packages/cli`: temp HOME, `KHIPU_KEYCHAIN=0`, never `TMPDIR=/tmp`, hermetic PG from `tests/hermetic_pg.py`. Each change gets at most 3 runs, then stop and report. Each session ends with `git diff --name-only` checked against its file list.

1. **Schema, gate, projector.** Inspect `migrate.py`, `0030_forgotten_sources.sql` and `db.has_columns` before writing. Build 0047 (tables, legacy proven and unproven claims, initial projection, triggers) and `provenance.py` (`record_claim`, `project`).
   - Oracle: new `tests/test_provenance_gate.py`, covering I6 (direct SQL, a simulated old dedup and an old forget each refused at COMMIT), the legacy classification table in section 4, and the 0045/0046 refusal.
   - Stop if psycopg raises `08K01` as anything but plain `OperationalError`, or if main's `unreachable_error` returns False for it. Report back, because 5.2 then needs a different signal.
   - Read-only on capture, forget and replica code in this phase.
2. **Capture through claims.** Wire `commitments.open_from_episode`, `decisions.insert_decisions_from_episode`, `decisions.create_decision` and `deliverables.insert_deliverables_from_episode` to `record_claim` and `project()`. In `capture.py`, add the deferral classes and `fact_gaps` on both the insert and merge paths, and positive-only readiness.
   - Oracle: the existing `test_commitments*`, `test_decisions*`, `test_capture_fail_open.py` and dedup suites unchanged, plus the I4 injection test and the I2 schedules that involve only capture.
   - Stop if an existing assertion outside the fail-open tests has to change.
   - Built (phase 2 report, `jobs/2026-10-23/prov-phase2/report.md`): `FactStepDeferred` covers all five capture steps on both paths (commitments, decisions, reversal detection, close rules, deliverables); the outbox keeps such a job past its attempts with `fact_deferred` and a `deferral_reason`; a 08K01 at COMMIT is raised as `FactStepDeferred` by `write_pg`. Tests seeding facts with raw SQL call `tests/fact_seed.py: adopt()` in the same transaction. `test_forget_conflict_reader.py` stays red until phase 3 (the old forget cascade meets the forget gate).
3. **Forget, backfill, close rules.** Change the `forget.py` cascade to delete claims and run `project()`. Add `khipu provenance backfill` (streamed) and its nightly hook. Exclude `deleted_at` from `close_rules._episode_states` (original audit P2). `close_rules._pr_rule` rebuilds `pr_check` and drops `needs_owner` (`close_rules.py:666-669`), and no close rule honours it yet: make `_pr_rule` keep it and skip auto-close for a fact that carries it (`needs_owner` is deliberately not gated, so the nightly close transaction is not refused meanwhile). `close_rules.backfill_prs` finds nothing after 0047 (the migration proves NULL `prs` itself), so `test_owed_close_rules.py::test_a_pre_migration_hub_still_answers`, which expects it to fill one row after a full migrate, needs its expectation moved to the migration.
   - Built (phase 3 report, `jobs/2026-10-23/prov-phase3/report.md`): the forget cascade, `khipu provenance backfill` and its nightly hook (inside the close-rules step, so the nightly's step list is unchanged), `_episode_states` excludes forgotten captures, and `needs_owner` is kept by `_pr_rule` and blocks both the PR and the decision rule. `test_a_pre_migration_hub_still_answers` passes unchanged: NULL `prs` now stays NULL through 0047 and `backfill_prs` fills it through claims (section 4). Replica stream reproductions (first review #4, #7) are phase 4's.
   - Oracle: I1, I2 and I3 generated schedules. Port every reproduction from the three reviews as tests: first review #1-#7; second review P2-1 and P2-3; third review #2 (all three schedules), #3 and #5, the last with a 100k-episode peak-RSS bound. The scenarios are in the reports; the `/tmp` probe scripts may be gone.
   - Stop if any scenario needs fuzzy matching to pass.
4. **Replica stream.** Change `hub_snapshot.py` so decisions and deliverables pull from `fact_changes`, retire the watermark, add the same-snapshot tombstones, the `forget_everywhere` local apply and the erasure-registry fence on derivative imports.
   - Oracle: an inverted-commit-order test (first review #4 shape, 2,000 decisions), the scrub-then-pull test, the I1 replica variant, then one full hermetic suite run. The named baseline failures are the only failures allowed.
   - Built (phase 4 report, `jobs/2026-10-23/prov-phase4/report.md`): 0047 gains `fact_changes.via`/`at`, the row trigger and `fact_changes_floor`; `hub_snapshot` reads the stream in the pull's snapshot and in dumps, `sync_fact_changes` and `apply_fact_rows`; `provenance.prune_changes` in the nightly; forget's deadlock retry. Tests in `tests/test_provenance_replica.py`.
   - Stop if the full suite shows new failures after 3 runs.
5. **Rollout.** Add a doctor check for 0047 state, `fact_gaps` and the section-4 limit count. Then a cross-family review of the branch (Codex Astra: state machines and privacy) before merge.
   - Deploy: promote on this Mac, the other Macs and the gateway, then run 0047 in a quiet window. Matt's go is needed for the hub migration because it is an outward action.
   - Stop if any writer is not yet promoted, or if the review finds a P1.
6. **Owner-write binding (separate branch, any time).** Section 7.
   - Oracle: the two-hub probes from the third review #1 (both prior-commit states), the clear race (#4), and the old wedge case (second review P2-2).

## 9. Out of scope

- Gateway sharing revocation (original audit P1) and gateway path removal (P2). They are unrelated to provenance and were reverted with #348. They need their own re-land.
- Exact `seen_count` and `last_seen_at` after a forget.
- Purging forgotten raw or unproven claims from the hub. Unforget and restore.
- An owner UI to review or restore unproven legacy wording. Doctor reports counts only.
- Replacing the commitments full-table pull. The `content_hash` of forgotten text, which stays as identity, not wording.
- The legacy JSONL and outbox scrub logic in `forget.py`, which is unchanged.
- Briefs beyond today's stale-on-forgotten-source rule.
- PostgreSQL 19 `PROPERTY GRAPH` statements.
