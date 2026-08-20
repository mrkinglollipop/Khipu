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

The rule text is one place (RULE_MD) so the two shapes never drift. Claude
Code SessionStart also appends a small cwd-scoped (else recents) slice so
recall is pushed, not only pulled.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

RULE_MD = """# Khipu memory (MCP server `khipu`)

You have a persistent, cross-session memory: past conversation episodes, topic
pages, and a knowledge graph, searchable through the `khipu` MCP tools.

- Before answering anything that might have prior context (a project, a
  decision, a preference, "what did we do about X"), call `khipu_search` with a
  short natural-language query. Set `semantic: true` for meaning-based recall
  (cosine fused with query-term overlap). Default ILIKE ranks by how many
  query tokens match, not the whole phrase as one substring.
- `khipu_get` loads a search hit by id: full episode (summary, decisions,
  preferences, topics), a topic page, or media (path/sha256/mime). Search
  snippets are teasers; fetch the hit instead of guessing from a clipped line.
- `khipu_graph` expands wiki/path/graphify node ids. Topic slugs from search
  work. Digit ids are episodes: the walk is that episode's capture topics,
  not a graph node named with the episode number. Use `khipu_get` for the row.
- `khipu_status` tells you whether the hub is reachable and how fresh it is.
- Capture writes: on a local Mac with a Khipu capture hook (`khipu-stop-hook`
  or `khipu-aegis-capture`) do **not** call `khipu_capture` and do **not** pipe
  `capture_v2.py` — the hook is the writer (`capture_mode=hub`). An MCP write
  here double-captures. In a harness where a hook runs with `capture_mode`
  `dual`, the `khipu_capture` tool declines and says so — that is expected;
  the hook already has it. Cloud / HTTPS gateway (no hook): `khipu_capture`
  is the only write, so do it with a 1-3 sentence summary, short topic slugs,
  and any decisions or preferences; set `session_id` to
  `<harness>:<something stable>` (e.g. `grokbot:<repo>:<task>`).

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


_CWD_SKIP = frozenset(
    {
        "src",
        "lib",
        "app",
        "apps",
        "packages",
        "cli",
        "tests",
        "ios",
        "macos",
        "www",
        "frontend",
        "backend",
    }
)


def cwd_search_term(cwd: str | None) -> str:
    """Last non-generic path segment: ``.../Code/Khipu/packages/cli`` → ``Khipu``."""
    if not cwd:
        return ""
    p = Path(cwd.rstrip("/"))
    while p.name.lower() in _CWD_SKIP and p.parent != p:
        p = p.parent
    return p.name.strip()


def session_start_context(cwd: str | None = None) -> str:
    """RULE_MD plus a fail-open pushed slice. Never raises."""
    body = RULE_MD.strip()
    try:
        extra = _pushed_memory_slice(cwd)
    except Exception:
        extra = ""
    if extra:
        return f"{body}\n\n{extra}"
    return body


def _pushed_memory_slice(cwd: str | None) -> str:
    from khipu.activity import recent_episodes
    from khipu.cli import _search_query
    from khipu.db import connect
    from khipu.snippets import clip_snippet
    from khipu.topic_graph import enrich_search_results

    rows: list[dict] = []
    term = cwd_search_term(cwd)
    if term:
        with connect() as conn:
            with conn.cursor() as cur:
                rows = enrich_search_results(cur, _search_query(cur, term, 6))
    if not rows:
        rows = [
            {
                "kind": "episode",
                "id": str(r["id"]),
                "snippet": r.get("summary") or "",
            }
            for r in recent_episodes(limit=5)
        ]
    if not rows:
        return ""
    lines = [
        "## Pushed slice",
        "Loaded for this session without a search. Call `khipu_get` for a "
        "full row; `khipu_search` for more.",
    ]
    if term:
        lines.append(f"cwd token: `{term}`")
    for r in rows[:5]:
        snip = clip_snippet(str(r.get("snippet") or r.get("label") or ""), 220)
        lines.append(f"- {r.get('kind')} `{r.get('id')}`: {snip}")
    return "\n".join(lines)


def session_start_main() -> None:
    """Claude Code SessionStart entry: read hook JSON on stdin, print additionalContext."""
    cwd = None
    try:
        raw = sys.stdin.read()
        if raw.strip():
            payload = json.loads(raw)
            if isinstance(payload, dict):
                cwd = payload.get("cwd") or payload.get("cwd_path")
    except Exception:
        cwd = None
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": session_start_context(cwd),
                }
            }
        )
    )
