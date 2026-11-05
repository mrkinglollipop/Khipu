"""Detached execution of confirmed owner actions that scan or change batches.

Only the request id is queued. The worker re-reads the held request and its
confirmation, so Forget can still remove its words before execution starts.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from khipu.owner_act import SPECS

LONG_ACTIONS = frozenset(action for action in SPECS
                         if action.startswith(("outbox.", "ledger.", "owed.", "forget."))
                         and action != "forget.not_same") | {"accounts.move"}


def is_long(action: str, values: dict) -> bool:
    if action in LONG_ACTIONS:
        return True
    if action == "direction.undo":
        from khipu import owner_direction
        original = owner_direction._find(values.get("id"))
        return bool(original and original.get("action") in LONG_ACTIONS)
    return False


def start_authorized(action: str, values: dict, stamp: dict) -> tuple[bool, dict]:
    """Keep the authorization a direct owner turn already passed, without another ask."""
    from khipu import owner_direction

    try:
        request = owner_direction.hold(action, values, authorized=stamp)
    except owner_direction.DirectionRefused as refused:
        return False, refused.refusal()
    return start(request)


def path(request: str) -> Path:
    from khipu.paths import ensure_data_dir

    return ensure_data_dir() / "owner-jobs" / (request + ".json")


def _save(request: str, value: dict) -> None:
    from khipu import owner_direction

    destination = path(request)
    destination.parent.mkdir(parents=True, exist_ok=True)
    owner_direction._write_private(destination, json.dumps(value, ensure_ascii=False))


def _spawn(request: str) -> int:
    return subprocess.Popen(
        [sys.executable, "-m", "khipu.owner_background", request],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True).pid


def _identity(pid) -> str | None:
    """The start time ps reports for a process, or None when it is gone. Pid
    plus start time tells our worker from an unrelated process that reuses the
    pid after a restart."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        out = subprocess.run(["ps", "-o", "stat=,lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"  # can't tell: treat the worker as alive rather than misreport it
    stat, _, started = out.stdout.strip().partition(" ")
    return None if not stat or stat.startswith("Z") else started.strip()  # a zombie is dead, whoever has yet to reap it


def _note_worker(request: str, pid: int) -> None:
    """Keep the worker's identity beside the job, unless it already finished."""
    try:
        job = json.loads(path(request).read_text())
        if job.get("state") == "started" and not job.get("pid"):
            _save(request, dict(job, pid=pid, worker=_identity(pid)))
    except (OSError, ValueError):
        pass


def start(request: dict) -> tuple[bool, dict]:
    from khipu import owner_direction

    ident = request["id"]
    try:
        _save(ident, {"state": "started", "started_ts": time.time()})
        _note_worker(ident, _spawn(ident))
    except OSError as exc:
        result = {"ok": False, "code": "start_failed", "error": f"The job could not start: {exc}. Nothing was applied."}
        owner_direction.settle(ident, False, result)
        try:
            _save(ident, {"state": "finished", "ok": False, "result": owner_direction.result_counts(result)})
        except OSError:
            pass
        return False, result
    return True, {"ok": True, "started": True, "applied": False, "request": ident,
                  "line": "Started; it will finish on its own. The result will be recorded in Owner actions."}


def run(ident: str) -> tuple[bool, dict]:
    from khipu import owner_act, owner_direction

    job = json.loads(path(ident).read_text())
    if job.get("state") == "finished":
        return job["ok"], job["result"]
    _note_worker(ident, os.getpid())
    request = next((r for r in owner_direction._read_requests(owner_direction.requests_path()) if r.get("id") == ident), None)
    if request is None or request.get("state") != "applying" or "args" not in request:
        ok, result = False, {"ok": False, "code": "request_gone", "error": "The held request is no longer available; nothing was applied."}
        owner_direction.settle(ident, False, result)
    else:
        try:
            ok, result = owner_act._apply_confirmed(request, request["execution_stamp"])
        except Exception as exc:  # a failed worker must leave a final outcome
            owner_direction._abandon()
            ok, result = False, {"ok": False, "code": "failed", "error": f"The job failed ({type(exc).__name__})."}
            owner_direction.settle(ident, False, result)
    _save(ident, {"state": "finished", "ok": ok, "result": owner_direction.result_counts(result)})
    return ok, result


NO_WORKER_GRACE_S = 900  # a job whose worker never recorded itself is given this long to start


def _worker_gone(job: dict, now: float) -> bool:
    """True only when the job's worker is known to be dead: its pid is gone or
    belongs to a different process. A live one, however slow, is left alone."""
    pid = job.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return now - float(job.get("started_ts") or 0) > NO_WORKER_GRACE_S
    seen = _identity(pid)
    return seen is None or (seen != "unknown" and job.get("worker") not in (None, seen))


def heal(now: float | None = None) -> list[str]:
    """Turn each started job whose worker died (the Mac restarted, the process
    was killed) into a plain ``interrupted`` outcome. The action may have
    partly applied, so it is never run again here: the owner decides."""
    from khipu import owner_direction

    now = time.time() if now is None else now
    ended: list[str] = []
    try:
        files = sorted(path("x").parent.glob("*.json"))
    except OSError:
        return ended
    for file in files:
        try:
            job = json.loads(file.read_text())
        except (OSError, ValueError):
            continue
        if job.get("state") != "started":
            continue
        ident = file.stem
        request = next((r for r in owner_direction._read_requests(owner_direction.requests_path()) if r.get("id") == ident), None)
        if request is not None and request.get("state") == "applying":
            if not _worker_gone(job, now):
                continue
            owner_direction.settle(ident, False, {"code": "interrupted"}, state_name="interrupted")
            ended.append(ident)
        # Else the request was settled some other way: nothing waits on this job.
        _save(ident, {"state": "finished", "ok": bool(request and request.get("state") == "applied"),
                      "result": {"code": "interrupted"} if ident in ended else owner_direction.result_counts(request and request.get("result"))})
    return ended


if __name__ == "__main__":
    ok, _ = run(sys.argv[1])
    raise SystemExit(0 if ok else 1)
