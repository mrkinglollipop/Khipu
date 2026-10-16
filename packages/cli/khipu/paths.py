"""Local Mac data directory for Khipu (not the database)."""
from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

LEGACY_DIR = Path.home() / ".config" / "alzy"
DEFAULT_DIR = Path.home() / ".config" / "khipu"
POINTER_NAME = "data_root.json"
SENSITIVE_FILENAMES = frozenset({"dsn", "gateway-token"})


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
    with sensitive_writer(dest) as archive_file:
        with zipfile.ZipFile(archive_file, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in src.rglob("*"):
                if not p.is_file():
                    continue
                if p.name == POINTER_NAME and src == DEFAULT_DIR:
                    continue
                zf.write(p, arcname=str(p.relative_to(src)))
                count += 1
    return {"ok": True, "archive": str(dest), "files": count, "source": str(src)}


_REGISTRY_NAME = "forgotten_sources.json"
_REGISTRY_LOCK_NAME = ".forgotten_sources.lock"
_SNAPSHOT_NAME = "hub_snapshot.sqlite"  # khipu.hub_snapshot.SNAPSHOT_NAME
_MEMORY_LOCK_NAME = ".memory.lock"  # khipu.memory_lock.LOCK_NAME


def _forgotten_job(data: bytes, gate) -> bool:
    """Whether a backed-up file is a queued job (outbox, dead letter,
    quarantine) whose payload is a forgotten source."""
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError):
        return False
    return (isinstance(value, dict) and isinstance(value.get("payload"), dict)
            and not gate.allows(value["payload"]))


def import_local(*, source: Path, merge: bool = True) -> dict:
    """Copy a backup (a directory or a .zip) into the data folder.

    A completed forget must survive the restore of a backup taken before it:
    the backup's tombstone registry is merged into this one, never copied
    over it, and every queued job naming a forgotten source is left behind.
    The backup's replica is written to a staging copy, scrubbed there, and
    only then atomically made the replica, so no reader sees it unscrubbed.
    The whole import holds the memory lock shared, so it never interleaves
    with a forget.
    """
    refuse_if_isolated("an imported data folder")
    from khipu import memory_lock

    with memory_lock.shared():
        return _import_local(source=source, merge=merge)


def _skipped_name(parts: tuple[str, ...]) -> bool:
    """Lock files and the replica's journal: a journal from the backup would
    be replayed into whatever replica is current, so it is never copied."""
    return (parts in ((_REGISTRY_NAME,), (_REGISTRY_LOCK_NAME,), (_MEMORY_LOCK_NAME,))
            or (len(parts) == 1 and parts[0].startswith(_SNAPSHOT_NAME + "-")))


_REPLICA_TEMP = re.compile(r"^\.hub_snapshot\..+\.sqlite(-.+)?$")
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


def _import_local(*, source: Path, merge: bool) -> dict:
    from khipu import hub_snapshot, tombstones

    source = source.expanduser()
    target = ensure_data_dir()
    imported = 0
    skipped_forgotten = 0
    skipped_replica_temps: list[str] = []
    staged: Path | None = None

    def skipped(parts: tuple[str, ...]) -> bool:
        reason = _skip_reason(parts)
        if reason == "replica_temp":
            skipped_replica_temps.append("/".join(parts))
        return reason is not None

    def stage(reader) -> None:
        nonlocal staged
        if staged is None:
            staged = hub_snapshot.staging_path()
        copy_sensitive(reader, staged)

    try:
        if source.is_dir():
            registry = source / _REGISTRY_NAME
            if registry.is_file():
                tombstones.merge_registry_bytes(registry.read_bytes())
            with tombstones.write_gate() as gate:
                for p in source.rglob("*"):
                    if not p.is_file():
                        continue
                    rel = p.relative_to(source)
                    if skipped(rel.parts):
                        continue
                    dst = target / rel
                    if _aliases_protected(target.resolve(), rel.parts, dst.resolve()):
                        raise RuntimeError(f"refusing backup path that aliases a protected file: {rel}")
                    if dst.exists() and not merge:
                        continue
                    if dst.exists() and p.samefile(dst):
                        raise shutil.SameFileError(f"{p!s} and {dst!s} are the same file")
                    if p.suffix == ".json" and _forgotten_job(p.read_bytes(), gate):
                        skipped_forgotten += 1
                        continue
                    if rel.parts == (_SNAPSHOT_NAME,):
                        with p.open("rb") as source_file:
                            stage(source_file)
                        imported += 1
                        continue
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if dst.name in SENSITIVE_FILENAMES:
                        with p.open("rb") as source_file:
                            copy_sensitive(source_file, dst)
                    else:
                        shutil.copy2(p, dst)
                    imported += 1
        elif source.is_file() and source.suffix.lower() == ".zip":
            with zipfile.ZipFile(source, "r") as zf:
                members = _checked_zip_members(zf, target)
                for parts, info, _dest in members:
                    if parts == (_REGISTRY_NAME,):
                        tombstones.merge_registry_bytes(zf.read(info))
                with tombstones.write_gate() as gate:
                    for parts, info, dest in members:
                        if skipped(parts):
                            continue
                        if dest.exists() and not merge:
                            continue
                        if parts[-1].endswith(".json") and _forgotten_job(zf.read(info), gate):
                            skipped_forgotten += 1
                            continue
                        if parts == (_SNAPSHOT_NAME,):
                            with zf.open(info) as source_file:
                                stage(source_file)
                            imported += 1
                            continue
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(info) as source_file:
                            if dest.name in SENSITIVE_FILENAMES:
                                copy_sensitive(source_file, dest)
                            else:
                                with open(dest, "wb") as out:
                                    shutil.copyfileobj(source_file, out)
                        imported += 1
        else:
            raise RuntimeError("import source must be a .zip or a directory")
        scrub = None
        if staged is not None:
            # After the gate is released: publishing takes the registry and
            # then the refresh lock, the order every replica writer uses.
            scrub = hub_snapshot.publish_imported_snapshot(staged)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
    out = {
        "ok": True,
        "imported": imported,
        "skipped_forgotten": skipped_forgotten,
        "target": str(target),
        "source": str(source),
        "merge": merge,
        "skipped_replica_temps": skipped_replica_temps,
    }
    if skipped_replica_temps:
        out["notes"] = [f"left out {len(skipped_replica_temps)} temporary replica "
                        f"{'file' if len(skipped_replica_temps) == 1 else 'files'} a crashed "
                        "refresh left behind; those are never restored"]
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
        "files": list_local_files(),
    }
