**Khipu memory evidence and retrieval — scope, 27 September 2026, amended 28 September**

Status: approved with amendments on 28 September 2026. The user delegated approval to the [review](../research/hindsight-plan-review-2026-09-28.md) and authorized implementation. Local implementation and testing are authorized. Each outward action — pushing a branch, applying a migration to the hub, redeploying the gateway, releasing the desktop app, installing hooks — still needs its own authorization. This document is the design source of truth for the [thin plan](2026-09-27-memory-reasoning-plan.md).

Read the [verification record](../research/hindsight-parity-verification-2026-09-27.md) first, then the [review](../research/hindsight-plan-review-2026-09-28.md). The record contains pinned revisions, corrected parity claims, the three recall paths, the harness matrix, exact Aegis dependencies, and the executed offline checks. The review contains the findings behind every amendment below. Those contracts are not duplicated here.

The intended outcome is that an agent in any supported harness retrieves the relevant approved work, distinguishes current decisions from history, and can follow evidence to its source. Improvements must preserve the existing capture ownership, cross-machine hub, intentional same-owner cross-project recall, topic pages, commitments, and deliverables.

**Architecture choice.** Extend Khipu incrementally. Retain PostgreSQL and existing source IDs, registry ownership, MCP boundary, local snapshots, and harness adapters. Centralize new validity/provenance policy while allowing different retrieval implementations to satisfy different latency budgets. Do not require expensive reflection or model reranking on every prompt.

An optional isolated Hindsight comparison is useful experimental work. Replacing the engine now is not selected: no measured gain on this workload exists, and migration would still have to preserve Khipu's operational semantics. Keeping all current behavior unchanged is a valid evaluation control, but does not address the verified gaps.

**In scope.**

- Make the test suite hermetic before anything else changes: a temporary home directory, Keychain lookups off, connection-string and key variables cleared, live tests only on explicit opt-in. `pytest` is the oracle; the baseline is the 11 named failures and the gate is no new failure.
- Restore the per-prompt recall lane to its budget before adding anything to it. In production it misses its deadline on 85% of prompts and returns nothing. The fix changes speed and failure behavior only: identical hits whenever both legs finish, keyword-only results marked as degraded when the embedding is late, and a doctor check on the lane's real outcome rate so the failure can never again be silent.
- Add a capability signal: a real server version and a `capabilities` list on MCP initialize and in the status payload, so a client can test for a feature instead of inferring it.
- Make forgetting complete: a forgotten episode is not returned by any reader, and forgetting cascades to the decisions and deliverables that episode produced.
- Give supersession a write path every harness can reach: the existing CLI, an MCP pair (`khipu_decisions` to read, `khipu_decisions_update` to supersede, restore or retract), and conservative detection at capture. Each records its source and reason.
- Extend the existing evaluator to score evidence relevance, current-truth errors, abstention, source grounding, latency, and per-operation model/token usage. Cover all three retrieval paths and their user-facing consumers.
- Extend source and decision records with traceable evidence, actual rationale when available, source type, event/recorded/effective time, revisions, and explicit supersession/conflict state. Preserve the manual CLI.
- Make retrieval and rendering honor that state. A mixed episode containing valid and superseded decisions must not be discarded wholesale. Historical questions must still retrieve historical evidence.
- Add bounded graph candidates and time interpretation, then test an optional reranker as an independently switchable component. Keep exact-string retrieval and existing search-mode semantics.
- Evolve the existing topic-page pipeline into source-backed incremental briefs. Inspect the deployed legacy producer before replacing any of its responsibilities. Optional cited reflection follows dependable evidence and remains a separate operation.
- Update Khipu-owned snapshot formats, caches, MCP/gateway schemas, hook renderers, tool descriptions, health reporting, and pack tests where required for those outcomes.

**Out of scope.** Aegis changes other than the adapter packet described under the Aegis boundary; Aegis Ledger integration; new team/tenant sharing; a replacement wiki; a new UI; Hindsight adoption in production; migrating private captures to third-party services; changing another harness's controls to configure Codex. Khipu Owed remains capture-derived obligations and never becomes authoritative Aegis task state.

**Evidence and changing-fact rules.**

1. Preserve original evidence; derive observations and summaries separately. Every derived claim must identify its sources and derivation version. Missing source spans in old captures stay unknown; no fabricated backfill.
2. Separate explicit user decisions, tool-observed outcomes, and assistant interpretations. A later paraphrase or repeated copied summary cannot silently overrule a user instruction or count as independent support.
3. Extend the existing decisions registry rather than creating competing authority. Store actual supplied rationale; inferred explanations are labeled as inference. Supersession requires matching scope and a defensible reversal; ambiguity retains competing claims.
4. Keep recorded time, source event time, and effective validity distinct. Add temporal interpretation conservatively; ambiguous dates degrade visibly rather than producing invented precision.
5. Use additive migrations and compatibility projections. Keep episode/topic IDs and old tools stable. New internal claim records must remain resolvable; do not emit unknown public result kinds until every required client can fetch and render them.
6. Redact before extraction and preserve the redaction boundary in evidence references. Retention/forgetting must invalidate derived facts, summaries, embeddings, graph links, and local caches without resurrecting removed material.
7. Detection never changes authoritative state on its own authority. Capture-time detection writes a candidate link (old decision, new decision, kind, confidence, source, reason). A candidate becomes a supersession only when a person or an agent confirms it, or when automatic application is switched on and the match is an explicit reversal in the same project. Every supersession can be restored.
8. One validity policy, in one module, serves every reader. It covers both existing mechanisms (`topics.superseded_by` and `decisions.superseded_by`) and normalizes free-text topic status through a table; an unknown status is treated as current.

**Engineering rules added in review.**

- **Switches.** Every change to ranking, candidate generation, extraction prompts or model use ships behind a named switch that defaults off (`features` in the hub config, overridable by `KHIPU_FEATURE_<NAME>`). Additive metadata is emitted whenever the underlying data exists. With every switch off, existing outputs are unchanged except for added keys.
- **Schema.** New columns are nullable or defaulted. Every reader and writer works before and after a migration is applied, using the existing column-presence checks. No unique index is added to a table that already holds duplicates.
- **Dependencies.** No new runtime dependency. Time interpretation is a bounded parser on the standard library. No model is loaded in-process.
- **Providers.** Reranking and reflection use the configured `models.synth` provider. No other provider sees memory content without a separate, explicit decision.
- **Replica.** The local replica carries decisions, topic supersession and event time, and a schema version. A reader given an older replica reports validity as unknown and otherwise behaves as before. Two writers of different versions may share one replica file on a machine that runs both the desktop app and a checkout.
- **Oldest permitted consumer.** Desktop app 0.4.4 with its bundled CLI; the gateway at its last deployed build; hooks at `9d7d7e0`; Aegis at its remote `main`.

**Retrieval and multi-harness contract.**

The explicit search, local snapshot-first prompt search, and bounded gateway search are separate acceptance targets. A change only to `embed.hybrid_search` is incomplete. Share state/policy helpers and compare outcomes across the three paths; identical algorithms or identical top-k under different budgets are not required. The session-start slice, `khipu_get`, the decisions and deliverables readers, and the stale-replica search payload are recall surfaces too and are held to the same validity and forgetting rules.

The explicit search path has no deadline today. Its p50/p95 is recorded in the Phase 1 baseline, and each added leg carries its own deadline and is dropped, with the reason disclosed in the payload, when it misses. A validity marker uses the status slot a row already has and must fit the existing 600-character block. Natural-language time is interpreted in UTC unless the caller supplies a zone, and the interpretation is returned with the results. New retrieval legs are fused in rank space with a bounded weight.

Add metadata without removing or retyping current keys. Preserve `prior_work` array/empty/null meanings and the existing text fallback. Add a version/capability signal before depending on new metadata; older clients receive a truthful compatible projection. Distinguish no relevant evidence from timeout, stale snapshot, unavailable provider, and unsupported feature: `prior_work_meta` gains an `outcome` field with a closed set of values (`match`, `no_match`, `gated`, `dedup`, `timeout`, `error`), so a client can honor an abstention instead of overriding it with a second search.

Preserve the documented budgets and cache placement in the verification record. Deeper graph traversal, reranking, consolidation, and reflection have explicit budgets and safe fallbacks. Include all extraction, refresh, embedding, reranking, and reflection costs; fast query latency must not hide an unbounded background bill.

Source revisions must propagate to replicas and invalidate derived briefs. Existing 24-hour snapshot eligibility is insufficient proof of current-fact freshness. For connected supported paths, propose a 60-second capture/correction-to-query target and measure it; old/offline replicas disclose their last successful source generation and do not assert that old facts are current. Capture queuing and bounded shutdown behavior remain intact.

Dedup must distinguish unchanged source IDs from changed revisions. Updating a decision under an existing ID must be capable of reaching a session that recently saw its old version. The dedup key gains a revision suffix only when a row's validity differs from the default, so keys for unchanged rows stay byte-identical across the upgrade. Khipu-owned hooks are updated here; the corresponding Aegis consumer work is in the adapter packet.

Capture acceptance includes native events and transcript formats for each harness, root/child lineage, retry idempotency, queue/outbox recovery, abrupt end/compaction, and cross-machine replay. Capture remains single-writer per source event; a queue acknowledgment is not a hub-persistence claim. Injected recalled material must not become fresh independent evidence when recaptured.

Cursor and cloud clients cannot be declared equivalent to automatic prompt hooks. Their gate includes a real session that actually calls the relevant MCP tool. Configuration presence, tool discovery, and `/healthz` are necessary observations but not proof of capture or recall.

**Local evaluation and acceptance.**

Use a versioned synthetic corpus plus private golden cases retained outside the public repository. Freeze current behavior, model versions, inputs, and budgets as a control. Evaluate capture-to-answer separately from retrieval-only so extraction differences are visible.

The mandatory scenarios are: prior approved work; explicit reversal; historical/as-of question; same wording in different projects; indirect graph relationship; exact command/error/path; no relevant memory; ambiguous/conflicting evidence; mixed current/superseded episode; stale/offline snapshot and reconnection; changed revision under an existing ID; deferred commitment; existing artifact; duplicate/cross-harness recapture; source deletion; quota failure; malformed extraction; and crash/retry during consolidation.

Release-blocking correctness cases have zero unauthorized-scope returns, zero source-ID misattributions, zero current-truth assertions from known superseded-only evidence, and zero loss of original evidence in recovery fixtures. Historical retrieval remains available. Exact-string and existing golden positives must not regress. Proposed quality components must show repeatable benefit on their target cases under frozen model/context budgets; small score noise does not justify enabling them.

Record p50/p95 and timeout/miss rates per path, online/offline state, model, cache condition, and corpus size. Keep existing hard deadlines; do not raise them to manufacture a quality win. Freeze practical soft budgets from the Phase 1 baseline before implementation. Repeated oracle attempts remain capped at three per change, with one reserved for root acceptance.

Each implementation session names an exact direct local oracle based on the files changed. Use mocked/in-memory contracts first, then an isolated PostgreSQL/snapshot fixture, then genuine harness sessions. Live fixture writes require an isolated namespace/database and source identity; never use the production memory corpus as scratch data. Existing manual `khipu recall eval` is extended, not replaced by GitHub Actions.

The synthetic corpus is a generated SQLite replica, so the scenario suite runs the local recall path end to end with no database and no network. Hub-path ranking is covered by query-shape tests against a fake cursor and by the private golden set, which is read-only against the hub and runs only on explicit opt-in.

**Aegis boundary and explicit dependency.**

No Aegis change is needed for compatibility. Its parser reads named keys from the response and ignores the rest by design, with a test that feeds it unknown keys; `kind` and `status` are plain strings, and it already renders a row's status into the recalled line. Preserve its current status/search shapes so existing bounded prior-work recall benefits from server-side improvements unchanged.

The adapter packet is an enhancement, authorized on 28 September, and is built in a dedicated Aegis worktree on a branch from Aegis's remote `main`, under that repository's own rules (signed-off commits, its local oracle, its integration waves). It contains three changes: honor `prior_work_meta.outcome` so a `no_match` no longer triggers the fallback search; read the capability signal; carry validity and revision through its own duplicate filter. It keeps `semantic=true` as the default for initial recall: hybrid mode adds a literal leg measured in seconds and is not safe inside Aegis's recall budget until Phase 1 says otherwise. Adding a new Khipu read tool to Aegis's read-only allowlist is a permission decision and is reported to the user, not made in the packet. Native source fetch or reflect exposure is another conditional Aegis feature, not an implicit side effect of a new Khipu tool.

The Khipu phase supplies example responses and consumer contract fixtures in this repository. Never claim full Aegis acceptance from a mocked fixture. Its standalone behavior, local memory tools, explicit task authority, cache-stable prompt prefix, and current installation are preserved.

**Rollout and recovery.**

Keep each new quality component disabled until its gate passes; retain the old retrieval path as a control and rollback option. Migration rehearsal includes backup/restore and an old-client/new-server compatibility check. Shadow evaluation must not write inferred decisions into authoritative state. Any replacement of the legacy consolidation producer needs one-writer cutover, replay identity, and rollback evidence.

Local implementation/testing and actual distribution are separate. Before any later gateway deployment, desktop release, hook installation, or cloud configuration change, recheck this project's documented procedure and obtain authorization for that action. Final acceptance is per harness and deployed revision. The oldest permitted consumer remains a compatibility test, and a held external Aegis dependency remains visibly incomplete.
