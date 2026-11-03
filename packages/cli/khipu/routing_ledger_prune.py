"""Nightly removal of routing delivery ledgers nobody has used for a while.

One ``*.routing.json`` ledger exists per conversation and nothing else ever
removes it; ``khipu doctor`` reads every one. A ledger goes only when its
newest evidence (every timestamp inside it, and its file mtime) is older than
``routing_ledger_keep_days``, and only while its own lock is free: the lock is
taken non-blocking and the ledger is removed before it is released, so a hook
that was waiting on that lock reads an absent ledger and starts a fresh one.
Lock files are never removed: a hook may already hold the old one open, and a
new one at the same name would let two writers hold "the" lock at once.  A
ledger that is not a regular file is skipped, never removed.  One that is a
regular file but whose timestamps cannot be read (too large, not JSON) cannot
show it was used, so it goes once its file time is past the keep window: it is
a cache of what a hook already delivered, rebuilt on the next prompt, not
owner data.  Ledgers are taken oldest file time first and at most BATCH_MAX a
run, and a run that stops early (time or batch) says how many are left, so the
next night carries on from the oldest.  Nothing else in the folder is touched."""
from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import time
from datetime import datetime, timezone

DEFAULT_KEEP_DAYS = 7
TIME_BUDGET_S = 120
BATCH_MAX = 20000
_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")


def _keep_days():
    from khipu import routing

    try:
        return int(routing.settings()["routing_ledger_keep_days"])
    except (KeyError, ValueError, TypeError):
        return DEFAULT_KEEP_DAYS


def _stamps(value, found):
    if isinstance(value, str):
        if _STAMP.match(value):
            try:
                found.append(_parse(value))
            except ValueError:
                pass
    elif isinstance(value, dict):
        for item in value.values():
            _stamps(item, found)
    elif isinstance(value, list):
        for item in value:
            _stamps(item, found)


def _parse(value):
    from khipu import routing_delivery

    return routing_delivery._parse(value)


def newest_evidence(path):
    """Epoch seconds of the ledger's newest sign of use; None when any of it
    cannot be read, which the caller treats as "keep"."""
    from khipu import routing_delivery

    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_size > routing_delivery._SESSION_FILE_BUDGET:
            return None
        # O_NOFOLLOW and O_NONBLOCK: a symlink or FIFO swapped in after lstat
        # fails or returns at once instead of blocking the nightly.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            opened = os.fstat(f.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > routing_delivery._SESSION_FILE_BUDGET:
                return None
            found = []
            _stamps(json.loads(f.read(routing_delivery._SESSION_FILE_BUDGET + 1)), found)
        newest = opened.st_mtime
        for moment in found:
            newest = max(newest, moment.timestamp())
        return newest
    except (OSError, ValueError, RecursionError):
        return None


def _ledger_paths(folder, stop, cutoff):
    """``(old, recent, odd, exhausted)``: regular ledgers whose file time is
    past the cutoff as ``(mtime, path)`` oldest first, how many are newer
    (their evidence is at least their file time, so they stay unread), the
    ledgers that are not regular files, and whether the budget ran out."""
    old, recent, odd = [], 0, []
    exhausted = False
    with os.scandir(folder) as entries:
        for entry in entries:
            if time.monotonic() >= stop:
                exhausted = True
                break
            if not entry.name.endswith(".routing.json"):
                continue
            path = folder / entry.name
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                odd.append(path)
                continue
            if not stat.S_ISREG(info.st_mode):
                odd.append(path)
            elif info.st_mtime >= cutoff:
                recent += 1
            else:
                old.append((info.st_mtime, path))
    old.sort()
    return old, recent, odd, exhausted


def prune(*, keep_days=None, now=None, folder=None, budget_s=TIME_BUDGET_S):
    from khipu import recall_prompt

    days = _keep_days() if keep_days is None else int(keep_days)
    counts = dict(scanned=0, removed=0, kept=0, skipped=0, busy=0, errors=0)
    out = dict(counts, ok=True, keep_days=days, unreadable_removed=0)
    if days <= 0:
        out["disabled"] = True
        return out
    folder = folder or recall_prompt._dedup_path("ledger").parent
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    cutoff = now - days * 86400
    stop = time.monotonic() + budget_s
    try:
        old, recent, odd, exhausted = _ledger_paths(folder, stop, cutoff)
    except OSError:
        out.update(ok=False, errors=1, error="ledger folder unreadable")
        return out
    if exhausted:
        out["time_budget_exhausted"] = True
    out["scanned"] += recent + len(odd)
    out["kept"] += recent
    out["skipped"] += len(odd)
    done = 0
    for _mtime, path in old[:BATCH_MAX]:
        if time.monotonic() >= stop:
            out["time_budget_exhausted"] = True
            break
        done += 1
        out["scanned"] += 1
        try:
            state = _state(path, cutoff)
            first = state
            if state in ("old", "stale_unreadable"):
                state = _remove(path, cutoff)
                if state == "removed" and first == "stale_unreadable":
                    out["unreadable_removed"] += 1
        except OSError:
            state = "errors"
        out[state if state in counts else "errors"] += 1
    out["remaining"] = len(old) - done
    out["ok"] = out["errors"] == 0
    # Stopping early, or skipping ledgers that are not files, must not read as
    # a silent green success: surface it. The next run carries on, oldest first.
    reasons = []
    if out.get("time_budget_exhausted"):
        reasons.append("time budget exhausted")
    if out["remaining"]:
        reasons.append(f"{out['remaining']} old ledger(s) left for the next run")
    if out["skipped"] and not out["removed"]:
        reasons.append(f"{out['skipped']} ledger(s) that are not files skipped")
    if reasons:
        out["deferred"] = "routing ledger prune: " + "; ".join(reasons)
    return out


def _state(path, cutoff):
    newest = newest_evidence(path)
    if newest is None:
        # No readable timestamps: the file time alone decides, and only for a
        # regular file (a link or FIFO is never removed).
        try:
            info = os.lstat(path)
        except OSError:
            return "skipped"
        return "stale_unreadable" if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff else "skipped"
    return "old" if newest < cutoff else "kept"


def _remove(path, cutoff):
    try:
        lock = path.with_suffix(".lock").open("a")
    except OSError:
        return "errors"
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "busy"
        # A writer may have landed between the first look and the lock.
        state = _state(path, cutoff)
        if state not in ("old", "stale_unreadable"):
            return state
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return "removed"
