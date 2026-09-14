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

``reconcile()`` upserts every file it finds into ``topics`` and — since
Phase 4 (F5) — tombstones a ``note:`` topic whose ``source_path`` file no
longer exists on disk, bounded by the same circuit-breaker style the wiki
reconcile uses (never more than 20% of note topics in one run; see
``_tombstone_missing_notes``). ``changed_only=True`` (F1) skips any file
whose mtime/size match a small on-disk state file from the last run —
unchanged files cost one ``stat()``, nothing is read or parsed — so the
Stop hook can call this on every turn without re-walking hundreds of notes.
The nightly still calls the full (``changed_only=False``) reconcile. Never
runs against the live hub in a test: every test here injects a temp dir and
mocks ``khipu.db.connect`` with a fake cursor, same as every other write
path in this package.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NOTE_SLUG_PREFIX = "note:"
_WIKILINK_RE = re.compile(r"\[\[([a-z0-9][a-z0-9_-]*)\]\]", re.I)
# Bound on how many consecutive slug segments can fold into one path
# component (a directory name with a space in it) while resolving a
# Claude Code project slug back to a real path — see resolve_claude_project_path.
_MAX_JOIN_SEGMENTS = 4
# F5: never tombstone more than this share of live note: topics in one
# reconcile — a mount blip that makes every memory dir read as empty must
# not read as "every note was deleted" (same posture as the wiki reconcile's
# KHIPU_ALLOW_MASS_TOMBSTONE guard in khipu.mirror).
TOMBSTONE_MAX_FRACTION = 0.2


def _log(msg: str) -> None:
    print(f"[khipu-notes] {msg}", file=sys.stderr)


def claude_projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


def codex_memories_root() -> Path:
    return Path.home() / ".codex" / "memories"


def cursor_memory_roots() -> list[Path]:
    """Per-project Cursor memory dirs, F6. Checked live on the maintainer's
    Mac 2026-09-14: no ``~/.cursor/**/memory/*.md`` exists today — Cursor's
    own per-project state lives under ``~/.cursor/projects/<slug>/`` but
    that tree has no ``memory`` subdirectory yet. Empty root list for now;
    the scanner (``_iter_note_file_candidates``) and the WatchPaths agent both consult
    this function, so a directory that appears later is picked up with no
    further code change — just re-running ``khipu notes reconcile`` /
    ``khipu jobs install``."""
    root = Path.home() / ".cursor" / "projects"
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*/memory") if p.is_dir())


def aegis_memory_roots() -> list[Path]:
    """Aegis memory dirs, F6. Checked live 2026-09-14: Aegis sandboxes its
    own state and keeps no ``memory/*.md`` tree Khipu can reach from outside
    its sandbox today (see the ``screen-lease-protocol-with-aegis-sessions``
    / ``aegis-native-only`` memory topics — Aegis is deliberately kept off
    every Khipu-owned write path). Empty for now, same posture as
    ``cursor_memory_roots`` above: the code path exists so a future Aegis
    memory export needs no new plumbing, only a real root here."""
    return []


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


def _file_mtime_iso(path: Path) -> str | None:
    """A file's own mtime as an ISO-8601 UTC string, or None when it cannot
    be stat'd. Never ``now()`` — see R7."""
    try:
        ts = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _note_event_at(flat: dict[str, str], path: Path) -> str | None:
    """R7: a note's own ``event_at`` — the frontmatter's ``modified``
    (flat), else the nested ``metadata.modified``, else the file's own
    mtime. Never ``now()``: a note nobody touched today must not read as
    touched today just because Khipu happened to reconcile it today."""
    from khipu.mirror import _parse_frontmatter_date

    for key in ("modified", "metadata.modified"):
        val = _parse_frontmatter_date(flat.get(key))
        if val:
            return val
    return _file_mtime_iso(path)


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
    event_at = _note_event_at(flat, path)
    links = _extract_note_links(body)
    frontmatter = {
        "title": name,
        "status": status,
        "status_raw": type_raw or None,
        "links": links,
        "project": project,
        "note_source": str(path),
        # P5 G5: Claude Code's own note kind (feedback/user/project/reference)
        # — a DIFFERENT axis from `status` above (which `type_raw` also feeds,
        # for the rare note whose type string itself reads as a lifecycle
        # word, e.g. "shipped and wrapped"). Kept verbatim here so ranking
        # (khipu.recency.apply_project_and_status) can read it without
        # re-deriving it from status, which normalizes it away.
        "type": type_raw or None,
        # G1: the one-line summary rendered beside the title in the host
        # index (khipu.organise.rewrite_index) — Claude Code's own
        # generated MEMORY.md uses exactly this frontmatter field the same
        # way, so a Khipu rewrite reads the same as the host's own.
        "description": flat.get("description") or None,
    }
    # khipu.organise.split_note (G4) writes a `parent: <slug>` frontmatter
    # line on every child note it creates; carried through so a child's
    # provenance survives the mirror, not just its own file.
    parent = flat.get("parent")
    if parent:
        frontmatter["parent"] = parent
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
        "event_at": event_at,
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


def _iter_note_file_candidates() -> list[tuple[Path, str, str | None]]:
    """Every note file on disk -> ``(path, harness, claude_slug)``, no
    parsing and — deliberately — no project resolution: ``_project_for_slug``
    does a real filesystem DFS (``resolve_claude_project_path``) and is not
    cheap across dozens of projects. The one place that lists every memory
    dir the notes scanner knows — ``launchd_gen``'s WatchPaths render (F1)
    and ``notes_freshness`` (D4) both walk this same set so they can never
    drift from what ``_build_plan`` actually reconciles. ``claude_slug`` is
    the raw ``~/.claude/projects/<slug>`` directory name (None for
    codex/cursor/aegis); resolve it to a project lazily, only for a file
    you are about to actually parse — see ``_build_plan``."""
    out: list[tuple[Path, str, str | None]] = []
    for proj_dir in _claude_project_dirs(claude_projects_root()):
        files = _iter_note_files(proj_dir / "memory")
        out.extend((f, "claude_code", proj_dir.name) for f in files)
    out.extend((f, "codex", None) for f in _iter_note_files(codex_memories_root()))
    # F6: empty today on every checked Mac (see cursor_memory_roots /
    # aegis_memory_roots) — the loop runs regardless so a root that appears
    # later needs no code change here.
    for root in cursor_memory_roots():
        out.extend((f, "cursor", None) for f in _iter_note_files(root))
    for root in aegis_memory_roots():
        out.extend((f, "aegis", None) for f in _iter_note_files(root))
    return out


def memory_dirs() -> list[Path]:
    """Every memory *directory* the notes scanner reads from — for the
    WatchPaths LaunchAgent (F1), which watches directories, not the
    individual files inside them. Each Claude Code project's own
    ``memory/`` dir (not the ``~/.claude/projects`` parent: launchd's
    WatchPaths fires on changes to a listed path's own contents, not on a
    write several directories below it), Codex's single root, and any
    Cursor/Aegis roots that exist (F6, none today)."""
    dirs: list[Path] = []
    for proj_dir in _claude_project_dirs(claude_projects_root()):
        mem = proj_dir / "memory"
        if mem.is_dir():
            dirs.append(mem)
    codex = codex_memories_root()
    if codex.is_dir():
        dirs.append(codex)
    dirs.extend(cursor_memory_roots())
    dirs.extend(aegis_memory_roots())
    return dirs


def _state_path() -> Path:
    from khipu.paths import ensure_data_dir

    d = ensure_data_dir() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d / "notes-reconcile-state.json"


def _read_state() -> dict[str, Any]:
    """F1: ``{path: {mtime, size}}`` from the last ``changed_only`` run,
    plus ``last_reconcile_at`` — never raises; a missing/corrupt state file
    just means every file looks changed on the next call."""
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("files"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"last_reconcile_at": None, "files": {}}


def _write_state(state: dict[str, Any]) -> None:
    try:
        tmp = _state_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(_state_path())
    except OSError:
        pass


def _file_sig(path: Path) -> dict[str, float | int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return {"mtime": st.st_mtime, "size": st.st_size}


def _build_plan(
    *, changed_only: bool = False, state: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Every note file found -> a plain plan (parsed topic dict + source
    harness), with no DB access at all, so `reconcile(dry_run=True)` and the
    real write share exactly one discovery pass.

    ``changed_only=True`` (F1) costs one ``stat()`` per file and skips
    parsing (reading + hashing) any file whose mtime/size match ``state``
    (from ``_read_state()``) — this is what keeps the Stop hook's call
    cheap on a project with hundreds of untouched notes.
    """
    plan: list[dict[str, Any]] = []
    files_state = (state or {}).get("files", {}) if changed_only else {}
    # Memoized within this one call: several notes under the same Claude
    # Code project would otherwise each pay for _project_for_slug's real
    # filesystem DFS. Resolved lazily (only for a file that is actually
    # about to be parsed) so a changed_only run with nothing changed never
    # calls it at all — measured live: this dropped a nothing-changed Stop
    # hook run from ~450-500ms to well under 300ms across ~45 projects.
    project_cache: dict[str, str | None] = {}

    def _project_for(claude_slug: str | None) -> str | None:
        if claude_slug is None:
            return None
        if claude_slug not in project_cache:
            project_cache[claude_slug] = _project_for_slug(claude_slug)
        return project_cache[claude_slug]

    for path, harness, claude_slug in _iter_note_file_candidates():
        if changed_only:
            sig = _file_sig(path)
            if sig is None:
                continue
            prev = files_state.get(str(path))
            if prev and prev.get("mtime") == sig["mtime"] and prev.get("size") == sig["size"]:
                continue
        parsed = _note_topic_dict(path, project=_project_for(claude_slug))
        if parsed is not None:
            plan.append({
                "harness": harness, "parsed": parsed, "path": str(path),
                "claude_slug": claude_slug,
            })
    return plan


def _project_short(item: dict[str, Any]) -> str:
    """A short, filesystem-free identifier to namespace a colliding note slug
    by (G3): the resolved project string's last segment when known, else the
    raw ``~/.claude/projects/<slug>`` directory name's last '-'-joined
    segment, else the note file's own parent-of-parent directory name (the
    project dir itself, one level above ``memory/``). Never raises, never
    empty (falls back to "unknown")."""
    project = ((item.get("parsed") or {}).get("frontmatter") or {}).get("project")
    claude_slug = item.get("claude_slug")
    src = project or claude_slug
    if not src:
        try:
            src = Path(item["path"]).parent.parent.name
        except Exception:  # noqa: BLE001 — namespacing must never raise
            src = None
    seg = [s for s in re.split(r"[\\/]+", str(src or "").strip()) if s]
    last = seg[-1] if seg else str(src or "")
    short = last.strip("-").strip().lower() or "unknown"
    return short[:60]


def _resolve_collisions(plan: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """G3: a note's slug comes from its frontmatter ``name`` alone, so the
    same title under two different project dirs used to collide on
    ``ON CONFLICT (slug)`` — last write wins, silently overwriting a
    different note's content. This groups this run's plan by the slug
    ``_note_topic_dict`` already computed and, for any group spanning more
    than one candidate:

      - identical ``digest`` (the host slugged the same real project path
        two ways, so two-plus directories hold byte-identical copies) ->
        ingest ONE representative (deterministic: lowest source path),
        report the rest as ``duplicate_copies``, write nothing for them;
      - different ``digest`` under the same name from different projects ->
        a REAL collision: every item in the group is re-slugged to
        ``note:<project-short>/<name>`` so nothing overwrites anything else,
        and the caller (``reconcile``) writes a redirect row at the old bare
        slug (status ``superseded``, body naming the new slugs) once the
        re-slugged items have themselves been written.

    Returns ``(resolved_plan, report)``; ``resolved_plan`` is the same shape
    ``reconcile`` already upserts from (only ``parsed["slug"]``/frontmatter
    may have changed), ``report`` carries the counts plus the old->new slug
    map for the redirect-writing step.
    """
    by_slug: dict[str, list[dict[str, Any]]] = {}
    for item in plan:
        by_slug.setdefault(item["parsed"]["slug"], []).append(item)
    out: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "duplicate_copies": 0, "collisions_reslugged": 0, "redirects": [],
    }
    for base_slug, items in by_slug.items():
        if len(items) == 1:
            out.append(items[0])
            continue
        digests = {it["parsed"]["digest"] for it in items}
        if len(digests) == 1:
            # Same content under more than one directory: the host slugged
            # one real project path two ways. Keep exactly one candidate,
            # deterministically (lowest source path), so re-running this
            # pass always picks the same survivor.
            items_sorted = sorted(items, key=lambda it: it["path"])
            out.append(items_sorted[0])
            report["duplicate_copies"] += len(items) - 1
            continue
        # Real collision: different content under the same name from
        # different project dirs. Namespace every one of them so the next
        # write can never silently clobber a different note's revision.
        new_slugs: list[str] = []
        for it in items:
            proj_short = _project_short(it)
            name_part = base_slug[len(NOTE_SLUG_PREFIX):]
            new_slug = f"{NOTE_SLUG_PREFIX}{proj_short}/{name_part}"
            it["parsed"]["slug"] = new_slug
            it["parsed"]["frontmatter"]["renamed_from"] = base_slug
            new_slugs.append(new_slug)
            out.append(it)
        report["collisions_reslugged"] += 1
        report["redirects"].append({
            "old_slug": base_slug,
            "new_slugs": sorted(set(new_slugs)),
            # any one of the colliding items' own source paths — a redirect
            # row needs *a* source_path, not one belonging to either winner.
            "source_path": sorted(items, key=lambda it: it["path"])[0]["path"],
        })
    return out, report


def _redirect_topic_dict(old_slug: str, new_slugs: list[str]) -> dict[str, Any]:
    """G3: the topic dict for the tombstone-free redirect row a re-slugged
    collision leaves at its old bare slug — status ``superseded``, body
    naming every project-namespaced replacement, so a stale link or an old
    search result lands somewhere useful instead of a 404."""
    from khipu.mirror import topic_content_hash

    links = sorted(set(new_slugs))
    body = (
        "This note's name collided across more than one project and was "
        "split by project (G3): " + ", ".join(f"[[{s}]]" for s in links) + "\n"
    )
    return {
        "slug": old_slug,
        "title": f"{old_slug} (superseded — project collision)",
        "status": "superseded",
        "body": body,
        "digest": topic_content_hash(body),
        "links": links,
        "frontmatter": {
            "title": old_slug,
            "status": "superseded",
            "status_raw": "collision-redirect",
            "links": links,
        },
        "created_at": None,
        "updated_at": None,
        "event_at": None,
    }


def _tombstone_missing_notes(cur) -> dict[str, Any]:
    """F5: mark a ``note:`` topic ``deleted_at = now()`` when its
    ``source_path`` file no longer exists. Circuit-broken the same way the
    wiki reconcile is (``khipu.mirror.reconcile_from_files``'s mass-tombstone
    guard): never more than ``TOMBSTONE_MAX_FRACTION`` of live note topics in
    one run — a mount blip that makes every memory dir read as empty must
    not silently empty the corpus. Only the full (non ``changed_only``)
    reconcile calls this; it needs every live note topic's row, which a
    Stop-hook-cheap changed-only pass does not fetch."""
    cur.execute(
        "SELECT slug, source_path FROM topics WHERE slug LIKE %s AND deleted_at IS NULL",
        (NOTE_SLUG_PREFIX + "%",),
    )
    rows = cur.fetchall()
    total = len(rows)
    missing = [slug for slug, path in rows if not path or not Path(path).is_file()]
    out: dict[str, Any] = {"checked": total, "missing": len(missing), "tombstoned": 0, "skipped": False}
    if not missing:
        return out
    if len(missing) > 1 and len(missing) > total * TOMBSTONE_MAX_FRACTION:
        out["skipped"] = True
        out["reason"] = (
            f"{len(missing)}/{total} note topics have no source file on disk — "
            "refusing, looks like a mount blip rather than a real bulk delete"
        )
        return out
    for slug in missing:
        cur.execute(
            "UPDATE topics SET deleted_at = now() WHERE slug = %s AND deleted_at IS NULL",
            (slug,),
        )
    out["tombstoned"] = len(missing)
    return out


def _any_memory_root_exists() -> bool:
    """Whether at least one memory root is reachable at all — gates the
    tombstone sweep (F5): a host with NO reachable root (unconfigured, or a
    mount that vanished) must read the same as an empty plan always has —
    no DB touch, never a mass-tombstone from a blip that looks identical to
    "nothing here"."""
    if claude_projects_root().is_dir():
        return True
    if codex_memories_root().is_dir():
        return True
    return bool(cursor_memory_roots()) or bool(aegis_memory_roots())


def reconcile(*, dry_run: bool = False, changed_only: bool = False) -> dict[str, Any]:
    """Mirror every harness-native note into ``topics``.

    ``changed_only=True`` (F1) is the Stop-hook-cheap path: unchanged files
    (matched against the state file from the last run) cost one ``stat()``
    and are skipped entirely, and — since that is the whole point — a run
    that finds nothing changed never even opens a DB connection. The full
    (``changed_only=False``, what the nightly calls) reconcile also runs the
    F5 tombstone sweep (``_tombstone_missing_notes``), which needs every
    live note topic's row and so is not part of the cheap path. Fail-open at
    the per-file level (one bad note is reported in ``errors`` and does not
    sink the batch) but lets a connection-level failure (no hub reachable at
    all) propagate — the caller (`cli.cmd_notes` interactively, `jobs`
    nightly, the Stop hook, the WatchPaths agent) decides how to surface
    that, same posture as every other write path in this package.
    """
    state = _read_state() if changed_only else None
    raw_plan = _build_plan(changed_only=changed_only, state=state)
    # G3: resolve same-name-different-project collisions and duplicate-
    # directory copies BEFORE anything is written (and before dry_run's
    # preview) — see _resolve_collisions's own docstring.
    plan, collision_report = _resolve_collisions(raw_plan)
    out: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "changed_only": changed_only,
        "claude_projects_scanned": len(_claude_project_dirs(claude_projects_root())),
        "codex_root_found": codex_memories_root().is_dir(),
        "candidates": len(plan),
        "written": 0,
        "errors": [],
        "slugs": [p["parsed"]["slug"] for p in plan],
        "duplicate_copies": collision_report["duplicate_copies"],
        "collisions_reslugged": collision_report["collisions_reslugged"],
    }
    if dry_run:
        return out
    run_tombstone = (not changed_only) and _any_memory_root_exists()
    if not plan and not run_tombstone:
        if changed_only:
            _write_state({
                "last_reconcile_at": datetime.now(timezone.utc).isoformat(),
                "files": dict((state or {}).get("files", {})),
            })
        return out

    from khipu.db import connect
    from khipu.mirror import _upsert_topic

    new_files_state = dict((state or {}).get("files", {})) if changed_only else {}
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
                    if changed_only:
                        sig = _file_sig(Path(item["path"]))
                        if sig is not None:
                            new_files_state[item["path"]] = sig
                except Exception as exc:  # noqa: BLE001 — one bad note must not sink the batch
                    out["errors"].append({"path": item["path"], "error": f"{type(exc).__name__}: {exc}"})
                    _log(f"upsert failed for {item['path']}: {exc}")
            # G3: the old bare slug becomes a redirect row (never deleted —
            # a stale bookmark or a stale search hit still resolves).
            for redirect in collision_report["redirects"]:
                try:
                    redirect_parsed = _redirect_topic_dict(redirect["old_slug"], redirect["new_slugs"])
                    _upsert_topic(
                        cur, redirect_parsed, redirect["source_path"],
                        source="notes-reconcile", note="slug collision redirect (G3)",
                    )
                except Exception as exc:  # noqa: BLE001 — a redirect failure must not sink the batch
                    out["errors"].append({
                        "path": redirect["old_slug"], "error": f"{type(exc).__name__}: {exc}",
                    })
                    _log(f"redirect write failed for {redirect['old_slug']}: {exc}")
            if run_tombstone:
                out["tombstone"] = _tombstone_missing_notes(cur)
        conn.commit()
    if changed_only:
        _write_state({
            "last_reconcile_at": datetime.now(timezone.utc).isoformat(),
            "files": new_files_state,
        })
    try:
        from khipu import organise

        out["organise"] = organise.after_reconcile(
            written_plan=plan, changed_only=changed_only, dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001 — organisation must never break reconcile
        out["organise"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        _log(f"organise.after_reconcile failed: {exc}")
    return out


def notes_freshness(cur) -> dict[str, Any]:
    """D4: can `khipu status`/`khipu_status` tell notes are stale? Compares
    the newest note file's own mtime on disk against the newest
    ``event_at`` already mirrored into ``note:`` topics, plus when a
    (full or changed-only) reconcile last ran. ``cur`` is a live hub
    cursor — the caller (``drift.status_payload``) already holds one."""
    newest_mtime: float | None = None
    for path, _harness, _claude_slug in _iter_note_file_candidates():
        try:
            m = path.stat().st_mtime
        except OSError:
            continue
        if newest_mtime is None or m > newest_mtime:
            newest_mtime = m
    cur.execute(
        "SELECT MAX(event_at) FROM topics WHERE slug LIKE %s AND deleted_at IS NULL",
        (NOTE_SLUG_PREFIX + "%",),
    )
    row = cur.fetchone()
    newest_topic_event_at = row[0] if row else None
    state = _read_state()
    return {
        "newest_note_mtime": (
            datetime.fromtimestamp(newest_mtime, tz=timezone.utc).isoformat()
            if newest_mtime is not None else None
        ),
        "newest_note_topic_event_at": (
            newest_topic_event_at.isoformat()
            if hasattr(newest_topic_event_at, "isoformat") else newest_topic_event_at
        ),
        "notes_last_reconcile_at": state.get("last_reconcile_at"),
    }


def backfill_event_at() -> dict[str, Any]:
    """R7, one-time (idempotent): existing ``note:`` topics written before
    this phase have no ``event_at`` yet. Re-parses every note file on disk
    and fills in ``event_at`` for its topic row, ONLY where the row's
    ``event_at`` is still null — safe to re-run any time, and a no-op once
    every note topic has been reconciled at least once post-migration
    (ordinary ``reconcile()`` upserts set ``event_at`` going forward)."""
    from khipu.db import connect

    plan = _build_plan()
    out: dict[str, Any] = {"ok": True, "candidates": len(plan), "updated": 0}
    if not plan:
        return out
    with connect() as conn:
        with conn.cursor() as cur:
            for item in plan:
                parsed = item["parsed"]
                cur.execute(
                    "UPDATE topics SET event_at = %s::timestamptz "
                    "WHERE slug = %s AND event_at IS NULL",
                    (parsed.get("event_at"), parsed["slug"]),
                )
                out["updated"] += cur.rowcount or 0
        conn.commit()
    return out
