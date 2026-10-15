"""Exact session exclusion shared by subscription adapters and native capture."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
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
    # ~/.claude/local/claude: Claude Code's own local install (`claude migrate-installer`).
    "claude": ("~/.npm-global/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude",
               "~/.local/bin/claude", "~/.claude/local/claude"),
    "codex": ("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
              "/opt/homebrew/bin/codex", "/usr/local/bin/codex", "~/.npm-global/bin/codex"),
    # The plan usage probes (khipu.usage) look these two up the same way.
    "grok": ("~/.grok/bin/grok", "/opt/homebrew/bin/grok", "/usr/local/bin/grok", "~/.local/bin/grok"),
    "muse": ("/opt/homebrew/bin/muse", "/usr/local/bin/muse", "~/.local/bin/muse"),
}
# Where a Node version manager puts a CLI installed with ``npm i -g`` (or its
# own global add), none of them on launchd's PATH: tried last, in this order.
# A ``*`` is a Node version folder, newest version first.
_NODE_MANAGER_DIRS = ("~/.nvm/versions/node/*/bin", "~/.volta/bin", "~/Library/pnpm", "~/.bun/bin",
                      "~/.local/share/fnm/node-versions/*/installation/bin",
                      "~/Library/Application Support/fnm/node-versions/*/installation/bin",
                      "~/.fnm/node-versions/*/installation/bin", "~/.asdf/shims")


def _node_version(folder: str) -> tuple[int, ...]:
    """``v22.10.1`` as (22, 10, 1), compared as numbers (v9 is older than
    v22); a folder that is not a version sorts after every version."""
    m = re.match(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", folder)
    return tuple(int(g or 0) for g in m.groups()) if m else (-1,)


def _node_manager_candidates(name: str):
    for pattern in _NODE_MANAGER_DIRS:
        base, star, tail = pattern.partition("/*/")
        try:
            root = Path(base).expanduser()
        except RuntimeError:  # no home to expand
            continue
        if not star:
            yield root / name
            continue
        try:
            versions = [d for d in root.iterdir() if d.is_dir()]
        except OSError:
            continue
        for folder in sorted(versions, key=lambda d: (_node_version(d.name), d.name), reverse=True):
            yield folder / tail / name


def _executable(path: Path) -> bool:
    with contextlib.suppress(OSError):
        return path.is_file() and os.access(path, os.X_OK)
    return False


def _chosen(name: str) -> str | None:
    """The CLI the owner chose in the app (``cli_path.<name>``), when it is
    still an executable file. A broken config never hides the CLI."""
    try:
        from khipu.config import CLI_SETTINGS, cli_setting

        key = f"cli_path.{name}"
        return cli_setting(key) if key in CLI_SETTINGS else None
    except Exception:  # noqa: BLE001 — fall through to the usual lookup
        return None


def binary(name: str) -> str | None:
    """Absolute path of the user's CLI, or None. The launchd drain job runs with
    a bare PATH (2026-10-13: every capture failed with FileNotFoundError the
    minute the provider was switched), so PATH is only the first place looked;
    ``KHIPU_<NAME>_CLI`` names an explicit binary and is looked at before
    anything else, then the CLI the owner chose in the app
    (``cli_path.<name>``). After PATH come the usual install folders, then the folders
    a Node version manager installs into (``_NODE_MANAGER_DIRS``), where only
    an executable file counts. A Node CLI found there still needs node on its
    PATH: the plan usage probes add the CLI's own folder
    (``usage._probe_path``)."""
    import shutil

    override = (os.environ.get(f"KHIPU_{name.upper()}_CLI") or "").strip()
    if override:
        return override if Path(override).is_file() else None
    chosen = _chosen(name)
    if chosen:
        return chosen
    found = shutil.which(name)
    if found:
        return found
    for candidate in _CANDIDATES.get(name, ()):
        p = Path(candidate).expanduser()
        if p.is_file() or p.is_symlink():
            return str(p)
    for p in _node_manager_candidates(name):
        if _executable(p):
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
