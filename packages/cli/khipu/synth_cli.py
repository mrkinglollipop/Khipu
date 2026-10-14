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


_CANDIDATES = {
    "claude": ("~/.npm-global/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude",
               "~/.local/bin/claude"),
    "codex": ("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
              "/opt/homebrew/bin/codex", "/usr/local/bin/codex", "~/.npm-global/bin/codex"),
    # The plan usage probes (khipu.usage) look these two up the same way.
    "grok": ("~/.grok/bin/grok", "/opt/homebrew/bin/grok", "/usr/local/bin/grok", "~/.local/bin/grok"),
    "muse": ("/opt/homebrew/bin/muse", "/usr/local/bin/muse", "~/.local/bin/muse"),
}


def binary(name: str) -> str | None:
    """Absolute path of the user's CLI, or None. The launchd drain job runs with
    a bare PATH (2026-10-13: every capture failed with FileNotFoundError the
    minute the provider was switched), so PATH is only the first place looked;
    ``KHIPU_<NAME>_CLI`` names an explicit binary."""
    import shutil

    override = (os.environ.get(f"KHIPU_{name.upper()}_CLI") or "").strip()
    if override:
        return override if Path(override).is_file() else None
    found = shutil.which(name)
    if found:
        return found
    for candidate in _CANDIDATES.get(name, ()):
        p = Path(candidate).expanduser()
        if p.is_file() or p.is_symlink():
            return str(p)
    return None


# The summariser runs on the owner's default Claude login (``~/.claude``), never
# on the login of whichever session queued the capture: a Stop hook fired from
# a session on a second account carries that account's CLAUDE_CONFIG_DIR, and on
# 2026-10-14 the drains it spawned billed Haiku to the second account and hit
# its five-hour limit while the main account was idle.
_DROPPED = {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CONFIG_DIR"}


def environment() -> dict[str, str]:
    return {k: v for k, v in os.environ.items()
            if k not in _DROPPED and not k.startswith("KHIPU_")}


def error_text(message: str, prompt: str, env: dict[str, str]) -> str:
    """CLI error results may echo stdin or inherited secrets; scrub before clipping."""
    if prompt:
        message = message.replace(prompt, "[prompt]")
    for value in sorted(set(env.values()), key=len, reverse=True):
        if len(value) >= 4:
            message = message.replace(value, "[environment]")
    return message[:300]
