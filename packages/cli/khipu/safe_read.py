"""Reading a file someone else's tool wrote, without being led somewhere else.

The one way Khipu reads a project's summary file (work.py) and an old tracker
file (ledger_import.py). Before anything is opened, the path is refused when
it is in a private place or in a login folder (``agent_view.in_login_folder``:
the same list the scrubs use, each harness's own variable included). Then its
folder is reached one folder at a time from ``/`` with ``O_NOFOLLOW`` (only a
link the system owns at the top of the disk, ``/tmp`` or ``/var`` on macOS, is
resolved), each folder on the way refused when it is a login folder by device
and inode (another name for the same folder, such as macOS's
``/System/Volumes/Data/...``, passes a name check), and the file is opened by name from that folder's descriptor with
``O_RDONLY | O_NONBLOCK | O_NOFOLLOW``. What was opened must be a regular file
with one name: a second name could be a hard link to a file inside a login
folder. After reading, the name is looked up again in the same folder and must
still be that file (device and inode) with one name, so a name unlinked or
swapped between the open and the ``fstat`` is caught. The path is then walked
again from ``/`` the same way, and its folder and its file must be the very
folder and file that were opened, so a parent folder moved or replaced after
the open is caught too. Nothing is ever written.
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

    def __init__(self, fd: int, st: os.stat_result, dir_fd: int, name: str, folder: str):
        self.fd, self.st, self._dir_fd, self._name, self._folder = fd, st, dir_fd, name, folder

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
        that file still has one name, and the path walked again from ``/``
        reaches the same folder and the same file."""
        try:
            now = os.fstat(self.fd)
            named = os.stat(self._name, dir_fd=self._dir_fd, follow_symlinks=False)
            held = os.fstat(self._dir_fd)
            again = _walk(self._folder)
            try:
                folder = os.fstat(again)
                found = os.stat(self._name, dir_fd=again, follow_symlinks=False)
            finally:
                os.close(again)
        except (OSError, Refused):
            raise Refused from None
        same = (self.st.st_dev, self.st.st_ino)
        if now.st_nlink != 1 or named.st_nlink != 1 or not stat.S_ISREG(named.st_mode) \
                or (named.st_dev, named.st_ino) != same or (found.st_dev, found.st_ino) != same \
                or (folder.st_dev, folder.st_ino) != (held.st_dev, held.st_ino):
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


def _login_ids() -> frozenset[tuple[int, int]]:
    """``agent_view.login_folder_ids``; unsure refuses every walk."""
    from khipu import agent_view

    try:
        return agent_view.login_folder_ids()
    except Exception:  # noqa: BLE001
        raise Refused from None


def _walk(folder: str) -> int:
    """A descriptor for ``folder``, reached one folder at a time from ``/``
    without following a link. Raises ``Refused`` for a link on the way, or
    for a folder on the way that is a login folder by its device and inode,
    whatever name reached it, before anything inside it is opened."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    logins = _login_ids()
    dir_fd = os.open("/", os.O_RDONLY | directory)
    try:
        for part in Path(folder).parts[1:]:
            step = os.open(part, os.O_RDONLY | directory | nofollow, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = step
            st = os.fstat(dir_fd)
            if (st.st_dev, st.st_ino) in logins:
                raise Refused
    except OSError as exc:
        os.close(dir_fd)
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise Refused from None
        raise
    except BaseException:
        os.close(dir_fd)
        raise
    return dir_fd


def open_file(path: str | os.PathLike, limit: int = 0) -> Opened:
    """Open ``path`` read-only as the module text says. Raises ``Refused``,
    ``FileNotFoundError``, ``IsADirectoryError``, ``TooBig`` (over ``limit``
    bytes; 0 is no limit) or ``OSError``."""
    raw = os.path.expanduser(os.fspath(path))
    # Normalising "." or ".." away would open a different file than the one
    # named when a link sits before it ("link/.." is the link's parent, not
    # this folder), so a path holding either is refused, never collapsed.
    if any(part in (".", "..") for part in raw.split(os.sep)):
        raise Refused
    p = Path(os.path.abspath(raw))
    if private_place(str(p)):
        raise Refused
    folder = system_links_resolved(str(p.parent))
    if private_place(os.path.join(folder, p.name)):
        raise Refused
    dir_fd = _walk(folder)
    try:
        try:
            fd = os.open(p.name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0),
                         dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise Refused from None
            raise
    except BaseException:
        os.close(dir_fd)
        raise
    opened = Opened(fd, os.fstat(fd), dir_fd, p.name, folder)
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
