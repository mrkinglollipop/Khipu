**Review of the memory evidence and retrieval proposal — 28 September 2026**

The proposal's factual claims about Khipu, Aegis and Hindsight hold, and its direction (extend Khipu incrementally, evaluator first, optional components behind switches) is sound. It is approved with the amendments recorded here, which are folded into the [scope](../plans/2026-09-27-memory-reasoning-scope.md) and the [thin plan](../plans/2026-09-27-memory-reasoning-plan.md). Nine findings were blocking: each would have produced either a regression or a feature that does nothing in production.

Status of the [handoff](../plans/2026-09-27-fable-review-handoff.md): its "review only" and "no Aegis changes" conditions were lifted by the user on 28 September. Implementation is authorized; Aegis changes are authorized in a dedicated worktree, handled separately from Khipu.

**Method.** Five independent read-only reviews (Khipu core, Khipu harness paths, the Aegis consumer, the Hindsight source, prior Khipu plans) were dispatched and their material findings rechecked by the reviewer against source. The full Khipu test suite was run at the reviewed revision. Read-only counts were taken from the production hub. No production row was modified by the review except test probe rows that the existing suite writes and deletes itself (finding B1).

| Subject | Revision reviewed | Note |
|---|---|---|
| Khipu | `9d7d7e0` | Matches remote `main`. Hub schema is at migration `0023`, PostgreSQL 19 beta 3. |
| Aegis | `9c09aec26` (remote `main`) | The proposal pinned `42dde0418`, which is 42 integration waves old. Every Khipu consumer file is byte-identical between the two, so its Aegis claims stand. |
| Hindsight | `ccfe85b` (remote `main`) | Not a released version. |

**Claims verified.** All eight Khipu core claims, all six harness claims and all five Aegis claims in the [verification record](hindsight-parity-verification-2026-09-27.md) were confirmed, with three corrections:

- Aegis does not fall back to search on an authentication failure (401/403); it returns the error. Fallback applies to an empty result and to non-authentication errors only.
- The 600-character figure is the budget of the whole rendered block. Snippets are clipped separately, to 90 characters in the hook renderer and 160 in the status payload.
- The evaluator gap is larger than stated. `khipu recall eval` exercises `hybrid_search` in hybrid mode only. The per-prompt path searches the local replica first and falls back to semantic mode; the status path is a third implementation with its own deadline logic. Neither has any evaluation coverage today.

**Blocking findings.**

| # | Finding | Evidence | Consequence | Correction |
|---|---|---|---|---|
| B1 | The documented test oracle is not hermetic on a configured machine. | Running the suite re-pointed three live hook launchers under the Khipu data directory at the checkout under test (`integrations._shim` re-points on any target mismatch; several tests call it with the real home directory). About 45 tests run against the production hub and the embedding API whenever a connection string resolves; two of them insert and delete probe episodes, which remain in the local replica until its next full refresh. The suite also writes the real query log, embed budget and reconcile state. | Every oracle run during implementation would have redirected live capture hooks to work-in-progress code and written to production. The proposal's "three oracle runs per change" multiplies it. | Make the suite hermetic by default: temporary home directory, Keychain lookups off, connection-string and key variables cleared. Live tests run only with an explicit opt-in. Added as Phase 0. |
| B2 | No supersession data exists, and only one rarely-usable way to create it. | Production hub: 40,409 decision rows, 0 superseded, 0 with a rationale. The only writer is `khipu decisions supersede OLD NEW`, a CLI that cloud harnesses cannot reach. | Validity-aware recall, the centre of the proposal, would change nothing in production. | Specify the write paths: an agent-callable MCP pair (`khipu_decisions`, `khipu_decisions_update`), the existing CLI, and conservative detection at capture that records candidates without changing authoritative state unless enabled. |
| B3 | Forgetting is incomplete. | `khipu_get` returns a forgotten episode in full (`activity.episode_detail` and `hub_snapshot.episode_detail_snapshot` do not test `deleted_at`; confirmed against a forgotten probe episode). 92 decision rows belong to forgotten episodes and are still eligible for the session-start "decisions still standing" block. Deliverable lines have the same gap. | Forgotten content can be recalled. New derived records would inherit the defect. | Filter forgotten episodes in every reader now (no migration needed), and cascade forgetting to decisions and deliverables. |
| B4 | The local replica cannot carry validity. | `hub_snapshot._TABLES` has no `decisions`, `commitments` or `deliverables`; the `topics` export omits `superseded_by` and `event_at`; there is no schema version. | The per-prompt path, the highest-traffic recall surface, reads the replica first. It would be blind to every validity change. | Extend the replica, add a schema version, and make every reader tolerate an older replica. A machine that runs both the desktop app and a checkout has two writers of different versions for one replica file; readers must degrade to "validity unknown", never fail. |
| B5 | Live hooks on the maintainer's machine run from a session worktree. | The launcher links resolve into a worktree left by an earlier program, and running MCP servers were started from it. | Removing or moving that worktree breaks capture and recall in every local harness. | Re-point the launchers at the main checkout before any cleanup. Operational, not a code change. |
| B6 | No rule on new runtime dependencies. | Khipu vendors four packages and ships them in the desktop bundle and the gateway image. A missing vendored module has already blanked part of the app once. Hindsight's time parser and default reranker need `dateparser`, `sentence-transformers` and a model download. | A dependency added for a recall feature can break the bundle, the hooks and the gateway independently. | No new runtime dependency. Time interpretation is a bounded parser on the standard library. A reranker is reached over HTTP through the already-configured model provider. |
| B7 | No rule on which model providers may see memory. | The README states that only PostgreSQL and the configured model provider see data. | A reranker or reflection step on a new provider would silently break a published privacy statement. | Reranking and reflection use the configured `models.synth` provider (cloud or local). Any other provider is a separate, explicit decision. |
| B8 | The explicit search path has no latency budget and is already slow. | One hybrid search during this review took 6.6 s, 3.0 s of it in the literal leg. | Graph candidates and reranking add to a path with no ceiling. | Record explicit-search p50/p95 in the Phase 1 baseline and gate each added leg on it. Each new leg has its own deadline and is dropped, with a disclosed reason, when it misses. |
| B9 | "The oldest permitted consumer" is undefined. | The scope requires a compatibility test against it without naming it. | The compatibility gate cannot be run. | Defined as: desktop app 0.4.4 with its bundled CLI, the gateway at the last deployed build, hooks at `9d7d7e0`, and Aegis at remote `main`. |

**Non-blocking findings.** Each is folded into the scope unless marked otherwise.

1. Two supersession mechanisms already exist and differ: `topics.superseded_by` is a slug with a search-time de-rank; `decisions.superseded_by` is a row id with exclusion in one reader. The central policy must cover both.
2. Topic `status` is free text with 17 distinct values in production. The policy needs a normalization table, not an equality test.
3. `khipu decisions list` shows current and superseded rows interleaved with no marker.
4. The prompt-recall duplicate filter keys on kind and id, has no expiry, and would suppress an item whose validity just changed.
5. `SERVER_VERSION` is a constant `0.1.0`. Only the gateway exposes a build, and only on an authenticated health check.
6. `decisions` has no uniqueness constraint (31 duplicate groups in production). A unique index cannot be added without first resolving them, so it is not added; duplicate handling stays in the application.
7. The graph has no episode nodes and no persisted episode-to-topic edge, and the one bounded traversal orders alphabetically. A candidate leg needs its own scoring and must use plain joins, not the SQL/PGQ path the code itself describes as unstable.
8. Hindsight documents a recall collapse (0.97 to 0.40 at 20 results) from boosting one retrieval arm in score space. New legs are fused in rank space with a bounded weight.
9. Aegis allowlists three Khipu tool names as read-only. A new read tool is prompted for until Aegis adds it.
10. Aegis treats an empty `prior_work` as "field unsupported" and runs a second search inside the same two-second budget, which overrides a deliberate abstention. `prior_work_meta` needs a machine-readable outcome.
11. The desktop app renders no status or validity. New metadata is safe for it and invisible in it. UI work is out of scope.
12. Gateway tokens are rate-limit labels, not scopes. A new write tool is callable by any valid token, as `khipu_forget` already is.
13. Natural-language time needs a stated time zone. The hub stores UTC; the caller's zone is unknown to the gateway.
14. Validity markers must fit the existing 600-character block. The marker is one word in the slot the row already has.
15. `CONTRIBUTING.md` documents `unittest discover`, which skips the shared fixture that resets a process-wide schema cache and reports 21 failures where `pytest` reports 11.
16. The 11 baseline failures are stale tests, recorded since 14 September. One of them, the replica's `since` filter test, uses fixed dates that have aged out and sits inside the code that time interpretation will change.
17. Recapture contamination is controlled in practice: no production episode carries injected recall text in its verbatim tier, and no Aegis transcript contains the injected heading. The remaining path, an assistant restating recalled memory in its own words, stays an acceptance case.
18. Search results are verbose (paths and neighbours on every topic row). An optional compact form is recommended, not scheduled.

**Hindsight capabilities the comparison did not cover.**

| Capability | Decision |
|---|---|
| Separate fields for answer text and cited ids in synthesis output | Adopt in the reflection phase. |
| Detection of synthesized documents whose sources were later deleted | Adopt in the briefs phase; the forget cascade supplies the signal. |
| Edit-operation refresh of a document instead of full resynthesis | Adopt in the briefs phase. |
| Rank-space fusion and multiplicative secondary signals | Adopt (finding 8). |
| Per-scope capacity limits on derived observations | Adopt in the briefs phase. |
| Token-budgeted recall output | Recommended (finding 18). |
| Stripping injected memory by wrapper tag before retaining a transcript | Already present in Khipu for every local harness. |
| Calibration text on injected memory | Already present. |
| Per-repository memory identity shared across harnesses, worktree-aware | Already present. |
| Idempotent hook write-back with retry | Already present (queue and outbox). |
| Entity resolution, bank aliases, disposition traits, admission control, webhooks | Not adopted. No measured need in a single-owner coding workflow. |

**Decisions taken in review.**

1. Every ranking change ships behind a named switch that defaults off. Additive metadata is emitted whenever the data exists.
2. Validity policy lives in one module used by all recall paths.
3. The manual CLI, the MCP tool pair and capture-time detection all record who or what made a supersession and why.
4. Migrations stay additive, nullable or defaulted, and every reader and writer works before and after they are applied.
5. `pytest`, hermetic by default, is the single oracle. The baseline is the 11 named failures; the gate is no new failure.
6. No Aegis change is required for compatibility: its parser ignores unknown keys by design and has a test for it. The Aegis adapter is an enhancement, prepared in its own worktree after the Khipu contract is final.

**Open questions for the user.**

1. Whether any provider other than the configured one may be trialled for reranking. Default: no.
2. Authorization for each outward step when the work is ready: pushing the branch, applying the migration to the hub, redeploying the gateway, releasing the desktop app.
3. Whether the stale pull request for the drag-to-Applications installer and the untracked design documents in an older worktree should be kept.

**Conclusion.** The scope is ready with the amendments above. Work begins at Phase 0.
