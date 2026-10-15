"""Finding the login folders Khipu could track (settings clarity phase 9, 9A).

Scope: docs/plans/2026-10-15-settings-clarity.md, "Phase 9", sections 3
(Discovery), 10 (login paths retention) and 11 (the agents' view), and
decision 20. Per Mac; nothing here reaches the hub or an MCP tool.

Sources, exactly:

1. Folder names in the home folder: direct children of ``$HOME`` only whose
   name matches ``NAME_RE`` (backups and logs skipped), directories only.
2. The login folder each conversation ran under: ``login_home`` in the
   capture hook's session state, else derived from ``transcript_path``;
   only for sessions seen within the 30-day retention and after the last
   Forget (``session_capture.login_homes_forgotten_at``). The capture state
   lives in Khipu's own folder (``~/.grok/khipu`` by default); that is
   Khipu's data, not the Grok login's, and nothing else under ``~/.grok``
   is read.
3. T3's provider instances (Claude and Codex drivers), ``config.homePath``.
4. Installed CLIs whose default login is neither tracked nor turned down.
   A default folder of a CLI that is not installed is never a candidate
   (Khipu's own capture folder makes ``~/.grok`` exist on every Mac).

Khipu's own process environment is not a source. Every candidate is
de-duplicated by real identity (``claude_homes.path_identity``) against the
tracked accounts, the turned-down folders and the default logins.

Nothing here opens, lists or reads a file inside a login folder: it lists
``$HOME``, stats a folder itself and asks the CLI. The check is a one-shot
child per folder whose argv comes only from ``STATUS_ARGV`` (or
``VERSION_ARGV`` for the can't-run state), or the existing plan reads
(``usage.probe_grok``, ``usage.probe_muse``, ``usage.probe_account`` with
``rollout=False``). Everything a CLI prints is dropped after classification.
Discovery never writes the usage cache.

Decision 20: Claude Code and Codex write into a login folder when asked
whether it is signed in (Grok Build is treated the same until shown
otherwise), so a folder of those CLIs found only by its name is never checked
automatically: it shows as not checked until the owner presses Check
(``discover(check=...)``).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import select
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import MappingProxyType

from khipu import claude_homes, t3, usage

CLIS = ("claude", "codex", "grok", "muse")
FOLDER_CLIS = ("claude", "codex", "grok")
NAME_RE = re.compile(r"^\.(claude|codex|grok)(-[A-Za-z0-9._-]+)?$")
_SKIP_ENDINGS = ("-backup", "-backups", "-logs")
# "-bak", "-bak2", "-bak-old", "-bak.1": a backup; "-bakery" is a name.
_SKIP_BAK = re.compile(r"^-bak(\d+|[-.].*)?$")
# The capture hook's harness names (Grok Build is captured as ``aegis``).
SESSION_CLIS = MappingProxyType({"claude_code": "claude", "codex": "codex", "aegis": "grok"})
T3_DRIVERS = MappingProxyType({t3.CLAUDE_DRIVER: "claude", t3.CODEX_DRIVER: "codex"})

# The only argv discovery builds, by lookup, never from text.
STATUS_ARGV = MappingProxyType({"claude": ("auth", "status", "--json"), "codex": ("login", "status")})
VERSION_ARGV = ("--version",)
# CLIs whose status check writes into the folder it is asked about (decision 20).
WRITES_WHEN_CHECKED = frozenset({"claude", "codex", "grok"})

CHECK_CAP = 12
PARALLEL = 2
CHILD_TIMEOUT_S = 5.0
SIGNED_IN_WAIT_S = 24 * 3600
RETRY_WAIT_S = 24 * 3600
COUNT_WINDOW_S = 7 * 86400
COUNT_CAP = 99
OUTPUT_CAP = 64 * 1024

LOCK_NAME = "usage.discover.lock"
STORE_NAME = "discovery.json"
DECLINED_NAME = "accounts-declined.json"
STORE_VERSION = 1

SIGNED_IN = "signed_in"
NOT_SIGNED_IN = "not_signed_in"
COULDNT_CHECK = "couldnt_check"
NOT_CHECKED = "not_checked"
PENDING = "pending"

# Fixed reason codes (the app words them); no CLI text is ever one.
R_TIMEOUT = "timeout"
R_START = "start_failed"
R_NODE = "node_missing"
R_NOT_INSTALLED = "not_installed"
R_BAD = "bad_reply"
R_INTERNAL = "internal_error"

MUSE_IDENTITY = "default"
_VERSION_RE = re.compile(rb"(\d+\.\d+(?:\.\d+)*)")


class RefusedArgv(ValueError):
    """An argv outside ``STATUS_ARGV`` / ``VERSION_ARGV``: never run."""


# ---- paths -------------------------------------------------------------------


def _home() -> Path:
    raw = os.environ.get("HOME")
    return Path(raw) if raw else Path.home()


def _expand(raw) -> Path | None:
    """An absolute path from a configured or recorded value; ``~`` is
    ``$HOME``. A relative value has no anchor and is skipped."""
    if not isinstance(raw, str) or not raw.strip() or not raw.isprintable():
        return None
    raw = raw.strip()
    if raw == "~":
        return _home()
    if raw.startswith("~/"):
        return _home() / raw[2:]
    path = Path(raw)
    return path if path.is_absolute() else None


def display_path(path) -> str:
    """``~/...`` under the home folder, else the absolute path."""
    p = os.path.normpath(str(path))
    h = os.path.normpath(str(_home()))
    if p == h:
        return "~"
    if p.startswith(h.rstrip(os.sep) + os.sep):
        return "~/" + p[len(h.rstrip(os.sep)) + 1:]
    return p


def default_folder(cli: str) -> Path | None:
    return _home() / f".{cli}" if cli in FOLDER_CLIS else None


def identity(path) -> str:
    """``claude_homes.path_identity`` as text (the inode when the folder
    exists, the resolved path otherwise)."""
    ident = claude_homes.path_identity(path)
    if ident[0] == "inode":
        return f"inode:{ident[1]}:{ident[2]}"
    return f"path:{ident[1]}"


def _is_dir(path) -> bool:
    """A stat of the folder itself (symlinks followed); nothing inside it."""
    try:
        return stat.S_ISDIR(os.stat(path).st_mode)
    except (OSError, ValueError):
        return False


def name_match(name: str) -> str | None:
    """The CLI a home-folder child named ``name`` belongs to, or None (not a
    login folder name, or a backup or logs folder)."""
    m = NAME_RE.match(name)
    if not m:
        return None
    suffix = m.group(2) or ""
    if suffix.endswith(_SKIP_ENDINGS) or _SKIP_BAK.match(suffix):
        return None
    return m.group(1)


def default_name(cli: str, path, *, default: bool) -> str | None:
    """"Claude client" for ``~/.claude-client``, "Claude" for the default
    login, "Claude <folder>" elsewhere; None when the folder is in a private
    place or its name looks like a secret (the owner names it instead)."""
    label = usage.LABELS[cli]
    if default or path is None:
        return label
    folder = os.path.basename(os.path.normpath(str(path)))
    m = NAME_RE.match(folder)
    name = f"{label} {m.group(2)[1:]}" if m and m.group(1) == cli and m.group(2) else f"{label} {folder}"
    from khipu.redact import redact_secrets
    from khipu.routing import credential_location

    if credential_location(str(path)) or redact_secrets(name, force=True)[1] \
            or redact_secrets(str(path), force=True)[1]:
        return None
    if not name.isprintable() or len(name) > usage.LABEL_MAX:
        return None
    return name


# ---- files Khipu keeps (per Mac, next to the usage cache) -------------------------


def store_file() -> Path:
    return usage.cache_file().with_name(STORE_NAME)


def declined_file() -> Path:
    return usage.cache_file().with_name(DECLINED_NAME)


# Discovery's own files sit in Khipu's data folder (``state/`` next to the
# usage cache). Each is reached through descriptors with O_NOFOLLOW below the
# data folder, so a ``state`` folder or file swapped for a symlink can never
# redirect a read or a write into a login folder; T3's settings likewise below
# the home folder.
_O_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
READ_MAX = 16 * 1024 * 1024


@contextlib.contextmanager
def _dir_fd(base: Path, parts: tuple[str, ...], *, create: bool = False):
    """A descriptor for ``base/parts...``, following ``base`` itself but no
    symlink in ``parts``; None when missing or a step is a symlink."""
    fds: list[int] = []
    try:
        try:
            if create:
                base.mkdir(parents=True, exist_ok=True)
            fds.append(os.open(base, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)))
            for part in parts:
                if create:
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(part, 0o700, dir_fd=fds[-1])
                fds.append(os.open(part, _O_DIR, dir_fd=fds[-1]))
        except OSError:
            yield None
            return
        yield fds[-1]
    finally:
        for fd in fds:
            os.close(fd)


def _read_at(dfd: int | None, name: str) -> str | None:
    if dfd is None:
        return None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > READ_MAX:
            return None
        data = b""
        while len(data) <= READ_MAX:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            data += chunk
        return data.decode("utf-8") if len(data) <= READ_MAX else None
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)


def _data_state(*, create: bool = False):
    """The descriptor for Khipu's ``state`` folder beside the usage cache."""
    return _dir_fd(usage.cache_file().parent.parent, (usage.cache_file().parent.name,), create=create)


def read_nofollow(base: Path, parts: tuple[str, ...]) -> str | None:
    """``base/parts...`` as text, with no symlink below ``base``."""
    with _dir_fd(base, parts[:-1]) as dfd:
        return _read_at(dfd, parts[-1])


def _load_store() -> dict:
    with _data_state() as dfd:
        text = _read_at(dfd, STORE_NAME)
    try:
        data = json.loads(text) if text is not None else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
        data = {"version": STORE_VERSION}
    if not isinstance(data.get("candidates"), dict):
        data["candidates"] = {}
    data["candidates"] = {k: v for k, v in data["candidates"].items() if isinstance(v, dict)}
    return data


def _save_store(data: dict) -> None:
    text = json.dumps(data, indent=2, sort_keys=True)
    with _data_state(create=True) as dfd:
        if dfd is None:
            return
        tmp = f"{STORE_NAME}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            try:
                os.write(fd, text.encode("utf-8"))
            finally:
                os.close(fd)
            os.rename(tmp, STORE_NAME, src_dir_fd=dfd, dst_dir_fd=dfd)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dfd)


def turned_down() -> list[dict]:
    """The folders the owner chose Not this one for, or stopped tracking
    (``[{cli, path, identity, why, at}]``). 9B writes this file; a missing or
    corrupt one is an empty list."""
    with _data_state() as dfd:
        text = _read_at(dfd, DECLINED_NAME)
    try:
        data = json.loads(text) if text is not None else None
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and e.get("cli") in CLIS]


def _declined_keys() -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for entry in turned_down():
        cli = entry["cli"]
        if cli == "muse":
            keys.add((cli, MUSE_IDENTITY))
            continue
        if isinstance(entry.get("identity"), str) and entry["identity"]:
            keys.add((cli, entry["identity"]))
        path = _expand(entry.get("path")) if entry.get("path") else default_folder(cli)
        if path is not None:
            keys.add((cli, identity(path)))
    return keys


def _tracked_keys() -> set[tuple[str, str]]:
    """Valid configured accounts; a home-less one stands for its CLI's
    default folder."""
    keys: set[tuple[str, str]] = set()
    for account in usage.configured_accounts():
        if account.get("invalid"):
            continue
        cli = account["harness"]
        if cli == "muse":
            keys.add((cli, MUSE_IDENTITY))
            continue
        home = account.get("home")
        path = usage._home_path(home) if home else default_folder(cli)
        if path is not None:
            keys.add((cli, identity(path)))
    return keys


# ---- sources -----------------------------------------------------------------------


def _source_names() -> list[tuple[str, Path]]:
    """Source 1: (cli, path) for each matching direct child of ``$HOME``
    that is a directory, real folders before symlinks."""
    home = _home()
    found: list[tuple[bool, str, str, Path]] = []
    try:
        with os.scandir(home) as entries:
            for entry in entries:
                cli = name_match(entry.name)
                if cli is None:
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=True):
                        continue
                    link = entry.is_symlink()
                except OSError:
                    continue
                found.append((link, entry.name, cli, home / entry.name))
    except OSError:
        return []
    return [(cli, path) for _link, _name, cli, path in sorted(found)]


def _transcript_folder(cli: str, transcript) -> Path | None:
    """The login folder a transcript path sits in, by its path alone."""
    if not isinstance(transcript, str) or not transcript.strip():
        return None
    path = Path(transcript)
    if not path.is_absolute():
        return None
    marker = "projects" if cli == "claude" else "sessions"
    for parent in path.parents:
        if parent.name == marker:
            folder = parent.parent
            if cli == "grok" and not folder.name.startswith(".grok"):
                return None
            return folder
    return None


def _seen(state: dict) -> float:
    try:
        seen = float(state.get("seen_ts") or 0)
    except (TypeError, ValueError):
        return 0.0
    return seen if math.isfinite(seen) else 0.0


def session_folder(cli: str, state: dict, *, now_ts: float | None = None,
                   forgotten_at: float | None = None) -> Path | None:
    """Source 2 for one session state: its recorded ``login_home``, else the
    folder derived from its ``transcript_path``. Nothing for a session last
    seen more than ``LOGIN_HOME_KEEP_S`` ago or at or before the last Forget
    (``forgotten_at``): retention and Forget make discovery forget too."""
    from khipu import session_capture as sc

    now_ts = time.time() if now_ts is None else now_ts
    seen = _seen(state)
    if now_ts - seen > sc.LOGIN_HOME_KEEP_S:
        return None
    if forgotten_at is not None and seen <= forgotten_at:
        return None
    recorded = state.get(sc.LOGIN_HOME_FIELD)
    if isinstance(recorded, str) and recorded.strip():
        return _expand(recorded)
    return _transcript_folder(cli, state.get("transcript_path"))


def _source_sessions(now_ts: float) -> list[tuple[str, Path, int]]:
    """Source 2: (cli, folder, conversations in the last 7 days) for every
    folder a saved session ran under that exists as a directory."""
    from khipu import session_capture as sc

    folders: dict[tuple[str, str], list] = {}
    is_dir: dict[str, bool] = {}
    forgotten_at = sc.login_homes_forgotten_at()
    for name, state in sc.iter_states():
        prefix, sep, _rest = name.partition("--")
        cli = SESSION_CLIS.get(prefix)
        if not sep or cli is None:
            continue
        folder = session_folder(cli, state, now_ts=now_ts, forgotten_at=forgotten_at)
        if folder is None:
            continue
        text = str(folder)
        if text not in is_dir:
            is_dir[text] = _is_dir(folder)
        if not is_dir[text]:
            continue
        slot = folders.setdefault((cli, text), [folder, 0])
        if not state.get("subagent") and 0 <= now_ts - _seen(state) <= COUNT_WINDOW_S:
            slot[1] += 1
    return [(cli, folder, min(count, COUNT_CAP)) for (cli, _t), (folder, count) in folders.items()]


def _source_t3() -> list[tuple[str, Path, str]]:
    """Source 3: (cli, folder, instance name) for T3's Claude and Codex
    instances; an empty ``homePath`` is the default login."""
    out = []
    home = _home()
    rel = t3.settings_path(home).relative_to(home).parts
    text = read_nofollow(home, rel)
    for inst in t3.provider_instances(None, tuple(T3_DRIVERS), text=text):
        cli = T3_DRIVERS[inst["driver"]]
        folder = _expand(inst["home_path"]) if inst["home_path"] else default_folder(cli)
        if folder is not None and _is_dir(folder):
            out.append((cli, folder, inst["name"]))
    return out


def _gather(now_ts: float) -> tuple[dict, set, set]:
    """Every candidate from sources 1 to 4, keyed by id, minus the tracked
    and turned-down ones; returns (candidates, tracked keys, declined keys)."""
    tracked, declined = _tracked_keys(), _declined_keys()
    defaults = {cli: identity(default_folder(cli)) for cli in FOLDER_CLIS}
    installed = {cli: bool(usage._command(cli)) for cli in CLIS}
    found: dict[str, dict] = {}

    def add(cli: str, path: Path | None, source: str, *, count: int | None = None) -> None:
        ident = identity(path) if path is not None else MUSE_IDENTITY
        is_default = path is None or defaults.get(cli) == ident
        if (cli, ident) in tracked or (cli, ident) in declined:
            return
        if is_default and not installed[cli]:
            return  # a default login only for an installed CLI (source 4)
        cid = f"{cli}:{ident}"
        rec = found.get(cid)
        if rec is None:
            shown = default_folder(cli) if is_default and path is not None else path
            rec = {"id": cid, "cli": cli, "identity": ident, "default": is_default,
                   "abs": str(shown) if shown is not None else None,
                   "path": display_path(shown) if shown is not None else None,
                   "sources": [], "conversations_7d": None}
            found[cid] = rec
        if source not in rec["sources"]:
            rec["sources"].append(source)
        if count is not None:
            rec["conversations_7d"] = min(COUNT_CAP, (rec["conversations_7d"] or 0) + count)

    for cli, path in _source_names():
        add(cli, path, "name")
    for cli, path, count in _source_sessions(now_ts):
        add(cli, path, "conversation", count=count)
    for cli, path, _name in _source_t3():
        add(cli, path, "t3")
    for cli in CLIS:
        if installed[cli]:
            add(cli, default_folder(cli), "installed")
    for rec in found.values():
        rec["name"] = default_name(rec["cli"], rec["abs"], default=rec["default"])
    return found, tracked, declined


def needs_owner_check(rec: dict) -> bool:
    """Decision 20: a Claude, Codex or Grok folder that no conversation ran
    on, T3 does not name and is not the default login waits for Check."""
    return (rec["cli"] in WRITES_WHEN_CHECKED and not rec.get("default")
            and not ({"conversation", "t3"} & set(rec.get("sources") or ())))


# ---- the one-shot runner -------------------------------------------------------------


def argv_allowed(cli: str, argv) -> bool:
    """True only for ``(binary, *STATUS_ARGV[cli])`` and ``(binary,
    *VERSION_ARGV)``: exact tuples, nothing added or left off."""
    if not isinstance(argv, tuple) or len(argv) < 2:
        return False
    binary, rest = argv[0], argv[1:]
    if not isinstance(binary, str) or not binary or not os.path.isabs(binary):
        return False
    return rest == VERSION_ARGV or (cli in STATUS_ARGV and rest == STATUS_ARGV[cli])


def run_once(cli: str, argv, *, home: str | None = None, timeout: float | None = None,
             run: "usage._Run | None" = None) -> dict:
    """Run one allowed argv as a child with no stdin, no terminal, the probe
    environment, a fresh empty temp cwd and a deadline; kill its whole
    process group at the deadline and always at the end. Returns
    ``{outcome: exited|timeout|start_failed, code, out, err}`` with the
    output capped; callers classify it and drop it."""
    argv = tuple(argv) if isinstance(argv, (list, tuple)) else argv
    if not argv_allowed(cli, argv):
        raise RefusedArgv(f"discovery runs only its fixed argv for {cli}")
    timeout = CHILD_TIMEOUT_S if timeout is None else timeout
    env = usage.probe_environment(cli, home, argv[0])
    with tempfile.TemporaryDirectory(prefix="khipu-discover-") as cwd:
        try:
            proc = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, cwd=cwd, env=env, start_new_session=True)
        except OSError:
            return {"outcome": "start_failed", "code": None, "out": b"", "err": b""}
        if run is not None:
            run.add(proc)
        deadline = time.monotonic() + timeout
        bufs = {proc.stdout.fileno(): bytearray(), proc.stderr.fileno(): bytearray()}
        out_fd, err_fd = proc.stdout.fileno(), proc.stderr.fileno()
        live = set(bufs)
        outcome, code = "exited", None
        try:
            while live:
                left = deadline - time.monotonic()
                if left <= 0:
                    outcome = "timeout"
                    break
                ready, _, _ = select.select(list(live), [], [], min(left, 0.25))
                for fd in ready:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        live.discard(fd)
                        continue
                    room = OUTPUT_CAP - len(bufs[fd])
                    if room > 0:
                        bufs[fd] += chunk[:room]
                if not ready and proc.poll() is not None:
                    break  # exited; a grandchild may still hold a pipe
            if outcome == "exited":
                try:
                    code = proc.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    outcome = "timeout"
        finally:
            usage._kill(proc)
            with contextlib.suppress(Exception):
                proc.stderr.close()
            if run is not None:
                run.discard(proc)
        return {"outcome": outcome, "code": code, "out": bytes(bufs[out_fd]), "err": bytes(bufs[err_fd])}


def _failure(res: dict) -> str:
    if res["outcome"] == "timeout":
        return R_TIMEOUT
    if res["outcome"] == "start_failed":
        return R_START
    if res["code"] == 127:
        return R_NODE
    return usage._exit_code(res["code"]) if res["code"] else R_BAD


def classify_status(cli: str, res: dict) -> tuple[str, str | None]:
    """(state, reason) from one status run; the CLI's text is read here only
    to choose a fixed result and is then dropped."""
    if res["outcome"] != "exited":
        return COULDNT_CHECK, _failure(res)
    if cli == "claude":
        try:
            data = json.loads(res["out"].decode("utf-8", "replace"))
        except ValueError:
            data = None
        logged = data.get("loggedIn") if isinstance(data, dict) else None
        if isinstance(logged, bool):
            return (SIGNED_IN, None) if logged else (NOT_SIGNED_IN, None)
        return COULDNT_CHECK, _failure(res)
    if cli == "codex":
        if res["code"] == 0:
            return SIGNED_IN, None
        text = (res["out"] + b"\n" + res["err"]).decode("utf-8", "replace")
        if res["code"] and res["code"] > 0 and usage._SIGNIN.search(text):
            return NOT_SIGNED_IN, None
        return COULDNT_CHECK, _failure(res)
    return COULDNT_CHECK, R_BAD


def _from_probe(data: dict) -> tuple[str, str | None, str | None]:
    if data.get("ok"):
        return SIGNED_IN, None, data.get("plan")
    code = data.get("error") or R_BAD
    if code == usage.E_SIGNIN:
        return NOT_SIGNED_IN, None, None
    return COULDNT_CHECK, code, None


class _CutOff(Exception):
    """A check whose child timed out on the run's overall deadline rather
    than its own ``CHILD_TIMEOUT_S``: left for later, never stamped."""


_TIMEOUT_CODES = frozenset({R_TIMEOUT, usage.E_TIMEOUT})


def _check_one(rec: dict, *, run: "usage._Run", end: float) -> tuple[str, str | None, str | None]:
    """(state, reason, plan) for one candidate. Raises ``_CutOff`` when the
    status check timed out only because the run's deadline cut its time."""
    cli = rec["cli"]
    home = None if rec.get("default") else rec.get("abs")
    clipped = [False]

    def left() -> float:
        remaining = end - time.monotonic()
        if remaining < CHILD_TIMEOUT_S:
            clipped[0] = True
        return max(0.1, min(CHILD_TIMEOUT_S, remaining))

    def settle(state: str, reason: str | None, plan: str | None = None) -> tuple[str, str | None, str | None]:
        if state == COULDNT_CHECK and reason in _TIMEOUT_CODES and clipped[0]:
            raise _CutOff
        return state, reason, plan

    binary = usage._command(cli)
    if not binary:
        return COULDNT_CHECK, R_NOT_INSTALLED, None
    if cli in STATUS_ARGV:
        res = run_once(cli, (binary, *STATUS_ARGV[cli]), home=home, timeout=left(), run=run)
        state, reason, _ = settle(*classify_status(cli, res))
        del res
        plan = None
        if state == SIGNED_IN and end - time.monotonic() > 0.5:
            row = usage.probe_account({"label": usage.LABELS[cli], "harness": cli, "home": home},
                                      timeout=left(), rollout=False)
            plan = row.get("plan") if row.get("ok") else None
        return state, reason, plan
    if cli == "grok":
        return settle(*_from_probe(usage.probe_grok(home, timeout=left())))
    if cli == "muse":
        return settle(*_from_probe(usage.probe_muse(timeout=left())))
    return COULDNT_CHECK, R_BAD, None


def _run_checks(recs: list[dict], now_ts: float) -> dict:
    """Check ``recs`` in order, at most ``PARALLEL`` children at a time, under
    one overall deadline (``CHILD_TIMEOUT_S`` + ``usage.OVERALL_GRACE_S``) that
    kills every child still running. Updates each checked record; a folder
    the overall deadline cut off keeps its previous result, is counted as
    left for later and is marked ``cut_off`` so it goes first next run.
    Returns the counts."""
    stats = {"checked": 0, "couldnt_check": 0, "left_for_later": 0}
    if not recs:
        return stats
    run = usage._Run()
    queue = list(recs)
    lock = threading.Lock()
    results: dict[str, tuple] = {}
    started: set[str] = set()
    closed = [False]
    end = time.monotonic() + CHILD_TIMEOUT_S + usage.OVERALL_GRACE_S

    def worker() -> None:
        usage._local.run = run
        try:
            while True:
                with lock:
                    if closed[0] or not queue or end - time.monotonic() < 0.5:
                        return
                    rec = queue.pop(0)
                    started.add(rec["id"])
                try:
                    result = _check_one(rec, run=run, end=end)
                except _CutOff:
                    continue  # begun, never finished: left for later
                except Exception:  # noqa: BLE001 — one folder never breaks the run
                    result = (COULDNT_CHECK, R_INTERNAL, None)
                with lock:
                    if not closed[0]:
                        results[rec["id"]] = result
        finally:
            usage._local.run = None

    threads = [threading.Thread(target=worker, daemon=True, name="khipu-discover")
               for _ in range(min(PARALLEL, len(queue)))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(0.0, end - time.monotonic()))
    with lock:
        closed[0] = True
        done, begun = dict(results), set(started)
    run.kill_all()
    for rec in recs:
        if rec["id"] not in done:
            if rec["id"] in begun:
                rec["cut_off"] = True
            stats["left_for_later"] += 1
            continue
        state, reason, plan = done[rec["id"]]
        rec.pop("cut_off", None)
        rec.update(state=state, reason=reason, plan=plan, last_checked=now_ts)
        stats["checked"] += 1
        if state == COULDNT_CHECK:
            stats["couldnt_check"] += 1
    return stats


# ---- discovery -----------------------------------------------------------------------


def _due(rec: dict, now_ts: float, force: bool) -> bool:
    last = rec.get("last_checked")
    if force or not isinstance(last, (int, float)):
        return True
    wait = SIGNED_IN_WAIT_S if rec.get("state") == SIGNED_IN else RETRY_WAIT_S
    return now_ts - last >= wait


def _order(rec: dict) -> tuple:
    """A folder the last run's deadline cut off first, then folders used by
    conversations, then never-checked folders newest first seen, then the
    least recently checked: so every candidate is checked within
    ceil(n / CHECK_CAP) runs."""
    last = rec.get("last_checked")
    never = not isinstance(last, (int, float))
    return (not rec.get("cut_off"), "conversation" not in rec["sources"], not never,
            -float(rec.get("first_seen") or 0) if never else float(last))


def _iso(ts) -> str | None:
    return usage._iso(ts) if isinstance(ts, (int, float)) else None


def public(rec: dict) -> dict:
    """One candidate as the app sees it (never an agent: it carries a path)."""
    owner = needs_owner_check(rec)
    state = rec.get("state") or (NOT_CHECKED if owner else PENDING)
    sources = sorted(rec.get("sources") or ())
    return {
        "id": rec["id"], "cli": rec["cli"], "cli_name": usage.CLI_NAMES[rec["cli"]], "path": rec.get("path"),
        "default": bool(rec.get("default")), "sources": sources, "name": rec.get("name"),
        "first_seen": _iso(rec.get("first_seen")), "last_checked": _iso(rec.get("last_checked")),
        "state": state, "reason": rec.get("reason") if state == COULDNT_CHECK else None,
        "plan": rec.get("plan") if state == SIGNED_IN else None,
        "conversations_7d": rec.get("conversations_7d"),
        "needs_owner_check": owner,
        # Home's Found card lists only signed-in finds a conversation used.
        "offer_on_home": state == SIGNED_IN and "conversation" in sources,
    }


def _view(store: dict) -> dict:
    cands = sorted(store["candidates"].values(),
                   key=lambda r: ("conversation" not in (r.get("sources") or ()), r.get("cli") or "",
                                  r.get("path") or ""))
    last = store.get("last_run") if isinstance(store.get("last_run"), dict) else None
    return {"ok": True, "candidates": [public(r) for r in cands],
            "last_run": dict(last, at=_iso(last.get("at"))) if last else None}


def _try_lock() -> int | None:
    with _data_state(create=True) as dfd:
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


def _resolve_check(target: str, cands: dict, tracked: set, declined: set, now_ts: float) -> tuple[dict | None, str | None]:
    """The candidate an owner's Check names: a candidate's id or path, or a
    ``~/`` or absolute path to an existing folder whose name is a login
    folder's name."""
    target = (target or "").strip()
    if target in cands:
        cands[target]["owner_checked"] = True
        return cands[target], None
    path = _expand(target)
    if path is None:
        return None, "bad_folder"
    if not _is_dir(path):
        return None, "folder_not_found"
    ident = identity(path)
    for rec in cands.values():
        if rec["identity"] == ident:
            rec["owner_checked"] = True
            return rec, None
    cli = name_match(os.path.basename(os.path.normpath(str(path))))
    if cli is None:
        return None, "not_a_login_folder"
    if (cli, ident) in tracked:
        return None, "already_tracked"
    if (cli, ident) in declined:
        return None, "turned_down"
    is_default = identity(default_folder(cli)) == ident
    shown = default_folder(cli) if is_default else path
    rec = {"id": f"{cli}:{ident}", "cli": cli, "identity": ident, "default": is_default, "abs": str(shown),
           "path": display_path(shown), "sources": ["owner"], "conversations_7d": None,
           "first_seen": now_ts, "name": default_name(cli, str(shown), default=is_default),
           "owner_checked": True}
    cands[rec["id"]] = rec
    return rec, None


def discover(*, now: bool = False, check: str | None = None, at: float | None = None) -> dict:
    """One discovery run: gather the candidates, check what is due (or, with
    ``check``, only that one folder, ignoring the wait), keep the result and
    return it. A run while another holds the lock checks nothing and returns
    the last stored result with ``looking: true``."""
    fd = _try_lock()
    if fd is None:
        out = _view(_load_store())
        out["looking"] = True
        if check is not None:
            out["check"] = {"ok": False, "error": "looking"}
        return out
    try:
        now_ts = time.time() if at is None else at
        store = _load_store()
        previous = store["candidates"]
        found, tracked, declined = _gather(now_ts)
        # An owner-checked folder outside the sources stays while it exists,
        # as the owner's only: no conversation source or count carries over.
        for cid, old in previous.items():
            if cid in found or "owner" not in (old.get("sources") or ()):
                continue
            key = (old.get("cli"), old.get("identity"))
            if old.get("cli") in FOLDER_CLIS and old.get("abs") and _is_dir(old["abs"]) \
                    and identity(old["abs"]) == old.get("identity") and key not in tracked and key not in declined:
                found[cid] = {k: old.get(k) for k in ("id", "cli", "identity", "default", "abs", "path", "name")}
                found[cid].update(sources=["owner"], conversations_7d=None)
        for cid, rec in found.items():
            old = previous.get(cid) or {}
            first = old.get("first_seen")
            rec["first_seen"] = first if isinstance(first, (int, float)) else now_ts
            if old.get("owner_checked"):
                rec["owner_checked"] = True
            if old.get("cut_off"):
                rec["cut_off"] = True
            # A folder no longer backed by a session or T3 falls back to
            # decision 20's not checked unless the owner checked it.
            if needs_owner_check(rec) and not rec.get("owner_checked"):
                continue
            for key in ("last_checked", "state", "reason", "plan"):
                rec[key] = old.get(key)
        store["candidates"] = found  # a folder no source finds any more is pruned
        result = None
        if check is not None:
            rec, error = _resolve_check(check, found, tracked, declined, now_ts)
            if error:
                result = {"ok": False, "error": error}
            else:
                stats = _run_checks([rec], now_ts)
                result = {"ok": bool(stats["checked"]), "candidate": public(rec)}
                if not stats["checked"]:
                    result["error"] = R_TIMEOUT
        else:
            auto = [r for r in found.values() if not needs_owner_check(r)]
            due = sorted((r for r in auto if _due(r, now_ts, now)), key=_order)
            stats = _run_checks(due[:CHECK_CAP], now_ts)
            stats["left_for_later"] += max(0, len(due) - CHECK_CAP)
            stats["not_checked"] = sum(1 for r in found.values() if needs_owner_check(r))
            store["last_run"] = dict(stats, at=now_ts)
        _save_store(store)
        out = _view(store)
        out["looking"] = False
        if result is not None:
            out["check"] = result
        return out
    finally:
        usage._release(fd)


# ---- the CLIs themselves ---------------------------------------------------------------


def _cli_state(cli: str, run: "usage._Run") -> dict:
    binary = usage._command(cli)
    out = {"cli": cli, "cli_name": usage.CLI_NAMES[cli], "path": binary, "state": "not_installed",
           "version": None, "reason": None}
    if not binary:
        return out
    res = run_once(cli, (binary, *VERSION_ARGV), timeout=CHILD_TIMEOUT_S, run=run)
    if res["outcome"] == "exited" and res["code"] == 0:
        m = _VERSION_RE.search(res["out"])
        out.update(state="installed", version=m.group(1).decode() if m else None)
    elif res["outcome"] == "start_failed":
        out.update(state="cant_run", reason="cant_start")
    else:
        out.update(state="cant_run", reason=_failure(res))
    return out


def cli_states() -> dict:
    """Per CLI: not installed, installed (with its version) or can't run
    (with a fixed reason), from ``<cli> --version`` in the probe environment;
    and where Khipu found it. For the app only (it carries paths)."""
    run = usage._Run()
    results: dict[str, dict] = {}

    def work(cli: str) -> None:
        try:
            results[cli] = _cli_state(cli, run)
        except Exception:  # noqa: BLE001
            results[cli] = {"cli": cli, "cli_name": usage.CLI_NAMES[cli], "path": None, "state": "cant_run",
                            "version": None, "reason": R_INTERNAL}

    queue, lock = list(CLIS), threading.Lock()

    def worker() -> None:
        while True:
            with lock:
                if not queue:
                    return
                cli = queue.pop(0)
            work(cli)

    threads = [threading.Thread(target=worker, daemon=True, name="khipu-cli") for _ in range(PARALLEL)]
    for thread in threads:
        thread.start()
    end = time.monotonic() + CHILD_TIMEOUT_S + usage.OVERALL_GRACE_S
    for thread in threads:
        thread.join(max(0.0, end - time.monotonic()))
    run.kill_all()
    return {cli: results.get(cli) or {"cli": cli, "cli_name": usage.CLI_NAMES[cli], "path": None,
                                      "state": "cant_run", "version": None, "reason": R_TIMEOUT}
            for cli in CLIS}


# ---- one read of an unsaved account ------------------------------------------------------


def try_account(harness: str, folder: str | None = None, *, timeout: float = usage.TIMEOUT_S) -> dict:
    """One plan read of an account that is not saved (the dialog's step 3).
    Never falls back to files inside the folder and never writes the usage
    cache."""
    harness = (harness or "").strip().lower()
    if harness not in usage.HARNESSES:
        return {"ok": False, "error": "unknown_cli"}
    home = None
    if folder:
        if harness == "muse":
            return {"ok": False, "error": "muse_folder"}
        path = _expand(folder)
        if path is None:
            return {"ok": False, "error": "bad_folder"}
        if not _is_dir(path):
            return {"ok": False, "error": "folder_not_found"}
        home = None if identity(path) == identity(default_folder(harness)) else str(path)
    row = usage.probe_account({"label": usage.LABELS[harness], "harness": harness, "home": home},
                              timeout=timeout, rollout=False)
    return {"ok": bool(row.get("ok")), "default": home is None, "row": row}
