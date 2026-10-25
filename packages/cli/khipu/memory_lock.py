"""One per-Mac memory lock that serializes forget against every local writer.

Scope: docs/plans/2026-10-11-capture-identity.md ("Durable forget: one memory
lock"). Forget holds it EXCLUSIVE for its whole run, from publishing the
tombstone registry to its completion check. Every local writer in the
inventory (capture, the mirror, the outbox, native capture jobs, the replica,
legacy log writes, backup and join import) holds it SHARED for its whole
write. Two forgets therefore serialize, and no writer interleaves with one.

Lock order: this lock is always taken before any other Khipu lock (outbox,
queue claim, replica refresh, tombstone registry). A writer that takes another
lock first and then this one could wait on a forget that is waiting on the
other lock.

A context that already holds the lock re-enters for free; a writer that tries
to start a forget inside its own write is refused, since upgrading would wait
on itself. A legacy child process inherits its parent's descriptor through
``handoff`` and validates it like the registry handoff does.
"""
from __future__ import annotations

import fcntl
import os
import stat
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

from khipu.tombstones import RegistryError

LOCK_NAME = ".memory.lock"
_HANDOFF_ENV = ("KHIPU_MEMORY_LOCK_FD", "KHIPU_MEMORY_LOCK_DEV", "KHIPU_MEMORY_LOCK_INO")

# "shared" or "exclusive", with the descriptor that holds it.
_HELD: ContextVar[tuple[str, int] | None] = ContextVar("khipu_memory_lock_held", default=None)


class MemoryLockError(RegistryError):
    """The memory lock could not be taken; the caller must not write."""


def lock_path() -> Path:
    from khipu.paths import ensure_data_dir

    return ensure_data_dir() / LOCK_NAME


def _inherited() -> int | None:
    """The parent's descriptor, validated against the lock file on disk."""
    values = [os.environ.get(name) for name in _HANDOFF_ENV]
    if not any(values):
        return None
    if not all(values):
        raise MemoryLockError("memory lock handoff is incomplete")
    try:
        fd, dev, ino = (int(v) for v in values)
        held, on_disk = os.fstat(fd), os.stat(lock_path())
    except (OSError, ValueError) as exc:
        raise MemoryLockError("memory lock handoff is invalid") from exc
    if (held.st_dev, held.st_ino) != (dev, ino) or (on_disk.st_dev, on_disk.st_ino) != (dev, ino):
        raise MemoryLockError("memory lock handoff does not match this data folder")
    try:
        # The same open file description already holds it shared, so this
        # returns at once; a forged descriptor would have to wait, and fails.
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError as exc:
        raise MemoryLockError("memory lock handoff is not held") from exc
    return fd


@contextmanager
def _take(mode: str, wait: float | None = None) -> Iterator[None]:
    try:
        path = lock_path()
        # A data folder that is not a real directory is refused rather than
        # turned into a file name in the working directory.
        if not stat.S_ISDIR(os.stat(path.parent).st_mode):
            raise NotADirectoryError(str(path.parent))
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except (OSError, TypeError) as exc:
        raise MemoryLockError(f"memory lock unavailable: {type(exc).__name__}") from exc
    try:
        flag = fcntl.LOCK_EX if mode == "exclusive" else fcntl.LOCK_SH
        try:
            if wait is None:
                fcntl.flock(fd, flag)
            else:
                # Bounded: waited for no longer than ``wait`` seconds.
                until = time.monotonic() + wait
                while True:
                    try:
                        fcntl.flock(fd, flag | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        left = until - time.monotonic()
                        if left <= 0:
                            raise MemoryLockError("memory lock busy until the deadline") from None
                        time.sleep(min(0.05, left))
        except OSError as exc:
            raise MemoryLockError(f"memory lock unavailable: {type(exc).__name__}") from exc
        token = _HELD.set((mode, fd))
        try:
            yield
        finally:
            _HELD.reset(token)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def shared(wait: float | None = None) -> Iterator[None]:
    """Hold the lock for one local write. Re-entrant. Waits for a forget to
    finish, for at most ``wait`` seconds when given."""
    if _HELD.get() is not None:
        yield
        return
    fd = _inherited()
    if fd is not None:
        token = _HELD.set(("shared", fd))
        try:
            yield
        finally:
            _HELD.reset(token)
        return
    with _take("shared", wait):
        yield


@contextmanager
def exclusive(wait: float | None = None) -> Iterator[None]:
    """Hold the lock for a whole forget. Waits for every writer to finish,
    for at most ``wait`` seconds when given."""
    held = _HELD.get()
    if held is not None and held[0] == "exclusive":
        yield
        return
    if held is not None or any(os.environ.get(name) for name in _HANDOFF_ENV):
        raise MemoryLockError("a forget cannot start inside a local write")
    with _take("exclusive", wait):
        yield


def handoff() -> tuple[dict[str, str], tuple[int, ...]]:
    """Environment and descriptor for a legacy child run inside a shared hold."""
    held = _HELD.get()
    if held is None:
        return {}, ()
    st = os.fstat(held[1])
    return ({"KHIPU_MEMORY_LOCK_FD": str(held[1]), "KHIPU_MEMORY_LOCK_DEV": str(st.st_dev),
             "KHIPU_MEMORY_LOCK_INO": str(st.st_ino)}, (held[1],))
