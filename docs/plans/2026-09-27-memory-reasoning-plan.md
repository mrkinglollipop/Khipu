**Khipu memory improvements — thin plan, 27 September 2026**

Read [the proposed scope](2026-09-27-memory-reasoning-scope.md) first and [the verification record](../research/hindsight-parity-verification-2026-09-27.md) for the actual gaps and harness contracts. Status: proposed, awaiting scope approval. No implementation is started, and this is not a set of build briefs.

Each row is one bounded session. Create its fresh implementation brief only after scope approval, with owned worktree/files and an exact direct local oracle. Root owns integration and final checks; each repeated oracle has at most three attempts, including root's reserved final run.

| Session | Read-to-act focus and deliverable | Depends on / exit gate |
|---|---|---|
| Phase 1, session A | Inspect the existing evaluator and search logging; freeze synthetic/private controls and add outcome, freshness, abstention, latency and operation-cost measurements. | Scope approval. Reproducible baseline across all three recall implementations. |
| Phase 1, session B | Inspect the existing harness adapters, snapshot schema and formatters; create compatibility fixtures and a per-harness acceptance checklist. | 1A. Every path in the verification matrix has an oracle; external Aegis requirements are explicit. |
| Phase 2, session A | Inspect capture/extraction, decisions and migrations; add evidence/revision/time contracts while retaining existing IDs and single-writer behavior. | 1B. Old-client compatibility, capture identity, source preservation and migration recovery pass. |
| Phase 2, session B | Inspect supersession readers, replicas and prompt dedup; apply current/historical/conflict policy to all recall paths. | 2A. Reversal, mixed-episode, same-ID update, stale-cache, abstention and historical cases pass. Hold Aegis-native metadata/dedup acceptance until its external packet is approved and implemented. |
| Phase 3, session A | Inspect graph and temporal indexing; introduce bounded candidate retrieval and time interpretation behind independent switches. | 2B Khipu gates. Measured gain without exact-match, validity or deadline regressions. |
| Phase 3, session B | Inspect the measured ranking misses; trial a small optional reranker only where it can improve them. | 3A. Repeated gain under frozen budgets, or retain the existing ranker and record rejection. |
| Phase 4, session A | Inspect the actual configured consolidation producer and current topics; implement incremental source-backed briefs with revision, failure and deletion handling. | 2B; consume 3A only where needed. One writer, recovery, freshness and no self-reinforcing evidence pass. Stop before replacing any uninspected external producer. |
| Phase 4, session B | Inspect the evidence/brief APIs; add optional bounded reflection with source references and unresolved conflicts. | 4A and measured demand from Phase 1 cases. Grounding/abstention/cost gates pass; no automatic prompt-path LLM loop. |
| Phase 5, session A | Exercise Claude Code, Codex and Cursor in isolated real sessions, including capture, interruption, correction, recall and offline recovery. | Relevant implementation gates. Per-harness evidence and exact installed revision; no config-only acceptance. |
| Phase 5, session B | Exercise gateway/cloud clients and validate the unchanged Aegis consumer's compatible behavior; keep any enhanced Aegis acceptance separately held. | Khipu gates and separately authorized test/deployment environment. No Aegis edits; no claim of full parity while its adapter dependency remains open. |

Phase 4A may run independently of Phase 3B after its own prerequisites pass; parallel work requires separate ownership. No future phase is dispatched before its dependencies and approval are satisfied.

The first approved implementation slice is **Phase 1, session A**. Full Hindsight benchmarking, engine replacement, team authorization, UI redesign and release work are not prerequisites or silently added scope. The final handoff names remaining external gates, exact source/deployed revisions, local test evidence, and rollback state.
