"""Signing in a new account from the app (settings clarity phase 9, slice 9D;
docs/plans/2026-10-15-settings-clarity.md, "Signing in a new account").

Three calls the app makes in turn: ``make_folder`` creates the new login
folder (``~/.<cli>-<slug>``, decision 2), ``start`` starts that CLI's own
login in it and returns at once with an attempt id, ``status`` reads the
attempt and ``cancel`` ends it. ``reap`` ends a leftover attempt on the next
start and when the app quits. The existing Sign in again path
(``usage.sign_in``) is separate and unchanged.

What this never does: read inside a login folder (it stats the folder
itself, asks the CLI and, to remove a folder it made, calls ``rmdir``, which
only removes an empty one), read a Claude or Codex login's output, or keep a
Grok Build or Muse sign-in code anywhere but in the one reply that carries it
to the dialog. Nothing here is reached from an MCP tool.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import select
import signal
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import MappingProxyType

from khipu import discover, usage

CLIS = ("claude", "codex", "grok", "muse")
FOLDER_CLIS = ("claude", "codex", "grok")
# Each CLI's own login, fixed. Claude Code and Codex open the browser and
# return to a local callback; Grok Build and Muse print a link and a code.
ARGV = MappingProxyType({
    "claude": ("auth", "login", "--claudeai"),
    "codex": ("login",),
    "grok": ("login", "--device-auth"),
    "muse": ("login",),
})
BROWSER = frozenset({"claude", "codex"})
CODE = frozenset({"grok", "muse"})

# The plan's guardrails: a CLI gets "Sign in to another account" only once its
# proof passed on a real Mac. Codex waits for the Keychain proof (a second
# CODEX_HOME keeps its own sign-in), Grok Build for the device-code proof (no
# change to the default login or its leader socket); Muse's code sign-in has
# not been run by Khipu either. Until then the app offers only "Use a login I
# already have" for them, and ``start`` refuses them.
PROVEN = MappingProxyType({"claude": True, "codex": False, "grok": False, "muse": False})
GUARDRAIL = MappingProxyType({
    "codex": "codex_keychain",
    "grok": "grok_device_code",
    "muse": "muse_device_code",
})

SLUG_MAX = 32
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
_FOLDER_RE = re.compile(r"^\.(claude|codex|grok)-([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)$")
_SKIP_SLUGS = frozenset({"default", "backup", "backups", "logs", "bak"})
TAKEN_TRIES = 20

WAIT_S = usage.SIGN_IN_WAIT_S
CODE_WAIT_S = 20.0
LINE_MAX = 1024
READ_MAX = 64 * 1024
KILL_GRACE_S = 2.0

# Where a Grok Build or Muse code line may send the owner. Not yet confirmed
# against a real run (the guardrail run confirms both the hosts and the code's
# shape before ``PROVEN`` turns on).
CODE_HOSTS = MappingProxyType({
    "grok": ("accounts.x.ai", "auth.x.ai", "x.ai", "grok.com", "accounts.grok.com"),
    "muse": ("meta.ai", "www.meta.ai", "auth.meta.ai"),
})
_CODE_SHAPE = r"[A-Z0-9]{4}(?:-[A-Z0-9]{2,8}){1,2}"


def _code_pattern(hosts: tuple[str, ...]) -> re.Pattern:
    """One printable line carrying an https link on ``hosts`` and a code,
    in either order; nothing else on the line is kept."""
    host = "|".join(re.escape(h) for h in hosts)

    def link(n: int) -> str:
        return rf"(?P<link{n}>https://(?:{host})(?:/[A-Za-z0-9._~/?=&%+-]*)?)(?![^\s,;)])"

    def code(n: int) -> str:
        return rf"(?<![A-Za-z0-9-])(?P<code{n}>{_CODE_SHAPE})(?![A-Za-z0-9-])"

    rest = r"[\x20-\x7e]*?"
    return re.compile(rf"^{rest}(?:{link(1)}{rest}{code(1)}|{code(2)}{rest}{link(2)}){rest}\s*$")


CODE_PATTERNS = MappingProxyType({cli: _code_pattern(hosts) for cli, hosts in CODE_HOSTS.items()})

LOCK_NAME = "usage.sign_in.lock"
ATTEMPT_NAME = "usage.sign_in.json"
FOLDERS_NAME = "signin-folders.json"

E_REFUSED = "refused"
E_GUARDRAIL = "not_proven"
E_BUSY = "busy"
E_NOT_INSTALLED = usage.E_NOT_INSTALLED
E_START = usage.E_START
E_NO_CODE = "no_code"
E_SLUG = "bad_name"
E_TAKEN = "folder_taken"
E_NO_ATTEMPT = "no_attempt"

WAITING = "waiting"
STARTING = "starting"
SIGNED_IN = "signed_in"
NOT_SIGNED_IN = "not_signed_in"
COULDNT_CHECK = "couldnt_check"
TIMED_OUT = "timeout"
CANCELLED = "cancelled"


def wait_s() -> float:
    """The 15-minute wait; a shorter one only under a throwaway test home
    (``KHIPU_TEST=1`` with its own ``KHIPU_DATA_DIR``)."""
    raw = os.environ.get("KHIPU_SIGN_IN_WAIT_S")
    if raw and os.environ.get("KHIPU_TEST") == "1" and os.environ.get("KHIPU_DATA_DIR"):
        with contextlib.suppress(ValueError):
            return max(1.0, float(raw))
    return WAIT_S


def offers() -> dict:
    """Which CLIs the app may offer "Sign in to another account" for, and
    the guardrail each other one waits for."""
    return {cli: {"offered": bool(PROVEN.get(cli)), "folder": cli in FOLDER_CLIS,
                  "kind": "browser" if cli in BROWSER else "code",
                  **({} if PROVEN.get(cli) else {"waits_for": GUARDRAIL.get(cli)})}
            for cli in CLIS}


# ---- Khipu's own files -----------------------------------------------------------


def _read_json(name: str):
    with discover._data_state() as dfd:
        text = discover._read_at(dfd, name)
    try:
        return json.loads(text) if text is not None else None
    except ValueError:
        return None


def _write_json(name: str, data) -> bool:
    """Write with a temp file and rename; False when it could not be kept."""
    raw = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    with discover._data_state(create=True) as dfd:
        if dfd is None:
            return False
        tmp = f"{name}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            try:
                view = memoryview(raw)
                while view:
                    view = view[os.write(fd, view):]
            finally:
                os.close(fd)
            os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            return True
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dfd)
            return False


def _delete(name: str) -> None:
    with discover._data_state() as dfd:
        if dfd is not None:
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=dfd)


def _lock() -> int | None:
    """The one-sign-in lock (``flock LOCK_NB``); None when someone holds it."""
    with discover._data_state(create=True) as dfd:
        if dfd is None:
            return None
        try:
            fd = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
        except OSError:
            return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _attempt() -> dict | None:
    data = _read_json(ATTEMPT_NAME)
    return data if isinstance(data, dict) and isinstance(data.get("id"), str) else None


def _folders() -> list[dict]:
    data = _read_json(FOLDERS_NAME)
    items = data.get("folders") if isinstance(data, dict) else None
    return [f for f in items if isinstance(f, dict)] if isinstance(items, list) else []


def _save_folders(items: list[dict]) -> bool:
    return _write_json(FOLDERS_NAME, {"version": 1, "folders": items})


def _made_item(cli: str, name: str, info: os.stat_result) -> dict | None:
    """Khipu's record of the folder ``name`` it made for ``cli``, when the
    folder there now is still that same folder."""
    for item in _folders():
        if item.get("cli") == cli and item.get("name") == name and item.get("identity") == _ident(info):
            return item
    return None


# ---- the new login folder ------------------------------------------------------------


def slug(name: str, cli: str) -> str | None:
    """The folder slug for an account name: lowercase, a leading CLI word
    dropped ("Claude client" -> "client"), only ``[a-z0-9-]``, repeated dashes
    collapsed, 1 to 32 characters; None when nothing usable is left or it is
    a name discovery skips."""
    if cli not in FOLDER_CLIS or not isinstance(name, str) or not name.isprintable():
        return None
    words = [w for w in re.split(r"[^a-z0-9]+", name.lower()) if w]
    # One leading CLI word, the longest form first ("Grok Build", "Grok",
    # "grok"); a second one is part of the name.
    for prefix in sorted({tuple(usage.CLI_NAMES[cli].lower().split()), (usage.LABELS[cli].lower(),), (cli,)},
                         key=len, reverse=True):
        if tuple(words[:len(prefix)]) == prefix:
            words = words[len(prefix):]
            break
    text = "-".join(words)[:SLUG_MAX].strip("-")
    if not text or not _SLUG_RE.match(text) or text in _SKIP_SLUGS:
        return None
    if discover.name_match(f".{cli}-{text}") != cli:
        return None
    return text


def _lstat_dir(path: Path) -> os.stat_result | None:
    """The folder's own entry when it is a real folder, never a symlink."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return info if stat.S_ISDIR(info.st_mode) else None


def _ident(info: os.stat_result) -> str:
    return f"inode:{info.st_dev}:{info.st_ino}"


def make_folder(cli: str, name: str) -> dict:
    """Create ``~/.<cli>-<slug>`` with an exclusive ``mkdir`` (0700, never
    ``makedirs``); ``-2``, ``-3`` when anything is at that path. A folder
    Khipu made for the same slug that never signed in is reused."""
    if cli not in FOLDER_CLIS:
        return {"ok": False, "error": E_REFUSED}
    base = slug(name, cli)
    if base is None:
        return {"ok": False, "error": E_SLUG}
    fd = _lock()
    if fd is None:
        return {"ok": False, "error": E_BUSY}
    try:
        home = discover._home()
        made = _folders()
        for item in made:
            folder = item.get("name")
            m = _FOLDER_RE.match(folder) if isinstance(folder, str) else None
            if not m or m.group(1) != cli or item.get("cli") != cli or item.get("slug") != base \
                    or item.get("signed_in"):
                continue
            info = _lstat_dir(home / folder)
            if info is not None and _ident(info) == item.get("identity"):
                return {"ok": True, "folder": discover.display_path(home / folder), "reused": True}
        for n in range(1, TAKEN_TRIES + 1):
            folder = f".{cli}-{base}" if n == 1 else f".{cli}-{base}-{n}"
            path = home / folder
            try:
                os.mkdir(path, 0o700)
            except FileExistsError:
                continue
            except OSError:
                return {"ok": False, "error": E_START}
            info = _lstat_dir(path)
            kept = [m for m in made if m.get("name") != folder]
            if info is not None:
                kept.append({"cli": cli, "slug": base, "name": folder, "identity": _ident(info),
                             "created": time.time(), "signed_in": False})
            if info is None or not _save_folders(kept):
                # Unrecorded, ``start`` would refuse it: take back the empty folder.
                with contextlib.suppress(OSError):
                    os.rmdir(path)
                return {"ok": False, "error": E_START}
            return {"ok": True, "folder": discover.display_path(path), "reused": False}
        return {"ok": False, "error": E_TAKEN}
    finally:
        usage._release(fd)


def khipu_made(cli: str, folder: str | None) -> tuple[Path | None, str | None]:
    """The folder ``start`` may sign in to: ``~/.<cli>-<slug>`` directly in
    the home folder, a real folder (no symlink), one Khipu made and recorded,
    with the same identity it had then."""
    if cli not in FOLDER_CLIS or not isinstance(folder, str) or not folder.isprintable():
        return None, E_REFUSED
    if not folder.startswith("~/") or "/" in folder[2:]:
        return None, E_REFUSED
    name = folder[2:]
    m = _FOLDER_RE.match(name)
    if not m or m.group(1) != cli:
        return None, E_REFUSED
    path = discover._home() / name
    info = _lstat_dir(path)
    if info is None or _made_item(cli, name, info) is None:
        return None, E_REFUSED
    return path, None


def _ever_signed_in(cli: str, path: Path) -> bool:
    info = _lstat_dir(path)
    item = _made_item(cli, path.name, info) if info is not None else None
    return item is None or bool(item.get("signed_in"))


def _mark_signed_in(cli: str, path: Path) -> None:
    made = _folders()
    changed = False
    for item in made:
        if item.get("cli") == cli and item.get("name") == path.name and not item.get("signed_in"):
            item["signed_in"] = True
            changed = True
    if changed:
        _save_folders(made)


# ---- the login process -----------------------------------------------------------------


def proc_info(pid: int) -> dict | None:
    """``{stat, lstart, command}`` for ``pid`` as ``ps`` reports it, or None
    when there is no such process."""
    try:
        # -ww: the whole command line, never cut to a terminal's width.
        res = subprocess.run(["/bin/ps", "-ww", "-o", "stat=,lstart=,command=", "-p", str(int(pid))],
                             stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    parts = res.stdout.decode("utf-8", "replace").strip().split(None, 6)
    if len(parts) < 7:
        return None
    return {"stat": parts[0], "lstart": " ".join(parts[1:6]), "command": parts[6]}


def _matches(rec: dict, info: dict | None) -> bool:
    """The live process is the one the record names: same start time, the
    same command line as at the start, and that line ends with the CLI's
    fixed sign-in arguments."""
    if not info or str(info.get("stat") or "").startswith("Z"):
        return False
    cli = rec.get("cli")
    tail = list(ARGV.get(cli, ()))
    if not tail or rec.get("tail") != tail:
        return False
    lstart, command = rec.get("lstart"), rec.get("command")
    if not isinstance(lstart, str) or not lstart or not isinstance(command, str) or not command:
        return False
    return info.get("lstart") == lstart and info.get("command") == command \
        and command.endswith(" " + " ".join(tail))


def _alive(rec: dict, ps) -> bool:
    return _matches(rec, ps(rec.get("pid")))


def kill_if_ours(rec: dict, *, ps=None, killpg=None) -> str:
    """Kill the attempt's process group only when its process is still the
    one recorded: ``killed``, ``gone`` (it ended) or ``stale`` (the number
    now belongs to something else; nothing is killed)."""
    ps = ps or proc_info
    killpg = killpg or os.killpg
    pid, pgid = rec.get("pid"), rec.get("pgid")
    if not isinstance(pid, int) or not isinstance(pgid, int) or pid <= 1 or pgid <= 1:
        return "stale"
    info = ps(pid)
    if info is None or str(info.get("stat") or "").startswith("Z"):
        return "gone"
    if not _matches(rec, info):
        return "stale"
    with contextlib.suppress(OSError):
        if os.getpgid(pid) != pgid:
            return "stale"
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        killpg(pgid, signal.SIGTERM)
    end = time.monotonic() + KILL_GRACE_S
    while time.monotonic() < end:
        if not _alive(rec, ps):
            return "killed"
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        killpg(pgid, signal.SIGKILL)
    return "killed"


def _read_code(stream, cli: str, deadline: float) -> tuple[str, str] | None:
    """Read the login's output line by line until one line matches the
    CLI's code pattern; each line is capped at ``LINE_MAX`` bytes and the
    whole read at ``READ_MAX``. Only the matched link and code are kept."""
    pattern = CODE_PATTERNS[cli]
    fd = stream.fileno()
    buf = bytearray()
    total = 0
    skipping = False
    while total < READ_MAX:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        ready, _, _ = select.select([fd], [], [], min(left, 0.25))
        if not ready:
            continue
        chunk = os.read(fd, min(4096, READ_MAX - total))  # never a byte past the cap
        if not chunk:
            return None
        total += len(chunk)
        buf += chunk
        while True:
            nl = buf.find(b"\n")
            if nl < 0:
                if len(buf) > LINE_MAX:
                    buf.clear()
                    skipping = True
                break
            line = bytes(buf[:nl])
            del buf[:nl + 1]
            if skipping or len(line) > LINE_MAX:
                skipping = False
                continue
            m = pattern.match(line.decode("ascii", "replace").rstrip("\r"))
            del line
            if m:
                link = m.group("link1") or m.group("link2")
                code = m.group("code1") or m.group("code2")
                return link, code
    return None


def _drain(stream) -> None:
    """The rest of a code login's output goes to /dev/null unread, in a
    process of its own that ends when the login closes its output."""
    with contextlib.suppress(OSError):
        subprocess.Popen(["/bin/cat"], stdin=stream, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    with contextlib.suppress(OSError):
        stream.close()


def _public(rec: dict) -> dict:
    out = {k: rec.get(k) for k in ("id", "cli", "folder", "kind", "started", "deadline")}
    out["cli_name"] = usage.CLI_NAMES.get(rec.get("cli"), rec.get("cli"))
    return out


def _stop_own(proc, killpg=None) -> None:
    """End the group of a child this call started and has not reaped: its
    number cannot belong to anything else until it is waited for."""
    with contextlib.suppress(Exception):
        if proc.poll() is None:
            (killpg or os.killpg)(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.wait(timeout=2)
    with contextlib.suppress(Exception):
        if proc.stdout:
            proc.stdout.close()


def _deadline(rec: dict) -> float:
    value = rec.get("deadline")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _end_attempt(rec: dict) -> None:
    cwd = rec.get("cwd")
    if isinstance(cwd, str) and os.path.basename(cwd).startswith("khipu-sign-in-"):
        with contextlib.suppress(OSError):
            os.rmdir(cwd)
    _delete(ATTEMPT_NAME)


def reap(*, ps=None, killpg=None) -> dict:
    """End a leftover attempt: kill its group only if its process is still
    the recorded one, and delete the record either way."""
    fd = _lock()
    if fd is None:
        return {"ok": False, "error": E_BUSY}
    try:
        rec = _attempt()
        if rec is None:
            return {"ok": True, "reaped": None}
        result = kill_if_ours(rec, ps=ps, killpg=killpg)
        _end_attempt(rec)
        return {"ok": True, "reaped": result}
    finally:
        usage._release(fd)


def running(*, ps=None) -> bool:
    """True while a sign-in Khipu started is still running (discovery waits)."""
    rec = _attempt()
    return rec is not None and _alive(rec, ps or proc_info)


def start(cli: str, folder: str | None = None, *, again: bool = False, popen=None, ps=None,
          killpg=None, drain=None, now=None) -> dict:
    """Start ``cli``'s login in the folder Khipu made (none for Muse, whose
    one login is re-signed) and return at once with the attempt id; for
    Grok Build and Muse, once the code line arrives (``CODE_WAIT_S``). One
    sign-in at a time: a second start gets ``busy`` unless ``again`` (Start
    again), which ends the running one first. A leftover attempt (its
    process ended, is no longer ours, or outlived its wait, as after a crash)
    is reaped first."""
    if cli not in CLIS:
        return {"ok": False, "error": E_REFUSED}
    if not PROVEN.get(cli):
        return {"ok": False, "error": E_GUARDRAIL, "waits_for": GUARDRAIL.get(cli)}
    if cli == "muse":
        if folder:
            return {"ok": False, "error": E_REFUSED}
        path = None
    else:
        path, error = khipu_made(cli, folder)
        if path is None:
            return {"ok": False, "error": error}
    ps = ps or proc_info
    fd = _lock()
    if fd is None:
        return {"ok": False, "error": E_BUSY}
    try:
        # Checked under the lock: a folder that has signed in is an account
        # now, re-signed only through Sign in again.
        if path is not None and _ever_signed_in(cli, path):
            return {"ok": False, "error": E_REFUSED}
        old = _attempt()
        if old is not None:
            now_ts = time.time() if now is None else now
            current = now_ts < _deadline(old)
            if current and not again and _alive(old, ps):
                return {"ok": False, "error": E_BUSY, "attempt": _public(old)}
            kill_if_ours(old, ps=ps, killpg=killpg)
            _end_attempt(old)
        binary = usage._command(cli)
        if not binary:
            return {"ok": False, "error": E_NOT_INSTALLED}
        argv = [binary, *ARGV[cli]]
        env = usage.probe_environment(cli, str(path) if path else None, binary)
        cwd = tempfile.mkdtemp(prefix="khipu-sign-in-")
        code_kind = cli in CODE
        try:
            proc = (popen or subprocess.Popen)(
                argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if code_kind else subprocess.DEVNULL,
                stderr=subprocess.STDOUT if code_kind else subprocess.DEVNULL,
                start_new_session=True, close_fds=True)
        except OSError:
            with contextlib.suppress(OSError):
                os.rmdir(cwd)
            return {"ok": False, "error": E_START}
        started = time.time() if now is None else now
        info = None
        for _ in range(5):
            info = ps(proc.pid)
            if info and info.get("lstart") and info.get("command"):
                break
            time.sleep(0.05)
        rec = {"id": secrets.token_hex(8), "cli": cli, "kind": "code" if code_kind else "browser",
               "folder": discover.display_path(path) if path else None,
               "pid": proc.pid, "pgid": proc.pid, "lstart": (info or {}).get("lstart"),
               "command": (info or {}).get("command"), "tail": list(ARGV[cli]),
               "started": started, "deadline": started + wait_s(), "cwd": cwd}
        # Without the process's own start time and command line no later call
        # could prove the group is ours, so it is ended now through the handle
        # that started it (the only time Khipu signals without that proof).
        if not _matches(rec, info) or not _write_json(ATTEMPT_NAME, rec):
            _stop_own(proc, killpg)
            _end_attempt(rec)
            return {"ok": False, "error": E_START}
        out = {"ok": True, "attempt": _public(rec), "state": WAITING}
        if code_kind:
            try:
                found = _read_code(proc.stdout, cli, time.monotonic() + CODE_WAIT_S)
            except (OSError, ValueError):
                found = None
            if found is None:
                kill_if_ours(rec, ps=ps, killpg=killpg)
                _end_attempt(rec)
                with contextlib.suppress(Exception):
                    proc.stdout.close()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=KILL_GRACE_S)
                return {"ok": False, "error": E_NO_CODE, "cli_name": usage.CLI_NAMES[cli]}
            (drain or _drain)(proc.stdout)
            out["link"], out["code"] = found
            del found
        return out
    finally:
        usage._release(fd)


def _signed_in(cli: str, path: Path | None, check) -> str:
    if check is not None:
        return check(cli, path)
    home = str(path) if path else None
    if cli in discover.STATUS_ARGV:
        binary = usage._command(cli)
        if not binary:
            return COULDNT_CHECK
        res = discover.run_once(cli, (binary, *discover.STATUS_ARGV[cli]), home=home)
        state, _ = discover.classify_status(cli, res)
        del res
    elif cli == "grok":
        state = discover._from_probe(usage.probe_grok(home))[0]
    else:
        state = discover._from_probe(usage.probe_muse())[0]
    return {discover.SIGNED_IN: SIGNED_IN, discover.NOT_SIGNED_IN: NOT_SIGNED_IN}.get(state, COULDNT_CHECK)


def status(attempt_id: str, *, ps=None, killpg=None, check=None, now=None) -> dict:
    """The attempt's state: ``waiting`` while its login runs; once it ended,
    ``signed_in`` or ``not_signed_in`` by asking the CLI (never its output),
    or ``timeout`` after the wait, when Khipu stops it."""
    ps = ps or proc_info
    rec = _attempt()
    if rec is None or rec.get("id") != attempt_id:
        return {"ok": False, "error": E_NO_ATTEMPT}
    fd = _lock()
    if fd is None:
        return {"ok": True, "attempt": _public(rec), "state": STARTING}
    try:
        rec = _attempt()
        if rec is None or rec.get("id") != attempt_id:
            return {"ok": False, "error": E_NO_ATTEMPT}
        now_ts = time.time() if now is None else now
        if _alive(rec, ps):
            if now_ts < _deadline(rec):
                return {"ok": True, "attempt": _public(rec), "state": WAITING}
            kill_if_ours(rec, ps=ps, killpg=killpg)
            _end_attempt(rec)
            # A finished attempt is an answer, not a refusal: ``ok: false`` is
            # kept for real refusals (``no_attempt``), which the app ignores.
            return {"ok": True, "attempt": _public(rec), "state": TIMED_OUT, "error": usage.E_SIGN_IN_TIMEOUT}
        path = None
        if rec.get("folder"):
            path, _ = khipu_made(rec.get("cli"), rec.get("folder"))
        if rec.get("folder") and path is None:
            state = COULDNT_CHECK  # no longer the folder Khipu made: never asked about
        elif rec.get("cli") not in CLIS:
            state = COULDNT_CHECK
        else:
            try:
                state = _signed_in(rec["cli"], path, check)
            except Exception:  # a failed check is "couldn't check", never a crash of the dialog
                state = COULDNT_CHECK
        if state == SIGNED_IN and path is not None:
            _mark_signed_in(rec["cli"], path)
        _end_attempt(rec)
        return {"ok": True, "attempt": _public(rec), "state": state}
    finally:
        usage._release(fd)


def cancel(attempt_id: str | None, *, remove_folder: bool = False, cli: str | None = None,
           folder: str | None = None, ps=None, killpg=None) -> dict:
    """Stop and cancel: end the attempt's own login (only if its process is
    still the recorded one). With ``remove_folder`` (the owner's Cancel), a
    folder Khipu made that never signed in is removed if it is still empty.

    The owner's Cancel can come after the attempt already ended (failed,
    timed out) or never got one (no code): then ``cli`` and ``folder`` (the
    app passes the folder Khipu returned at creation) name the folder, and
    only that removal runs, under the same lock and the same checks."""
    fd = _lock()
    if fd is None:
        return {"ok": False, "error": E_BUSY}
    try:
        rec = _attempt()
        if rec is None or not attempt_id or rec.get("id") != attempt_id:
            if not (remove_folder and cli and folder):
                return {"ok": False, "error": E_NO_ATTEMPT}
            # Never take a folder from under a login that is still running in it.
            in_use = rec is not None and rec.get("cli") == cli and rec.get("folder") == folder
            return {"ok": True, "state": CANCELLED, "stopped": None,
                    "folder_removed": False if in_use else remove_unused_folder(cli, folder)}
        result = kill_if_ours(rec, ps=ps, killpg=killpg)
        _end_attempt(rec)
        out = {"ok": True, "attempt": _public(rec), "state": CANCELLED, "stopped": result}
        if remove_folder and rec.get("folder"):
            out["folder_removed"] = remove_unused_folder(rec["cli"], rec["folder"])
        return out
    finally:
        usage._release(fd)


def remove_unused_folder(cli: str, folder: str) -> bool:
    """Remove a folder Khipu made that never signed in; ``rmdir`` refuses a
    folder that holds anything, so nothing inside is ever looked at."""
    path, _ = khipu_made(cli, folder)
    if path is None or _ever_signed_in(cli, path):
        return False
    try:
        os.rmdir(path)
    except OSError:
        return False
    _save_folders([m for m in _folders() if not (m.get("cli") == cli and m.get("name") == path.name)])
    return True
