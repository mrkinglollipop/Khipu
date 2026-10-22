"""Coordinated graph writers and validated, same-filesystem publication."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile


@contextmanager
def writer_lock(database: Path):
    # Keep the lock inode stable across database replacements and path aliases.
    database = database.resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    lock = database.with_name('.' + database.name + '.lock')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def publish(stage: Path, database: Path) -> None:
    """Never open the published database for writing; failures propagate.

    The sibling is private and disposable. The caller owns recovery/cleanup
    of the worker stage. Readers with an open handle retain the old inode.
    """
    database = database.resolve()
    try:
        mode = stat.S_IMODE(database.stat().st_mode)
    except FileNotFoundError:
        mode = 0o600
    fd, name = tempfile.mkstemp(prefix='.' + database.name + '.publish-',
                                suffix='.sqlite', dir=database.parent)
    os.close(fd)
    sibling = Path(name)
    try:
        with open(stage, 'rb') as src, open(sibling, 'wb') as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        conn = sqlite3.connect(sibling.as_uri() + '?mode=ro', uri=True)
        try:
            if conn.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise sqlite3.DatabaseError('graph publication integrity check failed')
            for table in ('nodes', 'edges', 'embeddings'):
                conn.execute(f'SELECT * FROM {table} LIMIT 0')
        finally:
            conn.close()
        os.chmod(sibling, mode)
        os.replace(sibling, database)
        directory = os.open(database.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        sibling.unlink(missing_ok=True)
