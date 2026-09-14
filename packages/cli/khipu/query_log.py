"""Search query log — the free training set for W6's golden queries and the
zero-result detector (W2.5). Append-only JSONL under the state dir
(``data_dir()/query_log.jsonl``), rotated at 5 MB. Called from both the MCP
``khipu_search`` tool and the ``khipu search`` CLI command, right after a
search returns. Fail-open by design: a logging bug must never break search.
"""
from __future__ import annotations

import hashlib
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
    redact: bool = False,
    degraded: str | None = None,
) -> None:
    """Append one search to the log. Never raises.

    ``redact=True`` stores a hash and the length instead of the query text —
    the gateway host is on the public internet and its disk must never hold
    what people asked (audit 2026-09-04).

    ``filters`` is stored with empty/None values dropped, so a query log line
    reads as "what was actually constrained", not a fixed-shape record full
    of nulls.

    ``degraded`` (F4) is ``hybrid_search``'s own ``degraded`` reason
    ("no-embedding" / "embed-budget" / "embed-unavailable") when the
    semantic leg didn't run and the search fell back to literal + lexical —
    stored so ``degraded_count`` (and ``khipu status``'s
    ``search_degraded_last_24h``) can aggregate it instead of it staying a
    buried key nothing ever reads.
    """
    try:
        from khipu.paths import ensure_data_dir

        ensure_data_dir()
        path = log_path()
        _rotate_if_needed(path)
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "harness": (harness or "").strip() or _default_harness(),
            "query": None if redact else query,
            "mode": mode,
            "filters": {k: v for k, v in (filters or {}).items() if v},
            "result_count": int(result_count),
            **({"query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest()[:16],
                "query_len": len(query)} if redact else {}),
            **({"degraded": degraded} if degraded else {}),
            "top": [
                {"kind": r.get("kind"), "id": r.get("id"), "score": r.get("score")}
                for r in (top or [])[:3]
            ],
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except Exception:  # noqa: BLE001 — logging must never break a search
        pass


def degraded_count(*, hours: int = 24) -> int:
    """F4: how many searches degraded (any non-empty ``degraded`` reason)
    in the last ``hours`` — the aggregate ``khipu status``'s
    ``search_degraded_last_24h`` reports. Never raises; an unreadable log
    reads as 0."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(0, int(hours)))
    n = 0
    for line in _read_lines(log_path()):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not entry.get("degraded"):
            continue
        ts_raw = entry.get("ts")
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if ts >= cutoff:
            n += 1
    return n


def degraded_rate(*, n: int = 50) -> dict[str, Any]:
    """D6/F4: fraction of the last `n` searches that degraded — a rate, not
    just `degraded_count`'s raw 24h total, so a burst of degraded searches
    shows even on a day with heavy volume. Red past 20% (D6's threshold); no
    searches yet reads as ok with a 0 sample, not red-on-idleness."""
    entries = tail(n)
    if not entries:
        return {"ok": True, "rate": 0.0, "sampled": 0, "degraded": 0}
    degraded = sum(1 for e in entries if e.get("degraded"))
    rate = degraded / len(entries)
    return {"ok": rate <= 0.20, "rate": rate, "sampled": len(entries), "degraded": degraded}


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
