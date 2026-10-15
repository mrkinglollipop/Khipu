"""Plan usage: how much of each subscription CLI's plan is left.

Every CLI already reports its own plan headroom through a protocol it speaks,
using the login it already holds, so Khipu runs the CLI as the user, asks, and
reads the answer. Khipu never reads, holds or stores a credential, and no model
call is made (no tokens are spent). Scope: docs/plans/2026-10-14-plan-usage.md.

One adapter per CLI, each a short child process:

  claude  ``claude -p`` in stream-json mode, SDK control requests ``initialize``
          then ``get_usage`` (``skip_behaviors``)
  codex   ``codex app-server`` (stdio JSON-RPC): ``initialize``, ``initialized``,
          ``account/rateLimits/read``; falls back to the newest ``rate_limits``
          in the rollout files when the live call fails (bounded: five newest
          files, last 256 KiB of each, inside the probe's own timeout)
  grok    ``grok agent stdio`` (ACP) with ``GROK_HOME``: ``initialize``, then
          ``_x.ai/billing``
  muse    ``muse serve --no-session-log`` (MSP): ``initialize``, ``initialized``,
          ``usage/read`` (last observed figures, never refreshed by a turn)

Each child gets an allowlisted environment (``probe_environment``: no value is
read for any other name), a fresh empty temp directory as its cwd, its stdin
closed as soon as the CLI allows, a deadline, and a hard kill of its whole
process group. A failure is reported as a fixed short code (``timeout``,
``not_signed_in``, ``bad_reply``, ``exit_<n>``, ...): no text a CLI wrote ever
reaches a row, the cache, the CLI output or the MCP result. Results are cached
in ``state/usage.json`` per (harness, home); a window a CLI does not report is
absent, never zero.
"""
from __future__ import annotations

import contextlib
import fcntl
import heapq
import json
import math
import os
import re
import select
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HARNESSES = ("claude", "codex", "grok", "muse")
LABELS = {"claude": "Claude", "codex": "Codex", "grok": "Grok", "muse": "Muse"}
TIMEOUT_S = 5.0
DEFAULT_MAX_AGE_S = 120
CACHE_NAME = "usage.json"
CACHE_VERSION = 2
REFRESH_LOCK_NAME = "usage.refresh.lock"
WRITE_LOCK_NAME = "usage.write.lock"
# probe_all's overall deadline is the per-probe timeout plus this grace.
OVERALL_GRACE_S = 2.0
ROLLOUT_FILES = 5
ROLLOUT_TAIL_BYTES = 256 * 1024

# Where each CLI keeps the login a probe should read; set only for the
# account's own harness and only when the account names a home.
_HOME_VARS = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "grok": "GROK_HOME"}
# The only names whose values a probe reads from Khipu's environment (plus
# LC_*). Everything else, an unlisted SERVICE_TOKEN included, is never read.
_ENV_ALLOW = ("HOME", "USER", "LOGNAME", "PATH", "SHELL", "TMPDIR", "LANG", "TERM")
# Grok Build 1.0.52 has no flag or variable that starts it with no MCP servers
# (checked: `grok --help`, `grok agent --help`, `grok agent stdio --help`, the
# binary's GROK_* names). These two stop it importing Cursor's and Claude's
# MCP configuration (on Matt's Mac 20 of 28 servers, firecrawl among them);
# the servers in its own config.toml are still announced. No MCP child was
# seen starting before the billing answer; the probe reads no notification and
# kills the whole process group at the answer or the deadline.
_GROK_ENV = {"GROK_CURSOR_MCPS_ENABLED": "0", "GROK_CLAUDE_MCPS_ENABLED": "0"}
# Added after the caller's PATH. Khipu.app starts under launchd's bare PATH
# (/usr/bin:/bin:/usr/sbin:/sbin, promote.LAUNCHD_PATH), and an npm-installed
# CLI is a ``#!/usr/bin/env node`` shim that exits 127 there before it answers
# unless node's folder is on PATH. So the child's PATH gains the folder
# synth_cli.binary found the CLI in (under nvm and fnm, node sits in that same
# folder), the folder of the file it links to, then these.
_TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "~/.npm-global/bin", "~/.local/bin")
_SYSTEM_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")

_CLAUDE_ARGS = ("-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--setting-sources", "project", "--no-session-persistence",
                # No MCP servers: the probe never uses a tool, and the user's
                # servers (Khipu's own among them) cost startup time.
                "--strict-mcp-config")
_CLIENT_INFO = {"name": "khipu_usage", "version": "1"}

# Fixed error codes. A CLI's own words are matched against _SIGNIN to choose a
# code and are then dropped; they are never returned, cached or logged.
E_NOT_INSTALLED = "not_installed"
E_START = "start_failed"
E_TIMEOUT = "timeout"
E_SIGNIN = "not_signed_in"
E_BAD = "bad_reply"
E_NO_ANSWER = "no_answer"
E_NO_LIMITS = "no_plan_limits"
E_CLI = "cli_error"
E_INVALID = "invalid_account"
E_INTERNAL = "internal_error"
E_PENDING = "pending"
# The CLIs by the names the app's form gives them.
CLI_NAMES = {"claude": "Claude Code", "codex": "Codex", "grok": "Grok Build", "muse": "Muse"}
# Plain words only: the app shows notes as written to an owner who is not
# technical, so a note names a login folder, never a home, a PATH, a token or
# a probe. The live read's own code goes in the row's ``live_error`` field,
# never into the note.
ROLLOUT_NOTE = "Codex did not answer just now; these figures are from its last turn"


def _note(code: str | None, harness: str | None = None, home: str | None = None) -> str | None:
    """Khipu's own sentence for a failure ``code`` on one account, or None."""
    name = CLI_NAMES.get(harness or "", "This CLI")
    folder = " with this login folder" if home else ""
    if code == E_NOT_INSTALLED:
        return f"{name} is not installed on this Mac"
    if code == E_SIGNIN:
        return f"{name} is not signed in{folder}. Open {name}{folder} and sign in again"
    if code == E_NO_LIMITS:
        return f"This sign-in reports no plan limits. Open {name}{folder} and sign in again (/login)"
    if code == E_PENDING:
        return "Reading for the first time; ask again in a few seconds"
    return None


def _rollout_note(code: str, home: str | None) -> str:
    """The note on Codex's last-turn figures after its live read failed with
    ``code``: a signed-out Codex says so, since reading again will not help."""
    if code == E_SIGNIN:
        folder = " with this login folder" if home else ""
        return (f"Codex is not signed in{folder}, so these figures are from its last turn. "
                f"Open Codex{folder} and sign in again")
    return ROLLOUT_NOTE
_SIGNIN = re.compile(r"not (signed|logged) in|(log|sign) ?in (is )?required|please (log|sign) ?in|"
                     r"unauthori[sz]ed|unauthenticated|authentication (failed|required)|\b401\b", re.I)


class ProbeError(Exception):
    """The CLI did not answer, or answered with an error. ``code`` is one of
    the fixed codes above; the CLI's own text is never carried."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _classify(text, fallback: str) -> str:
    return E_SIGNIN if isinstance(text, str) and _SIGNIN.search(text) else fallback


# ---- child process ---------------------------------------------------------


def _command(harness: str) -> str | None:
    from khipu import synth_cli

    return synth_cli.binary(harness)


def _home_path(home: str) -> Path | None:
    """``home`` with ``~`` expanded, or None when it names a user this Mac
    does not have (``Path.expanduser`` raises RuntimeError for ``~nouser``)."""
    try:
        return Path(home).expanduser()
    except RuntimeError:
        return None


def _probe_path(inherited: str | None, binary: str | None) -> str:
    """The child's PATH: the caller's own entries first, then the CLI's folder,
    the folder of the file it links to, the usual tool folders and the system
    ones, each once (see ``_TOOL_DIRS``)."""
    dirs = [d for d in (inherited or "").split(os.pathsep) if d]
    if binary:
        with contextlib.suppress(OSError, ValueError):
            dirs.append(os.path.dirname(os.path.abspath(binary)))
            dirs.append(os.path.dirname(os.path.realpath(binary)))
    for folder in _TOOL_DIRS:
        with contextlib.suppress(RuntimeError):
            dirs.append(str(Path(folder).expanduser()))
    dirs.extend(_SYSTEM_DIRS)
    return os.pathsep.join(dict.fromkeys(dirs))


def probe_environment(harness: str, home: str | None = None, binary: str | None = None) -> dict[str, str]:
    """The child's whole environment, built from an allowlist: a value is read
    only for the names in ``_ENV_ALLOW`` and ``LC_*`` (other names are seen,
    never read), then the account's own home and, for Grok, the compat knobs.
    PATH gains the folders a ``binary`` that is a node shim needs to find node
    when Khipu runs under launchd's bare PATH (``_probe_path``)."""
    env: dict[str, str] = {}
    for name in _ENV_ALLOW:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    env["PATH"] = _probe_path(env.get("PATH"), binary)
    for name in list(os.environ):  # names only
        if name.startswith("LC_"):
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
    if home and harness in _HOME_VARS:
        path = _home_path(home)
        env[_HOME_VARS[harness]] = str(path if path is not None else home)
    if harness == "grok":
        env.update(_GROK_ENV)
    return env


def _stderr_tail(errfile) -> str:
    """For classification only; the caller never returns it."""
    try:
        errfile.seek(0, os.SEEK_END)
        size = errfile.tell()
        errfile.seek(max(0, size - 4000))
        return errfile.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return ""


class _Run:
    """The child processes of one probe_all, so its overall deadline can kill
    every process group still alive (grandchildren in the group included)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: set[subprocess.Popen] = set()
        self._closed = False

    def add(self, proc: subprocess.Popen) -> None:
        with self._lock:
            if not self._closed:
                self._procs.add(proc)
                return
        _killpg(proc)  # started after the deadline

    def discard(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)

    def kill_all(self) -> None:
        with self._lock:
            self._closed = True
            procs = list(self._procs)
        for proc in procs:
            _killpg(proc)


_local = threading.local()


def _killpg(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, signal.SIGKILL)


def _kill(proc: subprocess.Popen) -> None:
    _killpg(proc)
    with contextlib.suppress(Exception):
        proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=2)
    for stream in (proc.stdin, proc.stdout):
        with contextlib.suppress(Exception):
            if stream:
                stream.close()


def _exit_code(code) -> str:
    if code is None or code == 0:
        return E_NO_ANSWER
    return f"exit_{code}" if code > 0 else f"signal_{-code}"


def _converse(argv: list[str], messages: list[dict], is_answer, *, env: dict[str, str],
              deadline: float, close_stdin: bool) -> list[dict]:
    """Write ``messages``, read JSON lines until ``is_answer(frame)`` or the
    ``deadline`` (monotonic), then kill the whole process group.

    The child runs in a fresh empty temp directory. ``close_stdin``: close
    stdin right after the requests. Codex and Grok exit on end of input before
    answering, so for them stdin stays open until the answer arrives (the kill
    closes it). Returns the response frames read; notifications are dropped
    unread (Grok's carry the user's MCP configuration)."""
    with tempfile.TemporaryDirectory(prefix="khipu-usage-") as cwd, tempfile.TemporaryFile() as errfile:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errfile,
                                    cwd=cwd, env=env, start_new_session=True)
        except OSError:
            raise ProbeError(E_START) from None
        run = getattr(_local, "run", None)
        if run is not None:
            run.add(proc)
        try:
            try:
                proc.stdin.write("".join(json.dumps(m) + "\n" for m in messages).encode())
                proc.stdin.flush()
                if close_stdin:
                    proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            fd = proc.stdout.fileno()
            buf = b""
            frames: list[dict] = []
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ProbeError(E_TIMEOUT)
                ready, _, _ = select.select([fd], [], [], min(left, 0.25))
                if not ready:
                    if proc.poll() is not None:
                        # The CLI exited; a grandchild may still hold stdout.
                        raise ProbeError(_classify(_stderr_tail(errfile), _exit_code(proc.returncode)))
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    with contextlib.suppress(Exception):
                        proc.wait(timeout=min(0.5, max(0.0, deadline - time.monotonic())))
                    raise ProbeError(_classify(_stderr_tail(errfile), _exit_code(proc.returncode)))
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        frame = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(frame, dict):
                        continue
                    if "id" in frame or frame.get("type") == "control_response":
                        frames.append(frame)
                        if is_answer(frame):
                            return frames
        finally:
            _kill(proc)
            if run is not None:
                run.discard(proc)


# ---- small helpers -----------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value) -> str | None:
    """Epoch seconds, epoch milliseconds or an ISO string -> ``...Z`` (seconds)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 100_000_000_000 else value
            dt = datetime.fromtimestamp(seconds, timezone.utc)
        elif isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_time(text) -> datetime | None:
    if not text or not isinstance(text, str):
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _dict(parent: dict, key: str, *, required: bool = False) -> dict:
    """A nested object: absent (None) is ``{}`` unless required; any other
    type is a malformed reply."""
    value = parent.get(key)
    if value is None:
        if required:
            raise ProbeError(E_BAD)
        return {}
    if not isinstance(value, dict):
        raise ProbeError(E_BAD)
    return value


def _list(parent: dict, key: str) -> list:
    value = parent.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ProbeError(E_BAD)
    return value


def _text(value, limit: int = 64) -> str | None:
    """A short printable string the CLI reports as data (plan, version)."""
    if isinstance(value, str) and value.strip() and len(value) <= limit and value.isprintable():
        return value.strip()
    return None


def _mins(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _window_name(mins) -> str:
    return {300: "five_hour", 1440: "daily", 10080: "weekly"}.get(mins, f"{mins}_min" if mins else "window")


def _window(name: str, used, resets_at, mins=None, **extra) -> dict | None:
    """Absent ``used`` = no window; a present non-number = malformed reply."""
    if used is None:
        return None
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used):
        raise ProbeError(E_BAD)
    w = {"name": name, "used_pct": used, "resets_at": _iso(resets_at)}
    if mins:
        w["window_mins"] = mins
    w.update({k: v for k, v in extra.items() if v is not None})
    return w


def _answer(frames: list[dict], rid) -> dict:
    for frame in frames:
        if frame.get("id") == rid:
            if frame.get("error") is not None:
                err = frame["error"]
                raise ProbeError(_classify(err.get("message") if isinstance(err, dict) else err, E_CLI))
            result = frame.get("result")
            if not isinstance(result, dict):
                raise ProbeError(E_BAD)
            return result
    raise ProbeError(E_NO_ANSWER)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 2) if math.isfinite(number) else None


def _bool(value):
    return value if isinstance(value, bool) else None


def _guard(fn):
    def run(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ProbeError as exc:
            return {"ok": False, "error": exc.code}
        except (AttributeError, TypeError, KeyError, IndexError, ValueError):
            return {"ok": False, "error": E_BAD}  # a reply shape no check above foresaw
    run.__name__ = fn.__name__
    run.__doc__ = fn.__doc__
    return run


def _not_installed(harness: str) -> dict:
    return {"ok": False, "error": E_NOT_INSTALLED}


# ---- Claude ------------------------------------------------------------------


def _claude_is_answer(frame: dict) -> bool:
    if frame.get("type") != "control_response":
        return False
    response = frame.get("response")
    # A malformed response ends the conversation; parse_claude reports it.
    return not isinstance(response, dict) or response.get("request_id") == "usage"


def parse_claude(frames: list[dict]) -> dict:
    responses: dict[str, dict] = {}
    for frame in frames:
        if frame.get("type") != "control_response":
            continue
        response = frame.get("response")
        if not isinstance(response, dict):
            raise ProbeError(E_BAD)
        rid = response.get("request_id")
        if isinstance(rid, str):
            responses[rid] = response
    init = responses.get("init") or {}
    init_body = init.get("response") if isinstance(init.get("response"), dict) else {}
    usage = responses.get("usage")
    if usage is None:
        raise ProbeError(E_NO_ANSWER)
    if usage.get("subtype") == "error":
        raise ProbeError(_classify(usage.get("error"), E_CLI))
    body = _dict(usage, "response", required=True)
    if body.get("rate_limits") is None:
        raise ProbeError(E_NO_LIMITS)
    limits = _dict(body, "rate_limits")
    five, week = _dict(limits, "five_hour"), _dict(limits, "seven_day")
    windows = [
        _window("five_hour", five.get("utilization"), five.get("resets_at"), 300),
        _window("weekly", week.get("utilization"), week.get("resets_at"), 10080),
    ]
    seen = set()
    for scoped in _list(limits, "model_scoped"):
        if not isinstance(scoped, dict):
            raise ProbeError(E_BAD)
        model = _text(scoped.get("display_name"))
        if model:
            seen.add(model.lower())
            windows.append(_window(f"weekly_{re.sub(r'[^a-z0-9]+', '_', model.lower())}",
                                   scoped.get("utilization"), scoped.get("resets_at"), 10080, model=model))
    for key, model in (("seven_day_opus", "Opus"), ("seven_day_sonnet", "Sonnet")):
        block = _dict(limits, key)
        if block and model.lower() not in seen:
            windows.append(_window(f"weekly_{model.lower()}", block.get("utilization"),
                                   block.get("resets_at"), 10080, model=model))
    extra = _dict(limits, "extra_usage")
    credits = {k: v for k, v in {
        "extra_usage_enabled": _bool(extra.get("is_enabled")),
        "extra_usage_used": _number(extra.get("used_credits")),
        "extra_usage_limit": _number(extra.get("monthly_limit")),
        "currency": _text(extra.get("currency"), 8),
    }.items() if v is not None} or None
    return {"ok": True, "plan": _text(body.get("subscription_type")), "windows": [w for w in windows if w],
            "credits": credits, "version": _text(init_body.get("claude_code_version")), "source": "live"}


@_guard
def probe_claude(config_dir: str | None = None, *, timeout: float = TIMEOUT_S) -> dict:
    """Claude Code's own ``/usage`` figures for the login in ``config_dir``
    (``CLAUDE_CONFIG_DIR``; None = the default ``~/.claude``). Never ``--bare``:
    bare mode skips the keychain, so there is no login to read."""
    binary = _command("claude")
    if not binary:
        return _not_installed("claude")
    messages = [
        {"type": "control_request", "request_id": "init", "request": {"subtype": "initialize"}},
        {"type": "control_request", "request_id": "usage",
         "request": {"subtype": "get_usage", "skip_behaviors": True}},
    ]
    frames = _converse([binary, *_CLAUDE_ARGS], messages, _claude_is_answer,
                       env=probe_environment("claude", config_dir, binary), deadline=time.monotonic() + timeout,
                       close_stdin=True)
    return parse_claude(frames)


# ---- Codex -------------------------------------------------------------------


def _codex_windows(snapshot: dict, *, camel: bool) -> list[dict]:
    windows = []
    for slot in ("primary", "secondary"):
        block = _dict(snapshot, slot)
        if not block:
            continue  # either may be null: the pro plan reports one weekly window
        if camel:
            used, mins, resets = block.get("usedPercent"), block.get("windowDurationMins"), block.get("resetsAt")
        else:
            used, mins, resets = block.get("used_percent"), block.get("window_minutes"), block.get("resets_at")
        mins = _mins(mins)
        w = _window(_window_name(mins), used, resets, mins, slot=slot)
        if w:
            windows.append(w)
    return windows


def _codex_credits(snapshot: dict, *, camel: bool) -> dict | None:
    credits = _dict(snapshot, "credits")
    out = {
        "balance": _number(credits.get("balance")),
        "has_credits": _bool(credits.get("hasCredits" if camel else "has_credits")),
        "unlimited": _bool(credits.get("unlimited")),
    }
    return {k: v for k, v in out.items() if v is not None} or None


def parse_codex(frames: list[dict]) -> dict:
    version = None
    for frame in frames:
        if frame.get("id") == 1 and isinstance(frame.get("result"), dict):
            agent = frame["result"].get("userAgent")
            m = re.search(r"/(\d[\w.\-]*)", agent) if isinstance(agent, str) else None
            version = _text(m.group(1)) if m else None
    result = _answer(frames, 2)
    snapshot = _dict(result, "rateLimits", required=True)
    return {"ok": True, "plan": _text(snapshot.get("planType")), "windows": _codex_windows(snapshot, camel=True),
            "credits": _codex_credits(snapshot, camel=True), "version": version, "source": "live"}


def _rollout_event(path: str) -> dict | None:
    """The last ``token_count`` event with ``rate_limits`` in the final
    ``ROLLOUT_TAIL_BYTES`` of one rollout file."""
    try:
        with open(path, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            start = max(0, size - ROLLOUT_TAIL_BYTES)
            fh.seek(start)
            data = fh.read(ROLLOUT_TAIL_BYTES)
    except OSError:
        return None
    lines = data.split(b"\n")
    if start:
        lines = lines[1:]  # the first line was cut by the seek
    for raw in reversed(lines):
        if b'"token_count"' not in raw or b'"rate_limits"' not in raw:
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        payload = event.get("payload") if isinstance(event, dict) else None
        if isinstance(payload, dict) and payload.get("type") == "token_count" \
                and isinstance(payload.get("rate_limits"), dict):
            return event
    return None


def codex_rollout(home: str | None = None, *, deadline: float | None = None) -> dict | None:
    """The newest ``rate_limits`` in a ``token_count`` event, from the
    ``ROLLOUT_FILES`` newest rollout files by mtime (last ``ROLLOUT_TAIL_BYTES``
    of each): current as of the last Codex turn, not live. Gives up (None) at
    ``deadline`` (monotonic; default one probe timeout from now)."""
    deadline = time.monotonic() + TIMEOUT_S if deadline is None else deadline
    root = Path(home or "~/.codex").expanduser() / "sessions"
    newest: list[tuple[float, str]] = []
    stack = [str(root)]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if time.monotonic() > deadline:
                        return None
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.name.startswith("rollout-") and entry.name.endswith(".jsonl") \
                                and entry.is_file(follow_symlinks=False):
                            item = (entry.stat(follow_symlinks=False).st_mtime, entry.path)
                            if len(newest) < ROLLOUT_FILES:
                                heapq.heappush(newest, item)
                            else:
                                heapq.heappushpop(newest, item)
                    except OSError:
                        continue
        except OSError:
            continue
    for _mtime, path in sorted(newest, reverse=True):
        if time.monotonic() > deadline:
            return None
        event = _rollout_event(path)
        if not event:
            continue
        limits = event["payload"]["rate_limits"]
        try:
            return {"ok": True, "plan": _text(limits.get("plan_type")),
                    "windows": _codex_windows(limits, camel=False),
                    "credits": _codex_credits(limits, camel=False),
                    "source": "rollout", "observed_at": _iso(event.get("timestamp"))}
        except ProbeError:
            continue
    return None


@_guard
def probe_codex(home: str | None = None, *, timeout: float = TIMEOUT_S) -> dict:
    """Codex's ``account/rateLimits/read``; on failure, the last turn's figures
    from the rollout files (``source: "rollout"``). Both fit in ``timeout``:
    the live call leaves a fifth of it (at most 1 s) for the fallback."""
    deadline = time.monotonic() + timeout
    binary = _command("codex")
    try:
        if not binary:
            raise ProbeError(E_NOT_INSTALLED)
        messages = [
            {"id": 1, "method": "initialize", "params": {"clientInfo": _CLIENT_INFO}},
            {"method": "initialized"},
            {"id": 2, "method": "account/rateLimits/read"},
        ]
        frames = _converse([binary, "app-server"], messages, lambda f: f.get("id") == 2,
                           env=probe_environment("codex", home, binary), deadline=deadline - min(1.0, timeout * 0.2),
                           close_stdin=False)
        return parse_codex(frames)
    except ProbeError as exc:
        fallback = codex_rollout(home, deadline=deadline)
        if fallback is None:
            raise
        fallback["note"] = _rollout_note(exc.code, home)
        fallback["live_error"] = exc.code
        return fallback


# ---- Grok --------------------------------------------------------------------


def parse_grok(frames: list[dict]) -> dict:
    version = None
    for frame in frames:
        if frame.get("id") == 1 and isinstance(frame.get("result"), dict):
            meta = frame["result"].get("_meta")
            version = _text(meta.get("agentVersion")) if isinstance(meta, dict) else None
    result = _answer(frames, 2)
    config = _dict(result, "config", required=True)
    period = _dict(config, "currentPeriod")
    kind = period.get("type")
    kind = re.sub(r"[^a-z0-9_]", "", kind.lower().removeprefix("usage_period_type_"))[:32] \
        if isinstance(kind, str) else ""
    window = _window(kind or "period", config.get("creditUsagePercent"),
                     period.get("end") or config.get("billingPeriodEnd"))
    balance = _number(_dict(config, "prepaidBalance").get("val"))
    credits = {"prepaid_balance": balance} if balance is not None else None
    return {"ok": True, "plan": _text(result.get("subscription_tier")), "windows": [window] if window else [],
            "credits": credits, "version": version, "source": "live"}


@_guard
def probe_grok(grok_home: str | None = None, *, timeout: float = TIMEOUT_S) -> dict:
    """Grok Build's ``_x.ai/billing`` (ACP, undocumented) for the login in
    ``grok_home`` (``GROK_HOME``; None = the default ``~/.grok``). See
    ``_GROK_ENV``: Grok cannot be started with no MCP servers at all."""
    binary = _command("grok")
    if not binary:
        return _not_installed("grok")
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": 1, "clientCapabilities": {}, "clientInfo": _CLIENT_INFO}},
        {"jsonrpc": "2.0", "id": 2, "method": "_x.ai/billing", "params": {}},
    ]
    frames = _converse([binary, "agent", "--no-leader", "stdio"], messages, lambda f: f.get("id") == 2,
                       env=probe_environment("grok", grok_home, binary), deadline=time.monotonic() + timeout,
                       close_stdin=False)
    return parse_grok(frames)


# ---- Muse --------------------------------------------------------------------


def parse_muse(frames: list[dict]) -> dict:
    version = None
    for frame in frames:
        if frame.get("id") == 1 and isinstance(frame.get("result"), dict):
            info = frame["result"].get("serverInfo")
            version = _text(info.get("version")) if isinstance(info, dict) else None
    usage = _answer(frames, 2).get("usage")
    if usage is None or usage == {}:
        # A fresh host answers {} until a Muse turn has run: truthful absence.
        return {"ok": True, "plan": None, "windows": [], "version": version, "source": "last_observed",
                "note": "no turn observed yet"}
    if not isinstance(usage, dict):
        raise ProbeError(E_BAD)
    window, weekly = _dict(usage, "window"), _dict(usage, "weekly")
    mins = _mins(window.get("windowDurationMins"))
    windows = [
        _window(_window_name(mins), window.get("usedPercent"), window.get("resetsAtMs"), mins),
        _window("weekly", weekly.get("usedPercent"), weekly.get("resetsAtMs"), 10080),
    ]
    return {"ok": True, "plan": _text(usage.get("tier")), "windows": [w for w in windows if w],
            "version": version, "source": "last_observed", "observed_at": _iso(usage.get("observedAtMs"))}


@_guard
def probe_muse(*, timeout: float = TIMEOUT_S) -> dict:
    """Muse's ``usage/read``: the figures as of the last Muse turn (marked
    ``observed_at``); never refreshed by spending a turn."""
    binary = _command("muse")
    if not binary:
        return _not_installed("muse")
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"clientInfo": _CLIENT_INFO, "capabilities": {"experimentalApi": True}}},
        {"jsonrpc": "2.0", "method": "initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "usage/read", "params": {}},
    ]
    frames = _converse([binary, "serve", "--no-session-log"], messages, lambda f: f.get("id") == 2,
                       env=probe_environment("muse", None, binary), deadline=time.monotonic() + timeout, close_stdin=True)
    return parse_muse(frames)


# ---- accounts ----------------------------------------------------------------


ACCOUNTS_MAX = 32
LABEL_MAX = 64
HOME_MAX = 1024
_ACCOUNT_FIELDS = ("label", "harness", "home")


class AccountsError(ValueError):
    """A ``usage_accounts`` list ``khipu config --set`` refuses. ``code`` is
    fixed (the app turns it into its own sentence and never shows the text);
    ``index`` is the 0-based entry at fault, ``field`` the one field at fault
    (label, harness or home) and ``other`` the earlier entry it repeats."""

    def __init__(self, message: str, code: str, *, index: int | None = None, field: str | None = None,
                 other: int | None = None):
        super().__init__(message)
        self.code, self.index, self.field, self.other = code, index, field, other

    def refusal(self) -> dict:
        return {"ok": False, "error": str(self), "code": self.code, "index": self.index, "field": self.field,
                "other": self.other}


def _entry_problem(harness: str, home: str | None) -> tuple[str, str, str] | None:
    """Why an entry cannot be read, as (code, field, plain reason), or None.
    The reason is shown in the app as written: no home, harness or path."""
    if harness not in HARNESSES:
        named = f'the CLI "{harness}"' if harness else "an entry with no CLI"
        return "unknown_cli", "harness", (f"Khipu does not read {named}; it reads Claude Code, Codex, "
                                          "Grok Build and Muse")
    if harness == "muse" and home:
        return "muse_folder", "home", "Muse has one sign-in, so it takes no login folder"
    path = _home_path(home) if home else None
    # A relative folder (".claude-second", the "~/" left off) can never be
    # read: each probe starts in a fresh empty temp folder, so it would always
    # find an empty, signed-out login.
    if home and (path is None or not path.is_absolute()):
        return "no_such_folder", "home", (f"Khipu cannot find the login folder {home}; start it with ~/ "
                                          "(for example ~/.codex-work) or give the full path")
    return None


def _account_from_entry(entry) -> dict:
    """One ``usage_accounts`` entry as an account; a problem goes in
    ``invalid`` (the report shows the entry instead of hiding it)."""
    if not isinstance(entry, dict):
        return {"label": "?", "harness": "?", "home": None, "invalid": "this entry is not an account"}
    harness = str(entry.get("harness") or "").strip().lower()
    label = str(entry.get("label") or "").strip() or LABELS.get(harness, harness or "?")
    home = entry.get("home")
    home = str(home).strip() if isinstance(home, str) and home.strip() else None
    account = {"label": label, "harness": harness or "?", "home": home}
    problem = _entry_problem(harness, home)
    if problem:
        account["invalid"] = problem[2]
    return account


def configured_accounts() -> list[dict]:
    """Config ``usage_accounts`` (``[{label, harness, home?}]``), or by default
    every installed CLI with its default home. Invalid entries come back with
    an ``invalid`` reason so the report shows them instead of hiding them."""
    from khipu.config import load_config

    raw = load_config().get("usage_accounts")
    if not isinstance(raw, list) or not raw:
        return [{"label": LABELS[h], "harness": h, "home": None} for h in HARNESSES if _command(h)]
    return [_account_from_entry(entry) for entry in raw]


def parse_usage_accounts(raw) -> list[dict]:
    """``raw`` (JSON text or a list) as the ``usage_accounts`` entries to store,
    or ``AccountsError`` (a ValueError) naming the first problem with a fixed
    code. Stricter than ``configured_accounts``, which shows a hand-edited bad
    entry rather than refusing it: ``khipu config --set usage_accounts``
    stores only entries that can be probed, each login once, each label once."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise AccountsError('usage_accounts must be JSON: a list of {"label", "harness", "home"} entries',
                                "bad_list") from None
    if not isinstance(raw, list):
        raise AccountsError('usage_accounts must be a list of {"label", "harness", "home"} entries', "bad_list")
    if len(raw) > ACCOUNTS_MAX:
        raise AccountsError(f"usage_accounts holds at most {ACCOUNTS_MAX} entries, got {len(raw)}", "too_many")
    out: list[dict] = []
    labels: dict[str, int] = {}
    logins: dict[str, int] = {}
    for i, entry in enumerate(raw):
        where = f"usage_accounts entry {i + 1}"
        if not isinstance(entry, dict):
            raise AccountsError(f"{where} is not an object", "bad_entry", index=i)
        extra = sorted(str(k) for k in entry if k not in _ACCOUNT_FIELDS)
        if extra:
            raise AccountsError(f"{where} has an unknown field {extra[0]!r}; the fields are label, harness and home",
                                "bad_entry", index=i)
        for field in _ACCOUNT_FIELDS:
            value = entry.get(field)
            if value is None:
                continue
            if not isinstance(value, str):
                raise AccountsError(f"{where}: {field} must be text", "not_one_line", index=i, field=field)
            if not value.isprintable():
                raise AccountsError(f"{where}: {field} must be one line of plain text", "not_one_line",
                                    index=i, field=field)
        for field, limit in (("label", LABEL_MAX), ("home", HOME_MAX)):
            if len((entry.get(field) or "").strip()) > limit:
                raise AccountsError(f"{where}: {field} is longer than {limit} characters", "too_long",
                                    index=i, field=field)
        harness = str(entry.get("harness") or "").strip().lower()
        home = (entry.get("home") or "").strip() or None
        problem = _entry_problem(harness, home)
        if problem:
            code, field, reason = problem
            raise AccountsError(f"{where}: {reason}", code, index=i, field=field)
        account = _account_from_entry(entry)
        name = account["label"].casefold()
        if name in labels:
            raise AccountsError(f"two usage_accounts entries are named {account['label']!r}", "name_taken",
                                index=i, field="label", other=labels[name])
        key = _key(account)
        if key in logins:
            raise AccountsError(f"{where} ({account['label']}) reads the same login as an earlier entry",
                                "login_taken", index=i, field="home", other=logins[key])
        labels[name] = i
        logins[key] = i
        stored = {"label": account["label"], "harness": account["harness"]}
        if account["home"]:
            stored["home"] = account["home"]
        out.append(stored)
    return out


def accounts_status() -> dict:
    """What ``khipu config`` shows: the stored ``usage_accounts`` (None when
    unset), where the list comes from, and the accounts it resolves to."""
    from khipu.config import load_config

    raw = load_config().get("usage_accounts")
    stored = raw if isinstance(raw, list) and raw else None
    try:
        user_home = str(Path.home())
    except RuntimeError:
        user_home = None
    # ``user_home`` lets the app compare "~/.x" with "/Users/me/.x" the way
    # the stored list is checked, before anything is saved.
    return {"value": stored, "source": "file" if stored is not None else "default",
            "accounts": configured_accounts(), "user_home": user_home}


def _key(account: dict) -> str:
    """Cache identity: the harness and the home whose login answers (labels
    are display names and may change)."""
    home = account.get("home")
    if not home:
        return f"{account['harness']}|"
    path = _home_path(home)
    return f"{account['harness']}|{path if path is not None else home}"


def _blank_row(account: dict) -> dict:
    return {"harness": account["harness"], "account": account["label"], "home": account.get("home"),
            "plan": None, "windows": [], "credits": None, "version": None, "source": None,
            "observed_at": None, "error": None, "note": None}


def probe_account(account: dict, *, timeout: float = TIMEOUT_S) -> dict:
    # probe_all supplies one shared run for its worker threads. A standalone
    # reset check owns one too, so a helper alarm or stop can unwind this call
    # and kill every vendor process group before the child exits.
    owned_run = getattr(_local, "run", None) is None
    if owned_run:
        _local.run = _Run()
    try:
        harness, home = account["harness"], account.get("home")
        started = time.monotonic()
        if account.get("invalid"):
            data = {"ok": False, "error": E_INVALID,
                    "note": f"This account entry is not valid: {account['invalid']}"}
        elif harness == "claude":
            data = probe_claude(home, timeout=timeout)
        elif harness == "codex":
            data = probe_codex(home, timeout=timeout)
        elif harness == "grok":
            data = probe_grok(home, timeout=timeout)
        elif harness == "muse":
            data = probe_muse(timeout=timeout)
        else:
            data = {"ok": False, "error": E_INVALID}
        row = _blank_row(account)
        row.update(data)
        row.update({"probed_at": _iso(_now().timestamp()), "ms": int((time.monotonic() - started) * 1000)})
        if not row.get("ok") and not row.get("note"):
            row["note"] = _note(row.get("error"), harness, home)
        return row
    finally:
        if owned_run:
            _local.run.kill_all()
            _local.run = None


def _failed_row(account: dict, code: str) -> dict:
    return dict(_blank_row(account), ok=False, error=code, note=_note(code, account["harness"], account.get("home")),
                probed_at=_iso(_now().timestamp()), ms=None)


def probe_all(accounts: list[dict], *, timeout: float = TIMEOUT_S) -> list[dict]:
    """Every account in parallel; one hung CLI never delays the others past
    its own timeout, and a failed probe never hides the rest. Returns by an
    overall deadline (``timeout`` + ``OVERALL_GRACE_S``) whatever happens: a
    probe still running then is reported ``timeout`` and its process group is
    killed. Daemon threads, no executor: nothing waits on a stuck worker,
    interpreter exit included."""
    if not accounts:
        return []
    run = _Run()
    results: list[dict | None] = [None] * len(accounts)

    def work(i: int, account: dict) -> None:
        _local.run = run
        try:
            results[i] = probe_account(account, timeout=timeout)
        except Exception:  # noqa: BLE001 — one account never breaks the report
            results[i] = _failed_row(account, E_INTERNAL)
        finally:
            _local.run = None

    threads = [threading.Thread(target=work, args=(i, a), daemon=True, name=f"khipu-usage-{a['harness']}")
               for i, a in enumerate(accounts)]
    for thread in threads:
        thread.start()
    end = time.monotonic() + timeout + OVERALL_GRACE_S
    for thread in threads:
        thread.join(max(0.0, end - time.monotonic()))
    run.kill_all()
    return [results[i] if results[i] is not None else _failed_row(a, E_TIMEOUT)
            for i, a in enumerate(accounts)]


# ---- cache + report ------------------------------------------------------------


def cache_file() -> Path:
    from khipu.paths import data_dir

    return data_dir() / "state" / CACHE_NAME


def _load_cache() -> dict[str, dict]:
    try:
        data = json.loads(cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return {}  # an older layout (keyed by label) is simply re-probed
    rows = data.get("rows")
    return {k: v for k, v in rows.items() if isinstance(v, dict)} if isinstance(rows, dict) else {}


def _save_cache(rows: dict[str, dict]) -> None:
    path = cache_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp",
                                         encoding="utf-8") as fh:
            json.dump({"version": CACHE_VERSION, "rows": rows}, fh, indent=2, sort_keys=True)
            tmp = Path(fh.name)
        os.replace(tmp, path)
    except OSError:
        pass  # a read-only data dir costs the cache, never the answer


def _open_lock(name: str) -> int | None:
    path = cache_file().with_name(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None


def _try_refresh_lock() -> int | None:
    """The refresh lock (one refresh at a time across every process on this
    Mac), or None when another refresh holds it. flock: released if the holder
    dies."""
    fd = _open_lock(REFRESH_LOCK_NAME)
    if fd is None:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _release(fd: int | None) -> None:
    if fd is None:
        return
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(fd)


def _merge(previous: dict | None, row: dict) -> dict:
    """A good probe replaces the row. A failed one keeps the last good figures
    with their own ``probed_at`` (and ``last_good_at``); the attempt goes in
    ``last_attempt_at`` / ``last_error``, however many failures in a row.
    The note is the failure's own (the ``/login`` hint, say): a note that
    came with the kept figures (the Codex fallback's) held only while that
    read was good."""
    attempt = row.get("probed_at")
    if row.get("ok"):
        return dict(row, last_attempt_at=attempt, last_error=None, last_good_at=attempt)
    if previous and previous.get("last_good_at"):
        kept = {k: v for k, v in previous.items() if k != "live_error"}
        kept.update(ok=False, error=row.get("error"), last_attempt_at=attempt, last_error=row.get("error"),
                    ms=row.get("ms"), account=row.get("account"), home=row.get("home"), note=row.get("note"))
        return kept
    return dict(row, last_attempt_at=attempt, last_error=row.get("error"), last_good_at=None)


def _store(accounts: list[dict], rows: list[dict]) -> list[dict]:
    """Merge fresh rows into the cache under the write lock (re-read first,
    so concurrent writers lose nothing) and return the merged rows."""
    fd = _open_lock(WRITE_LOCK_NAME)
    try:
        if fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_EX)
        cache = _load_cache()
        merged = []
        for account, row in zip(accounts, rows):
            key = _key(account)
            out = _merge(cache.get(key), row)
            if not account.get("invalid"):
                cache[key] = {k: v for k, v in out.items() if k not in ("cached", "stale", "refreshing")}
            merged.append(out)
        _save_cache(cache)
        return merged
    finally:
        _release(fd)


_refresh_thread: threading.Thread | None = None


def _start_refresh(accounts: list[dict], timeout: float) -> bool:
    """Start one background refresh unless one is already running anywhere.
    True when a refresh is (now) running."""
    global _refresh_thread
    fd = _try_refresh_lock()
    if fd is None:
        return True  # another refresh holds the lock

    def refresh() -> None:
        try:
            _store(accounts, probe_all(accounts, timeout=timeout))
        except Exception:  # noqa: BLE001 — a background refresh never raises into the server
            pass
        finally:
            _release(fd)

    _refresh_thread = threading.Thread(target=refresh, daemon=True, name="khipu-usage-refresh")
    _refresh_thread.start()
    return True


def wait_for_refresh(timeout: float | None = None) -> bool:
    """Join this process's background refresh (tests, and a caller that wants
    to); True when none is running."""
    thread = _refresh_thread
    if thread is None:
        return True
    thread.join(timeout)
    return not thread.is_alive()


def _age_s(row: dict, now: datetime) -> float | None:
    when = _parse_time(row.get("observed_at")) or _parse_time(row.get("probed_at"))
    return None if when is None else (now - when).total_seconds()


def report(*, fresh: bool = False, max_age: float = DEFAULT_MAX_AGE_S, timeout: float = TIMEOUT_S,
           accounts: list[dict] | None = None, background: bool = False) -> dict:
    """One row per account. A cached row is reused while its last attempt is
    younger than ``max_age`` seconds (``fresh`` bypasses it).

    ``background=False`` (the CLI): probe what is due now, bounded by
    ``probe_all``'s overall deadline. ``background=True`` (the MCP tool):
    never wait on a probe: answer from the cache at once (an account never
    probed reads ``pending``) and start at most one refresh in the background
    (``refreshing: true``).

    A row whose figures are older than ``max_age`` (a cached row, Muse's last
    observed turn, Codex's rollout fallback), or that holds the last good
    figures after a failed probe, is marked ``stale: true``, never dropped."""
    accounts = configured_accounts() if accounts is None else accounts
    cache = _load_cache()
    now = _now()
    rows: list[dict | None] = [None] * len(accounts)
    due: list[int] = []
    for i, account in enumerate(accounts):
        if account.get("invalid"):
            rows[i] = dict(_merge(None, probe_account(account, timeout=timeout)), cached=False)
            continue
        cached = cache.get(_key(account))
        attempted = _parse_time((cached or {}).get("last_attempt_at") or (cached or {}).get("probed_at"))
        young = bool(cached and attempted and (now - attempted).total_seconds() <= max_age)
        if fresh or not young:
            due.append(i)
        if cached and (background or (young and not fresh)):
            rows[i] = dict(cached, account=account["label"], home=account.get("home"), cached=True)
    refreshing = False
    if due and background:
        refreshing = _start_refresh([accounts[i] for i in due], timeout)
        for i in due:
            if rows[i] is None:
                rows[i] = dict(_blank_row(accounts[i]), ok=False, error=E_PENDING, note=_note(E_PENDING),
                               probed_at=None, ms=None, cached=False)
            rows[i]["refreshing"] = refreshing
    elif due:
        fd = _try_refresh_lock()  # keeps a background refresh from starting meanwhile; never waited on
        if fd is None:
            # A reset check and every foreground refresh share this lock.  Do
            # not start a second child merely because this is the CLI path:
            # report the in-flight read in the same shape as the background
            # path instead.
            refreshing = True
            for i in due:
                if rows[i] is None:
                    rows[i] = dict(_blank_row(accounts[i]), ok=False, error=E_PENDING, note=_note(E_PENDING),
                                   probed_at=None, ms=None, cached=False)
                rows[i]["refreshing"] = True
        else:
            try:
                probed = probe_all([accounts[i] for i in due], timeout=timeout)
                merged = _store([accounts[i] for i in due], probed)
            finally:
                _release(fd)
            for i, row in zip(due, merged):
                rows[i] = dict(row, cached=False)
    now = _now()
    for row in rows:
        age = _age_s(row, now)
        row["stale"] = bool((not row.get("ok") and row.get("last_good_at"))
                            or (age is not None and age > max_age))
    return {"generated_at": _iso(now.timestamp()), "max_age_s": max_age, "refreshing": refreshing,
            "accounts": rows, "summary_text": [summary_line(r) for r in rows]}


# ---- text ----------------------------------------------------------------------


def _window_label(window: dict) -> str:
    name = window.get("name") or "window"
    if name == "five_hour":
        return "5 h"
    if name == "weekly":
        return "week"
    if name == "daily":
        return "day"
    if name.startswith("weekly_"):
        return f"week {window.get('model') or name.removeprefix('weekly_')}"
    return name.replace("_", " ")


def _pct(value) -> str:
    return f"{value:g}%" if isinstance(value, (int, float)) else "?"


def _when(iso: str | None) -> str:
    dt = _parse_time(iso)
    return dt.strftime("%a %H:%MZ") if dt else "?"


def summary_line(row: dict) -> str:
    """'Claude (max): 5 h 13%, week 34%; tightest week 34% resets Sun 15:00Z'."""
    head = row.get("account") or row.get("harness") or "?"
    if row.get("plan"):
        head += f" ({row['plan']})"
    windows = row.get("windows") or []
    if row.get("error") == E_PENDING and not windows:
        return f"{head}: first probe running, ask again in a few seconds"
    if not row.get("ok") and not windows:
        return f"{head}: unavailable: {row.get('error') or 'probe failed'}"
    if not windows:
        text = f"{head}: {row.get('note') or 'no windows reported'}"
    else:
        text = f"{head}: " + ", ".join(f"{_window_label(w)} {_pct(w.get('used_pct'))}" for w in windows)
        tight = max(windows, key=lambda w: w.get("used_pct") if isinstance(w.get("used_pct"), (int, float)) else -1)
        if tight.get("resets_at"):
            text += f"; tightest {_window_label(tight)} {_pct(tight.get('used_pct'))} resets {_when(tight['resets_at'])}"
    credits = row.get("credits") or {}
    for key, label in (("balance", "credits"), ("prepaid_balance", "prepaid")):
        if isinstance(credits.get(key), (int, float)):
            text += f", {label} {credits[key]:g}"
    if row.get("observed_at"):
        text += f", as of {_when(row['observed_at'])}"
    if row.get("source") == "rollout":
        text += " (from the last Codex turn)"
    if not row.get("ok"):
        text += f" [probe failed: {row.get('error')}]"
    if row.get("stale"):
        text += " [stale]"
    return text


def table(rep: dict) -> str:
    header = ("ACCOUNT", "PLAN", "WINDOW", "USED", "RESETS", "NOTE")
    lines: list[tuple[str, ...]] = []
    for row in rep.get("accounts") or []:
        notes = []
        if not row.get("ok"):
            notes.append(f"failed: {row.get('error')}")
        if row.get("note"):
            notes.append(row["note"])
        if row.get("observed_at"):
            notes.append(f"as of {_when(row['observed_at'])}")
        credits = row.get("credits") or {}
        if isinstance(credits.get("balance"), (int, float)):
            notes.append(f"credits {credits['balance']:g}")
        if isinstance(credits.get("prepaid_balance"), (int, float)):
            notes.append(f"prepaid {credits['prepaid_balance']:g}")
        if row.get("stale"):
            notes.append("stale")
        windows = row.get("windows") or [None]
        for n, w in enumerate(windows):
            lines.append((
                (row.get("account") or "?") if n == 0 else "",
                (row.get("plan") or "-") if n == 0 else "",
                _window_label(w) if w else "-",
                _pct(w.get("used_pct")) if w else "-",
                _when(w.get("resets_at")) if w and w.get("resets_at") else "-",
                "; ".join(notes) if n == 0 else "",
            ))
    widths = [max(len(r[i]) for r in [header, *lines]) for i in range(len(header) - 1)]
    out = []
    for r in [header, *lines]:
        out.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r[:-1])) + "  " + r[-1])
    return "\n".join(line.rstrip() for line in out)


# ---- doctor and khipu_status: the cache only --------------------------------------

# The line the app's Home callout uses too (PlansCard.tsx PLAN_ALERT_PCT).
ALERT_PCT = 90.0


def _shown_pct(value) -> float | None:
    """The one-decimal figure the app shows and judges its lines on
    (``Math.round(x * 10) / 10``), or None when the CLI gave no number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return math.floor(value * 10 + 0.5) / 10


def _window_words(window: dict) -> str:
    """'five-hour', 'week', 'Fable week', 'day', '60 min'."""
    name = window.get("name") or "window"
    if name == "five_hour":
        return "five-hour"
    if name == "weekly":
        return "week"
    if name == "daily":
        return "day"
    if name.startswith("weekly_"):
        return f"{window.get('model') or name.removeprefix('weekly_').replace('_', ' ')} week"
    mins = re.fullmatch(r"(\d+)_min", name)
    return f"{mins.group(1)} min" if mins else name.replace("_", " ")


def _reset_words(iso: str | None, now: datetime) -> str | None:
    """'resets in 40 min' under an hour, else 'resets Sun 15:00Z'."""
    when = _parse_time(iso)
    if when is None:
        return None
    ahead = (when - now).total_seconds()
    if ahead < 3600:
        return f"resets in {max(1, round(ahead / 60))} min"
    return f"resets {_when(iso)}"


def _in_force(window: dict, now: datetime) -> bool:
    """A window with a figure whose reset has not passed (a passed reset has
    started the window again, so its old figure no longer applies)."""
    if not isinstance(window, dict) or _shown_pct(window.get("used_pct")) is None:
        return False
    when = _parse_time(window.get("resets_at"))
    return when is None or when > now


def doctor_block(*, max_age: float = DEFAULT_MAX_AGE_S, now: datetime | None = None) -> dict:
    """What ``khipu doctor`` and ``khipu_status`` carry: per configured
    account, its tightest window still in force (label, used %, reset time,
    stale flag), and one warning line per window still in force at
    ``ALERT_PCT`` or more. Read from the usage cache only: no CLI is probed,
    no lock is waited on, and nothing here ever gates doctor's ``ok``."""
    now = now or _now()
    cache = _load_cache()
    accounts: list[dict] = []
    warnings: list[str] = []
    for account in configured_accounts():
        entry = {"account": account["label"], "harness": account["harness"], "home": account.get("home"),
                 "ok": None, "error": None, "plan": None, "window": None, "used_pct": None, "resets_at": None,
                 "figures_at": None, "stale": False}
        if account.get("invalid"):
            entry.update(ok=False, error=E_INVALID)
            accounts.append(entry)
            continue
        row = cache.get(_key(account))
        if not row:
            entry["never_read"] = True  # `khipu usage` has not read this account yet
            accounts.append(entry)
            continue
        age = _age_s(row, now)
        stale = bool((not row.get("ok") and row.get("last_good_at")) or (age is not None and age > max_age))
        live = [w for w in row.get("windows") or [] if _in_force(w, now)]
        tight = max(live, key=lambda w: _shown_pct(w.get("used_pct")), default=None)
        entry.update(ok=bool(row.get("ok")), error=row.get("error"), plan=row.get("plan"),
                     figures_at=row.get("observed_at") or row.get("probed_at"), stale=stale)
        if tight is not None:
            entry.update(window=_window_words(tight), used_pct=_shown_pct(tight.get("used_pct")),
                         resets_at=tight.get("resets_at"))
        accounts.append(entry)
        head = account["label"] + (f" ({row['plan']})" if row.get("plan") else "")
        kept = f" as of {_when(entry['figures_at'])}" if stale or not row.get("ok") else ""
        for window in sorted(live, key=lambda w: -_shown_pct(w.get("used_pct"))):
            pct = _shown_pct(window.get("used_pct"))
            if pct < ALERT_PCT:
                continue
            reset = _reset_words(window.get("resets_at"), now)
            warnings.append(f"Plan nearly used up: {head} {_window_words(window)} at {pct:g}%{kept}"
                            + (f", {reset}" if reset else ""))
    return {"source": "cache", "max_age_s": max_age, "alert_pct": ALERT_PCT, "accounts": accounts,
            "warnings": warnings}
