"""Owner turns: when the owner last typed in each conversation, recorded by
Khipu's own prompt hook.

The owner-direction check (``owner_direction``) accepts an owner action from
a conversation only when an owner turn of that conversation is recorded here
within the window. An agent cannot write here: the only writer is the
UserPromptSubmit hook, which the harness runs with the turn the person sent.
A background job, a dispatched job's brief or another session's message is
never recorded as this conversation's typed turn.

One file per session, ``<capture home>/owner-turns/<session>.jsonl``, mode
0600 in a 0700 folder: never synced, captured, searched or backed up. Each
line is a time and facts about the turn, never its text. Lines older than
``RETENTION_S`` go on the next write or read of the file, and on the timer's
``sweep`` (the replica-refresh job) when neither comes. Standard library plus
``khipu.t3`` and ``khipu.turn_text``, so the hook's recorder starts in a few
milliseconds without the rest of the package.

Which turns count (``eligible``) is decided at check time against the
current settings, so turning the check off or tightening it applies to turns
already recorded. The recorder stores the facts: harness, entrypoint,
whether the turn held typed words, whether it was a machine message, a T3
helper or a dispatched brief, and which process ran the hook
(``hook_issuer``), which the check needs to be above the call that acts.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from khipu import antigravity_db, hook_shape, t3
from khipu.turn_text import clean_user_text, is_machine, owner_text

RETENTION_S = 24 * 3600
# Front ends whose typed turns are the owner's. ``harness:entrypoint``, where
# the entrypoint is Claude Code's CLAUDE_CODE_ENTRYPOINT ("cli" a terminal,
# Remote Control and the JetBrains plugin included; "claude-desktop" and
# "claude-desktop-3p" the desktop app's Code tab; "claude-vscode" the VS Code
# extension; "remote", "remote_desktop" and "remote_mobile" a cloud session
# typed in from the web, desktop or mobile app), Codex's originator
# ("codex_cli_rs" the terminal, "codex_desktop" the app, "codex_vscode" the
# extension), "t3" for a session T3 Code runs, and "aegis" for the Aegis pack
# (Grok Build included). Never trusted: "sdk-cli" (``claude -p``), "sdk-ts"
# and "sdk-py" (the Agent SDK), "mcp", "local-agent" and the "remote_*"
# triggers, Slack and Teams, "codex_exec" (``codex exec``) and "codex_sdk_ts".
DEFAULT_TRUSTED = ("claude_code:cli", "claude_code:claude-desktop", "claude_code:claude-desktop-3p",
                   "claude_code:claude-vscode", "claude_code:remote", "claude_code:remote_desktop",
                   "claude_code:remote_mobile", "claude_code:t3", "codex:codex_cli_rs", "codex:codex_desktop",
                   "codex:codex_vscode", "codex:t3", "aegis:aegis")
# Entrypoints a job runs as, not a person: a refusal never suggests trusting them.
MACHINE_ENTRYPOINTS = ("sdk-cli", "sdk-ts", "sdk-py", "mcp", "claude-code-github-action", "local-agent",
                       "remote_trigger", "remote_cowork_trigger", "codex_exec", "codex_sdk_ts", "unknown")
DEFAULTS = {
    "enabled": True,
    "latest_window_h": 6.0,
    "trusted": list(DEFAULT_TRUSTED),
    "session_binding": True,
}
SESSION_ONLY_HARNESSES = ("aegis",)
# The shells the prompt shim runs as. On macOS /bin/sh runs as /bin/bash
# (or dash), so the kernel's path never says "sh" alone.
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
                try:
                    number = float(value)
                except OverflowError:
                    continue
                if math.isfinite(number):
                    out[key] = number
            elif isinstance(default, list) and isinstance(value, list) \
                    and all(isinstance(v, str) for v in value):
                out[key] = list(value)
    return out


def write_settings(values: dict) -> dict:
    if "latest_window_h" in values:
        try:
            value = float(values["latest_window_h"])
        except (TypeError, ValueError, OverflowError):
            raise ValueError("latest_window_h must be a finite number above 0") from None
        if not math.isfinite(value) or value <= 0:
            raise ValueError("latest_window_h must be a finite number above 0")
        values = dict(values, latest_window_h=value)
    current = settings()
    current.update(values)
    _write_private(_ensure_folder() / "settings.json", json.dumps(current, indent=2, allow_nan=False) + "\n")
    return settings()


def session_file(session: str) -> Path:
    return folder() / f"{_SAFE.sub('_', session)[:128]}.jsonl"


def harness_of(env: dict, payload: dict) -> str:
    """The harness running this hook (``khipu.hook_shape.detect``): the
    harness's own signals first (the payload, its transcript path, its own
    environment markers), an environment it merely inherited last, so a Codex
    job started from inside a Claude Code session is Codex, not claude_code."""
    return hook_shape.detect(payload, env)[0]


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


def _t3_thread(session: str) -> str | None:
    """The T3 thread running ``session``, from T3's own record. Every row is
    looked at (a long-running conversation's row falls behind the newest
    window as other threads start), and a failed read (a lock, the deadline)
    is tried once more; a second failure means no thread."""
    for _ in range(2):
        thread, definitive = t3.thread_for_session_result(session, whole_table=True)
        if definitive:
            return thread
    return None


def _t3_membership(session: str) -> str:
    """"t3" when T3 runs ``session``, "none" when T3's record settles that it
    does not (or there is no T3 on this Mac), "unverified" when the record
    exists but could not be read twice. Owner eligibility must not treat a
    failed read as "not T3": that would hand a T3 session its native, trusted
    entrypoint back."""
    if not session:
        return "none"
    path = Path(os.environ.get("KHIPU_T3_DB") or Path.home() / ".t3/userdata/statev2.sqlite")
    if not path.is_file():
        return "none"
    for _ in range(2):
        thread, definitive = t3.thread_for_session_result(session, whole_table=True)
        if definitive:
            return "t3" if thread else "none"
    return "unverified"


def _t3_entry(session: str, native: str) -> str:
    member = _t3_membership(session)
    return {"t3": "t3", "unverified": "t3-unverified"}.get(member, native)


def dispatched_lane(env: dict) -> bool:
    """A dispatched job's environment (Khipu's launchers and church-harness
    set ``CLAUDE_DISPATCHED_LANE``; unset, empty and "0" mean the owner's own
    session), as ``recall_prompt.dispatched_lane`` reads it."""
    return (env.get("CLAUDE_DISPATCHED_LANE") or "").strip() not in ("", "0")


# T3's Agent SDK sets these for its own top-level threads too, so inside T3
# they are not evidence of a job; T3's delegated-thread record decides.
_T3_SDK_LINEAGE = ("CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_HOST_SESSION_ID")


def job_marker(env: dict) -> bool:
    """A job marker that holds in every harness, T3 included: the runner's
    lane marker, ``KHIPU_JOB=1`` or a parent session."""
    return _marked(env, "", t3_thread=True)


def _marked(env: dict, session: str, *, t3_thread: bool) -> bool:
    from khipu import routing_advice

    if dispatched_lane(env):
        return True
    # The lane marker's meaning ("0" is the owner's) is ``dispatched_lane``'s.
    own = {k: v for k, v in env.items()
           if k != "CLAUDE_DISPATCHED_LANE" and not (t3_thread and k in _T3_SDK_LINEAGE)}
    if session and not (own.get("CLAUDE_CODE_SESSION_ID") or "").strip():
        own["CLAUDE_CODE_SESSION_ID"] = session
    return routing_advice.is_dispatched(own)


def dispatched_job(env: dict, session: str = "", *, t3_thread: bool = False) -> str | None:
    """Why this context is a dispatched job's, never the owner's, or None.

    One test for the recorder and the owner-direction check: the runner's
    lane marker, then the lineage markers ``routing_advice.is_dispatched``
    reads (``KHIPU_JOB=1``, a parent session, a Claude Code child, a host
    session id that is not this session's), with ``session`` standing in
    for an own id the environment does not name. ``t3_thread``: the session
    runs in T3, where the Claude Code child and host-id markers are set for
    the owner's own threads too, so they are skipped and T3's record of a
    delegated child thread decides; a lookup that does not complete fails
    closed."""
    if _marked(env, session, t3_thread=t3_thread):
        return "a dispatched job's turn"
    if t3_thread:
        thread = _t3_thread(session) if session else None
        if not thread:
            return "a T3 thread Khipu could not look up, so it can't tell it is not a job"
        for _ in range(2):
            delegated, definitive = t3.delegated_thread_result(thread)
            if definitive:
                return "a T3 delegated child thread" if delegated else None
        return "a T3 thread Khipu could not look up, so it can't tell it is not a job"
    return None


def entrypoint_of(harness: str, env: dict, payload: dict, session: str, issuer: dict | None = None) -> str:
    if harness == "aegis":
        return _t3_entry(session, "aegis")
    if harness == "claude_code":
        entry = (env.get("CLAUDE_CODE_ENTRYPOINT") or "unknown").strip() or "unknown"
        # Claude Code turns only "cli" into "sdk-cli" for ``claude -p``: a
        # job started from a desktop, VS Code or remote session keeps that
        # session's entrypoint, so the issuer's own command line decides.
        if (issuer or {}).get("print") and not entry.startswith("sdk-"):
            return "sdk-cli"
        # A session T3 Code runs has a T3 thread, whatever entrypoint it
        # reports: T3 turns are never owner turns, so membership decides, not
        # the native entrypoint. ``claude -p`` (sdk-cli) stays a machine.
        return entry if entry == "sdk-cli" else _t3_entry(session, entry)
    if harness == "codex":
        origin = _codex_originator(str(payload.get("transcript_path") or ""))
        return "codex_exec" if origin == "codex_exec" else _t3_entry(session, origin or "unknown")
    if harness == "cursor":
        return _t3_entry(session, hook_shape.cursor_entrypoint(payload))
    if harness == "antigravity":
        surface = hook_shape.antigravity_entrypoint(payload)
        return _t3_entry(session, surface)
    return "unknown"


def canonical_prompt(text: str) -> str:
    """The one canonical form of a prompt's text, shared by the recorder and
    capture (routing plan section 13 E): Unicode NFC, trimmed, whitespace
    collapsed. Only its hash is ever stored."""
    return " ".join(unicodedata.normalize("NFC", text or "").split())


def prompt_sha256(text: str) -> str:
    return hashlib.sha256(canonical_prompt(text).encode("utf-8")).hexdigest()


def occurrences_folder() -> Path:
    return folder() / "occurrences"


def occurrence_file(session: str) -> Path:
    return occurrences_folder() / f"{_SAFE.sub('_', session)[:128]}.json"


def _high_water_file(session: str) -> Path:
    return occurrences_folder() / f"{_SAFE.sub('_', session)[:128]}.hw.json"


def _high_water(session: str) -> dict:
    """The durable high-water counts kept beside (not in) the counter
    file, so a counter deleted after its turn records were pruned is
    still known to have existed: recreating it is a lost counter, and
    its numbers are never reused."""
    try:
        data = json.loads(_high_water_file(session).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    counts = data.get("counts") if isinstance(data, dict) else None
    return {k: int(v) for k, v in counts.items() if isinstance(v, int) and v > 0} \
        if isinstance(counts, dict) else {}


def occurrence_state(session: str) -> dict | None:
    """The session's durable occurrence counter, or None when it is gone.
    It never expires (section 13 E): it is kept apart from the 24-hour turn
    records and removed only when the session is forgotten for good."""
    try:
        data = json.loads(occurrence_file(session).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("counts"), dict) else None


def _next_occurrence(session: str, sha: str, now: float) -> int:
    """Number this hash in this session: first typing 1, identical later 2.

    A counter file lost from disk is recreated with a ``lost`` marker (and
    the time), so directions from before the marker can stay ``unjoined``
    rather than be matched on a reused number."""
    state = occurrence_state(session)
    if state is None:
        high = _high_water(session)
        earlier = bool(high) or any(isinstance(t.get("prompt_sha256"), str)
                                     for t in _read_lines(session_file(session)))
        # A lost counter restarts from its high-water counts, so a
        # retyped prompt never reuses an occurrence from before the loss.
        state = {"counts": dict(high), "lost": bool(earlier), "recreated_at": now if earlier else None}
    counts = state["counts"]
    counts[sha] = int(counts.get(sha) or 0) + 1
    d = occurrences_folder()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(folder(), 0o700)
    os.chmod(d, 0o700)
    _write_private(occurrence_file(session), json.dumps(state) + "\n")
    _write_private(_high_water_file(session), json.dumps({"counts": counts}) + "\n")
    return counts[sha]


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


# A brief is known by hashes of its words, never the words: the whole brief,
# and its first ``_HEAD`` characters, which a turn holding the brief after
# other words (a wrapper the cleaning left) still contains at a word start
# within its first ``_HEAD_SCAN`` characters.
_HEAD = 200
_HEAD_SCAN = 4096


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def note_brief(text: str, *, parent: str, harness: str, now: float | None = None) -> None:
    """A delegation's brief (the delegation hook calls this): a turn whose
    text is this brief is a dispatched job's, never the owner's, and the
    brief names the parent session the job was started from. Only hashes
    of the brief are kept."""
    norm = _norm(text or "")
    # Kept while owner direction is off too: a turn that could turn it back
    # on is recorded then, and a brief must never pass for one.
    if len(norm) < 20 or not parent:
        return
    head = norm[:_HEAD]
    line = {"ts": time.time() if now is None else now, "sha": _hash(norm), "head_sha": _hash(head),
            "head_len": len(head), "parent": parent, "harness": harness}
    try:
        path = _ensure_folder() / "briefs.jsonl"
        lines = [b for b in _read_lines(path) if line["ts"] - float(b.get("ts") or 0) < RETENTION_S]
        _write_private(path, "".join(json.dumps(_no_text(b)) + "\n" for b in lines + [line]))
    except OSError:
        pass


def _no_text(brief: dict) -> dict:
    """A brief line as it is kept now: an older line's copied words go."""
    return {k: v for k, v in brief.items() if k != "head"}


def brief_for(text: str, now: float | None = None) -> dict | None:
    """The recorded brief this turn's text is, if any."""
    norm = _norm(text or "")
    if not norm:
        return None
    now = time.time() if now is None else now
    whole = _hash(norm)
    starts = [0] + [i + 1 for i, c in enumerate(norm[:_HEAD_SCAN]) if c == " "]
    heads: dict[int, set[str]] = {}
    for b in reversed(_read_lines(folder() / "briefs.jsonl")):
        if now - float(b.get("ts") or 0) >= RETENTION_S:
            continue
        if b.get("sha") == whole:
            return b
        size = b.get("head_len")
        if isinstance(size, int) and size >= 20 and b.get("head_sha"):
            if size not in heads:
                heads[size] = {_hash(norm[i:i + size]) for i in starts if len(norm) - i >= size}
            if b["head_sha"] in heads[size]:
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


def untrusted_why(key: str) -> str:
    return (f"{key} is not a front end that counts as the owner. To add it, switch it on in the Khipu app "
            "(Settings, Owner actions, Front ends that count as you), or ask for it in a conversation "
            "from a front end already on that list")


def untrusted_front_end(turns_: list[dict], conf: dict | None = None) -> str | None:
    """The front end of the latest turn that would count but for its front
    end not being trusted, as ``harness:entrypoint``; None when there is none."""
    conf = conf or settings()
    for turn in reversed(turns_):
        key = f"{turn.get('harness')}:{turn.get('entrypoint')}"
        if key in conf["trusted"] or turn.get("entrypoint") in MACHINE_ENTRYPOINTS:
            continue
        if eligible(turn, {**conf, "trusted": [*conf["trusted"], key]})[0]:
            return key
    return None


def eligible(turn: dict, conf: dict | None = None) -> tuple[bool, str]:
    """Whether a recorded turn can carry the owner's words now, and why not."""
    conf = conf or settings()
    if turn.get("entrypoint") in MACHINE_ENTRYPOINTS:
        return False, "a machine entrypoint can never authorize owner changes"
    if turn.get("machine"):
        return False, "a message the harness wrote, not a typed turn"
    if turn.get("wrapped"):
        return False, "a wrapper away from its harness's place, so the typed words can't be told apart"
    if turn.get("helper"):
        return False, "a T3 helper session"
    if turn.get("dispatched"):
        return False, turn.get("job") or "a dispatched job's turn"
    if turn.get("delegated"):
        return False, "a dispatched job's brief"
    if not turn.get("typed", bool((turn.get("text") or "").strip())):
        return False, "no typed text"
    if turn.get("entrypoint") == "t3-unverified":
        return False, ("Khipu could not read T3's record to rule out a T3 conversation; "
                       "try again, or confirm owner changes in the Khipu app")
    if turn.get("entrypoint") == "t3":
        # T3 hands the hook only the prompt text, never which delivery fired it,
        # so a scheduled task's or relayed message with the owner's words can't
        # be told apart from the owner typing (three attempts, 2026-11-01). Not
        # a setting: switching it on would reopen that.
        return False, ("a T3 conversation can't be told apart from T3's own scheduled or relayed messages; "
                       "confirm owner changes in the Khipu app")
    key = f"{turn.get('harness')}:{turn.get('entrypoint')}"
    if key not in conf["trusted"]:
        return False, untrusted_why(key)
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
    return low in REFUSED_ISSUERS or low.startswith(REFUSED_ISSUER_PREFIXES) or low in HOOK_SHELLS


def _proc_args(pid: int) -> list[str] | None:
    """A process's argv as the kernel has it (``KERN_PROCARGS2``); None when
    it can't be read."""
    if sys.platform != "darwin" or pid <= 0:
        return None
    try:
        import ctypes

        libc = ctypes.CDLL("/usr/lib/libc.dylib")
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or size.value < 4:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None
        raw = buf.raw[:size.value]
        argc = int.from_bytes(raw[:4], sys.byteorder)
        # The exec path, its NUL padding, then argc NUL-ended strings.
        rest = raw[4:].split(b"\0", 1)[1].lstrip(b"\0") if b"\0" in raw[4:] else b""
        args = rest.split(b"\0")[:argc]
        return [os.fsdecode(a) for a in args] if len(args) == argc else None
    except (OSError, AttributeError, ValueError, IndexError):
        return None


PROMPT_SHIM = "khipu-prompt-recall"
# Antigravity's PostInvocation and Stop hooks (``khipu-stop-hook --event X``)
# record a turn no PreInvocation could read (``antigravity_late_turn``).
STOP_SHIM = "khipu-stop-hook"


def _runs_the_shim(args: list[str] | None, shim: str = PROMPT_SHIM) -> bool:
    """A shell running the shim as a script: ``sh <path>/khipu-prompt-recall``."""
    return bool(args) and len(args) >= 2 and os.path.basename(args[1]) == shim


def _shim_flags(words: list[str], shim: str) -> bool:
    if shim == STOP_SHIM:
        return len(words) == 2 and words[0] == "--event" and words[1].isalpha()
    return all(w.startswith("--") for w in words)


def _only_the_shim(args: list[str] | None, shim: str = PROMPT_SHIM) -> bool:
    """A harness's hook shell, ``sh -c '<path>/khipu-prompt-recall [--flag]'``
    (``<path>/khipu-stop-hook --event <Name>`` for the stop shim) and nothing
    else. An agent's Bash tool shell runs its own command line, so it never
    reads as this."""
    import shlex

    if not args or len(args) != 3 or args[1] != "-c":
        return False
    try:
        words = shlex.split(args[2])
    except ValueError:
        return False
    return bool(words) and os.path.basename(words[0]) == shim and _shim_flags(words[1:], shim)


def _print_mode(args: list[str] | None) -> bool:
    """A Claude Code command line run as ``claude -p`` / ``--print``."""
    if not args:
        return False
    for a in args[1:]:
        if a == "--":
            return False
        if a in ("-p", "--print") or a.startswith("--print="):
            return True
    return False


def hook_issuer(shim: str = PROMPT_SHIM) -> dict | None:
    """The process that ran this hook. The recorder's parent must be the
    prompt shim (a shell running ``khipu-prompt-recall``; ``shim`` names
    ``khipu-stop-hook`` for Antigravity's late record); above it, only a
    harness's own hook shell (``sh -c`` with the shim as its whole command)
    is passed over, and the first process after that is the issuer.
    ``{"pid", "start", "exe"}`` for a harness; ``{"refused": why}`` when the
    recorder was not started by the shim, or the shim by a shell, an
    interpreter or anything that is not a harness's hook (an agent's Bash
    tool running it); None when the tree can't be read. Same-user processes
    can't be told apart for certain (scope 2.6): this closes running the
    recorder or the shim from an agent's shell, not ``exec`` tricks."""
    me = _proc_info(os.getpid())
    if me is None:
        return None
    pid, born = me[0], me[2]
    for step in range(_ISSUER_MOST):
        info = _proc_info(pid)
        if info is None:
            return None
        parent, exe, started = info
        if started > born:
            return None  # the parent died and its number went to a newer process
        born = started
        name = os.path.basename(os.path.realpath(exe))
        if step == 0:
            if name not in HOOK_SHELLS or not _runs_the_shim(_proc_args(pid), shim):
                return {"refused": f"{name} (not the prompt hook)"}
            pid = parent
            continue
        if (name in HOOK_SHELLS or name in REFUSED_ISSUERS) and _only_the_shim(_proc_args(pid), shim):
            pid = parent
            continue
        if _refused_issuer(name):
            return {"refused": name}
        out = {"pid": pid, "start": started, "exe": name}
        if _print_mode(_proc_args(pid)):
            out["print"] = True
        return out
    return None


def record(payload: dict, env: dict | None = None, now: float | None = None,
           issuer: dict | None = None, extra: dict | None = None) -> str | None:
    """Record one prompt-submit turn. Returns its id, or None when nothing
    was recorded. ``issuer`` None means this process's (``hook_issuer``).
    ``extra`` (Antigravity's recorder only): ``fallback`` marks a turn read
    from the payload's ``lastUserInput`` (``ag_step``: that turn's
    ``initialNumSteps``); ``duplicate_of`` + ``occurrence`` make this row a
    late second copy of that turn, sharing its occurrence.
    Never raises."""
    try:
        return _record(payload, os.environ if env is None else env, time.time() if now is None else now, issuer,
                       extra or {})
    except Exception:  # noqa: BLE001 - the recorder must never fail a prompt
        return None


def _record(payload: dict, env, now: float, issuer: dict | None, extra: dict) -> str | None:
    if not isinstance(payload, dict):
        return None
    conf = settings()
    session = hook_shape.session_id(payload).strip()
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
        "entrypoint": entrypoint_of(harness, env, payload, session, issuer), "cwd": cwd,
        "turn": secrets.token_hex(4),
        "typed": bool(text.strip()) and not machine,
        "machine": machine, "wrapped": wrapped, "helper": t3.is_helper_session(cwd), "issuer": issuer,
    }
    job = dispatched_job(env, session, t3_thread=turn["entrypoint"] == "t3")
    if job:
        turn["dispatched"] = True
        turn["job"] = job
    brief = None if machine else brief_for(text, now)
    turn["delegated"] = brief is not None
    if brief is not None:
        turn["parent"] = brief.get("parent")
        turn["brief"] = brief.get("sha")
    if text.strip() and not machine and brief is None:
        # Section 13 E: the hash and its durable occurrence, never the text.
        sha = prompt_sha256(text)
        turn["prompt_sha256"] = sha
        shared = extra.get("occurrence")
        # A late duplicate of a recorded turn is the same typing: it takes
        # that turn's occurrence rather than numbering a second one.
        turn["occurrence"] = shared if type(shared) is int and shared >= 1 \
            else _next_occurrence(session, sha, now)
    for flag in ("fallback", "duplicate_of"):
        if extra.get(flag):
            turn[flag] = extra[flag]
    if extra.get("fallback") and type(extra.get("ag_step")) is int:
        turn["ag_step"] = extra["ag_step"]
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
    return turn["turn"]


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
    file when none remain. Words an older line still holds (a brief's
    ``head``, a turn's ``text``) go too. Returns how many lines went."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    rows = _read_lines(path) if raw else []
    kept = [r for r in rows if now - _ts(r) < RETENTION_S]
    gone = len(raw.splitlines()) - len(kept)
    if any("head" in r or "text" in r for r in kept):
        kept = [{k: v for k, v in r.items() if k not in ("head", "text")} for r in kept]
    elif not gone:
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
    capture hook's storage sweep), so a turn's record goes a day after it
    was typed even when no later prompt or read comes: every turn file and
    briefs.jsonl line by line (a line a crash cut short goes too), the
    rewrite temps a crash left, and the old widenings file. Local
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
        from khipu import routing_mismatches

        out["mismatches"] = routing_mismatches.sweep(datetime.fromtimestamp(now, timezone.utc))
    except Exception as err:  # noqa: BLE001
        out["ok"] = False
        out["errors"].append(f"old model mismatches: {type(err).__name__}: {err}")
    try:
        from khipu import owner_direction

        out["pending"] = owner_direction.remove_old_widenings()
    except Exception as err:  # noqa: BLE001
        out["ok"] = False
        out["errors"].append(f"old widenings: {type(err).__name__}: {err}")
    return out


def _brief_keys(rows: list[dict]) -> set[str]:
    """The brief hashes these turns name: the brief a dispatched job's turn
    was (``brief``), and the hash of a turn's words on a line written under
    the old design."""
    keys = {r["brief"] for r in rows if isinstance(r.get("brief"), str)}
    keys |= {_hash(_norm(str(r["text"]))) for r in rows if r.get("text")}
    return keys


class ForgetCleanupError(OSError):
    """Completed independent scrubs, with safe step labels and partial counts."""

    def __init__(self, errors: list[str], removed: dict):
        super().__init__("; ".join(errors))
        self.errors = errors
        self.removed = removed


def forget_sessions(sessions, *, protected_sessions=()) -> dict:
    """Remove session turns, counters, directions and briefs independently.

    A failure in one file must not block another scrub. Report all failures
    after attempting every step so forget stays incomplete. Words belonging
    to a separate live episode are retained, including shared brief text.
    """
    names = {n for n in sessions if n}
    protected = set(protected_sessions)
    names -= protected
    out = {"files": 0, "lines": 0, "briefs": 0}
    if not names:
        return out
    errors = []

    def attempt(label, action):
        try:
            action()
        except Exception as err:  # noqa: BLE001 - finish the independent scrubs before raising
            errors.append(f"{label}: {type(err).__name__}")

    d = folder()
    files = {session_file(n).name for n in names}
    protected_files = {session_file(n).name for n in protected}
    texts: set[str] = set()
    listed = []
    try:
        # scandir must report an unreadable folder, rather than look empty.
        with os.scandir(d) as entries:
            listed = sorted(e.name for e in entries)
    except FileNotFoundError:
        pass
    except Exception as err:  # noqa: BLE001
        errors.append(f"turn directory: {type(err).__name__}")

    def scrub_turn(name):
        path = d / name
        path.read_text(encoding="utf-8")  # _read_lines alone hides read errors
        rows = _read_lines(path)
        if name in files:
            texts.update(_brief_keys(rows))
            path.unlink()
            out["files"] += 1
            return
        mine = [r for r in rows if r.get("session") in names]
        if not mine:
            return
        texts.update(_brief_keys(mine))
        kept = [r for r in rows if r not in mine]
        if kept:
            _write_private(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept))
            out["lines"] += len(mine)
        else:
            path.unlink()
            out["files"] += 1

    for name in listed:
        if name != "briefs.jsonl" and name.endswith(".jsonl") and name not in protected_files:
            attempt("turn file", lambda: scrub_turn(name))
    for name in sorted(names):
        for counter in (occurrence_file(name), _high_water_file(name)):
            attempt("session counter", lambda: counter.unlink(missing_ok=True))

    def scrub_directions():
        from khipu import routing_directions

        try:
            routing_directions.forget_sessions(names)
        except routing_directions.MalformedRows as err:
            # Row keys only (ids), never a row's words.
            errors.append(f"routing directions: malformed row(s) {', '.join(err.keys)} left unchecked")
        except routing_directions.StoreUnreadable:
            errors.append("routing directions: store unreadable, left as it is")

    attempt("routing directions", scrub_directions)

    def scrub_briefs():
        briefs = d / "briefs.jsonl"
        try:
            briefs.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        rows = _read_lines(briefs)
        kept = [b for b in rows if b.get("parent") in protected
                or (b.get("parent") not in names and b.get("sha") not in texts)]
        if len(kept) != len(rows) or any("head" in b and b.get("parent") not in protected for b in kept):
            _write_private(briefs, "".join(json.dumps(b if b.get("parent") in protected else _no_text(b))
                                          + "\n" for b in kept))
            out["briefs"] = len(rows) - len(kept)

    attempt("briefs", scrub_briefs)
    if errors:
        raise ForgetCleanupError(errors, out)
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
            if now - float(t.get("ts") or 0) >= RETENTION_S or t.get("duplicate_of"):
                continue
            row = out.setdefault(str(t.get("harness") or "unknown"), {"turns": 0, "eligible": 0, "entrypoints": {}})
            row["turns"] += 1
            row["eligible"] += 1 if t.get("eligible") else 0
            entry = str(t.get("entrypoint") or "unknown")
            row["entrypoints"][entry] = row["entrypoints"].get(entry, 0) + 1
    return out


_HARNESS_WORDS = {"claude_code": "Claude Code", "codex": "Codex", "aegis": "Aegis", "cursor": "Cursor",
                  "antigravity": "Antigravity"}


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
    # A row the routing-directions store cannot use is never repaired or
    # moved automatically: it stays as written, is skipped by readers, and
    # can keep a forget incomplete. Doctor names it and stays red.
    try:
        from khipu import routing_directions

        bad = routing_directions.malformed_rows()
    except Exception as err:  # noqa: BLE001 - a failed look is reported, never read as clean
        bad = [{"key": "(store)", "reason": f"could not be checked: {type(err).__name__}"}]
    damaged = [(f"Routing directions: the store file {routing_directions.STORE_NAME} cannot be read ({row['reason']}); "
                f"it is left as it is, nothing is shown as owed from it, and forgets stay incomplete until it is "
                f"fixed or removed by hand.") if row["key"] == "(store)" else
               (f"Routing directions: row {row['key']} is malformed ({row['reason']}); it is kept as written and "
                f"not shown as owed, and a forget stays incomplete while its session cannot be read. "
                f"Fix or remove it by hand.") for row in bad]
    if not settings()["enabled"]:
        return {"ok": not damaged, "enabled": False, "harnesses": per, "missing": [], "warnings": damaged,
                "summary": "Owner direction is off."}
    installed = _prompt_hook_installed() if installed is None else installed
    missing = [h for h in installed if not per.get(h, {}).get("turns")]
    warnings = [f"Owner direction is on, but no turn from {_HARNESS_WORDS.get(h, h)} was recorded in the last day; "
                f"its prompt hook may not be running, and owner directions from it are refused." for h in missing]
    warnings += damaged
    parts = [f"{_HARNESS_WORDS.get(h, h)} {row['turns']}" for h, row in sorted(per.items())]
    return {"ok": not missing and not damaged, "enabled": True, "harnesses": per, "missing": missing,
            "warnings": warnings,
            "summary": "Turns recorded in the last day: " + (", ".join(parts) if parts else "none") + "."}


# ---- Antigravity: the prompt comes from the transcript ------------------------
#
# Antigravity's PreInvocation payload carries no prompt text (scope doc section
# 3.2). The docs fire it before every model call; T3's runtime 1.3.0 fires it
# once per user turn (proof run 5), and both are handled. The turn
# is the newest USER_INPUT step in the conversation's transcript, and a turn is
# handled once: a marker per (conversation, step) is created exclusively, so the
# first model call after a new USER_INPUT wins and every later call, or a second
# copy of the hook (the global file and a workspace file both loaded), is told
# to do nothing. It never depends on ``invocationNum``, which the docs count per
# turn and the ACP runtime counts per session.

_AG_CHUNK = 4 << 20
# Longest stretch of transcript searched for the newest USER_INPUT.
_AG_MAX_SCAN = 512 << 20
_AG_WRAP = re.compile(r"^\s*<USER_REQUEST>(.*)</USER_REQUEST>\s*$", re.S)
_AG_MARKER_KEEP_S = 3 * 24 * 3600
# PreInvocation can fire before the USER_INPUT step reaches the transcript;
# the gate re-reads briefly (well inside the hook's 3 s timeout).
_AG_FLUSH_WAIT_S = 0.4
_AG_FLUSH_POLL_S = 0.05


def _ag_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for key in ("text", "content", "message"):
            if isinstance(content.get(key), str):
                return content[key]
        return ""
    if isinstance(content, list):
        return "\n".join(t for t in (_ag_text(c) for c in content) if t)
    return ""


def _ag_last_user_input(path: str) -> tuple[str, str] | None:
    """(step key, text) of the newest USER_INPUT step in a transcript, read
    from the end. None when there is none or the file cannot be read. A
    ``.db`` path is T3's SQLite trajectory (``antigravity_db``)."""
    if antigravity_db.is_db(path):
        return antigravity_db.last_user_input(path)
    for line in _ag_lines_newest_first(path):
        try:
            step = json.loads(line)
        except (ValueError, RecursionError):  # any line that will not parse is skipped
            continue
        if not isinstance(step, dict) or step.get("type") != "USER_INPUT":
            continue
        text = _ag_text(step.get("content"))
        wrapped = _AG_WRAP.match(text)
        text = (wrapped.group(1) if wrapped else text).strip()
        if not text:
            continue
        index = step.get("step_index")
        if isinstance(index, int) and not isinstance(index, bool):
            return f"step-{index}", text
        # No step index: the text and the step's time tell two identical prompts apart.
        stamp = f"{text}\0{step.get('created_at')}"
        return "h-" + hashlib.sha256(stamp.encode("utf-8", "replace")).hexdigest()[:16], text
    return None


def _ag_lines_newest_first(path: str):
    """Lines of a JSONL transcript, newest first, read backwards in chunks so
    a long run of steps after the USER_INPUT does not hide it. Stops after
    ``_AG_MAX_SCAN`` bytes; an unreadable file yields nothing."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            floor = max(0, end - _AG_MAX_SCAN)
            # Pieces of the line that started in a chunk read earlier (newer
            # bytes first), joined once when its start is found.
            carry: list[bytes] = []
            while end > floor:
                start = max(floor, end - _AG_CHUNK)
                fh.seek(start)
                parts = fh.read(end - start).split(b"\n")
                end = start
                carry.append(parts[-1])
                if len(parts) > 1:
                    line = b"".join(reversed(carry))
                    if line.strip():
                        yield line.decode("utf-8", errors="replace")
                    for raw in reversed(parts[1:-1]):
                        if raw.strip():
                            yield raw.decode("utf-8", errors="replace")
                    carry = [parts[0]]
                else:
                    carry[-1:] = [parts[0]]
            # At the cap the first piece is a whole line only when the byte
            # before it ends a line; at the file's start it always is.
            if carry and (floor == 0 or _byte_before(fh, floor) == b"\n"):
                line = b"".join(reversed(carry))
                if line.strip():
                    yield line.decode("utf-8", errors="replace")
    except OSError:
        return


def _byte_before(fh, offset: int) -> bytes:
    fh.seek(offset - 1)
    return fh.read(1)


def _ag_flush_wait() -> float:
    try:
        return max(0.0, min(2.0, float(os.environ.get("KHIPU_AG_FLUSH_WAIT_S", _AG_FLUSH_WAIT_S))))
    except ValueError:
        return _AG_FLUSH_WAIT_S


# A PreInvocation that cannot record leaves a note of its time under the
# payload's ``initialNumSteps``: the step count when its turn began, which is
# the index of that turn's USER_INPUT step (T3 2026-10-29: 0 for the step-0
# turn). Only the turn whose step index equals it takes that time, so a note
# left by a later model call of one turn never dates the next turn; a turn
# with no matching note is dated when it is recorded. Stop clears the notes.
_AG_PENDING_KEEP_S = 3600
_AG_PENDING_MAX = 32
# A turn recorded from the payload's ``lastUserInput`` (no USER_INPUT step in
# the transcript yet) is keyed by its ``initialNumSteps`` and text, so the same
# words typed in a later turn are a new turn. Its own step, once flushed, is
# recorded again under its step key as a duplicate row. The copy shares the
# first row's occurrence only with proof it is that turn: the step index is
# the first row's ``initialNumSteps`` and the words match. Without that proof
# it is numbered as a turn of its own: a double count is acceptable, a lost
# turn is not.
_AG_FALLBACK = "last-"
# Only a PreInvocation names its turn: T3 1.3.0 fires one per user turn, and
# PostInvocation's ``initialNumSteps`` is the step count at the END of the
# invocation (4 after three tool calls, proof run 5), while Stop has none.
# Every PreInvocation writes its own value as the session's current turn; a
# late ``lastUserInput`` row is keyed by that, never by its own payload, and
# with no PreInvocation seen yet (a Stop can fire before the first) there is
# no turn to bind it to, so it records nothing.


def _ag_dir() -> Path:
    directory = folder() / "antigravity"
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def _ag_file(directory: Path, kind: str, session: str) -> Path:
    return directory / (kind + hashlib.sha256(session.encode("utf-8", "replace")).hexdigest()[:40])


def _ag_step_number(key: str) -> int | None:
    if key.startswith("step-") and key[5:].isdigit():
        return int(key[5:])
    return None


def _ag_initial_steps(payload: dict) -> int | None:
    n = payload.get("initialNumSteps")
    return n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else None


def _ag_notes(path: Path) -> dict[str, float]:
    try:
        notes = json.loads(path.read_text(encoding="utf-8")).get("steps")
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(notes, dict):
        return {}
    return {k: float(v) for k, v in notes.items()
            if isinstance(k, str) and k.isdigit() and isinstance(v, (int, float)) and not isinstance(v, bool)}


def _ag_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _ag_save_notes(path: Path, notes: dict[str, float]) -> None:
    try:
        if not notes:
            path.unlink(missing_ok=True)
            return
        keep = sorted(notes, key=int)[-_AG_PENDING_MAX:]
        _ag_write(path, json.dumps({"steps": {k: notes[k] for k in keep}}))
    except OSError:
        pass


def _ag_note_unseen(session: str, steps: int | None, now: float) -> None:
    """Keep the first unrecorded PreInvocation's time for the turn at step
    ``steps`` (never overwritten). No note without a step count to bind it."""
    if steps is None:
        return
    path = _ag_file(_ag_dir(), "pending-", session)
    notes = _ag_notes(path)
    notes.setdefault(str(steps), now)
    _ag_save_notes(path, notes)


def _ag_note_turn(session: str, steps: int | None) -> None:
    path = _ag_file(_ag_dir(), "turn-", session)
    try:
        if steps is None:
            path.unlink(missing_ok=True)
        else:
            _ag_write(path, json.dumps({"steps": steps}))
    except OSError:
        pass


def _ag_turn_steps(session: str) -> int | None:
    """The newest PreInvocation's ``initialNumSteps``; None when none was seen."""
    try:
        n = json.loads(_ag_file(_ag_dir(), "turn-", session).read_text(encoding="utf-8")).get("steps")
    except (OSError, ValueError, AttributeError):
        return None
    return n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else None


def _ag_clear_unseen(session: str) -> None:
    try:
        _ag_file(_ag_dir(), "pending-", session).unlink()
    except OSError:
        pass


def _ag_claim(session: str, key: str, now: float, steps: int | None = None,
              text: str = "") -> tuple[float, dict] | None:
    """Create the turn's once-per-turn marker. None when the turn was already
    handled; else the turn's time (the note's when a PreInvocation of this
    same turn could not read it) and what ``record`` needs to number it:
    a ``lastUserInput`` turn is marked a fallback bound to ``steps`` (the
    payload's ``initialNumSteps``), and the flushed step of that same turn
    is marked its duplicate. Every Antigravity record goes through here."""
    directory = _ag_dir()
    marker = directory / hashlib.sha256(f"{session}\0{key}".encode("utf-8", "replace")).hexdigest()[:40]
    try:
        os.close(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        return None
    for old in directory.iterdir():
        try:
            if now - old.stat().st_mtime > _AG_MARKER_KEEP_S:
                old.unlink()
        except OSError:
            continue
    at = now
    index = _ag_step_number(key)
    if index is not None:
        path = _ag_file(directory, "pending-", session)
        notes = _ag_notes(path)
        ts = notes.get(str(index))
        if ts is not None and now - _AG_PENDING_KEEP_S <= ts <= now:
            at = ts
        _ag_save_notes(path, {k: v for k, v in notes.items() if int(k) > index})
    elif steps is not None and key.startswith(_AG_FALLBACK):
        # A late row of a turn whose PreInvocation could not read it keeps
        # that call's time; the note stays to date the flushed copy.
        ts = _ag_notes(_ag_file(directory, "pending-", session)).get(str(steps))
        if ts is not None and now - _AG_PENDING_KEEP_S <= ts <= now:
            at = ts
    if key.startswith(_AG_FALLBACK):
        return at, ({"fallback": True} if steps is None else {"fallback": True, "ag_step": steps})
    return at, (_ag_original(session, index, text, now) if index is not None else {})


def _ag_original(session: str, index: int, text: str, now: float) -> dict:
    """The not yet paired ``lastUserInput`` row of the turn whose step is
    ``index`` with the same words: its id and occurrence, so this flushed
    copy shares its numbering. Empty when no row is that turn."""
    typed = owner_text(text)
    if typed is None or not typed.strip():
        return {}
    sha = prompt_sha256(typed)
    rows = turns(session, now)
    paired = {t.get("duplicate_of") for t in rows}
    for t in rows:
        if t.get("fallback") and type(t.get("ag_step")) is int and t["ag_step"] == index \
                and t.get("prompt_sha256") == sha and t.get("turn") not in paired \
                and type(t.get("occurrence")) is int:
            return {"duplicate_of": t["turn"], "occurrence": t["occurrence"]}
    return {}


def _ag_read(payload, wait: float | None, late: bool = False) -> tuple[str, tuple[str, str] | None, int | None] | None:
    """(session, newest user turn or None, the turn's step count) for an
    Antigravity payload with no prompt of its own; None for any other
    payload. ``late`` (PostInvocation, Stop): the step count is the newest
    PreInvocation's, and ``lastUserInput`` is used only when one was seen."""
    if not isinstance(payload, dict) or hook_shape.detect(payload, os.environ)[0] != "antigravity":
        return None
    if isinstance(payload.get("prompt"), str) and payload["prompt"].strip():
        return None
    session = hook_shape.session_id(payload).strip()
    if not session:
        return None
    found = None
    steps = _ag_turn_steps(session) if late else _ag_initial_steps(payload)
    tp = hook_shape.transcript_path(payload)
    last = payload.get("lastUserInput") or payload.get("last_user_input")
    has_last = isinstance(last, str) and bool(last.strip())
    if tp:
        found = _ag_last_user_input(tp)
        if found is None and not has_last and wait:
            give_up = time.monotonic() + wait
            while found is None and time.monotonic() < give_up:
                time.sleep(_AG_FLUSH_POLL_S)
                found = _ag_last_user_input(tp)
    if found is None and has_last and not (late and steps is None):
        wrapped = _AG_WRAP.match(last)
        text = (wrapped.group(1) if wrapped else last).strip()
        if text:
            found = (_AG_FALLBACK + ("" if steps is None else f"{steps}-")
                     + hashlib.sha256(last.encode("utf-8", "replace")).hexdigest()[:16], text)
    return session, found, steps


def _ag_filled(payload: dict, text: str) -> dict:
    out = dict(payload)
    out["prompt"] = text
    if not isinstance(out.get("cwd"), str) or not out["cwd"]:
        workspace = hook_shape.workspace(payload)
        if workspace:
            out["cwd"] = workspace
    return out


def _antigravity_turn(payload, now: float | None, wait: float | None):
    """(gate answer, the new turn's time, (session, the payload's step count)
    when this call recorded nothing and its own turn may not be written yet,
    ``record``'s extra for the new turn)."""
    read = _ag_read(payload, _ag_flush_wait() if wait is None else max(0.0, wait))
    if read is None:
        return None, None, None, {}
    session, found, steps = read
    if found is None:
        return None, None, (session, steps), {}
    key, text = found
    claimed = _ag_claim(session, key, time.time() if now is None else now, steps, text)
    if claimed is None:
        # Already handled, or the newest step is the last turn's and this
        # call's own turn is not written yet.
        return {}, None, (session, steps), {}
    return _ag_filled(payload, text), claimed[0], None, claimed[1]


def antigravity_prompt(payload: dict, now: float | None = None, wait: float | None = None,
                       issuer: dict | None = None) -> dict | None:
    """The PreInvocation gate (``antigravity_turn``) with the new turn recorded
    in the same process that claimed it, so no exit between the claim and the
    record can lose the turn. A call that records nothing leaves the pending
    stamp, so the turn's PostInvocation or Stop records it with this time.
    Never raises."""
    try:
        stamp = time.time() if now is None else now
        if isinstance(payload, dict) and hook_shape.detect(payload, os.environ)[0] == "antigravity" \
                and hook_shape.session_id(payload).strip():
            _ag_note_turn(hook_shape.session_id(payload).strip(), _ag_initial_steps(payload))
        out, at, unseen, extra = _antigravity_turn(payload, stamp, wait)
        if unseen is not None:
            _ag_note_unseen(unseen[0], unseen[1], stamp)
        if out:
            record(out, now=at, issuer=issuer, extra=extra)
        return out
    except Exception:  # noqa: BLE001 - the gate must never fail a prompt
        return None


def antigravity_late_turn(payload: dict, event: str, now: float | None = None,
                          issuer: dict | None = None) -> str | None:
    """Record at PostInvocation or Stop an Antigravity turn no PreInvocation
    could read (a one-call turn has no later PreInvocation), under the same
    once-per-turn marker, with the time of the first PreInvocation of that
    same turn that could not read it (else this call's time).
    Returns the recorded turn's id, None when nothing was recorded. Prints
    nothing; never raises."""
    try:
        read = _ag_read(payload, 0, late=True)
        if read is None:
            return None
        session, found, steps = read
        stamp = time.time() if now is None else now
        recorded = None
        if found is not None:
            claimed = _ag_claim(session, found[0], stamp, steps, found[1])
            if claimed is not None:
                if issuer is None:
                    issuer = hook_issuer(STOP_SHIM) or {"refused": "unknown"}
                recorded = record(_ag_filled(payload, found[1]), now=claimed[0], issuer=issuer, extra=claimed[1])
        if str(event).lower() == "stop":
            _ag_clear_unseen(session)
        return recorded
    except Exception:  # noqa: BLE001 - a turn's end is never failed by the recorder
        return None


def antigravity_turn(payload: dict, now: float | None = None, wait: float | None = None) -> dict | None:
    """What the prompt hook does with one Antigravity PreInvocation payload.

    None: not for this function (another harness, or it already names a
    prompt), or no user turn could be read yet, so the payload goes on
    unchanged and no marker is written: a PreInvocation that fires before
    Antigravity flushes the USER_INPUT step never ends the hook, and a later
    call of the same turn can still record it. ``{}``: this user turn was
    already handled (its once-per-turn marker exists), so the hook prints
    ``{}``. Otherwise the payload with ``prompt`` (and ``cwd``) filled in
    from the transcript, for the first model call of a new turn. Before
    giving up on an unflushed transcript it re-reads for up to ``wait``
    seconds (``KHIPU_AG_FLUSH_WAIT_S``, default 0.4). It records nothing
    (``antigravity_prompt`` does). Never raises."""
    try:
        return _antigravity_turn(payload, now, wait)[0]
    except Exception:  # noqa: BLE001 - the gate must never fail a prompt
        return None


def main(argv: list[str]) -> int:
    """The prompt hook's recorder: the payload on stdin, prints nothing.
    Always exits 0.

    ``--antigravity-turn`` is the gate in front of it (see ``antigravity_turn``),
    and records a new turn itself (``antigravity_prompt``): it prints nothing
    for a payload to pass on as it is (no turn readable yet included), ``{}``
    only for a turn already handled, and the payload with its prompt filled
    in for a new one, already recorded."""
    import sys

    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    except ValueError:
        return 0
    if "--antigravity-turn" in argv:
        turn = antigravity_prompt(payload)
        if turn is not None:
            sys.stdout.write(json.dumps(turn, ensure_ascii=True))
        return 0
    record(payload)
    return 0
