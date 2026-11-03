"""Cap a log where it is written, with one ``stat`` on the common path.

``khipu.storage``'s sweep trims every log over ``log_max_mb``, but it only
runs when something calls it. A log whose writer notices its own size does
not depend on that: the hook that appends to ``stop-hook.log`` and the
capture, recall and nightly writers call ``rotate_if_over`` before they
write. Standard library only, so a hook on every prompt can import it.

The lines over the cap are copied to ``<name>.1`` (replacing an older copy)
and the log is emptied in place, never renamed: a process that still holds
the log open, such as a hook's detached child, keeps writing to the same
file. A writer that finds another process rotating skips; the next write
tries again.
"""
from __future__ import annotations

import fcntl
import os
import shutil
from pathlib import Path

DEFAULT_CAP_BYTES = 5 * 1024 * 1024  # log_max_mb's default (khipu.storage)
# A log this small is under any cap worth setting: one ``stat`` and nothing else.
FLOOR_BYTES = 64 * 1024
_CAP_CACHE: list = []  # the configured cap, read once per process


def reset_cache() -> None:
    _CAP_CACHE.clear()


def _configured_cap() -> int | None:
    """``log_max_mb`` from the config, in bytes; None when 0 (no cap). Read
    once per process (and only for a log past ``FLOOR_BYTES``), so a log
    writer pays for the config at most once however often it writes."""
    if not _CAP_CACHE:
        _CAP_CACHE.append(_read_cap())
    return _CAP_CACHE[0]


def _read_cap() -> int | None:
    try:
        from khipu.config import load_config

        raw = load_config().get("log_max_mb")
        if raw is None or isinstance(raw, bool):
            return DEFAULT_CAP_BYTES
        mb = float(raw)
    except Exception:  # noqa: BLE001 - an unreadable config keeps the default
        return DEFAULT_CAP_BYTES
    if mb != mb or mb < 0:
        return DEFAULT_CAP_BYTES
    return None if mb == 0 else int(mb * 1024 * 1024)


def rotate_if_over(path: str | os.PathLike | None, cap_bytes: int | None = None) -> bool:
    """Start ``path`` afresh when it is over the cap, keeping one older copy.
    True when it rotated. Never raises: a log must never stop its writer."""
    if not path:
        return False
    log = Path(path)
    try:
        info = os.lstat(log)
        if (cap_bytes is None and info.st_size <= FLOOR_BYTES) or os.path.islink(log) \
                or not os.path.isfile(log):
            return False
        cap = cap_bytes if cap_bytes is not None else _configured_cap()
        if cap is None or info.st_size <= cap:
            return False
        with open(log, "r+b") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False  # another writer is rotating it
            if os.fstat(stream.fileno()).st_size <= cap:
                return False
            older = log.with_name(log.name + ".1")
            if not older.is_symlink():
                shutil.copyfile(log, older)
            stream.truncate(0)
        return True
    except Exception:  # noqa: BLE001 - logging housekeeping never breaks the writer
        return False
