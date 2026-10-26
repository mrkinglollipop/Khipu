"""Lightweight identities for buffered routing answers; no engine import."""
from __future__ import annotations

import hashlib
import json
import os


def digest(view: dict) -> str:
    body = {k: v for k, v in view.items() if k != "digest"}
    text = json.dumps(body, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def revision(view: dict) -> str:
    """Routing state and pace decisions, excluding advancing measurements.

    Effective entities cover owner, account and source changes. Pace verdicts
    can also change on the clock without moving the routing version.
    """
    # Deliberately exclude cap_proposals, machine_holds, tier_table,
    # priority_ended and proposal seven-day expiry from this whitelist.
    # Source edits are protected by file stats; clock-only changes outside
    # the whitelist do not invalidate a buffered answer.
    decisions = {}
    for account in view.get("accounts") or []:
        windows = []
        for pace in account.get("pace") or []:
            decision = {key: pace[key] for key in ("headline", "cycle_resets_at", "why") if key in pace}
            for key in ("plan", "share_cap", "day_cap", "pace_target", "check_pace_target"):
                value = pace.get(key)
                if isinstance(value, dict):
                    decision[key] = {k: value[k] for k in ("verdict", "why", "revision") if k in value}
            if pace.get("ceiling_verdicts"):
                decision["ceiling_verdicts"] = pace["ceiling_verdicts"]
            if decision:
                windows.append(dict(decision, window=pace.get("window")))
        if windows:
            decisions[account["key"]] = windows
    return digest({**{k: view.get(k) for k in ("epoch", "version", "effective", "record")},
                   "pace_decisions": decisions})


def matches(path: str, expected: str) -> bool:
    """Read a changed view once; damaged or concurrently replaced views fail closed."""
    try:
        with open(path, encoding="utf-8") as fh:
            before = os.fstat(fh.fileno())
            view = json.load(fh)
        after = os.stat(path)
        if (before.st_mtime_ns, before.st_size, before.st_ino) != (
                after.st_mtime_ns, after.st_size, after.st_ino):
            return False
        return (isinstance(view, dict) and view.get("digest") == digest(view)
                and revision(view) == expected)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return False
