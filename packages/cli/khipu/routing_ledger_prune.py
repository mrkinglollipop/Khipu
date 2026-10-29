"""Nightly removal of routing delivery ledgers nobody has used for a while.

One ``*.routing.json`` ledger exists per conversation and nothing else ever
removes it; ``khipu doctor`` reads every one. A ledger goes only when its
newest evidence (any timestamp inside it, or its file mtime) is older than
``routing_ledger_keep_days``, and only while its own lock is free: the lock is
taken non-blocking, the ledger is removed before the lock is released, so a
hook that was waiting on that lock reads an absent ledger and starts a fresh
one rather than writing over state we deleted.  Nothing else in the folder is
touched."""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
from datetime import datetime, timezone

DEFAULT_KEEP_DAYS = 7
TIME_BUDGET_S = 120
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
    """Epoch seconds of the ledger's newest sign of use; None if unreadable."""
    from khipu import routing_delivery

    try:
        stat = path.stat()
        newest = stat.st_mtime
        if stat.st_size <= routing_delivery._SESSION_FILE_BUDGET:
            found = []
            _stamps(json.loads(path.read_text()), found)
            for moment in found:
                newest = max(newest, moment.timestamp())
        return newest
    except (OSError, ValueError):
        try:
            return path.stat().st_mtime
        except OSError:
            return None


def prune(*, keep_days=None, now=None, folder=None, budget_s=TIME_BUDGET_S):
    from khipu import recall_prompt

    days = _keep_days() if keep_days is None else int(keep_days)
    counts = dict(scanned=0, removed=0, kept=0, busy=0, errors=0)
    out = dict(counts, ok=True, keep_days=days)
    if days <= 0:
        out["disabled"] = True
        return out
    folder = folder or recall_prompt._dedup_path("ledger").parent
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    cutoff = now - days * 86400
    stop = time.monotonic() + budget_s
    try:
        paths = sorted(folder.glob("*.routing.json"))
    except OSError:
        out.update(ok=False, errors=1, error="ledger folder unreadable")
        return out
    for path in paths:
        if time.monotonic() >= stop:
            out["time_budget_exhausted"] = True
            break
        out["scanned"] += 1
        try:
            if _old(path, cutoff):
                state = _remove(path, cutoff)
            else:
                state = "kept"
        except OSError:
            state = "errors"
        out[{"removed": "removed", "kept": "kept", "busy": "busy"}.get(state, "errors")] += 1
    out["ok"] = out["errors"] == 0
    return out


def _old(path, cutoff):
    newest = newest_evidence(path)
    return newest is not None and newest < cutoff


def _remove(path, cutoff):
    lock_path = path.with_suffix(".lock")
    try:
        lock = lock_path.open("a")
    except OSError:
        return "errors"
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "busy"
        # A writer may have landed between the first look and the lock.
        if not _old(path, cutoff):
            return "kept"
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            pass
        return "removed"
