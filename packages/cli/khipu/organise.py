"""Organisation without the user (Phase 5, G1/G2/G4/G6 orchestration).

Per the plan's Phase 5 paragraph: everything here runs BY ITSELF — the index
rewrite, size warnings, and the stale report all fire from
``khipu.notes.reconcile`` (which ``after_reconcile`` below is called from, at
the end of every reconcile: the Stop hook, the ``com.khipu.notes-watch``
WatchPaths agent, and the nightly) and, separately, section-aware re-embedding
is just ``khipu.embed.backfill`` picking up the changed sections next sweep.
The ``khipu notes index/split/stale/supersede`` CLI verbs exist for the
maintainer to run by hand — they are never the ONLY way any of this runs.

G3 (slug collisions) lives in ``khipu.notes`` itself (it has to run inline
with the upsert, not after it). G6 (recall hit counts) lives in
``khipu.topic_hits``. This module owns:

  - ``rewrite_index`` (G1) — keep a host's memory-dir ``MEMORY.md`` under its
    load cap by ranking note lines (type, hits_30d, event_at) and moving the
    overflow to ``_overflow_index.md``, which recall (not the host) serves.
  - ``size_warnings`` (G2) — any note file over ``NOTE_SIZE_WARN_BYTES``.
  - ``split_note`` (G2/G4) — split an oversized ledger by top-level ``## ``
    section into dated child notes, replacing the parent body with a
    section list of ``[[links]]``.
  - ``stale_report`` (G2) — report-only: note topics with no recent hits,
    old ``event_at``, and a status/type that doesn't exempt them.
  - ``supersede`` (R6/G5) — ``khipu notes supersede OLD NEW``.
  - ``after_reconcile`` — the auto-run orchestrator; writes the evidence
    file ``khipu doctor`` reads (``last_run``).
"""
from __future__ import annotations

import fcntl
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

NOTE_SIZE_WARN_BYTES = 50 * 1024
STALE_DAYS = 90
DEFAULT_INDEX_CAP_BYTES = 20_000
MAX_DROP_FRACTION = 0.30
OVERFLOW_NAME = "_overflow_index.md"
INDEX_NAME = "MEMORY.md"
# G5: lower rank number = kept first when the index is trimmed to fit the cap.
_TYPE_RANK = {"feedback": 0, "user": 1, "project": 2, "reference": 3}
_INDEX_LINE_RE = re.compile(r"^- \[")
# G2's fix text points at this exact command, so any doctor/report caller
# that wants the literal instruction has one place to read it from.
SPLIT_FIX_TEMPLATE = "khipu notes split {path}"


def _log(msg: str) -> None:
    import sys

    print(f"[khipu-organise] {msg}", file=sys.stderr)


def _evidence_path() -> Path:
    from khipu.paths import ensure_data_dir

    d = ensure_data_dir() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d / "notes-organise-last.json"


def last_run() -> dict[str, Any] | None:
    """G1/G2 doctor row: the last organisation run's summary, or None when
    none has run yet on this Mac. Never raises; a corrupt evidence file
    reads as None, same as a missing one."""
    try:
        data = json.loads(_evidence_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_evidence(payload: dict[str, Any]) -> None:
    try:
        tmp = _evidence_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
        tmp.replace(_evidence_path())
    except OSError:
        pass


# --------------------------------------------------------------------------
# G1 — index under the host's load cap
# --------------------------------------------------------------------------


def _is_khipu_recognised_index(text: str) -> bool:
    """Never touch a file that isn't shaped like a generated note index — at
    least one ``- [`` bullet line, the same shape Claude Code's own
    ``MEMORY.md`` writer uses. An empty/missing file is fine to write (it
    becomes one)."""
    return any(_INDEX_LINE_RE.match(line) for line in text.splitlines())


def _split_preamble(text: str) -> tuple[str, list[str]]:
    """Hand-written text above the FIRST ``- [`` bullet line is the preamble
    (preserved verbatim on rewrite); every line from there down is the
    generated list."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _INDEX_LINE_RE.match(line):
            preamble = "\n".join(lines[:i]).rstrip("\n")
            return preamble, lines[i:]
    return text.rstrip("\n"), []


def _index_candidates(memory_dir: Path, cur=None) -> list[dict[str, Any]]:
    """Every note in ``memory_dir`` -> the fields ``rewrite_index`` ranks and
    renders by. Parsed straight off disk (the one parser, ``khipu.notes.
    _note_topic_dict``) rather than requiring every note to already be
    mirrored into ``topics`` — an index rewrite must work even on a hub
    that's behind. ``hits_30d`` joins in from ``topic_hits`` when ``cur`` is
    given (dry-run callers with no DB access still rank on type + event_at)."""
    from khipu.notes import _iter_note_files, _note_topic_dict

    out: list[dict[str, Any]] = []
    for path in _iter_note_files(memory_dir):
        parsed = _note_topic_dict(path, project=None)
        if parsed is None:
            continue
        fm = parsed.get("frontmatter") or {}
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        out.append({
            "path": path,
            "slug": parsed["slug"],
            "title": parsed["title"],
            "type": fm.get("type"),
            "description": fm.get("description") or "",
            "event_at": parsed.get("event_at"),
            "hits_30d": 0,
            "bytes": size,
        })
    if cur is not None and out:
        slugs = [c["slug"] for c in out]
        try:
            cur.execute(
                "SELECT slug, hits_30d FROM topic_hits WHERE slug = ANY(%s)", (slugs,)
            )
            hits = {slug: int(h or 0) for slug, h in cur.fetchall()}
            for c in out:
                c["hits_30d"] = hits.get(c["slug"], 0)
        except Exception as exc:  # noqa: BLE001 — ranking degrades, never breaks
            _log(f"hits_30d join skipped: {type(exc).__name__}: {exc}")
    return out


def _rank_key(c: dict[str, Any]) -> tuple:
    """G1/G5: type (feedback/user ahead of project/reference), then hits_30d
    descending, then event_at descending — the three signals named in the
    Phase 5 plan paragraph, in that priority order."""
    type_rank = _TYPE_RANK.get(str(c.get("type") or "").strip().lower(), 4)
    return (type_rank, -int(c.get("hits_30d") or 0), -_event_epoch(c.get("event_at")))


def _event_epoch(event_at: Any) -> float:
    try:
        s = str(event_at).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except (TypeError, ValueError):
        return 0.0


_LINE_TARGET_RE = re.compile(r"^- \[[^\]]*\]\(([^)]+)\)")


def _parse_existing_lines(existing_lines: list[str]) -> dict[str, str]:
    """G1 incident (2026-09-14): filename (the ``(file.md)`` link target) ->
    the FULL existing bullet line, verbatim. This is what makes a rewrite
    non-destructive — a note that already has a line keeps that EXACT line
    (hand-edited text included), no matter what its frontmatter says today;
    only a note with no line yet gets one freshly rendered. Only ``- [``
    lines are indexed (anything else should not appear here — the preamble
    was already split off)."""
    out: dict[str, str] = {}
    for ln in existing_lines:
        m = _LINE_TARGET_RE.match(ln)
        if m:
            out[m.group(1)] = ln
    return out


def _unescape_yaml_dquote(s: str) -> str:
    """G1 incident: a frontmatter value's outer quote is stripped by
    ``khipu.notes._parse_note_frontmatter`` (one leading/trailing char, not a
    real YAML unescape) — an internal ``\\"`` from a double-quoted YAML
    scalar survives as a literal backslash-quote pair. Never let that escape
    artifact reach a rendered index line."""
    return (s or "").replace('\\"', '"')


def _render_line(c: dict[str, Any]) -> str:
    """A FRESH line, rendered from frontmatter — used ONLY for a note that
    has no existing line to preserve (see ``_parse_existing_lines`` /
    ``_line_for``)."""
    rel = c["path"].name
    title = _unescape_yaml_dquote(c["title"])
    desc = _unescape_yaml_dquote(c.get("description") or "")
    desc_part = f" — {desc}" if desc else ""
    return f"- [{title}]({rel}){desc_part}"


def _line_for(c: dict[str, Any], existing_by_target: dict[str, str]) -> str:
    """The line to emit for one candidate: the existing line verbatim when
    one already names this file, else a freshly rendered one. This is the
    ONE place text is chosen, so ranking (which only decides ORDER, via
    ``_rank_key``) can never accidentally carry a text rewrite with it."""
    existing = existing_by_target.get(c["path"].name)
    return existing if existing is not None else _render_line(c)


BACKUP_KEEP = 20


def _project_slug_for_dir(memory_dir: Path) -> str:
    """A filesystem-safe folder name for this memory dir's backups — the
    Claude Code project slug (``memory_dir.parent.name``) for the common
    ``<project>/memory`` shape, else the dir's own name (codex's single
    ``~/.codex/memories`` root, or any future non-nested root)."""
    name = memory_dir.parent.name if memory_dir.name == "memory" else memory_dir.name
    return name or "unknown"


def _backup_index(memory_dir: Path, existing_text: str) -> Path | None:
    """G1 incident (2026-09-14): a copy of the index BEFORE any rewrite —
    never inside the memory dir itself (that's the file being protected).
    Lives at ``<khipu data dir>/index-backups/<project-slug>/
    MEMORY.md.<UTC timestamp>``; only the last ``BACKUP_KEEP`` are kept per
    project. Never raises — a failed backup is logged and returns None, and
    the caller treats that as "do not proceed with the write" (see
    ``rewrite_index``: a None backup_path when one was expected is visible
    in the run result / doctor row, not silently swallowed).
    """
    from khipu.paths import ensure_data_dir

    try:
        backup_dir = ensure_data_dir() / "index-backups" / _project_slug_for_dir(memory_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_dir / f"{INDEX_NAME}.{ts}"
        # A same-second retry (test loops, a rapid double-fire) must not
        # silently clobber the previous backup — suffix with an attempt
        # counter rather than overwrite.
        n = 1
        while backup_path.exists():
            n += 1
            backup_path = backup_dir / f"{INDEX_NAME}.{ts}.{n}"
        backup_path.write_text(existing_text, encoding="utf-8")
        existing_backups = sorted(backup_dir.glob(f"{INDEX_NAME}.*"))
        for stale in existing_backups[: max(0, len(existing_backups) - BACKUP_KEEP)]:
            try:
                stale.unlink()
            except OSError:
                pass
        return backup_path
    except OSError as exc:  # noqa: BLE001 — a failed backup must be visible, not silently swallowed
        _log(f"index backup failed for {memory_dir}: {exc}")
        return None


def rewrite_index(
    memory_dir: Path, *, cap_bytes: int = DEFAULT_INDEX_CAP_BYTES, dry_run: bool = False,
    force: bool = False, cur=None,
) -> dict[str, Any]:
    """G1: rewrite ``memory_dir/MEMORY.md`` from the note files' own
    frontmatter, ranked by (type, hits_30d, event_at), keeping the top lines
    under ``cap_bytes``; the rest go to ``_overflow_index.md`` in the same
    dir (never loaded by the host — served through recall/search instead).

    Refuses (never writes) when: the existing index isn't Khipu-recognised
    (no ``- [`` lines — could be a maintainer's own file); the rewrite would
    drop more than ``MAX_DROP_FRACTION`` of the existing index's lines and
    ``force`` is false; or another rewrite of this same dir is already in
    flight (an advisory flock on ``.khipu-index.lock``, so the Stop hook and
    the WatchPaths agent can never race each other onto the same file).
    """
    index_path = memory_dir / INDEX_NAME
    overflow_path = memory_dir / OVERFLOW_NAME
    existing_text = ""
    if index_path.is_file():
        try:
            existing_text = index_path.read_text(encoding="utf-8")
        except OSError:
            existing_text = ""
    existing_overflow_text = ""
    if overflow_path.is_file():
        try:
            existing_overflow_text = overflow_path.read_text(encoding="utf-8")
        except OSError:
            existing_overflow_text = ""
    existing_had_index = bool(existing_text.strip())
    if existing_had_index and not _is_khipu_recognised_index(existing_text):
        return {
            "ok": True, "skipped": True, "dir": str(memory_dir),
            "reason": "existing MEMORY.md has no '- [' bullet lines — not a Khipu-recognised index",
        }
    lock_path = memory_dir / ".khipu-index.lock"
    try:
        lock_fh = open(lock_path, "a+")
    except OSError:
        lock_fh = None
    if lock_fh is not None:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_fh.close()
            return {"ok": True, "skipped": True, "dir": str(memory_dir), "reason": "another index rewrite is already in flight"}
    try:
        preamble, existing_lines = _split_preamble(existing_text)
        existing_bullet_count = sum(1 for ln in existing_lines if _INDEX_LINE_RE.match(ln))
        # G1 incident (2026-09-14): every line whose note is still present
        # keeps its EXACT existing text — ranking below only ever decides
        # ORDER, never text. _overflow_index.md is Khipu's own generated
        # file (never host-loaded, never meant for hand-editing) but is
        # matched the same way for consistency and so an unchanged overflow
        # also counts as a true no-op below.
        existing_by_target = _parse_existing_lines(existing_lines)
        _, existing_overflow_lines = _split_preamble(existing_overflow_text)
        existing_by_target.update(_parse_existing_lines(existing_overflow_lines))

        candidates = _index_candidates(memory_dir, cur=cur)
        candidates.sort(key=_rank_key)

        preamble_block = (preamble + "\n\n") if preamble else ""
        kept: list[dict[str, Any]] = []
        overflow: list[dict[str, Any]] = []
        total = len((preamble_block).encode("utf-8"))
        for c in candidates:
            line_bytes = len(_line_for(c, existing_by_target).encode("utf-8")) + 1
            if kept and total + line_bytes > cap_bytes:
                overflow.append(c)
                continue
            kept.append(c)
            total += line_bytes

        # "Dropped" is measured against the OLD visible index, not against
        # today's candidate count: an old index naming 10 notes when only 1
        # note file exists today (9 deleted/moved out from under it) must
        # refuse just as loudly as a cap suddenly shrinking what fits.
        dropped = max(0, existing_bullet_count - len(kept))
        if existing_bullet_count and not force:
            drop_fraction = dropped / existing_bullet_count if existing_bullet_count else 0.0
            if drop_fraction > MAX_DROP_FRACTION:
                return {
                    "ok": True, "skipped": True, "dir": str(memory_dir),
                    "reason": (
                        f"rewrite would drop {dropped}/{existing_bullet_count} "
                        f"({drop_fraction:.0%}) existing index lines without --force"
                    ),
                    "lines_before": existing_bullet_count, "would_keep": len(kept),
                    "would_move_to_overflow": len(overflow),
                }

        new_body = "\n".join(_line_for(c, existing_by_target) for c in kept)
        new_text = preamble_block + new_body + ("\n" if new_body else "")
        overflow_text = (
            "\n".join(_line_for(c, existing_by_target) for c in overflow) + ("\n" if overflow else "")
        )

        # G1 incident: every kept/overflow line's TEXT is now always either
        # preserved verbatim or freshly rendered for a genuinely new note —
        # so the only way new_text/overflow_text can differ from what is
        # already on disk is a real membership or order change. When
        # neither differs this is a true no-op: no write, no backup, no
        # mtime bump.
        changed = new_text != existing_text or overflow_text != existing_overflow_text
        summary = {
            "ok": True, "skipped": False, "dir": str(memory_dir), "changed": changed,
            "lines_before": existing_bullet_count, "lines_kept": len(kept),
            "lines_moved_to_overflow": len(overflow),
            "bytes_before": len(existing_text.encode("utf-8")),
            "bytes_after": len(new_text.encode("utf-8")),
            "cap_bytes": cap_bytes, "dry_run": dry_run, "backup_path": None,
        }
        if dry_run or not changed:
            return summary

        if existing_had_index:
            backup_path = _backup_index(memory_dir, existing_text)
            if backup_path is None:
                # A backup was owed (there is existing text to protect) and
                # failed — refuse the write rather than proceed unprotected.
                return {
                    "ok": True, "skipped": True, "dir": str(memory_dir),
                    "reason": "refusing to rewrite: backup of the existing index failed",
                }
            summary["backup_path"] = str(backup_path)

        tmp = index_path.with_suffix(".md.tmp")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(index_path)
        if overflow:
            otmp = overflow_path.with_suffix(".md.tmp")
            otmp.write_text(overflow_text, encoding="utf-8")
            otmp.replace(overflow_path)
        elif overflow_path.is_file():
            try:
                overflow_path.unlink()
            except OSError:
                pass
        return summary
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            lock_fh.close()


# --------------------------------------------------------------------------
# G2 — note size policy + split
# --------------------------------------------------------------------------


def size_warnings(memory_dirs: list[Path]) -> list[dict[str, Any]]:
    """G2: every note file over ``NOTE_SIZE_WARN_BYTES``, with the doctor fix
    text. Cheap (one ``stat()`` per file); never raises."""
    from khipu.notes import _iter_note_files

    out: list[dict[str, Any]] = []
    for memory_dir in memory_dirs:
        for path in _iter_note_files(memory_dir):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > NOTE_SIZE_WARN_BYTES:
                out.append({
                    "path": str(path), "bytes": size,
                    "fix": SPLIT_FIX_TEMPLATE.format(path=path),
                })
    return sorted(out, key=lambda w: -w["bytes"])


_SECTION_HEADING_RE = re.compile(r"(?m)^## [^\n]*$")


def split_note(path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """G2/G4: split one oversized note by top-level ``## `` section into
    ``<stem>--<section-slug>.md`` child notes carrying the parent's
    frontmatter plus ``parent: <parent-name>``; the parent's body becomes a
    section list of ``[[links]]`` to its children. Returns without writing
    anything when the note has fewer than two ``## `` sections (nothing to
    split) or when ``dry_run``. Re-reconciling (so the split lands in
    ``topics`` immediately) is the CALLER's job — ``cli.cmd_notes`` does it
    right after a real (non-dry-run) split.
    """
    from khipu.notes import _parse_note_frontmatter
    from khipu.topic_graph import topic_slug_from_label

    if not path.is_file():
        return {"ok": False, "error": f"no such file: {path}"}
    text = path.read_text(encoding="utf-8")
    flat, body = _parse_note_frontmatter(text)
    matches = list(_SECTION_HEADING_RE.finditer(body))
    if len(matches) < 2:
        return {"ok": True, "skipped": True, "reason": "fewer than two '## ' sections — nothing to split"}

    name = flat.get("name") or path.stem
    stem = path.stem
    parent_type = flat.get("metadata.type") or flat.get("type") or "project"
    parent_modified = flat.get("metadata.modified") or flat.get("modified") or ""

    sections: list[tuple[str, str, str]] = []  # (heading, slug, text)
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        heading = body[m.start() : m.end()][3:].strip()
        slug = topic_slug_from_label(heading) or f"section-{i}"
        sections.append((heading, slug, body[start:end].strip() + "\n"))

    children: list[dict[str, str]] = []
    for heading, slug, section_text in sections:
        child_stem = f"{stem}--{slug}"
        child_name = f"{name} — {heading}"
        child_fm = (
            "---\n"
            f"name: {child_name}\n"
            f'description: "Split from {name} (G4)"\n'
            "metadata:\n"
            f"  node_type: memory\n"
            f"  type: {parent_type}\n"
            f"  modified: {parent_modified}\n"
            f"  parent: {stem}\n"
            "---\n\n"
        )
        children.append({
            "path": str(path.parent / f"{child_stem}.md"),
            "text": child_fm + section_text,
            "slug_hint": child_stem,
            "heading": heading,
        })

    parent_body = "\n".join(f"- [[{Path(c['path']).stem}]] — {c['heading']}" for c in children) + "\n"
    parent_text = text[: text.index(body)] + parent_body if body in text else parent_body
    # If the note had no frontmatter block at all, text.index(body) == 0 and
    # the line above degenerates to parent_body alone — correct either way.

    out: dict[str, Any] = {
        "ok": True, "dry_run": dry_run, "parent": str(path),
        "children": [c["path"] for c in children],
    }
    if dry_run:
        return out

    for c in children:
        Path(c["path"]).write_text(c["text"], encoding="utf-8")
    path.write_text(parent_text, encoding="utf-8")
    return out


# --------------------------------------------------------------------------
# G2 — stale-note report (report-only, never gates doctor)
# --------------------------------------------------------------------------


def stale_report(cur, *, project: str | None = None) -> dict[str, Any]:
    """G2: note topics with no hits in ``STALE_DAYS`` days, ``event_at``
    older than ``STALE_DAYS`` days, and a status/type that does not exempt
    them — evergreen status, or feedback/user type (a hard-won lesson or an
    explicit instruction is worth keeping regardless of recency). Report
    only: doctor shows the count as info, never red — ported in spirit from
    the legacy nightly's ``_prune_stale_topic_pages`` (savannah-os
    consolidate_nightly.py), which also only reported its bare-zombie
    clusters rather than auto-archiving them.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_DAYS)
    # NOTE_SLUG_PREFIX + '%' is bound as a PARAMETER, never inlined into the
    # SQL text — a literal '%' in the query string is a psycopg placeholder
    # character, so `LIKE 'note:%'` written straight into the SQL raises
    # "only '%s', '%b', '%t' are allowed as placeholders" at execute time.
    params: list[Any] = ["note:%", cutoff.isoformat()]
    project_clause = ""
    if project:
        project_clause = " AND t.frontmatter->>'project' = %s"
        params.append(project)
    cur.execute(
        f"""
        SELECT t.slug, t.title, t.event_at, t.status, t.frontmatter->>'type',
               COALESCE(h.hits_30d, 0)
        FROM topics t
        LEFT JOIN topic_hits h ON h.slug = t.slug
        WHERE t.slug LIKE %s AND t.deleted_at IS NULL
          AND (t.event_at IS NULL OR t.event_at < %s::timestamptz)
          AND COALESCE(h.hits_30d, 0) = 0
          AND t.status <> 'evergreen'
          AND COALESCE(t.frontmatter->>'type', '') NOT IN ('feedback', 'user')
          {project_clause}
        ORDER BY t.event_at ASC NULLS FIRST
        """,
        params,
    )
    rows = [
        {"slug": slug, "title": title, "event_at": event_at, "status": status, "type": note_type, "hits_30d": hits}
        for slug, title, event_at, status, note_type, hits in cur.fetchall()
    ]
    return {"ok": True, "days": STALE_DAYS, "count": len(rows), "notes": rows}


# --------------------------------------------------------------------------
# R6/G5 — supersede
# --------------------------------------------------------------------------


def supersede(cur, old_slug: str, new_slug: str) -> dict[str, Any]:
    """``khipu notes supersede OLD NEW`` — sets OLD's status to superseded
    and points ``superseded_by`` at NEW. Refuses when OLD doesn't exist or
    is already deleted; NEW is not required to exist yet (it may be about
    to be written in the same session)."""
    cur.execute("SELECT slug FROM topics WHERE slug = %s AND deleted_at IS NULL", (old_slug,))
    if not cur.fetchone():
        return {"ok": False, "error": f"no live topic at slug {old_slug!r}"}
    cur.execute(
        "UPDATE topics SET status = 'superseded', superseded_by = %s WHERE slug = %s",
        (new_slug, old_slug),
    )
    return {"ok": True, "old_slug": old_slug, "new_slug": new_slug}


# --------------------------------------------------------------------------
# Orchestration — the "runs by itself" hook (called from khipu.notes.reconcile)
# --------------------------------------------------------------------------


def after_reconcile(*, written_plan: list[dict[str, Any]], changed_only: bool, dry_run: bool) -> dict[str, Any]:
    """Called from the end of ``khipu.notes.reconcile`` (never called
    directly by a human — see the module docstring) so the index rewrite,
    size warnings and stale report run automatically from every trigger that
    already calls reconcile: the Stop hook, the ``com.khipu.notes-watch``
    WatchPaths agent, and the nightly.

    ``changed_only=True`` (Stop hook / WatchPaths) only rewrites the index
    for the FEW memory dirs this run actually touched — cheap. A full
    (nightly) reconcile's plan already spans every note file on disk, so the
    dirs it touches are every memory dir there is, and this also refreshes
    the heavier, report-only evidence (size warnings, stale report, hit
    aggregation) that doctor reads.
    """
    if dry_run:
        return {"ok": True, "skipped": True, "reason": "dry_run"}
    dirs = sorted({Path(item["path"]).parent for item in written_plan})
    out: dict[str, Any] = {
        "ok": True, "ts": datetime.now(timezone.utc).isoformat(),
        "changed_only": changed_only, "dirs_touched": len(dirs),
        "index": [], "hits": None, "size_warnings": None, "stale": None,
    }
    try:
        from khipu.db import connect

        with connect() as conn:
            with conn.cursor() as cur:
                for d in dirs:
                    try:
                        out["index"].append(rewrite_index(d, dry_run=False, cur=cur))
                    except Exception as exc:  # noqa: BLE001 — one dir must not sink the rest
                        out["index"].append({"ok": False, "dir": str(d), "error": f"{type(exc).__name__}: {exc}"})
                from khipu import topic_hits

                if changed_only:
                    out["hits"] = topic_hits.apply_incremental(cur)
                else:
                    out["hits"] = topic_hits.recompute(cur)
                    out["size_warnings"] = size_warnings(dirs)
                    out["stale"] = stale_report(cur)
            conn.commit()
    except Exception as exc:  # noqa: BLE001 — organisation must never break reconcile
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
    # Merge onto the previous evidence so a changed_only run (which skips the
    # heavier stale/size passes) doesn't blank out the last full run's
    # numbers — doctor should keep showing the last real stale count, not
    # None, between nightlies.
    prev = last_run() or {}
    merged = dict(prev)
    merged.update({k: v for k, v in out.items() if v is not None})
    for k in ("size_warnings", "stale"):
        if out.get(k) is None and prev.get(k) is not None:
            merged[k] = prev[k]
    _write_evidence(merged)
    return out
