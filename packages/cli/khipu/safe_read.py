"""Reading a file someone else's tool wrote, without being led somewhere else.

The one way Khipu reads a project's summary file (work.py) and an old tracker
file (ledger_import.py). Before anything is opened, the path is refused when
it is in a private place or in a login folder (``agent_view.in_login_folder``:
the same list the scrubs use, each harness's own variable included). Then its
folder is reached one folder at a time from ``/`` with ``O_NOFOLLOW`` (only a
link the system owns at the top of the disk, ``/tmp`` or ``/var`` on macOS, is
resolved), and the file is opened by name from that folder's descriptor with
``O_RDONLY | O_NONBLOCK | O_NOFOLLOW``. What was opened must be a regular file
with one name: a second name could be a hard link to a file inside a login
folder. After reading, the name is looked up again in the same folder and must
still be that file (device and inode) with one name, so a name unlinked or
swapped between the open and the ``fstat`` is caught. Nothing is ever written.
"""
from __future__ import annotations

import errno
import os
import stat
from pathlib import Path


class Refused(Exception):
    """A private or login folder, a link on the way, or a file with another
    name (or one that changed while it was read)."""


class TooBig(Exception):
    def __init__(self, size: int, limit: int):
        super().__init__(f"{size} bytes, over {limit}")
        self.size, self.limit = size, limit


def private_place(path: str) -> bool:
    """A private place or a login folder, by the name as given and as the
    links resolve. Unsure is private."""
    from khipu import agent_view, routing_sources

    try:
        return routing_sources.private_path(path) or agent_view.in_login_folder(path) \
            or agent_view.in_login_folder(os.path.realpath(path))
    except Exception:  # noqa: BLE001
        return True


def system_links_resolved(folder: str) -> str:
    """``folder`` with only its first name resolved, and only when that is a
    link owned by root at the top of the disk. Every other link stays, so the
    walk refuses it."""
    parts = Path(folder).parts
    if len(parts) < 2:
        return folder
    top = os.path.join("/", parts[1])
    try:
        st = os.lstat(top)
    except OSError:
        return folder
    if not stat.S_ISLNK(st.st_mode) or st.st_uid != 0:
        return folder
    real = os.path.realpath(top)
    return os.path.join(real, *parts[2:]) if real != top else folder


class Opened:
    """An open file from ``open_file``; ``st`` is its ``fstat`` at the open.
    Use as a context manager, or call ``close``."""

    def __init__(self, fd: int, st: os.stat_result, dir_fd: int, name: str):
        self.fd, self.st, self._dir_fd, self._name = fd, st, dir_fd, name

    def read(self, most: int = 0) -> bytes:
        """Up to ``most`` bytes (0: all of it)."""
        chunks: list[bytes] = []
        got = 0
        while not most or got < most:
            chunk = os.read(self.fd, min(1 << 20, most - got) if most else 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
        return b"".join(chunks)

    def recheck(self) -> None:
        """Raise ``Refused`` unless the name still names the opened file, and
        that file still has one name."""
        try:
            now = os.fstat(self.fd)
            named = os.stat(self._name, dir_fd=self._dir_fd, follow_symlinks=False)
        except OSError:
            raise Refused from None
        if now.st_nlink != 1 or named.st_nlink != 1 or not stat.S_ISREG(named.st_mode) \
                or (named.st_dev, named.st_ino) != (self.st.st_dev, self.st.st_ino):
            raise Refused

    def close(self) -> None:
        for fd in (self.fd, self._dir_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        self.fd = self._dir_fd = -1

    def __enter__(self) -> Opened:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def open_file(path: str | os.PathLike, limit: int = 0) -> Opened:
    """Open ``path`` read-only as the module text says. Raises ``Refused``,
    ``FileNotFoundError``, ``IsADirectoryError``, ``TooBig`` (over ``limit``
    bytes; 0 is no limit) or ``OSError``."""
    p = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
    if private_place(str(p)):
        raise Refused
    folder = system_links_resolved(str(p.parent))
    if private_place(os.path.join(folder, p.name)):
        raise Refused
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    dir_fd = os.open("/", os.O_RDONLY | directory)
    try:
        try:
            for part in Path(folder).parts[1:]:
                step = os.open(part, os.O_RDONLY | directory | nofollow, dir_fd=dir_fd)
                os.close(dir_fd)
                dir_fd = step
            fd = os.open(p.name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | nofollow, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise Refused from None
            raise
    except BaseException:
        os.close(dir_fd)
        raise
    opened = Opened(fd, os.fstat(fd), dir_fd, p.name)
    try:
        st = opened.st
        if stat.S_ISDIR(st.st_mode):
            raise IsADirectoryError("a folder, not a file")
        if not stat.S_ISREG(st.st_mode):
            raise OSError("not a regular file")
        if st.st_nlink != 1:
            raise Refused
        if limit and st.st_size > limit:
            raise TooBig(st.st_size, limit)
    except BaseException:
        opened.close()
        raise
    return opened


def read_file(path: str | os.PathLike, limit: int = 0) -> tuple[bytes, os.stat_result]:
    """The whole file (up to ``limit`` bytes) and its ``fstat``, re-checked
    after reading."""
    with open_file(path, limit) as opened:
        data = opened.read(limit)
        opened.recheck()
        return data, opened.st
