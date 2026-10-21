"""Rule 12 model mismatches the dispatch check found, kept for a week.

``routing_advisory.dispatch_line`` words a mismatch inside a dispatch's
tool result and records nothing. This keeps one row per mismatch so the app's
Checks group can list today's: the time, the account and model used, the
model class the confirmed table asked for, the work class (from the brief's
``task:`` line, or marked as a guess) and a project label. Never brief text,
a session id or a path.

Each row is its own small file in the owner-turns folder (0700, files 0600,
never synced), ``routing-mismatch-<time>-<pid>-<random>.json``. A row never
rewrites another, so concurrent dispatches lose none. The dispatch hook hands
a row to a short-lived thread and waits at most ``HANDOFF_S``; the file is
created exclusively and without blocking, so a slow disk or a FIFO in the
folder cannot hold the advisory back. A row that did not land is lost, which
costs one line of display-only history.

Rows reach the owner only through ``routing.public_view`` (``khipu route
--json``). No push, child page, conversation view, replication record, doctor
block, log or gateway projection reads this module. Every field is checked on
the way in and again on the way out: an account is one of Khipu's own labels,
a model a plain model id, and anything shaped like a path or a session is
dropped, never truncated. A read that fails is an empty list. Rows older than
a week are removed when read and by ``owner_turns.sweep``.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
import threading
from datetime import datetime, timedelta, timezone

from khipu import owner_turns
from khipu.routing_advisory import TASK_CLASSES

PREFIX = "routing-mismatch-"
KEEP = timedelta(days=7)
HANDOFF_S = 0.05
MOST_FILES = 500
_FILE_MOST = 4096
UNLISTED_MODEL = "an unlisted model"
UNLISTED_ACCOUNT = "an unlisted account"
_TIERS = ("opus", "sonnet", "haiku")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,63}")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()-]{0,59}")
_SESSIONISH = re.compile(r"session|[0-9a-f]{8}-[0-9a-f]{4}-|[0-9a-f]{16,}|[A-Za-z0-9]{32,}", re.I)


def shaped_like_private(text: str) -> bool:
    """A path, a session id or a long opaque token."""
    return "/" in text or "\\" in text or ".." in text or bool(_SESSIONISH.search(text))


def _model(value) -> str:
    ok = isinstance(value, str) and _MODEL.fullmatch(value) and not shaped_like_private(value)
    return value if ok else UNLISTED_MODEL


def _label(value) -> str | None:
    ok = isinstance(value, str) and _LABEL.fullmatch(value) and not shaped_like_private(value)
    return value if ok else None


def _parse(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _row(found: dict, now: datetime) -> dict | None:
    """The row for a found mismatch, or None when its class or tiers are not
    ones the table can hold."""
    task, expected, actual = found.get("task"), found.get("expected"), found.get("actual")
    if task not in TASK_CLASSES or expected not in _TIERS or actual not in _TIERS:
        return None
    return {"at": now.isoformat(timespec="seconds"), "task": task, "expected": expected, "actual": actual,
            "guessed": found.get("guessed") is True,
            "account": _label(found.get("account")) or UNLISTED_ACCOUNT,
            "model": _model(found.get("model")), "project": _label(found.get("project"))}


def _write_event(path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def record(found: dict, *, now: datetime | None = None) -> bool:
    """Hand one mismatch to the store. True when the row landed within the
    handoff deadline. Never raises and never waits longer than ``HANDOFF_S``."""
    try:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        row = _row(found, now)
        if row is None:
            return False
        name = f"{PREFIX}{now.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}-{secrets.token_hex(4)}.json"
        outcome: list[bool] = []

        def work() -> None:
            try:
                folder = owner_turns._ensure_folder()
                _write_event(folder / name, json.dumps(row) + "\n")
                outcome.append(True)
            except Exception:  # noqa: BLE001 - the row is lost, the advisory is not
                pass

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        thread.join(HANDOFF_S)
        return bool(outcome)
    except Exception:  # noqa: BLE001 - a record that cannot be kept never changes the advisory
        return False


def _read(path) -> dict | None:
    """One event file, or None for anything else: not a plain file, too big,
    unreadable or invalid. Never blocks on a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _FILE_MOST:
            return None
        data = json.loads(os.read(fd, _FILE_MOST).decode("utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
    return data if isinstance(data, dict) else None


def _rows(now: datetime, *, expire: bool = False) -> list[dict]:
    """The valid rows from the last week, oldest first. With ``expire`` the
    files of older (and unreadable, stale) rows are removed too."""
    out: list[dict] = []
    try:
        with os.scandir(owner_turns.folder()) as entries:
            names = sorted(e.name for e in entries if e.name.startswith(PREFIX) and e.name.endswith(".json"))
    except OSError:
        return []
    if expire:
        # Expiry covers the whole store, not only the newest files shown.
        for name in names[:-MOST_FILES]:
            path = owner_turns.folder() / name
            data = _read(path)
            at = _parse(data.get("at")) if data else None
            if (at is not None and at <= now - KEEP) or (at is None and _stale(path, now)):
                try:
                    path.unlink()
                except OSError:
                    pass
    for name in names[-MOST_FILES:]:
        path = owner_turns.folder() / name
        data = _read(path)
        at = _parse(data.get("at")) if data else None
        row = _row(data, at) if at else None
        if row is not None:
            row["guessed"] = data.get("guessed") is True
            row["at"] = at.isoformat(timespec="seconds")
        old = at is not None and at <= now - KEEP
        if expire and (old or (row is None and _stale(path, now))):
            try:
                path.unlink()
            except OSError:
                pass
        if row is not None and not old:
            out.append(row)
    return sorted(out, key=lambda r: r["at"])


def _stale(path, now: datetime) -> bool:
    """An unreadable file may be a write in progress: only an old one goes."""
    try:
        return now.timestamp() - path.stat().st_mtime > 60
    except OSError:
        return False


def today(now: datetime) -> list[dict]:
    """The rows from the owner's local day of ``now``, oldest first. Rows past
    their week are removed here too."""
    try:
        now = now.astimezone(timezone.utc)
        start = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        return [r for r in _rows(now, expire=True) if _parse(r["at"]).astimezone() >= start]
    except Exception:  # noqa: BLE001 - a damaged store must not break the route view
        return []


def sweep(now: datetime | None = None) -> int:
    """Remove rows past their week. Returns how many files went."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        before = len(_listing())
        _rows(now, expire=True)
        return before - len(_listing())
    except Exception:  # noqa: BLE001 - a timer sweep never fails its job
        return 0


def _listing() -> list[str]:
    with os.scandir(owner_turns.folder()) as entries:
        return [e.name for e in entries if e.name.startswith(PREFIX)]
