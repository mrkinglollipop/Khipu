"""Recall accuracy as a nightly measurement: the ``recall_eval`` nightly step,
the doctor row that reads it, and ``khipu recall eval --install``.

The step scores the maintainer-local golden set (``recall_eval``) on every
path against this Mac's replica only: the hub is never contacted, so the run
adds no hub load and cannot be slowed by an outage. Entries whose expected
ids are now forgotten, erased or deleted are skipped, never counted as
misses (a forgotten episode that no longer answers is the forget working).

``<data dir>/state/recall-eval-history.json`` keeps the last 30 run records:
figures, counts and the golden file's size and mtime. It never holds a query
or an id, so it is not owner text and forget has nothing to scan in it.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from khipu import recall_eval

HISTORY_KEEP = 30
BUDGET_S = 300
MIN_ENTRIES = 20
STALE_AFTER_H = 48
ALLOWED_DROP = 0.05
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


@contextlib.contextmanager
def _replica_only():
    """Every hub connect refuses for the run, so retrieval answers from the
    replica. The refusal is raised before ``try_hub_connect`` could note an
    outage, so the live hooks' reachability memo is never touched."""
    from khipu import hub_snapshot

    real = hub_snapshot.try_hub_connect

    def refuse(*_a: Any, **_k: Any):
        raise ConnectionError("recall eval reads the local replica only")

    hub_snapshot.try_hub_connect = refuse
    try:
        yield
    finally:
        hub_snapshot.try_hub_connect = real


def _unavailable_ids(ids: set[str], con) -> set[str]:
    """Ids that no longer answer: forgotten, erased or deleted episodes (the
    replica's episodes view already hides all three) and deleted topics."""
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
        else:
            try:
                row = con.execute("SELECT deleted_at FROM topics WHERE slug = ?", (ident,)).fetchone()
            except Exception:  # noqa: BLE001 - an old replica without topics: nothing to judge
                row = None
            if row is not None and row[0] is not None:
                gone.add(ident)
    return gone


def _golden_stat(path: Path) -> dict[str, Any]:
    try:
        st = path.stat()
    except OSError:
        return {"bytes": None, "mtime": None}
    return {"bytes": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()}


def _previous_figures(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    for rec in reversed(history):
        if isinstance(rec.get("paths"), dict) and rec["paths"]:
            return {"ts": rec.get("ts"), "paths": {
                p: {"hit_rate": m.get("hit_rate"), "mrr": m.get("mrr")}
                for p, m in rec["paths"].items() if isinstance(m, dict)}}
    return None


def run_nightly_eval(*, golden: Path | None = None, budget_s: float = BUDGET_S) -> dict[str, Any]:
    """The nightly step. ``ok`` means the measurement ran; a regression is the
    doctor row's to flag. Returns the compact record it stored."""
    from khipu import hub_snapshot

    now = datetime.now(timezone.utc)
    path = golden or recall_eval.default_golden_path()
    history = read_history()
    record: dict[str, Any] = {
        "ts": now.isoformat(), "ok": True, "entries": 0, "scored": 0,
        "skipped_forgotten": 0, "truncated": False, "paths": {},
        "previous": _previous_figures(history), "golden": _golden_stat(path),
    }
    try:
        entries = recall_eval.load_golden(path) if path.is_file() else []
    except (OSError, ValueError) as exc:
        record.update(ok=False, error=f"golden file unreadable ({type(exc).__name__})")
        entries = []
    record["entries"] = len(entries)
    if entries:
        try:
            con = hub_snapshot.open_snapshot()
        except Exception as exc:  # noqa: BLE001 - no replica: nothing to score against
            record.update(ok=False, error=f"replica unavailable ({type(exc).__name__})")
            entries = []
        else:
            try:
                named = {str(x) for e in entries for x in (e.get("expect") or [])}
                gone = _unavailable_ids(named, con)
            finally:
                con.close()
    if entries:
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
        with _replica_only():
            for entry in scoring:
                if time.monotonic() > deadline:
                    record["truncated"] = True
                    break
                for p in recall_eval.ALL_PATHS:
                    if recall_eval._entry_applies(entry, p):
                        rows[p].append(recall_eval.eval_one_path(entry, p))
                record["scored"] += 1
        for p, got in rows.items():
            summary = recall_eval.summarize_rows(got)
            record["paths"][p] = {k: summary[k] for k in _KEPT_METRICS}
    _write_history([*history, record])
    return record


# ---- doctor row --------------------------------------------------------------

def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def recall_eval_health(*, now: datetime | None = None) -> dict[str, Any]:
    """The doctor row. ``level`` is green / yellow / red; yellow never gates
    (``ok`` stays true), red does."""
    history = read_history()
    if not history:
        from khipu.jobs import _is_index_sync_host

        if not _is_index_sync_host():
            return {"ok": True, "applicable": False,
                    "note": "not the nightly host — evaluated on the sync host"}
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

    if violations:
        return out("red", f"{violations} memories you forgot or replaced came back in search results. "
                          "Run `khipu recall eval --path all` to see which.")
    if rate is not None and isinstance(prev_rate, (int, float)) and rate < prev_rate - ALLOWED_DROP - 1e-9:
        return out("red", f"Recall got worse: the right memory is found {_pct(rate)} of the time, "
                          f"down from {_pct(prev_rate)} the run before.")
    if rate is not None and rate < HIT_FLOOR:
        return out("red", f"Recall is below the {_pct(HIT_FLOOR)} floor: the right memory is found "
                          f"{_pct(rate)} of the time.")
    if not last.get("ok", True):
        return out("yellow", f"Tonight's recall check could not run: {last.get('error') or 'unknown reason'}.")
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
    query (an existing entry wins), refusing any whose expected ids are not
    in the replica. Returns counts only; no query is ever returned."""
    from khipu import hub_snapshot

    path = golden or recall_eval.default_golden_path()
    try:
        incoming = recall_eval.load_golden(candidates)
        existing_text = path.read_text(encoding="utf-8") if path.is_file() else ""
        existing = recall_eval.load_golden(path) if existing_text else []
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
            "rejected_missing_id": missing, "total": len(existing) + len(lines)}
