"""Storage hygiene: Khipu never keeps files nobody reads
(docs/plans/2026-10-16-offline-and-self-healing.md, slice 8).

``sweep`` removes what no reader opens again and trims what would otherwise
grow forever. The replica job (``khipu snapshot heal``, every few minutes on
every Mac) runs it at most once a day through ``sweep_if_due``.

Never touched here: memory content, the current replica, the live slots,
the outbox, ``refused/`` and the pending-erasure folder.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

# Every retention number and threshold, changeable in Settings (Health
# checks) and liftable on the spot: a 0 keeps everything.
SETTINGS: dict[str, tuple[str, Any, str]] = {
    "disk_warn_free_gb": ("number", 10, "Warn when the disk that holds Khipu's data or its temporary "
                                        "files has less than this many GB free"),
    "disk_warn_free_percent": ("number", 5, "Warn when less than this percent of that disk is free"),
    "disk_fail_free_gb": ("number", 3, "Show red and hold back updates when less than this many GB is free"),
    "disk_fail_free_percent": ("number", 2, "Show red and hold back updates when less than this percent "
                                            "of the disk is free"),
    "commitments_backup_keep_days": ("count", 30, "Keep commitments backups for this many days "
                                                  "(0: keep every backup)"),
    "synth_usage_keep_days": ("count", 90, "Keep this many days of the summariser's token counts "
                                           "(0: keep every line)"),
    "log_max_mb": ("number", 5, "Start a log afresh when it passes this many MB, keeping one older "
                                "copy (0: no cap)"),
}
_PERCENTS = ("disk_warn_free_percent", "disk_fail_free_percent")

SWEEP_EVERY_S = 24 * 3600
SWEEP_STATE_NAME = "storage-sweep.json"
# Replaced by prompt-query-embed-cache.sqlite3 (recall_prompt); no reader.
LEGACY_QUERY_CACHE = "prompt-query-embed-cache.json"


def _coerce(kind: str, value: Any, key: str) -> Any:
    from khipu import routing

    out = routing._coerce(kind, value, key)
    if key in _PERCENTS and out > 100:
        raise ValueError(f"{key} must be a percent from 0 to 100, got {value!r}")
    return out


def settings(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Each setting's value: the stored one when valid, else the default."""
    if config is None:
        from khipu.config import load_config

        config = load_config()
    out: dict[str, Any] = {}
    for key, (kind, default, _words) in SETTINGS.items():
        value = default
        if config.get(key) is not None:
            try:
                value = _coerce(kind, config[key], key)
            except (ValueError, TypeError):
                value = default
        out[key] = value
    return out


def settings_status() -> dict[str, dict]:
    from khipu.config import load_config

    config = load_config()
    values = settings(config)
    out = {}
    for key, (kind, default, words) in SETTINGS.items():
        source = "default"
        if config.get(key) is not None:
            try:
                _coerce(kind, config[key], key)
                source = "file"
            except (ValueError, TypeError):
                source = "invalid"
        out[key] = {"value": values[key], "default": default, "kind": kind, "source": source, "words": words}
    return out


def set_setting(key: str, raw: Any) -> Path:
    """Store a setting (``None`` removes it: the default again)."""
    from khipu.config import config_file, mutate_config

    if key not in SETTINGS:
        raise KeyError(f"unknown storage setting {key!r}")
    with mutate_config() as data:
        if raw is None:
            data.pop(key, None)
        else:
            data[key] = _coerce(SETTINGS[key][0], raw, key)
    return config_file()


def _state_dir() -> Path:
    from khipu.paths import data_dir

    return data_dir() / "state"


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _remove(path: Path, out: dict[str, Any], what: str) -> None:
    size = _size(path)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        out["errors"].append(f"{what}: {describe_os_error(exc)}")
        return
    out["removed"].append(what)
    out["freed_bytes"] += size


def describe_os_error(exc: BaseException) -> str:
    """An OS error in plain words; a full disk is named as one."""
    import errno

    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
        where = f" ({exc.filename})" if getattr(exc, "filename", None) else ""
        return f"the disk is full: no space left to write{where}"
    return f"{type(exc).__name__}: {exc}"


# ---- logs and usage lines --------------------------------------------------
#
# Every log Khipu appends to without a cap of its own. Capped elsewhere and
# left alone here: the search query log (query_log, 5 MB), embed timings
# (embed, the last N lines) and the routing record (routing). promotions.jsonl
# is the promotion audit record and is never trimmed.


def _seen_before(st: os.stat_result, seen: set[tuple[int, int]]) -> bool:
    key = (st.st_dev, st.st_ino)
    if key in seen:
        return True
    seen.add(key)
    return False


def log_dirs() -> list[Path]:
    """The folders Khipu's logs live in, each once (on a disk that ignores
    case ``Logs/Khipu`` and ``Logs/khipu`` are one folder)."""
    from khipu import jobs, session_capture
    from khipu.paths import data_dir

    candidates = [jobs.LOG_DIR, jobs.LOG_DIR_LEGACY, jobs.LOG_DIR_CONFIG,
                  # bin/khipu-stop-hook's stderr; data_dir()/logs in an isolated run.
                  Path.home() / "Library" / "Logs" / "khipu", data_dir() / "logs",
                  session_capture.khipu_home() / "logs"]
    out: list[Path] = []
    seen: set[tuple[int, int]] = set()
    for directory in candidates:
        if directory is None:
            continue
        try:
            st = directory.stat()
        except OSError:
            continue
        if not _seen_before(st, seen):
            out.append(directory)
    return out


def log_files() -> list[Path]:
    """Every ``*.log`` in the log folders, plus the promotion retry's output."""
    from khipu.promote import config_dir

    out = []
    for directory in log_dirs():
        try:
            out.extend(sorted(p for p in directory.glob("*.log") if p.is_file() and not p.is_symlink()))
        except OSError:
            continue
    retry = config_dir() / "state" / "promote-retry.log"
    if retry.is_file() and not retry.is_symlink():
        out.append(retry)
    return out


def _rotate_log(path: Path, cap_bytes: int, out: dict[str, Any]) -> None:
    """Move a log over the cap to ``<name>.1`` (replacing an older one) and
    start it afresh.

    The lines are copied and the log emptied in place rather than renamed:
    a running daemon's launchd output stays open, and after a rename it would
    write into the ``.1`` forever. The log keeps its time, which job status
    reads as a run's evidence. When the copy cannot be made (the disk is
    full), the log is renamed, which needs no space."""
    try:
        st = path.stat()
    except OSError:
        return
    if st.st_size <= cap_bytes:
        return
    older = path.with_name(path.name + ".1")
    tmp = path.with_name(f".{path.name}.1.tmp")
    out["freed_bytes"] += _size(older)
    try:
        older.unlink(missing_ok=True)
        try:
            shutil.copyfile(path, tmp)
            os.replace(tmp, older)
            with open(path, "r+b") as fh:
                fh.truncate(0)
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError:
            tmp.unlink(missing_ok=True)
            os.replace(path, older)
    except OSError as exc:
        out["errors"].append(f"log {path.name}: {describe_os_error(exc)}")
        return
    out["rotated"].append(path.name)


def _ts(line: str) -> datetime | None:
    try:
        row = json.loads(line)
        at = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
    except (ValueError, TypeError, KeyError):
        return None
    return at if at.tzinfo is not None else None


def trim_synth_usage(days: int, *, now: datetime | None = None) -> dict[str, Any]:
    """Keep the last ``days`` days of state/synth-usage.jsonl (0 keeps every
    line); a line that does not parse is dropped. The file is rewritten
    beside itself and swapped in; a line appended while it was rewritten is
    carried over."""
    from khipu.extract import synth_usage_path
    from khipu.lines import split_lines

    out = {"dropped": 0, "kept": 0}
    path = synth_usage_path()
    if days <= 0 or not path.is_file():
        return out
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    data = path.read_bytes()
    kept = []
    for line in split_lines(data):
        at = _ts(line)
        if at is not None and at >= cutoff:
            kept.append(line)
    out["kept"] = len(kept)
    out["dropped"] = len([ln for ln in split_lines(data) if ln.strip()]) - len(kept)
    if not out["dropped"]:
        return out
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write("".join(f"{ln}\n" for ln in kept).encode("utf-8"))
            with path.open("rb") as current:
                current.seek(len(data))
                fh.write(current.read())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return out


def sweep() -> dict[str, Any]:
    """One pass over everything Khipu keeps that nobody reads."""
    out: dict[str, Any] = {"ok": True, "removed": [], "rotated": [], "freed_bytes": 0, "errors": []}
    try:
        limits = settings()
    except Exception as exc:  # noqa: BLE001 — an unreadable config falls back to the defaults
        out["errors"].append(f"settings: {describe_os_error(exc)}")
        limits = {key: spec[1] for key, spec in SETTINGS.items()}
    _remove(_state_dir() / LEGACY_QUERY_CACHE, out, f"state/{LEGACY_QUERY_CACHE}")
    cap = float(limits["log_max_mb"])
    if cap > 0:
        for log in log_files():
            _rotate_log(log, int(cap * 1024 * 1024), out)
    try:
        trimmed = trim_synth_usage(int(limits["synth_usage_keep_days"]))
        if trimmed["dropped"]:
            out["synth_usage_dropped"] = trimmed["dropped"]
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"state/synth-usage.jsonl: {describe_os_error(exc)}")
    try:
        from khipu import hub_snapshot

        temps = hub_snapshot.sweep_stale_temps()
        out["removed"].extend(temps.get("removed") or [])
    except Exception as exc:  # noqa: BLE001 — one store's miss never stops the rest
        out["errors"].append(f"replica temps: {describe_os_error(exc)}")
    out["ok"] = not out["errors"]
    return out


def _sweep_state_path() -> Path:
    return _state_dir() / SWEEP_STATE_NAME


def sweep_if_due(*, now: datetime | None = None) -> dict[str, Any] | None:
    """``sweep`` when the last one is a day old or more, else None."""
    now = now or datetime.now(timezone.utc)
    path = _sweep_state_path()
    try:
        last = datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["at"])
        if 0 <= (now - last).total_seconds() < SWEEP_EVERY_S:
            return None
    except (OSError, ValueError, KeyError, TypeError):
        pass
    out = sweep()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"at": now.isoformat(), "result": out}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        out["errors"].append(f"sweep record: {describe_os_error(exc)}")
    return out
