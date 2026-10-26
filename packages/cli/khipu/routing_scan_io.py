"""Owner-local scan I/O, isolated from readers and killable on filesystem stalls.

Mac/Linux only: fork preserves the scanner's local configuration without any
service or shell invocation. Children use _exit, never Python's shutdown joins.
"""
from __future__ import annotations

import json
import os
import select
import signal
import stat
import time
from pathlib import Path
from typing import Any, Callable

_PENDING: set[int] = set()
_MAX_PENDING = 32
_MAX_RESULT_BYTES = 8_000_000
_DATALESS = 0x40000000  # Darwin SF_DATALESS (not exported by every Python).


def protected(path: Path, home: Path) -> bool:
    """Lexical only; resolving or stating a cloud alias can itself hydrate it."""
    path = Path(os.path.abspath(path))
    home = Path(os.path.abspath(home))
    roots = ("Documents", "Desktop", "Downloads", "Pictures", "Movies", "Music",
             "Library/Mobile Documents", "Library/CloudStorage", "Library/iCloud",
             "Library/Mail", "Library/Messages", "Library/Safari", "iCloud Drive")
    return any(path == home / root or (home / root) in path.parents for root in roots) \
        or any(part.lower().endswith(".icloud") or "icloud" in part.lower()
               or part.lower() == "com~apple~clouddocs" for part in path.parts)


def safety(path: Path, home: Path) -> str | None:
    if protected(path, home):
        return "protected_path"
    # Refuse aliases rather than resolve a link into a protected/cloud tree.
    for component in reversed((path, *path.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "unreadable"
        if stat.S_ISLNK(info.st_mode):
            return "symlink"
        if getattr(info, "st_flags", 0) & _DATALESS:
            return "dataless"
    return None


def read_file(path: Path, home: Path, limit: int) -> dict[str, Any]:
    reason = safety(path, home)
    if reason:
        return {"reason": reason}
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            return {"reason": "not_regular"}
        if info.st_size > limit:
            return {"reason": "too_large"}
        # Read limit+1 even if the file grows after stat (no size TOCTOU).
        with path.open("rb") as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            return {"reason": "too_large"}
        return {"text": content.decode("utf-8")}
    except (OSError, UnicodeDecodeError):
        return {"reason": "unreadable"}


def _reap() -> None:
    for pid in tuple(_PENDING):
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            done = pid
        if done:
            _PENDING.discard(pid)


def bounded(action: Callable[[], Any], seconds: float) -> tuple[str, Any]:
    """Deadline covers stat/open/read/discovery and partial pipe delivery.

    SIGKILL cannot interrupt every kernel wait. Reap with WNOHANG only; track
    outstanding children and stop creating them at a fixed cap until reaped.
    Unsupported hosts fail closed rather than run unbounded in the reader.
    """
    if seconds <= 0:
        return "timeout", None
    if not hasattr(os, "fork"):
        return "unavailable", None
    _reap()
    if len(_PENDING) >= _MAX_PENDING:
        return "worker_limit", None
    end = time.monotonic() + seconds
    reader, writer = os.pipe()
    try:
        pid = os.fork()
    except OSError:
        os.close(reader)
        os.close(writer)
        return "unavailable", None
    if pid == 0:
        os.close(reader)
        try:
            payload = json.dumps(action(), ensure_ascii=True).encode("utf-8")
            if len(payload) <= _MAX_RESULT_BYTES:
                view = memoryview(payload)
                while view:
                    view = view[os.write(writer, view):]
        except BaseException:
            pass
        finally:
            os.close(writer)
            os._exit(0)
    os.close(writer)
    _PENDING.add(pid)
    os.set_blocking(reader, False)
    chunks = bytearray()
    status = "timeout"
    try:
        while time.monotonic() < end:
            if not select.select([reader], [], [], max(0, end - time.monotonic()))[0]:
                break
            try:
                chunk = os.read(reader, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                try:
                    return "ok", json.loads(chunks)
                except (ValueError, UnicodeDecodeError):
                    return "unreadable", None
            chunks.extend(chunk)
            if len(chunks) > _MAX_RESULT_BYTES:
                status = "too_large"
                break
        return status, None
    finally:
        os.close(reader)
        # Even successful workers may have a few instructions left before _exit.
        # Never let worker termination turn a timeout into an unbounded wait.
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _reap()
