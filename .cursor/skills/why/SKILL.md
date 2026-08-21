---
name: why
description: >-
  Use for "why does X work this way", design rationale, regressions, or tradeoffs
  for code/decisions in this workspace. Internal archaeology via git/gh, Notion,
  graph, and memory (topics + episodes). Does NOT search the open web — that is
  the research skill. Use how for runtime behavior.
---

# Why

Orchestrator-owned skill. Investigate **motivation and intent** behind code in this workspace. Companion to `how` (mechanics) and `research` (external web).

## HARD boundary vs research

| Skill | Owns | Does **not** |
|-------|------|--------------|
| **why** | Internal trail: git/`gh`, Notion, graph, memory topics + episodes | Open web, Tavily/Firecrawl/YouTube, industry “why companies do X” |
| **research** | External evidence via `scripts/_clients` / gather\|deep | Claiming a blog post is why *this* repo shipped a design |

- Industry / library / market “why?” with no workspace code anchor → **`research`** (or `web-search` for one lookup).
- “Why is *our* code shaped this way?” → **`why`**.
- Mixed: run `why` on the code anchor first; hand off external leg to `research`. Never merge web hits into **What We Found** as local intent.
- **Do not** call `web-search` / `research` from inside this skill.

## Operating posture

Evidence before narrative. Cite or label inference. Null results are first-class. Prefer “appears to” over confident storytelling. Do not invent intent from code shape alone.

## Step 1 — Target and question

Parse target (code/pattern/decision) and question type (rationale, tradeoff, edge cases, business constraint, dead code, history). If vague, state best-guess interpretation and proceed.

## Step 2 — Code anchor (inline, cheap)

Before investigators:

- File path(s) + line range(s), key symbols
- Recent commits / blame; PR numbers from merge subjects
- Pull PR bodies via `gh` when substantive

Pass this seed to every investigator.

## Step 3 — Parallel investigators

Spawn one investigator per **available** category (not empty theater):

| Category | Source | Always? |
|----------|--------|---------|
| Source control | git + `gh` (blame/log, PR bodies, reviews, issues) | Yes |
| Long-form / decisions | Notion MCP + `query_graph.py` + memory topics **and** episode grep (incl. `episodes_<YYYY>.jsonl` archives per `memory.mdc`) | When MCPs/paths exist |
| Issue tracker | GitHub Issues via `gh` if linked | When PR/issue IDs appear |
| Observability / Slack / Sentry / warehouse / open web | — | Gap line in Sources Consulted only |

### Task pins

**Readonly default (HARD):** every Task below uses `readonly: true`. Investigators and synthesizer are **read-only archaeology** — no `Write` / `StrReplace` / `Delete`, no git mutate, no file creation.

| Role | subagent_type | readonly | model |
|------|---------------|----------|-------|
| Investigators (git/gh, graph/memory/episodes) | `generalPurpose` | `true` | `cursor-grok-4.6-medium` |
| Investigator (Notion MCP) | `generalPurpose` | `true` | `cursor-grok-4.6-medium` |
| Synthesizer | `generalPurpose` | `true` | `cursor-grok-4.6-high` |

**Notion MCP escape (narrow):** use `readonly: false` **only** when a Notion investigator Task fails because `readonly: true` strips required MCP reads — never for git/gh/graph/memory investigators or the synthesizer. Orchestrator must still dispatch with **mutation forbidden** (no Write tools; prompt must state read-only). Revert to `readonly: true` on retry when MCP works under readonly.

Each investigator gets: `references/investigator-prompt.md` + matching playbook under `references/sources/` + code anchor + user question.

## Step 4 — Synthesize

One synthesizer with all findings (including nulls), code anchor, question, `references/epistemics.md`, `references/synthesizer-prompt.md`.

## Step 5 — Present

Do **not** rewrite confidence hedges. Thin local record → say so in What We Don't Know; do not pad with web speculation.

## Output format

**The Question.** **The Code in Question.** **What We Found (direct evidence).** **What We Can Reasonably Infer.** **Competing Hypotheses** (if needed). **What We Don't Know.** **Sources Consulted** (one line per category, including empties/skips).

Optional: if the ask is a precursor to changing the code, add Preserve / Change / Avoid / Risk bullets.
