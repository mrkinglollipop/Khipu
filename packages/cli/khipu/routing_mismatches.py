"""Rule 12 model mismatches the dispatch check found, kept for a week.

``routing_advisory.dispatch_line`` words a mismatch inside a dispatch's
tool result and records nothing. This keeps one row per mismatch so the app's
Checks group can list today's: the time, the account and model used, the
model class the confirmed table asked for, the work class (from the brief's
``task:`` line, or marked as a guess) and a project label. Never brief text,
a session id or a path.

The rows live only in the owner-turns folder (0700, file 0600, never synced),
and reach the owner only through ``routing.public_view`` (``khipu route
--json``). No push, child page, conversation view, replication record, doctor
block, log or gateway projection reads this module. A read that fails is an
empty list.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from khipu import owner_turns

STORE_NAME = "routing-mismatches.json"
KEEP = timedelta(days=7)
MOST_ROWS = 200
_LABEL = re.compile(r"[^A-Za-z0-9 ._()/-]")


def _label(value, most: int = 80) -> str | None:
    """A short display label: no control characters, no brief-sized text."""
    if not isinstance(value, str):
        return None
    text = _LABEL.sub("", value).strip()[:most]
    return text or None


def _parse(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _path():
    return owner_turns.folder() / STORE_NAME


def _rows() -> list[dict]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.get("mismatches") if isinstance(data, dict) else None
    return [r for r in rows if isinstance(r, dict) and _parse(r.get("at"))] if isinstance(rows, list) else []


def record(found: dict, *, now: datetime | None = None) -> None:
    """Keep one mismatch ``dispatch_line`` found. Rows older than a week go on
    this write. Raises OSError when the file cannot be written."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    row = {
        "at": now.isoformat(timespec="seconds"),
        "task": _label(found.get("task"), 40),
        "expected": _label(found.get("expected"), 20),
        "actual": _label(found.get("actual"), 20),
        "guessed": found.get("guessed") is True,
        "account": _label(found.get("account")),
        "model": _label(found.get("model")),
        "project": _label(found.get("project")),
    }
    kept = [r for r in _rows() if (_parse(r["at"]) or now) > now - KEEP]
    owner_turns._ensure_folder()
    owner_turns._write_private(_path(), json.dumps({"mismatches": (kept + [row])[-MOST_ROWS:]}, indent=1) + "\n")


def today(now: datetime) -> list[dict]:
    """The rows from the owner's local day of ``now``, oldest first."""
    try:
        start = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        return [r for r in sorted(_rows(), key=lambda r: r["at"]) if _parse(r["at"]).astimezone() >= start]
    except Exception:  # noqa: BLE001 - a damaged store must not break the route view
        return []
