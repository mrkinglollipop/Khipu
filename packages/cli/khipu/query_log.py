"""Search query log — the free training set for W6's golden queries and the
zero-result detector (W2.5). Append-only JSONL under the state dir
(``data_dir()/query_log.jsonl``), rotated at 5 MB. Called from both the MCP
``khipu_search`` tool and the ``khipu search`` CLI command, right after a
search returns. Fail-open by design: a logging bug must never break search.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

MAX_BYTES = 5 * 1024 * 1024
LOG_NAME = "query_log.jsonl"
ROTATED_SUFFIX = ".1"


def log_path() -> Path:
    from khipu.paths import data_dir

    return data_dir() / LOG_NAME


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.is_file() and path.stat().st_size >= MAX_BYTES:
            rotated = path.with_name(path.name + ROTATED_SUFFIX)
            path.replace(rotated)
    except OSError:
        pass


def _default_harness() -> str:
    return (os.environ.get("KHIPU_HARNESS") or "").strip() or "mcp"


def log_query(
    query: str,
    *,
    mode: str,
    filters: dict[str, Any] | None = None,
    result_count: int,
    top: list[dict[str, Any]] | None = None,
    harness: str | None = None,
) -> None:
    """Append one search to the log. Never raises.

    ``filters`` is stored with empty/None values dropped, so a query log line
    reads as "what was actually constrained", not a fixed-shape record full
    of nulls.
    """
    try:
        from khipu.paths import ensure_data_dir

        ensure_data_dir()
        path = log_path()
        _rotate_if_needed(path)
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "harness": (harness or "").strip() or _default_harness(),
            "query": query,
            "mode": mode,
            "filters": {k: v for k, v in (filters or {}).items() if v},
            "result_count": int(result_count),
            "top": [
                {"kind": r.get("kind"), "id": r.get("id"), "score": r.get("score")}
                for r in (top or [])[:3]
            ],
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except Exception:  # noqa: BLE001 — logging must never break a search
        pass


def _read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def tail(n: int = 20) -> list[dict[str, Any]]:
    """The last ``n`` log entries, newest last (matches file order)."""
    lines = _read_lines(log_path())[-max(1, int(n)) :]
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def zero_results(days: int = 7) -> list[dict[str, Any]]:
    """Queries logged with ``result_count == 0`` in the last ``days`` days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, int(days)))
    out: list[dict[str, Any]] = []
    for line in _read_lines(log_path()):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("result_count") != 0:
            continue
        ts_raw = entry.get("ts")
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if ts >= cutoff:
            out.append(entry)
    return out
