"""Exact session exclusion shared by subscription adapters and native capture."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from khipu.paths import data_dir

_observed: ContextVar[list[str] | None] = ContextVar("synth_cli_sessions", default=None)


def root() -> Path:
    return data_dir() / "state" / "synth-cli"


def session_ids() -> set[str]:
    """Malformed or unavailable markers never suppress an ordinary capture."""
    try:
        rows = json.loads((root() / "sessions.json").read_text(encoding="utf-8"))
        if not isinstance(rows, list) or not all(isinstance(s, str) for s in rows):
            return set()
        return set(rows)
    except Exception:  # noqa: BLE001 — hooks fail open
        return set()


def register(session_id: str) -> str:
    """Persist before launch; a failed marker write must prevent the call."""
    directory = root()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "sessions.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / "sessions.json"
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(rows, list) or not all(isinstance(s, str) for s in rows):
                rows = []
        except (OSError, ValueError):
            rows = []
        rows = [s for s in rows if s != session_id] + [session_id]
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as fh:
            tmp = Path(fh.name)
            try:
                json.dump(rows[-2000:], fh)
                fh.flush()
                os.fsync(fh.fileno())
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
    observed = _observed.get()
    if observed is not None:
        observed.append(session_id)
    return session_id


def new_session() -> str:
    return register(str(uuid.uuid4()))


@contextmanager
def observe_sessions():
    sessions: list[str] = []
    token = _observed.set(sessions)
    try:
        yield sessions
    finally:
        _observed.reset(token)


def environment() -> dict[str, str]:
    return {k: v for k, v in os.environ.items()
            if k not in {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"} and not k.startswith("KHIPU_")}


def error_text(message: str, prompt: str, env: dict[str, str]) -> str:
    """CLI error results may echo stdin or inherited secrets; scrub before clipping."""
    if prompt:
        message = message.replace(prompt, "[prompt]")
    for value in sorted(set(env.values()), key=len, reverse=True):
        if len(value) >= 4:
            message = message.replace(value, "[environment]")
    return message[:300]
