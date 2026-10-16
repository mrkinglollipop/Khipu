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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SWEEP_EVERY_S = 24 * 3600
SWEEP_STATE_NAME = "storage-sweep.json"
# Replaced by prompt-query-embed-cache.sqlite3 (recall_prompt); no reader.
LEGACY_QUERY_CACHE = "prompt-query-embed-cache.json"


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


def sweep() -> dict[str, Any]:
    """One pass over everything Khipu keeps that nobody reads."""
    out: dict[str, Any] = {"ok": True, "removed": [], "freed_bytes": 0, "errors": []}
    _remove(_state_dir() / LEGACY_QUERY_CACHE, out, f"state/{LEGACY_QUERY_CACHE}")
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
