"""A short memo of "the hub did not answer", so the per-prompt hook and the
search tools skip a connect they know will fail
(docs/plans/2026-10-16-offline-and-self-healing.md, design B).

Any failed connect writes it, any success clears it, and it is ignored once
``hub_down_memo_for`` seconds old, so it can never stick. It is advisory and
all on this Mac's clock; the file holds ``{"since", "at", "error"}``.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MEMO_NAME = "hub-unreachable.json"
DEFAULT_WINDOW_S = 60


def memo_path() -> Path:
    from khipu.paths import data_dir

    return data_dir() / "state" / MEMO_NAME


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _window_s() -> float:
    try:
        from khipu.replica_heal import settings

        return float(settings()["hub_down_memo_for"])
    except Exception:  # noqa: BLE001 - an unreadable setting is the default, never a failure
        return float(DEFAULT_WINDOW_S)


def _read() -> dict[str, Any] | None:
    try:
        loaded = json.loads(memo_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _age_s(memo: dict[str, Any], key: str = "at") -> float | None:
    try:
        at = datetime.fromisoformat(str(memo.get(key)).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if at.tzinfo is None:
        return None
    return (_now() - at).total_seconds()


def known_unreachable() -> dict[str, Any] | None:
    """The memo while it is current (0 <= age <= window), else None. A window
    of 0 means the memo is never consulted."""
    window = _window_s()
    if window <= 0:
        return None
    memo = _read()
    if memo is None:
        return None
    age = _age_s(memo)
    if age is None or not 0 <= age <= window:
        return None
    return memo


def note_unreachable(error: object = "") -> None:
    """Record a failed connect. Best effort: never raises."""
    from khipu.paths import isolated_dir

    try:
        if isolated_dir() is not None or _window_s() <= 0:
            return
        now = _now().isoformat()
        prior = known_unreachable()
        memo = {"since": (prior or {}).get("since") or now, "at": now, "error": str(error)[:200]}
        dest = memo_path()
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".hub-unreachable.", dir=dest.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(memo, fh)
            os.replace(name, dest)
        finally:
            Path(name).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - the memo is advisory
        pass


def note_reachable() -> None:
    """Clear the memo after a success. No write when there is none."""
    from khipu.paths import isolated_dir

    try:
        if isolated_dir() is not None:
            return
        memo_path().unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - the memo is advisory
        pass
