**Khipu memory evidence and retrieval — proposed scope, 27 September 2026**

Status: proposed for approval. The user requested verified findings and a plan. This document authorizes no implementation, live migration, release, installation, or Aegis change. It is the design source of truth for the [thin plan](2026-09-27-memory-reasoning-plan.md).

Read the [verification record](../research/hindsight-parity-verification-2026-09-27.md) first. It contains pinned revisions, corrected parity claims, the three recall paths, the harness matrix, exact Aegis dependencies, and the executed offline checks. Those contracts are not duplicated here.

The intended outcome is that an agent in any supported harness retrieves the relevant approved work, distinguishes current decisions from history, and can follow evidence to its source. Improvements must preserve the existing capture ownership, cross-machine hub, intentional same-owner cross-project recall, topic pages, commitments, and deliverables.

**Architecture choice.** Extend Khipu incrementally. Retain PostgreSQL and existing source IDs, registry ownership, MCP boundary, local snapshots, and harness adapters. Centralize new validity/provenance policy while allowing different retrieval implementations to satisfy different latency budgets. Do not require expensive reflection or model reranking on every prompt.

An optional isolated Hindsight comparison is useful experimental work. Replacing the engine now is not selected: no measured gain on this workload exists, and migration would still have to preserve Khipu's operational semantics. Keeping all current behavior unchanged is a valid evaluation control, but does not address the verified gaps.

**In scope.**

- Extend the existing evaluator to score evidence relevance, current-truth errors, abstention, source grounding, latency, and per-operation model/token usage. Cover all three retrieval paths and their user-facing consumers.
- Extend source and decision records with traceable evidence, actual rationale when available, source type, event/recorded/effective time, revisions, and explicit supersession/conflict state. Preserve the manual CLI.
- Make retrieval and rendering honor that state. A mixed episode containing valid and superseded decisions must not be discarded wholesale. Historical questions must still retrieve historical evidence.
- Add bounded graph candidates and time interpretation, then test an optional reranker as an independently switchable component. Keep exact-string retrieval and existing search-mode semantics.
- Evolve the existing topic-page pipeline into source-backed incremental briefs. Inspect the deployed legacy producer before replacing any of its responsibilities. Optional cited reflection follows dependable evidence and remains a separate operation.
- Update Khipu-owned snapshot formats, caches, MCP/gateway schemas, hook renderers, tool descriptions, health reporting, and pack tests where required for those outcomes.

**Out of scope.** Aegis repository/config/app changes; Aegis Ledger integration; new team/tenant sharing; a replacement wiki; a new UI; Hindsight adoption in production; migrating private captures to third-party services; changing another harness's controls to configure Codex. Khipu Owed remains capture-derived obligations and never becomes authoritative Aegis task state.

**Evidence and changing-fact rules.**

1. Preserve original evidence; derive observations and summaries separately. Every derived claim must identify its sources and derivation version. Missing source spans in old captures stay unknown; no fabricated backfill.
2. Separate explicit user decisions, tool-observed outcomes, and assistant interpretations. A later paraphrase or repeated copied summary cannot silently overrule a user instruction or count as independent support.
3. Extend the existing decisions registry rather than creating competing authority. Store actual supplied rationale; inferred explanations are labeled as inference. Supersession requires matching scope and a defensible reversal; ambiguity retains competing claims.
4. Keep recorded time, source event time, and effective validity distinct. Add temporal interpretation conservatively; ambiguous dates degrade visibly rather than producing invented precision.
5. Use additive migrations and compatibility projections. Keep episode/topic IDs and old tools stable. New internal claim records must remain resolvable; do not emit unknown public result kinds until every required client can fetch and render them.
6. Redact before extraction and preserve the redaction boundary in evidence references. Retention/forgetting must invalidate derived facts, summaries, embeddings, graph links, and local caches without resurrecting removed material.

**Retrieval and multi-harness contract.**

The explicit search, local snapshot-first prompt search, and bounded gateway search are separate acceptance targets. A change only to `embed.hybrid_search` is incomplete. Share state/policy helpers and compare outcomes across the three paths; identical algorithms or identical top-k under different budgets are not required.

Add metadata without removing or retyping current keys. Preserve `prior_work` array/empty/null meanings and the existing text fallback. Add a version/capability signal before depending on new metadata; older clients receive a truthful compatible projection. Distinguish no relevant evidence from timeout, stale snapshot, unavailable provider, and unsupported feature.

Preserve the documented budgets and cache placement in the verification record. Deeper graph traversal, reranking, consolidation, and reflection have explicit budgets and safe fallbacks. Include all extraction, refresh, embedding, reranking, and reflection costs; fast query latency must not hide an unbounded background bill.

Source revisions must propagate to replicas and invalidate derived briefs. Existing 24-hour snapshot eligibility is insufficient proof of current-fact freshness. For connected supported paths, propose a 60-second capture/correction-to-query target and measure it; old/offline replicas disclose their last successful source generation and do not assert that old facts are current. Capture queuing and bounded shutdown behavior remain intact.

Dedup must distinguish unchanged source IDs from changed revisions. Updating a decision under an existing ID must be capable of reaching a session that recently saw its old version. Khipu-owned hooks can be updated here; the corresponding Aegis consumer work is external.

Capture acceptance includes native events and transcript formats for each harness, root/child lineage, retry idempotency, queue/outbox recovery, abrupt end/compaction, and cross-machine replay. Capture remains single-writer per source event; a queue acknowledgment is not a hub-persistence claim. Injected recalled material must not become fresh independent evidence when recaptured.

Cursor and cloud clients cannot be declared equivalent to automatic prompt hooks. Their gate includes a real session that actually calls the relevant MCP tool. Configuration presence, tool discovery, and `/healthz` are necessary observations but not proof of capture or recall.

**Local evaluation and acceptance.**

Use a versioned synthetic corpus plus private golden cases retained outside the public repository. Freeze current behavior, model versions, inputs, and budgets as a control. Evaluate capture-to-answer separately from retrieval-only so extraction differences are visible.

The mandatory scenarios are: prior approved work; explicit reversal; historical/as-of question; same wording in different projects; indirect graph relationship; exact command/error/path; no relevant memory; ambiguous/conflicting evidence; mixed current/superseded episode; stale/offline snapshot and reconnection; changed revision under an existing ID; deferred commitment; existing artifact; duplicate/cross-harness recapture; source deletion; quota failure; malformed extraction; and crash/retry during consolidation.

Release-blocking correctness cases have zero unauthorized-scope returns, zero source-ID misattributions, zero current-truth assertions from known superseded-only evidence, and zero loss of original evidence in recovery fixtures. Historical retrieval remains available. Exact-string and existing golden positives must not regress. Proposed quality components must show repeatable benefit on their target cases under frozen model/context budgets; small score noise does not justify enabling them.

Record p50/p95 and timeout/miss rates per path, online/offline state, model, cache condition, and corpus size. Keep existing hard deadlines; do not raise them to manufacture a quality win. Freeze practical soft budgets from the Phase 1 baseline before implementation. Repeated oracle attempts remain capped at three per change, with one reserved for root acceptance.

Each implementation session names an exact direct local oracle based on the files changed. Use mocked/in-memory contracts first, then an isolated PostgreSQL/snapshot fixture, then genuine harness sessions. Live fixture writes require an isolated namespace/database and source identity; never use the production memory corpus as scratch data. Existing manual `khipu recall eval` is extended, not replaced by GitHub Actions.

**Aegis boundary and explicit dependency.**

No Aegis work is included. Preserve its current status/search shapes so existing bounded prior-work recall can benefit from server-side improvements. Full enhanced parity requires a separately authorized adapter packet for hybrid opt-in on initial/fallback recall, supported-empty/no-match handling, and evidence/validity/revision parsing and dedup. Native source fetch or reflect exposure is another conditional Aegis feature, not an implicit side effect of a new Khipu tool.

The Khipu phase supplies example responses and consumer contract fixtures in this repository. When an Aegis change becomes necessary, report the exact dependency and hold that acceptance item. Continue independent Khipu work; never edit Aegis or claim full Aegis acceptance from a mocked fixture. Its standalone behavior, local memory tools, explicit task authority, cache-stable prompt prefix, and current installation are preserved.

**Rollout and recovery.**

Keep each new quality component disabled until its gate passes; retain the old retrieval path as a control and rollback option. Migration rehearsal includes backup/restore and an old-client/new-server compatibility check. Shadow evaluation must not write inferred decisions into authoritative state. Any replacement of the legacy consolidation producer needs one-writer cutover, replay identity, and rollback evidence.

Local implementation/testing and actual distribution are separate. Before any later gateway deployment, desktop release, hook installation, or cloud configuration change, recheck this project's documented procedure and obtain authorization for that action. Final acceptance is per harness and deployed revision. The oldest permitted consumer remains a compatibility test, and a held external Aegis dependency remains visibly incomplete.
