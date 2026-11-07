"""Retention for what Khipu keeps per session and in its archives.

Called from ``khipu.storage.sweep`` (the replica job's daily pass and the
nightly). Deletions are by exact name pattern and age only, through a
descriptor for the folder opened without following symlinks; a symlink, a
directory or any name that no pattern names is never touched, and nothing
here leaves Khipu's own folders.

- ``prune_session_state``: the capture home's ``state/`` folder gets a
  ``<harness>--<session>.json`` (offsets) with a ``.tmp`` orphan a killed save
  can leave, ``recall--<session>.json`` (dedup), a ``.capture-now.json``
  request and a ``.lock`` per session, and a ``title-cache--<digest>.json`` per
  conversation, and nothing removed them. A group goes when every file in it
  is older than the keep window and its lock is free (a held lock means the
  session is active). The delivery ledgers belong to ``routing_ledger_prune``
  and their ``.routing.lock`` files are never removed (see its docstring: two
  hooks could end up holding different inodes as "the" lock).
- ``prune_archives``: ``empty/`` (claimed capture jobs that yielded no episode,
  and the ``.error`` notes of quarantined ones) and the outbox ``quarantine/``,
  aged by the later of a file's modify and change time: a rename keeps the
  modify time of an old job but stamps a new change time, and the retention
  runs from when it was archived.
  Outbox ``resolved/`` is not touched: ``outbox.is_resolved_payload`` reads those
  records to keep a swept line from being queued again.
- ``held_counts``: work that needs the owner (outbox ``dead/``, held capture
  jobs) is never removed; ``warnings`` says so past a count.
"""
from __future__ import annotations

import fcntl
import os
import re
import stat
import time
from pathlib import Path
from typing import Any

TIME_BUDGET_S = 120
HELD_WARN_COUNT = 25
_STALE_LOCK_DIR_S = 60  # session_capture._path_lock takes over a directory lock this old

_GROUP = re.compile(r"^(?P<stem>(?P<prefix>[A-Za-z0-9_.\-]+?)--.+?)(?P<kind>\.routing|\.capture-now)?"
                    r"\.(?P<ext>json|lock|tmp)$")
_TITLE_CACHE = re.compile(r"^title-cache--[A-Za-z0-9_.\-]+\.json$")
_ARCHIVE_NAME = re.compile(r"^[^/]+\.(json|error)$")
_O_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW


def _session_prefixes() -> frozenset[str]:
    from khipu import session_capture

    return frozenset((*session_capture.HARNESSES, "recall"))


def _seen_ts(dfd: int, name: str) -> float:
    from khipu import session_capture

    try:
        import json

        text = session_capture._read_state_at(dfd, name)
        st = json.loads(text) if text is not None else None
        return float(st.get("seen_ts") or 0) if isinstance(st, dict) else 0.0
    except (ValueError, TypeError, OSError):
        return 0.0


def _unlink(dfd: int, name: str, out: dict[str, Any], *, directory: bool = False) -> bool:
    try:
        size = os.stat(name, dir_fd=dfd, follow_symlinks=False).st_size
        if directory:
            os.rmdir(name, dir_fd=dfd)
        else:
            os.unlink(name, dir_fd=dfd)
    except FileNotFoundError:
        return False
    except OSError:
        out["errors"] += 1
        return False
    out["removed"] += 1
    out["freed_bytes"] += 0 if directory else size
    return True


def _group_old(dfd: int, group: dict[str, Any], cutoff: float, now: float, out: dict[str, Any]) -> None:
    ledger, lock, kind = group.get("json"), group.get("lock"), group["kind"]
    orphan = group.get("tmp")  # a save_state rewrite a killed hook left behind
    members = [m for m in (ledger, lock, orphan) if m is not None]
    if max(st.st_mtime for _name, st in members) >= cutoff:
        return
    if ledger is not None and kind == "" and _seen_ts(dfd, ledger[0]) >= cutoff:
        return
    out["candidates"] += 1
    held = None
    if lock is not None:
        lock_name, lock_st = lock
        if stat.S_ISDIR(lock_st.st_mode):
            if now - lock_st.st_mtime <= _STALE_LOCK_DIR_S:
                out["busy"] += 1
                return
        elif stat.S_ISREG(lock_st.st_mode):
            try:
                held = os.open(lock_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(held)
                out["busy"] += 1
                return
            except OSError:
                if held is not None:
                    os.close(held)
                out["errors"] += 1
                return
        else:
            lock = None
    try:
        if ledger is not None:
            name, st = ledger
            if not stat.S_ISREG(st.st_mode):
                return
            try:
                if os.stat(name, dir_fd=dfd, follow_symlinks=False).st_mtime >= cutoff:
                    return  # written since the listing
            except FileNotFoundError:
                pass
            _unlink(dfd, name, out)
        if orphan is not None and stat.S_ISREG(orphan[1].st_mode):
            try:
                if os.stat(orphan[0], dir_fd=dfd, follow_symlinks=False).st_mtime < cutoff:
                    _unlink(dfd, orphan[0], out)
            except FileNotFoundError:
                pass
        if lock is not None:
            _unlink(dfd, lock[0], out, directory=stat.S_ISDIR(lock[1].st_mode))
    finally:
        if held is not None:
            os.close(held)


def prune_session_state(*, keep_days: int, now: float | None = None, budget_s: float = TIME_BUDGET_S,
                        ) -> dict[str, Any]:
    """Remove per-session debris older than ``keep_days`` from the capture
    home's ``state/`` folder (0 keeps everything). Oldest groups first is not
    needed: a run that hits its time budget says so and the next run goes on."""
    from khipu import session_capture

    out: dict[str, Any] = dict(ok=True, scanned=0, candidates=0, removed=0, busy=0, errors=0,
                               freed_bytes=0, keep_days=int(keep_days))
    if keep_days <= 0:
        out["disabled"] = True
        return out
    now = time.time() if now is None else now
    cutoff = now - keep_days * 86400
    stop = time.monotonic() + budget_s
    prefixes = _session_prefixes()
    with session_capture._state_dir_fd() as dfd:
        if dfd is None:
            return out
        groups: dict[str, dict[str, Any]] = {}
        titles: list[tuple[str, os.stat_result]] = []
        try:
            with os.scandir(dfd) as entries:
                for entry in entries:
                    if time.monotonic() >= stop:
                        out["time_budget_exhausted"] = True
                        break
                    name = entry.name
                    if entry.is_symlink() or name.startswith("."):
                        continue
                    match = _GROUP.match(name)
                    cache = _TITLE_CACHE.match(name)
                    if not (cache or (match and match["prefix"] in prefixes)):
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    out["scanned"] += 1
                    if cache:
                        if stat.S_ISREG(st.st_mode):
                            titles.append((name, st))
                        continue
                    if match["kind"] == ".routing" or (match["ext"] == "tmp" and match["kind"]):
                        continue  # routing_ledger_prune's files; their locks are never removed
                    group = groups.setdefault(match["stem"] + (match["kind"] or ""),
                                              {"stem": match["stem"], "kind": match["kind"] or ""})
                    group[match["ext"]] = (name, st)
        except OSError:
            out.update(ok=False, errors=out["errors"] + 1, error="the capture state folder is unreadable")
            return out
        for name, st in titles:
            if st.st_mtime < cutoff:
                _unlink(dfd, name, out)
        for group in groups.values():
            if time.monotonic() >= stop:
                out["time_budget_exhausted"] = True
                break
            _group_old(dfd, group, cutoff, now, out)
    out["ok"] = out["errors"] == 0
    if out.get("time_budget_exhausted"):
        out["deferred"] = "session state prune: time budget exhausted, the next run goes on"
    return out


def _prune_folder(path: Path, cutoff: float, out: dict[str, Any], budget_stop: float) -> None:
    try:
        dfd = os.open(path, _O_DIR)
    except FileNotFoundError:
        return
    except OSError:
        out["errors"] += 1
        return
    try:
        with os.scandir(dfd) as entries:
            for entry in entries:
                if time.monotonic() >= budget_stop:
                    out["time_budget_exhausted"] = True
                    return
                if entry.is_symlink() or not _ARCHIVE_NAME.match(entry.name):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                out["scanned"] += 1
                # os.replace keeps the modify time of what it moves, so an old job
                # quarantined today is judged by when it arrived (its change time).
                if max(st.st_mtime, st.st_ctime) < cutoff:
                    _unlink(dfd, entry.name, out)
    except OSError:
        out["errors"] += 1
    finally:
        os.close(dfd)


def prune_archives(*, keep_days: int, now: float | None = None, budget_s: float = TIME_BUDGET_S,
                   folders: list[Path] | None = None) -> dict[str, Any]:
    """Remove archived files older than ``keep_days`` (0 keeps everything):
    the capture ``empty/`` archive and the outbox ``quarantine/``."""
    out: dict[str, Any] = dict(ok=True, scanned=0, removed=0, errors=0, freed_bytes=0, keep_days=int(keep_days))
    if keep_days <= 0:
        out["disabled"] = True
        return out
    if folders is None:
        from khipu import outbox, session_capture

        folders = [session_capture.empty_dir(), outbox.quarantine_dir()]
    cutoff = (time.time() if now is None else now) - keep_days * 86400
    stop = time.monotonic() + budget_s
    for folder in folders:
        _prune_folder(folder, cutoff, out, stop)
    out["ok"] = out["errors"] == 0
    if out.get("time_budget_exhausted"):
        out["deferred"] = "archive prune: time budget exhausted, the next run goes on"
    return out


def _count(path: Path, pattern: str) -> int:
    try:
        return sum(1 for p in path.glob(pattern) if p.is_file() and not p.is_symlink())
    except OSError:
        return 0


def held_counts() -> dict[str, int]:
    """Work that waits for the owner and is never removed here."""
    from khipu import outbox, session_capture

    try:
        capture_held = len(session_capture.held_jobs())
    except Exception:  # noqa: BLE001 - a miss shows as zero; doctor's outbox row names the real problem
        capture_held = 0
    try:
        refused = outbox.announce_refused_count()
    except Exception:  # noqa: BLE001 - a miss shows as zero; doctor's outbox row names the real problem
        refused = 0
    return {"outbox_dead": _count(outbox.dead_dir(), "*.json"), "outbox_announce_refused": refused,
            "capture_held": capture_held}


def warnings(counts: dict[str, int] | None = None, threshold: int = HELD_WARN_COUNT) -> list[str]:
    counts = held_counts() if counts is None else counts
    out = []
    retry = "Run `khipu outbox retry-dead` or look at them."
    refused = counts.get("outbox_announce_refused", 0)
    dead_step = retry if not refused else (
        f"{refused} of them were refused for not announcing source identities. "
        "`khipu outbox retry-dead` leaves those held, since it could land them as duplicates.")
    for key, where, next_step in (("outbox_dead", "the outbox's dead folder", dead_step),
                                  ("capture_held", "the capture queue", retry)):
        if counts.get(key, 0) > threshold:
            out.append(f"{counts[key]} jobs are held in {where} and wait for the owner (more than {threshold}); "
                       f"they are never removed on their own. {next_step}")
    return out
