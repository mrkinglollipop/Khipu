"""Local Mac data directory for Khipu (not the database)."""
from __future__ import annotations

import json
import os
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

LEGACY_DIR = Path.home() / ".config" / "alzy"
DEFAULT_DIR = Path.home() / ".config" / "khipu"
POINTER_NAME = "data_root.json"


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


def _env_override() -> Path | None:
    raw = (os.environ.get("KHIPU_DATA_DIR") or os.environ.get("ALZY_DATA_DIR") or "").strip()
    return Path(raw).expanduser() if raw else None


def default_data_dir() -> Path:
    return DEFAULT_DIR


def pointer_file() -> Path:
    # Pointer always lives under the default config home so we can find overrides.
    return DEFAULT_DIR / POINTER_NAME


def data_dir() -> Path:
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
    return ensure_data_dir() / "dsn"


def root_cert_file() -> Path:
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
        out.append({"path": rel, "bytes": p.stat().st_size})
    return out


def backup_local(*, dest: Path) -> dict:
    src = ensure_data_dir()
    dest = dest.expanduser()
    if dest.suffix.lower() != ".zip":
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = dest / f"khipu-local-{stamp}.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in src.rglob("*"):
            if not p.is_file():
                continue
            if p.name == POINTER_NAME and src == DEFAULT_DIR:
                continue
            zf.write(p, arcname=str(p.relative_to(src)))
            count += 1
    return {"ok": True, "archive": str(dest), "files": count, "source": str(src)}


def import_local(*, source: Path, merge: bool = True) -> dict:
    source = source.expanduser()
    target = ensure_data_dir()
    imported = 0
    if source.is_dir():
        for p in source.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(source)
            dst = target / rel
            if dst.exists() and not merge:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            if dst.name == "dsn":
                dst.chmod(0o600)
            imported += 1
    elif source.is_file() and source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                # Zip-slip guard. Compare by path components, not by string
                # prefix: with target /a/b, "/a/bc/evil".startswith("/a/b") is
                # True, so a sibling directory escaped the old check entirely
                # (audit 2026-08-17).
                dest = (target / info.filename).resolve()
                if not dest.is_relative_to(target.resolve()):
                    raise RuntimeError(f"refusing unsafe zip path: {info.filename}")
                if dest.exists() and not merge:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                if dest.name == "dsn":
                    dest.chmod(0o600)
                imported += 1
    else:
        raise RuntimeError("import source must be a .zip or a directory")
    return {
        "ok": True,
        "imported": imported,
        "target": str(target),
        "source": str(source),
        "merge": merge,
    }


def paths_status() -> dict:
    d = data_dir()
    return {
        "data_dir": str(d),
        "default_data_dir": str(DEFAULT_DIR),
        "override_env": bool(_env_override()),
        "pointer": str(pointer_file()) if pointer_file().is_file() else None,
        "files": list_local_files(),
    }
