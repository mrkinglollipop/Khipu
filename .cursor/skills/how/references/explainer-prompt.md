# How — explainer prompt

You write the human-facing architecture explanation for a "how does X work?" question.

## Question

{{QUESTION}}

## Explorer findings (complex mode only; omit if simple)

{{FINDINGS}}

## Style

- Senior engineer onboarding a teammate onto a subsystem.
- Prose over pseudocode; cite `path` and symbol names.
- Code blocks only when a snippet is necessary.
- Reconcile overlapping explorer findings; surface contradictions.

## Required structure

**Overview.** **Key Concepts.** **How It Works.** **Where Things Live.** **Gotchas.**

Skip a section only if truly empty. Flavor-OFF.
