"""Owner turns: what the owner typed, recorded by Khipu's own prompt hook.

The owner-direction check (``owner_direction``) accepts an owner action from
a conversation only when the owner's words are in a turn recorded here. An
agent cannot write here: the only writer is the UserPromptSubmit hook, which
the harness runs with the turn the person sent. Text that reached the agent
any other way (a page, a file, a tool's output, another session's message)
never passes through this hook as this session's typed turn.

One file per session, ``<capture home>/owner-turns/<session>.jsonl``, mode
0600 in a 0700 folder: never synced, captured, searched or backed up. Lines
older than ``RETENTION_S`` go on the next write or read of the file, and on
the timer's ``sweep`` (the replica-refresh job) when neither comes. Standard
library plus ``khipu.t3`` and ``khipu.turn_text``, so the hook's recorder
starts in a few milliseconds without the rest of the package.

Which turns count (``eligible``) is decided at check time against the
current settings, so turning the check off or tightening it applies to turns
already recorded. The recorder stores the facts: harness, entrypoint, and
whether the turn was a machine message, a T3 helper or a dispatched brief,
and which process ran the hook (``hook_issuer``), which the check needs to
be above the call that acts on the turn.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from khipu import t3
from khipu.turn_text import clean_user_text, is_machine, owner_text

RETENTION_S = 24 * 3600
TEXT_MOST = 8000
TURN_LINE = "Owner turn: "
TOKEN_RE = re.compile(r"[0-9a-f]{8}")
# Front ends whose typed turns are the owner's. ``harness:entrypoint``, where
# the entrypoint is Claude Code's CLAUDE_CODE_ENTRYPOINT ("cli" interactive,
# Remote Control included; "sdk-cli" is ``claude -p``), Codex's originator
# ("codex_exec" is ``codex exec``), "t3" for a session T3 Code runs, and
# "aegis" for the Aegis pack (Grok Build included).
DEFAULT_TRUSTED = ("claude_code:cli", "claude_code:t3", "codex:codex_cli_rs", "codex:codex_desktop",
                   "codex:codex_vscode", "codex:t3", "aegis:aegis")
DEFAULTS = {
    "enabled": True,
    "latest_window_h": 6.0,
    "earlier_window_min": 30.0,
    "trusted": list(DEFAULT_TRUSTED),
    "session_binding": True,
}
SESSION_ONLY_HARNESSES = ("aegis",)
# The shells a harness runs a hook command through, and the shim itself:
# skipped on the way up to the hook's issuer. On macOS /bin/sh runs as
# /bin/bash (or dash), so the kernel's path never says "sh" alone.
HOOK_SHELLS = ("sh", "bash", "dash")
# An issuer that is one of these is an agent's own shell or a script, never
# a harness running its prompt hook: its turn never carries the owner's
# word. node is not here: Claude Code can run as node.
REFUSED_ISSUERS = ("zsh", "fish", "tcsh", "csh", "ksh")
REFUSED_ISSUER_PREFIXES = ("python", "perl", "ruby", "osascript")
_ISSUER_MOST = 16
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def home() -> Path:
    """The capture home, resolved as ``session_capture.khipu_home`` does."""
    isolated = os.environ.get("KHIPU_ISOLATED_DIR")
    if isolated:
        return Path(isolated) / "capture"
    return Path(os.environ.get("KHIPU_CAPTURE_HOME") or os.environ.get("KHIPU_AEGIS_HOME")
                or str(Path.home() / ".grok" / "khipu"))


def folder() -> Path:
    return home() / "owner-turns"


def _ensure_folder() -> Path:
    d = folder()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def settings() -> dict:
    """The owner-direction settings, defaults filled in."""
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    try:
        data = json.loads((folder() / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if isinstance(data, dict):
        for key, default in DEFAULTS.items():
            value = data.get(key)
            if isinstance(default, bool) and isinstance(value, bool):
                out[key] = value
            elif isinstance(default, float) and isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and value > 0:
                out[key] = float(value)
            elif isinstance(default, list) and isinstance(value, list) \
                    and all(isinstance(v, str) for v in value):
                out[key] = list(value)
    return out


def write_settings(values: dict) -> dict:
    current = settings()
    current.update(values)
    _write_private(_ensure_folder() / "settings.json", json.dumps(current, indent=2) + "\n")
    return settings()


def session_file(session: str) -> Path:
    return folder() / f"{_SAFE.sub('_', session)[:128]}.jsonl"


def harness_of(env: dict, payload: dict) -> str:
    """The harness running this hook, from its environment (the same signals
    ``routing.conversation_author`` reads) and the payload's transcript."""
    named = (env.get("KHIPU_HARNESS") or "").strip().lower()
    if named:
        return named
    if env.get("GROK_HOOK_NAME") or env.get("GROK_HOOK_EVENT"):
        return "aegis"
    if env.get("CLAUDECODE") or env.get("CLAUDE_CODE_ENTRYPOINT") or env.get("CLAUDE_CODE_SESSION_ID"):
        return "claude_code"
    transcript = str(payload.get("transcript_path") or "")
    if env.get("CODEX_THREAD_ID") or env.get("CODEX_HOME") or "/.codex/" in transcript:
        return "codex"
    if "/.claude" in transcript:
        return "claude_code"
    return "unknown"


def _codex_originator(transcript: str) -> str | None:
    """Codex's ``originator`` from its rollout's first line (session_meta):
    ``codex_cli_rs`` typed in the terminal, ``codex_exec`` a dispatched job."""
    if not transcript:
        return None
    try:
        with open(transcript, "rb") as fh:
            first = fh.readline(1 << 20)
        meta = json.loads(first.decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    payload = meta.get("payload") if isinstance(meta, dict) else None
    originator = payload.get("originator") if isinstance(payload, dict) else None
    return originator if isinstance(originator, str) and originator else None


def entrypoint_of(harness: str, env: dict, payload: dict, session: str) -> str:
    if harness == "aegis":
        return "aegis"
    if harness == "claude_code":
        entry = (env.get("CLAUDE_CODE_ENTRYPOINT") or "unknown").strip() or "unknown"
        # T3 Code drives Claude through the Agent SDK; a session it runs has
        # a T3 thread. ``claude -p`` (sdk-cli) never counts as T3.
        if entry.startswith("sdk-") and entry != "sdk-cli" and t3.thread_for_session(session):
            return "t3"
        return entry
    if harness == "codex":
        origin = _codex_originator(str(payload.get("transcript_path") or ""))
        if origin != "codex_exec" and t3.thread_for_session(session):
            return "t3"
        return origin or "unknown"
    return "unknown"


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def note_brief(text: str, *, parent: str, harness: str, now: float | None = None) -> None:
    """A delegation's brief (the delegation hook calls this): a turn whose
    text is this brief is a dispatched job's, never the owner's, and the
    brief names the parent session the job was started from."""
    norm = _norm(text or "")
    if len(norm) < 20 or not parent or not settings()["enabled"]:
        return
    line = {"ts": time.time() if now is None else now, "head": norm[:200],
            "sha": hashlib.sha256(norm.encode()).hexdigest(), "parent": parent, "harness": harness}
    try:
        path = _ensure_folder() / "briefs.jsonl"
        lines = [b for b in _read_lines(path) if line["ts"] - float(b.get("ts") or 0) < RETENTION_S]
        _write_private(path, "".join(json.dumps(b) + "\n" for b in lines + [line]))
    except OSError:
        pass


def brief_for(text: str, now: float | None = None) -> dict | None:
    """The recorded brief this turn's text is, if any."""
    norm = _norm(text or "")
    if not norm:
        return None
    now = time.time() if now is None else now
    for b in reversed(_read_lines(folder() / "briefs.jsonl")):
        if now - float(b.get("ts") or 0) >= RETENTION_S:
            continue
        head = str(b.get("head") or "")
        if b.get("sha") == hashlib.sha256(norm.encode()).hexdigest() or (len(head) >= 20 and head in norm):
            return b
    return None


def _read_lines(path: Path) -> list[dict]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def eligible(turn: dict, conf: dict | None = None) -> tuple[bool, str]:
    """Whether a recorded turn can carry the owner's words now, and why not."""
    conf = conf or settings()
    if turn.get("machine"):
        return False, "a message the harness wrote, not a typed turn"
    if turn.get("wrapped"):
        return False, "a wrapper away from its harness's place, so the typed words can't be told apart"
    if turn.get("helper"):
        return False, "a T3 helper session"
    if turn.get("delegated"):
        return False, "a dispatched job's brief"
    if not (turn.get("text") or "").strip():
        return False, "no typed text"
    key = f"{turn.get('harness')}:{turn.get('entrypoint')}"
    if key not in conf["trusted"]:
        return False, f"{key} is not a trusted front end"
    if turn.get("harness") in SESSION_ONLY_HARNESSES and not conf["session_binding"]:
        return False, "session-only binding is off"
    return True, ""


def _proc_info(pid: int) -> tuple[int, str, float] | None:
    """A process's parent, the path of the file it runs and when it started,
    as the kernel has them; None when they can't be read. A copy of
    ``routing._proc_info``: this module stays standard library only."""
    if sys.platform != "darwin" or pid <= 0:
        return None
    try:
        import ctypes
        import struct

        lib = ctypes.CDLL("/usr/lib/libproc.dylib")
        info = ctypes.create_string_buffer(136)  # struct proc_bsdinfo
        if lib.proc_pidinfo(pid, 3, ctypes.c_uint64(0), info, 136) != 136:  # PROC_PIDTBSDINFO
            return None
        path = ctypes.create_string_buffer(4096)  # PROC_PIDPATHINFO_MAXSIZE
        if lib.proc_pidpath(pid, path, 4096) <= 0:
            return None
        sec, usec = struct.unpack_from("QQ", info, 120)  # pbi_start_tvsec, pbi_start_tvusec
        return struct.unpack_from("I", info, 16)[0], os.fsdecode(path.value), sec + usec / 1e6
    except (OSError, AttributeError, ValueError):
        return None


def _refused_issuer(name: str) -> bool:
    low = name.lower()
    return low in REFUSED_ISSUERS or low.startswith(REFUSED_ISSUER_PREFIXES)


def hook_issuer() -> dict | None:
    """The process that ran this hook: the first one above this process that
    is not a hook shell (``HOOK_SHELLS``). ``{"pid", "start", "exe"}`` for a
    harness; ``{"refused": exe}`` when it is an interactive shell or a script
    interpreter (someone ran the shim by hand); None when the tree can't be
    read. Same-user processes can't be told apart for certain (scope 2.8):
    this closes running the shim from an agent's shell, not ``exec`` tricks."""
    me = _proc_info(os.getpid())
    if me is None:
        return None
    pid, born = me[0], me[2]
    for _ in range(_ISSUER_MOST):
        info = _proc_info(pid)
        if info is None:
            return None
        parent, exe, started = info
        if started > born:
            return None  # the parent died and its number went to a newer process
        born = started
        name = os.path.basename(os.path.realpath(exe))
        if name in HOOK_SHELLS:
            pid = parent
            continue
        if _refused_issuer(name):
            return {"refused": name}
        return {"pid": pid, "start": started, "exe": name}
    return None


def record(payload: dict, env: dict | None = None, now: float | None = None,
           issuer: dict | None = None) -> str | None:
    """Record one prompt-submit turn. Returns the turn token to push into the
    conversation when the turn is eligible and the harness shows hook output
    (Claude Code, Codex), else None. ``issuer`` None means this process's
    (``hook_issuer``). Never raises."""
    try:
        return _record(payload, os.environ if env is None else env, time.time() if now is None else now, issuer)
    except Exception:  # noqa: BLE001 - the recorder must never fail a prompt
        return None


def _record(payload: dict, env, now: float, issuer: dict | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    conf = settings()
    if not conf["enabled"]:
        return None
    session = str(payload.get("session_id") or payload.get("sessionId") or "").strip()
    prompt = payload.get("prompt")
    if not session or not isinstance(prompt, str):
        return None
    harness = harness_of(env, payload)
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else ""
    typed = owner_text(prompt)
    machine = is_machine(clean_user_text(prompt, owner=True), anywhere=True)
    # A wrapper away from its harness's place: which words are the owner's
    # can't be told, so none are kept and the turn never counts.
    wrapped = typed is None and not machine
    text = "" if typed is None else typed
    if issuer is None:
        issuer = hook_issuer() or {"refused": "unknown"}
    turn = {
        "at": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts": now, "harness": harness, "session": session,
        "entrypoint": entrypoint_of(harness, env, payload, session), "cwd": cwd,
        "turn": secrets.token_hex(4),
        "text": "" if machine else text[:TEXT_MOST],
        "machine": machine, "wrapped": wrapped, "helper": t3.is_helper_session(cwd), "issuer": issuer,
    }
    brief = None if machine else brief_for(text, now)
    turn["delegated"] = brief is not None
    if brief is not None:
        turn["parent"] = brief.get("parent")
    ok, why = eligible(turn, conf)
    turn["eligible"] = ok
    if why:
        turn["why"] = why
    path = _ensure_folder() / session_file(session).name
    line = json.dumps(turn, ensure_ascii=False) + "\n"
    # Append is the common case (one small write); the file is rewritten only
    # when its oldest line has passed the retention, and other sessions' old
    # files are swept only then or when this session's file is new.
    oldest = _first_ts(path)
    if oldest is not None and now - oldest >= RETENTION_S:
        kept = [t for t in _read_lines(path) if now - float(t.get("ts") or 0) < RETENTION_S]
        _write_private(path, "".join(json.dumps(t, ensure_ascii=False) + "\n" for t in kept) + line)
    else:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line)
    if oldest is None or now - oldest >= RETENTION_S:
        _sweep(now)
    return turn["turn"] if ok and harness not in SESSION_ONLY_HARNESSES else None


def _first_ts(path: Path) -> float | None:
    """The first line's time, or None when the file is missing or unreadable."""
    try:
        with open(path, "rb") as fh:
            first = fh.readline(1 << 16)
        return float(json.loads(first).get("ts") or 0)
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def _ts(row: dict) -> float:
    try:
        return float(row.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _prune(path: Path, now: float) -> int:
    """Drop the lines of one file older than the retention, by each line's
    own time (a file's mtime says nothing about its oldest line); delete the
    file when none remain. Returns how many lines went."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    rows = _read_lines(path) if raw else []
    kept = [r for r in rows if now - _ts(r) < RETENTION_S]
    gone = len(raw.splitlines()) - len(kept)
    if not gone:
        return 0
    if kept:
        _write_private(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept))
    else:
        path.unlink(missing_ok=True)
    return gone


def _sweep(now: float) -> dict:
    """Prune every turn file and briefs.jsonl line by line."""
    out = {"ok": True, "lines": 0, "errors": []}
    try:
        paths = sorted(folder().glob("*.jsonl"))
    except OSError as err:
        return {"ok": False, "lines": 0, "errors": [f"{folder()}: {err}"]}
    for path in paths:
        try:
            out["lines"] += _prune(path, now)
        except OSError as err:
            out["ok"] = False
            out["errors"].append(f"{path.name}: {err.strerror or err}")
    return out


def sweep(now: float | None = None) -> dict:
    """The retention, applied on a timer (the replica-refresh job and the
    capture hook's storage sweep), so the owner's words go a day after they
    were typed even when no later prompt or read comes: every turn file and
    briefs.jsonl line by line (a line a crash cut short goes too), the
    widenings waiting for a yes, and the rewrite temps a crash left. Local
    files only. Never raises; ``ok`` false names what could not be pruned."""
    now = time.time() if now is None else now
    try:
        out = _sweep(now)
    except Exception as err:  # noqa: BLE001 - a timer sweep reports, never fails its job
        out = {"ok": False, "lines": 0, "errors": [f"{type(err).__name__}: {err}"]}
    try:
        from khipu import owner_files

        out["temps"] = owner_files.clean_temps(now=now)["removed"]
    except Exception as err:  # noqa: BLE001
        out["ok"] = False
        out["errors"].append(f"rewrite temps: {type(err).__name__}: {err}")
    try:
        from khipu import owner_direction

        out["pending"] = owner_direction.expire_pending(now)
    except Exception as err:  # noqa: BLE001
        out["ok"] = False
        out["errors"].append(f"pending widenings: {type(err).__name__}: {err}")
    return out


def forget_sessions(sessions) -> dict:
    """Forget's owner words scan on the turn files: delete every recorded
    turn of these sessions (their files whole, whatever a crash left in
    them, and their lines in any other file) and the briefs they gave or
    were. Rewrite temps are ``owner_files.clean_temps``'s. Raises OSError
    when a file can't be read or changed, so the forget stays open."""
    names = {n for n in sessions if n}
    out = {"files": 0, "lines": 0, "briefs": 0}
    d = folder()
    if not names or not d.exists():
        return out
    files = {session_file(n).name for n in names}
    # scandir, not glob: a folder that can't be listed must raise, never
    # look empty.
    with os.scandir(d) as entries:
        listed = sorted(e.name for e in entries)
    texts: set[str] = set()
    for name in listed:
        path = d / name
        if name == "briefs.jsonl" or not name.endswith(".jsonl"):
            continue
        path.read_text(encoding="utf-8")  # unreadable raises
        rows = _read_lines(path)
        if path.name in files:
            # The session's own file goes whole: a line a crash cut short
            # holds the words too, though it reads as no turn.
            texts |= {hashlib.sha256(_norm(str(r.get("text") or "")).encode()).hexdigest() for r in rows}
            path.unlink()
            out["files"] += 1
            continue
        mine = [r for r in rows if r.get("session") in names]
        if not mine:
            continue
        texts |= {hashlib.sha256(_norm(str(r.get("text") or "")).encode()).hexdigest() for r in mine}
        kept = [r for r in rows if r not in mine]
        if kept:
            _write_private(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept))
            out["lines"] += len(mine)
        else:
            path.unlink()
            out["files"] += 1
    briefs = d / "briefs.jsonl"
    rows = _read_lines(briefs)
    kept = [b for b in rows if b.get("parent") not in names and b.get("sha") not in texts]
    if len(kept) != len(rows):
        _write_private(briefs, "".join(json.dumps(b) + "\n" for b in kept))
        out["briefs"] = len(rows) - len(kept)
    return out


def turns(session: str, now: float | None = None) -> list[dict]:
    """The session's recorded turns, oldest first, within the retention."""
    now = time.time() if now is None else now
    path = session_file(session)
    rows = _read_lines(path)
    if any(now - _ts(t) >= RETENTION_S for t in rows):
        # Retention is a deadline on the words, not a filter on the read.
        try:
            _prune(path, now)
        except OSError:
            pass
    return [t for t in rows if t.get("session") == session and now - _ts(t) < RETENTION_S]


def counts(now: float | None = None) -> dict:
    """Turns recorded in the last day per harness (doctor's row)."""
    now = time.time() if now is None else now
    out: dict[str, dict] = {}
    for path in folder().glob("*.jsonl"):
        if path.name == "briefs.jsonl":
            continue
        for t in _read_lines(path):
            if now - float(t.get("ts") or 0) >= RETENTION_S:
                continue
            row = out.setdefault(str(t.get("harness") or "unknown"), {"turns": 0, "eligible": 0, "entrypoints": {}})
            row["turns"] += 1
            row["eligible"] += 1 if t.get("eligible") else 0
            entry = str(t.get("entrypoint") or "unknown")
            row["entrypoints"][entry] = row["entrypoints"].get(entry, 0) + 1
    return out


_HARNESS_WORDS = {"claude_code": "Claude Code", "codex": "Codex", "aegis": "Aegis"}


def _prompt_hook_installed() -> list[str]:
    from khipu import integrations

    out = []
    for harness in _HARNESS_WORDS:
        try:
            if integrations.status(harness).get("prompt_recall") == "installed":
                out.append(harness)
        except Exception:  # noqa: BLE001 - an unreadable pack is not a recorded one
            continue
    return out


def doctor_block(now: float | None = None, installed: list[str] | None = None) -> dict:
    """Doctor's "owner direction" row: turns recorded in the last day per
    harness. Red when the check is on and a harness whose prompt hook is
    installed recorded none (no record means every direction from it is
    refused). Off says off."""
    per = counts(now)
    if not settings()["enabled"]:
        return {"ok": True, "enabled": False, "harnesses": per, "missing": [], "warnings": [],
                "summary": "Owner direction is off."}
    installed = _prompt_hook_installed() if installed is None else installed
    missing = [h for h in installed if not per.get(h, {}).get("turns")]
    warnings = [f"Owner direction is on, but no turn from {_HARNESS_WORDS.get(h, h)} was recorded in the last day; "
                f"its prompt hook may not be running, and owner directions from it are refused." for h in missing]
    parts = [f"{_HARNESS_WORDS.get(h, h)} {row['turns']}" for h, row in sorted(per.items())]
    return {"ok": not missing, "enabled": True, "harnesses": per, "missing": missing, "warnings": warnings,
            "summary": "Turns recorded in the last day: " + (", ".join(parts) if parts else "none") + "."}


def with_turn_line(out: dict, token: str | None) -> dict:
    """The hook's output with the turn token added to what the conversation
    sees. ``{}`` (nothing recalled) becomes a context of just that line.
    Anything but a token the recorder makes is left out. The recall client
    (``bin/khipu-recall-client.py``) carries a copy, pinned equal by test."""
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        return out
    line = f"{TURN_LINE}{token}"
    out = dict(out) if isinstance(out, dict) else {}
    hso = out.get("hookSpecificOutput")
    if isinstance(hso, dict):
        hso = dict(hso)
        ctx = hso.get("additionalContext")
        hso["additionalContext"] = f"{ctx}\n\n{line}" if isinstance(ctx, str) and ctx else line
        out["hookSpecificOutput"] = hso
    else:
        out["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit", "additionalContext": line}
    return out


def main(argv: list[str]) -> int:
    """The prompt hook's recorder: the payload on stdin, prints the token
    (or nothing). Always exits 0."""
    import sys

    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    except ValueError:
        return 0
    token = record(payload)
    if token:
        sys.stdout.write(token)
    return 0
