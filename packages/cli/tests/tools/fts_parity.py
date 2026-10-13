"""Parity harness: the FTS path of ``search_snapshot`` must return exactly the
rows, in exactly the order, of the LIKE path, on a real replica.

    python3 tests/tools/fts_parity.py /path/to/copy-of-hub_snapshot.sqlite

Run it on a ``.backup`` copy, never the live replica: it adds the FTS tables
to the file if they are missing. Queries come from the local query log (last
400 distinct) and the recall golden set; the output names them only by index,
never by text. No network, no Postgres.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from khipu import hub_snapshot as hs  # noqa: E402

CONFIG = Path.home() / ".config" / "khipu"
LOG_QUERIES = 400


def _mb(path: Path) -> str:
    return f"{path.stat().st_size / 1e6:.1f} MB"


def ensure_fts(path: Path) -> None:
    before = _mb(path)
    con = sqlite3.connect(str(path))
    try:
        rows = dict(con.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('episodes_fts', 'topics_fts')"
        ).fetchall())
        if rows and not all("trigram" in (sql or "") for sql in rows.values()):
            sys.exit("replica has FTS tables with another tokenizer; use a fresh copy")
        started = time.perf_counter()
        if len(rows) < 2:
            con.executescript(hs._FTS_SCHEMA_SQL)
        for table in ("episodes_fts", "topics_fts"):
            con.execute(f"INSERT INTO {table}({table}) VALUES ('rebuild')")
        con.commit()
        elapsed = time.perf_counter() - started
    finally:
        con.close()
    print(f"rebuild: {elapsed:.1f} s; size before {before}, after {_mb(path)}")


def load_queries() -> tuple[list[str], int, int]:
    seen: set[str] = set()
    logged: list[str] = []
    log = CONFIG / "query_log.jsonl"
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines() if log.is_file() else []
    for line in reversed(lines):
        try:
            q = json.loads(line).get("query")
        except (json.JSONDecodeError, AttributeError):
            continue
        if not isinstance(q, str) or not q.strip() or q in seen:
            continue
        seen.add(q)
        logged.append(q)
        if len(logged) >= LOG_QUERIES:
            break
    golden: list[str] = []
    gpath = CONFIG / "recall-golden.jsonl"
    if gpath.is_file():
        for line in gpath.read_text(encoding="utf-8").splitlines():
            try:
                q = json.loads(line).get("query")
            except (json.JSONDecodeError, AttributeError):
                continue
            if isinstance(q, str) and q.strip():
                golden.append(q)
    return logged + golden, len(logged), len(golden)


def _pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))] if xs else 0.0


def _keys(rows) -> list[tuple[str, str]]:
    return [(r["kind"], str(r["id"])) for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("replica", type=Path)
    args = ap.parse_args()
    replica = args.replica.resolve()
    live = (CONFIG / "hub_snapshot.sqlite").resolve()
    if replica == live:
        sys.exit("refusing to modify the live replica; pass a .backup copy")
    ensure_fts(replica)

    queries, n_log, n_golden = load_queries()
    print(f"queries: {len(queries)} ({n_log} from the query log, {n_golden} golden, "
          f"golden indexes {n_log}..{len(queries) - 1})")

    scratch = Path(tempfile.mkdtemp(prefix="fts-parity-"))
    hs._FTS_STATE.update(key=None, ready=False)
    like_ms: list[float] = []
    fts_ms: list[float] = []
    like_on_fts_ms: list[float] = []
    took_fts = fell_back = identical = 0
    diffs: list[tuple[int, list, list]] = []
    errors: list[tuple[int, str]] = []
    with (
        mock.patch.object(hs, "snapshot_path", return_value=replica),
        mock.patch.object(hs, "meta_path", return_value=scratch / "meta.json"),
    ):
        for i, q in enumerate(queries):
            try:
                with mock.patch.object(hs, "_fts_ready", return_value=False):
                    t0 = time.perf_counter()
                    like = _keys(hs.search_snapshot(q, 12, kind=None))
                    like_elapsed = (time.perf_counter() - t0) * 1000
                    like_ms.append(like_elapsed)
                with (
                    mock.patch.object(hs, "_fts_match_terms", wraps=hs._fts_match_terms) as used,
                    mock.patch.object(hs, "_fts_fallback", wraps=hs._fts_fallback) as failed,
                ):
                    t0 = time.perf_counter()
                    fts = _keys(hs.search_snapshot(q, 12, kind=None))
                    elapsed = (time.perf_counter() - t0) * 1000
            except Exception as exc:  # noqa: BLE001 - report and keep going
                errors.append((i, type(exc).__name__))
                continue
            if not used.called or failed.called:
                fell_back += 1
                continue
            took_fts += 1
            fts_ms.append(elapsed)
            like_on_fts_ms.append(like_elapsed)
            if fts == like:
                identical += 1
            else:
                diffs.append((i, like, fts))

    print(f"took FTS: {took_fts}; fell back to LIKE: {fell_back}; errors: {len(errors)}")
    pct = 100.0 * identical / took_fts if took_fts else 0.0
    print(f"identical on the FTS path: {identical}/{took_fts} ({pct:.1f}%)")
    for i, like, fts in diffs[:5]:
        print(f"  query #{i}\n    LIKE {like}\n    FTS  {fts}")
    for i, name in errors[:5]:
        print(f"  query #{i} raised {name}")
    for label, xs in (("LIKE, all queries", like_ms), ("LIKE, FTS-path queries", like_on_fts_ms),
                      ("FTS, FTS-path queries", fts_ms)):
        print(f"{label}: p50 {_pct(xs, 0.5):.1f} ms, p90 {_pct(xs, 0.9):.1f} ms, "
              f"max {max(xs, default=0.0):.1f} ms")
    print(f"load average: {' '.join(f'{x:.2f}' for x in os.getloadavg())}")
    return 0 if took_fts and identical == took_fts and not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
