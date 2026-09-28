# Fable review: Khipu memory improvements

> Superseded on 28 September 2026. The review is complete: see [the review record](../research/hindsight-plan-review-2026-09-28.md). The user lifted the two conditions stated below: implementation is authorized, and Aegis changes are authorized in a dedicated worktree. This request is kept as the record of what was asked.

Read [the proposed scope](2026-09-27-memory-reasoning-scope.md) first, then [the source verification](../research/hindsight-parity-verification-2026-09-27.md) and [the phased plan](2026-09-27-memory-reasoning-plan.md). Review the proposal; do not implement it. The scope remains unapproved. The user explicitly prohibited changes to the Aegis repository and requires any Aegis dependency to be reported separately.

The [original comparison](../research/hindsight-comparison-2026-09-27.md) supplies research context. The subsequent verification record governs corrected claims. [Archived evidence](../research/evidence/hindsight-parity-2026-09-27/README.md) includes the independent source reviews, exact test selectors, original runner and passing output. No essential review evidence depends on temporary files or this conversation.

## Source baselines and proof limits

Review source at the pinned revisions linked in the verification record:

- Khipu: `9d7d7e09ef63d760ce2fd526b7ed4c1785b6c209`.
- Hindsight main: `ccfe85b4851957ac2adf88b4a9ddf9668b2882f1`. This is not a claim about every released version.
- Aegis, read only: `42dde0418822ead6a0c9fcf7f75629fdf8e1c467`.

The documentation branch starts from Khipu `cebd732ecfff6c6d014ca0c8c6f0f4893366d6a2`; its application files are not the reviewed Khipu baseline. Use a separate checkout at the reviewed revision or the pinned source links when checking implementation claims. Do not infer behavior from this documentation checkout's older application source.

One offline invocation passed 115 tests and 6 subtests. That establishes selected existing unit contracts. Hindsight quality, installed harness behavior, deployed consolidation, and cross-product performance were not exercised. Prior live acceptance is historical evidence, not acceptance of these proposed changes.

## Review questions

1. Are the corrected parity claims supported by the pinned implementations? Separate confirmed gaps from hypotheses and unmeasured benefits.
2. Does the scope preserve Khipu's three recall paths: explicit search, local snapshot prompt recall, and bounded gateway prompt recall? Check Claude Code, Codex, Cursor, Aegis, and cloud clients individually, including capture ownership and identity.
3. Are current versus historical truth, mixed episodes, source authority, revision-aware deduplication, freshness, deletion, offline replay, and old-client compatibility specified sufficiently?
4. Are the evaluator-first sequence, latency/cost gates, optional reranking/reflection, and incremental topic-page approach proportionate? Identify missing acceptance cases and unnecessary scope.
5. Are Aegis dependencies complete and isolated? Preserve standalone Aegis and the distinction between Khipu Owed and Aegis Ledger. Do not modify Aegis code, configuration, installed app, or runtime.

Return findings in severity order with an exact document/source location, evidence, practical consequence, and smallest proposed correction. Distinguish blocking issues, nonblocking suggestions, and unresolved questions. Conclude whether the scope is ready for user approval. Review only: do not implement, deploy, publish, merge, or approve on the user's behalf.
