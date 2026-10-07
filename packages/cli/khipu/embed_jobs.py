# --bypass-harness (sonnet lane) — authored directly by the dispatched on-sub
# agent (brief: do not delegate); no further agent to route to.
"""Embedding jobs with receipts: the progress file a long backfill keeps, and
the runner that gives a detached backfill a clean life cycle.

The desktop app starts a backfill as a detached process and then *reads a
file*; it never holds a pipe. So each job writes ``<data_dir>/jobs/<id>.json``
at least once per batch:

    {job, kind, profile, space, state, done, total, failed,
     started_at, updated_at, error, pid}

``state`` is ``running`` | ``done`` | ``failed`` | ``cancelled``. ``space`` is
``"memory"`` or ``"library:NAME"``. ``done``/``total``/``failed`` count chunks.
A ``done`` job with ``failed`` > 0 or ``done`` < ``total`` is partial and the
screen must say so. SIGTERM (or SIGINT) asks the job to stop: it finishes the
batch in flight, writes ``cancelled`` and exits 0.

``khipu embed jobs`` lists the files; ``--clear`` removes the finished ones. A
file that says ``running`` whose process is gone (a crash, a ``kill -9``, a
reboot) is reported ``failed`` with the reason, so a screen never spins on a
corpse.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

STATES_FINISHED = ("done", "failed", "cancelled")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def jobs_dir() -> Path:
    from khipu.paths import ensure_data_dir

    d = ensure_data_dir() / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_job_id(job_id: str) -> str:
    if not JOB_ID_RE.match(job_id or ""):
        raise ValueError(f"job id {job_id!r} must match [A-Za-z0-9._-]{{1,80}}")
    return job_id


def new_job_id(space: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", space.lower()).strip("-") or "job"
    return f"{slug}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{secrets.token_hex(2)}"


class Job:
    """One job's progress file. ``update`` is called by the backfill after each
    batch; ``cancelled`` is read by it before the next one. Writes are atomic
    (temp file + rename) so a reader never sees half a file, and a write that
    fails never fails the embed."""

    def __init__(self, job_id: str, *, kind: str, profile: str, space: str) -> None:
        self.id = validate_job_id(job_id)
        self.kind = kind
        self.profile = profile
        self.space = space
        self.started_at = _now()
        self.done = 0
        self.total = 0
        self.failed = 0
        self.state = "running"
        self.error: str | None = None
        self._cancel = threading.Event()
        self.path = jobs_dir() / f"{self.id}.json"

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def request_cancel(self) -> None:
        self._cancel.set()

    def snapshot(self) -> dict[str, Any]:
        return {
            "job": self.id, "kind": self.kind, "profile": self.profile, "space": self.space,
            "state": self.state, "done": self.done, "total": self.total, "failed": self.failed,
            "started_at": self.started_at, "updated_at": _now(), "error": self.error,
            "pid": os.getpid(),
        }

    def write(self) -> None:
        try:
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.snapshot()), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            print(f"[khipu-jobs] could not write {self.path}: {exc}", file=sys.stderr, flush=True)

    def update(self, done: int, total: int, failed: int) -> None:
        self.done, self.total, self.failed = int(done), int(total), int(failed)
        self.write()

    def finish(self, state: str, error: str | None = None) -> None:
        self.state = state
        self.error = error
        self.write()


def run_job(
    *, kind: str, profile: str, space: str, work: Callable[[Job], dict[str, Any]],
    job_id: str | None = None, stream=None,
) -> int:
    """Run ``work(job)`` as a job and print ONE final JSON line to ``stream``
    (stdout by default). Exit code: 0 for done and cancelled, 1 for failed.

    ``work`` returns the backfill's stats: ``embedded`` and ``failed`` /
    ``failed_chunks`` counts, ``cancelled``, ``budget_exhausted``. It decides
    the end state: cancelled > budget exhausted (failed, resumable) > nothing
    embedded and everything failed (failed) > done (partial when ``failed``)."""
    out = stream if stream is not None else sys.stdout
    job = Job(job_id or new_job_id(space), kind=kind, profile=profile, space=space)
    previous = {}

    def _on_term(_signum, _frame) -> None:
        job.request_cancel()

    # Only the main thread may install handlers; a test driving this from a
    # worker thread simply runs without them.
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, _on_term)
    job.write()
    try:
        stats = work(job)
    except Exception as exc:  # noqa: BLE001 - the receipt carries the failure
        job.finish("failed", f"{type(exc).__name__}: {exc}")
        print(json.dumps({"ok": False, "job": job.id, "state": "failed", "error": job.error}),
              file=out, flush=True)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    failed = int(stats.get("failed_chunks", stats.get("failed", 0)) or 0)
    embedded = int(stats.get("embedded", 0) or 0)
    job.done, job.failed = embedded, failed
    error = None
    if stats.get("cancelled") or job.cancelled:
        state = "cancelled"
    elif stats.get("budget_exhausted"):
        state, error = "failed", "embed budget exhausted; the run is resumable (raise KHIPU_EMBED_DAILY_CALLS or wait for tomorrow)"
    elif failed and not embedded:
        state = "failed"
        error = f"every batch failed ({failed} chunks); see the job log"
        if stats.get("embed_provider") == "missing key":
            error = "no API key for this provider"
    else:
        state = "done"
    job.finish(state, error)
    receipt = {"ok": state != "failed", "job": job.id, "state": state, "profile": profile,
               "space": space, "done": job.done, "total": job.total, "failed": job.failed}
    if error:
        receipt["error"] = error
    print(json.dumps(receipt), file=out, flush=True)
    return 1 if state == "failed" else 0


# ---- reading --------------------------------------------------------------------

def _pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True
    return True


def list_jobs() -> list[dict[str, Any]]:
    """Every job file, newest first. A ``running`` job whose process is gone is
    rewritten ``failed`` so the next reader sees the truth."""
    rows: list[dict[str, Any]] = []
    for path in sorted(jobs_dir().glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        if row.get("state") == "running" and not _pid_alive(row.get("pid")):
            row["state"] = "failed"
            row["error"] = "the job's process exited without finishing (crashed or killed)"
            row["updated_at"] = _now()
            try:
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(row), encoding="utf-8")
                tmp.replace(path)
            except OSError:
                pass
        rows.append(row)
    rows.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
    return rows


def clear_jobs() -> dict[str, Any]:
    """Remove the finished jobs' files; a running job's file stays."""
    removed = 0
    for row in list_jobs():
        if row.get("state") in STATES_FINISHED and JOB_ID_RE.match(str(row.get("job") or "")):
            try:
                (jobs_dir() / f"{row['job']}.json").unlink()
                removed += 1
            except OSError:
                pass
    return {"ok": True, "removed": removed}
