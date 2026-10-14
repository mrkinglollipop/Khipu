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
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

LIVE_NAME = "live"
SLOT_NAMES = ("live-a", "live-b")
GATE_ORDER = ("doctor", "recall", "capture", "version")
# launchd starts a job with this PATH and nothing else (no plist sets one).
LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
RECALL_BUDGET_MS = 1200.0
REPLAY_WINDOW_S = 24 * 60 * 60
REPLAY_TAIL_BYTES = 2 * 1024 * 1024
GATE_HARNESS = "claude_code"
MCP_NOTE = "Running MCP servers keep the old code until their session restarts."
# The replay builds its hook environment from these names only, so a
# credential in the caller's environment never reaches it.
REPLAY_ENV_KEYS = ("HOME", "USER", "LOGNAME", "SHELL", "PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")
# Variables that would redirect a hook away from what the harness gives it.
_HOOK_ENV_DROP = (
    "KHIPU_ROOT", "ALZY_ROOT", "PYTHONPATH", "PYTHONHOME", "KHIPU_CAPTURE_HOME", "KHIPU_AEGIS_HOME",
    "KHIPU_CAPTURE_QUEUE_ONLY", "KHIPU_CAPTURE_NO_DRAIN", "KHIPU_HARNESS", "KHIPU_HOOK_STDIN",
    "KHIPU_HOOK_INLINE", "KHIPU_PARENT_SESSION", "KHIPU_RECALL_DEADLINE", "KHIPU_GATE",
    "GROK_HOOK_NAME", "GROK_HOOK_EVENT", "CLAUDE_CODE_HOST_SESSION_ID",
)
_SUMMARY_RE = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \[khipu-prompt-recall\] "
    r"session=(?P<session>\S*) reason=(?P<reason>.*?) ms=(?P<ms>[0-9.]+) "
    r"hits=(?P<hits>\[.*?\]) legs="
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


def hook_log_path() -> Path:
    """The per-prompt hook's own log (``khipu.recall_prompt._log_path``)."""
    home = os.environ.get("KHIPU_CAPTURE_HOME") or os.environ.get("KHIPU_AEGIS_HOME")
    base = Path(home) if home else Path.home() / ".grok" / "khipu"
    return base / "logs" / "prompt-recall.log"


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


def _code_env(root: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """``base`` (default: the caller's environment minus redirects) with
    PYTHONPATH set the way the launchers set it for ``root``."""
    env = _harness_env() if base is None else dict(base)
    env["PYTHONPATH"] = f"{Path(root) / 'packages' / 'cli'}:{Path(root) / '.python_libs'}"
    env.setdefault("PYTHONPYCACHEPREFIX", str(Path.home() / "Library" / "Caches" / "Khipu" / "pycache"))
    return env


def _harness_env() -> dict[str, str]:
    """What a harness hands a hook: the caller's environment, minus anything
    that would redirect the hook from its own root, queue or harness."""
    env = dict(os.environ)
    for key in _HOOK_ENV_DROP:
        env.pop(key, None)
    return env


# ---- processes ---------------------------------------------------------------


def _run(argv: list[str], *, env: dict[str, str] | None = None, input: str | None = None,
         cwd: str | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
    """Every subprocess the gate starts goes through here (tests mock it)."""
    try:
        return subprocess.run(argv, env=env, input=input, cwd=cwd, capture_output=True,
                              text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
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
    return {"ok": True, "from": before, "to": str(slot)}


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


def _step(fn: Callable[..., tuple[bool, str]]) -> Callable[..., dict[str, Any]]:
    """Wrap a check returning (ok, detail) into ``{"ok", "ms", "detail"}``;
    a crash is a failed step, never a pass."""
    def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        t0 = time.monotonic()
        try:
            ok, detail = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — a crashed check must not look like a pass
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        return {"ok": bool(ok), "ms": round((time.monotonic() - t0) * 1000), "detail": detail}
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


@_step
def gate_doctor(candidate: Path) -> tuple[bool, str]:
    """1. ``khipu doctor --quick`` from the candidate under the environment a
    launchd job rendered for it would get, so a missing binary or module
    shows here and not at the next scheduled run."""
    from khipu import launchd_gen

    ctx = launchd_gen.render_context(root=Path(candidate))
    r = _run([ctx["KHIPU_PYTHON"], "-m", "khipu", "doctor", "--quick"], env=_launchd_env(ctx),
             cwd=ctx["WORKING_DIRECTORY"], timeout=90)
    try:
        out = json.loads(r.stdout or "{}")
    except ValueError:
        out = {}
    if r.returncode == 0 and out.get("ok") is True:
        return True, f"doctor --quick ok ({ctx['KHIPU_PYTHON']}, PATH={LAUNCHD_PATH})"
    problems = []
    failed_imports = (out.get("imports") or {}).get("failed") or {}
    if failed_imports:
        problems.append("imports: " + "; ".join(f"{k} ({v})" for k, v in failed_imports.items()))
    if out and not (out.get("config") or {}).get("ok"):
        problems.append(f"config: {(out.get('config') or {}).get('error')}")
    if out and not out.get("dsn_present"):
        problems.append("no DSN")
    if out and not out.get("dsn_file_ok", True):
        problems.append("DSN file unhealthy")
    if out.get("job_failures"):
        problems.append(f"jobs failing: {out['job_failures']}")
    if out and not out.get("synth_provider_ok", True):
        problems.append(f"summariser provider: {out.get('synth_provider')}")
    if not out:
        problems.append(f"exit {r.returncode}: {_tail(r.stderr or r.stdout)}")
    return False, "; ".join(problems) or f"exit {r.returncode}"


def _parse_summary(line: str) -> dict[str, Any] | None:
    m = _SUMMARY_RE.match(line)
    if not m:
        return None
    try:
        hits = [str(h) for h in ast.literal_eval(m.group("hits"))]
    except (ValueError, SyntaxError):
        hits = []
    out: dict[str, Any] = {"stamp": m.group("stamp"), "session": m.group("session"),
                           "reason": m.group("reason"), "ms": float(m.group("ms")), "hits": hits}
    if " replay=" in line:
        try:
            replay = json.loads(line.rsplit(" replay=", 1)[1])
        except ValueError:
            replay = None
        if isinstance(replay, dict) and isinstance(replay.get("prompt"), str):
            out["prompt"] = replay["prompt"]
            out["cwd"] = str(replay.get("cwd") or "")
    return out


def _read_tail(path: Path, max_bytes: int = REPLAY_TAIL_BYTES) -> str:
    with open(path, "rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        fh.seek(max(0, size - max_bytes))
        return fh.read().decode("utf-8", errors="replace")


def sample_prompts(log: Path, n: int = 5, *, now: float | None = None) -> list[dict[str, Any]]:
    """Up to ``n`` distinct prompts the hook searched for in the last day,
    spread evenly over the day, read from the hook's own log and nothing else."""
    from khipu.recall_prompt import _classify_call_reason

    now = time.time() if now is None else now
    try:
        text = _read_tail(Path(log))
    except OSError:
        return []
    seen: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        row = _parse_summary(line)
        if not row or not (row.get("prompt") or "").strip():
            continue
        try:
            stamp = datetime.strptime(row["stamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if now - stamp.timestamp() > REPLAY_WINDOW_S or _classify_call_reason(row["reason"]) is None:
            continue
        seen.pop(row["prompt"], None)
        seen[row["prompt"]] = row
    rows = list(seen.values())
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)]


def _replay_env(capture_home: Path) -> dict[str, str]:
    env = {key: os.environ[key] for key in REPLAY_ENV_KEYS if key in os.environ}
    # Its own log and dedup state, and the one-shot path: the warm service
    # answers with whatever code it started on, which is not the candidate.
    env["KHIPU_CAPTURE_HOME"] = str(capture_home)
    env["KHIPU_RECALL_DAEMON"] = "0"
    return env


def _recall_once(hook: Path, sample: dict[str, Any], session: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="khipu-gate-recall-") as tmp:
        payload = {"session_id": session, "prompt": sample["prompt"], "cwd": sample.get("cwd") or "",
                   "hook_event_name": "UserPromptSubmit"}
        t0 = time.monotonic()
        r = _run([str(hook)], env=_replay_env(Path(tmp)), input=json.dumps(payload), timeout=15)
        wall_ms = round((time.monotonic() - t0) * 1000)
        row = None
        log = Path(tmp) / "logs" / "prompt-recall.log"
        try:
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
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
            "hits": row["hits"] if row else []}


@_step
def gate_recall_replay(candidate: Path, n: int = 5) -> tuple[bool, str]:
    """2. Replay real prompts from the last day of the per-prompt hook's log
    through the candidate's ``khipu-prompt-recall``, exactly as a harness runs
    it (stdin JSON), each inside the 1.2 s budget, and compare the hit ids with
    the live code's answer; an empty answer where live had one refuses."""
    log = hook_log_path()
    samples = sample_prompts(log, n)
    if not samples:
        return False, (f"no replayable prompt in the last day of {log} (summary lines carry "
                       "replay= from the release that added this gate on)")
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
    for i, sample in enumerate(samples):
        live = _recall_once(live_hook, sample, f"khipu-gate-{nonce}-live-{i}") if live_hook.exists() else None
        cand = _recall_once(cand_hook, sample, f"khipu-gate-{nonce}-cand-{i}")
        label = f"prompt {i + 1}"
        reason = cand["reason"] or ""
        if cand["exit"] != 0 or not cand["valid_json"]:
            problems.append(f"{label}: hook exit {cand['exit']}, valid_json={cand['valid_json']}")
            continue
        if cand["reason"] is None:
            problems.append(f"{label}: the hook wrote no summary line")
            continue
        if reason.startswith(("timeout", "error", "gate error")):
            problems.append(f"{label}: {reason}")
            continue
        if cand["ms"] is not None and cand["ms"] > RECALL_BUDGET_MS:
            problems.append(f"{label}: {cand['ms']:.0f} ms > {RECALL_BUDGET_MS:.0f} ms")
            continue
        live_hits = (live or {}).get("hits") or []
        if live_hits and not cand["hits"]:
            problems.append(f"{label}: empty answer where live had {len(live_hits)} hit(s)")
            continue
        overlap = len(set(live_hits) & set(cand["hits"]))
        notes.append(f"{label}: {len(cand['hits'])} hit(s), {overlap} shared with live, {cand['ms']:.0f} ms")
    if problems:
        return False, "; ".join(problems)
    return True, f"{len(samples)} prompt(s) replayed; " + "; ".join(notes)


def _fixture(candidate: Path) -> Path:
    rel = Path("packages") / "cli" / "tests" / "fixtures" / "gate_transcript.jsonl"
    own = Path(candidate) / rel
    if own.is_file():
        return own
    return Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "gate_transcript.jsonl"


@_step
def gate_capture(candidate: Path) -> tuple[bool, str]:
    """3. One real capture end to end: a fixture transcript through the
    candidate's ``khipu-stop-hook`` (inline drain, its own queue), the episode
    found in the hub, then forgotten."""
    cand = Path(candidate)
    hook = _launcher(cand, "khipu-stop-hook")
    if not hook.is_file():
        return False, f"candidate has no {hook}"
    fixture = _fixture(cand)
    if not fixture.is_file():
        return False, f"no fixture transcript at {fixture}"
    sid = f"khipu-gate-{uuid.uuid4().hex[:12]}"
    stored = f"{GATE_HARNESS}:{sid}"
    with tempfile.TemporaryDirectory(prefix="khipu-gate-capture-") as tmp:
        home = Path(tmp) / "capture"
        transcript = Path(tmp) / "transcript.jsonl"
        shutil.copyfile(fixture, transcript)
        env = _harness_env()
        env.update(KHIPU_CAPTURE_HOME=str(home), KHIPU_HOOK_INLINE="1",
                   KHIPU_HARNESS=GATE_HARNESS, KHIPU_GATE="1")
        payload = {"session_id": sid, "transcript_path": str(transcript),
                   "hook_event_name": "SessionEnd", "cwd": tmp}
        r = _run([str(hook)], env=env, input=json.dumps(payload), timeout=100)
        left = sorted(p.name for p in (home / "queue").glob("*.json")) if (home / "queue").is_dir() else []
        try:
            log = (home / "logs" / "session-capture.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            log = ""
    py = _hook_python()
    code_env = _code_env(cand)
    found = _run([py, "-c", _FIND_EPISODES, stored], env=code_env, timeout=60)
    try:
        ids = [int(x) for x in json.loads(found.stdout or "[]")]
    except (ValueError, TypeError):
        return False, f"episode lookup failed: {_tail(found.stderr or found.stdout)}"
    if not ids:
        why = f"{len(left)} job(s) still queued" if left else "queue empty"
        return False, (f"no episode for {stored} after the stop hook (exit {r.returncode}, {why}); "
                       f"capture log: {_tail(log, 600) or 'empty'}")
    forgotten, failed = [], []
    for episode_id in ids:
        f = _run([py, "-m", "khipu", "episode", "forget", str(episode_id)], env=code_env, timeout=60)
        (forgotten if f.returncode == 0 else failed).append(episode_id)
    if failed:
        return False, f"episode(s) {ids} landed but forget failed for {failed}; forget them by hand"
    return True, f"episode(s) {ids} landed for {stored} and were forgotten"


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
    """Every step in order; the first failure stops the rest."""
    results = []
    for name in GATE_ORDER:
        res = {"step": name, **GATE_STEPS[name](Path(candidate))}
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
        lines = state_file().read_text(encoding="utf-8").splitlines()
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


def _after_flip() -> dict[str, Any]:
    """Re-render any plist the new live code renders differently, then
    restart the two long-running jobs that hold the old modules."""
    from khipu.jobs import PLIST_QUEUE_DRAIN, PLIST_RECALL_DAEMON, _plist_path

    live = live_root()
    out: dict[str, Any] = {"kicked": [], "kick_failed": []}
    if live is not None:
        r = _run([_hook_python(), "-m", "khipu", "jobs", "refresh"], env=_code_env(live), timeout=120)
        try:
            refreshed = json.loads(r.stdout or "{}")
        except ValueError:
            refreshed = {"ok": False, "error": _tail(r.stderr or r.stdout)}
        out["jobs_refresh"] = {k: refreshed.get(k) for k in ("ok", "refreshed", "current", "external", "error")
                               if k in refreshed}
    uid = os.getuid()
    for label in (PLIST_QUEUE_DRAIN, PLIST_RECALL_DAEMON):
        if not _plist_path(label).is_file():
            continue
        r = _run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"], timeout=30)
        (out["kicked"] if r.returncode == 0 else out["kick_failed"]).append(label)
    out["note"] = MCP_NOTE
    return out


# ---- commands ----------------------------------------------------------------


def promote(ref: str = "origin/main") -> dict[str, Any]:
    """Fetch, check the inactive slot out at ``ref``, gate it, and on a pass
    flip ``live`` to it. A failure anywhere leaves the link as it was."""
    t0 = time.monotonic()
    with _lock() as held:
        if not held:
            return {"ok": False, "action": "promote", "error": "another promotion is running"}
        repo = _repo()
        from_slot = active_slot()
        entry: dict[str, Any] = {"action": "promote", "ref": ref,
                                 "from_slot": str(from_slot) if from_slot else None,
                                 "from_commit": _head(from_slot)}
        resolved = _fetch_and_resolve(repo, ref)
        if not resolved["ok"]:
            return record({**entry, "ok": False, "error": resolved["error"],
                           "ms": round((time.monotonic() - t0) * 1000)})
        slot = inactive_slot()
        entry.update(commit=resolved["commit"], slot=str(slot))
        prepared = _prepare_slot(slot, resolved["commit"], repo)
        if not prepared["ok"]:
            return record({**entry, "ok": False, "error": f"slot: {prepared['error']}",
                           "ms": round((time.monotonic() - t0) * 1000)})
        gate = run_gate(slot)
        entry["gate"] = gate
        entry["gate_ms"] = sum(step["ms"] for step in gate)
        failed = next((step for step in gate if not step["ok"]), None)
        if failed is not None:
            return record({**entry, "ok": False, "failed_step": failed["step"],
                           "error": f"{failed['step']}: {failed['detail']}",
                           "ms": round((time.monotonic() - t0) * 1000)})
        flipped = flip(slot)
        if not flipped["ok"]:
            return record({**entry, "ok": False, "error": f"flip: {flipped['error']}",
                           "ms": round((time.monotonic() - t0) * 1000)})
        entry["after"] = _after_flip()
        return record({**entry, "ok": True, "ms": round((time.monotonic() - t0) * 1000)})


def _restore_point(target: Path) -> str | None:
    """The commit ``target`` held when it was last live: the ``from_commit``
    of the last successful promotion away from it."""
    for row in reversed(records()):
        if row.get("action") == "promote" and row.get("ok"):
            if row.get("from_slot") == str(target):
                return row.get("from_commit")
            return None
    return None


def rollback() -> dict[str, Any]:
    """Flip ``live`` to the other slot, with no gate. If a failed or gate-only
    run has since moved that slot, it is first put back on the commit it was
    live on."""
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
        restore = _restore_point(target)
        if restore and _head(target) != restore:
            r = _git(["checkout", "--detach", "--quiet", restore], cwd=target)
            if r.returncode != 0:
                return record({**entry, "ok": False,
                               "error": f"could not put {target.name} back on {restore[:12]}: {_tail(r.stderr)}"})
            entry["restored"] = restore
        entry["commit"] = _head(target)
        flipped = flip(target)
        if not flipped["ok"]:
            return record({**entry, "ok": False, "error": f"flip: {flipped['error']}"})
        entry["after"] = _after_flip()
        return record({**entry, "ok": True, "ms": round((time.monotonic() - t0) * 1000)})


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
    behind it, and the last gate result. Reads only; no fetch."""
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
        return "\n".join(lines)
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
    if out.get("ok"):
        if action in ("promote", "rollback"):
            lines.append(f"live is now {Path(out['slot']).name}. {MCP_NOTE}")
            after = out.get("after") or {}
            if after.get("kick_failed"):
                lines.append(f"kickstart failed: {after['kick_failed']}")
    else:
        lines.append(f"not done: {out.get('error')}")
        if action == "promote":
            lines.append("live is unchanged")
    return "\n".join(lines)
