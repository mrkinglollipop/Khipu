"""Local Mac data directory for Khipu (not the database)."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat as stat_module
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

LEGACY_DIR = Path.home() / ".config" / "alzy"
DEFAULT_DIR = Path.home() / ".config" / "khipu"
POINTER_NAME = "data_root.json"
SENSITIVE_FILENAMES = frozenset({"dsn", "gateway-token"})


def open_trusted_directory(path: Path, *, create: bool = False) -> int:
    """Walk via descriptors, refusing user symlinks and writable ancestors.

    Root-owned links in root-controlled directories permit macOS's /tmp
    and /var aliases. Sticky root-owned temporary directories are allowed;
    all other components must be writable only by their root/current owner.
    The caller owns the returned descriptor.
    """
    import errno

    path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    if ".." in path.parts:
        raise OSError(errno.EPERM, "parent traversal refused")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    pending = list(path.parts[1:])
    links = 0
    try:
        while pending:
            part = pending.pop(0)
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            try:
                child = os.open(part, flags, dir_fd=fd)
            except OSError:
                link = os.stat(part, dir_fd=fd, follow_symlinks=False)
                parent = os.fstat(fd)
                if not (stat_module.S_ISLNK(link.st_mode) and link.st_uid == 0
                        and parent.st_uid == 0 and not parent.st_mode & 0o022):
                    raise
                links += 1
                target = Path(os.readlink(part, dir_fd=fd))
                if links > 40 or ".." in target.parts:
                    raise OSError(errno.ELOOP, "untrusted symlink traversal")
                if target.is_absolute():
                    root_fd = os.open(target.anchor, flags)
                    os.close(fd)
                    fd = root_fd
                    pending = list(target.parts[1:]) + pending
                else:
                    pending = list(target.parts) + pending
                continue
            info = os.fstat(child)
            sticky_root = info.st_uid == 0 and info.st_mode & stat_module.S_ISVTX
            if info.st_uid not in (0, os.geteuid()) or (info.st_mode & 0o022 and not sticky_root):
                os.close(child)
                raise OSError(errno.EPERM, "untrusted directory")
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def sensitive_writer(path: Path):
    """Open a sensitive local file as 0600 before any bytes are written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            yield handle
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def write_sensitive_bytes(path: Path, data: bytes) -> None:
    with sensitive_writer(path) as handle:
        handle.write(data)


def copy_sensitive(source, path: Path) -> None:
    with sensitive_writer(path) as handle:
        shutil.copyfileobj(source, handle)


def repo_root() -> Path:
    """The Khipu checkout this code is running from.

    ``KHIPU_ROOT`` / ``ALZY_ROOT`` win when set (release builds inject KHIPU_ROOT
    via Info.plist; a second machine may keep the repo anywhere). Otherwise the
    root is derived from this file's own location — packages/cli/khipu/paths.py
    is three levels below it — so nothing anywhere assumes a particular disk
    layout. Before this the fallback was one developer's Mac.
    """
    for key in ("KHIPU_ROOT", "ALZY_ROOT"):
        v = (os.environ.get(key) or "").strip()
        if v:
            return Path(v)
    return Path(__file__).resolve().parents[3]


# One directory that holds every piece of state a run writes, for the
# promotion gate's end-to-end capture and replay (khipu.promote). When it is
# set, ``data_dir()`` is this directory (config, outbox, hub replica, decision
# sync, embedding and budget state), ``session_capture.khipu_home()`` is its
# ``capture/`` (queue, hook state, heartbeats, hook logs), the launchers log
# into its ``logs/``, the file-wiki path settings read as unset and the
# file-wiki writer is skipped. Credentials are the one thing still read from
# the configured data dir (``credential_dir``), through the normal lookups.
ISOLATED_ENV = "KHIPU_ISOLATED_DIR"


def isolated_dir() -> Path | None:
    raw = (os.environ.get(ISOLATED_ENV) or "").strip()
    return Path(raw).expanduser() if raw else None


class IsolatedWriteRefused(RuntimeError):
    """A live-only write (data-dir pointer, DSN, certificate, Keychain) was
    attempted inside an isolated run."""


def refuse_if_isolated(what: str) -> None:
    """Raise when an isolated run (``KHIPU_ISOLATED_DIR``) tries to write
    something only the live install owns. Readers never call this."""
    isolated = isolated_dir()
    if isolated is not None:
        raise IsolatedWriteRefused(
            f"refusing to write {what} inside an isolated run ({ISOLATED_ENV}={isolated}); "
            "the live data-dir pointer, DSN, certificate and Keychain are written only outside it")


def _env_override() -> Path | None:
    raw = (os.environ.get("KHIPU_DATA_DIR") or os.environ.get("ALZY_DATA_DIR") or "").strip()
    return Path(raw).expanduser() if raw else None


def default_data_dir() -> Path:
    return DEFAULT_DIR


def pointer_file() -> Path:
    # Pointer always lives under the default config home so we can find overrides.
    return DEFAULT_DIR / POINTER_NAME


def data_dir() -> Path:
    isolated = isolated_dir()
    if isolated is not None:
        return isolated
    return credential_dir()


def credential_dir() -> Path:
    """The configured data dir (``KHIPU_DATA_DIR``, the pointer file, the
    default), ignoring ``KHIPU_ISOLATED_DIR``: where the on-disk DSN and root
    certificate are read from, so an isolated run reaches the hub through the
    same lookups as a live one."""
    env = _env_override()
    if env:
        return env
    ptr = pointer_file()
    if ptr.is_file():
        try:
            payload = json.loads(ptr.read_text(encoding="utf-8"))
            # Check the RAW string, not the Path: Path("") is PosixPath("."), whose
            # str() is "." — truthy. A pointer missing its "path" key therefore used
            # to resolve the whole data dir to the CURRENT WORKING DIRECTORY, which
            # is where the `dsn` file (Postgres credentials) would then be written
            # (audit 2026-08-17).
            raw = str(payload.get("path") or "").strip()
            if raw:
                return Path(raw).expanduser()
        except Exception:
            pass
    return DEFAULT_DIR


def set_data_dir(path: Path) -> Path:
    refuse_if_isolated("the data-dir pointer")
    target = path.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    DEFAULT_DIR.mkdir(parents=True, exist_ok=True)
    pointer_file().write_text(
        json.dumps({"path": str(target), "updated_at": datetime.now(timezone.utc).isoformat()})
        + "\n",
        encoding="utf-8",
    )
    # Seed essentials from previous locations when empty.
    for name in ("dsn", "root.crt"):
        dst = target / name
        if dst.is_file():
            continue
        for src_root in (DEFAULT_DIR, LEGACY_DIR):
            src = src_root / name
            if src.is_file() and src.resolve() != dst:
                shutil.copy2(src, dst)
                if name == "dsn":
                    dst.chmod(0o600)
                break
    return target


def ensure_data_dir() -> Path:
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


LEDGER_INBOX = "ledger-inbox"


def ledger_inbox() -> Path:
    """The ledger inbox, ``<data dir>/ledger-inbox/``, made when missing
    (owner only). A project's orchestrator copies its old tracker here at the
    switch, so the app's Choose file can reach a tracker that lived in a login
    folder; Khipu itself never writes, moves or deletes the original."""
    inbox = ensure_data_dir() / LEDGER_INBOX
    inbox.mkdir(mode=0o700, exist_ok=True)
    return inbox


def dsn_file() -> Path:
    if isolated_dir() is not None:
        return credential_dir() / "dsn"
    return ensure_data_dir() / "dsn"


def root_cert_file() -> Path:
    if isolated_dir() is not None:
        return credential_dir() / "root.crt"
    return ensure_data_dir() / "root.crt"


def pycache_dir() -> Path:
    """Where every Khipu launcher's CPython bytecode cache lands — always
    OUTSIDE any signed .app bundle. Every launcher (hook wrappers, the desktop
    app's own shell-out, launchd jobs) exports PYTHONPYCACHEPREFIX to this
    directory so running the bundled CLI never writes new files into
    Contents/Resources/khipu after it is signed. Release 0.3.15 skipped this:
    the bundled Python wrote __pycache__/*.pyc inside the signed bundle, and
    the next in-app update's Gatekeeper re-validation saw the added files and
    reported "Khipu is damaged" (confirmed via `codesign -vvv --deep --strict`;
    release withdrawn).

    Not ``data_dir()`` — that is relocatable and backed up/restored as user
    data; bytecode cache is neither.
    """
    return Path.home() / "Library" / "Caches" / "Khipu" / "pycache"


def ensure_pycache_dir() -> Path:
    d = pycache_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_local_files() -> list[dict]:
    d = ensure_data_dir()
    out = []
    if not d.is_dir():
        return out
    for p in sorted(d.rglob("*")):
        if not p.is_file():
            continue
        if p.name == POINTER_NAME and d == DEFAULT_DIR:
            continue
        rel = str(p.relative_to(d))
        try:
            st = p.stat()
        except OSError:  # removed between the walk and the stat (a lock file)
            continue
        # mtime in epoch milliseconds, the unit the desktop's Date math uses.
        out.append({"path": rel, "bytes": st.st_size, "mtime": int(st.st_mtime * 1000)})
    return out


def backup_local(*, dest: Path) -> dict:
    src = ensure_data_dir()
    dest = dest.expanduser()
    if dest.suffix.lower() != ".zip":
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = dest / f"khipu-local-{stamp}.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    replica = src / _SNAPSHOT_NAME
    staged: Path | None = None
    _remove_crashed_export_scratch(src)
    try:
        with sensitive_writer(dest) as archive_file:
            with zipfile.ZipFile(archive_file, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                if replica.is_file():
                    # The replica goes in as a checked copy, never its live
                    # file or journal: a copy whose fact rows belong to a
                    # refused generation carries none (hub_snapshot.
                    # export_replica_copy).
                    from khipu import hub_snapshot

                    staged = hub_snapshot.staging_path()
                    hub_snapshot.export_replica_copy(replica, staged)
                    zf.write(staged, arcname=_SNAPSHOT_NAME)
                    count += 1
                for p in src.rglob("*"):
                    if not p.is_file():
                        continue
                    if p.name == POINTER_NAME and src == DEFAULT_DIR:
                        continue
                    if staged is not None and p == staged:
                        continue
                    if p.parent == src and (p.name == _SNAPSHOT_NAME
                                            or p.name.startswith(_SNAPSHOT_NAME + "-")):
                        continue
                    # Another refresh's or export's temporary replica, or a
                    # sidecar of one, never went through the export.
                    if p.parent == src and (_replica_temp((p.name,)) or _EXPORT_SCRATCH.match(p.name)):
                        continue
                    zf.write(p, arcname=str(p.relative_to(src)))
                    count += 1
    except OSError as exc:
        from khipu.storage import describe_os_error, disk_full

        if not disk_full(exc):
            raise
        # A half-written archive is no backup and only holds space.
        dest.unlink(missing_ok=True)
        return {"ok": False, "error": f"the backup could not be written: {describe_os_error(exc)}",
                "archive": str(dest), "source": str(src)}
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
    return {"ok": True, "archive": str(dest), "files": count, "source": str(src)}


_REGISTRY_NAME = "forgotten_sources.json"
_REGISTRY_LOCK_NAME = ".forgotten_sources.lock"
_SNAPSHOT_NAME = "hub_snapshot.sqlite"  # khipu.hub_snapshot.SNAPSHOT_NAME
_MEMORY_LOCK_NAME = ".memory.lock"  # khipu.memory_lock.LOCK_NAME


def _job_refusal(data: bytes, gate) -> str | None:
    """Why a backed-up queued job (outbox, dead letter, quarantine) may not
    be restored, or None when it may. Fails closed: a job that cannot be
    parsed, or whose payload cannot be judged against the registry, is never
    treated as allowed, since it can still hold a forgotten source's text."""
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        return "unreadable"
    try:
        from khipu.capture_stores import outbox_payloads

        allowed = all(gate.allows(payload) for payload in outbox_payloads(value))
    except Exception:  # noqa: BLE001 (a job the gate cannot judge is not restored)
        return "unreadable"
    return None if allowed else "forgotten"


def import_local(*, source: Path, merge: bool = True) -> dict:
    """Copy a backup (a directory or a .zip) into the data folder.

    Only what a store in khipu/capture_stores.py says a backup may restore
    is copied, each file through that store's scrub; anything else (temps,
    sidecars, locks, records, unknown names) is left out and listed in
    ``not_restored``. A completed forget must survive the restore of a
    backup taken before it: the backup's tombstone registry is merged into
    this one, never copied over it, and every queued job naming a forgotten
    source is left behind.
    The backup's replica is written to a staging copy, scrubbed there, and
    only then atomically made the replica, so no reader sees it unscrubbed.
    The whole import holds the memory lock shared, so it never interleaves
    with a forget.
    """
    refuse_if_isolated("an imported data folder")
    from khipu import memory_lock

    with memory_lock.shared():
        out = _import_local(source=source, merge=merge)
    if (out.get("snapshot_scrub") or {}).get("imported"):
        # A restored replica shows no fact row until a copy (design 6.2),
        # taken after the memory lock: the copy lock is outermost.
        from khipu import hub_snapshot

        out["snapshot_scrub"]["facts"] = hub_snapshot.copy_facts()
    return out


def _skipped_name(parts: tuple[str, ...]) -> bool:
    """Lock files and the replica's journal: a journal from the backup would
    be replayed into whatever replica is current, so it is never copied."""
    return (parts in ((_REGISTRY_NAME,), (_REGISTRY_LOCK_NAME,), (_MEMORY_LOCK_NAME,))
            or (len(parts) == 1 and parts[0].startswith(_SNAPSHOT_NAME + "-")))


_REPLICA_TEMP = re.compile(r"^\.hub_snapshot\..+\.sqlite(-.+)?$")
# The scratch copy (fact rows included) an earlier export made beside its
# staged replica, ``.<staged name>.facts-<pid>`` where the staged name is
# ``staging_path``'s ``.hub_snapshot.<8 mkstemp characters>.sqlite``, with
# its sidecars. Exactly that name: any other file is the owner's.
_EXPORT_SCRATCH = re.compile(r"^\.\.hub_snapshot\.[a-z0-9_]{8}\.sqlite\.facts-(\d+)(-journal|-wal|-shm)?$")


def _remove_crashed_export_scratch(src: Path) -> None:
    """Delete the scratch copies crashed earlier exports left in the data
    folder, those whose process is gone."""
    for p in src.glob("..hub_snapshot.*.sqlite.facts-*"):
        found = _EXPORT_SCRATCH.match(p.name)
        if not found or not p.is_file():
            continue
        try:
            os.kill(int(found.group(1)), 0)
            continue
        except ProcessLookupError:
            p.unlink(missing_ok=True)
        except (PermissionError, OSError):
            continue
_WRITER_RECORDS = ".legacy_writers"  # khipu.writer_groups.DIR_NAME


def _replica_temp(parts: tuple[str, ...]) -> bool:
    """A temporary replica a crashed refresh left (``.hub_snapshot.<random>.sqlite``)
    or a journal or WAL sidecar of one. Never restored: it never went
    through staging and scrubbing, so it can hold a forgotten row."""
    return len(parts) == 1 and bool(_REPLICA_TEMP.match(parts[0]))


def _skip_reason(parts: tuple[str, ...]) -> str | None:
    if _skipped_name(parts) or parts[0] == _WRITER_RECORDS:
        return "lock"
    if _replica_temp(parts):
        return "replica_temp"
    return None


_DRIVE = re.compile(r"^[A-Za-z]:")


def _aliases_protected(root: Path, parts: tuple[str, ...], dest: Path) -> bool:
    """Whether a member other than the exact top-level name resolves onto the
    registry, a lock, the replica or its journal. Those exact names go through
    their merge, staging or skip paths; nothing may plainly copy over them."""
    if len(parts) == 1:
        return False
    protected = {(root / name).resolve()
                 for name in (_REGISTRY_NAME, _REGISTRY_LOCK_NAME, _MEMORY_LOCK_NAME, _SNAPSHOT_NAME)}
    return dest in protected or (dest.parent == root and (dest.name.startswith(_SNAPSHOT_NAME + "-")
                                                          or _replica_temp((dest.name,))
                                                          or dest.name == _WRITER_RECORDS))


def _checked_zip_members(zf: zipfile.ZipFile, target: Path) -> list[tuple[tuple[str, ...], zipfile.ZipInfo, Path]]:
    """Every file member as (normalised parts, info, resolved destination).

    The whole import is refused before anything is written when one member
    is unsafe. A member is classified only by its normalised name, so an
    alias such as nested/../forgotten_sources.json cannot reach the registry
    or the replica by a route that skips their merge or staging paths.
    """
    root = target.resolve()
    seen: set[tuple[str, ...]] = set()
    members = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename
        raw = name.split("/")
        if (not name or name.startswith("/") or "\\" in name or _DRIVE.match(name)
                or ".." in raw or "\0" in name):
            raise RuntimeError(f"refusing unsafe zip path: {name!r}")
        parts = tuple(part for part in raw if part not in ("", "."))
        if not parts:
            raise RuntimeError(f"refusing unsafe zip path: {name!r}")
        if parts in seen:
            raise RuntimeError(f"refusing duplicate zip path: {name!r}")
        seen.add(parts)
        # Zip-slip guard. Compare by path components, not by string prefix:
        # with target /a/b, "/a/bc/evil".startswith("/a/b") is True, so a
        # sibling directory escaped the old check entirely (audit 2026-08-17).
        dest = root.joinpath(*parts).resolve()
        if not dest.is_relative_to(root):
            raise RuntimeError(f"refusing unsafe zip path: {name!r}")
        if _aliases_protected(root, parts, dest):
            raise RuntimeError(f"refusing zip path that aliases a protected file: {name!r}")
        members.append((parts, info, dest))
    return members


def _not_restored_why(parts: tuple[str, ...]) -> str:
    reason = _skip_reason(parts)
    if reason == "replica_temp":
        return ("a temporary replica a crashed refresh left; it never went through staging "
                "and scrubbing, so it can hold a forgotten row")
    if reason == "lock":
        return "a lock, journal or writer record of the Mac that made the backup"
    if any(part.startswith(".") for part in parts):
        return "a temporary or hidden file, such as one a crashed write left behind"
    return "no store in the capture store inventory claims this file, so it is never restored"


# How each restore scrub treats a file a store claims (khipu/capture_stores.py,
# RESTORE_SCRUBS): "copy" restores it as it is, "job" leaves it out when it is
# a queued job for a forgotten source, "golden" drops each recall test line
# that names a forgotten episode, "stage" scrubs the replica in a staging
# copy before it becomes the replica, "merged" means the file was merged
# before the copy loop and is not copied.
RESTORE_SCRUBS = {
    "queued job": "job",
    "replica staging": "stage",
    "content addressed": "copy",  # images shared by hash: a known boundary of forget
    "registry merge": "merged",
    "recall golden": "golden",
    "no capture text": "copy",
}


# Read flags for a backed-up file: never follow a link, never wait on a FIFO.
_READ_NO_FOLLOW = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
_DIR_NO_FOLLOW = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)


def _refuse_same_file(st: os.stat_result, dest: Path) -> None:
    try:
        known = dest.stat()
    except OSError:
        return
    if (known.st_dev, known.st_ino) == (st.st_dev, st.st_ino):
        raise shutil.SameFileError(f"{dest!s} is the same file as its backup copy")


_WRITE_NO_FOLLOW = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)


class UnsafeDestination(RuntimeError):
    """A restore destination that is a link, a folder, a special file or a
    file with more than one name. It is never written through."""


def _destination_folder(root_fd: int, parts: tuple[str, ...], *, create: bool) -> int | None:
    """A descriptor of the folder a restore destination lives in, reached from
    the state folder's descriptor one component at a time without following
    links. None when a folder is missing and ``create`` is False. Raises
    UnsafeDestination when a component is a link or not a folder."""
    fd = os.dup(root_fd)
    try:
        for name in parts[:-1]:
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    os.close(fd)
                    return None
                os.mkdir(name, 0o700, dir_fd=fd)
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat_module.S_ISDIR(st.st_mode):
                raise UnsafeDestination(f"refusing to restore through {'/'.join(parts)}: "
                                        f"{name} is not a plain folder")
            sub = os.open(name, _DIR_NO_FOLLOW, dir_fd=fd)
            os.close(fd)
            fd = sub
        return fd
    except BaseException:
        os.close(fd)
        raise


def _check_destination(st: os.stat_result, parts: tuple[str, ...], registry: os.stat_result | None) -> None:
    rel = "/".join(parts)
    if not stat_module.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise UnsafeDestination(f"refusing to restore over {rel}: it exists as something other "
                                "than a regular file with one name (a link, folder or shared file)")
    if registry is not None and (st.st_dev, st.st_ino) == (registry.st_dev, registry.st_ino):
        raise UnsafeDestination(f"refusing to restore over {rel}: it is the forget registry, "
                                "which a restore only merges")


def _registry_identity(root_fd: int) -> os.stat_result | None:
    try:
        return os.stat(_REGISTRY_NAME, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _preflight_destination(root_fd: int, parts: tuple[str, ...]) -> None:
    """Refuse before anything is written when a destination already exists
    as anything but a regular file with one name, or a folder on its way is
    a link."""
    if parts == (_REGISTRY_NAME,):
        raise UnsafeDestination("refusing to restore over the forget registry, which a restore only merges")
    fd = _destination_folder(root_fd, parts, create=False)
    if fd is None:
        return
    try:
        try:
            st = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        _check_destination(st, parts, _registry_identity(root_fd))
    finally:
        os.close(fd)


def _restore_file(root_fd: int, parts: tuple[str, ...], source) -> None:
    """Write a restored file under the state folder, opened from the folder's
    descriptor without following any link. The opened file must be a regular
    file with one name that is not the forget registry, or nothing is written."""
    if parts == (_REGISTRY_NAME,):
        raise UnsafeDestination("refusing to restore over the forget registry, which a restore only merges")
    sensitive = parts[-1] in SENSITIVE_FILENAMES
    folder = _destination_folder(root_fd, parts, create=True)
    try:
        fd = os.open(parts[-1], _WRITE_NO_FOLLOW, 0o600 if sensitive else 0o666, dir_fd=folder)
    except OSError as exc:
        os.close(folder)
        raise UnsafeDestination(f"refusing to restore over {'/'.join(parts)}: it could not be "
                                f"opened as a plain file ({type(exc).__name__})") from exc
    os.close(folder)
    try:
        _check_destination(os.fstat(fd), parts, _registry_identity(root_fd))
        if sensitive:
            os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        with os.fdopen(fd, "wb") as out:
            fd = -1
            shutil.copyfileobj(source, out)
    finally:
        if fd != -1:
            os.close(fd)


def _write_staged(source, staged: Path) -> None:
    """The one write of a backup file's bytes into its private staging copy."""
    with sensitive_writer(staged) as handle:
        shutil.copyfileobj(source, handle)


def _stage_fd(fd: int, staged: Path) -> None:
    """Copy an opened backup file into its staging copy. Takes ownership of
    ``fd``. Raises OSError when it is not a regular file."""
    with os.fdopen(fd, "rb") as handle:
        if not stat_module.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError("not a regular file")
        _write_staged(handle, staged)


def _walk_backup(dir_fd: int, parts: tuple[str, ...], visit, unreadable) -> None:
    """Every entry under an opened backup folder, through folder descriptors
    opened without following links, so a folder swapped for a link after
    the walk began cannot redirect a read. ``visit(parts, dir_fd, name, st)``
    gets each entry that is not a folder; ``unreadable(parts, why)`` gets each
    folder or entry that cannot be listed or examined. Nothing is skipped
    silently."""
    try:
        with os.scandir(dir_fd) as listing:
            names = sorted(entry.name for entry in listing)
    except OSError as exc:
        unreadable(parts, f"its folder could not be listed ({type(exc).__name__}), so nothing "
                          "in it was restored")
        return
    for name in names:
        child = (*parts, name)
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError as exc:
            unreadable(child, f"it could not be examined ({type(exc).__name__}), so it was "
                              "not restored")
            continue
        if not stat_module.S_ISDIR(st.st_mode):
            visit(child, dir_fd, name, st)
            continue
        try:
            sub = os.open(name, _DIR_NO_FOLLOW, dir_fd=dir_fd)
        except OSError as exc:
            unreadable(child, f"its folder could not be opened ({type(exc).__name__}), so "
                              "nothing in it was restored")
            continue
        try:
            _walk_backup(sub, child, visit, unreadable)
        finally:
            os.close(sub)


def _import_local(*, source: Path, merge: bool) -> dict:
    """Two phases. Staging reads every candidate file of the backup exactly
    once, opened without following links, into a private staging folder (the
    replica straight into its own staging copy). Checking and restoring then
    use only those staged bytes, so a backup file replaced after it was read
    cannot change what is restored. A file that cannot be read or judged is
    left out and listed in ``not_restored``."""
    import tempfile

    from khipu import hub_snapshot, tombstones
    from khipu.capture_stores import restore_entry

    source = source.expanduser()
    target = ensure_data_dir()
    root = target.resolve()
    imported = 0
    skipped_forgotten = 0
    skipped_replica_temps: list[str] = []
    not_restored: list[dict[str, str]] = []
    replica: Path | None = None
    registry: Path | None = None
    # (scrub, staged copy, destination, backup path) for each file to restore.
    candidates: list[tuple[str, Path, Path, str]] = []
    private = Path(tempfile.mkdtemp(prefix="khipu-import-"))

    def left_out(parts: tuple[str, ...], why: str) -> None:
        not_restored.append({"path": "/".join(parts), "why": why})

    def scrub_for(parts: tuple[str, ...]) -> str | None:
        """The scrub a backed-up file goes through, or None (and listed in
        the report) when no store claims it."""
        entry = restore_entry(parts)
        if entry is None or _skip_reason(parts) is not None:
            if _skip_reason(parts) == "replica_temp":
                skipped_replica_temps.append("/".join(parts))
            left_out(parts, _not_restored_why(parts))
            return None
        return RESTORE_SCRUBS[entry.scrub]

    def staging_for(scrub: str) -> Path:
        nonlocal replica
        if scrub == "stage":
            if replica is None:
                replica = hub_snapshot.staging_path()
            return replica
        return private / str(len(candidates))

    def claim(parts: tuple[str, ...], dest: Path) -> str | None:
        """The scrub for a member that is to be staged, or None."""
        if parts == (_REGISTRY_NAME,):
            return None
        scrub = scrub_for(parts)
        if scrub is None or scrub == "merged":
            return None
        if _aliases_protected(root, parts, dest.resolve()):
            raise RuntimeError(f"refusing backup path that aliases a protected file: "
                               f"{'/'.join(parts)}")
        if dest.exists() and not merge:
            return None
        return scrub

    def visit(parts: tuple[str, ...], dir_fd: int, name: str, st: os.stat_result) -> None:
        nonlocal registry
        dest = target.joinpath(*parts)
        if parts == (_REGISTRY_NAME,):
            staged, scrub = private / "registry", None
        else:
            scrub = claim(parts, dest)
            if scrub is None:
                return
            staged = staging_for(scrub)
        if stat_module.S_ISLNK(st.st_mode):
            # A link is never followed; one that points at the very file it
            # would restore over is the same-file case, refused loudly.
            try:
                pointed = os.stat(name, dir_fd=dir_fd)
            except OSError:
                pointed = None
            if pointed is not None:
                _refuse_same_file(pointed, dest)
            left_out(parts, "a link, which a restore never follows")
            return
        try:
            fd = os.open(name, _READ_NO_FOLLOW, dir_fd=dir_fd)
        except OSError as exc:
            left_out(parts, f"it could not be read ({type(exc).__name__}), so it was not restored")
            return
        try:
            _refuse_same_file(os.fstat(fd), dest)
        except BaseException:
            os.close(fd)
            raise
        try:
            _stage_fd(fd, staged)
        except OSError as exc:
            staged.unlink(missing_ok=True)
            left_out(parts, f"it could not be read ({type(exc).__name__}), so it was not restored")
            return
        if scrub is None:
            registry = staged
            scrub_for(parts)  # still listed: the registry is merged, never copied
        else:
            candidates.append((scrub, staged, dest, "/".join(parts)))

    try:
        if source.is_dir():
            top = os.open(str(source), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                _walk_backup(top, (), visit, left_out)
            finally:
                os.close(top)
        elif source.is_file() and source.suffix.lower() == ".zip":
            with open(source, "rb") as archive, zipfile.ZipFile(archive, "r") as zf:
                for parts, info, dest in _checked_zip_members(zf, target):
                    if parts == (_REGISTRY_NAME,):
                        staged, scrub = private / "registry", None
                    else:
                        scrub = claim(parts, dest)
                        if scrub is None:
                            continue
                        staged = staging_for(scrub)
                    try:
                        with zf.open(info) as member:
                            _write_staged(member, staged)
                    except (OSError, zipfile.BadZipFile, ValueError, EOFError) as exc:
                        staged.unlink(missing_ok=True)
                        left_out(parts, f"it could not be read ({type(exc).__name__}), so it "
                                        "was not restored")
                        continue
                    if scrub is None:
                        registry = staged
                        scrub_for(parts)
                    else:
                        candidates.append((scrub, staged, dest, "/".join(parts)))
        else:
            raise RuntimeError("import source must be a .zip or a directory")
        # Nothing below reads the backup again: only the staged copies.
        root_fd = os.open(str(target), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            # Every destination is checked before anything is written, so an
            # unsafe one refuses the whole restore rather than half of it.
            for scrub, _staged, _dest, rel in candidates:
                if scrub != "stage":
                    _preflight_destination(root_fd, tuple(rel.split("/")))
            if registry is not None:
                tombstones.merge_registry_bytes(registry.read_bytes())
            with tombstones.write_gate() as gate:
                for scrub, staged, _dest, rel in candidates:
                    if scrub == "job":
                        refusal = _job_refusal(staged.read_bytes(), gate)
                        if refusal == "forgotten":
                            skipped_forgotten += 1
                            continue
                        if refusal is not None:
                            not_restored.append({"path": rel, "why": (
                                "a queued job that could not be read or checked against the "
                                "forget registry, so it may hold a forgotten source and was not "
                                "restored")})
                            continue
                    if scrub == "golden":
                        from khipu.recall_eval import drop_forgotten_lines

                        try:
                            text, dropped = drop_forgotten_lines(
                                staged.read_bytes().decode("utf-8"), gate.episode_ids)
                        except ValueError:
                            not_restored.append({"path": rel, "why": (
                                "a recall test file with a line that could not be read or checked "
                                "against the forget registry, so it may quote a forgotten episode "
                                "and was not restored")})
                            continue
                        skipped_forgotten += dropped
                        staged.write_bytes(text.encode("utf-8"))
                    if scrub != "stage":
                        with open(staged, "rb") as staged_file:
                            _restore_file(root_fd, tuple(rel.split("/")), staged_file)
                    imported += 1
        finally:
            os.close(root_fd)
        scrub = None
        if replica is not None and any(c[0] == "stage" for c in candidates):
            # After the gate is released: publishing takes the registry and
            # then the refresh lock, the order every replica writer uses.
            scrub = hub_snapshot.publish_imported_snapshot(replica)
    finally:
        shutil.rmtree(private, ignore_errors=True)
        if replica is not None:
            replica.unlink(missing_ok=True)
    out = {
        "ok": True,
        "imported": imported,
        "skipped_forgotten": skipped_forgotten,
        "target": str(target),
        "source": str(source),
        "merge": merge,
        "skipped_replica_temps": skipped_replica_temps,
        "not_restored": not_restored,
    }
    notes = []
    if skipped_replica_temps:
        notes.append(f"left out {len(skipped_replica_temps)} temporary replica "
                     f"{'file' if len(skipped_replica_temps) == 1 else 'files'} a crashed "
                     "refresh left behind; those are never restored")
    others = len(not_restored) - len(skipped_replica_temps)
    if others:
        notes.append(f"left out {others} {'file' if others == 1 else 'files'} that could not "
                     "be restored (temporary files, locks, records, unknown names, or files "
                     "that could not be read or checked); see not_restored")
    if notes:
        out["notes"] = notes
    if scrub is not None:
        out["snapshot_scrub"] = scrub
    return out


def paths_status() -> dict:
    d = data_dir()
    return {
        "data_dir": str(d),
        "default_data_dir": str(DEFAULT_DIR),
        "override_env": bool(_env_override()),
        "pointer": str(pointer_file()) if pointer_file().is_file() else None,
        "ledger_inbox": str(ledger_inbox()),
        "files": list_local_files(),
    }
