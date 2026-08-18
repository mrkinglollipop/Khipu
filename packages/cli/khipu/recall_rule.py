"""Prompt-time recall rule — the third pack component (P3 step 4, 2026-08-17).

A THIN cadence rule, not memory content. It tells the model that Khipu exists,
which MCP tools to reach for, and when — recall itself happens through the
tools, on demand. Two native shapes, per the one-pack-per-harness rule:

  Claude Code  a SessionStart hook (bin/khipu-recall-hook) that prints the rule as
               hookSpecificOutput.additionalContext — the same mechanism the
               workspace's other SessionStart hooks use. Global: every session.
  Cursor       a .cursor/rules/khipu.mdc file (alwaysApply) — Cursor's global
               "User Rules" live in app state, not a writable file, so this is
               PROJECT-scoped by design; the installer takes --project <dir>.
  Aegis        none. SessionStart/UserPromptSubmit are Observe gates (verified
               2026-08-17): stdout is discarded, so no rule can be injected.
               Recall is MCP + Stop-gate context; that is a fact, not a gap.

The rule text is one place (RULE_MD) so the two shapes never drift.
"""
from __future__ import annotations

RULE_MD = """# Khipu memory (MCP server `khipu`)

You have a persistent, cross-session memory: past conversation episodes, topic
pages, and a knowledge graph, searchable through the `khipu` MCP tools.

- Before answering anything that might have prior context (a project, a
  decision, a preference, "what did we do about X"), call `khipu_search` with a
  short natural-language query. Set `semantic: true` for meaning-based recall;
  leave it false for exact terms and slugs.
- `khipu_graph` expands a node id (from search results) to its neighbors.
- `khipu_status` tells you whether the hub is reachable and how fresh it is.
- When you finish a substantive piece of work (a decision, a fix, a finding
  worth remembering), call `khipu_capture` with a 1-3 sentence summary, short
  topic slugs, and any decisions or preferences; set `session_id` to
  `<harness>:<something stable>` (e.g. `grokbot:<repo>:<task>`). In a harness
  where a Khipu capture hook runs (capture_mode `dual`), the tool declines and
  says so — that is expected; the hook already has it. Where you reach Khipu
  through the HTTPS gateway (cloud agents), the tool is the only way the
  session gets remembered, so do it.

Recall is on demand: search when it would change your answer, not on every turn.
"""

CURSOR_MDC = (
    "---\n"
    "description: Khipu cross-session memory — when and how to use the khipu MCP tools\n"
    "alwaysApply: true\n"
    "---\n\n"
    + RULE_MD
)


def claude_additional_context() -> str:
    return RULE_MD.strip()


def cursor_mdc() -> str:
    return CURSOR_MDC
