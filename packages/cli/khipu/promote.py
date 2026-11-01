"""Promotion gate: merged is not live until ``khipu promote`` says so.

Scope: docs/plans/2026-10-14-promotion-gate.md. Two git worktrees of the
repository, ``~/.config/khipu/live-a`` and ``live-b``, each pinned to a commit,
and one symlink, ``~/.config/khipu/live``, naming the one every launcher,
launchd job and MCP server runs. ``promote`` checks the inactive slot out at a
ref, runs the live gate against it (four steps, each its own function returning
``{"ok", "ms", "detail"}``), and only on a pass flips the link; ``rollback``
flips back with no gate. Every run is a line in ``state/promotions.jsonl``.

Nothing here runs on import. A machine with no ``live`` link is unaffected:
``khipu integrations install`` and ``khipu jobs install`` keep pointing at the
checkout they run from until a link exists.
"""
from __future__ import annotations

import ast
import fcntl
import getpass
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from khipu.lines import read_lines, split_lines

LIVE_NAME = "live"
SLOT_NAMES = ("live-a", "live-b")
GATE_ORDER = ("doctor", "recall", "capture", "version")
# The steps that need the hub. After an outage they wait for it (bounded by
# the owner's `promote_hub_wait_minutes`) instead of refusing; a refusal that
# is only the hub being out of reach queues the promotion, and the next
# queue-drain run retries it once the hub answers
# (docs/plans/2026-10-16-offline-and-self-healing.md, E).
HUB_STEPS = ("recall", "capture")
HUB_WAIT_KEY = "promote_hub_wait_minutes"
HUB_POLL_S = 10.0
HUB_PROBE_TIMEOUT_S = 3
# The owner's one-time override ("Update anyway"): red doctor checks the next
# promotion skips once. It is named in that run's record, then clears itself,
# and lapses unused after OVERRIDE_TTL_S.
GATE_IGNORE_KEY = "promote_gate_ignore"
OVERRIDE_TTL_S = 24 * 60 * 60
# Never part of the doctor step's red list (gate_doctor skips them).
_DOCTOR_NOT_GATED = ("jobs_ok", "synth_provider_ok")
# launchd starts a job with this PATH and nothing else (no plist sets one).
LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
RECALL_BUDGET_MS = 1200.0
# A replayed run over the budget fails only when it is also this much slower
# than the run it is compared with (live's, or its own routing-off run).
SLOWNESS_SLACK_MS = 200.0
REPLAY_WINDOW_S = 24 * 60 * 60
REPLAY_TAIL_BYTES = 2 * 1024 * 1024
GATE_HARNESS = "claude_code"
MCP_NOTE = "Running MCP servers keep the old code until their session restarts."
# Every environment the gate hands a subprocess is built from these names
# only: the gate never reads a variable whose name holds KEY or TOKEN, and no
# credential in the caller's environment reaches a hook. Hooks find their keys
# the way a live hook does (Keychain, key files).
BASE_ENV_KEYS = ("HOME", "USER", "LOGNAME", "SHELL", "PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
                 "KHIPU_PYTHON")
# The caller's data dir: everything but an isolated run's own state lives
# there, and an isolated run still reads its credentials from it.
DATA_DIR_ENV_KEYS = ("KHIPU_DATA_DIR", "ALZY_DATA_DIR")
# What a candidate needs before its capture may run: the isolated data dir
# (khipu.paths) and the stop hook's gate mode (khipu.hook_background).
_ISOLATION_MARKS = (("packages/cli/khipu/paths.py", 'ISOLATED_ENV = "KHIPU_ISOLATED_DIR"'),
                    ("packages/cli/khipu/hook_background.py", 'os.environ.get("KHIPU_GATE") == "1"'))
_SUMMARY_RE = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \[khipu-prompt-recall\] "
    r"session=(?P<session>\S*) reason=(?P<reason>.*?) ms=(?P<ms>[0-9.]+) "
    r"hits=(?P<hits>\[.*?\]) legs="
)
# The summary line's last field when the routing push is on (phase 3b), and
# the search's degraded note before it.
_ROUTING_FIELD_RE = re.compile(r" routing=(?P<status>\S+)\s*$")
_DEGRADED_RE = re.compile(r" degraded=(?P<degraded>.*?)(?= stage=| offline=| routing=|\s*$)")
_LEGS_RE = re.compile(r" legs=(?P<legs>\[.*?\]) degraded=")
# Routing statuses a candidate's replay with the push on may log and still
# pass: it waited for another fold past its budget (``fold-busy``: on a live
# Mac, the daemon's poll), or the view changed and could not be saved
# (``unsaved``). Every other ``skipped:*`` fails it: the read ran out of its
# budget, the routing code failed, or its child died before it answered
# (``no-answer``), which on a live Mac skips routing on every prompt.
ROUTING_SKIPS_OK = ("skipped:fold-busy", "skipped:unsaved")
# The designed fail-closed withhold: the cold one-shot routing child could not
# build a complete inventory inside its bound. Under load that happens to a
# sound candidate, so it passes with a note unless no routing-on run routed.
ROUTING_WITHHOLD = "skipped:error-InventoryUnavailable"
# The routing record's own files, copied into a replay's isolated dir so the
# candidate reads a record of the live size (never written back).
ROUTING_RECORD_FILES = ("events.jsonl", "view.json")
# Asks the candidate's own code whether its per-prompt push is on in the
# replay's environment (a candidate with no routing push says no), and folds
# the copied record with it first (argv[1] "1"), as the recall daemon's poll
# keeps it folded on a live Mac, so the prompt times the read and not a first
# fold of a record it never saw. Its one line: {"push": true | false}.
_PREFOLD = (
    "import json, sys\n"
    "try:\n"
    "    from khipu import routing\n"
    "except ImportError:\n"
    "    routing = None\n"
    "off = getattr(routing, 'push_off', None)\n"
    "on = off is not None and not off('prompt')\n"
    "if on and sys.argv[1:] == ['1']:\n"
    "    routing.read_view()\n"
    "print(json.dumps({'push': on}))\n"
)
_FIND_EPISODES = (
    "import json, sys\n"
    "from khipu.db import connect\n"
    "with connect() as conn:\n"
    "    with conn.cursor() as cur:\n"
    "        cur.execute('SELECT id FROM episodes WHERE session_id = %s AND deleted_at IS NULL "
    "ORDER BY id', (sys.argv[1],))\n"
    "        print(json.dumps([int(r[0]) for r in cur.fetchall()]))\n"
)
_CODE_FACTS = (
    "import json, khipu\n"
    "from khipu import hub_snapshot as h\n"
    "meta = h.meta_path()\n"
    "disk = None\n"
    "try:\n"
    "    disk = json.loads(meta.read_text(encoding='utf-8')).get('schema_version')\n"
    "except (OSError, ValueError):\n"
    "    pass\n"
    "print(json.dumps({'version': khipu.__version__, 'schema': h.SNAPSHOT_SCHEMA_VERSION,"
    " 'disk_schema': disk, 'replica': str(h.snapshot_path())}))\n"
)


# ---- places ------------------------------------------------------------------


def config_dir() -> Path:
    """``~/.config/khipu``, read at call time."""
    return Path.home() / ".config" / "khipu"


def live_link() -> Path:
    return config_dir() / LIVE_NAME


def live_root() -> Path | None:
    """The ``live`` link (its path, not its target) when it exists and
    resolves to a directory, else None."""
    link = live_link()
    return link if link.is_symlink() and link.is_dir() else None


def slots() -> list[Path]:
    return [config_dir() / name for name in SLOT_NAMES]


def _link_target(link: Path) -> Path | None:
    if not link.is_symlink():
        return None
    target = Path(os.readlink(link))
    return target if target.is_absolute() else link.parent / target


def active_slot() -> Path | None:
    """The slot ``live`` names, or None when there is no link or it names
    something that is not one of the two slots."""
    target = _link_target(live_link())
    if target is None:
        return None
    for slot in slots():
        if os.path.normpath(str(target)) == os.path.normpath(str(slot)):
            return slot
    return None


def inactive_slot() -> Path:
    first, second = slots()
    return second if active_slot() == first else first


def state_file() -> Path:
    return config_dir() / "state" / "promotions.jsonl"


def live_state_file() -> Path:
    """Each slot's last-live commit, written on every successful flip."""
    return config_dir() / "state" / "live-slots.json"


def gate_runs_dir() -> Path:
    """One isolated data dir per gate capture, kept until the hub confirms its
    episode is gone."""
    return config_dir() / "state" / "gate-runs"


def is_promotion_root(root: Path) -> bool:
    """True when ``root`` is the ``live`` link or one of the two slots."""
    norm = os.path.normpath(str(root))
    return any(norm == os.path.normpath(str(p)) for p in (live_link(), *slots()))


def query_log_path() -> Path:
    """Khipu's search log (``khipu.query_log.log_path``: ``query_log.jsonl``
    in the data dir, the config dir unless it was moved)."""
    from khipu.query_log import log_path

    return log_path()


def _repo() -> Path:
    from khipu.paths import repo_root

    return repo_root()


def _live_code_root() -> Path | None:
    """The code the harnesses run right now: the ``live`` link, else the
    checkout the installed ``khipu-mcp`` launcher points into."""
    live = live_root()
    if live is not None:
        return live
    shim = config_dir() / "bin" / "khipu-mcp"
    if shim.is_symlink():
        from khipu.integrations import _launcher_root

        root = _launcher_root(os.readlink(shim))
        if root and Path(root).is_dir():
            return Path(root)
    return None


def _launcher(root: Path, name: str) -> Path:
    return Path(root) / "packages" / "cli" / "bin" / name


def _hook_python() -> str:
    """The interpreter the launchers pick (``KHIPU_PYTHON``, else Homebrew 3.11)."""
    explicit = os.environ.get("KHIPU_PYTHON")
    if explicit and os.access(explicit, os.X_OK):
        return explicit
    brew = "/opt/homebrew/bin/python3.11"
    return brew if os.access(brew, os.X_OK) else sys.executable


def _self_root() -> Path:
    """The checkout this module runs from (not ``KHIPU_ROOT``)."""
    return Path(__file__).resolve().parents[3]


def _code_env(root: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """``base`` (default: ``_base_env`` plus the data-dir names) with
    PYTHONPATH set the way the launchers set it for ``root``."""
    env = _base_env(DATA_DIR_ENV_KEYS) if base is None else dict(base)
    env["PYTHONPATH"] = f"{Path(root) / 'packages' / 'cli'}:{Path(root) / '.python_libs'}"
    env.setdefault("PYTHONPYCACHEPREFIX", str(Path.home() / "Library" / "Caches" / "Khipu" / "pycache"))
    return env


# The harnesses that start the hooks export PYTHONDONTWRITEBYTECODE=1, so a
# hook only ever reads bytecode, never writes it. A module nothing else has
# cached for the path a hook imports it by is compiled from source on every
# one-shot prompt: routing alone took 1.0 to 2.6 s on a loaded Mac, past the
# routing child's 1 s wait. A bytecode file names its source by path, and a
# path under the ``live`` link holds one slot's code at a time, so that path
# is compiled right after the flip (the old slot's hooks read it until then).
#
# Every file is compiled again (``force``), with checked-hash invalidation: a
# pyc records its source's hash, which every import checks. A timestamp pyc is
# trusted on the source's mtime second and size alone, and two slots' versions
# of a file can share both, so the previous slot's bytecode would be served
# under ``live`` (and compileall would skip it as current).
_PRECOMPILE = (
    "import compileall, py_compile, sys\n"
    "sys.exit(0 if compileall.compile_dir(sys.argv[1], quiet=1, force=True,"
    " invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH) else 1)\n"
)


def precompile(root: Path, *, python: str | None = None, timeout: float = 300) -> dict[str, Any]:
    """Write the bytecode for ``root``'s khipu package, keyed by ``root`` as
    given (the ``live`` link, or a slot), under the PYTHONPYCACHEPREFIX the
    launchers set: never inside the code's own folders, so never inside a
    signed app bundle. ``python`` defaults to the launchers' interpreter (the
    cache is per Python version). Never fails a promotion."""
    pkg = Path(root) / "packages" / "cli" / "khipu"
    if not pkg.is_dir():
        return {"ok": False, "error": f"no khipu package in {root}"}
    t0 = time.monotonic()
    env = _code_env(root)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    r = _run([python or _hook_python(), "-c", _PRECOMPILE, str(pkg)], env=env, timeout=timeout)
    out: dict[str, Any] = {"ok": r.returncode == 0, "ms": round((time.monotonic() - t0) * 1000)}
    if r.returncode != 0:
        out["error"] = _tail(r.stderr or r.stdout) or f"exit {r.returncode}"
    return out


def _base_env(extra: tuple[str, ...] = ()) -> dict[str, str]:
    """The caller's values for ``BASE_ENV_KEYS`` and ``extra``, read one name
    at a time; nothing else from the environment."""
    env: dict[str, str] = {}
    for key in (*BASE_ENV_KEYS, *extra):
        if "KEY" in key or "TOKEN" in key:
            raise ValueError(f"the gate never reads {key}")
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    return env


# ---- isolation ---------------------------------------------------------------


def _seed_config(target: Path) -> None:
    """The live ``config.json`` (settings, switches, key-file paths) copied
    into an isolated dir, without the file-wiki paths and without any value
    whose name says it is a secret."""
    from khipu.config import ISOLATED_UNSET, config_file

    try:
        data = json.loads(config_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    keep = {}
    for key, value in (data if isinstance(data, dict) else {}).items():
        low = str(key).lower()
        if key in ISOLATED_UNSET or any(word in low for word in ("token", "secret", "password")):
            continue
        if "key" in low and not low.endswith("_key_file"):
            continue
        keep[key] = value
    (target / "config.json").write_text(json.dumps(keep, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _link_replica(target: Path) -> None:
    """Read-only access to the live hub replica for a replay: links to the
    replica, its meta file and its pending-erasure list. The recall lane only
    opens the replica read-only (``mode=ro``)."""
    from khipu import hub_snapshot

    live = hub_snapshot.snapshot_path()
    for src in (live, hub_snapshot.meta_path(), hub_snapshot._pending_erasure_dir(live)):
        dst = target / src.name
        if src.exists() and not (dst.exists() or dst.is_symlink()):
            dst.symlink_to(src)


def _isolate(directory: Path, *, replica: bool = False, legacy: bool = False) -> dict[str, str]:
    """Make ``directory`` an isolated data dir and return the variables that
    point a hook at it. ``KHIPU_ISOLATED_DIR`` is the one current code
    honours (it keeps reading credentials from the caller's data dir, which
    the caller's ``KHIPU_DATA_DIR`` names). ``legacy`` is for code older than
    that variable (a replay's live baseline): ``KHIPU_DATA_DIR`` then points
    into the isolated dir too, and such code finds the DSN in the Keychain."""
    directory = Path(directory)
    for sub in (directory, directory / "capture", directory / "logs"):
        sub.mkdir(parents=True, exist_ok=True)
    if not (directory / "config.json").exists():
        _seed_config(directory)
    if replica:
        _link_replica(directory)
    env = {"KHIPU_ISOLATED_DIR": str(directory), "KHIPU_CAPTURE_HOME": str(directory / "capture")}
    if legacy:
        env["KHIPU_DATA_DIR"] = str(directory)
    return env


def _honours_isolation(root: Path) -> bool:
    for rel, mark in _ISOLATION_MARKS:
        try:
            if mark not in (Path(root) / rel).read_text(encoding="utf-8"):
                return False
        except OSError:
            return False
    return True


# ---- processes ---------------------------------------------------------------


def _run(argv: list[str], *, env: dict[str, str] | None = None, input: str | None = None,
         cwd: str | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
    """Every subprocess the gate starts goes through here (tests mock it).
    With no ``cwd`` it runs in an empty folder of its own, never the
    caller's: ``python -c`` and ``-m`` put the working folder first on
    sys.path, so a gate run from a checkout's packages/cli would otherwise
    import that checkout's khipu instead of the code it is checking."""
    try:
        with tempfile.TemporaryDirectory(prefix="khipu-gate-cwd-") as neutral:
            return subprocess.run(argv, env=env, input=input, cwd=cwd or neutral, capture_output=True,
                                  encoding="utf-8", timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return subprocess.CompletedProcess(argv, 124, out, f"timed out after {timeout:g}s")
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"{type(exc).__name__}: {exc}")


def _git(args: list[str], *, cwd: Path, timeout: float = 180) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return subprocess.CompletedProcess(["git", *args], 1, "", f"{type(exc).__name__}: {exc}")


def _git_out(args: list[str], *, cwd: Path) -> str | None:
    r = _git(args, cwd=cwd)
    return r.stdout.strip() if r.returncode == 0 else None


def _head(root: Path | None) -> str | None:
    if root is None or not Path(root).is_dir():
        return None
    return _git_out(["rev-parse", "HEAD"], cwd=Path(root))


def _tail(text: str, n: int = 400) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else "..." + text[-n:]


# ---- slots -------------------------------------------------------------------


def flip(slot: Path) -> dict[str, Any]:
    """Point ``live`` at ``slot`` atomically: a new link under a temporary
    name, then ``rename`` over the old one, so no reader ever sees no link."""
    slot = Path(slot)
    if slot not in slots():
        return {"ok": False, "error": f"not a live slot: {slot}"}
    if not (slot / "packages" / "cli" / "khipu").is_dir():
        return {"ok": False, "error": f"slot holds no checkout: {slot}"}
    link = live_link()
    if (link.exists() or link.is_symlink()) and not link.is_symlink():
        return {"ok": False, "error": f"{link} is a real file or directory, not a link; refusing"}
    before = os.readlink(link) if link.is_symlink() else None
    from_slot = active_slot()
    from_commit = _head(from_slot)
    tmp = link.parent / f".{LIVE_NAME}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        os.symlink(str(slot), tmp)
        os.replace(tmp, link)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    out: dict[str, Any] = {"ok": True, "from": before, "to": str(slot)}
    # Both commits have now been live: the one just left and the one just
    # entered. A rollback only ever goes back to a commit recorded here.
    seen = {s.name: c for s, c in ((from_slot, from_commit), (slot, _head(slot))) if s is not None and c}
    if seen:
        try:
            _remember_live(seen)
        except OSError as exc:
            out["live_state_error"] = f"{type(exc).__name__}: {exc}"
    return out


def last_live() -> dict[str, str]:
    """``{slot name: commit}``, the commit each slot last held while live."""
    try:
        data = json.loads(live_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items() if v} if isinstance(data, dict) else {}


def _remember_live(seen: dict[str, str]) -> None:
    path = live_state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {**last_live(), **seen}
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _vendored_libs(repo: Path) -> Path | None:
    """The checkout's untracked ``.python_libs``: in ``repo`` itself, else in
    the main worktree of its repository."""
    own = Path(repo) / ".python_libs"
    if own.is_dir():
        return own.resolve()
    common = _git_out(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=Path(repo))
    if common and Path(common).name == ".git":
        main = Path(common).parent / ".python_libs"
        if main.is_dir():
            return main
    return None


def _prepare_slot(slot: Path, commit: str, repo: Path) -> dict[str, Any]:
    """Create ``slot`` as a detached worktree at ``commit``, or move it there.
    The launchers put ``<root>/.python_libs`` on PYTHONPATH, and that folder is
    untracked, so the slot links to the checkout's own copy."""
    slot = Path(slot)
    if not slot.exists():
        slot.parent.mkdir(parents=True, exist_ok=True)
        r = _git(["worktree", "add", "--detach", str(slot), commit], cwd=repo)
    else:
        r = _git(["checkout", "--detach", "--quiet", commit], cwd=slot)
    if r.returncode != 0:
        return {"ok": False, "error": _tail(r.stderr or r.stdout) or f"git exit {r.returncode}"}
    libs = _vendored_libs(repo)
    target = slot / ".python_libs"
    if libs is not None and not (target.exists() or target.is_symlink()):
        try:
            target.symlink_to(libs)
        except OSError as exc:
            return {"ok": False, "error": f".python_libs link: {type(exc).__name__}: {exc}"}
    return {"ok": True, "slot": str(slot), "commit": _head(slot) or commit}


def _fetch_and_resolve(repo: Path, ref: str) -> dict[str, Any]:
    remotes = set((_git_out(["remote"], cwd=repo) or "").split())
    head = ref.split("/", 1)[0]
    remote = head if "/" in ref and head in remotes else ("origin" if "origin" in remotes else None)
    if remote:
        r = _git(["fetch", "--quiet", remote], cwd=repo)
        if r.returncode != 0:
            return {"ok": False, "error": f"git fetch {remote}: {_tail(r.stderr) or r.returncode}"}
    commit = _git_out(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=repo)
    if not commit:
        return {"ok": False, "error": f"unknown ref: {ref}"}
    return {"ok": True, "commit": commit, "fetched": remote}


# ---- the gate ----------------------------------------------------------------


def _step(fn: Callable[..., tuple[Any, ...]]) -> Callable[..., dict[str, Any]]:
    """Wrap a check returning (ok, detail) or (ok, detail, extra) into
    ``{"ok", "ms", "detail", **extra}``; a crash is a failed step, never a
    pass."""
    def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        t0 = time.monotonic()
        extra: dict[str, Any] = {}
        try:
            ok, detail, *rest = fn(*args, **kwargs)
            extra = dict(rest[0]) if rest else {}
        except Exception as exc:  # noqa: BLE001 — a crashed check must not look like a pass
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        return {"ok": bool(ok), "ms": round((time.monotonic() - t0) * 1000), "detail": detail, **extra}
    wrapped.__name__ = fn.__name__
    wrapped.__doc__ = fn.__doc__
    return wrapped


def _launchd_env(ctx: dict[str, str]) -> dict[str, str]:
    """The environment launchd gives a rendered job: its fixed keys, the
    passthrough values, a bare PATH, and the login basics launchd sets."""
    from khipu.launchd_gen import PASSTHROUGH_ENV

    env = {"HOME": str(Path.home()), "PATH": LAUNCHD_PATH}
    for key in ("USER", "LOGNAME", "SHELL", "TMPDIR"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    for key in PASSTHROUGH_ENV:
        if (os.environ.get(key) or "").strip():
            env[key] = os.environ[key]
    for key in ("KHIPU_ROOT", "PYTHONPATH", "PYTHONPYCACHEPREFIX"):
        env[key] = ctx[key]
    return env


# ---- the owner's override and the hub wait (offline slice 7) -----------------


def gate_ignorable() -> tuple[str, ...]:
    """The doctor checks the owner may have the next update skip once: every
    local check doctor --quick gates on, plus pending database updates."""
    from khipu.cli import LOCAL_GATES

    # "jobs": a scheduled job's last run failed. Owner-liftable like the rest:
    # otherwise the fix for a failing nightly could never be promoted.
    return (*(k[:-3] for k in LOCAL_GATES if k not in _DOCTOR_NOT_GATED), "schema_migrations", "jobs")


def _override_age_s(stored: dict[str, Any]) -> float | None:
    try:
        at = datetime.fromisoformat(str(stored.get("set_at")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if at.tzinfo is None:
        return None
    return (datetime.now(timezone.utc) - at).total_seconds()


def _held_by_queue(stored: dict[str, Any]) -> bool:
    """True when a promotion waiting for the hub was queued with this very
    override: it keeps the owner's approval for its retry past the day."""
    entry = queued()
    return bool(entry and stored.get("set_at") and entry.get("override_set_at") == stored.get("set_at"))


def gate_override() -> dict[str, Any] | None:
    """The owner's one-time "Update anyway" (``promote_gate_ignore``):
    ``{"checks", "set_at", "by"}``, or None when there is none, it is older
    than ``OVERRIDE_TTL_S``, or the stored value is not one (a standing list
    from before is never read). Only the gate's doctor step reads it."""
    from khipu.config import load_config

    stored = load_config().get(GATE_IGNORE_KEY)
    if not isinstance(stored, dict) or not isinstance(stored.get("checks"), list):
        return None
    age = _override_age_s(stored)
    if age is None or age < -60:
        return None
    if age > OVERRIDE_TTL_S and not _held_by_queue(stored):
        return None
    known = gate_ignorable()
    checks = [name for name in stored["checks"] if isinstance(name, str) and name in known]
    if not checks:
        return None
    return {"checks": checks, "set_at": stored.get("set_at"), "by": stored.get("by")}


def gate_ignore() -> list[str]:
    """The checks the next promotion skips once (empty when none)."""
    found = gate_override()
    return list(found["checks"]) if found else []


def set_gate_ignore(value: "str | list[str] | None", *, by: str | None = None) -> Path:
    """Store the owner's one-time override for these checks (comma-separated
    text or a list), stamped now; empty or None removes it. ``by`` is how
    the owner said so ("app" or "terminal"); the only caller outside this
    module is the config command, after its owner check. ValueError names
    an unknown check."""
    from khipu.config import config_file, mutate_config

    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    names = list(dict.fromkeys(n.strip() for n in (value or []) if n and n.strip()))
    known = gate_ignorable()
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ValueError(f"{GATE_IGNORE_KEY}: not a check the gate runs: {', '.join(unknown)}; "
                         f"one of {', '.join(known)}")
    with mutate_config() as data:
        if names:
            # Same-second owner choices must have distinct stamps for compare-and-clear.
            stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
            data[GATE_IGNORE_KEY] = {"checks": names, "set_at": stamp, "by": by or "?"}
        else:
            data.pop(GATE_IGNORE_KEY, None)
        return config_file()


def clear_gate_override(set_at: Any) -> bool:
    """Remove the override a promotion just used, only if it is still the
    one stamped ``set_at`` (a newer choice made meanwhile stays)."""
    from khipu.config import mutate_config

    with mutate_config() as data:
        stored = data.get(GATE_IGNORE_KEY)
        if isinstance(stored, dict) and stored.get("set_at") == set_at:
            data.pop(GATE_IGNORE_KEY, None)
            return True
    return False


def _blocked_by() -> list[str]:
    """The red checks that held the latest gated run back at the doctor
    step, when an override could let it through (nothing else was wrong)."""
    for row in reversed(records()):
        step = next((s for s in row.get("gate") or [] if isinstance(s, dict) and s.get("step") == "doctor"), None)
        if step is None:
            continue
        if step.get("ok") or step.get("other_problems") or not step.get("red_checks"):
            return []
        known = gate_ignorable()
        reds = [str(n) for n in step["red_checks"]]
        return reds if all(n in known for n in reds) else []
    return []


def gate_ignore_status() -> dict[str, Any]:
    found = gate_override()
    expires = None
    held = False
    if found:
        held = _held_by_queue({"set_at": found["set_at"]})
    if found and not held:
        at = datetime.fromisoformat(str(found["set_at"]).replace("Z", "+00:00"))
        expires = (at.timestamp() + OVERRIDE_TTL_S)
        expires = datetime.fromtimestamp(expires, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {"value": list(found["checks"]) if found else [], "set_at": found["set_at"] if found else None,
            "by": found["by"] if found else None, "expires_at": expires,
            "held_by_queue": held,
            "blocked_by": _blocked_by(), "choices": list(gate_ignorable())}


def hub_reachable(*, timeout: int = HUB_PROBE_TIMEOUT_S) -> bool | None:
    """True when the hub answers a connect, False when it cannot be reached,
    None when no hub is configured here or the probe failed for another
    reason (no wait and no queue then: the refusal stands as it is)."""
    try:
        from khipu.hub_snapshot import hub_connection_failed, try_hub_connect

        from khipu.db import resolve_dsn

        resolve_dsn()
    except Exception:  # noqa: BLE001 — no DSN: nothing to wait for
        return None
    try:
        conn = try_hub_connect(connect_timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return False if hub_connection_failed(exc) else None
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    return True


def hub_wait_s() -> int:
    from khipu.config import minute_setting

    try:
        return minute_setting(HUB_WAIT_KEY) * 60
    except Exception:  # noqa: BLE001
        return 10 * 60


def _wait_for_hub(budget_s: float) -> bool:
    """Poll the hub until it answers or ``budget_s`` runs out."""
    end = time.monotonic() + max(0.0, budget_s)
    while True:
        if hub_reachable() is True:
            return True
        left = end - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(HUB_POLL_S, left))


def _offline_only(out: dict[str, Any], okey: str) -> bool:
    block = out.get(okey[:-3])
    return isinstance(block, dict) and block.get("offline_only") is True


@_step
def gate_doctor(candidate: Path) -> tuple[bool, str] | tuple[bool, str, dict[str, Any]]:
    """1. ``khipu doctor --quick`` from the candidate under the environment a
    launchd job rendered for it would get, so a missing binary or module
    shows here and not at the next scheduled run.

    A red check whose block says ``offline_only`` (its evidence is all from
    a time the hub could not be reached) does not refuse a candidate, and
    nor does a check the owner's one-time override names (``gate_override``);
    both are named in the step's record, with the override itself, so the
    promotion can clear it after this one use."""
    from khipu import launchd_gen

    override = gate_override()
    used = {"override": override} if override else {}
    ctx = launchd_gen.render_context(root=Path(candidate))
    r = _run([ctx["KHIPU_PYTHON"], "-m", "khipu", "doctor", "--quick"], env=_launchd_env(ctx),
             cwd=ctx["WORKING_DIRECTORY"], timeout=90)
    try:
        out = json.loads(r.stdout or "{}")
    except ValueError:
        out = {}
    if r.returncode == 0 and out.get("ok") is True:
        return True, f"doctor --quick ok ({ctx['KHIPU_PYTHON']}, PATH={LAUNCHD_PATH})", used
    hard = r.returncode not in (0, 2) or not out
    problems = []
    failed_imports = (out.get("imports") or {}).get("failed") or {}
    if failed_imports:
        problems.append("imports: " + "; ".join(f"{k} ({v})" for k, v in failed_imports.items()))
    if out and not (out.get("config") or {}).get("ok"):
        problems.append(f"config: {(out.get('config') or {}).get('error')}")
    if out and not out.get("dsn_present"):
        problems.append("no DSN")
    jobs_red = bool(out.get("job_failures"))
    if out and not out.get("synth_provider_ok", True):
        problems.append(f"summariser provider: {out.get('synth_provider')}")
    reds = [key for key, value in out.items()
            if key.endswith("_ok") and value is False and key not in _DOCTOR_NOT_GATED]
    if jobs_red:
        reds.append("jobs_ok")
    offline = [k[:-3] for k in reds if _offline_only(out, k)]
    ignore = set(override["checks"]) if override else set()
    ignored = [k[:-3] for k in reds if k[:-3] in ignore and k[:-3] not in offline]
    red = [k[:-3] for k in reds if k[:-3] not in offline and k[:-3] not in ignored]
    other = bool(problems) or hard
    if "jobs" in red:
        problems.append(f"jobs failing: {out['job_failures']}")
    if red:
        problems.append("red: " + ", ".join(red))
    if not out:
        problems.append(f"exit {r.returncode}: {_tail(r.stderr or r.stdout)}")
    if not problems and not hard and (offline or ignored):
        notes = []
        if offline:
            notes.append("red only from a time the hub was out of reach: " + ", ".join(offline))
        if ignored:
            notes.append("skipped once, as the owner chose Update anyway: " + ", ".join(ignored))
        extra = {k: v for k, v in (("offline_reds", offline), ("ignored_reds", ignored)) if v}
        return True, "doctor --quick ok; " + "; ".join(notes), {**extra, **used}
    return False, "; ".join(problems) or f"exit {r.returncode}", {
        **({"red_checks": red} if red else {}), **({"other_problems": True} if other else {}), **used}


def _parse_summary(line: str) -> dict[str, Any] | None:
    m = _SUMMARY_RE.match(line)
    if not m:
        return None
    try:
        hits = [str(h) for h in ast.literal_eval(m.group("hits"))]
    except (ValueError, SyntaxError):
        hits = []
    routed = _ROUTING_FIELD_RE.search(line)
    degraded = _DEGRADED_RE.search(line)
    legs = _LEGS_RE.search(line)
    return {"stamp": m.group("stamp"), "session": m.group("session"),
            "reason": m.group("reason"), "ms": float(m.group("ms")), "hits": hits,
            "routing": routed.group("status") if routed else None,
            "degraded": degraded.group("degraded") if degraded else None,
            "legs": legs.group("legs") if legs else None}


def _read_tail(path: Path, max_bytes: int = REPLAY_TAIL_BYTES) -> list[str]:
    """The whole lines in the last ``max_bytes`` of ``path``, each decoded as
    strict UTF-8: the queries are compared and replayed, and replacing a bad
    byte can merge two of them, so an undecodable line is skipped."""
    with open(path, "rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        offset = max(0, size - max_bytes)
        before = b"\n"
        if offset:
            fh.seek(offset - 1)
            before = fh.read(1)
        fh.seek(offset)
        data = fh.read()
    if before != b"\n":
        data = data.partition(b"\n")[2]  # the seek landed inside a line
    out = []
    for raw in data.split(b"\n"):
        try:
            out.append(raw.decode("utf-8").removesuffix("\r"))
        except UnicodeDecodeError:
            continue
    return out


def sample_queries(log: Path, n: int = 5, *, now: float | None = None) -> list[dict[str, Any]]:
    """Up to ``n`` distinct searches from the last day of ``query_log.jsonl``
    (any harness, non-empty ``query``), spread evenly over the day, each masked
    with the capture path's secret masker before it goes anywhere. Reads that
    log and nothing else."""
    from khipu.redact import redact_secrets

    now = time.time() if now is None else now
    try:
        lines = _read_tail(Path(log))
    except OSError:
        return []
    seen: dict[str, dict[str, Any]] = {}
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or not isinstance(row.get("query"), str) or not row["query"].strip():
            continue
        try:
            stamp = datetime.strptime(str(row.get("ts")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if now - stamp.timestamp() > REPLAY_WINDOW_S:
            continue
        # Masked whatever KHIPU_REDACT_SECRETS says: the text leaves this
        # process for a hook's stdin.
        query, _masked = redact_secrets(row["query"].strip(), force=True)
        seen.pop(query, None)
        seen[query] = {"prompt": query, "harness": row.get("harness")}
    rows = list(seen.values())
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)]


def _replay_env(isolated: Path, *, legacy: bool = False) -> dict[str, str]:
    """An isolated data dir (its own log, dedup state, embedding cache and
    budget) that reads the live replica, and the one-shot path: the warm
    service answers with whatever code it started on, which is not the
    candidate."""
    env = _base_env(DATA_DIR_ENV_KEYS)
    env.update(_isolate(isolated, replica=True, legacy=legacy))
    env["KHIPU_RECALL_DAEMON"] = "0"
    return env


def _copy_routing_record(isolated: Path) -> bool:
    """The live routing record (its log and view) copied into a replay's
    isolated dir; True when there was one. Reads those two files only."""
    from khipu import routing

    live = routing.routing_dir()
    target = Path(isolated).joinpath(*routing.STATE_DIR)
    copied = False
    for name in ROUTING_RECORD_FILES:
        src = live / name
        if not src.is_file():
            continue
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target / name)
        copied = True
    return copied


def _recall_once(hook: Path, sample: dict[str, Any], session: str, *, legacy: bool = False,
                 routing: bool | None = None, root: Path | None = None) -> dict[str, Any]:
    """One prompt through ``hook`` in its own isolated dir. ``routing``
    True or False sets the routing push on or off for this run (None leaves
    it to the copied settings); with it on, the live routing record is
    copied in and folded first with the code at ``root``, which also says
    whether its push is on here (``push``: True, False, or None when it
    could not say)."""
    push = None
    with tempfile.TemporaryDirectory(prefix="khipu-gate-recall-") as tmp:
        payload = {"session_id": session, "transcript_path": "", "cwd": str(Path.home()),
                   "hook_event_name": "UserPromptSubmit", "prompt": sample["prompt"]}
        env = _replay_env(Path(tmp), legacy=legacy)
        if routing is not None:
            env["KHIPU_FEATURE_ROUTING_PUSH"] = "1" if routing else "0"
        if routing and root is not None:
            copied = _copy_routing_record(Path(tmp))
            p = _run([_hook_python(), "-c", _PREFOLD, "1" if copied else "0"], env=_code_env(Path(root), env),
                     timeout=60)
            push = _probe_answer(p)
        t0 = time.monotonic()
        r = _run([str(hook)], env=env, input=json.dumps(payload), timeout=15)
        wall_ms = round((time.monotonic() - t0) * 1000)
        row = None
        log = Path(env["KHIPU_CAPTURE_HOME"]) / "logs" / "prompt-recall.log"
        try:
            for line in read_lines(log):
                row = _parse_summary(line) or row
        except OSError:
            row = None
    try:
        output = json.loads((r.stdout or "").strip() or "{}")
        valid = isinstance(output, dict)
    except ValueError:
        valid = False
    return {"exit": r.returncode, "valid_json": valid, "wall_ms": wall_ms,
            "reason": row["reason"] if row else None, "ms": row["ms"] if row else None,
            "hits": row["hits"] if row else [], "routing": row["routing"] if row else None,
            "degraded": row["degraded"] if row else None, "legs": row["legs"] if row else None,
            "push": push}


def _probe_answer(r: subprocess.CompletedProcess) -> bool | None:
    """What the candidate's probe (``_PREFOLD``) said about its push, or None."""
    if r.returncode != 0:
        return None
    try:
        out = json.loads(split_lines((r.stdout or "").strip())[-1])
    except (ValueError, IndexError):
        return None
    push = out.get("push") if isinstance(out, dict) else None
    return push if isinstance(push, bool) else None


def _routing_skip_problem(status: str) -> str | None:
    """Why a run with routing on that logged ``status`` fails, or None."""
    if not status.startswith("skipped:") or status.startswith(ROUTING_SKIPS_OK) or status == ROUTING_WITHHOLD:
        return None
    if status == "skipped:budget":
        return "its routing read ran out of its budget"
    if status.startswith("skipped:error"):
        return "its routing read failed"
    if status == "skipped:no-answer":
        return "its routing read gave no answer (the reader died or wrote nothing it could read)"
    return "its routing read was skipped"


def _slower_than(run: dict[str, Any], refs: tuple[Any, ...]) -> bool:
    """Whether ``run`` is over ``RECALL_BUDGET_MS`` and also slower than every
    reference time (in ms; None ones are ignored) by more than
    ``SLOWNESS_SLACK_MS``. With no reference time, over the budget is enough.
    The hook self-limits near the budget, so a cold process whose embedding
    is late lands a little over it on live's own code too."""
    ms = run.get("ms")
    if ms is None or ms <= RECALL_BUDGET_MS:
        return False
    return all(ms > r + SLOWNESS_SLACK_MS for r in refs if r is not None)


def _shared_slowness_note(run: dict[str, Any], refs: tuple[Any, ...]) -> str | None:
    """The note for a run over the budget that is no slower than its
    references by the slack: slowness live shares, not the candidate's."""
    ms = run.get("ms")
    if ms is None or ms <= RECALL_BUDGET_MS or _slower_than(run, refs):
        return None
    given = [r for r in refs if r is not None]
    return f"{ms:.0f} ms, reference {max(given):.0f} ms: shared slowness, not the candidate"


def _search_problem(run: dict[str, Any], refs: tuple[Any, ...] = ()) -> str | None:
    """Why one replayed run's own search fails the step, or None: the hook
    exited non-zero or wrote no JSON, wrote no summary line, timed out or
    failed, or took longer than ``RECALL_BUDGET_MS`` and longer than every
    reference time in ``refs`` plus ``SLOWNESS_SLACK_MS`` (``_slower_than``).
    The same test for the run with routing off and the run with routing on."""
    if run["exit"] != 0 or not run["valid_json"]:
        return f"hook exit {run['exit']}, valid_json={run['valid_json']}"
    reason = run.get("reason")
    if reason is None:
        return "the hook wrote no summary line"
    if reason.startswith(("timeout", "error", "gate error")):
        return reason
    if _slower_than(run, refs):
        given = [r for r in refs if r is not None]
        against = f", reference {max(given):.0f} ms" if given else ""
        return f"{run['ms']:.0f} ms > {RECALL_BUDGET_MS:.0f} ms{against}"
    return None


def _empty_by_timing(live: dict[str, Any] | None, cand: dict[str, Any]) -> bool:
    """Whether the candidate answered nothing where live had hits only
    because its embedding was late or failed: it logged an embedding
    degraded note and ran no cosine leg where live ran one. A query whose
    words match nothing finds its hits on the cosine leg alone, so such a
    run is empty with sound code."""
    if not (live and live.get("hits")) or cand["hits"]:
        return False
    return (str(cand.get("degraded") or "").startswith("embedding")
            and "'cosine'" in str(live.get("legs") or "") and "'cosine'" not in str(cand.get("legs") or ""))


def _embedding_failed(live: dict[str, Any] | None, cand: dict[str, Any]) -> bool:
    """Whether the candidate's embedding failed (not merely ran late) on a
    prompt where live ran the cosine leg."""
    return (str(cand.get("degraded") or "").startswith("embedding error")
            and "'cosine'" in str((live or {}).get("legs") or ""))


ABSTAINED_REASON = "ok (no relevant match)"


def _abstained(live: dict[str, Any] | None, cand: dict[str, Any]) -> bool:
    """Whether the candidate answered nothing where live had hits because its
    relevance gate found nothing relevant: the hook's own summary reason says
    so. That is policy silence (the always-push code live may still run does
    not abstain), not breakage, unless it happens on every such prompt."""
    return bool((live or {}).get("hits")) and not cand["hits"] and cand.get("reason") == ABSTAINED_REASON


def _off_problem(label: str, live: dict[str, Any] | None, cand: dict[str, Any]) -> str | None:
    """Why the candidate's run with routing off fails the step, or None: its
    own search fails (``_search_problem``), or it answered nothing where the
    live code had hits, unless only its embedding was late
    (``_empty_by_timing``) or the candidate's relevance gate abstained
    (``_abstained``). The same test for the first run and the replayed
    one."""
    what = _search_problem(cand, ((live or {}).get("ms"),))
    if what:
        return f"{label}: {what}"
    live_hits = (live or {}).get("hits") or []
    if live_hits and not cand["hits"] and not _empty_by_timing(live, cand) and not _abstained(live, cand):
        return f"{label}: empty answer where live had {len(live_hits)} hit(s)"
    return None


def _comparable(off: dict[str, Any], on: dict[str, Any]) -> bool:
    """Whether two runs' search answers can be compared: the same legs ran
    and the same degraded note. Each run is a cold one-shot hook, so an
    embedding late in one of them (a lexical-only answer) is timing, not
    routing."""
    return off.get("degraded") == on.get("degraded") and off.get("legs") == on.get("legs")


def _routing_problem(label: str, off: dict[str, Any], on: dict[str, Any],
                     live: dict[str, Any] | None = None) -> str | None:
    """Why the candidate's run with routing on fails the step, or None: its
    routing read was skipped for any reason but a benign one
    (``ROUTING_SKIPS_OK``), its code said the push was on and the hook
    logged no routing status, its own search fails as a run with routing
    off would (``_search_problem``), or its search answer is not the one it
    gave with routing off when both ran the same legs with the same
    degraded note (``_comparable``)."""
    status = on.get("routing") or ""
    what = _routing_skip_problem(status)
    if what:
        return f"{label}: with routing on, {what} (routing={status})"
    if on.get("push") is True and not status and on.get("reason") is not None:
        return f"{label}: with routing on, the candidate's push is on but its hook logged no routing status"
    what = _search_problem(on, (off.get("ms"), (live or {}).get("ms")))
    if what:
        return f"{label}: with routing on, {what}"
    if _comparable(off, on) and on["hits"] != off["hits"]:
        return (f"{label}: the search answer changed with routing on ({len(on['hits'])} hit(s) against "
                f"{len(off['hits'])} with it off)")
    return None


@_step
def gate_recall_replay(candidate: Path, n: int = 5) -> tuple[bool, str]:
    """2. Replay real searches from the last day of ``query_log.jsonl``
    through the candidate's ``khipu-prompt-recall``, each wrapped as the
    harness's UserPromptSubmit stdin JSON, each inside the 1.2 s budget, and
    compare the hit ids with the live code's answer; an empty answer where
    live had one refuses. Each search is replayed through the candidate
    twice, with the routing push off and on (phase 3c): the candidate fails
    when its routing read is skipped for any reason but a benign one (it
    ran out of its budget, failed, or its reader died before it answered),
    when its push is on and its hook logs no routing status, when its
    search with routing on times out, fails or runs past the 1.2 s budget
    (the same test as with routing off), or when its search answer differs
    with routing on while both runs ran the same legs with the same degraded
    note. A run over the 1.2 s budget fails only when it is also more than
    ``SLOWNESS_SLACK_MS`` slower than live's run of the same prompt (with
    routing on: than both live's and its own routing-off run); otherwise it
    is noted as slowness live shares. The candidate's median is also held to
    live's median plus the slack. An empty answer where live had hits that came only from the
    candidate's embedding being late or failed (no cosine leg where live ran
    one) replays live and the candidate once more; if it is still that, it
    is noted, not failed (a query whose words match nothing finds its hits
    on the cosine leg alone), unless the candidate's embedding failed on
    every replayed prompt while live's ran: that refuses. A failing pair, or one whose legs or degraded note differ, is
    replayed once more first, so a search or a read that was late once is
    not taken for the candidate's routing, and both replayed runs are held
    to their tests (the run with routing off to the search and empty-answer
    tests); a pair whose legs or degraded note still differ is noted, not
    failed (timing, not routing)."""
    samples = sample_queries(query_log_path(), n)
    if not samples:
        return False, "no queries in the last day; use Khipu once and re-run"
    live_root = _live_code_root()
    live_hook = config_dir() / "bin" / "khipu-prompt-recall"
    if not live_hook.exists() and live_root is not None:
        live_hook = _launcher(live_root, "khipu-prompt-recall")
    cand_hook = _launcher(Path(candidate), "khipu-prompt-recall")
    if not cand_hook.is_file():
        return False, f"candidate has no {cand_hook}"
    nonce = uuid.uuid4().hex[:10]
    problems: list[str] = []
    notes: list[str] = []
    live_legacy = live_root is None or not _honours_isolation(live_root)
    cand_legacy = not _honours_isolation(Path(candidate))
    routed_runs = 0
    withheld_runs = 0
    embed_failed: dict[int, bool] = {}
    abstentions = 0
    live_hit_prompts = 0
    cand_times: list[float] = []
    live_times: list[float] = []
    for i, sample in enumerate(samples):
        live = (_recall_once(live_hook, sample, f"khipu-gate-{nonce}-live-{i}", legacy=live_legacy, routing=False)
                if live_hook.exists() else None)
        cand = _recall_once(cand_hook, sample, f"khipu-gate-{nonce}-cand-{i}", legacy=cand_legacy, routing=False)
        label = f"prompt {i + 1}"
        problem = _off_problem(label, live, cand)
        if problem is None and _empty_by_timing(live, cand):
            # Once more, both live and the candidate, before an empty answer
            # from a late embedding is set aside as timing.
            live = _recall_once(live_hook, sample, f"khipu-gate-{nonce}-live-{i}-again", legacy=live_legacy,
                                routing=False)
            cand = _recall_once(cand_hook, sample, f"khipu-gate-{nonce}-cand-{i}-again", legacy=cand_legacy,
                                routing=False)
            problem = _off_problem(label, live, cand)
        live_hits = (live or {}).get("hits") or []
        embed_failed[i] = _embedding_failed(live, cand)
        if problem:
            problems.append(problem)
            continue
        on = _recall_once(cand_hook, sample, f"khipu-gate-{nonce}-route-{i}", legacy=cand_legacy, routing=True,
                          root=Path(candidate))
        problem = _routing_problem(label, cand, on, live)
        if problem or not _comparable(cand, on):
            # Once more, both ways, before the step fails or the answers go
            # uncompared: a search late once (embedding late, a cold cache)
            # is not routing, and nor is a read that refolded once because a
            # runner's heartbeat was rewritten between the prefold and the
            # hook's read.
            cand = _recall_once(cand_hook, sample, f"khipu-gate-{nonce}-cand-{i}-again", legacy=cand_legacy,
                                routing=False)
            on = _recall_once(cand_hook, sample, f"khipu-gate-{nonce}-route-{i}-again", legacy=cand_legacy,
                              routing=True, root=Path(candidate))
            # Both replayed runs are tested: the one with routing off
            # replaces the first, so it is held to the first one's tests.
            problem = _off_problem(label, live, cand) or _routing_problem(label, cand, on, live)
            embed_failed[i] = _embedding_failed(live, cand)
        if problem:
            problems.append(problem)
            continue
        overlap = len(set(live_hits) & set(cand["hits"]))
        note = f"{label}: {len(cand['hits'])} hit(s), {overlap} shared with live, {cand['ms']:.0f} ms"
        live_ms = (live or {}).get("ms")
        if cand.get("ms") is not None and live_ms is not None:
            cand_times.append(cand["ms"])
            live_times.append(live_ms)
        shared = _shared_slowness_note(cand, (live_ms,))
        if shared:
            note += f" ({shared}; live {live_ms:.0f} ms)" if live_ms is not None else f" ({shared})"
        live_hit_prompts += bool(live_hits)
        if _abstained(live, cand):
            abstentions += 1
            note += f", abstained where live had {len(live_hits)} hit(s) (relevance gate: policy, not a failure)"
        if _empty_by_timing(live, cand):
            note += (f", empty where live had {len(live_hits)} hit(s) not counted (no cosine leg after a replay: "
                     f"timing, not code; degraded={cand.get('degraded')})")
        if on.get("routing") == ROUTING_WITHHOLD:
            withheld_runs += 1
            note += f", routing withheld ({ROUTING_WITHHOLD}: no complete inventory in time under load)"
        elif on.get("routing"):
            # Only a read that delivered counts as routed: a benign skip
            # (ROUTING_SKIPS_OK) passes but proves nothing about the inventory.
            if not str(on["routing"]).startswith("skipped:"):
                routed_runs += 1
            on_ms = f"{on['ms']:.0f} ms" if on.get("ms") is not None else "no time"
            note += f", {on_ms} with routing on (routing={on['routing']})"
            shared = _shared_slowness_note(on, (cand.get("ms"), live_ms))
            if shared:
                note += f" ({shared})"
        if not _comparable(cand, on):
            note += ", answers not compared (their legs or degraded note differed twice: timing, not routing)"
        notes.append(note)
    if len(embed_failed) == len(samples) and all(embed_failed.values()):
        # Once in a while is the provider; on every prompt while live's
        # embedding ran is the candidate's code.
        problems.append(f"the candidate's embedding failed on all {len(samples)} prompt(s) while live's ran")
    if live_hit_prompts and abstentions == live_hit_prompts:
        # One abstention is the gate being selective; on every prompt where
        # live had hits it is a gate that is broken.
        problems.append(f"the candidate abstained by policy on all {live_hit_prompts} replayed prompt(s) "
                        f"where live had hits")
    if withheld_runs and not routed_runs:
        problems.append(f"with routing on, the routing read withheld ({ROUTING_WITHHOLD}) on all {withheld_runs} "
                        f"replayed prompt(s) and none routed")
    medians = ""
    if cand_times:
        cand_median, live_median = statistics.median(cand_times), statistics.median(live_times)
        if cand_median - live_median > SLOWNESS_SLACK_MS:
            problems.append(f"the candidate is slower than live by {cand_median - live_median:.0f} ms at the median "
                            f"({cand_median:.0f} ms against {live_median:.0f} ms)")
        medians = f"median {cand_median:.0f} ms, live {live_median:.0f} ms; "
    if problems:
        return False, "; ".join(problems)
    tail = "" if routed_runs else " (routing did not run: the candidate has no routing push, or it is switched off here)"
    head = f"{len(samples)} prompt(s) replayed; {medians}"
    if withheld_runs:
        head += f"{withheld_runs} of {len(samples)} prompt(s) withheld routing ({ROUTING_WITHHOLD}), {routed_runs} routed; "
    if abstentions:
        head += f"{abstentions} of {live_hit_prompts} prompt(s) where live had hits were abstentions by the relevance gate; "
    return True, head + "; ".join(notes) + tail


def _fixture(candidate: Path) -> Path:
    rel = Path("packages") / "cli" / "tests" / "fixtures" / "gate_transcript.jsonl"
    own = Path(candidate) / rel
    if own.is_file():
        return own
    return Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "gate_transcript.jsonl"


def _lookup(stored: str, env: dict[str, str]) -> list[int] | None:
    """Live (not forgotten) hub episodes for ``stored``; None when the hub
    could not be asked."""
    r = _run([_hook_python(), "-c", _FIND_EPISODES, stored], env=env, timeout=60)
    if r.returncode != 0:
        return None
    try:
        return [int(x) for x in json.loads(r.stdout or "")]
    except (ValueError, TypeError):
        return None


def _cleanup_env(run_dir: Path, code_root: Path) -> dict[str, str]:
    return _code_env(Path(code_root), {**_base_env(DATA_DIR_ENV_KEYS), **_isolate(run_dir)})


def _cleanup_session(stored: str, run_dir: Path, code_root: Path,
                     ids: list[int] | None = None) -> dict[str, Any]:
    """Forget every live hub episode of one gate session, from inside that
    run's isolated dir (so the replica erasure and the outbox check stay
    there), and mark the cleanup done only once the hub answers that none is
    left."""
    env = _cleanup_env(Path(run_dir), Path(code_root))
    if ids is None:
        ids = _lookup(stored, env)
        if ids is None:
            return {"ok": False, "error": "hub lookup failed"}
    failed = []
    for episode_id in ids:
        f = _run([_hook_python(), "-m", "khipu", "episode", "forget", str(episode_id)], env=env, timeout=60)
        if f.returncode != 0:
            failed.append(episode_id)
    left = _lookup(stored, env)
    if left is None:
        return {"ok": False, "forgotten": ids, "error": "hub lookup after forget failed"}
    if left:
        return {"ok": False, "forgotten": [i for i in ids if i not in left],
                "error": f"still live in the hub: {left}" + (f" (forget failed for {failed})" if failed else "")}
    record({"action": "gate-cleanup", "session_id": stored, "dir": str(run_dir),
            "cleanup": "cleanup_done", "forgotten": ids})
    shutil.rmtree(run_dir, ignore_errors=True)
    return {"ok": True, "forgotten": ids}


def pending_cleanups() -> list[dict[str, Any]]:
    """Gate sessions whose hub episode is not yet confirmed gone: a
    ``cleanup_pending`` line in promotions.jsonl, or a run dir's marker, with
    no ``cleanup_done`` after it."""
    latest: dict[str, dict[str, Any]] = {}
    for row in records():
        if row.get("action") == "gate-cleanup" and row.get("session_id"):
            latest[str(row["session_id"])] = row
    try:
        markers = sorted(gate_runs_dir().glob("*/gate.json"))
    except OSError:
        markers = []
    for marker in markers:
        try:
            row = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(row, dict) and row.get("session_id") and str(row["session_id"]) not in latest:
            latest[str(row["session_id"])] = {**row, "dir": str(marker.parent)}
    return [row for row in latest.values() if row.get("cleanup") == "cleanup_pending"]


def retry_cleanups() -> list[dict[str, Any]]:
    """One more try at every pending gate cleanup (run under the lock)."""
    out = []
    for row in pending_cleanups():
        res = _cleanup_session(str(row["session_id"]), Path(row["dir"]), _self_root())
        out.append({"session_id": row["session_id"], **res})
    return out


@_step
def gate_capture(candidate: Path) -> tuple[bool, str]:
    """3. One real capture end to end: a fixture transcript through the
    candidate's ``khipu-stop-hook`` (inline drain, gate mode), the episode
    found in the hub, then forgotten. Everything but that one hub episode
    lives in an isolated data dir under ``state/gate-runs/<session>``; the
    session id is recorded there and in promotions.jsonl as
    ``cleanup_pending`` before the hook runs, and the next promote or status
    retries the cleanup until the hub confirms the episode is gone."""
    cand = Path(candidate)
    hook = _launcher(cand, "khipu-stop-hook")
    if not hook.is_file():
        return False, f"candidate has no {hook}"
    fixture = _fixture(cand)
    if not fixture.is_file():
        return False, f"no fixture transcript at {fixture}"
    if not _honours_isolation(cand):
        return False, ("the candidate predates the isolated gate capture (KHIPU_ISOLATED_DIR); "
                       "its capture would touch live state, so it is not run")
    sid = f"khipu-gate-{uuid.uuid4().hex[:12]}"
    stored = f"{GATE_HARNESS}:{sid}"
    run_dir = gate_runs_dir() / sid
    iso = _isolate(run_dir)
    marker = {"session_id": stored, "created": _now(), "cleanup": "cleanup_pending"}
    (run_dir / "gate.json").write_text(json.dumps(marker) + "\n", encoding="utf-8")
    record({"action": "gate-cleanup", "dir": str(run_dir), **marker})
    transcript = run_dir / "transcript.jsonl"
    shutil.copyfile(fixture, transcript)
    env = {**_base_env(DATA_DIR_ENV_KEYS), **iso, "KHIPU_HOOK_INLINE": "1", "KHIPU_HARNESS": GATE_HARNESS,
           "KHIPU_GATE": "1"}
    payload = {"session_id": sid, "transcript_path": str(transcript),
               "hook_event_name": "SessionEnd", "cwd": str(run_dir)}
    r = _run([str(hook)], env=env, input=json.dumps(payload), timeout=100)
    queue = Path(iso["KHIPU_CAPTURE_HOME"]) / "queue"
    left = sorted(p.name for p in queue.glob("*.json")) if queue.is_dir() else []
    try:
        log = (Path(iso["KHIPU_CAPTURE_HOME"]) / "logs" / "session-capture.log").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        log = ""
    pending = "cleanup pending; the next `khipu promote` or `--status` retries it"
    ids = _lookup(stored, _cleanup_env(run_dir, cand))
    if ids is None:
        return False, f"episode lookup failed after the stop hook (exit {r.returncode}); {pending}"
    if not ids:
        why = f"{len(left)} job(s) still queued" if left else "queue empty"
        detail = (f"no episode for {stored} after the stop hook (exit {r.returncode}, {why}); "
                  f"capture log: {_tail(log, 600) or 'empty'}")
        if r.returncode == 124:
            # A hook killed at the timeout may still land its episode.
            return False, f"{detail}; {pending}"
        _cleanup_session(stored, run_dir, cand, ids=[])
        return False, detail
    cleaned = _cleanup_session(stored, run_dir, cand, ids=ids)
    if not cleaned["ok"]:
        return False, f"episode(s) {ids} landed for {stored}; forget not confirmed ({cleaned['error']}); {pending}"
    return True, f"episode(s) {ids} landed for {stored} and were forgotten (hub confirmed)"


def _code_facts(root: Path) -> dict[str, Any]:
    r = _run([_hook_python(), "-c", _CODE_FACTS], env=_code_env(Path(root)), timeout=60)
    try:
        out = json.loads(r.stdout or "")
    except ValueError:
        return {"error": _tail(r.stderr or r.stdout) or f"exit {r.returncode}"}
    return out if isinstance(out, dict) else {"error": "unexpected output"}


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", str(text or ""))[:3]) or (0,)


@_step
def gate_version(candidate: Path) -> tuple[bool, str]:
    """4. The candidate's ``khipu.__version__`` is not older than live's, and
    the replica schema on disk is the one the candidate expects, or an older
    one its next refresh rebuilds."""
    cand = _code_facts(Path(candidate))
    if "error" in cand:
        return False, f"candidate: {cand['error']}"
    parts = []
    live_root = _live_code_root()
    if live_root is not None:
        live = _code_facts(live_root)
        if "error" in live:
            return False, f"live: {live['error']}"
        if _version_tuple(cand["version"]) < _version_tuple(live["version"]):
            return False, f"candidate {cand['version']} is older than live {live['version']}"
        parts.append(f"version {live['version']} -> {cand['version']}")
    else:
        parts.append(f"version {cand['version']} (no live code to compare)")
    disk, want = cand.get("disk_schema"), cand.get("schema")
    if disk is None:
        parts.append("no replica on disk")
    elif disk == want:
        parts.append(f"replica schema {disk}")
    elif isinstance(disk, int) and isinstance(want, int) and disk < want:
        parts.append(f"replica schema {disk}, candidate rebuilds it at {want} on refresh")
    else:
        return False, f"replica schema {disk} on disk is newer than the candidate's {want}"
    return True, "; ".join(parts)


GATE_STEPS: dict[str, Callable[[Path], dict[str, Any]]] = {
    "doctor": gate_doctor,
    "recall": gate_recall_replay,
    "capture": gate_capture,
    "version": gate_version,
}


def run_gate(candidate: Path) -> list[dict[str, Any]]:
    """Every step in order; the first failure stops the rest. A step that
    needs the hub and failed while the hub was out of reach waits for it
    (``promote_hub_wait_minutes``) and runs once more; if the hub never
    answers, the step is marked ``hub_unreachable``."""
    results = []
    for name in GATE_ORDER:
        res = {"step": name, **GATE_STEPS[name](Path(candidate))}
        if not res["ok"] and name in HUB_STEPS and hub_reachable() is False:
            budget = hub_wait_s()
            t0 = time.monotonic()
            if _wait_for_hub(budget):
                res = {"step": name, **GATE_STEPS[name](Path(candidate)),
                       "waited_for_hub_s": round(time.monotonic() - t0)}
            else:
                res = {**res, "hub_unreachable": True, "waited_for_hub_s": round(time.monotonic() - t0),
                       "detail": f"the hub could not be reached (waited {round(budget / 60)} min): "
                                 f"{res.get('detail')}"}
        results.append(res)
        if not res["ok"]:
            break
    return results


# ---- records -----------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _who() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "?"


def record(entry: dict[str, Any]) -> dict[str, Any]:
    entry = {"ts": _now(), "who": _who(), **entry}
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")
    return entry


def records() -> list[dict[str, Any]]:
    try:
        lines = split_lines(state_file().read_text(encoding="utf-8"))
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def queue_file() -> Path:
    """A promotion the gate refused only because the hub was out of reach."""
    return config_dir() / "state" / "promote-queued.json"


def queued() -> dict[str, Any] | None:
    try:
        data = json.loads(queue_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("ref") else None


def _write_queue(data: dict[str, Any]) -> None:
    path = queue_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _clear_queue() -> None:
    try:
        queue_file().unlink()
    except FileNotFoundError:
        pass


def _queue(ref: str, commit: str | None, error: str, previous: dict[str, Any] | None,
           override_set_at: Any = None) -> dict[str, Any]:
    """Queue (or re-queue) a promotion. The file is replaced atomically, so a
    crash leaves the old entry or the new one, never neither. A re-queue
    carries the override the first run used (``override_set_at``)."""
    held = override_set_at or (previous or {}).get("override_set_at")
    data = {"ref": ref, "commit": commit, "error": error, "request_id": uuid.uuid4().hex,
            "queued_at": (previous or {}).get("queued_at") or _now(),
            "attempts": int((previous or {}).get("attempts") or 0) + 1, "last_attempt_at": _now(),
            **({"override_set_at": held} if held else {})}
    _write_queue(data)
    return data


@contextmanager
def _lock():
    path = config_dir() / "state" / "promote.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = True
        except OSError:
            held = False
        yield held


# ---- after a flip ------------------------------------------------------------


def _before_flip() -> dict[str, Any] | None:
    """Compile the code still live through the ``live`` link, just before
    the flip. A cache written before checked-hash bytecode (timestamp pycs)
    would otherwise serve the old slot's code to hooks between the flip and
    ``_after_flip``'s compile, wherever a file kept its size and mtime second.
    Rewritten checked-hash, each one is checked against the new source."""
    live = live_root()
    return precompile(live) if live is not None else None


def _after_flip() -> dict[str, Any]:
    """Re-render any plist the new live code renders differently, restart
    the two long-running jobs that hold the old modules, then wake the
    replica job so the new code rebuilds a replica at another schema now,
    not at its next interval."""
    from khipu import replica_heal
    from khipu.jobs import PLIST_QUEUE_DRAIN, PLIST_RECALL_DAEMON, PLIST_REPLICA_REFRESH, _plist_path

    live = live_root()
    out: dict[str, Any] = {"kicked": [], "kick_failed": []}
    if live is not None:
        # Before the restarts, so the recall daemon starts warm too.
        out["precompile"] = precompile(live)
        from khipu.launchd_gen import PASSTHROUGH_ENV

        env = _code_env(live, _base_env((*DATA_DIR_ENV_KEYS, *PASSTHROUGH_ENV)))
        r = _run([_hook_python(), "-m", "khipu", "jobs", "refresh"], env=env, timeout=120)
        try:
            refreshed = json.loads(r.stdout or "{}")
        except ValueError:
            refreshed = {"ok": False, "error": _tail(r.stderr or r.stdout)}
        if not isinstance(refreshed, dict):
            refreshed = {"ok": False, "error": "jobs refresh returned an invalid result"}
        refreshed["ok"] = r.returncode == 0 and refreshed.get("ok") is True
        if not refreshed["ok"] and not refreshed.get("error"):
            refreshed["error"] = _tail(r.stderr or r.stdout) or f"jobs refresh exited {r.returncode}"
        out["jobs_refresh"] = {k: refreshed.get(k) for k in ("ok", "refreshed", "current", "external", "error", "results")
                               if k in refreshed}
    uid = os.getuid()
    for label in (PLIST_QUEUE_DRAIN, PLIST_RECALL_DAEMON):
        if not _plist_path(label).is_file():
            continue
        r = _run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"], timeout=30)
        (out["kicked"] if r.returncode == 0 else out["kick_failed"]).append(label)
    if _plist_path(PLIST_REPLICA_REFRESH).is_file():
        replica_heal.wake()
        out["replica_woken"] = True
    out["note"] = MCP_NOTE
    return out


def _post_flip_result(entry: dict[str, Any], t0: float) -> dict[str, Any]:
    # The live link has already moved. Never report it as unchanged, or queue
    # a second promotion as though this were a pre-flip gate refusal.
    entry["live_changed"] = True
    try:
        entry["after"] = _after_flip()
        refresh = entry["after"].get("jobs_refresh")
        error = (f"jobs refresh: {refresh.get('error') or 'failed'}"
                 if refresh is not None and not refresh.get("ok") else None)
    except Exception as exc:
        error = f"post-flip refresh: {type(exc).__name__}: {exc}"
    if error:
        return {**entry, "ok": False, "partial": True, "error": error,
                "ms": round((time.monotonic() - t0) * 1000)}
    return {**entry, "ok": True, "ms": round((time.monotonic() - t0) * 1000)}


# ---- commands ----------------------------------------------------------------


def promote(ref: str = "origin/main", *, retry_of: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fetch, check the inactive slot out at ``ref``, gate it, and on a pass
    flip ``live`` to it. Pre-flip failures leave the link as it was;
    post-flip refresh failures report a partial deployment with the link moved.

    A hub-only gate refusal is queued for ``retry_queued``. During a retry,
    a non-gate local failure stays queued too; only success, cancellation, or
    a real gate refusal ends that retry. ``retry_of`` is the queued entry a
    retry runs for.

    The queued entry is removed in one place, after the run returned and only
    when the run is not queued again (a re-queue replaces it in one atomic
    write): a run that dies part way leaves it queued for the next retry. The owner's
    one-time override the doctor step read is cleared only after a live flip
    or after it excused doctor reds and the whole gate passed. A failed gate
    keeps it, as does a queued retry (the retry is the same attempt)."""
    t0 = time.monotonic()
    with _lock() as held:
        if not held:
            return {"ok": False, "action": "promote", "error": "another promotion is running"}
        previous = queued()
        if retry_of is not None and not _same_queued_request(previous, retry_of):
            return record({"ok": True, "action": "retry-queued", "cancelled": True,
                           "queued": previous, "reason": "queued request was replaced or cancelled"})
        out = _promote_locked(ref, t0)
        failed = next((step for step in out.get("gate") or [] if not step["ok"]), None)
        doctor = next((step for step in out.get("gate") or [] if step.get("step") == "doctor"), None)
        override = (doctor or {}).get("override")
        retryable_hub_refusal = not out.get("ok") and failed is not None and failed.get("hub_unreachable")
        retryable_local_failure = (
            retry_of is not None
            and not out.get("ok")
            and failed is None
            and not out.get("cancelled")
            and not out.get("live_changed")
        )
        if retryable_hub_refusal or retryable_local_failure:
            # Written first (an atomic replace): a crash here keeps the old entry.
            queued_history = retry_of or previous or {}
            out["queued"] = _queue((retry_of or {}).get("ref") or ref,
                                   out.get("commit") or (retry_of or {}).get("commit"), out.get("error") or "",
                                   queued_history, (override or {}).get("set_at"))
        else:
            _clear_queue()
        if override:
            consumed = out.get("ok") or out.get("live_changed") or (doctor.get("ignored_reds") and failed is None)
            cleared = bool(consumed and not out.get("queued") and clear_gate_override(override.get("set_at")))
            out["override"] = {**override, "skipped": doctor.get("ignored_reds") or [], "cleared": cleared}
        if retry_of is not None:
            out["retry_of"] = retry_of.get("queued_at")
        return record(out)


def _promote_locked(ref: str, t0: float) -> dict[str, Any]:
    """The promotion itself, under the lock; the caller records the result."""
    repo = _repo()
    from_slot = active_slot()
    entry: dict[str, Any] = {"action": "promote", "ref": ref,
                             "from_slot": str(from_slot) if from_slot else None,
                             "from_commit": _head(from_slot)}
    cleanups = retry_cleanups()
    if cleanups:
        entry["cleanups"] = cleanups
    resolved = _fetch_and_resolve(repo, ref)
    if not resolved["ok"]:
        return {**entry, "ok": False, "error": resolved["error"],
                "ms": round((time.monotonic() - t0) * 1000)}
    slot = inactive_slot()
    entry.update(commit=resolved["commit"], slot=str(slot))
    prepared = _prepare_slot(slot, resolved["commit"], repo)
    if not prepared["ok"]:
        return {**entry, "ok": False, "error": f"slot: {prepared['error']}",
                "ms": round((time.monotonic() - t0) * 1000)}
    # The gate's replay runs the slot's hooks by the slot's own path.
    entry["precompile"] = precompile(slot)
    gate = run_gate(slot)
    entry["gate"] = gate
    entry["gate_ms"] = sum(step["ms"] for step in gate)
    failed = next((step for step in gate if not step["ok"]), None)
    if failed is not None:
        return {**entry, "ok": False, "failed_step": failed["step"],
                "error": f"{failed['step']}: {failed['detail']}",
                "ms": round((time.monotonic() - t0) * 1000)}
    entry["precompile_live"] = _before_flip()
    flipped = flip(slot)
    if not flipped["ok"]:
        return {**entry, "ok": False, "error": f"flip: {flipped['error']}",
                "ms": round((time.monotonic() - t0) * 1000)}
    return _post_flip_result(entry, t0)


def retry_queued() -> dict[str, Any]:
    """Run a queued promotion again once the hub answers. While the hub is
    still out of reach it stays queued and nothing runs."""
    entry = queued()
    if entry is None:
        return {"ok": True, "action": "retry-queued", "queued": None}
    if hub_reachable() is False:
        return {"ok": False, "action": "retry-queued", "waiting": True, "queued": entry,
                "error": "the hub is still out of reach; the promotion stays queued"}
    return promote(entry.get("commit") or entry["ref"], retry_of=entry)


RETRY_KICK_MIN_S = 4 * 60


def _same_queued_request(current: dict[str, Any] | None, expected: dict[str, Any]) -> bool:
    # Kicks update only scheduling metadata, not the intent being retried.
    return current is not None and (
        {k: v for k, v in current.items() if k != "kicked_at"}
        == {k: v for k, v in expected.items() if k != "kicked_at"}
    )


def kick_queued_retry() -> dict[str, Any] | None:
    """Called at the end of each queue-drain run: when a promotion is
    queued, start ``khipu promote --retry-queued`` in its own session, so
    the restart a successful promotion makes of queue-drain cannot stop it.
    Never raises; None when nothing is queued."""
    try:
        entry = queued()
        if entry is None:
            return None
        with _lock() as held:
            if not held:
                return {"kicked": False, "reason": "another promotion is running"}
            current = queued()
            if not _same_queued_request(current, entry):
                return {"kicked": False, "reason": "queued request was replaced or cancelled"}
            last = current.get("kicked_at")
            if last:
                age = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(str(last).replace("Z", "+00:00"))).total_seconds()
                if 0 <= age < RETRY_KICK_MIN_S:
                    return {"kicked": False, "reason": "kicked recently"}
            _write_queue({**current, "kicked_at": _now()})
        log = config_dir() / "state" / "promote-retry.log"
        with open(log, "a", encoding="utf-8") as fh:
            proc = subprocess.Popen([sys.executable, "-m", "khipu", "promote", "--retry-queued", "--json"],
                                    stdin=subprocess.DEVNULL, stdout=fh, stderr=fh,
                                    cwd=str(Path.home()), start_new_session=True)
        return {"kicked": True, "pid": proc.pid}
    except Exception as exc:  # noqa: BLE001 — the drain must never fail on this
        return {"kicked": False, "error": f"{type(exc).__name__}: {exc}"}


def rollback() -> dict[str, Any]:
    """Flip ``live`` to the other slot, with no gate, and only ever onto the
    commit that slot last held while live (``live-slots.json``, written on
    every flip). A slot a failed or gate-only run has moved since is put back
    on that commit first; a slot with no such commit is refused."""
    t0 = time.monotonic()
    with _lock() as held:
        if not held:
            return {"ok": False, "action": "rollback", "error": "another promotion is running"}
        current = active_slot()
        target = inactive_slot()
        entry: dict[str, Any] = {"action": "rollback", "from_slot": str(current) if current else None,
                                 "from_commit": _head(current), "slot": str(target)}
        if current is None:
            return record({**entry, "ok": False, "error": "no live slot to roll back from"})
        if not (target / "packages" / "cli" / "khipu").is_dir():
            return record({**entry, "ok": False, "error": f"no checkout in {target} to roll back to"})
        known = last_live().get(target.name)
        if not known:
            return record({**entry, "ok": False,
                           "error": f"no commit is recorded as having been live in {target.name}; "
                                    "nothing known-good to roll back to"})
        integrity = _git(["status", "--porcelain", "-z", "--untracked-files=all"], cwd=target)
        dirty = integrity.stdout
        # _prepare_slot provisions this one untracked link for the launchers.
        # Accept it only when it still points to this repository's shared libs;
        # other links, real directories, and all code changes remain dirty.
        if integrity.returncode == 0 and dirty == "?? .python_libs\0":
            common = _git_out(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=target)
            if common and Path(common).name == ".git":
                libs = target / ".python_libs"
                expected = Path(common).parent / ".python_libs"
                try:
                    if libs.is_symlink() and expected.is_dir() and libs.resolve() == expected.resolve():
                        dirty = ""
                except (OSError, RuntimeError):
                    pass
        if integrity.returncode != 0 or dirty:
            return record({**entry, "ok": False,
                           "error": f"dirty or unreadable rollback target {target.name}; refusing to activate it"})
        if _head(target) != known:
            r = _git(["checkout", "--detach", "--quiet", known], cwd=target)
            if r.returncode != 0 or _head(target) != known:
                return record({**entry, "ok": False,
                               "error": f"could not put {target.name} back on {known[:12]}: {_tail(r.stderr)}"})
            entry["restored"] = known
        entry["commit"] = known
        entry["precompile_live"] = _before_flip()
        flipped = flip(target)
        if not flipped["ok"]:
            return record({**entry, "ok": False, "error": f"flip: {flipped['error']}"})
        _clear_queue()
        return record(_post_flip_result(entry, t0))


def gate_only(step: str, ref: str | None = None) -> dict[str, Any]:
    """Run one gate step. With ``ref`` the inactive slot is checked out there
    first; without, the step runs against the inactive slot as it is, or the
    checkout this code runs from when there is no slot yet. Never flips."""
    if step not in GATE_STEPS:
        return {"ok": False, "action": "gate-only", "error": f"unknown step {step!r}; one of {list(GATE_ORDER)}"}
    with _lock() as held:
        if not held:
            return {"ok": False, "action": "gate-only", "error": "another promotion is running"}
        entry: dict[str, Any] = {"action": "gate-only", "step": step, "ref": ref}
        cleanups = retry_cleanups()
        if cleanups:
            entry["cleanups"] = cleanups
        slot = inactive_slot()
        if ref:
            repo = _repo()
            resolved = _fetch_and_resolve(repo, ref)
            if not resolved["ok"]:
                return record({**entry, "ok": False, "error": resolved["error"]})
            prepared = _prepare_slot(slot, resolved["commit"], repo)
            if not prepared["ok"]:
                return record({**entry, "ok": False, "error": f"slot: {prepared['error']}"})
            candidate = slot
        elif (slot / "packages" / "cli" / "khipu").is_dir():
            candidate = slot
        else:
            candidate = _repo()
        entry.update(candidate=str(candidate), commit=_head(candidate))
        result = {"step": step, **GATE_STEPS[step](candidate)}
        return record({**entry, "ok": result["ok"], "gate": [result]})


def status() -> dict[str, Any]:
    """Live slot and commit, main's commit (as last fetched), how far live is
    behind it, and the last gate result. No fetch; the one thing it may do is
    retry a pending gate cleanup (when no promotion holds the lock)."""
    with _lock() as held:
        cleanups = retry_cleanups() if held else []
    live = active_slot()
    live_commit = _head(live_link()) if live_root() is not None else None
    try:
        repo = _repo()
        main_commit = _git_out(["rev-parse", "--verify", "--quiet", "origin/main^{commit}"], cwd=repo)
    except Exception:  # noqa: BLE001
        repo, main_commit = None, None
    behind = None
    if repo is not None and live_commit and main_commit:
        count = _git_out(["rev-list", "--count", f"{live_commit}..{main_commit}"], cwd=repo)
        behind = int(count) if count and count.isdigit() else None
    rows = records()
    last_gate = next((r for r in reversed(rows) if r.get("gate")), None)
    last_promotion = next((r for r in reversed(rows) if r.get("action") == "promote" and r.get("ok")), None)
    return {
        "ok": True,
        "live": str(live_link()) if live_root() is not None else None,
        "live_slot": live.name if live else None,
        "live_commit": live_commit,
        "main_commit": main_commit,
        "behind_by": behind,
        "slots": {s.name: {"path": str(s), "exists": s.is_dir(), "commit": _head(s)} for s in slots()},
        "last_gate": ({k: last_gate.get(k) for k in ("ts", "action", "ok", "commit", "gate")}
                      if last_gate else None),
        "last_promotion": ({k: last_promotion.get(k) for k in ("ts", "ref", "commit", "slot")}
                           if last_promotion else None),
        "last_live": last_live(),
        "cleanups": cleanups,
        "cleanup_pending": [str(r["session_id"]) for r in pending_cleanups()],
        "queued": queued(),
    }


def format_result(out: dict[str, Any]) -> str:
    """Plain-text rendering of a promote / rollback / gate-only / status result."""
    lines = []
    action = out.get("action")
    if action is None and "live_slot" in out:
        lines.append(f"live: {out.get('live_slot') or 'none'} {(out.get('live_commit') or '')[:12]}")
        lines.append(f"main: {(out.get('main_commit') or '?')[:12]}  behind by: {out.get('behind_by')}")
        gate = out.get("last_gate") or {}
        if gate:
            lines.append(f"last gate: {gate.get('ts')} {gate.get('action')} "
                         f"{'ok' if gate.get('ok') else 'FAILED'} {(gate.get('commit') or '')[:12]}")
        if out.get("cleanup_pending"):
            lines.append(f"gate episodes not yet confirmed forgotten: {out['cleanup_pending']}")
        if out.get("queued"):
            q = out["queued"]
            lines.append(f"queued until the hub answers: {q.get('ref')} since {q.get('queued_at')}")
        return "\n".join(lines)
    if action == "retry-queued" and not out.get("gate"):
        if out.get("cancelled"):
            return f"retry-queued: cancelled; {out['reason']}"
        if out.get("queued") is None:
            return "retry-queued: nothing is queued"
        return f"retry-queued: {out.get('error')}"
    head = f"{action}"
    if out.get("ref"):
        head += f" {out['ref']}"
    if out.get("commit"):
        head += f" -> {out['commit'][:12]}"
    if out.get("slot"):
        head += f" in {Path(out['slot']).name}"
    lines.append(head)
    for step in out.get("gate") or []:
        lines.append(f"  {step['step']:<8} {'ok  ' if step['ok'] else 'FAIL'} {step['ms']:>6} ms  {step['detail']}")
    override = out.get("override")
    if override:
        lines.append(f"Update anyway ({', '.join(override.get('checks') or [])}): "
                     + ("used, and now cleared" if override.get("cleared")
                        else "kept for the retry" if out.get("queued") else "not cleared"))
    if out.get("ok"):
        if action in ("promote", "rollback"):
            lines.append(f"live is now {Path(out['slot']).name}. {MCP_NOTE}")
            after = out.get("after") or {}
            if after.get("kick_failed"):
                lines.append(f"kickstart failed: {after['kick_failed']}")
    else:
        lines.append(f"not done: {out.get('error')}")
        for result in ((out.get("after") or {}).get("jobs_refresh") or {}).get("results") or []:
            if not result.get("ok"):
                lines.append(f"  {result.get('job') or 'job'} failed: {result.get('error') or result}")
        if out.get("live_changed"):
            lines.append(f"live is now {Path(out['slot']).name}; deployment is partial. {MCP_NOTE}")
        elif action == "promote":
            lines.append("live is unchanged")
        if out.get("queued"):
            lines.append("queued: it runs again by itself once the hub answers")
    return "\n".join(lines)
