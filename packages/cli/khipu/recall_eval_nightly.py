"""Recall accuracy as a nightly measurement: the ``recall_eval`` nightly step,
the doctor row that reads it, and ``khipu recall eval --install``.

The step scores the maintainer-local golden set (``recall_eval``) on every
path against this Mac's replica only, with every hub, embedding, network and
subprocess path refused (``_offline``): no golden query leaves the machine,
the run adds no hub load and cannot be slowed by an outage. Entries whose
expected ids are now forgotten, erased or deleted are skipped, never counted
as misses (a forgotten episode that no longer answers is the forget working).

``<data dir>/state/recall-eval-history.json`` keeps the last 30 run records:
figures, counts and the golden file's size and mtime. It never holds a query
or an id, so it is not owner text and forget has nothing to scan in it.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from khipu import recall_eval
from khipu.lines import split_lines

HISTORY_KEEP = 30
BUDGET_S = 300
MIN_ENTRIES = 20
STALE_AFTER_H = 48
ALLOWED_DROP = 0.05
LINGER_WAIT_S = 30.0
FAILED_SHARE = 0.25
HIT_FLOOR = 0.6
GOLDEN_TEMP_PREFIX = ".recall-golden."
BACKUP_NAME = ".recall-golden.bak.tmp"
_KEPT_METRICS = ("total", "hits", "hit_rate", "mrr", "abstain_total", "abstain_correct",
                 "stale_violations", "timeouts", "errors")


def history_path() -> Path:
    from khipu.paths import data_dir

    return data_dir() / "state" / "recall-eval-history.json"


def read_history() -> list[dict[str, Any]]:
    try:
        data = json.loads(history_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    records = data.get("records") if isinstance(data, dict) else None
    return [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []


def _write_history(records: list[dict[str, Any]]) -> None:
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".recall-eval-history.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(json.dumps({"records": records[-HISTORY_KEEP:]}, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class _Offline:
    """What the offline guard refused during one run, by kind. Counts only."""

    def __init__(self) -> None:
        self.refused = {"hub": 0, "embed": 0, "network": 0, "process": 0}


@contextlib.contextmanager
def _offline():
    """Every way off this Mac refuses for the run, so retrieval answers from
    the replica alone and no golden query leaves the machine: hub connects
    (``try_hub_connect``, ``db.connect`` and ``psycopg.connect`` underneath
    both), embedding calls (the cosine leg is skipped and counted, unless the
    local query-vector cache already holds the vector), every socket connect
    and name lookup (any provider or HTTP client), and subprocess spawns (a
    helper process would sit outside these patches). The hub refusal reads as
    an unreachable hub, so callers take their replica fallback; the
    reachability memo the live hooks read is never written."""
    from khipu import db, embed, hub_reach, hub_snapshot

    guard = _Offline()
    try:
        import psycopg

        hub_exc: type[Exception] = psycopg.OperationalError
    except ImportError:  # pragma: no cover - psycopg ships with the CLI
        psycopg = None
        hub_exc = ConnectionError

    def refuser(kind: str, exc_type: type[Exception], message: str):
        def refuse(*_a: Any, **_k: Any):
            guard.refused[kind] += 1
            raise exc_type(message)
        return refuse

    hub = refuser("hub", hub_exc, "connection refused: the recall eval reads the local replica only")
    no_embed = refuser("embed", RuntimeError, "embedding refused: the recall eval runs offline")
    no_net = refuser("network", ConnectionRefusedError, "network refused: the recall eval runs offline")
    no_proc = refuser("process", PermissionError, "subprocess refused: the recall eval runs offline")
    targets: list[tuple[Any, str, Any]] = [
        (hub_snapshot, "try_hub_connect", hub), (db, "connect", hub),
        (embed, "embed_one", no_embed), (embed, "embed_batch", no_embed),
        (embed, "embed_batch_images", no_embed), (embed, "_post_json", no_embed),
        (socket.socket, "connect", no_net), (socket.socket, "connect_ex", no_net),
        (socket, "create_connection", no_net), (socket, "getaddrinfo", no_net),
        (subprocess, "Popen", no_proc),
        (hub_reach, "note_unreachable", lambda *_a, **_k: None),
        (hub_reach, "note_reachable", lambda *_a, **_k: None),
    ]
    if psycopg is not None:
        targets.append((psycopg, "connect", hub))
    saved = [(obj, name, getattr(obj, name)) for obj, name, _ in targets if hasattr(obj, name)]
    for obj, name, fake in targets:
        if hasattr(obj, name):
            setattr(obj, name, fake)
    try:
        yield guard
    finally:
        for obj, name, real in saved:
            setattr(obj, name, real)


def _join_new_threads(before: set[int], wait_s: float) -> int:
    """Wait (bounded) for threads the run started, so none is still reaching
    for a refused path when the guard comes off. Returns how many remain."""
    end = time.monotonic() + wait_s
    for t in threading.enumerate():
        if t.ident not in before and t is not threading.current_thread():
            t.join(max(0.0, end - time.monotonic()))
    return sum(1 for t in threading.enumerate() if t.ident not in before and t.is_alive()
               and t is not threading.current_thread())


def _row_exists(con, sql: str, arg: Any) -> tuple[bool, Any] | None:
    """(found, first column) for a one-row lookup; None when the replica has
    no such table (an old replica: nothing to judge)."""
    try:
        row = con.execute(sql, (arg,)).fetchone()
    except Exception:  # noqa: BLE001 - a missing table or column
        return None
    return (row is not None, row[0] if row else None)


def _unavailable_ids(ids: set[str], con) -> set[str]:
    """Ids that no longer answer: forgotten, erased or deleted episodes (the
    replica's episodes view already hides all three), and topic slugs or
    graph node ids the replica no longer holds or marks deleted. Library
    chunk ids are not in the replica and are never judged."""
    gone: set[str] = set()
    try:
        from khipu import tombstones

        forgotten = {str(i) for i in tombstones.forgotten_episode_ids()}
    except Exception:  # noqa: BLE001 - the replica view still covers forgotten rows
        forgotten = set()
    for ident in ids:
        if ident in forgotten:
            gone.add(ident)
        elif ident.isdigit():
            if con.execute("SELECT 1 FROM episodes WHERE id = ?", (int(ident),)).fetchone() is None:
                gone.add(ident)
        elif not ident.startswith("library:"):
            topic = _row_exists(con, "SELECT deleted_at FROM topics WHERE slug = ?", ident)
            if topic is not None and topic[0]:
                if topic[1] is not None:
                    gone.add(ident)
                continue
            node = _row_exists(con, "SELECT 1 FROM nodes WHERE id = ?", ident)
            if node is not None and node[0]:
                continue
            if topic is not None or node is not None:
                gone.add(ident)
    return gone


_ID_TYPES = (str, int)


def entry_problem(entry: Any) -> str | None:
    """Why a golden entry cannot be scored, as a short reason code (never the
    query), or None when its shape and types are sound."""
    if not isinstance(entry, dict):
        return "not_an_object"
    query = entry.get("query")
    if not isinstance(query, str) or not query.strip():
        return "bad_query"
    expect, expect_none = entry.get("expect"), entry.get("expect_none")
    if expect_none not in (None, True, False):
        return "bad_expect_none"
    if expect is not None and (not isinstance(expect, list) or any(
            isinstance(x, bool) or not isinstance(x, _ID_TYPES) for x in expect)):
        return "bad_expect"
    if bool(expect) == bool(expect_none):
        return "bad_expect"
    k = entry.get("k")
    if k is not None and (isinstance(k, bool) or not isinstance(k, int) or k < 1):
        return "bad_k"
    if entry.get("mode") is not None and entry["mode"] not in ("hybrid", "literal", "semantic"):
        return "bad_mode"
    paths = entry.get("paths")
    if paths is not None and (not isinstance(paths, list)
                              or any(p not in recall_eval.ALL_PATHS for p in paths)):
        return "bad_paths"
    stale = entry.get("stale")
    if stale is not None and (not isinstance(stale, list) or any(
            isinstance(x, bool) or not isinstance(x, _ID_TYPES) for x in stale)):
        return "bad_stale"
    for key in ("project", "cwd", "since", "until", "note", "source"):
        if entry.get(key) is not None and not isinstance(entry[key], str):
            return f"bad_{key}"
    n = entry.get("n")
    if n is not None and (isinstance(n, bool) or not isinstance(n, int)):
        return "bad_n"
    return None


def _load_entries(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The golden file's sound entries, and a count of malformed lines by
    reason. One bad line is skipped, never fatal (``load_golden`` raises)."""
    entries: list[dict[str, Any]] = []
    malformed: dict[str, int] = {}
    for raw in split_lines(path.read_text(encoding="utf-8")):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            problem: str | None = "bad_json"
        else:
            problem = entry_problem(entry)
        if problem:
            malformed[problem] = malformed.get(problem, 0) + 1
        else:
            entries.append(entry)
    return entries, malformed


def _golden_stat(path: Path) -> dict[str, Any]:
    try:
        st = path.stat()
    except OSError:
        return {"bytes": None, "mtime": None}
    return {"bytes": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()}


def _completed(rec: dict[str, Any]) -> bool:
    return bool(rec.get("ok", True)) and not rec.get("truncated") and bool(rec.get("paths"))


def _previous_figures(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The last completed run's figures: a stopped or failed run is no baseline."""
    for rec in reversed(history):
        if _completed(rec) and isinstance(rec.get("paths"), dict):
            return {"ts": rec.get("ts"), "paths": {
                p: {"hit_rate": m.get("hit_rate"), "mrr": m.get("mrr")}
                for p, m in rec["paths"].items() if isinstance(m, dict)}}
    return None


def _score_entry(entry: dict[str, Any], deadline: float) -> list[tuple[str, dict[str, Any]]] | None:
    """Every applicable path's row for one entry; None when the deadline
    stopped it mid-entry (its partial rows are dropped, not counted)."""
    rows = []
    for p in recall_eval.ALL_PATHS:
        if recall_eval._entry_applies(entry, p):
            row = recall_eval.eval_one_path(entry, p, deadline=deadline)
            if row.get("reason") == recall_eval.DEADLINE_REASON:
                return None
            rows.append((p, row))
    return rows


def run_nightly_eval(*, golden: Path | None = None, budget_s: float = BUDGET_S) -> dict[str, Any]:
    """The nightly step. ``ok`` means the measurement ran; a regression is the
    doctor row's to flag. The history record is written on every outcome.
    Returns the compact record it stored."""
    from khipu import hub_snapshot

    now = datetime.now(timezone.utc)
    path = golden or recall_eval.default_golden_path()
    history = read_history()
    record: dict[str, Any] = {
        "ts": now.isoformat(), "ok": True, "entries": 0, "scored": 0,
        "skipped_forgotten": 0, "skipped_ids": 0, "skipped_malformed": 0, "truncated": False,
        "paths": {}, "previous": _previous_figures(history), "golden": _golden_stat(path),
    }
    try:
        try:
            entries, malformed = _load_entries(path) if path.is_file() else ([], {})
        except (OSError, ValueError) as exc:
            record.update(ok=False, transient=True, error=f"golden file unreadable ({type(exc).__name__})")
            entries, malformed = [], {}
        record["entries"] = len(entries) + sum(malformed.values())
        record["skipped_malformed"] = sum(malformed.values())
        if malformed:
            record["malformed"] = malformed
        gone: set[str] = set()
        if entries:
            try:
                con = hub_snapshot.open_snapshot()
            except Exception as exc:  # noqa: BLE001 - no replica: nothing to score against
                record.update(ok=False, transient=True, error=f"replica unavailable ({type(exc).__name__})")
                entries = []
            else:
                try:
                    named = {str(x) for e in entries for x in (e.get("expect") or [])}
                    gone = _unavailable_ids(named, con)
                finally:
                    con.close()
                record["skipped_ids"] = len(gone)
        if entries:
            _score_all(entries, gone, record, budget_s)
    finally:
        _write_history([*history, record])
    return record


def _score_all(entries: list[dict[str, Any]], gone: set[str], record: dict[str, Any],
               budget_s: float) -> None:
    scoring: list[dict[str, Any]] = []
    for entry in entries:
        expect = [str(x) for x in (entry.get("expect") or [])]
        if expect and all(x in gone for x in expect):
            record["skipped_forgotten"] += 1
            continue
        scoring.append({**entry, "expect": [x for x in expect if x not in gone]}
                       if expect else entry)
    deadline = time.monotonic() + budget_s
    rows: dict[str, list[dict[str, Any]]] = {p: [] for p in recall_eval.ALL_PATHS}
    threads_before = {t.ident for t in threading.enumerate()}
    with _offline() as guard:
        try:
            for entry in scoring:
                if time.monotonic() >= deadline:
                    record["truncated"] = True
                    break
                try:
                    got = _score_entry(entry, deadline)
                except Exception:  # noqa: BLE001 - one bad entry is skipped, never fatal
                    record["skipped_malformed"] += 1
                    continue
                if got is None:
                    record["truncated"] = True
                    break
                for p, row in got:
                    rows[p].append(row)
                record["scored"] += 1
        finally:
            record["lingering_threads"] = _join_new_threads(threads_before, LINGER_WAIT_S)
    record["offline"] = {**guard.refused, "cosine_skipped": guard.refused["embed"] > 0}
    for p, got_rows in rows.items():
        summary = recall_eval.summarize_rows(got_rows)
        record["paths"][p] = {k: summary[k] for k in _KEPT_METRICS}


# ---- doctor row --------------------------------------------------------------

def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def recall_eval_health(*, now: datetime | None = None) -> dict[str, Any]:
    """The doctor row. ``level`` is green / yellow / red; yellow never gates
    (``ok`` stays true), red does."""
    from khipu.jobs import _is_index_sync_host

    if not _is_index_sync_host():
        return {"ok": True, "applicable": False,
                "note": "not the nightly host — evaluated on the sync host"}
    history = read_history()
    if not history:
        return {"ok": True, "level": "yellow",
                "summary": "Not measured yet. The first recall check runs with tonight's nightly."}
    last = history[-1]
    now = now or datetime.now(timezone.utc)
    try:
        age_h = (now - datetime.fromisoformat(str(last.get("ts")))).total_seconds() / 3600
    except (TypeError, ValueError):
        age_h = None
    paths = last.get("paths") if isinstance(last.get("paths"), dict) else {}
    violations = sum(int(m.get("stale_violations") or 0) for m in paths.values() if isinstance(m, dict))
    prompt = paths.get(recall_eval.PATH_PROMPT) if isinstance(paths.get(recall_eval.PATH_PROMPT), dict) else {}
    positives = int(prompt.get("total") or 0) - int(prompt.get("abstain_total") or 0)
    rate = float(prompt["hit_rate"]) if positives > 0 and prompt.get("hit_rate") is not None else None
    prev = ((last.get("previous") or {}).get("paths") or {}).get(recall_eval.PATH_PROMPT) or {}
    prev_rate = prev.get("hit_rate")
    detail = {"ts": last.get("ts"), "age_hours": None if age_h is None else round(age_h, 1),
              "prompt_hit_rate": rate, "previous_prompt_hit_rate": prev_rate,
              "stale_violations": violations, "entries": last.get("entries"),
              "skipped_forgotten": last.get("skipped_forgotten")}

    def out(level: str, summary: str) -> dict[str, Any]:
        return {"ok": level != "red", "level": level, "summary": summary, **detail}

    if not last.get("ok", True):
        return out("yellow", f"Tonight's recall check could not run: {last.get('error') or 'unknown reason'}.")
    if violations:
        return out("red", f"{violations} memories you forgot or replaced came back in search results. "
                          "Run `khipu recall eval --path all` to see which.")
    if last.get("truncated"):
        return out("yellow", f"The recall check stopped at its {BUDGET_S // 60}-minute limit before "
                             "every query ran, so tonight's figures are not compared.")
    failed = int(prompt.get("timeouts") or 0) + int(prompt.get("errors") or 0)
    total = int(prompt.get("total") or 0)
    if total and failed > FAILED_SHARE * total:
        return out("yellow", f"{failed} of {total} recall lookups failed or timed out, "
                             "so tonight's figures are not compared.")
    if rate is not None and isinstance(prev_rate, (int, float)) and rate < prev_rate - ALLOWED_DROP - 1e-9:
        return out("red", f"Recall got worse: the right memory is found {_pct(rate)} of the time, "
                          f"down from {_pct(prev_rate)} the run before.")
    if rate is not None and rate < HIT_FLOOR:
        return out("red", f"Recall is below the {_pct(HIT_FLOOR)} floor: the right memory is found "
                          f"{_pct(rate)} of the time.")
    if age_h is None or age_h > STALE_AFTER_H:
        return out("yellow", "The recall check has not run in over 48 hours.")
    if int(last.get("entries") or 0) < MIN_ENTRIES:
        return out("yellow", f"Only {last.get('entries') or 0} test queries in the golden set; "
                             f"{MIN_ENTRIES} are needed for a trustworthy reading.")
    if rate is None:
        return out("yellow", "The golden set has no queries for the prompt path, so recall is not measured.")
    return out("green", f"The right memory is found {_pct(rate)} of the time"
                        + (f" (was {_pct(prev_rate)})." if isinstance(prev_rate, (int, float)) else "."))


# ---- khipu recall eval --install ----------------------------------------------

def _write_golden_atomic(path: Path, text: str) -> None:
    """Replace the golden file, keeping the previous one beside it. The backup
    and the rewrite use the names forget's golden sweep already removes, so a
    forgotten episode's quote cannot outlive it there."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        backup = path.parent / BACKUP_NAME
        shutil.copyfile(path, backup)
        os.chmod(backup, 0o600)
    fd, tmp = tempfile.mkstemp(prefix=GOLDEN_TEMP_PREFIX, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def install_candidates(candidates: Path, golden: Path | None = None) -> dict[str, Any]:
    """Merge golden candidate entries into the golden file, de-duplicated by
    query (an existing entry wins), refusing any whose shape or types the
    scorer cannot take (counted by reason) and any whose expected ids are not
    in the replica. Returns counts only; no query or id is ever returned."""
    from khipu import hub_snapshot

    path = golden or recall_eval.default_golden_path()
    try:
        incoming, malformed = _load_entries(candidates)
        existing_text = path.read_text(encoding="utf-8") if path.is_file() else ""
        existing, _ = _load_entries(path) if existing_text else ([], {})
        con = hub_snapshot.open_snapshot()
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"could not read the files or the replica ({type(exc).__name__})"}
    try:
        gone = _unavailable_ids({str(x) for e in incoming for x in (e.get("expect") or [])}, con)
    finally:
        con.close()
    seen = {str(e["query"]).strip() for e in existing}
    lines: list[str] = []
    duplicates = missing = 0
    for entry in incoming:
        query = str(entry["query"]).strip()
        if query in seen:
            duplicates += 1
            continue
        if any(str(x) in gone for x in (entry.get("expect") or [])):
            missing += 1
            continue
        seen.add(query)
        lines.append(json.dumps(entry, ensure_ascii=False))
    if lines:
        base = existing_text if not existing_text or existing_text.endswith("\n") else existing_text + "\n"
        _write_golden_atomic(path, base + "\n".join(lines) + "\n")
    return {"ok": True, "added": len(lines), "duplicates": duplicates,
            "rejected_missing_id": missing, "rejected_malformed": sum(malformed.values()),
            "malformed_reasons": malformed, "total": len(existing) + len(lines)}
