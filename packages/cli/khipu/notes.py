"""Index harness-native per-project notes as topics (W4.3, plan §W4 item 3).

Claude Code keeps durable notes at ``~/.claude/projects/<slug>/memory/*.md``
(one file per topic plus a generated ``MEMORY.md`` index) that are invisible
to Khipu's own search/graph — the pushed slice can push episodes and capture
topics, but never these. This module mirrors them into ``topics`` the same
way a capture-time topic page lands there, with two differences from an
ordinary wiki topic:

  - a ``note:`` prefix on the slug, so a note can never collide with (or
    silently overwrite) a same-named wiki topic;
  - ``frontmatter["project"]`` set from the note's own repo mapping, so
    ``activity.project_slice`` (W4) can pull a repo's notes directly rather
    than only reaching them through an episode's already-linked topics.

Claude Code's memory-note frontmatter is NOT the flat ``status:``/``title:``
shape ``mirror.parse_topic_file`` expects — it is ``name``/``description``/a
nested ``metadata: {type, modified, ...}`` block — so this module parses it
itself rather than forcing the wrong parser onto it, then calls
``mirror.normalize_topic_status`` (the one status-normalizer) and
``mirror._upsert_topic`` (the one topic-upsert, the same one
``mirror.mirror_topic_file`` calls internally) directly.

``reconcile()`` is append-only and additive-only: it walks the files,
upserts each into ``topics``, and never deletes/tombstones a topic no longer
present (unlike the wiki reconcile) — a note vanishing from
``~/.claude/projects`` is not a signal Khipu should act on unattended. Never
runs against the live hub in a test: every test here injects a temp dir and
mocks ``khipu.db.connect`` with a fake cursor, same as every other write
path in this package.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

NOTE_SLUG_PREFIX = "note:"
_WIKILINK_RE = re.compile(r"\[\[([a-z0-9][a-z0-9_-]*)\]\]", re.I)
# Bound on how many consecutive slug segments can fold into one path
# component (a directory name with a space in it) while resolving a
# Claude Code project slug back to a real path — see resolve_claude_project_path.
_MAX_JOIN_SEGMENTS = 4


def _log(msg: str) -> None:
    print(f"[khipu-notes] {msg}", file=sys.stderr)


def claude_projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


def codex_memories_root() -> Path:
    return Path.home() / ".codex" / "memories"


def _walk_segments(base: Path, segments: list[str]) -> Path | None:
    """DFS over the real filesystem, not a string transform.

    A Claude Code project slug is the repo's absolute path with '/' replaced
    by '-' — but a space in a directory name collapses to '-' the exact same
    way (``/Volumes/My Drive/Code/Widget`` -> ``-Volumes-My-Drive-Code-
    Widget``), so the two are indistinguishable in the slug string alone.
    There is no clean inverse; this tries ``' '.join`` of 1..N consecutive
    segments as the next path component at each level, and only descends
    into a candidate that actually exists on disk — the filesystem itself
    disambiguates "Cloud" + "Storage" (two dirs) from "Cloud Storage" (one).
    """
    if not segments:
        return base if base.is_dir() else None
    limit = min(_MAX_JOIN_SEGMENTS, len(segments))
    for join_n in range(1, limit + 1):
        name = " ".join(segments[:join_n])
        candidate = base / name
        if candidate.is_dir():
            found = _walk_segments(candidate, segments[join_n:])
            if found is not None:
                return found
    return None


def resolve_claude_project_path(slug: str, *, root: Path = Path("/")) -> Path | None:
    """``~/.claude/projects/<slug>`` -> the repo's real absolute path, or
    None when nothing on disk matches. Never raises. ``root`` defaults to
    the real filesystem root; tests point it at a temp tree instead of
    faking the walk."""
    segments = [s for s in (slug or "").strip("-").split("-") if s]
    if not segments:
        return None
    try:
        return _walk_segments(root, segments)
    except OSError:
        return None


def _parse_note_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Permissive parse of Claude Code's memory-note frontmatter: flat
    ``key: value`` lines plus ONE level of nesting (a bare ``metadata:``
    line followed by indented ``sub_key: value`` lines), flattened to
    ``metadata.sub_key``. Deliberately not ``mirror.parse_topic_file``'s
    shape — see module docstring. Returns
    ``({flat key: value}, body-after-frontmatter)``; ``({}, text)`` when
    there is no ``---`` frontmatter block at all."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fm_text, body = parts[1], parts[2].lstrip("\n")
    flat: dict[str, str] = {}
    section = ""
    for raw_line in fm_text.splitlines():
        if not raw_line.strip() or ":" not in raw_line:
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        key, _, val = raw_line.strip().partition(":")
        key = key.strip()
        val = val.strip().strip("\"'")
        if indent == 0:
            if val:
                flat[key] = val
                section = ""
            else:
                section = key
        elif section:
            flat[f"{section}.{key}"] = val
    return flat, body


def _extract_note_links(body: str) -> list[str]:
    """``[[slug]]`` wiki-links in a note's body -> other notes' ``note:``
    slugs — real graph edges between notes, the same as a wiki topic's."""
    seen: list[str] = []
    for m in _WIKILINK_RE.finditer(body):
        target = f"{NOTE_SLUG_PREFIX}{m.group(1).strip().lower()}"
        if target not in seen:
            seen.append(target)
    return seen


def _note_topic_dict(path: Path, *, project: str | None) -> dict[str, Any] | None:
    """One note ``.md`` file -> the shape ``mirror._upsert_topic`` expects,
    or None when the file is missing/unreadable — mirrors
    ``mirror.read_topic_text``'s contract so a caller building a ``seen``
    set behaves the same way. Never raises."""
    from khipu.mirror import _parse_frontmatter_date, normalize_topic_status, topic_content_hash
    from khipu.topic_graph import topic_slug_from_label

    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    flat, body = _parse_note_frontmatter(text)
    name = flat.get("name") or path.stem
    # `name` is free text ("Aggressive automatic memory capture", em-dashes
    # and all) — slugify it the same way a capture topic label is slugified
    # so `topics.slug` stays a real slug; `title` keeps the readable form.
    slug = f"{NOTE_SLUG_PREFIX}{topic_slug_from_label(name) or path.stem}"
    type_raw = flat.get("metadata.type") or flat.get("type") or ""
    status = normalize_topic_status(type_raw)
    updated_at = _parse_frontmatter_date(flat.get("metadata.modified") or flat.get("modified"))
    links = _extract_note_links(body)
    frontmatter = {
        "title": name,
        "status": status,
        "status_raw": type_raw or None,
        "links": links,
        "project": project,
        "note_source": str(path),
    }
    return {
        "slug": slug,
        "title": name,
        "status": status,
        "body": body,
        "digest": topic_content_hash(text),
        "links": links,
        "frontmatter": frontmatter,
        "created_at": None,
        "updated_at": updated_at,
    }


def _iter_note_files(memory_dir: Path) -> list[Path]:
    """Flat (non-recursive) ``*.md`` in ``memory_dir``, excluding
    ``MEMORY.md`` (the generated index, not a note of its own)."""
    if not memory_dir.is_dir():
        return []
    return sorted(p for p in memory_dir.glob("*.md") if p.name != "MEMORY.md" and p.is_file())


def _claude_project_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


def _project_for_slug(slug: str) -> str | None:
    repo_path = resolve_claude_project_path(slug)
    if repo_path is None:
        return None
    try:
        from khipu.identity import resolve_repo_root

        return resolve_repo_root(str(repo_path)).get("project")
    except Exception:  # noqa: BLE001 — identity resolution is best-effort here
        return None


def _build_plan() -> list[dict[str, Any]]:
    """Every note file found -> a plain plan (parsed topic dict + source
    harness), with no DB access at all, so `reconcile(dry_run=True)` and the
    real write share exactly one discovery pass."""
    plan: list[dict[str, Any]] = []

    for proj_dir in _claude_project_dirs(claude_projects_root()):
        files = _iter_note_files(proj_dir / "memory")
        if not files:
            continue
        project = _project_for_slug(proj_dir.name)
        for f in files:
            parsed = _note_topic_dict(f, project=project)
            if parsed is not None:
                plan.append({"harness": "claude_code", "parsed": parsed, "path": str(f)})

    for f in _iter_note_files(codex_memories_root()):
        parsed = _note_topic_dict(f, project=None)
        if parsed is not None:
            plan.append({"harness": "codex", "parsed": parsed, "path": str(f)})

    return plan


def reconcile(*, dry_run: bool = False) -> dict[str, Any]:
    """Mirror every harness-native note into ``topics``. Append-only:
    upserts only, never tombstones. Fail-open at the per-file level (one bad
    note is reported in ``errors`` and does not sink the batch) but lets a
    connection-level failure (no hub reachable at all) propagate — the
    caller (`cli.cmd_notes` interactively, `jobs` nightly) decides how to
    surface that, same posture as every other write path in this package.
    """
    plan = _build_plan()
    out: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "claude_projects_scanned": len(_claude_project_dirs(claude_projects_root())),
        "codex_root_found": codex_memories_root().is_dir(),
        "candidates": len(plan),
        "written": 0,
        "errors": [],
        "slugs": [p["parsed"]["slug"] for p in plan],
    }
    if dry_run or not plan:
        return out

    from khipu.db import connect
    from khipu.mirror import _upsert_topic

    with connect() as conn:
        with conn.cursor() as cur:
            for item in plan:
                try:
                    _upsert_topic(
                        cur,
                        item["parsed"],
                        item["path"],
                        source="notes-reconcile",
                        note=f"harness-native note ({item['harness']})",
                    )
                    out["written"] += 1
                except Exception as exc:  # noqa: BLE001 — one bad note must not sink the batch
                    out["errors"].append({"path": item["path"], "error": f"{type(exc).__name__}: {exc}"})
                    _log(f"upsert failed for {item['path']}: {exc}")
        conn.commit()
    return out
