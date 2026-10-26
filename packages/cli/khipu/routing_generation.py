"""Lightweight identities for buffered routing answers; no engine import."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone


# Shared with the engine's loader; semantic fallback accepts the same format.
SCHEMA = 1


def _parse(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


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
    now = _parse(view.get("checked_at") or view.get("folded_at"))
    for account in view.get("accounts") or []:
        windows = []
        for pace in account.get("pace") or []:
            decision = {key: pace[key] for key in ("headline", "cycle_resets_at", "why") if key in pace}
            for key in ("plan", "share_cap", "day_cap", "pace_target", "check_pace_target"):
                value = pace.get(key)
                if isinstance(value, dict):
                    decision[key] = {k: value[k] for k in ("verdict", "why", "revision") if k in value}
                    if key in ("plan", "share_cap", "day_cap"):
                        eta = _parse(value.get("runs_out_at") or value.get("reaches_cap_at"))
                        decision[key]["crosses_within_hour"] = bool(
                            now is not None and eta is not None and now < eta
                            and eta - now <= timedelta(hours=1))
            if pace.get("ceiling_verdicts"):
                decision["ceiling_verdicts"] = pace["ceiling_verdicts"]
            if decision:
                windows.append(dict(decision, window=pace.get("window")))
        if windows:
            decisions[account["key"]] = windows
    return digest({**{k: view.get(k) for k in ("epoch", "version", "effective", "record")},
                   "pace_decisions": decisions,
                   "advice_cap_decisions": view.get("advice_cap_decisions")})


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
        return (isinstance(view, dict) and view.get("schema") == SCHEMA
                and view.get("digest") == digest(view)
                and revision(view) == expected)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return False
