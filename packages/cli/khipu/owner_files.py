"""Every file owner direction writes, found by pattern, never by name.

The recorder (``owner_turns``) writes into the capture home's owner-turns
folder; the check, the log and the widenings (``owner_direction``) write
beside the data folder's other files. Any of them can be left half written
by a crash, and every rewrite goes through a ``.<name>.<pid>.tmp`` file that
a crash can leave behind holding the owner's words. Forget and the expiry
sweep list these files here, by ``PATTERNS``, so a file kind added later is
still found: a file in the owner-turns folder that no pattern names is
listed as ``unknown`` and cleaned like a temp.

Not here: the log's own lines (``owner_direction.scrub_words``), the
widenings (``owner_direction.remove_old_widenings``) and the turn files' lines
(``owner_turns.forget_sessions``), which forget rewrites in place.
"""
from __future__ import annotations

import fnmatch
import os
import time
from pathlib import Path

from khipu import owner_turns

# A temp younger than this may belong to a rewrite still in progress; older
# ones are what a crash left.
TEMP_STALE_S = 60.0

# (kind, root, pattern): root "turns" is the owner-turns folder, "data" the
# data folder. The first pattern that matches a file names its kind.
PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("temp", "turns", ".*.tmp"),
    ("briefs", "turns", "briefs.jsonl"),
    ("settings", "turns", "settings.json"),
    ("temp", "turns", "occurrences/.*.tmp"),
    ("occurrences", "turns", "occurrences/*.json"),
    ("directions", "turns", "routing-directions.json"),
    # Persistent, empty flock inode: deleting it would let new writers
    # bypass a transaction still holding the old inode.
    ("lock", "turns", "routing-directions.lock"),
    ("mismatches", "turns", "routing-mismatch-*.json"),
    ("turns", "turns", "*.jsonl"),
    ("temp", "data", ".owner-*.tmp"),
    ("lock", "data", ".owner-*.lock"),
    ("log", "data", "owner-actions.jsonl"),
    ("head", "data", "owner-actions.head.json"),
    ("pending", "data", "owner-direction-pending.json"),
    ("unknown", "data", "owner-*"),
    ("unknown", "data", ".owner-*"),
    ("unknown", "turns", "*"),
)


def _roots() -> dict[str, Path]:
    from khipu.paths import data_dir

    return {"turns": owner_turns.folder(), "data": data_dir()}


def kind_of(path: Path) -> str | None:
    roots = _roots()
    for kind, root, pattern in PATTERNS:
        try:
            relative = path.relative_to(roots[root]).as_posix()
        except ValueError:
            continue
        if fnmatch.fnmatchcase(relative, pattern):
            return kind
    return None


def listed() -> list[Path]:
    """Every file under ``PATTERNS``. Raises OSError when a folder that
    exists can't be listed: an unlisted folder must never look empty."""
    out: list[Path] = []
    for name, root in _roots().items():
        if not root.exists():
            continue
        with os.scandir(root) as entries:
            names = sorted(e.name for e in entries if e.is_file(follow_symlinks=False))
        for entry in names:
            path = root / entry
            if any(r == name and fnmatch.fnmatchcase(entry, p) for _k, r, p in PATTERNS):
                out.append(path)
        sub = root / "occurrences"
        if name == "turns" and sub.exists():
            with os.scandir(sub) as entries:
                out += [sub / e.name for e in sorted(entries, key=lambda e: e.name)
                        if e.is_file(follow_symlinks=False)
                        and (fnmatch.fnmatchcase(e.name, "*.json") or fnmatch.fnmatchcase(e.name, ".*.tmp"))]
    return out


def _names_one(path: Path, sessions: set[str]) -> bool | None:
    """Whether the file's bytes name one of the sessions; None when it
    can't be read."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    text = raw.decode("utf-8", "replace")
    return any(s and s in text for s in sessions)


def clean_temps(sessions=None, now: float | None = None) -> dict:
    """Remove the rewrite temps (and unknown files) a crash left: every one
    older than ``TEMP_STALE_S``, and, for a forget (``sessions``), any that
    names one of the sessions or can't be read. A fresh one that names none
    of them may be another rewrite in progress: it is left, and ``left``
    counts it, so a forget stays open until it is gone."""
    now = time.time() if now is None else now
    names = {s for s in (sessions or ()) if s}
    out = {"removed": 0, "left": 0}
    turns = owner_turns.folder()
    for path in listed():
        kind = kind_of(path)
        # An unknown file beside the data folder's owner files may be
        # another feature's; only the owner-turns folder is owner
        # direction's alone.
        if not (kind == "temp" or (kind == "unknown" and path.parent == turns)):
            continue
        try:
            age = now - path.stat().st_mtime
        except FileNotFoundError:
            continue
        stale = age >= TEMP_STALE_S
        if not stale and names:
            named = _names_one(path, names)
            stale = named is not False
        if stale:
            path.unlink(missing_ok=True)
            out["removed"] += 1
        elif names:
            out["left"] += 1
    return out
