"""Topic/graph hygiene — W5.1 (topics vs tags) and W5.2 (path minting filter).

Two shapes of graph pollution measured 2026-09-03: 94% of capture-topic slugs
never match a real topic page (they become dangling `topic:` nodes), and 73%
of `path:` nodes are not paths at all (`a/b`, no extension, no leading `/` or
`~`). Both are filtered HERE, once, so every writer — the native extractor,
the legacy capture_v2 payload path, and gateway captures — goes through the
same rule instead of three drifting copies.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable

# ---- W5.1 topics vs tags ---------------------------------------------------

# A worktree-minted slug: extract.py used to append the cwd basename, and a
# worktree dir name typically ends in a short hex disambiguator.
_WORKTREE_HEX_RE = re.compile(r"-[0-9a-f]{6}$")

_NOISE_EXACT = {
    "tmp", "temp", "test", "testing", "general", "misc", "stuff", "things",
    "chat", "session", "sessions", "unknown", "other", "misc-notes",
}

_HARNESS_NAMES = {
    "claude", "cursor", "codex", "aegis", "grok", "claude-code", "claude_code",
    "grok-bot", "grokbot",
}


def is_noise_slug(slug: str) -> bool:
    s = (slug or "").strip().lower()
    if not s:
        return True
    if _WORKTREE_HEX_RE.search(s):
        return True
    if s in _NOISE_EXACT or s in _HARNESS_NAMES:
        return True
    return False


def classify_topics(cur, topics: Iterable[Any]) -> tuple[list[str], list[str], bool]:
    """(resolved, tags, unresolved_due_to_error) for a capture's topic slugs.

    The junk signal is the NOISE PATTERN (worktree/hex suffix, tmp, harness
    names, the short generic list — :func:`is_noise_slug`), never "has no
    page yet". Hub/cloud captures never see the file wiki, so gating minting
    on an existing page meant EVERY previously-unseen topic became a tag —
    94% of capture-topic slugs measured dangling 2026-09-03, and this was
    why: a clean, novel topic slug was silently demoted every single time.

    A slug that matches the noise set becomes a TAG (still visible on the
    episode, never a graph node). Every other slug is ``resolved`` — minted
    as a NEW ``topic:`` node when it does not already match a live topic page
    or a ``topic_aliases`` entry, or folded onto the existing page/alias's
    canonical slug when it does. Order is preserved and slugs are deduped.

    If the existence check itself fails (PG unreachable mid-call, or no
    cursor at all — the write path could not check), the caller should keep
    today's behaviour and flag ``raw.topics_unresolved=true``; that is what
    the third return value signals. In that case ``resolved`` carries every
    non-noise candidate unchanged (nothing dropped, nothing tagged) so a
    write in a degraded environment does not silently lose topics.
    """
    candidates: list[str] = []
    tags: list[str] = []
    seen_candidates: set[str] = set()
    seen_tags: set[str] = set()
    for raw in topics or []:
        s = str(raw or "").strip().lower()
        if not s:
            continue
        if is_noise_slug(s):
            if s not in seen_tags:
                seen_tags.add(s)
                tags.append(s)
            continue
        if s not in seen_candidates:
            seen_candidates.add(s)
            candidates.append(s)
    if not candidates or cur is None:
        return candidates, tags, bool(candidates and cur is None)

    try:
        cur.execute(
            "SELECT slug FROM topics WHERE slug = ANY(%s) AND deleted_at IS NULL",
            (candidates,),
        )
        exact = {r[0] for r in cur.fetchall()}
        remaining = [c for c in candidates if c not in exact]
        alias_map: dict[str, str] = {}
        if remaining:
            cur.execute(
                "SELECT alias, slug FROM topic_aliases WHERE alias = ANY(%s)",
                (remaining,),
            )
            alias_map = {a: s for a, s in cur.fetchall()}
    except Exception:  # noqa: BLE001 — degrade to "keep everything as topics"
        return candidates, tags, True

    resolved: list[str] = []
    seen_resolved: set[str] = set()
    for c in candidates:
        # Not a noise slug: it stays a topic whether or not it already has a
        # page — alias/exact-match just canonicalizes it onto the existing
        # slug rather than minting a near-duplicate.
        slug = alias_map.get(c, c)
        if slug not in seen_resolved:
            seen_resolved.add(slug)
            resolved.append(slug)
    return resolved, tags, False


# ---- W5.2 path minting filter ----------------------------------------------

def is_real_path(rel: str, *, repo_root: str | None = None) -> bool:
    """A ``path:`` node is only minted for something that looks like a real
    path: it has a file extension, it starts with an absolute/home/relative
    marker, or it actually exists under ``repo_root``."""
    s = (rel or "").strip()
    if not s:
        return False
    if os.path.splitext(s)[1]:
        return True
    if s.startswith(("/", "~", "./")):
        return True
    if repo_root:
        try:
            candidate = Path(repo_root).expanduser() / s.lstrip("/")
            if candidate.exists():
                return True
        except (OSError, ValueError):
            pass
    return False


def filter_real_paths(rels: Iterable[str], *, repo_root: str | None = None) -> list[str]:
    return [r for r in rels if is_real_path(r, repo_root=repo_root)]


def report_junk_paths(cur, *, sample_limit: int = 20) -> dict[str, Any]:
    """Read-only: how many existing ``path:`` nodes fail :func:`is_real_path`.

    No ``repo_root`` context is available for a historical node (the graph
    does not record which episode/topic minted it), so this checks the
    extension/leading-marker rule only — the ``exists()`` branch never
    applies here, matching the conservative dry-run report the scope calls
    for before any destructive backfill.
    """
    cur.execute("SELECT id FROM nodes WHERE type = 'path'")
    rows = [r[0] for r in cur.fetchall()]
    failing: list[str] = []
    for node_id in rows:
        rel = node_id[len("path:"):] if node_id.startswith("path:") else node_id
        if not is_real_path(rel):
            failing.append(node_id)
    return {
        "total_path_nodes": len(rows),
        "failing": len(failing),
        "sample": failing[:sample_limit],
    }


def apply_purge_junk_paths(cur) -> dict[str, Any]:
    """Destructive: delete every ``path:`` node failing :func:`is_real_path`
    (and its edges). Must NOT be run against the live shared hub without
    Matt's explicit go (scope §7 item 2) — callers are responsible for that
    gate; this function only executes what it is asked to.
    """
    report = report_junk_paths(cur, sample_limit=10_000_000)
    ids = report["sample"]
    if ids:
        cur.execute("DELETE FROM edges WHERE src = ANY(%s) OR dst = ANY(%s)", (ids, ids))
        cur.execute("DELETE FROM nodes WHERE id = ANY(%s)", (ids,))
    return {"deleted_nodes": len(ids), "sample": ids[:20]}


# ---- W1.5 identity backfill -------------------------------------------------

def apply_backfill_identity(cur, *, limit: int | None = None) -> dict[str, Any]:
    """Destructive: derive repo_root/project (via khipu.identity, using each
    episode's absolute-path ``scope`` as a stand-in cwd) for every episode
    that has none yet, and UPDATE it in place. Must NOT be run against the
    live shared hub without Matt's explicit go (scope §7 item 2) — callers
    are responsible for that gate; this function only executes what it is
    asked to.
    """
    from khipu.identity import resolve_repo_root

    sql = (
        "SELECT id, scope FROM episodes WHERE repo_root IS NULL "
        "AND scope IS NOT NULL AND scope LIKE '/%%' ORDER BY id DESC"
    )
    if limit:
        cur.execute(sql + " LIMIT %s", (limit,))
    else:
        cur.execute(sql)
    rows = cur.fetchall()
    updated = 0
    for eid, scope in rows:
        ident = resolve_repo_root(scope)
        if not ident.get("repo_root"):
            continue
        cur.execute(
            "UPDATE episodes SET repo_root = %s, project = %s WHERE id = %s",
            (ident["repo_root"], ident["project"], eid),
        )
        updated += cur.rowcount
    return {"scanned": len(rows), "updated": updated}


def backfill_identity_report(cur, *, sample_limit: int = 20) -> dict[str, Any]:
    """Dry-run only: how many episodes have an absolute-path ``scope`` and no
    ``repo_root``/``project`` yet, with a sample. Never writes.
    """
    cur.execute(
        """
        SELECT id, scope, session_id FROM episodes
        WHERE repo_root IS NULL
          AND scope IS NOT NULL AND scope LIKE '/%%'
        ORDER BY id DESC
        LIMIT %s
        """,
        (sample_limit,),
    )
    sample = [{"id": r[0], "scope": r[1], "session_id": r[2]} for r in cur.fetchall()]
    cur.execute(
        "SELECT COUNT(*) FROM episodes WHERE repo_root IS NULL AND scope IS NOT NULL AND scope LIKE '/%%'"
    )
    total = int(cur.fetchone()[0])
    return {"would_backfill": total, "sample": sample}
