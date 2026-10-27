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
          ``usage/read``; a fresh host has observed nothing, so the figures
          come from one tiny turn at most every MUSE_TURN_EVERY_S (never
          while Muse is limited) and are carried as last observed between
          turns; refusals in Muse's own session logs mark it limited

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
import copy
import fcntl
import hashlib
import heapq
import json
import math
import os
import re
import select
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from pathlib import Path

HARNESSES = ("claude", "codex", "grok", "muse", "glm")
LABELS = {"claude": "Claude", "codex": "Codex", "grok": "Grok", "muse": "Muse", "glm": "GLM"}
# One sign-in each and no login folder: Muse's is its own login, GLM's is the
# Coding Plan key (a Claude Code lane pointed at z.ai, not an Anthropic home).
NO_FOLDER = ("muse", "glm")
TIMEOUT_S = 5.0
# Grok's probe starts a whole `grok agent` (and its MCP servers), which took
# longer than TIMEOUT_S under load; the other harnesses' probes stay cheap.
GROK_TIMEOUT_S = 20.0
# GLM's probe is one HTTPS call to z.ai's quota endpoint, no child process.
GLM_TIMEOUT_S = 15.0
GLM_QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
DEEPSEEK_HISTORY_NAME = "deepseek-balance.json"
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
# Signed in on a plan with limits, but this reply left the figures out: a
# passing gap, not a sign-in problem, so the last figures stay shown.
E_NO_FIGURES = "no_plan_figures"
E_CLI = "cli_error"
E_INVALID = "invalid_account"
E_INTERNAL = "internal_error"
E_PENDING = "pending"
E_NO_FOLDER = "login_folder_missing"
E_NO_KEY = "no_key"
# DeepSeek reported balances only in a currency Khipu does not convert.
E_CURRENCY = "currency_unsupported"
# DeepSeek answered is_available false: the account cannot be used just now.
E_UNAVAILABLE = "account_unavailable"
E_NO_PLAN = "no_plan"
# z.ai refused the key: not a sign-in an owner can redo in a CLI, so it has its own code.
E_BAD_KEY = "key_rejected"
# The http-quota endpoint tried to send Khipu to a host the owner never confirmed.
E_REDIRECT = "redirect_refused"
HTTP_QUOTA_TIMEOUT_S = 15.0
HTTP_QUOTA_MAX_BYTES = 1 << 20
# A quota window longer than a year is not a window, it is a hostile figure.
HTTP_QUOTA_MAX_WINDOW_MINS = 366 * 24 * 60
NO_FOLDER_NOTE = "Khipu can't find this account's login folder on this Mac"
# The CLIs by the names the app's form gives them.
CLI_NAMES = {"claude": "Claude Code", "codex": "Codex", "grok": "Grok Build", "muse": "Muse",
             "glm": "GLM Coding Plan"}
# Plain words only: the app shows notes as written to an owner who is not
# technical, so a note names a login folder, never a home, a PATH, a token or
# a probe. The live read's own code goes in the row's ``live_error`` field,
# never into the note.
ROLLOUT_NOTE = "Codex did not answer just now; these figures are from its last turn"


def _note(code: str | None, harness: str | None = None, home: str | None = None) -> str | None:
    """Khipu's own sentence for a failure ``code`` on one account, or None."""
    name = CLI_NAMES.get(harness or "", "This CLI")
    folder = " with this login folder" if home else ""
    if code == E_REDIRECT:
        return "The quota endpoint tried to send Khipu to another host, so Khipu refused the answer"
    if code == E_CURRENCY:
        return "This account's balance is not in USD, which is the only currency Khipu reads"
    if code == E_UNAVAILABLE:
        return "The provider says this account can't be used just now, so its spend is unknown"
    if harness == "glm":
        return _glm_note(code)
    if harness == "deepseek":
        return _deepseek_note(code)
    if code == E_NOT_INSTALLED:
        return f"{name} is not installed on this Mac"
    if code == E_SIGNIN:
        return f"{name} is not signed in{folder}. Open {name}{folder} and sign in again"
    if code == E_NO_LIMITS:
        return f"This sign-in reports no plan limits. Open {name}{folder} and sign in again (/login)"
    if code == E_NO_FIGURES:
        return f"{name} did not send its plan figures just now; Khipu keeps the last ones and asks again"
    if code == E_PENDING:
        return "Reading for the first time; ask again in a few seconds"
    if code in (E_NO_KEY, E_NO_PLAN, E_BAD_KEY):
        return _glm_note(code)
    # Every other code the probes can return gets its plain cause too, so the
    # app shows Khipu's words for every failure and never invents its own.
    if code == E_TIMEOUT:
        return f"{name} didn't answer in time"
    if code == E_NO_ANSWER or (code and re.fullmatch(r"(exit|signal)_\d+", code)):
        return f"{name} didn't answer"
    if code == E_CLI:
        return f"{name} reported an error when asked"
    if code == E_BAD:
        return f"{name} answered in a way Khipu can't read"
    if code == E_START:
        return f"{name} could not be started"
    if code == E_INVALID:
        return "This account entry is not valid"
    if code == E_NO_FOLDER:
        return NO_FOLDER_NOTE
    if code == E_INTERNAL:
        return "Khipu could not run the check"
    return None


def _deepseek_note(code: str | None) -> str | None:
    """DeepSeek has no CLI: its read is the balance call Khipu makes with a key."""
    if code == E_NO_KEY:
        return "Khipu has no DeepSeek key; add it in Settings under Accounts and routing"
    if code in (E_SIGNIN, E_BAD_KEY):
        return "DeepSeek did not accept the key; replace it in Settings under Accounts and routing"
    if code in (E_NOT_INSTALLED, E_TIMEOUT, E_NO_ANSWER):
        return "Khipu can't reach DeepSeek from this Mac just now"
    if code in (E_BAD, E_CLI):
        return "DeepSeek answered in a way Khipu can't read; its spend is unknown"
    return None


def _glm_note(code: str | None) -> str | None:
    """GLM has no CLI to open or sign in to: its sign-in is a key Khipu reads."""
    if code == E_NO_KEY:
        return "Khipu has no GLM key; add it in Settings under Accounts and routing"
    if code == E_NOT_INSTALLED:
        return "Khipu can't reach z.ai from this Mac"
    if code in (E_SIGNIN, E_BAD_KEY):
        return "z.ai did not accept the GLM key; replace it in Settings under Accounts and routing"
    if code == E_NO_PLAN:
        return "z.ai reports no GLM Coding Plan on this key"
    if code == E_NO_LIMITS:
        return "z.ai reported no plan limits for this key"
    if code == E_NO_FIGURES:
        return "z.ai did not send its plan figures just now; Khipu keeps the last ones and asks again"
    if code == E_PENDING:
        return "Reading for the first time; ask again in a few seconds"
    if code == E_TIMEOUT:
        return "z.ai didn't answer in time"
    if code in (E_NO_ANSWER, E_START) or (code and re.fullmatch(r"(exit|signal)_\d+", code)):
        return "z.ai didn't answer"
    if code == E_CLI:
        return "z.ai reported an error when asked"
    if code == E_BAD:
        return "z.ai answered in a way Khipu can't read"
    if code == E_INVALID:
        return "This account entry is not valid"
    if code == E_NO_FOLDER:
        return NO_FOLDER_NOTE
    if code == E_INTERNAL:
        return "Khipu could not run the check"
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
                     r"unauthori[sz]ed|unauthenticated|authentication (failed|required)|\b401\b|"
                     r"(invalid|expired)( or expired)? credentials|no auth context", re.I)


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


def _login_folder_there(home: str) -> bool:
    """Whether ``home`` is a folder: a stat of the folder itself (a symlink
    to a folder counts), never a look inside it."""
    path = _home_path(home)
    try:
        return path is not None and stat.S_ISDIR(os.stat(path).st_mode)
    except (OSError, ValueError):
        return False


def _probe_path(inherited: str | None, binary: str | None) -> str:
    """The child's PATH: the caller's own entries first, then the CLI's folder,
    the folder of the file it links to, the usual tool folders and the system
    ones, each once (see ``_TOOL_DIRS``)."""
    dirs = [d for d in (inherited or "").split(os.pathsep) if d]
    # The Node folder the owner set in the app goes ahead of everything the
    # lookup guessed (settings clarity phase 9, decision 10).
    with contextlib.suppress(Exception):
        from khipu.config import cli_setting

        node_folder = cli_setting("node_folder")
        if node_folder:
            dirs.insert(0, node_folder)
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
        self.until = 0.0

    def add(self, proc: subprocess.Popen) -> None:
        with self._lock:
            if not self._closed:
                self._procs.add(proc)
                return
        _killpg(proc)  # started after the deadline

    def discard(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)

    def extend(self, deadline: float) -> None:
        """A probe that may run past the overall deadline by design (Muse's
        turn) names its own; the overall wait stretches to it."""
        with self._lock:
            self.until = max(self.until, deadline)

    @contextlib.contextmanager
    def committing(self):
        """Serialize persistent writes with cancellation of this batch."""
        with self._lock:
            if self._closed:
                raise ProbeError(E_TIMEOUT)
            yield

    def kill_all(self) -> None:
        with self._lock:
            self._closed = True
            procs = list(self._procs)
        for proc in procs:
            _killpg(proc)


_local = threading.local()


def _probe_commit():
    run = getattr(_local, "run", None)
    return run.committing() if run is not None else contextlib.nullcontext()


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
              deadline: float, close_stdin: bool, keep=None, follow=None) -> list[dict]:
    """Write ``messages``, read JSON lines until ``is_answer(frame)`` or the
    ``deadline`` (monotonic), then kill the whole process group.

    The child runs in a fresh empty temp directory. ``close_stdin``: close
    stdin right after the requests. Codex and Grok exit on end of input before
    answering, so for them stdin stays open until the answer arrives (the kill
    closes it). Returns the response frames read; notifications are dropped
    unread (Grok's carry the user's MCP configuration). ``keep`` replaces the
    test for which frames are response frames. ``follow(frame)`` returns the
    requests to write once that response frame has arrived (None: none)."""
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
                    if keep(frame) if keep else ("id" in frame or frame.get("type") == "control_response"):
                        frames.append(frame)
                        if is_answer(frame):
                            return frames
                        more = follow(frame) if follow else None
                        if more:
                            try:
                                proc.stdin.write("".join(json.dumps(m) + "\n" for m in more).encode())
                                proc.stdin.flush()
                            except (BrokenPipeError, OSError, ValueError):
                                pass
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
                # Grok puts the reason in ``data`` under a bare "Internal error".
                text = " ".join(str(err.get(k)) for k in ("message", "data") if isinstance(err.get(k), str)) \
                    if isinstance(err, dict) else err
                raise ProbeError(_classify(text, E_CLI))
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
        raise ProbeError(E_NO_FIGURES if body.get("rate_limits_available") is True else E_NO_LIMITS)
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
def probe_claude(config_dir: str | None = None, *, timeout: float = TIMEOUT_S, turn: bool = True) -> dict:
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
    deadline = time.monotonic() + timeout
    env = probe_environment("claude", config_dir, binary)
    frames = _converse([binary, *_CLAUDE_ARGS], messages, _claude_is_answer, env=env, deadline=deadline,
                       close_stdin=True)
    try:
        return parse_claude(frames)
    except ProbeError as exc:
        if exc.code != E_NO_FIGURES or not turn or not _turn_due(config_dir):
            raise
    # probe_all stops every probe OVERALL_GRACE_S after its timeout; the turn
    # ends just before that, so a slow turn reads as missing figures.
    turn = _claude_turn(binary, env, deadline + OVERALL_GRACE_S - 0.25)
    plan = _text(_dict(_dict(frames[-1], "response"), "response").get("subscription_type"))
    return dict(turn, plan=plan) if turn else {"ok": False, "error": E_NO_FIGURES}


# Claude Code answers get_usage from its own usage endpoint. When that endpoint
# turns the login away (HTTP 429, seen on a busy account from 2026-10-16), the
# reply carries no figures, while every model reply still carries them in its
# rate-limit headers. So Khipu asks for one tiny turn (cheapest model, no tools,
# a one-line system prompt) at most once per TURN_EVERY_S for each login, and
# reads the figures from the turn's rate_limit_event.
TURN_EVERY_S = 1800
TURN_STAMP_NAME = "usage.turn.json"
TURN_LOCK_NAME = "usage.turn.lock"
_CLAUDE_TURN_ARGS = ("-p", "Reply with OK.", "--output-format", "stream-json", "--verbose",
                     "--setting-sources", "project", "--no-session-persistence", "--strict-mcp-config",
                     "--model", "haiku", "--max-turns", "1", "--tools", "", "--system-prompt", "Reply with OK.")
_TURN_WINDOWS = {"five_hour": ("five_hour", 300), "seven_day": ("weekly", 10080)}


def _turn_due(config_dir: str | None, *, key: str | None = None, every: float = TURN_EVERY_S,
              directory: Path | None = None) -> bool:
    """True, and the attempt recorded, when this login's last turn was
    ``every`` seconds ago or more (or never). A data dir Khipu can't write
    means no turn: an unrecorded turn would run on every read. ``key`` names
    a login that has no folder (Muse); ``directory`` keeps its allowance
    beside that login rather than in a movable Khipu data directory."""
    if key is None:
        key = str(_home_path(config_dir) or config_dir) if config_dir else ""
    path = (directory / TURN_STAMP_NAME) if directory is not None else cache_file().with_name(TURN_STAMP_NAME)
    fd = _open_lock(TURN_LOCK_NAME, directory=path.parent)
    if fd is None:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            stamps = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stamps = {}
        if not isinstance(stamps, dict):
            stamps = {}
        now = time.time()
        last = stamps.get(key)
        if isinstance(last, (int, float)) and not isinstance(last, bool) and 0 <= now - last < every:
            return False
        stamps[key] = now
        try:
            tmp = path.with_name(TURN_STAMP_NAME + ".tmp")
            tmp.write_text(json.dumps(stamps, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            return False
        return True
    finally:
        _release(fd)


def _claude_turn_frame(frame: dict) -> bool:
    return frame.get("type") in ("rate_limit_event", "result") or (
        frame.get("type") == "system" and frame.get("subtype") == "init")


def parse_claude_turn(frames: list[dict]) -> dict | None:
    """The figures in a turn's last rate_limit_event, or None without one.
    ``unifiedWindows`` holds every window; an event without it names only the
    window it is about (``rateLimitType``). Utilization is a fraction there."""
    info = None
    version = None
    for frame in frames:
        if frame.get("type") == "rate_limit_event" and isinstance(frame.get("rate_limit_info"), dict):
            info = frame["rate_limit_info"]
        elif frame.get("type") == "system":
            version = _text(frame.get("claude_code_version"))
    if info is None:
        return None
    blocks = _dict(info, "unifiedWindows") or {info.get("rateLimitType"): info}
    windows = []
    for key, (name, mins) in _TURN_WINDOWS.items():
        block = blocks.get(key)
        if not isinstance(block, dict):
            continue
        used = block.get("utilization")
        used = round(used * 100, 2) if isinstance(used, (int, float)) and not isinstance(used, bool) else used
        windows.append(_window(name, used, block.get("resetsAt"), mins))
    windows = [w for w in windows if w]
    if not windows:
        return None
    # Fresh from the server as the turn ran: a live read, unlike Codex's
    # rollout figures, which may predate a limit.
    return {"ok": True, "windows": windows, "credits": None, "version": version, "source": "live"}


def _claude_turn(binary: str, env: dict[str, str], deadline: float) -> dict | None:
    try:
        frames = _converse([binary, *_CLAUDE_TURN_ARGS], [], lambda f: f.get("type") == "result", env=env,
                           deadline=deadline, close_stdin=True, keep=_claude_turn_frame)
        return parse_claude_turn(frames)
    except ProbeError:
        return None


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
    best = None
    best_at = None
    for _mtime, path in sorted(newest, reverse=True):
        if time.monotonic() > deadline:
            return None
        event = _rollout_event(path)
        if not event:
            continue
        limits = event["payload"]["rate_limits"]
        try:
            candidate = {"ok": True, "plan": _text(limits.get("plan_type")),
                         "windows": _codex_windows(limits, camel=False),
                         "credits": _codex_credits(limits, camel=False),
                         "source": "rollout", "observed_at": _iso(event.get("timestamp"))}
        except ProbeError:
            continue
        at = _parse_time(candidate["observed_at"])
        if best is None or (at is not None and (best_at is None or at > best_at)):
            best, best_at = candidate, at
    return best


@_guard
def probe_codex(home: str | None = None, *, timeout: float = TIMEOUT_S, rollout: bool = True) -> dict:
    """Codex's ``account/rateLimits/read``; on failure, the last turn's figures
    from the rollout files (``source: "rollout"``). Both fit in ``timeout``:
    the live call leaves a fifth of it (at most 1 s) for the fallback.
    ``rollout=False`` (discovery and ``--try``) never falls back: the rollout
    files are inside the login folder, which those reads never open."""
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
                           env=probe_environment("codex", home, binary),
                           deadline=deadline - (min(1.0, timeout * 0.2) if rollout else 0.0), close_stdin=False)
        return parse_codex(frames)
    except ProbeError as exc:
        if not rollout:
            raise
        fallback = codex_rollout(home, deadline=deadline)
        if fallback is None:
            raise
        fallback["note"] = _rollout_note(exc.code, home)
        fallback["live_error"] = exc.code
        return fallback


# ---- Grok --------------------------------------------------------------------


def _account_digest(harness: str, account_id) -> str | None:
    """Only a digest of a CLI-reported account id is ever kept: equality is
    all a subscription grouping needs, so the raw id is dropped here."""
    if not isinstance(account_id, str) or not account_id.strip():
        return None
    return "sub:" + hashlib.sha256(f"{harness}:{account_id.strip()}".encode()).hexdigest()[:24]


def parse_grok(frames: list[dict]) -> dict:
    if not any(frame.get("id") == 2 for frame in frames):
        _answer(frames, 1)  # a failed initialize is the probe's error
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
    account_id = None
    for source in (result, config):
        for key in ("account_id", "accountId", "user_id", "userId"):
            if isinstance(source.get(key), str) and source[key].strip():
                account_id = source[key].strip()
                break
        if account_id:
            break
    out = {"ok": True, "plan": _text(result.get("subscription_tier")), "windows": [window] if window else [],
           "credits": credits, "version": version, "source": "live"}
    digest = _account_digest("grok", account_id)
    if digest:
        out["account_digest"] = digest
    return out


def _grok_budget(timeout: float) -> float:
    """Grok's own budget when the caller left the default; an explicit
    timeout (anything but the default) is honoured as given."""
    return GROK_TIMEOUT_S if timeout == TIMEOUT_S else timeout


@_guard
def probe_grok(grok_home: str | None = None, *, timeout: float = TIMEOUT_S) -> dict:
    """Grok Build's ``_x.ai/billing`` (ACP, undocumented) for the login in
    ``grok_home`` (``GROK_HOME``; None = the default ``~/.grok``). See
    ``_GROK_ENV``: Grok cannot be started with no MCP servers at all.

    The billing request goes only after ``initialize`` has answered: sent
    together, Grok Build 1.0.52 can run it before the session has its
    sign-in and answers "Invalid or expired credentials ... no auth
    context", which read as ``cli_error`` from 2026-10-14 on."""
    binary = _command("grok")
    if not binary:
        return _not_installed("grok")
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": 1, "clientCapabilities": {}, "clientInfo": _CLIENT_INFO}}
    billing = {"jsonrpc": "2.0", "id": 2, "method": "_x.ai/billing", "params": {}}
    frames = _converse([binary, "agent", "--no-leader", "stdio"], [initialize],
                       lambda f: f.get("id") == 2 or (f.get("id") == 1 and f.get("error") is not None),
                       env=probe_environment("grok", grok_home, binary), deadline=time.monotonic() + _grok_budget(timeout),
                       close_stdin=False, follow=lambda f: [billing] if f.get("id") == 1 else None)
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


# A fresh ``muse serve`` host answers ``usage/read`` with {} until one of its
# own turns has run: the figures arrive in the provider's reply to a model
# call, and Muse keeps no copy across processes or in its session logs
# (checked on 1.4.6, 2026-10-18). So, like Claude's fallback, Khipu runs one
# tiny turn (the cheapest model, reasoning off, shell and writes off, every
# tool call denied) at most once per MUSE_TURN_EVERY_S, and only when the
# figures it holds are older than MUSE_REFRESH_S or a reset time in them has
# passed, and never while Muse is known to be limited. Measured 2026-10-18:
# about 29,200 input tokens a turn (Muse's own system prompt), and two turns
# moved neither the five-hour nor the weekly figure by a whole point.
MUSE_TURN_EVERY_S = 1800
MUSE_REFRESH_S = 15 * 60  # routing.FIGURES_STALE_S: older figures read "may be out of date"
MUSE_TURN_S = 20.0
MUSE_TURN_KEY = "muse|"
# Muse has one login per user, independent of Khipu's configured data directory.
MUSE_TURN_STATE = (".local", "state", "khipu", "muse")
MUSE_TURN_MODEL = "muse-spark-1.3"
MUSE_TURN_PROMPT = "Reply with OK."
_MUSE_TURN_ARGS = ("serve", "--no-session-log", "--model", MUSE_TURN_MODEL, "--disable-shell", "--disable-write")
# Muse's session logs (any process that ran Muse without --no-session-log):
# the newest few days, the newest files, the tail of each.
MUSE_SESSIONS = (".local", "share", "muse", "sessions")
MUSE_LOG_DAYS = 3
MUSE_LOG_FILES = 24
_MUSE_REFUSAL = re.compile(rb"Subscription quota exhausted\. Your usage window resets at "
                           rb"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)")
# A refusal holds no figure: it stands in the row as one window at 100% that
# ends at the refusal's reset time, so routing reads Muse as limited until then.
MUSE_REFUSED_WINDOW = "refused"


def _muse_usage(usage: dict, source: str) -> dict:
    window, weekly = _dict(usage, "window"), _dict(usage, "weekly")
    mins = _mins(window.get("windowDurationMins"))
    windows = [
        _window(_window_name(mins), window.get("usedPercent"), window.get("resetsAtMs"), mins),
        _window("weekly", weekly.get("usedPercent"), weekly.get("resetsAtMs"), 10080),
    ]
    return {"ok": True, "plan": _text(usage.get("tier")), "windows": [w for w in windows if w],
            "source": source, "observed_at": _iso(usage.get("observedAtMs"))}


def _muse_version(frames: list[dict]) -> str | None:
    for frame in frames:
        if frame.get("id") == 1 and isinstance(frame.get("result"), dict):
            info = frame["result"].get("serverInfo")
            return _text(info.get("version")) if isinstance(info, dict) else None
    return None


def parse_muse(frames: list[dict]) -> dict:
    version = _muse_version(frames)
    usage = _answer(frames, 2).get("usage")
    if usage is None or usage == {}:
        # A fresh host answers {} until a Muse turn has run: truthful absence.
        return {"ok": True, "plan": None, "windows": [], "version": version, "source": "last_observed",
                "note": "no turn observed yet"}
    if not isinstance(usage, dict):
        raise ProbeError(E_BAD)
    return dict(_muse_usage(usage, "last_observed"), version=version)


def parse_muse_turn(frames: list[dict]) -> dict:
    """The figures ``usage/read`` gave after Khipu's own turn (``source``
    ``turn``), and the reset time when the turn was refused for quota
    (``limited_until``). Neither: no figures."""
    version = _muse_version(frames)
    for rid in (1, 2, 3):
        if any(f.get("id") == rid for f in frames):
            _answer(frames, rid)  # a refused request raises its own code
    refused = None
    for frame in frames:
        if frame.get("method") == "turn/completed":
            quota = _dict(_dict(_dict(frame, "params"), "error"), "quota")
            refused = _iso(quota.get("resetsAtMs")) if quota else None
    usage = _answer(frames, 4).get("usage") if any(f.get("id") == 4 for f in frames) else None
    if usage is not None and not isinstance(usage, dict):
        raise ProbeError(E_BAD)
    if usage:
        row = _muse_usage(usage, "turn")
    elif refused:
        row = {"ok": True, "plan": None, "windows": [], "source": "turn", "observed_at": _iso(_now().timestamp())}
    else:
        raise ProbeError(E_NO_FIGURES)
    if refused:
        row["limited_until"] = refused
        row["windows"] = [*row["windows"], {"name": MUSE_REFUSED_WINDOW, "used_pct": 100, "resets_at": refused}]
    return dict(row, version=version)


def _uuid7() -> str:
    """MSP command ids are UUIDv7 (Python 3.11 has no ``uuid.uuid7``)."""
    import uuid

    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms << 80) | (0x7 << 76) | (((rand >> 62) & 0xFFF) << 64) | (0b10 << 62) | (rand & ((1 << 62) - 1))
    return str(uuid.UUID(int=value))


def _muse_turn_frame(frame: dict) -> bool:
    return "id" in frame or frame.get("method") == "turn/completed"


def _muse_turn(binary: str, env: dict[str, str], deadline: float) -> dict:
    """One tiny Muse turn in its own memory-only host, then ``usage/read``."""
    def follow(frame: dict) -> list[dict] | None:
        rid = frame.get("id")
        if frame.get("error") is not None:
            return None
        if rid == 1:
            return [{"jsonrpc": "2.0", "id": 2, "method": "session/start",
                     "params": {"commandId": _uuid7(), "approvalMode": "denyUnmatched"}}]
        if rid == 2:
            session = _dict(_dict(frame, "result"), "session").get("sessionId")
            if not isinstance(session, str):
                raise ProbeError(E_BAD)
            return [{"jsonrpc": "2.0", "id": 3, "method": "turn/start",
                     "params": {"commandId": _uuid7(), "sessionId": session, "reasoningEffort": "none",
                                "input": [{"type": "text", "text": MUSE_TURN_PROMPT}]}}]
        if frame.get("method") == "turn/completed":
            return [{"jsonrpc": "2.0", "id": 4, "method": "usage/read", "params": {}}]
        return None

    run = getattr(_local, "run", None)
    if run is not None:
        run.extend(deadline)
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"clientInfo": _CLIENT_INFO, "capabilities": {"experimentalApi": True}}},
        {"jsonrpc": "2.0", "method": "initialized"},
    ]
    frames = _converse([binary, *_MUSE_TURN_ARGS], messages,
                       lambda f: f.get("id") == 4 or (f.get("id") in (1, 2, 3) and f.get("error") is not None),
                       env=env, deadline=deadline, close_stdin=False, keep=_muse_turn_frame, follow=follow)
    return parse_muse_turn(frames)


def _muse_log_refusal() -> tuple[str, str] | None:
    """(reset time, refused at) of the newest quota refusal in Muse's own
    session logs, or None. Only structured failed-run reasons can establish
    a refusal; only its fixed words and ``recorded_at`` are kept."""
    root = Path.home().joinpath(*MUSE_SESSIONS)
    try:
        days = sorted(p for p in root.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]") if p.is_dir())
        files = []
        for day in days[-MUSE_LOG_DAYS:]:
            for path in day.glob("*/session.jsonl"):
                with contextlib.suppress(OSError):
                    files.append((path.stat().st_mtime, path))
    except OSError:
        return None
    best = None
    for mtime, path in sorted(files, reverse=True)[:MUSE_LOG_FILES]:
        try:
            with open(path, "rb") as fh:
                size = os.fstat(fh.fileno()).st_size
                fh.seek(max(0, size - ROLLOUT_TAIL_BYTES))
                data = fh.read(ROLLOUT_TAIL_BYTES)
        except OSError:
            continue
        for line in data.split(b"\n"):
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            payload = record.get("payload") if isinstance(record, dict) else None
            event = payload.get("event") if isinstance(payload, dict) and payload.get("kind") == "run" else None
            if not isinstance(event, dict) or event.get("kind") != "failed":
                continue
            reason = event.get("reason")
            match = _MUSE_REFUSAL.search(reason.encode()) if isinstance(reason, str) else None
            if not match:
                continue
            value = record.get("recorded_at")
            value = value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
            # recorded_at is epoch microseconds; a line without one dates from the file.
            at = _iso(value / 1_000_000 if value and value > 100_000_000_000_000 else value) if value \
                else _iso(mtime)
            reset = _iso(match.group(1).decode())
            if reset and at and (best is None or at > best[1]):
                best = (reset, at)
    return best


def _muse_routing_limit(now: datetime) -> bool:
    """Whether Khipu's routing record holds Muse limited or paused now."""
    try:
        from khipu import routing

        view = routing._load_view() or {}
        for entry in view.get("accounts") or []:
            if not isinstance(entry, dict) or entry.get("harness") != "muse":
                continue
            until = entry.get("until")
            ends = datetime.fromisoformat(until.replace("Z", "+00:00")) if isinstance(until, str) else None
            if entry.get("state") == routing.PAUSED and (ends is None or ends > now):
                return True
            if entry.get("state") == routing.LIMITED and ends is not None and ends > now:
                return True
    except Exception:  # noqa: BLE001 - an unreadable record leaves the other guards
        return False
    return False


def _muse_limit(prev: dict | None, refusal: tuple[str, str] | None, now: datetime) -> str | None:
    """The time Muse is known to be limited until (or "now" for an open-ended
    hold), from the figures Khipu holds, its session logs or the routing record."""
    ahead = []
    for window in (prev or {}).get("windows") or []:
        used = window.get("used_pct") if isinstance(window, dict) else None
        reset = _parse_iso(window.get("resets_at")) if isinstance(window, dict) else None
        if isinstance(used, (int, float)) and not isinstance(used, bool) and used >= 100 and reset and reset > now:
            ahead.append(reset)
    if refusal and _parse_iso(refusal[0]) and _parse_iso(refusal[0]) > now:
        ahead.append(_parse_iso(refusal[0]))
    if ahead:
        return _iso(max(ahead).timestamp())
    return _iso(now.timestamp()) if _muse_routing_limit(now) else None


def _parse_iso(value) -> datetime | None:
    iso = _iso(value) if isinstance(value, str) else None
    return datetime.fromisoformat(iso.replace("Z", "+00:00")) if iso else None


def _muse_stale(prev: dict | None, now: datetime) -> bool:
    windows = [w for w in (prev or {}).get("windows") or [] if isinstance(w, dict)
               and w.get("name") != MUSE_REFUSED_WINDOW]
    at = _parse_iso((prev or {}).get("observed_at"))
    if not windows or at is None or (now - at).total_seconds() >= MUSE_REFRESH_S:
        return True
    return any((_parse_iso(w.get("resets_at")) or now) <= now for w in windows)


def _muse_carry(row: dict, prev: dict | None, refusal: tuple[str, str] | None, now: datetime) -> dict:
    """The free read's row, holding the last figures Khipu observed (with
    their own ``observed_at``) and a refusal from the session logs whose
    reset is still ahead."""
    out = dict(row)
    if prev and (prev.get("ok") or prev.get("last_good_at")) and (prev.get("windows") or []) \
            and prev.get("observed_at"):
        out.update(plan=prev.get("plan"), source=prev.get("source") or "last_observed",
                   observed_at=prev["observed_at"], note=None)
        out["windows"] = [w for w in prev["windows"] if isinstance(w, dict)
                          and not (w.get("name") == MUSE_REFUSED_WINDOW
                                   and (_parse_iso(w.get("resets_at")) or now) <= now)]
        if prev.get("refusal_observed_at"):
            out["refusal_observed_at"] = prev["refusal_observed_at"]
        if prev.get("limited_until") and (_parse_iso(prev["limited_until"]) or now) > now:
            out["limited_until"] = prev["limited_until"]
    reset = _parse_iso(refusal[0]) if refusal else None
    if reset and reset > now:
        out["windows"] = [w for w in out.get("windows") or [] if w.get("name") != MUSE_REFUSED_WINDOW]
        out["windows"].append({"name": MUSE_REFUSED_WINDOW, "used_pct": 100, "resets_at": refusal[0],
                               "observed_at": refusal[1], "source": "session_logs"})
        out["limited_until"] = refusal[0]
        out["refusal_observed_at"] = refusal[1]
        if not out.get("observed_at"):
            out.update(observed_at=refusal[1], source="last_observed", note=None)
    return out


@_guard
def probe_muse(*, timeout: float = TIMEOUT_S, turn: bool = True) -> dict:
    """Muse's ``usage/read``, then (a fresh host having observed nothing)
    the figures from one tiny turn when one is due (``_muse_turn``), else
    the last ones Khipu observed, marked ``observed_at``. ``turn=False``
    (a sign-in check) never spends one."""
    binary = _command("muse")
    if not binary:
        return _not_installed("muse")
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"clientInfo": _CLIENT_INFO, "capabilities": {"experimentalApi": True}}},
        {"jsonrpc": "2.0", "method": "initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "usage/read", "params": {}},
    ]
    env = probe_environment("muse", None, binary)
    frames = _converse([binary, "serve", "--no-session-log"], messages, lambda f: f.get("id") == 2,
                       env=env, deadline=time.monotonic() + timeout, close_stdin=True)
    row = parse_muse(frames)
    if row["windows"]:
        return row
    now = _now()
    prev = _load_cache().get(MUSE_TURN_KEY)
    refusal = _muse_log_refusal()
    if turn and "KHIPU_ISOLATED_DIR" not in os.environ \
            and _muse_limit(prev, refusal, now) is None and _muse_stale(prev, now) \
            and _turn_due(None, key=MUSE_TURN_KEY, every=MUSE_TURN_EVERY_S,
                          directory=Path.home().joinpath(*MUSE_TURN_STATE)):
        try:
            turn = _muse_turn(binary, env, time.monotonic() + MUSE_TURN_S)
            return dict(turn, version=turn.get("version") or row.get("version"))
        except ProbeError as exc:
            if exc.code == E_SIGNIN:
                raise
    return _muse_carry(row, prev, refusal, now)


# ---- GLM (z.ai Coding Plan) ------------------------------------------------
#
# Matt's GLM lane is the Claude Code CLI pointed at z.ai, so there is no GLM
# CLI to ask. z.ai's monitor endpoint answers with the key as the raw
# ``Authorization`` value (no Bearer). The key is read into a local, goes only
# into that header, and never reaches a row, the cache, a log, argv or an error.

_GLM_UNITS = {3: ("five_hour", 300), 6: ("weekly", 10080)}
# z.ai's own codes for a missing plan and for a key it does not accept.
_GLM_NO_PLAN = {1113}
_GLM_BAD_KEY = {1000, 1001, 1002, 1003, 1004, 401}


def _glm_key() -> str | None:
    from khipu import keychain

    try:
        return keychain.resolve_glm_key() or None
    except Exception:  # noqa: BLE001 - no key anywhere; the message is never used
        return None


def _glm_request(key: str, timeout: float) -> tuple[int, bytes]:
    """(HTTP status, body) of the quota call; ProbeError for anything that is
    not an HTTP answer. No exception text is kept: it could echo the URL."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(GLM_QUOTA_URL, headers={"Authorization": key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URL
            return response.status, response.read(1 << 20)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read(1 << 20)
        except Exception:  # noqa: BLE001
            return exc.code, b""
    except TimeoutError:
        raise ProbeError(E_TIMEOUT) from None
    except urllib.error.URLError as exc:
        raise ProbeError(E_TIMEOUT if isinstance(exc.reason, TimeoutError) else E_NO_ANSWER) from None
    except Exception:  # noqa: BLE001 - OSError, ssl, http.client: any text could echo the key
        raise ProbeError(E_NO_ANSWER) from None


_GLM_QUOTA_TYPES = frozenset({"TOKENS_LIMIT", "CREDIT_LIMIT"})


def _glm_pct(limit: dict, *, strict: bool = False):
    """Used percent of one limit: z.ai's own ``percentage``, else used / limit."""
    pct = limit.get("percentage")
    if pct is None:
        used, total = limit.get("currentValue"), limit.get("usage")
        if isinstance(used, bool) or isinstance(total, bool) \
                or not isinstance(used, (int, float)) or not isinstance(total, (int, float)) or total <= 0:
            return None
        pct = used / total * 100
    if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not math.isfinite(pct):
        raise ProbeError(E_BAD)
    if strict and pct < 0:
        raise ProbeError(E_BAD)
    return round(min(100.0, max(0.0, float(pct))), 2)


def parse_glm(status: int, body: bytes) -> dict:
    try:
        reply = json.loads(body.decode("utf-8")) if body else None
    except (ValueError, UnicodeError):
        reply = None
    if not isinstance(reply, dict):
        if status in (401, 403):
            raise ProbeError(E_BAD_KEY)
        raise ProbeError(E_BAD)
    code = reply.get("code")
    if isinstance(code, int) and not isinstance(code, bool) and code != 200:
        if code in _GLM_NO_PLAN:
            raise ProbeError(E_NO_PLAN)
        raise ProbeError(E_BAD_KEY if code in _GLM_BAD_KEY or status in (401, 403) else E_CLI)
    if status in (401, 403):
        raise ProbeError(E_BAD_KEY)
    if status != 200:
        raise ProbeError(E_CLI)
    data = _dict(reply, "data", required=True)
    windows = []
    for limit in _list(data, "limits"):
        if not isinstance(limit, dict):
            raise ProbeError(E_BAD)
        unit = limit.get("unit")
        if isinstance(unit, bool) or unit not in _GLM_UNITS:
            continue
        # unit 3 / 6 are the quota windows; a same-unit request-count limit
        # (type TIME_LIMIT) must not stand in for one. z.ai renamed the quota
        # type TOKENS_LIMIT to CREDIT_LIMIT in October 2026.
        if isinstance(limit.get("type"), str) and limit["type"] not in _GLM_QUOTA_TYPES:
            continue
        name, mins = _GLM_UNITS[unit]
        if any(w["name"] == name for w in windows):
            continue
        window = _window(name, _glm_pct(limit), limit.get("nextResetTime"), mins)
        if window:
            windows.append(window)
    if not windows:
        raise ProbeError(E_NO_LIMITS)
    return {"ok": True, "plan": _text(data.get("level")), "windows": windows, "credits": None, "version": None,
            "source": "live"}


@_guard
def probe_glm(*, timeout: float = TIMEOUT_S) -> dict:
    """The Coding Plan's five-hour and weekly windows from z.ai. The default
    ``timeout`` is widened to ``GLM_TIMEOUT_S`` (an explicit one is honoured)."""
    key = _glm_key()
    if not key:
        raise ProbeError(E_NO_KEY)
    status, body = _glm_request(key, GLM_TIMEOUT_S if timeout == TIMEOUT_S else timeout)
    return parse_glm(status, body)


# ---- DeepSeek (measured account spend) ---------------------------------------
#
# Balance decreases are spend; increases are top-ups and never offset spend.
# Polling measures the whole account, regardless of which conversation used it.
# Spend and a top-up within one interval can net out; retain each observed
# increase with its interval so that limitation is explicit.
#
# The key goes only into one HTTPS Authorization header (no redirects/proxies).
# Owner-only, symlink-free history and locked atomic writes remain key-bound
# by HMAC-SHA256 under a random per-history salt; the key is never persisted.

DEEPSEEK_POLL_S = 60
DEEPSEEK_HISTORY_DAYS = 366
# This also admits pre-compaction V4 histories for lossless migration. Only
# the active cap windows (plus a day) need individual reading intervals.
DEEPSEEK_HISTORY_MAX = DEEPSEEK_HISTORY_DAYS * 86400 // DEEPSEEK_POLL_S + 2
DEEPSEEK_FRESH_S = 1800
DEEPSEEK_SKEW_S = 120
# V4 intervals migrate losslessly on the next poll. V5 daily sums have an
# explicit version so an older reader cannot silently omit the folded spend.
DEEPSEEK_HISTORY_VERSION = 5
DEEPSEEK_HISTORY_BYTES = 128 << 20
DEEPSEEK_LOCK_NAME = "deepseek-balance.lock"
_DEEPSEEK_STATE_CACHE: dict = {}


def _deepseek_key() -> str | None:
    from khipu import keychain

    try:
        return keychain.resolve_deepseek_key() or None
    except Exception:  # noqa: BLE001 - no key anywhere; the message is never used
        return None


def deepseek_history_file() -> Path:
    from khipu.paths import data_dir

    return data_dir() / "state" / DEEPSEEK_HISTORY_NAME


def deepseek_accounts(definitions: dict) -> list[dict]:
    """The DeepSeek accounts of the record, once a key makes them readable.
    Khipu reads one DeepSeek key, so with more than one DeepSeek account
    defined no balance can be attributed: each is marked invalid."""
    if not _deepseek_key():
        return []
    providers = definitions.get("providers") or {}
    out = []
    for ident, value in sorted((definitions.get("accounts") or {}).items()):
        if (value or {}).get("provider") == "deepseek" and "deepseek" in providers:
            out.append({"label": value.get("label") or ident, "harness": "deepseek", "home": None,
                        "provider": "deepseek", "quota_key": ident})
    if len(out) > 1:
        for account in out:
            account["invalid"] = "Khipu reads one DeepSeek key, and more than one DeepSeek account is defined"
    return out


def _deepseek_request(key: str, timeout: float) -> tuple[int, bytes]:
    """(HTTP status, body) of the balance call. No redirect is followed and no
    proxy is used, so the key can only ever reach api.deepseek.com over TLS."""
    return _https_get(DEEPSEEK_BALANCE_URL, f"Bearer {key}", timeout)


def parse_deepseek(status: int, body: bytes) -> Decimal:
    """The USD total balance of a /user/balance answer. An account DeepSeek
    marks unavailable is a failed read, whatever balance it shows."""
    if status in (401, 403):
        raise ProbeError(E_BAD_KEY)
    if status != 200:
        raise ProbeError(E_CLI)
    try:
        reply = json.loads(body.decode("utf-8"), parse_float=Decimal) if body else None
    except (ValueError, UnicodeError):
        reply = None
    if not isinstance(reply, dict) or not isinstance(reply.get("balance_infos"), list):
        raise ProbeError(E_BAD)
    if reply.get("is_available") is not True:
        raise ProbeError(E_UNAVAILABLE)
    infos = [i for i in reply["balance_infos"] if isinstance(i, dict)]
    usd = [i for i in infos if i.get("currency") == "USD"]
    if not usd:
        raise ProbeError(E_CURRENCY if infos else E_BAD)
    if len(usd) != 1:
        raise ProbeError(E_BAD)
    raw = usd[0].get("total_balance")
    try:
        total = _ds_money(raw)
    except (TypeError, ValueError, InvalidOperation):
        raise ProbeError(E_BAD) from None
    if total > 1e9:
        raise ProbeError(E_BAD)
    return total


def _ds_money(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("invalid money")
    if len(str(value)) > 128:
        raise ValueError("invalid money")
    number = Decimal(str(value))
    # Bounded scale keeps all retained sums exact in a 100-digit context.
    if not number.is_finite() or not 0 <= number <= Decimal("1e14") or number.as_tuple().exponent < -64:
        raise ValueError("invalid money")
    return number


def _ds_store_money(value: Decimal):
    """Keep legacy JSON numbers where lossless; use decimal text otherwise."""
    number = float(value)
    return number if Decimal(str(number)) == value else str(value)


def _ds_floor_number(value: Decimal) -> float:
    number = float(value)
    return math.nextafter(number, -math.inf) if Decimal(str(number)) > value else number


def _ds_floor_display(value: Decimal) -> float:
    with localcontext() as ctx:
        ctx.prec = 100
        return float(value.quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def _deepseek_measured_total(base: dict, amount: Decimal, cap_usd) -> dict:
    cap = _ds_money(cap_usd) if cap_usd is not None else None
    verdict = "unknown"
    if cap is not None and cap > 0:
        with localcontext() as ctx:
            ctx.prec = 100
            verdict = ("over" if amount > cap else "reached" if amount == cap else
                       "near" if amount >= cap * Decimal("0.85") else "under")
    return {**base, "measured_usd": str(amount), "at_least_usd": _ds_floor_display(amount),
            "status": "partial" if base.get("partial") else "known",
            "usd": _ds_floor_number(amount), "verdict": verdict, "reason": None}


def _deepseek_fingerprint(salt_hex: str, key: str) -> str:
    import hmac

    return hmac.new(bytes.fromhex(salt_hex), key.encode("utf-8"), hashlib.sha256).hexdigest()


def _ds_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _deepseek_valid(data, now_ts: float) -> bool:
    """The whole history, or nothing: a damaged file is never repaired into
    evidence. Times may not be in the future (beyond a small clock skew)."""
    if not isinstance(data, dict) or data.get("version") not in (4, DEEPSEEK_HISTORY_VERSION):
        return False
    if (data["version"] == 4 and "daily" in data) or (data["version"] == 5 and "daily" not in data):
        return False
    salt, fp = data.get("salt"), data.get("fingerprint")
    if not (isinstance(salt, str) and re.fullmatch(r"[0-9a-f]{32}", salt)
            and isinstance(fp, str) and re.fullmatch(r"[0-9a-f]{64}", fp)):
        return False
    since, last, buckets = data.get("since"), data.get("last"), data.get("buckets")
    limit = now_ts + DEEPSEEK_SKEW_S
    if not _ds_num(since) or not isinstance(last, list) or len(last) != 2 or not _ds_num(last[0]):
        return False
    try:
        _ds_money(last[1])
    except (ValueError, TypeError, InvalidOperation):
        return False
    if not since <= last[0] <= limit:
        return False
    if not isinstance(buckets, list) or len(buckets) > DEEPSEEK_HISTORY_MAX:
        return False
    topups = data.get("topups")
    if not isinstance(topups, list) or len(topups) > DEEPSEEK_HISTORY_MAX:
        return False
    for records in (buckets, topups):
        previous_end = None
        for record in records:
            if not isinstance(record, list) or len(record) != 3 or not all(map(_ds_num, record[:2])):
                return False
            start, end, amount = record
            try:
                if _ds_money(amount) <= 0:
                    return False
            except (ValueError, TypeError, InvalidOperation):
                return False
            if not (start < end <= last[0]) or (previous_end is not None and start < previous_end):
                return False
            previous_end = end
    daily = data.get("daily", [])
    if not isinstance(daily, list) or len(daily) > DEEPSEEK_HISTORY_DAYS + 1:
        return False
    previous_end = None
    for record in daily:
        if not isinstance(record, list) or len(record) != 6:
            return False
        start, end, spent, count, increase, seconds = record
        if (not _ds_num(start) or not _ds_num(end) or not start < end <= last[0]
                or (previous_end is not None and start < previous_end)
                or not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= DEEPSEEK_HISTORY_MAX
                or not _ds_num(seconds) or not 0 <= seconds <= end - start):
            return False
        try:
            debit, credit = _ds_money(spent), _ds_money(increase)
            if ((count == 0 and (credit != 0 or seconds != 0))
                    or (count > 0 and (credit <= 0 or seconds <= 0))
                    or (debit == 0 and count == 0)):
                return False
        except (ValueError, TypeError, InvalidOperation):
            return False
        if previous_end is not None and int(end // 86400) <= int(previous_end // 86400):
            return False
        previous_end = end
    # A single reading cannot be both a decrease and an increase. Check the
    # combined order too, so damaged cross-list intervals cannot invent spend.
    previous_end = None
    for record in heapq.merge(buckets, topups, daily):
        start, end = record[:2]
        if previous_end is not None and start < previous_end:
            return False
        previous_end = end
    if daily and any(records and records[0][0] < daily[-1][1] for records in (buckets, topups)):
        return False
    attempt = data.get("attempt")
    if attempt is not None:
        if not isinstance(attempt, dict) or not _ds_num(attempt.get("at")) or attempt["at"] > limit:
            return False
        if not isinstance(attempt.get("ok"), bool) or not (attempt.get("error") is None
                                                          or isinstance(attempt.get("error"), str)):
            return False
    return True


def _deepseek_dir(create: bool) -> int | None:
    """A descriptor on the history's folder. The folder itself is opened
    without following a symlink and must be a directory this user owns, so
    a planted link cannot move the history (or its lock) anywhere else."""
    from khipu.paths import open_trusted_directory

    try:
        fd = open_trusted_directory(deepseek_history_file().parent, create=create)
    except OSError:
        return None
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        os.close(fd)
        return None
    return fd


def _deepseek_read(dir_fd: int, now_ts: float) -> tuple[str, dict | None]:
    """("missing" | "damaged" | "ok", history). Not a regular file this user
    owns with no group or other access, or not valid: damaged."""
    try:
        fd = os.open(DEEPSEEK_HISTORY_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "damaged", None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > DEEPSEEK_HISTORY_BYTES):
            return "damaged", None
        with os.fdopen(os.dup(fd), "rb") as fh:
            raw = fh.read(DEEPSEEK_HISTORY_BYTES + 1)
    except OSError:
        return "damaged", None
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"))
        return ("ok", data) if _deepseek_valid(data, now_ts) else ("damaged", None)
    except (ValueError, UnicodeError, OverflowError, RecursionError):
        return "damaged", None


def _deepseek_write(dir_fd: int, data: dict) -> None:
    """Atomic, 0600, inside the descriptor's folder only. A failed write
    costs the history, never the answer."""
    tmp = f".{DEEPSEEK_HISTORY_NAME}.{os.getpid()}.{os.urandom(6).hex()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    except OSError:
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, DEEPSEEK_HISTORY_NAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)


@contextlib.contextmanager
def _deepseek_locked():
    """The history folder's descriptor, holding the history lock: every
    read-merge-write of the history runs under it, across processes."""
    dir_fd = _deepseek_dir(create=True)
    if dir_fd is None:
        yield None
        return
    lock_fd = None
    try:
        try:
            lock_fd = os.open(DEEPSEEK_LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                              0o600, dir_fd=dir_fd)
            info = os.fstat(lock_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise OSError("untrusted DeepSeek history lock")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except OSError:
            yield None
            return
        yield dir_fd
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(dir_fd)


def _deepseek_new(key: str, at: float, balance: float) -> dict:
    salt = os.urandom(16).hex()
    return {"version": DEEPSEEK_HISTORY_VERSION, "salt": salt, "fingerprint": _deepseek_fingerprint(salt, key),
            "since": at, "last": [at, _ds_store_money(_ds_money(balance))], "buckets": [], "topups": [], "daily": [],
            "attempt": {"at": at, "ok": True, "error": None}}


def _deepseek_cap_days() -> list[float]:
    """The saved local routing record's DeepSeek caps; no refold or IO
    beyond that file. Seven days also serves the usage report's own window."""
    from khipu import routing

    try:
        view = routing._load_view() or {}
        days = [7.0]
        for entry in (view.get("accounts") or []):
            if entry.get("harness") != "deepseek":
                continue
            for cap in entry.get("caps") or []:
                value = cap.get("days")
                if cap.get("kind") == "spend" and _ds_num(value) and 0 < value <= DEEPSEEK_HISTORY_DAYS:
                    days.append(value)
        return sorted(set(days))
    except Exception:  # noqa: BLE001 - retain precision when the local record cannot be read
        return [7.0, float(DEEPSEEK_HISTORY_DAYS)]


def _deepseek_precision_days() -> float:
    return max(_deepseek_cap_days())


def _deepseek_compact(data: dict, at: float, *, precise_days: float = 7) -> None:
    """Fold only intervals older than the longest cap plus one day. Daily
    rows keep independent exact decreases, top-up counts/increases and gap
    seconds; no net balance and no synthetic top-up interval enters spend."""
    cutoff = at - DEEPSEEK_HISTORY_DAYS * 86400
    fine_cutoff = at - (max(7, precise_days) + 1) * 86400
    data["version"] = DEEPSEEK_HISTORY_VERSION
    data["since"] = max(data["since"], cutoff)
    daily = data.setdefault("daily", [])
    remove = bisect_right(daily, cutoff, key=lambda r: r[1])
    if remove:
        data["since"] = max(data["since"], daily[remove - 1][1])
        del daily[:remove]
    retired = []
    for records, is_topup in ((data["buckets"], False), (data["topups"], True)):
        remove = max(bisect_right(records, cutoff, key=lambda r: r[1]),
                     len(records) - DEEPSEEK_HISTORY_MAX)
        if remove > 0:
            data["since"] = max(data["since"], records[remove - 1][1])
            del records[:remove]
        fold = bisect_right(records, fine_cutoff, key=lambda r: r[1])
        retired.extend((r, is_topup) for r in records[:fold])
        del records[:fold]
    with localcontext() as ctx:
        ctx.prec = 100
        for (start, end, amount), is_topup in sorted(retired, key=lambda item: item[0][0]):
            if not daily or int(daily[-1][1] // 86400) != int(end // 86400):
                daily.append([start, end, 0, 0, 0, 0])
            row = daily[-1]
            row[1] = end
            column = 4 if is_topup else 2
            row[column] = _ds_store_money(_ds_money(row[column]) + _ds_money(amount))
            if is_topup:
                row[3] += 1
                row[5] += end - start


def _deepseek_add(data: dict, at: float, balance: float, *, precise_days: float = 7) -> bool:
    """Record a consecutive reading, discarding late or same-time replies.
    Each positive decrease is attributed to the reading that measures it.
    An increase records its own interval and never subtracts from spend."""
    last_at, last_balance = data["last"]
    if at <= last_at:
        return False
    with localcontext() as ctx:
        ctx.prec = 100
        change = _ds_money(last_balance) - _ds_money(balance)
        if change:
            records = data["buckets" if change > 0 else "topups"]
            records.append([last_at, at, _ds_store_money(abs(change))])
    data["last"] = [at, _ds_store_money(_ds_money(balance))]
    if at >= (data.get("attempt") or {}).get("at", last_at):
        data["attempt"] = {"at": at, "ok": True, "error": None}
    _deepseek_compact(data, at, precise_days=precise_days)
    return True


def _deepseek_window(data: dict, start: float, end: float) -> tuple[Decimal, dict, float | None]:
    """Cache computed sums by interval indices, so advancing prompt times
    still expire individual intervals at the exact rolling boundary."""
    buckets, topups, daily = data["buckets"], data["topups"], data.get("daily", [])
    b0 = bisect_right(buckets, start, key=lambda r: r[1])
    b1 = bisect_right(buckets, end, key=lambda r: r[1])
    t0 = bisect_right(topups, start, key=lambda r: r[1])
    t1 = bisect_right(topups, end, key=lambda r: r[1])
    d0 = bisect_left(daily, start, key=lambda r: r[0])
    d1 = bisect_right(daily, end, key=lambda r: r[1])
    # A boundary inside a folded day lacks individual intervals. Never
    # attribute the whole day to it or claim complete coverage for it.
    partial_since = None
    if d0 and daily[d0 - 1][1] > start:
        partial_since = daily[d0 - 1][1]
    if d1 < len(daily) and daily[d1][0] < end:
        partial_since = max(partial_since or start, daily[d1][1])
    cache = _DEEPSEEK_STATE_CACHE.get("windows") if _DEEPSEEK_STATE_CACHE.get("data") is data else None
    indices = (b0, b1, t0, t1, d0, d1, partial_since)
    if cache is not None and indices in cache:
        return cache[indices]
    with localcontext() as ctx:
        ctx.prec = 100
        amount = sum((_ds_money(r[2]) for r in buckets[b0:b1]), Decimal(0))
        amount += sum((_ds_money(r[2]) for r in daily[d0:d1]), Decimal(0))
    intervals = [{"from": _iso(s), "to": _iso(e), "increase_usd": str(_ds_money(value)), "seconds": e - s}
                 for s, e, value in topups[t0:t1]]
    increases = {"topups_seen": len(intervals) + sum(r[3] for r in daily[d0:d1]),
                 "topup_intervals": intervals,
                 "possible_miss_seconds": sum(i["seconds"] for i in intervals) + sum(r[5] for r in daily[d0:d1])}
    out = (amount, increases, partial_since)
    if cache is not None:
        if len(cache) >= 32:
            cache.clear()
        cache[indices] = out
    return out


def deepseek_spend(data: dict, start: float, end: float) -> Decimal:
    """Exact decreases in (start, end]; folded days contribute only when
    wholly enclosed. Active cap windows retain individual intervals."""
    return _deepseek_window(data, start, end)[0]


def _deepseek_topups(data: dict, start: float, end: float) -> dict:
    return copy.deepcopy(_deepseek_window(data, start, end)[1])


def _deepseek_identity(dir_fd: int) -> tuple:
    """Metadata only, through the same trusted directory as the reader.
    Permission/owner changes and atomic replacements invalidate the cache."""
    parent = os.fstat(dir_fd)
    info = os.stat(DEEPSEEK_HISTORY_NAME, dir_fd=dir_fd, follow_symlinks=False)
    return (parent.st_dev, parent.st_ino, info.st_dev, info.st_ino, info.st_mtime_ns,
            info.st_ctime_ns, info.st_size, info.st_mode, info.st_uid)


def _deepseek_cached_history(now: datetime) -> tuple[str, dict | None]:
    """Validated local data only; key binding is checked by each consumer."""
    dir_fd = _deepseek_dir(create=False)
    if dir_fd is None:
        return "missing", None
    try:
        identity = _deepseek_identity(dir_fd)
        cached = _DEEPSEEK_STATE_CACHE
        data = cached.get("data")
        hit = (cached.get("identity") == identity and isinstance(data, dict)
               and isinstance(cached.get("windows"), dict))
        if hit:
            try:
                # A clock rollback must not bypass the reader's future-time check.
                limit = now.timestamp() + DEEPSEEK_SKEW_S
                future = data["last"][0] > limit or (data.get("attempt") or {}).get("at", 0) > limit
            except (KeyError, TypeError, IndexError):
                hit = False  # damaged/missing process cache: reread the trusted file
        if hit:
            status, data = ("damaged", None) if future else ("ok", data)
        else:
            cached.clear()
            status, data = _deepseek_read(dir_fd, now.timestamp())
            if status == "ok" and identity == _deepseek_identity(dir_fd):
                cached.update(identity=identity, data=data, windows={})
    except FileNotFoundError:
        status, data = "missing", None
    except OSError:
        status, data = "damaged", None
    finally:
        os.close(dir_fd)
    return status, data


def _deepseek_prime_cache() -> None:
    """Warm the persistent forkserver, so each prompt child inherits the
    same validated history and configured cap sums. No keys, network or writes."""
    try:
        now = _now()
        status, data = _deepseek_cached_history(now)
        if status == "ok":
            for days in _deepseek_cap_days():
                _deepseek_window(data, (now - timedelta(days=days)).timestamp(), now.timestamp())
    except Exception:  # noqa: BLE001 - a cache miss never prevents a prompt fork
        _DEEPSEEK_STATE_CACHE.clear()


def _deepseek_state(now: datetime) -> tuple[str, dict | None]:
    """("ok", history) when the stored history speaks for the current key;
    otherwise (why not, None)."""
    key = _deepseek_key()
    if not key:
        return "Khipu has no DeepSeek key, so no balance history speaks for an account", None
    status, data = _deepseek_cached_history(now)
    if status == "missing":
        return "no DeepSeek balance read yet", None
    if status != "ok":
        return "DeepSeek's balance history is damaged; Khipu starts it again at the next read", None
    if not _hmac_equal(data["fingerprint"], _deepseek_fingerprint(data["salt"], key)):
        return "DeepSeek's key changed since its balance history began; Khipu starts it again at the next read", None
    return "ok", data


def _hmac_equal(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a, b)


def deepseek_spend_total(now: datetime, days: float, cap_usd: float | None = None, *,
                         definitions: dict | None = None) -> dict:
    """Measured account spend over the rolling cap window, independent of
    conversation setup. Full coverage is known; filling history is partial
    but still judged against the cap as measured. A top-up does not make
    either unknown: its interval bounds where spend might have netted out.
    Failed/stale/unavailable reads and untrusted or missing history are
    unknown, even when previously measured spend reached the cap."""
    start = now - timedelta(days=days)
    base = {"from": _iso(start.timestamp()), "to": _iso(now.timestamp()), "events": None,
            "at_least_usd": None, "partial": True, "since": None,
            "topups_seen": None, "topup_intervals": [], "possible_miss_seconds": 0}
    try:
        if definitions is not None and sum(
                1 for v in (definitions.get("accounts") or {}).values()
                if isinstance(v, dict) and v.get("provider") == "deepseek") > 1:
            return {**base, "status": "unknown", "usd": None,
                    "reason": "more than one DeepSeek account is defined, and Khipu reads one DeepSeek key"}
        state, data = _deepseek_state(now)
        if state != "ok":
            return {**base, "status": "unknown", "usd": None, "reason": state}
        amount, topups, partial_since = _deepseek_window(data, start.timestamp(), now.timestamp())
        since = max(data["since"], partial_since or data["since"])
        partial = since > start.timestamp()
        out = _deepseek_measured_total(
            {**base, "partial": partial, "since": _iso(since), **copy.deepcopy(topups)}, amount, cap_usd)
        attempt = data.get("attempt") or {}
        if attempt.get("ok") is False:
            reason = "DeepSeek's last balance read failed"
        elif now.timestamp() - data["last"][0] > DEEPSEEK_FRESH_S:
            reason = "DeepSeek's balance has not been read in the last 30 minutes"
        else:
            return out
        return {**out, "status": "unknown", "usd": None, "verdict": "unknown", "reason": reason}
    except Exception:  # noqa: BLE001 - a broken history is unknown, never a crashed route
        return {**base, "status": "unknown", "usd": None, "reason": "DeepSeek's balance history can't be read"}


def _deepseek_record_failure(key: str, at: float, code: str) -> None:
    """Note a failed read on a history this key owns, so routing and the
    owner see the failure; no figure changes."""
    with _deepseek_locked() as dir_fd, _probe_commit():
        if dir_fd is None:
            return
        status, data = _deepseek_read(dir_fd, at)
        if status != "ok" or not _hmac_equal(data["fingerprint"], _deepseek_fingerprint(data["salt"], key)):
            return
        if at < max(data["last"][0], (data.get("attempt") or {}).get("at", 0)):
            return
        data["attempt"] = {"at": at, "ok": False, "error": code}
        _deepseek_write(dir_fd, data)


@_guard
def probe_deepseek(*, timeout: float = TIMEOUT_S) -> dict:
    """DeepSeek's balance and measured seven-day account spend. The shared
    usage poll makes DeepSeek due every 60 seconds; the daemon normally
    looks every 10 seconds (at most 60 while DeepSeek is tracked), bounding
    normal top-up intervals to two minutes. Sleep, failures and blocked
    refreshes can delay a read; each recorded top-up reports its actual
    interval rather than claiming the target cadence was achieved.
    The routing record owns the cap. History is partial until seven days
    are covered. A failed read stores no sample."""
    key = _deepseek_key()
    if not key:
        raise ProbeError(E_NO_KEY)
    # The reading's time is taken before the request: a slower concurrent
    # poll then sorts before a faster later one and is dropped, not merged.
    at = _now().timestamp()
    try:
        try:
            status, body = _deepseek_request(key, GLM_TIMEOUT_S if timeout == TIMEOUT_S else timeout)
            balance = parse_deepseek(status, body)
        except ProbeError as exc:
            _deepseek_record_failure(key, at, exc.code)
            raise
        with _deepseek_locked() as dir_fd, _probe_commit():
            data = None
            if dir_fd is not None:
                state, data = _deepseek_read(dir_fd, max(at, _now().timestamp()))
                if state != "ok" or not _hmac_equal(data["fingerprint"],
                                                    _deepseek_fingerprint(data["salt"], key)):
                    data = _deepseek_new(key, at, balance)
                    _deepseek_write(dir_fd, data)
                elif _deepseek_add(data, at, balance, precise_days=_deepseek_precision_days()):
                    _deepseek_write(dir_fd, data)
            if data is None:
                data = _deepseek_new(key, at, balance)
    finally:
        key = None  # the key lives only for the request above
    start = at - 7 * 86400
    spend = {"balance_usd": float(data["last"][1]),
             "last_7d_usd": _ds_floor_number(deepseek_spend(data, start, at)),
             "last_7d_at_least_usd": _ds_floor_display(deepseek_spend(data, start, at)),
             **_deepseek_topups(data, start, at),
             "partial": data["since"] > start, "since": _iso(data["since"])}
    return {"ok": True, "plan": None, "windows": [], "credits": None, "version": None,
            "source": "live", "spend": spend}


# ---- http-quota (owner-confirmed HTTPS quota endpoints, slice 9b) --------------
#
# The key floor (plan section 6): HTTPS only, to the exact host the owner
# confirmed on the provider fact. Redirects are refused, never followed. The
# key is read through ``keychain`` into a local at request time and goes only
# into the request header to that host: never argv, env, a URL or a file.
# Only the parsed quota numbers leave the parser; no response body, header
# or exception text is returned, cached or logged anywhere.

_HTTP_HOST = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?\Z")


def _find_socket(obj):
    """The socket under a connection or response, or None."""
    import socket as socket_module

    for _ in range(8):
        if obj is None:
            return None
        if isinstance(obj, socket_module.socket):
            return obj
        obj = getattr(obj, "fp", None) or getattr(obj, "raw", None) or getattr(obj, "_sock", None)
    return None


def _shrink_read_timeout(response, remaining: float) -> None:
    """Best-effort: give the socket under ``response`` only ``remaining``
    seconds, so one slow read cannot outlive the overall deadline."""
    sock = _find_socket(response)
    if sock is not None:
        try:
            sock.settimeout(max(0.0, remaining))
        except OSError:
            return


def _http_quota_request(key: str, host: str, timeout: float) -> tuple[int, bytes]:
    """(HTTP status, bounded body on 200 only) of the quota call. A redirect
    is a ProbeError, and no error body is ever read. Proxies are disabled, so
    only the owner-confirmed host is ever contacted, and ``timeout`` is an
    overall deadline (connect through status line, headers and body read),
    not a per-read budget."""
    # P3-4: the key goes out as a bare ``Authorization: <key>`` header value,
    # with no scheme (no ``Bearer`` prefix) — the form the z.ai-style quota
    # APIs this adapter targets accept, same as the GLM probe above.
    return _https_get(f"https://{host}/", key, timeout)


def _https_get(url: str, authorization: str, timeout: float) -> tuple[int, bytes]:
    """One HTTPS GET to exactly ``url`` with ``authorization`` as its
    Authorization header: no redirect is followed (a 3xx is E_REDIRECT), no
    proxy is used, no error body is read, and ``timeout`` is one overall
    deadline."""
    import http.client
    import socket as socket_module
    import urllib.error
    import urllib.request

    if not url.startswith("https://"):
        raise ProbeError(E_BAD)
    request = urllib.request.Request(url, headers={"Authorization": authorization,
                                                   "Accept": "application/json"})
    deadline = time.monotonic() + timeout

    # One watchdog deadline covers connect, TLS, the status line, the
    # response headers and the body. A per-read socket timeout alone cannot
    # do that: each dripped header line would earn a fresh budget. The
    # watchdog tracks every socket the request creates and closes it at the
    # deadline, repeatedly until the request is done, so a socket created
    # just as the deadline fires is closed too. It never holds the key.
    fired = threading.Event()
    done = threading.Event()
    connections: list = []
    response_sockets: list = []

    def _close_socket(sock) -> None:
        if sock is None:
            return
        try:
            sock.shutdown(socket_module.SHUT_RDWR)
        except Exception:  # noqa: BLE001 - already closed or not connected
            pass
        try:
            sock.close()
        except Exception:  # noqa: BLE001 - closing is best-effort
            pass

    def _watchdog() -> None:
        if done.wait(max(0.0, deadline - time.monotonic())):
            return
        fired.set()
        while not done.is_set():
            for conn in list(connections):
                _close_socket(getattr(conn, "sock", None))
            for sock in list(response_sockets):
                _close_socket(sock)
            done.wait(0.05)

    class _DeadlineConnection(http.client.HTTPSConnection):
        def __init__(self, *args, **kwargs):
            connections.append(self)
            super().__init__(*args, **kwargs)

    class _DeadlineHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(_DeadlineConnection, req,
                                context=self._context, check_hostname=self._check_hostname)

    watchdog = threading.Thread(target=_watchdog, daemon=True)
    watchdog.start()
    try:
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):  # noqa: ARG002 - refusing is the whole job
                return None

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                             _DeadlineHTTPSHandler())
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeError(E_TIMEOUT)
        response = opener.open(request, timeout=remaining)  # noqa: S310 - owner-confirmed https host
        sock = _find_socket(response)
        if sock is not None:
            response_sockets.append(sock)
        with response:
            if fired.is_set() or time.monotonic() >= deadline:
                raise ProbeError(E_TIMEOUT)
            status = getattr(response, "status", None)
            if not isinstance(status, int) or isinstance(status, bool):
                status = getattr(response, "code", None)
            if isinstance(status, int) and 300 <= status < 400:
                raise ProbeError(E_REDIRECT)
            if status != 200:
                return (status if isinstance(status, int) else 0), b""
            chunks, total = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if fired.is_set() or remaining <= 0:
                    raise ProbeError(E_TIMEOUT)
                _shrink_read_timeout(response, remaining)
                # read1 (one raw read) over read: read would wait to fill the
                # whole count, so a slow drip would never reach the deadline
                # check between chunks.
                read_chunk = getattr(response, "read1", response.read)
                chunk = read_chunk(min(65536, HTTP_QUOTA_MAX_BYTES + 1 - total))
                if fired.is_set() or time.monotonic() >= deadline:
                    raise ProbeError(E_TIMEOUT)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > HTTP_QUOTA_MAX_BYTES:
                    raise ProbeError(E_BAD)
                if time.monotonic() >= deadline:
                    raise ProbeError(E_TIMEOUT)
            return 200, b"".join(chunks)
    except ProbeError:
        raise
    except urllib.error.HTTPError as exc:
        if fired.is_set():
            raise ProbeError(E_TIMEOUT) from None
        # With redirects refused, urllib raises for a 3xx; its body (which a
        # hostile endpoint could fill with echoed material) is never read.
        if 300 <= exc.code < 400:
            raise ProbeError(E_REDIRECT) from None
        return exc.code, b""
    except TimeoutError:
        raise ProbeError(E_TIMEOUT) from None
    except urllib.error.URLError as exc:
        if fired.is_set():
            raise ProbeError(E_TIMEOUT) from None
        raise ProbeError(E_TIMEOUT if isinstance(exc.reason, TimeoutError) else E_NO_ANSWER) from None
    except Exception:  # noqa: BLE001 - any text could echo the key or the body
        if fired.is_set():
            raise ProbeError(E_TIMEOUT) from None
        raise ProbeError(E_NO_ANSWER) from None
    finally:
        done.set()


def parse_http_quota(status: int, body: bytes) -> dict:
    """Only quota numbers out of a quota reply: window figures and resets.
    Names are Khipu's own, derived from each window's length, never copied."""
    if 300 <= status < 400:
        raise ProbeError(E_REDIRECT)
    if status in (401, 403):
        raise ProbeError(E_BAD_KEY)
    if status != 200:
        raise ProbeError(E_CLI)
    try:
        reply = json.loads(body.decode("utf-8")) if body else None
    except (ValueError, UnicodeError, RecursionError):
        reply = None
    if not isinstance(reply, dict):
        raise ProbeError(E_BAD)
    code = reply.get("code")
    if isinstance(code, int) and not isinstance(code, bool) and code != 200:
        if code in _GLM_NO_PLAN:
            raise ProbeError(E_NO_PLAN)
        raise ProbeError(E_BAD_KEY if code in _GLM_BAD_KEY else E_CLI)
    data = reply.get("data") if isinstance(reply.get("data"), dict) else reply
    raw_windows = data.get("windows") if isinstance(data.get("windows"), list) else None
    windows = []
    if raw_windows is not None:
        for i, item in enumerate(raw_windows):
            if not isinstance(item, dict):
                raise ProbeError(E_BAD)
            used = item.get("used_pct", item.get("percentage", item.get("used_percent")))
            mins = _mins(item.get("window_mins", item.get("window_minutes")))
            name = _window_name(mins) if mins else ("quota" if i == 0 else f"quota_{i + 1}")
            resets = item.get("resets_at", item.get("reset_at", item.get("nextResetTime")))
            window = _window(name, _glm_pct(item, strict=True) if used is None and ("currentValue" in item or "usage" in item)
                             else used, resets, mins)
            if window:
                windows.append(window)
    else:
        for limit in _list(data, "limits"):
            if not isinstance(limit, dict):
                raise ProbeError(E_BAD)
            unit = limit.get("unit")
            if isinstance(unit, bool) or unit not in _GLM_UNITS:
                continue
            if isinstance(limit.get("type"), str) and limit["type"] not in _GLM_QUOTA_TYPES:
                continue
            name, mins = _GLM_UNITS[unit]
            if any(w["name"] == name for w in windows):
                continue
            window = _window(name, _glm_pct(limit, strict=True), limit.get("nextResetTime"), mins)
            if window:
                windows.append(window)
        if not windows and ("used_pct" in data or "percentage" in data):
            used = data.get("used_pct", data.get("percentage"))
            window = _window("quota", used, data.get("resets_at", data.get("reset_at")))
            if window:
                windows.append(window)
    if not windows:
        raise ProbeError(E_NO_LIMITS)
    # This adapter alone tightens the shared window shape: a figure outside
    # 0-100% or a window longer than a year is a hostile reply, not data.
    # Other adapters keep _window's finiteness-only behaviour.
    for window in windows:
        if not 0 <= window["used_pct"] <= 100 or window.get("window_mins", 0) > HTTP_QUOTA_MAX_WINDOW_MINS:
            raise ProbeError(E_BAD)
    return {"ok": True, "plan": None, "windows": windows, "credits": None, "version": None, "source": "live"}


@_guard
def probe_http_quota(account: dict, *, timeout: float = TIMEOUT_S) -> dict:
    """One http-quota provider account, read at its owner-confirmed host."""
    host = account.get("host")
    if not isinstance(host, str) or not _HTTP_HOST.fullmatch(host) or ".." in host:
        raise ProbeError(E_INVALID)
    from khipu import keychain

    provider = account.get("provider") or account.get("harness") or ""
    try:
        key = keychain.get_password(f"{provider}_api_key")
    except Exception:  # noqa: BLE001 - no key anywhere; the message is never used
        key = None
    if not key:
        raise ProbeError(E_NO_KEY)
    try:
        status, body = _http_quota_request(key, host,
                                           HTTP_QUOTA_TIMEOUT_S if timeout == TIMEOUT_S else timeout)
    finally:
        key = None  # the key lives only for the request above
    return parse_http_quota(status, body)


def http_quota_accounts() -> list[dict]:
    """The accounts whose provider fact names the http-quota adapter, keyed
    in the usage cache by their stable account id."""
    try:
        from khipu import routing

        definitions, _cache = routing._entry_definitions()
    except Exception:  # noqa: BLE001 - no record, no quota accounts
        return []
    providers = definitions.get("providers") or {}
    out = []
    for ident, value in sorted((definitions.get("accounts") or {}).items()):
        provider = providers.get((value or {}).get("provider") or "")
        if not provider or provider.get("probe") != "http-quota" or provider.get("id") == "deepseek":
            continue
        out.append({"label": value.get("label") or ident, "harness": provider["id"], "home": None,
                    "probe": "http-quota", "host": provider.get("host"), "provider": provider["id"],
                    "quota_key": ident})
    return out + deepseek_accounts(definitions)


# ---- accounts ----------------------------------------------------------------


ACCOUNTS_MAX = 32
LABEL_MAX = 64
HOME_MAX = 1024
_ACCOUNT_FIELDS = ("label", "harness", "home", "label_source")


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
                                          "Grok Build, Muse and the GLM Coding Plan")
    if harness in NO_FOLDER and home:
        return (f"{harness}_folder", "home",
                f"{LABELS[harness]} has one sign-in, so it takes no login folder")
    path = _home_path(home) if home else None
    # A relative folder (".claude-second", the "~/" left off) can never be
    # read: each probe starts in a fresh empty temp folder, so it would always
    # find an empty, signed-out login.
    if home and (path is None or not path.is_absolute()):
        return "no_such_folder", "home", (f"Khipu cannot find the login folder {home}; start it with ~/ "
                                          "(for example ~/.codex-work) or give the full path")
    if harness == "claude" and path is not None:
        from khipu import discover

        if discover.provider_home(path) == "glm":
            return "zai_home", "home", ("This login folder runs Claude Code against z.ai, so it is the GLM "
                                        "account; track GLM instead of reading it as Claude")
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
    if entry.get("label_source") in ("owner", "suggested"):
        account["label_source"] = entry["label_source"]
    problem = _entry_problem(harness, home)
    if problem:
        account["invalid"] = problem[2]
    return account


def available(harness: str) -> bool:
    """Whether Khipu can read this harness by default: its CLI is installed,
    or (GLM, which has no CLI) a key is stored."""
    if harness == "deepseek":
        return bool(_deepseek_key())
    return bool(_glm_key()) if harness == "glm" else bool(_command(harness))


def reads_none(config: dict | None = None) -> bool:
    """The owner's "Read no accounts" (``usage_accounts_none``, settings
    clarity decision 14): stored apart from an unset list, which reads every
    installed CLI's default login instead."""
    if config is None:
        from khipu.config import load_config

        config = load_config()
    raw = config.get("usage_accounts")
    return config.get("usage_accounts_none") is True and not (isinstance(raw, list) and raw)


def _cursor_entry(entry) -> bool:
    return isinstance(entry, dict) and str(entry.get("harness") or "").strip().lower() == "cursor"


def configured_accounts(config: dict | None = None) -> list[dict]:
    """Config ``usage_accounts`` (``[{label, harness, home?}]``), or by default
    every installed CLI with its default home, or none after the owner chose
    "Read no accounts". Invalid entries come back with an ``invalid`` reason
    so the report shows them instead of hiding them. ``config`` is a copy the
    caller already read, so one call never sees two versions of the list."""
    if config is None:
        from khipu.config import load_config

        config = load_config()
    raw = config.get("usage_accounts")
    if not isinstance(raw, list):
        if reads_none(config):
            return []
        return [{"label": LABELS[h], "harness": h, "home": None} for h in HARNESSES if available(h)]
    return [_account_from_entry(entry) for entry in raw if not _cursor_entry(entry)]


def default_login(harness: str) -> Path | None:
    """The CLI's own default login folder (``~/.claude``, ``~/.codex``,
    ``~/.grok``); None for Muse, which has one sign-in and no folder."""
    if harness not in _HOME_VARS:
        return None
    raw = os.environ.get("HOME")
    return (Path(raw) if raw else Path.home()) / f".{harness}"


def login_identity(account: dict) -> str:
    """The real folder an account reads, as ``claude_homes.path_identity``
    gives it (the inode when the folder exists, else the resolved path), so
    a symlink, a trailing slash, ``..`` or ``~`` against the full path all
    name one login. A home-less account stands for its CLI's default folder;
    Muse is one login."""
    from khipu import claude_homes

    harness = account.get("harness")
    if harness in NO_FOLDER:
        return f"{harness}|"
    home = account.get("home")
    path = _home_path(home) if home else default_login(harness)
    if path is None:
        return f"{harness}|{home}"
    ident = claude_homes.path_identity(path)
    return f"{harness}|" + ":".join(str(part) for part in ident)


# Names an account may not take (settings clarity, section 5): a CLI's bare
# name belongs to its default login, whose bare word conversations use.
NAME_CODES = {"name_has_bar": "a name can't hold |", "name_is_cli": "that name is a CLI's own name",
              "name_is_id": "that name is another account's id"}


def _name_problem(label: str, harness: str, home: str | None, ids: set[str]) -> tuple[str, str] | None:
    if "|" in label:
        return "name_has_bar", f"the name {label!r} holds |, which Khipu uses between a CLI and its folder"
    word = label.casefold()
    if word in HARNESSES and not (word == harness and not home):
        return "name_is_cli", (f"{label!r} is the name of a CLI; only that CLI's own default login is called "
                               f"{LABELS.get(word, word)}")
    if label.casefold() in {i.casefold() for i in ids}:
        return "name_is_id", f"{label!r} is another account's id"
    return None


def account_id_of(account: dict) -> str:
    from khipu.routing import account_id

    return account_id(_key(account))


def _account_ids(entries: list) -> set[str]:
    """The ids ``khipu_route`` gives these entries' accounts (a name may not
    equal one, or a conversation naming the id would reach the wrong one)."""
    out = set()
    for entry in entries:
        if isinstance(entry, dict):
            account = _account_from_entry(entry)
            if not account.get("invalid"):
                out.add(account_id_of(account))
    return out


def _same_entry(a, b) -> bool:
    def norm(e):
        return {k: (e.get(k).strip() if isinstance(e.get(k), str) else e.get(k)) for k in _ACCOUNT_FIELDS
                if e.get(k) not in (None, "")}
    return isinstance(a, dict) and isinstance(b, dict) and norm(a) == norm(b)


def parse_usage_accounts(raw, *, previous: list | None = None) -> list[dict]:
    """``raw`` (JSON text or a list) as the ``usage_accounts`` entries to store,
    or ``AccountsError`` (a ValueError) naming the first problem with a fixed
    code. Stricter than ``configured_accounts``, which shows a hand-edited bad
    entry rather than refusing it: ``khipu config --set usage_accounts``
    stores only entries that can be probed, each real login folder once, each
    name once (ignoring case), names by the naming rules (``NAME_CODES``).

    ``previous`` is the list stored now: an entry kept exactly as it is stored
    there is kept even when it breaks a rule (a hand-edited relative folder,
    an unknown CLI, an older name), so no save drops an entry the owner has
    not touched. Only new or changed entries are refused."""
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
    kept = list(previous or [])
    from khipu.routing_identity import STABLE

    # Only possible id names need the existing accounts' identities checked.
    ids = _account_ids(raw) if any(isinstance(e, dict)
                                   and (str(e.get("label") or "").strip().casefold().startswith("acct-")
                                        or STABLE.fullmatch(str(e.get("label") or "").strip().casefold()))
                                   for e in raw) else set()
    out: list[dict] = []
    # (account, kept) per entry: every entry is checked against every other
    # for its name and its real folder after the per-entry rules, kept ones
    # too, so a rename, a move or a repeated entry cannot reach a later one.
    checked: list[tuple[dict, bool]] = []
    for i, entry in enumerate(raw):
        where = f"usage_accounts entry {i + 1}"
        if any(_same_entry(entry, old) or (not isinstance(entry, dict) and type(entry) is type(old)
                                           and entry == old) for old in kept):
            # Stored as it is (an object, or any other JSON value): kept
            # whatever per-entry rule it breaks today.
            checked.append((_account_from_entry(entry), True))
            out.append(copy.deepcopy(entry))
            continue
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
        if entry.get("label_source") not in (None, "owner", "suggested"):
            raise AccountsError(f"{where}: label_source must be owner or suggested", "bad_entry",
                                index=i, field="label_source")
        account = _account_from_entry(entry)
        account["label_source"] = entry.get("label_source") or ("owner" if entry.get("label") else "suggested")
        checked.append((account, False))
        stored = {"label": account["label"], "harness": account["harness"], "label_source": account["label_source"]}
        if account["home"]:
            stored["home"] = account["home"]
        out.append(stored)
    def marks(account: dict) -> tuple[str, str | None]:
        return account["label"].casefold(), None if account.get("invalid") else login_identity(account)

    # A clash the stored list already holds between entries kept as they are
    # (a hand-edited list) is left for the owner to fix; no save adds one.
    before: dict[tuple[str, str | None], int] = {}
    for old in kept:
        name, key = marks(_account_from_entry(old))
        for mark in (("name", name), ("login", key)):
            before[mark] = before.get(mark, 0) + 1
    after: dict[tuple[str, str | None], int] = {}
    for account, _was_kept in checked:
        name, key = marks(account)
        for mark in (("name", name), ("login", key)):
            after[mark] = after.get(mark, 0) + 1
    labels: dict[str, int] = {}
    logins: dict[str, int] = {}
    for i, (account, was_kept) in enumerate(checked):
        name, key = marks(account)
        for seen, mark, code, field in ((labels, name, "name_taken", "label"), (logins, key, "login_taken", "home")):
            if mark is None or mark not in seen:
                continue
            kind = "name" if code == "name_taken" else "login"
            if was_kept and checked[seen[mark]][1] and after[(kind, mark)] <= before.get((kind, mark), 0):
                continue
            j = seen[mark]
            # Point at the entry the owner changed, not the one kept as stored.
            at, other = (j, i) if was_kept and not checked[j][1] else (i, j)
            if code == "name_taken":
                raise AccountsError(f"two usage_accounts entries are named {account['label']!r}", code,
                                    index=at, field=field, other=other)
            raise AccountsError(f"usage_accounts entry {at + 1} ({checked[at][0]['label']}) reads the same login "
                                f"as another entry", code, index=at, field=field, other=other)
        labels.setdefault(name, i)
        if key is not None:
            logins.setdefault(key, i)
    for i, (account, was_kept) in enumerate(checked):
        if was_kept:
            continue
        named = _name_problem(account["label"], account["harness"], account["home"],
                              (ids - {account_id_of(account)}) if ids else ids)
        if named:
            raise AccountsError(f"usage_accounts entry {i + 1}: {named[1]}", named[0], index=i, field="label")
    # Distinct labels can still collapse to one provider slug ("work!"
    # and "work"). Never let a write merge those account identities.
    from khipu.routing_identity import validate
    from khipu.routing import RouteError

    try:
        validate([a for a, _kept in checked if not a.get("invalid")])
    except RouteError as error:
        raise AccountsError(str(error), "name_taken", field="label") from None
    return out


def accounts_status() -> dict:
    """What ``khipu config`` shows: the stored ``usage_accounts`` (None when
    unset), where the list comes from, and the accounts it resolves to."""
    from khipu.config import load_config

    from khipu.config import accounts_version

    config = load_config()
    raw = config.get("usage_accounts")
    stored = raw if isinstance(raw, list) else None
    try:
        user_home = str(Path.home())
    except RuntimeError:
        user_home = None
    none = reads_none(config)
    # ``user_home`` lets the app compare "~/.x" with "/Users/me/.x" the way
    # the stored list is checked, before anything is saved. ``version`` is
    # what a write hands back (``--expect``) so it is refused when the list
    # changed after it was read.
    request = config.get("usage_accounts_request")
    return {"value": stored, "source": "none" if none else "file" if stored is not None else "default",
            "accounts": configured_accounts(config=config), "user_home": user_home, "version": accounts_version(config),
            "none": none, "request": request if isinstance(request, dict) else None}


def _key(account: dict) -> str:
    """Cache identity: the harness and the home whose login answers (labels
    are display names and may change). An http-quota account has no login:
    its stable account id is its cache key."""
    quota_key = account.get("quota_key")
    if isinstance(quota_key, str) and quota_key:
        return quota_key
    home = account.get("home")
    if not home:
        return f"{account['harness']}|"
    path = _home_path(home)
    return f"{account['harness']}|{path if path is not None else home}"


def _blank_row(account: dict) -> dict:
    return {"harness": account["harness"], "account": account["label"], "home": account.get("home"),
            "plan": None, "windows": [], "credits": None, "version": None, "source": None,
            "observed_at": None, "error": None, "note": None}


def probe_account(account: dict, *, timeout: float = TIMEOUT_S, rollout: bool = True, turn: bool = True) -> dict:
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
        elif harness == "deepseek":
            data = probe_deepseek(timeout=timeout)
        elif account.get("probe") == "http-quota" or account.get("quota_key"):
            data = probe_http_quota(account, timeout=timeout)
        elif harness in _HOME_VARS and not _login_folder_there(home or str(default_login(harness))):
            # Checked on the folder itself (nothing inside it is read): a CLI
            # pointed at a missing folder answers "not signed in", which a
            # sign-in can't fix when the folder moved or was removed. A
            # home-less account reads its CLI's default folder.
            data = {"ok": False, "error": E_NO_FOLDER, "note": NO_FOLDER_NOTE}
        elif harness == "claude":
            data = probe_claude(home, timeout=timeout, turn=turn)
        elif harness == "codex":
            data = probe_codex(home, timeout=timeout, rollout=rollout)
        elif harness == "grok":
            data = probe_grok(home, timeout=timeout)
        elif harness == "muse":
            data = probe_muse(timeout=timeout, turn=turn)
        elif harness == "glm":
            data = probe_glm(timeout=timeout)
        else:
            data = {"ok": False, "error": E_INVALID}
        data = dict(data)
        raw_account_id = None
        for key in ("account_id", "accountId", "user_id", "userId"):
            raw_account_id = data.pop(key, None) or raw_account_id
        digest = data.get("account_digest") or _account_digest(harness, raw_account_id)
        if digest:
            data["account_digest"] = digest
        else:
            data.pop("account_digest", None)
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
    overall deadline (the longest per-harness budget in play, Grok's
    included, + ``OVERALL_GRACE_S``) whatever happens: a
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
    longest = max([timeout] + [_grok_budget(timeout) for a in accounts if a.get("harness") == "grok"]
                  + [GLM_TIMEOUT_S if timeout == TIMEOUT_S else timeout for a in accounts if a.get("harness") in ("glm", "deepseek")])
    end = time.monotonic() + longest + OVERALL_GRACE_S
    for thread in threads:
        thread.join(max(0.0, end - time.monotonic()))
    later = run.until + 0.25
    if later > end:
        for thread in threads:
            thread.join(max(0.0, later - time.monotonic()))
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


def _save_cache(rows: dict[str, dict], *, strict: bool = False) -> None:
    """A read-only data dir costs the cache, never the answer; ``strict``
    (an account change's own write, which rolls back on it) raises."""
    path = cache_file()
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp",
                                         encoding="utf-8") as fh:
            tmp = Path(fh.name)
            json.dump({"version": CACHE_VERSION, "rows": rows}, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink()
        if strict:
            raise


def _open_lock(name: str, *, directory: Path | None = None) -> int | None:
    path = directory / name if directory is not None else cache_file().with_name(name)
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
    """A good probe replaces figures only when they are at least as recent.
    A failed live read keeps newer good figures even if a fallback succeeds.
    A failed probe keeps the last good figures
    with their own ``probed_at`` (and ``last_good_at``); the attempt goes in
    ``last_attempt_at`` / ``last_error``, however many failures in a row.
    The note is the failure's own (the ``/login`` hint, say): a note that
    came with the kept figures (the Codex fallback's) held only while that
    read was good."""
    attempt = row.get("probed_at")
    if row.get("ok"):
        old_time = (_oldest_figures_time(previous)
                    if previous and not _future_times(previous, _now()) else None)
        new_time = _oldest_figures_time(row)
        if previous and old_time is not None and (new_time is None or new_time < old_time):
            failure = row.get("live_error")
            kept = dict(previous, last_attempt_at=attempt, last_error=failure, ms=row.get("ms"),
                        account=row.get("account"), home=row.get("home"))
            if failure:
                kept.update(ok=False, error=failure, live_error=failure, note=row.get("note"))
            return kept
        return dict(row, last_attempt_at=attempt, last_error=row.get("live_error"), last_good_at=attempt)
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
        from khipu.config import read_config_unsettled
        from khipu.routing_sources import configured_keys

        # The list as it is now, not as it was when the read began: a read
        # still running when its account was stopped or moved writes no row
        # under a key the list no longer holds. Read unsettled: an account
        # change takes this cache's lock after the routing lock.
        current = configured_keys(read_config_unsettled()) | {
            _key(a) for a in accounts if a.get("quota_key")}
        merged = []
        for account, row in zip(accounts, rows):
            key = _key(account)
            out = _merge(cache.get(key), row)
            if not account.get("invalid") and key in current:
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


# Keeping the cache fresh without a reader asking (routing freshness,
# 2026-10-17): the five-minute queue-drain job refreshes what is older than
# ``PERIODIC_MAX_AGE_S``, and the per-prompt routing read starts one detached
# refresh when the cache is older than ``PROMPT_MAX_AGE_S``, at most once per
# ``KICK_EVERY_S``. Both go through the shared refresh lock, so one refresh
# runs at a time on this Mac.
PERIODIC_MAX_AGE_S = 300
PROMPT_MAX_AGE_S = 600
KICK_EVERY_S = 60
KICK_STAMP_NAME = "usage.kick"


def _due(accounts: list[dict], cache: dict[str, dict], now: datetime, max_age: float) -> list[dict]:
    """The valid accounts whose last attempt is older than ``max_age`` (or
    that were never read)."""
    out = []
    for account in accounts:
        if account.get("invalid"):
            continue
        cached = cache.get(_key(account)) or {}
        attempted = _parse_time(cached.get("last_attempt_at") or cached.get("probed_at"))
        age_limit = min(max_age, DEEPSEEK_POLL_S) if account.get("harness") == "deepseek" else max_age
        if attempted is None or _future_times(cached, now) or _reset_due(cached, now) or ((now - attempted).total_seconds() >= age_limit
                                 if account.get("harness") == "deepseek"
                                 else (now - attempted).total_seconds() > age_limit):
            out.append(account)
    return out


def refresh_due(*, max_age: float = PERIODIC_MAX_AGE_S, timeout: float = TIMEOUT_S,
                accounts: list[dict] | None = None) -> dict:
    """Read every account whose figures are older than ``max_age``, under
    the shared refresh lock taken without waiting: when another refresh
    holds it, that refresh is this one. What is due is judged again once the
    lock is held, so callers that race refresh once. Never raises:
    ``{"refreshed": n, "skipped": None | "fresh" | "busy" | "error"}``."""
    try:
        discover_quota = accounts is None
        accounts = configured_accounts() if accounts is None else accounts
        if discover_quota:
            accounts = [*accounts, *http_quota_accounts()]
        if not _due(accounts, _load_cache(), _now(), max_age):
            return {"refreshed": 0, "skipped": "fresh"}
        fd = _try_refresh_lock()
        if fd is None:
            return {"refreshed": 0, "skipped": "busy"}
        try:
            due = _due(accounts, _load_cache(), _now(), max_age)
            if not due:
                return {"refreshed": 0, "skipped": "fresh"}
            _store(due, probe_all(due, timeout=timeout))
            return {"refreshed": len(due), "skipped": None}
        finally:
            _release(fd)
    except Exception:  # noqa: BLE001 - a periodic refresh never fails its caller
        return {"refreshed": 0, "skipped": "error"}


def kick_refresh(*, max_age: float = PROMPT_MAX_AGE_S, spawn=None) -> bool:
    """For a caller that must not wait (the per-prompt routing read): when
    some account's figures are older than ``max_age``, no refresh holds the
    lock and none was started in the last ``KICK_EVERY_S``, start
    ``khipu usage --refresh-due`` as its own detached process and return at
    once. True when one was started. Never raises."""
    try:
        stamp = cache_file().with_name(KICK_STAMP_NAME)
        with contextlib.suppress(OSError):
            if 0 <= time.time() - stamp.stat().st_mtime < KICK_EVERY_S:
                return False
        if not _due([*configured_accounts(), *http_quota_accounts()], _load_cache(), _now(), max_age):
            return False
        fd = _try_refresh_lock()
        if fd is None:
            return False
        _release(fd)  # only a look: the child takes it for its own read
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        os.utime(stamp)
        argv = [sys.executable, "-m", "khipu", "usage", "--refresh-due", str(int(max_age)), "--json"]
        if spawn is not None:
            spawn(argv)
        else:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             cwd=str(Path.home()), start_new_session=True, close_fds=True)
        return True
    except Exception:  # noqa: BLE001 - the prompt never pays for a refresh
        return False


def _figures_time(row: dict, window: dict | None = None) -> datetime | None:
    """Stored provider figures need their observation time, not retrieval time."""
    when = _parse_time((window or {}).get("observed_at")) or _parse_time(row.get("observed_at"))
    if row.get("source") in ("rollout", "last_observed"):
        return when
    return (when or (_parse_time(row.get("last_good_at")) if not row.get("ok") else None)
            or _parse_time(row.get("probed_at")))


def _oldest_figures_time(row: dict) -> datetime | None:
    windows = [w for w in row.get("windows") or [] if isinstance(w, dict)]
    times = [_figures_time(row, w) for w in windows] if windows else [_figures_time(row)]
    return min(times) if all(t is not None for t in times) else None


def _age_s(row: dict, now: datetime) -> float | None:
    when = _oldest_figures_time(row)
    return None if when is None else (now - when).total_seconds()


def _expired_window(window: dict, now: datetime) -> bool:
    reset = _parse_time(window.get("resets_at"))
    return reset is not None and reset <= now


def _reset_due(row: dict, now: datetime) -> bool:
    attempted = _parse_time(row.get("last_attempt_at") or row.get("probed_at"))
    return any(_expired_window(w, now) and (attempted is None or attempted < _parse_time(w.get("resets_at")))
               for w in row.get("windows") or [] if isinstance(w, dict))


def _future_times(row: dict, now: datetime) -> bool:
    times = [_parse_time(row.get("last_attempt_at") or row.get("probed_at")), _figures_time(row)]
    times.extend(_figures_time(row, w) for w in row.get("windows") or [] if isinstance(w, dict))
    return any(at is not None and at > now for at in times)


def _stale_figures(row: dict, now: datetime, max_age: float) -> bool:
    age = _age_s(row, now)
    return bool(_future_times(row, now) or (not row.get("ok") and row.get("last_good_at"))
                or (age is None and row.get("source") in ("rollout", "last_observed"))
                or (age is not None and not 0 <= age <= max_age)
                or any(_expired_window(w, now) for w in row.get("windows") or [] if isinstance(w, dict)))


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
        young = bool(cached and attempted and 0 <= (now - attempted).total_seconds() <= max_age
                     and not _future_times(cached, now) and not _reset_due(cached, now))
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
            # An account read before keeps its last figures, marked stale
            # and refreshing as the background path's; only one never read
            # is pending (routing fix round 2, R2-5).
            refreshing = True
            for i in due:
                cached = cache.get(_key(accounts[i]))
                if rows[i] is None and cached:
                    rows[i] = dict(cached, account=accounts[i]["label"], home=accounts[i].get("home"), cached=True)
                elif rows[i] is None:
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
    for account, row in zip(accounts, rows):
        # The identity the routing record keys this account by, so the app
        # matches the two by login, never by a name that may be reused.
        row["key"] = _key(account)
        row["stale"] = _stale_figures(row, now, max_age)
        row["windows"] = [dict(w, used_pct=None, expired=True) if _expired_window(w, now) else dict(w)
                          for w in row.get("windows") or []]
    return {"generated_at": _iso(now.timestamp()), "max_age_s": max_age, "refreshing": refreshing,
            "accounts": rows, "summary_text": [summary_line(r) for r in rows]}


# What a conversation sees of a row (``khipu_usage``): an allowlist, so a field
# added to the owner's rows later stays out until named here. Never ``home``
# or any path: conversations reach other model providers (slice 9A).
_PUBLIC_ROW_FIELDS = ("harness", "account", "plan", "ok", "error", "live_error", "last_error", "version",
                      "source", "observed_at", "probed_at", "last_attempt_at", "last_good_at", "ms", "cached",
                      "stale", "refreshing")
_PUBLIC_WINDOW_FIELDS = ("name", "used_pct", "resets_at", "window_mins", "model", "expired")
INVALID_PUBLIC_NOTE = "This account entry is not valid; the owner can fix it in Khipu's Settings"


def public_row(row: dict) -> dict:
    """One report row as a conversation may see it: the account by its
    label and its ``khipu_route`` id, its state and figures, and a note in
    Khipu's own words with no login folder in it."""
    from khipu import routing

    harness, home = row.get("harness"), row.get("home")
    out = {k: row[k] for k in _PUBLIC_ROW_FIELDS if k in row}
    out["id"] = routing.account_id(_key({"harness": harness, "home": home}))
    out["harness"] = harness if harness in HARNESSES else "?"
    if routing.account_label_source(_key(row)) == "suggested":
        out["naming_hint"] = routing.account_identity.NAME_ACCOUNT
    for field in ("error", "live_error", "last_error"):
        if field in out and out[field] is not None and not _is_code(out[field]):
            out[field] = E_INTERNAL
    out["windows"] = [{k: w[k] for k in _PUBLIC_WINDOW_FIELDS if k in w}
                      for w in row.get("windows") or [] if isinstance(w, dict)]
    credits = row.get("credits")
    out["credits"] = ({k: v for k, v in credits.items() if isinstance(v, (bool, int, float))} or None
                      if isinstance(credits, dict) else None)
    error = out.get("error")
    if row.get("error") == E_INVALID:
        # The owner's reason names the entry as typed (a folder included).
        note = INVALID_PUBLIC_NOTE
    else:
        # Only a note Khipu itself writes for this row passes; any other text
        # (a cache edited by hand, a field from elsewhere) is written again.
        note = row.get("note")
        own = {_note(error, harness, home), _note(error, harness, None),
               _rollout_note(out.get("live_error") or error or "", home), ROLLOUT_NOTE, "no turn observed yet"}
        if note is not None and note not in own:
            note = _note(error, harness, home)
    out["note"] = note
    return out


_EXIT_CODE = re.compile(r"(?:exit|signal)_\d{1,4}")


def _is_code(value) -> bool:
    """Whether ``value`` is one of Khipu's own fixed error codes (``E_*``,
    or ``exit_N`` / ``signal_N`` from ``_exit_code``)."""
    if not isinstance(value, str):
        return False
    codes = {v for k, v in globals().items() if k.startswith("E_") and isinstance(v, str)}
    return value in codes or bool(_EXIT_CODE.fullmatch(value))


def public_report(rep: dict) -> dict:
    """``report`` as ``khipu_usage`` answers it: every row through
    ``public_row`` and each summary line rebuilt from the public row."""
    rows = [public_row(r) for r in rep.get("accounts") or []]
    return {"generated_at": rep.get("generated_at"), "max_age_s": rep.get("max_age_s"),
            "refreshing": bool(rep.get("refreshing")), "accounts": rows,
            "summary_text": [summary_line(r) for r in rows]}


def account_key(account: dict) -> str:
    """The identity an owner action carries: ``harness|login folder`` as
    ``_key`` writes it. Labels are display names and may repeat."""
    return _key(account)


def resolve_account(label: str | None = None, key: str | None = None) -> tuple[dict | None, str | None]:
    """The one valid configured account an owner action names, as
    ``(account, None)``, or ``(None, error)``. A ``key`` (``account_key``)
    wins over the label; a label alone that more than one account carries is
    refused (``E_AMBIGUOUS``), never resolved to the first."""
    valid = [a for a in configured_accounts() if not a.get("invalid")]
    if key is not None:
        harness, _, home = key.partition("|")
        wanted = _key({"harness": harness, "home": home or None})
        found = [a for a in valid if _key(a) == wanted]
        if label is not None:
            # Both given: they must name the same account (a tie on the key
            # is settled by the label, never by list order).
            found = [a for a in found if a["label"] == label]
    else:
        found = [a for a in valid if a["label"] == label]
    if len(found) > 1:
        return None, E_AMBIGUOUS
    return (found[0], None) if found else (None, E_NOT_CONFIGURED)


def configured_account(label: str) -> dict | None:
    """The configured account with this label (``configured_accounts``),
    never an entry that is not valid; None when there is none or when more
    than one carries it (``resolve_account`` says which)."""
    return resolve_account(label)[0]


# ---- signing in again from the app ---------------------------------------------
#
# Each CLI's own login, as its ``--help`` gives it, for the CLIs whose login
# opens the owner's browser and asks nothing on a terminal. Grok Build's
# ``grok login`` and Muse's ``muse login`` are left out: Muse shows a code to
# approve, and nothing in Grok's help says its login asks nothing on a
# terminal, so the app says "Sign in from Grok Build" (or Muse) instead.
# The argv is fixed; only the account's own login folder (its
# ``CLAUDE_CONFIG_DIR`` or ``CODEX_HOME``, set by ``probe_environment``)
# changes. Khipu never reads, prints or stores what the login writes.
SIGN_IN_ARGS = {"claude": ("auth", "login", "--claudeai"), "codex": ("login",)}
# How long Khipu waits for the owner to finish in the browser before it stops
# the login it started (pressing Sign in again starts a new one).
SIGN_IN_WAIT_S = 15 * 60
E_NOT_CONFIGURED = "not_configured"
E_AMBIGUOUS = "ambiguous_account"
E_NO_SIGN_IN = "no_sign_in"
E_SIGN_IN_TIMEOUT = "sign_in_timeout"


def sign_in_argv(harness: str, binary: str) -> list[str] | None:
    """The login's whole argv for ``harness``, or None when Khipu offers no
    sign-in for it."""
    args = SIGN_IN_ARGS.get(harness)
    return [binary, *args] if args else None


def sign_in(label: str | None, *, key: str | None = None, wait: float = SIGN_IN_WAIT_S,
            timeout: float = TIMEOUT_S, popen=None) -> dict:
    """Start the configured account's own login in its own login folder, in
    its own session with no terminal (it opens the browser), wait for it to
    end, then read that account's plan again. The desktop runner sets
    `KHIPU_DESKTOP_MANAGED=1`, which keeps this one child in its process group
    so its deadline can cancel the whole desktop action. Returns the outcome
    and the account's new row; the login's own output is never read."""
    account, error = resolve_account(label, key)
    if account is None:
        return {"ok": False, "error": error}
    harness = account["harness"]
    binary = _command(harness)
    if harness not in SIGN_IN_ARGS:
        return {"ok": False, "error": E_NO_SIGN_IN, "cli": CLI_NAMES.get(harness, harness)}
    if not binary:
        return {"ok": False, "error": E_NOT_INSTALLED}
    argv = sign_in_argv(harness, binary)
    env = probe_environment(harness, account.get("home"), binary)
    # `probe_environment` deliberately allowlists only provider-facing values;
    # this runner-control marker stays in Khipu's own environment.
    desktop_managed = os.environ.get("KHIPU_DESKTOP_MANAGED") == "1"
    start = popen or subprocess.Popen
    with tempfile.TemporaryDirectory(prefix="khipu-sign-in-") as cwd:
        proc = start(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=not desktop_managed)
        try:
            code = proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            # Under the desktop runner this child inherited the runner's
            # process group, so _killpg(proc) would target the wrong group.
            # Kill and reap it directly; the runner cancels the shared group.
            if desktop_managed:
                _kill(proc)
            else:
                _killpg(proc)
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)
            code = None
    row = report(fresh=True, accounts=[account], timeout=timeout)["accounts"][0]
    out = {"ok": code == 0, "account": account["label"], "row": row}
    if code is None:
        out["error"] = E_SIGN_IN_TIMEOUT
    elif code != 0:
        out["error"] = _exit_code(code)
    return out


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
        text = f"{head}: " + ", ".join(
            f"{_window_label(w)} unknown (reset passed)" if w.get("expired") else
            f"{_window_label(w)} {_pct(w.get('used_pct'))}" for w in windows)
        tight = max((w for w in windows if not w.get("expired")),
                    key=lambda w: w.get("used_pct") if isinstance(w.get("used_pct"), (int, float)) else -1,
                    default={})
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
    from khipu.config import load_config

    now = now or _now()
    cache = _load_cache()
    accounts: list[dict] = []

    raw = load_config().get("usage_accounts")
    warnings: list[str] = (["Ignored a tracked Cursor entry: Cursor is a transport on the xAI account; "
                            "its usage and limits count against Grok"]
                if isinstance(raw, list) and any(_cursor_entry(e) for e in raw) else [])
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
        stale = _stale_figures(row, now, max_age)
        live = [w for w in row.get("windows") or [] if _in_force(w, now)]
        tight = max(live, key=lambda w: _shown_pct(w.get("used_pct")), default=None)
        figures_time = _figures_time(row)
        entry.update(ok=bool(row.get("ok")), error=row.get("error"), plan=row.get("plan"),
                     figures_at=_iso(figures_time.timestamp()) if figures_time is not None else None, stale=stale)
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
