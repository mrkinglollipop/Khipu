---
name: how
description: >-
  Use for "how does X work", code walkthroughs before changing something, and
  placement / ownership / layering questions ("where should this live", "which
  package owns this"). Explains subsystem architecture and runtime flow. Optional
  critique mode for architectural issues. Use why for design motivation; use
  explore for bare file/symbol locate without a walkthrough.
---

# How

Orchestrator-owned skill. Explore the codebase and produce a senior-engineer onboarding explanation. Enough for a working mental model — not annotated source dumps.

**Not this:** financial/biblical Q&A; sticky potato-mode (PStack); bare “where is foo?” file locate → keep `explore` without this skill.

## Modes

1. **Explain** (default)
2. **Critique** — explain first, then multi-model architectural critics (when Matt asks for issues/improvements)

## Simple vs complex

| Bucket | When |
|--------|------|
| **Simple** | Single module/file, or a named symbol with a clear entry point |
| **Complex** | Multi-directory subsystem, cross-cutting feature, or full architecture overview |
| **Tie-break** | When in doubt → **simple** |

## Explain — simple

One Task:

- `subagent_type`: `explore` or `generalPurpose`
- `readonly`: `true`
- `model`: `cursor-grok-4.6-medium`

Agent explores and writes the explanation. Use `references/explainer-prompt.md`.

## Explain — complex

1. Decompose into 2–4 parallel exploration angles (distinct slices; avoid duplicate work).
2. Spawn all explorers in one message:
   - `subagent_type`: `explore` or `generalPurpose`
   - `readonly`: `true`
   - `model`: `cursor-grok-4.6-medium`
   - Prompt: `references/explorer-prompt.md` + specific angle
3. After explorers return, one explainer:
   - `subagent_type`: `generalPurpose`
   - `readonly`: `true`
   - `model`: `cursor-grok-4.6-high`
   - Prompt: `references/explainer-prompt.md` + all explorer findings

## Output format (explain)

Adapt sections to the question; skip empty ones.

**Overview.** 1–2 paragraphs. What it is, what it does, why it exists.

**Key Concepts.** Important types/services/abstractions needed for the rest.

**How It Works.** Trigger → steps → data flow → decision points. Prose; cite files/functions; code blocks only when necessary.

**Where Things Live.** Short map of relevant paths.

**Gotchas.** Non-obvious edges, historical weirdness.

## Critique mode

1. Run full explain flow first.
2. Spawn two critics in one message:
   - `subagent_type`: `generalPurpose`
   - `readonly`: `true`
   - `model`: `composer-2.5` and `cursor-grok-4.6-high` (one each)
   - Prompt: `references/critic-prompt.md` + explanation + file paths + `references/critique-rubric.md`
3. Each critic returns: severity, claim, evidence, suggested bucket (Act / Consider / Noted / Dismissed).
4. Orchestrator lead-judgment into those four buckets. **Not** the `session-auditor` / `audit-verifier` pipeline.

Present explanation first, then critique verdict.

## Present to Matt

Light edit for clarity OK. Flavor-ON in chat; skill bodies and subagent prompts stay Flavor-OFF.
