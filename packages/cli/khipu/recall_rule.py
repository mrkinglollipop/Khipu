"""Prompt-time recall rule — the third pack component (P3 step 4, 2026-08-17).

A THIN cadence rule, not memory content. It tells the model that Khipu exists,
which MCP tools to reach for, and when — recall itself happens through the
tools, on demand. Native shapes, per the one-pack-per-harness rule:

  Claude Code  a SessionStart hook (bin/khipu-recall-hook) that prints the rule as
               hookSpecificOutput.additionalContext — Claude's nested inject field.
               Global: every session. Also appends a cwd-scoped (else recents) slice.
  Cursor       both: (1) project ``.cursor/rules/khipu.mdc`` (alwaysApply) for pull,
               and (2) ``sessionStart`` hook emitting top-level ``additional_context``
               (Cursor's inject field — not Claude ``additionalContext``). The installer
               appends a second sessionStart entry with a PG-capable timeout; it does
               not replace harness ``session_start.sh``. The Cursor install command
               passes ``--cursor`` so the hook emits ``additional_context`` (not an
               inherited env var that could reshape Claude SessionStart).
  Aegis        none. SessionStart/UserPromptSubmit are Observe gates (verified
               2026-08-17): stdout is discarded, so no rule can be injected.
               Recall is MCP + Stop-gate context; that is a fact, not a gap.

The rule text is one place (RULE_MD) so the shapes never drift.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

RULE_MD = """# Khipu memory (MCP server `khipu`)

You have a persistent, cross-session memory: past conversation episodes, topic
pages, and a knowledge graph, searchable through the `khipu` MCP tools.

- Before answering anything that might have prior context (a project, a
  decision, a preference, "what did we do about X"), call `khipu_search` with a
  short natural-language query. Default `mode` is `hybrid`: cosine similarity,
  token overlap, and literal substring match, fused by reciprocal-rank fusion
  and ranked by score (graph nodes are excluded from the ranked results unless
  `kind: "node"` or the query looks id-shaped, e.g. contains `:` or `__`; if no
  embedding profile is active it degrades to literal + token overlap). Use
  `mode: "literal"` for exact strings, ids, hashes, or error text; `mode:
  "semantic"` (same as the legacy `semantic: true`) for cosine + token-overlap
  only, no literal list. Filters work on every mode: `kind` (episode/topic/node,
  or episode/topic/media for semantic), `project`, `since`/`until` (ISO date or
  relative like `7d`/`24h`), `session_id` (prefix match), `harness` (prefix of
  session_id before the colon).
- `khipu_get` loads a search hit by id: full episode (summary, decisions,
  preferences, topics), a topic page, or media (path/sha256/mime). Search
  snippets are teasers; fetch the hit instead of guessing from a clipped line.
- `khipu_graph` expands wiki/path/graphify node ids. Topic slugs from search
  work. Digit ids are episodes: the walk is that episode's capture topics,
  not a graph node named with the episode number. Use `khipu_get` for the row.
- `khipu_status` tells you whether the hub is reachable and how fresh it is.
- `khipu_owed` lists open (or closed/stale) commitments — followups, blockers,
  questions, promises — for a project. Open commitments for this repo are
  pushed at session start where a slice is available; call `khipu_owed` at
  session start for a harness with no pushed slice (Aegis), or whenever "what
  do I still owe on this project" would change the answer.
- `khipu_owed_update` closes, reopens or snoozes one commitment by id. Items
  marked "needs the user" are the user's to do or decide, not yours; the
  moment the user says one is done, close it here instead of waiting for the
  next capture to notice. Close your own promises when you keep them.
- `khipu_forget` forgets one episode completely (row, vectors, the
  commitments it opened, its legacy line) — for a capture that was wrong or a
  test. Through the gateway only cloud-harness captures can be forgotten.
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


# W4: ~1500 tokens at a conservative ~4 chars/token — the slice's hard
# budget from the plan. Enforced by dropping whole lines from the tail
# rather than mid-truncating one, so a rendered line is never cut mid-word.
_SLICE_BUDGET_CHARS = 6000


def _current_host_session_id() -> str | None:
    """This session's own lineage id, same `harness:hostid` shape
    capture.write_pg stores parent_session_id in (session_capture.
    parent_session_id). Widens the W4 episode match beyond `project` alone:
    a dispatched sibling whose project inheritance missed at capture time
    still surfaces here via its parent_session_id. Claude Code only today —
    no other harness pack was found to carry an equivalent env var (see
    session_capture.parent_session_id's docstring for the per-harness audit)."""
    host_id = (os.environ.get("CLAUDE_CODE_HOST_SESSION_ID") or "").strip()
    return f"claude_code:{host_id}" if host_id else None


def _fit_budget(lines: list[str], budget: int = _SLICE_BUDGET_CHARS) -> list[str]:
    out: list[str] = []
    total = 0
    for line in lines:
        total += len(line) + 1
        if total > budget and out:
            break
        out.append(line)
    return out


def _render_project_slice(label: str, slice_data: dict) -> str:
    from khipu.snippets import clip_snippet

    lines = ["## Pushed slice", f"project: `{label}`"]
    owed = slice_data.get("commitments") or []
    if owed:
        lines.append("### Open commitments")
        for c in owed[:5]:
            text = clip_snippet(str(c.get("text") or ""), 160)
            owner = str(c.get("owner") or "").strip()
            who = " (needs the user)" if owner == "user" else (" (yours)" if owner == "assistant" else "")
            lines.append(f"- [{c.get('kind')}]{who} `{c.get('id')}`: {text}")
        lines.append(
            "Items marked (needs the user) are the user's to do or decide — do not "
            "act on them yourself. When the user says one is done, close it right "
            "away with `khipu_owed_update` (or `khipu owed --close ID`) instead of "
            "waiting for the next capture to notice."
        )
    episodes = slice_data.get("episodes") or []
    if episodes:
        lines.append("### Recent episodes")
        for ep in episodes[:5]:
            text = clip_snippet(str(ep.get("summary") or ""), 200)
            lines.append(f"- episode `{ep.get('id')}`: {text}")
    topics = slice_data.get("topics") or []
    if topics:
        lines.append("### Linked topics")
        for t in topics[:3]:
            age = t.get("age_days")
            age_txt = f"{age}d old" if age is not None else "age unknown"
            title = t.get("title") or t.get("slug") or ""
            lines.append(f"- topic `{t.get('slug')}` ({age_txt}): {title}")
    lines.append(
        "Loaded for this session without a search. Call `khipu_get` for a "
        "full row; `khipu_search` for more."
    )
    return "\n".join(_fit_budget(lines))


def _stale_project_slice(project: str | None, host: str | None) -> str:
    """W4.2 degrade when the hub itself is unreachable: best-effort recent
    episodes from the local hub_snapshot, matched against `raw.project` /
    `raw.parent_session_id` (the snapshot's episodes table carries neither as
    a real column, and has no commitments table at all — a documented
    limitation surfaced by the `stale` line, not silently hidden). Never
    raises; empty string on any further failure, which lets the caller fall
    through to the plain cwd-token search."""
    if not project and not host:
        return ""
    try:
        from khipu import hub_snapshot
        from khipu.snippets import clip_snippet

        con = hub_snapshot.open_snapshot()
    except Exception:
        return ""
    hits: list[tuple[str, str]] = []
    try:
        cur = con.execute(
            "SELECT id, summary, raw FROM episodes "
            "ORDER BY COALESCE(ingested_at, ts) DESC LIMIT 200"
        )
        for eid, summary, raw in cur.fetchall():
            proj = parent = None
            if raw:
                try:
                    payload = json.loads(raw)
                    proj = payload.get("project")
                    parent = payload.get("parent_session_id")
                except (ValueError, TypeError):
                    pass
            if (project and proj == project) or (host and parent == host):
                hits.append((str(eid), summary or ""))
            if len(hits) >= 5:
                break
    except Exception:
        return ""
    finally:
        con.close()
    if not hits:
        return ""
    lines = [
        "## Pushed slice",
        f"stale: hub unreachable — episodes from the local snapshot "
        f"(project=`{project}`); no commitments available in this fallback.",
    ]
    for eid, summary in hits:
        lines.append(f"- episode `{eid}`: {clip_snippet(summary, 200)}")
    return "\n".join(lines)


def _pushed_memory_slice(cwd: str | None) -> str:
    """W4: repo-scoped first (commitments, then episodes, then linked
    topics), the cwd-token search only as a fallback when nothing repo-scoped
    resolved or came back empty — same precedence the plan's slice order
    describes. A PG failure degrades to the local hub_snapshot with a visible
    `stale` line and logs the miss via query_log so W6 can count it."""
    from khipu import activity
    from khipu.cli import _search_query
    from khipu.db import connect
    from khipu.snippets import clip_snippet
    from khipu.topic_graph import enrich_search_results

    try:
        from khipu.identity import resolve_repo_root

        ident = resolve_repo_root(cwd or "")
    except Exception:
        ident = {"repo_root": None, "project": None}
    project = ident.get("project")
    repo_root = ident.get("repo_root")
    host = _current_host_session_id()

    if project or host:
        slice_data = None
        try:
            slice_data = activity.project_slice(
                project=project, repo_root=repo_root, host_session_id=host
            )
        except Exception as exc:
            try:
                from khipu import query_log

                query_log.log_query(
                    f"slice:{project or host or ''}",
                    mode="slice",
                    filters={"project": project, "cwd": cwd},
                    result_count=0,
                )
            except Exception:
                pass
            stale = _stale_project_slice(project, host)
            if stale:
                return stale
            _ = exc  # PG unreachable and no snapshot to degrade to — fall through below
        if slice_data and (slice_data.get("commitments") or slice_data.get("episodes")):
            return _render_project_slice(project or host or "unknown", slice_data)

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
            for r in activity.recent_episodes(limit=5)
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


def _session_start_cwd() -> str | None:
    try:
        raw = sys.stdin.read()
        if raw.strip():
            payload = json.loads(raw)
            if isinstance(payload, dict):
                cwd = payload.get("cwd") or payload.get("cwd_path")
                if isinstance(cwd, str) and cwd.strip():
                    return cwd
                # Cursor sessionStart: common input uses workspace_roots, not cwd.
                roots = payload.get("workspace_roots")
                if isinstance(roots, list) and roots:
                    first = roots[0]
                    if isinstance(first, str) and first.strip():
                        return first
    except Exception:
        return None
    return None


def session_start_main(*, shape: str | None = None) -> None:
    """SessionStart entry: print the harness-native inject JSON.

    ``shape`` is ``cursor`` (flat ``additional_context``) or ``claude`` (nested
    ``hookSpecificOutput``). Default is Claude. Cursor installs pass ``--cursor``
    on the shim argv; a process-wide env var must not reshape Claude SessionStart.
    """
    cwd = _session_start_cwd()
    ctx = session_start_context(cwd)
    if shape is None:
        shape = "cursor" if "--cursor" in sys.argv[1:] else "claude"
    want = shape.strip().lower()
    if want == "cursor":
        print(json.dumps({"additional_context": ctx}))
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": ctx,
                }
            }
        )
    )
