"""Detached execution of confirmed owner actions that scan or change batches.

Only the request id is queued. The worker re-reads the held request and its
confirmation, so Forget can still remove its words before execution starts.
"""
from __future__ import annotations

import json
import subprocess
import sys
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


def start(request: dict) -> tuple[bool, dict]:
    from khipu import owner_direction

    ident = request["id"]
    try:
        _save(ident, {"state": "started"})
        _spawn(ident)
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


if __name__ == "__main__":
    ok, _ = run(sys.argv[1])
    raise SystemExit(0 if ok else 1)
