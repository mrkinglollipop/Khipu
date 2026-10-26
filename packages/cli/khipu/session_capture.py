# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""Harness-native session capture — enqueue inside the session, drain outside it.

This is Khipu's OWN capture step, and since 2026-08-17 (evening) it runs in
every local harness: Claude Code, Cursor, Codex and Aegis. It replaced a design
that turned out to be a promise rather than a mechanism:

    The legacy Claude Code / Cursor / Codex capture was MODEL-DRIVEN. A
    PostToolUse hook nudged the model "memory capture is overdue" and the model
    was supposed to run capture_v2. PreCompact was the safety net. Measured on
    2026-08-17: the nudge fired 377 times in one session and the model ran
    capture_v2 zero times, so the only episode that session ever produced was
    the compaction safety net. maintainer: "capturing only at compaction is absolutely
    terrible. That was a safety net."

So capture is now HOOK-DRIVEN and deterministic: the harness's Stop hook reads
the new part of the transcript, applies a fixed cadence (every MIN_TURNS user
turns or MIN_MINUTES, whichever first; PreCompact and SessionEnd always), and
queues an extraction job. No model in the decision. Nothing for the model to
forget.

The shape — enqueue in-session, drain out-of-session — is dictated by Aegis,
the first harness this ran in. **Aegis runs its hooks inside a macOS sandbox**
(measured from a real session): writable `/tmp`, `~/.grok`, the project dir;
DENIED `~/Library`, `~/.config/khipu`, the legacy Memory tree, the Keychain;
Postgres refused because libpq cannot read its TLS root cert. The first version
ignored that, logged to `~/Library/Logs`, and the failed shell redirect aborted
the hook before any work ran — exit 0, healthy-looking, captured nothing. So:

  in-session (must stay cheap and dependency-free; sandbox-safe)
      read the new transcript window, apply the cadence, write a JOB FILE under
      ``~/.grok/khipu/queue/``. No model call, no database, no Keychain. Every
      harness uses this same home because it is the one place all of them,
      sandboxed Aegis included, can write — one queue, one drainer.

  out-of-session (unsandboxed)
      ``drain()`` turns each job into an episode: khipu.extract (the model) then
      khipu.capture (PG first, file mirror, embed) — the same path every other
      episode takes. The Claude Code / Cursor / Codex Stop hooks are not
      sandboxed, so they drain right after they enqueue: capture lands in the
      same Stop that decided it was due. The nightly, the desktop app and
      `khipu sessions drain` are the other drainers.

Silent failure is the enemy this module was rebuilt against, so it keeps its
own evidence instead of assuming: every hook run writes a per-harness heartbeat
(``dispatch/<harness>.json``: last run, last due, turns seen since the last
successful capture, last error), and the drain records the last capture and
last failure per harness. ``liveness()`` turns that into a red/green answer to
"is this harness actually being recorded?" — surfaced by `khipu doctor`,
`khipu integrations verify`, and the desktop Integrations pane.
"""
from __future__ import annotations

import glob
import base64
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

from khipu import t3 as _t3
from khipu import wire
if not hasattr(_t3, 'cache_thread'):
    # A long-lived MCP server can still hold the t3 from before the
    # deploy that added this name; t3 is stdlib-only, so reloading is safe.
    import importlib
    importlib.reload(_t3)
from khipu.lines import split_lines
from khipu.t3 import cache_thread, is_helper_session
from khipu.turn_text import INJECTED_CONTEXT_TAGS, MACHINE_USER_TAGS, clean_user_text, is_machine  # noqa: F401

HARNESSES = ("claude_code", "cursor", "codex", "aegis", "grok_build")


def _env_int(new: str, old: str, default: str) -> str:
    return os.environ.get(new) or os.environ.get(old) or default


MIN_TURNS = int(_env_int("KHIPU_CAPTURE_MIN_TURNS", "KHIPU_AEGIS_MIN_TURNS", "5"))
MIN_MINUTES = float(_env_int("KHIPU_CAPTURE_MIN_MINUTES", "KHIPU_AEGIS_MIN_MINUTES", "20"))
MIN_CHARS = 200          # a window smaller than this is never worth a model call
MAX_TRANSCRIPT = 14_000  # chars carried in a job (tail of the window)
MAX_CAPTURE_NOTE = 14_000  # newest characters retained across capture-now requests
# Parts (= summariser calls = episodes) one capture may queue. 0 (default) =
# every part; a positive value is the owner's opt-in to keep only the newest.
# See the window-split note above _split_messages.
MAX_PARTS_PER_CAPTURE = max(0, int(os.environ.get("KHIPU_CAPTURE_MAX_PARTS", "0")))
FIRST_SIGHT_TAIL = 1_000_000  # bytes: first sight of a long transcript starts this far from its end

# Liveness thresholds — "the hook is running but nothing is landing".
STUCK_TURNS = MIN_TURNS * 3          # turns seen with no capture decided due
STUCK_MINUTES = MIN_MINUTES * 3      # minutes with pending turns and nothing due
QUEUE_STALE_S = 30 * 60
# A hook's drain is killed whenever its harness ends the hook (timeout, session
# end), leaving a claim nobody owns until the next drain reclaims it. That is
# the normal hand-over, not a fault; only a claim still abandoned after two
# runs of the 5-minute queue-drain job means nothing is reclaiming.
DEAD_CLAIM_GRACE_S = 10 * 60
# A hook that stops firing is invisible to every other check here: pending_turns
# resets to 0 on a successful capture, so a hook that dies right after one leaves
# a heartbeat with no error, no queue and nothing pending — green forever (audit
# 2026-08-17, the same shape as B15 one level up). The only honest evidence is
# the harness's own transcript: if it grew well after the hook last ran, the
# harness was in use and the hook did not fire. The margin must clear the longest
# plausible single turn (an agentic turn can run over an hour) — this is a
# stopped-hook detector, not a latency alarm.
HOOK_SILENT_S = int(_env_int("KHIPU_CAPTURE_HOOK_SILENT_MIN", "KHIPU_AEGIS_HOOK_SILENT_MIN", "120")) * 60
# Per-session state files are never pruned, so cap transcript reads on each
# `khipu doctor` by the hook's own recency stamp.
ACTIVITY_SCAN_LIMIT = 20              # a job this old means the drain is not landing


def khipu_home() -> Path:
    """Khipu's working area for native capture. `~/.grok/khipu` because Aegis's
    sandbox can write there and nowhere else useful; the other harnesses share
    it so there is exactly one queue and one drainer."""
    from khipu.paths import isolated_dir

    isolated = isolated_dir()
    if isolated is not None:
        return isolated / "capture"
    return Path(os.environ.get("KHIPU_CAPTURE_HOME") or os.environ.get("KHIPU_AEGIS_HOME")
                or str(Path.home() / ".grok" / "khipu"))


def state_dir() -> Path:
    return khipu_home() / "state"


def queue_dir() -> Path:
    return khipu_home() / "queue"


def empty_dir() -> Path:
    """Where a claimed job whose window yielded no episode is kept."""
    return khipu_home() / "empty"


def log_path() -> Path:
    return khipu_home() / "logs" / "session-capture.log"


def dispatch_dir() -> Path:
    return khipu_home() / "dispatch"


def dispatch_path(harness: str) -> Path:
    return dispatch_dir() / f"{_safe(harness)}.json"


def _log(msg: str) -> None:
    try:
        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            # ISO-8601 UTC (audit 2026-09-04): the old stamp was LOCAL time
            # with no offset, so a line could not be lined up with the hub's
            # timestamps or with the harness's own logs.
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            f.write(f"{stamp} [khipu-capture] {msg}\n")
    except OSError:
        pass  # never let logging fail a session


def _mint_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_ts(s: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


# ---- envelope ---------------------------------------------------------------

def _get(env: dict, *names: str, default: Any = None) -> Any:
    for n in names:
        if n in env and env[n] is not None:
            return env[n]
    return default


def norm_event(name: Any) -> str:
    return str(name or "").lower().replace("_", "").replace("-", "")


def session_id(env: dict) -> str:
    return str(_get(env, "sessionId", "session_id", "conversation_id", default="") or "")


def session_cwd(env: dict) -> str:
    cwd = _get(env, "cwd", default="")
    if not cwd:
        roots = _get(env, "workspace_roots", default=None)
        if isinstance(roots, list) and roots:
            cwd = roots[0]
    return str(cwd or "")


def parent_session_id(env: dict, osenv: dict | None = None, *, harness: str = "", sid: str = "") -> str:
    """Lineage for a dispatched child session (W1.3). ``KHIPU_PARENT_SESSION``
    is the primary channel and wins over everything else when set.

    Verified empirically 2026-09-03: a dispatched ``claude -p`` child gets its
    own ``CLAUDE_CODE_SESSION_ID`` but INHERITS the parent's unchanged
    ``CLAUDE_CODE_HOST_SESSION_ID`` (plus ``CLAUDE_CODE_CHILD_SESSION=1``). So
    for Claude Code, a present host id that differs from this session's own id
    IS the parent lineage — no export needed. Format matches how
    ``session_id`` is stored elsewhere (``f"{harness}:{sid}"`` in ``drain()``).

    Audited the same day for Cursor/Codex/Aegis: none carries an equivalent
    field today (Cursor: ``conversation_id`` + ``workspace_roots``, no lineage
    id; Codex: only ``CODEX_HOME``/``CODEX_SANDBOX``, a sandbox marker not
    lineage; Aegis: ``GROK_HOOK_NAME``/``GROK_HOOK_EVENT`` name the hook, not a
    parent). ``KHIPU_PARENT_SESSION`` stays their only channel; a short list of
    plausible payload field names is still checked for forward compat."""
    osenv = os.environ if osenv is None else osenv
    explicit = (osenv.get("KHIPU_PARENT_SESSION") or "").strip()
    if explicit:
        return explicit
    host_id = (osenv.get("CLAUDE_CODE_HOST_SESSION_ID") or "").strip()
    if host_id and host_id != sid:
        return f"claude_code:{host_id}"
    return str(_get(
        env, "parentSessionId", "parent_session_id",
        "agentSessionId", "agent_session_id", "parentId", "parent_id",
        default="",
    ) or "")


def capture_origin(env: dict, osenv: dict | None = None, *, harness: str = "", sid: str = "") -> str:
    """``job`` when this session is a dispatched job's, else ``conversation``
    (owed scope 2026-10-16, design (a)). A job has lineage
    (:func:`parent_session_id`: ``KHIPU_PARENT_SESSION``, a Claude Code child
    whose host id differs from its own, a payload parent field) or a runner
    that set ``KHIPU_JOB=1``. The host-id test needs this session's own id,
    so without one only the explicit markers count.

    No harness's own non-interactive marker is used: nothing in this repo
    confirms what Claude Code print mode or ``codex exec`` hand the hook
    (``CLAUDE_CODE_ENTRYPOINT`` is never interpreted here, and a Codex hook
    payload carries no exec marker), so a run started by hand with ``-p`` and
    no lineage stays a conversation."""
    osenv = os.environ if osenv is None else osenv
    if (osenv.get("KHIPU_JOB") or "").strip() == "1":
        return "job"
    if not sid:
        osenv = {k: v for k, v in osenv.items() if k != "CLAUDE_CODE_HOST_SESSION_ID"}
    return "job" if parent_session_id(env, osenv, harness=harness, sid=sid) else "conversation"


def infer_harness(env: dict, osenv: dict | None = None) -> str:
    """Which harness invoked us. The hook command is identical in every harness
    (the packs re-point drifted entries to one canonical shim path), so the
    identity has to come from what the harness hands us. Explicit KHIPU_HARNESS
    always wins (the Aegis pack sets it)."""
    osenv = os.environ if osenv is None else osenv
    explicit = osenv.get("KHIPU_HARNESS")
    if explicit:
        return explicit
    if osenv.get("GROK_HOOK_NAME") or osenv.get("GROK_HOOK_EVENT"):
        return "aegis"
    tp = str(_get(env, "transcriptPath", "transcript_path", default="") or "")
    if "conversation_id" in env or "workspace_roots" in env or "/.cursor/" in tp:
        return "cursor"
    # Path evidence before env-only Codex markers: a Claude session started
    # inside Codex inherits CODEX_HOME but still writes under a Claude home.
    if "/.claude/" in tp or _in_claude_home(tp, osenv):
        return "claude_code"
    if "/.codex/" in tp or osenv.get("CODEX_HOME") or osenv.get("CODEX_SANDBOX"):
        return "codex"
    if osenv.get("CLAUDE_PROJECT_DIR"):
        return "claude_code"
    return "unknown"


def _in_claude_home(tp: str, osenv: dict) -> bool:
    """A transcript under ``<home>/projects/`` of any Claude home Khipu finds
    (CLAUDE_CONFIG_DIR, T3's accounts): ``/.claude/`` only names the default."""
    if not tp:
        return False
    try:
        from khipu import claude_homes

        return claude_homes.transcript_home(tp, claude_homes.discover(environ=osenv)) is not None
    except Exception:  # noqa: BLE001 — inference is best-effort, never fatal
        return False


def _current_claude_home() -> Path:
    """The Claude config home this hook is running with: ``CLAUDE_CONFIG_DIR``
    when set (``~`` means the real home), else ``~/.claude``."""
    raw = (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    if raw:
        try:
            from khipu import claude_homes

            expanded = claude_homes._expand(raw, Path.home())
        except Exception:  # noqa: BLE001 — best-effort, never fatal in a hook
            expanded = None
        if expanded is not None:
            return expanded
    return Path.home() / ".claude"


def _relocated_claude_transcript(pointer: Path, sid: str) -> Path | None:
    """A known Claude session's transcript under the hook's own config home.

    A session started under one config home and resumed under another
    (``CLAUDE_CONFIG_DIR=~/.claude-t3-second claude --resume <id>``, or the
    reverse) keeps its transcript in the projects folder of the home it is
    now running under, while the payload still names the old home's path.
    The old file may still exist, so look for ``<sid>.jsonl`` (or the
    pointer's file name) under ``<current home>/projects/*/`` first; the
    caller then captures from the file found here."""
    if not sid:
        return None
    projects = _current_claude_home() / "projects"
    try:
        if not projects.is_dir():
            return None
        names = {f"{sid}.jsonl", pointer.name} - {""}
        candidates = sorted({p for p in projects.glob("*/*.jsonl") if p.name in names and p.is_file()})
    except OSError:
        return None
    if not candidates:
        return None
    # Same project folder as the pointer wins: it is the same cwd.
    for candidate in candidates:
        if candidate.parent.name == pointer.parent.name:
            return candidate
    return candidates[0]


def transcript_path(env: dict, harness: str = "", *, synth_sessions: set[str] | None = None) -> Path | None:
    from khipu.synth_cli import session_ids

    if session_id(env) in (session_ids() if synth_sessions is None else synth_sessions):
        return None
    p = _get(env, "transcriptPath", "transcript_path")
    if p:
        return Path(str(p))
    sid = session_id(env)
    cwd = session_cwd(env)
    # Grok omits the pointer only when updates.jsonl does not exist yet; the
    # session dir is $GROK_HOME/sessions/<percent-encoded cwd>/<sessionId>/.
    if cwd and sid and harness in ("aegis", "grok_build", ""):
        grok_home = Path(os.environ.get("GROK_HOME") or str(Path.home() / ".grok"))
        cand = grok_home / "sessions" / urllib.parse.quote(str(cwd), safe="") / str(sid) / "updates.jsonl"
        if cand.is_file():
            return cand
    # Cursor keeps one JSONL per conversation under its per-project tree.
    if sid and harness in ("cursor", ""):
        hits = glob.glob(str(Path.home() / ".cursor" / "projects" / "*" / "agent-transcripts" / sid / f"{sid}.jsonl"))
        if hits:
            return Path(hits[0])
    return None


# ---- transcript readers ---------------------------------------------------------

def _clean_user_text(text: str) -> str:
    """The typed part of a user turn (``turn_text.clean_user_text``)."""
    return clean_user_text(text)


# "User" messages the harness writes, not the person: background-task
# notifications, messages relayed between sessions, slash-command wrappers. An
# orchestrator session receives hundreds of them; counted as user turns and
# rendered in full they made a 398 MB transcript look like 529 turns and queue
# 104 summariser calls in one capture (2026-10-13). None of them counts as a
# user turn. A relayed cross-session message carries a teammate's words, so its
# whole body stays (outer tag stripped); a task notification keeps its
# <summary> text; the rest stay as one line so the summariser knows they
# happened.
_TAG_NAME = re.compile(r"<([A-Za-z][\w-]*)")
_OUTER_TAG = re.compile(r"^<[^>]*>|</[^>]*>\s*$")
_SUMMARY = re.compile(r"<summary>(.*?)</summary>", re.S)


def _machine_user_message(text: str) -> str | None:
    """What the window keeps for a machine-generated user message, or None
    when ``text`` (already cleaned) is something the person wrote."""
    head = text.lstrip()
    if not is_machine(head):
        return None
    m = _TAG_NAME.match(head)
    tag = m.group(1) if m else "notification"
    if tag.startswith("cross-session-"):
        return (_OUTER_TAG.sub("", head.strip()).strip()
                or f"[notification: {tag}, {len(text):,} chars]")
    if tag == "task-notification":
        s = _SUMMARY.search(head)
        summary = s.group(1).strip() if s else ""
        if summary:
            return f"[notification: task-notification] {summary}"
    return f"[notification: {tag}, {len(text):,} chars]"


# Parts Codex prepends to a user response_item that the user never typed:
# AGENTS.md, environment/app context, plugin lists, and the <image> wrappers
# around attached files (the typed text is its own part).
_CODEX_INJECTED = re.compile(
    r"\s*(# AGENTS\.md instructions\b|# Files mentioned by the user:|</?(?:image|recommended_plugins|"
    r"environment_context|user_instructions|permissions instructions|app-context|collaboration_mode|"
    r"skills_instructions|apps_instructions|plugins_instructions|multi_agent_mode|turn_aborted)\b)"
)


def _blocks_text(content: Any) -> tuple[str, list[str], bool]:
    """(text, tool markers, is_tool_result_only) for a Claude/Cursor/Codex
    message content — a string or a list of typed blocks."""
    if isinstance(content, str):
        return content, [], False
    if not isinstance(content, list):
        return "", [], False
    parts: list[str] = []
    tools: list[str] = []
    saw_result = False
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t in ("text", "input_text", "output_text"):
            if isinstance(b.get("text"), str):
                parts.append(b["text"])
        elif t == "tool_use":
            tools.append(str(b.get("name") or "tool")[:160])
        elif t == "tool_result":
            saw_result = True
    return "\n".join(parts), tools, (saw_result and not parts)


def _parse_line(d: dict) -> list[tuple[str, str]]:
    """One transcript line → zero or more (role, text) items, for any of the
    shapes Khipu reads (sniffed per line, so a mixed file still works):

      Aegis ACP   {"params": {"update": {"sessionUpdate": ..., "content": ...}}}
      Claude Code {"type": "user"|"assistant", "message": {"role", "content"}}
      Cursor      {"role": ..., "message": {"content": [...]}}
      Codex       {"type": "event_msg", "payload": {"type": "user_message"|
                   "agent_message", "message": ...}}  (+ function_call items)
    """
    u = (d.get("params") or {}).get("update") if isinstance(d.get("params"), dict) else None
    if isinstance(u, dict):
        kind = u.get("sessionUpdate")
        if kind in ("user_message_chunk", "agent_message_chunk"):
            c = u.get("content") or {}
            text = c.get("text") if isinstance(c, dict) and c.get("type") == "text" else None
            return [("user" if kind == "user_message_chunk" else "assistant", text)] if text else []
        if kind == "tool_call":
            return [("tool", str(u.get("title") or u.get("kind") or "tool")[:160])]
        return []
    if d.get("type") in ("event_msg", "response_item") and isinstance(d.get("payload"), dict):
        p = d["payload"]
        pt = p.get("type")
        if pt == "user_message" and isinstance(p.get("message"), str):
            return [("user", p["message"])]
        if pt == "agent_message" and isinstance(p.get("message"), str):
            return [("assistant", p["message"])]
        if pt == "function_call":
            return [("tool", str(p.get("name") or "tool")[:160])]
        if pt == "message" and d.get("type") == "response_item" and p.get("role") in ("user", "assistant"):
            # Codex desktop / app-server rollouts (Aug 2026) write ONLY these —
            # no event_msg user_message/agent_message at all. Emitted as a
            # provisional role; read_window pairs individual matching copies
            # with event_msg turns. Injected context parts are dropped here.
            parts = [b.get("text") for b in (p.get("content") or []) if isinstance(b, dict)
                     and b.get("type") in ("input_text", "output_text") and isinstance(b.get("text"), str)]
            if p["role"] == "user":
                parts = [t for t in parts if not _CODEX_INJECTED.match(t)]
            text = "\n".join(parts)
            return [(f"codex:{p['role']}", text)] if text.strip() else []
        return []
    msg = d.get("message")
    if isinstance(msg, dict):
        role = str(msg.get("role") or d.get("role") or "")
        if role not in ("user", "assistant"):
            return []
        text, tools, result_only = _blocks_text(msg.get("content"))
        if role == "user" and result_only:
            return []                     # a tool result carried in a "user" line is not a turn
        out: list[tuple[str, str]] = []
        if text.strip():
            out.append((role, text))
        out.extend(("tool", t) for t in tools)
        return out
    return []


# PNG/JPEG only (same table as embed_batch_images). Drain lands files; the hook
# does not — Aegis hooks cannot write ~/.config/khipu. Embed stays on
# ``khipu embed-media-backfill``.
_LAND_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
}
_MAX_LAND_BYTES = 4 * 1024 * 1024


def _normalize_land_mime(mime: str | None) -> str | None:
    raw = (mime or "").strip().lower().split(";")[0].strip()
    if raw == "image/jpg":
        raw = "image/jpeg"
    if raw in ("image/png", "image/jpeg"):
        return raw
    return None


def _bytes_from_b64(data: str | None, mime: str | None) -> tuple[bytes, str] | None:
    mime_n = _normalize_land_mime(mime)
    if not mime_n or not isinstance(data, str) or not data.strip():
        return None
    try:
        raw = base64.b64decode(data, validate=False)
    except (ValueError, TypeError):
        return None
    if not raw or len(raw) > _MAX_LAND_BYTES:
        return None
    return raw, mime_n


def _bytes_from_data_uri(value: str) -> tuple[bytes, str] | None:
    if not value.startswith("data:image/"):
        return None
    try:
        header, b64 = value.split(",", 1)
    except ValueError:
        return None
    mime = header[5:].split(";")[0]
    return _bytes_from_b64(b64, mime)


def _land_allow_roots(
    transcript_path: Path,
    dest_root: Path,
    source_roots: list[Path],
) -> list[Path]:
    """Resolved trees a JSONL filesystem image path may be copied from."""
    roots: list[Path] = []
    for candidate in (transcript_path.parent, dest_root, *source_roots):
        try:
            roots.append(candidate.resolve())
        except OSError:
            continue
    return roots


def _path_under_roots(resolved: Path, allow_roots: list[Path]) -> bool:
    for root in allow_roots:
        try:
            if resolved.is_relative_to(root):
                return True
        except (ValueError, OSError):
            continue
    return False


def _bytes_from_local_path(
    value: Any,
    allow_roots: list[Path] | None = None,
) -> tuple[bytes, str] | None:
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("data:image/"):
        return _bytes_from_data_uri(value)
    if value.startswith("http://") or value.startswith("https://"):
        return None
    p = Path(value)
    if value.startswith("file://"):
        p = Path(value[7:])
    if not p.is_absolute():
        return None
    try:
        resolved = p.resolve()
    except OSError:
        return None
    # resolve() first; leftover `..` or a path that escaped the allowlist
    # (including symlink traversal) must not be copied.
    if ".." in resolved.parts:
        return None
    if not _path_under_roots(resolved, allow_roots or []):
        return None
    if not resolved.is_file():
        return None
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }.get(resolved.suffix.lower())
    if not mime:
        return None
    try:
        nbytes = resolved.stat().st_size
    except OSError:
        return None
    if nbytes > _MAX_LAND_BYTES:
        return None
    try:
        raw = resolved.read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    return raw, mime


def _image_blobs_from_obj(
    obj: Any,
    *,
    allow_roots: list[Path] | None = None,
    depth: int = 0,
) -> list[tuple[bytes, str]]:
    if depth > 12 or obj is None:
        return []
    found: list[tuple[bytes, str]] = []
    if isinstance(obj, str):
        blob = _bytes_from_data_uri(obj)
        return [blob] if blob else []
    if isinstance(obj, list):
        for item in obj:
            found.extend(
                _image_blobs_from_obj(item, allow_roots=allow_roots, depth=depth + 1)
            )
        return found
    if not isinstance(obj, dict):
        return []
    inline = obj.get("inline_data") or obj.get("inlineData")
    if isinstance(inline, dict):
        blob = _bytes_from_b64(
            inline.get("data") if isinstance(inline.get("data"), str) else None,
            inline.get("mime_type") or inline.get("mimeType"),
        )
        if blob:
            found.append(blob)
    source = obj.get("source")
    if isinstance(source, dict):
        blob = _bytes_from_b64(
            source.get("data") if isinstance(source.get("data"), str) else None,
            source.get("media_type") or source.get("mediaType") or source.get("mime_type"),
        )
        if blob:
            found.append(blob)
        for key in ("path", "url", "file_path", "filePath"):
            blob = _bytes_from_local_path(source.get(key), allow_roots)
            if blob:
                found.append(blob)
    att = obj.get("attachment") if isinstance(obj.get("attachment"), dict) else None
    if att:
        blob = _bytes_from_b64(
            att.get("data") if isinstance(att.get("data"), str) else None,
            att.get("media_type") or att.get("mediaType") or att.get("mime"),
        )
        if blob:
            found.append(blob)
        for key in ("path", "url", "file_path", "filePath", "filename"):
            blob = _bytes_from_local_path(att.get(key), allow_roots)
            if blob:
                found.append(blob)
    if isinstance(obj.get("data"), str) and obj["data"].startswith("data:image/"):
        blob = _bytes_from_data_uri(obj["data"])
        if blob:
            found.append(blob)
    for v in obj.values():
        if v is inline or v is source or v is att:
            continue
        found.extend(
            _image_blobs_from_obj(v, allow_roots=allow_roots, depth=depth + 1)
        )
    return found


def land_transcript_images(
    path: Path,
    *,
    start_offset: int = 0,
    end_offset: int | None = None,
) -> dict[str, int]:
    """Copy PNG/JPEG out of a JSONL window into conversation_memory's media root.

    No Gemini. Drain-only. Skips when ``conversation_memory.embed_media`` is off.
    """
    from khipu.sources import (
        conversation_media_root,
        embed_media_enabled,
        sources_with_embed_media,
    )

    stats = {"landed": 0, "skipped_existing": 0, "skipped": 0}
    if not embed_media_enabled("conversation_memory"):
        return stats
    try:
        size = path.stat().st_size
    except OSError as e:
        _log(f"land-images: cannot stat {path}: {e}")
        return stats
    start = max(0, int(start_offset))
    end = size if end_offset is None else max(start, min(int(end_offset), size))
    if start >= end:
        return stats
    try:
        with path.open("rb") as f:
            f.seek(start)
            data = f.read(end - start)
    except OSError as e:
        _log(f"land-images: cannot read {path}: {e}")
        return stats
    dest_root = conversation_media_root()
    source_roots: list[Path] = []
    for src in sources_with_embed_media():
        raw = src.get("root")
        if not isinstance(raw, str) or not raw.strip():
            continue
        source_roots.append(Path(raw).expanduser())
    allow_roots = _land_allow_roots(path, dest_root, source_roots)
    seen: set[str] = set()
    for raw_line in split_lines(data):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            d = json.loads(raw_line)
        except ValueError:
            continue
        for raw, mime in _image_blobs_from_obj(d, allow_roots=allow_roots):
            sha = hashlib.sha256(raw).hexdigest()
            if sha in seen:
                stats["skipped_existing"] += 1
                continue
            seen.add(sha)
            ext = _LAND_MIME_EXT[mime]
            dest = dest_root / f"{sha}{ext}"
            if dest.is_file():
                stats["skipped_existing"] += 1
                continue
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            try:
                tmp.write_bytes(raw)
                tmp.replace(dest)
            except OSError as e:
                stats["skipped"] += 1
                _log(f"land-images: write failed {dest.name}: {e}")
                continue
            stats["landed"] += 1
    return stats


# Claude Code writes the retained conversation back into the transcript after
# every compaction: the same messages again, at new offsets, with new uuids
# (timestamps are no marker; copies carry old and new ones). Reading forward
# from the offset captured every copy again; one 398 MB orchestrator transcript
# had 25,054 assistant lines for 2,483 distinct messages (2026-10-13). The
# capture paths pass the session's fingerprints of already-captured messages
# and an exact copy is dropped: nothing is lost.
SEEN_TEXT_MAX = 60_000
# Short messages ("ok", "yes", "continue") recur legitimately across a session
# and must keep counting as turns; only messages at least this long are
# fingerprinted. A re-emitted short copy costs nothing to summarise again.
FINGERPRINT_MIN_CHARS = 40


def text_fingerprint(role: str, text: str) -> str:
    return hashlib.sha1(f"{role}\n{text}".encode("utf-8")).hexdigest()[:16]


def remember_seen(st: dict, fresh: list[str]) -> None:
    """Append ``fresh`` fingerprints to the state's ``seen_text`` (newest last),
    bounded at SEEN_TEXT_MAX."""
    if fresh:
        st["seen_text"] = (list(st.get("seen_text") or []) + fresh)[-SEEN_TEXT_MAX:]


def _fingerprints_for_msgs(msgs: list[tuple[str, str]]) -> list[str]:
    """Fingerprints for messages long enough to enter ``seen_text``."""
    out: list[str] = []
    for role, text in msgs:
        if role in ("user", "assistant") and len(text) >= FINGERPRINT_MIN_CHARS:
            out.append(text_fingerprint(role, text))
    return out


def read_window(path: Path, offset: int, *, seen: Any = None,
                stats: dict | None = None, prefix: dict | None = None,
                replay_prefix: dict | None = None,
                end_offset: int | None = None) -> tuple[list[tuple[str, str]], int, int]:
    """Parse the transcript from ``offset``. Returns (messages, new_offset,
    user_turns). Consecutive same-role text is coalesced; tool calls become
    one-line markers; thoughts / reasoning / meta lines are dropped.

    With ``seen`` (fingerprints already captured in this session), a user or
    assistant message whose fingerprint is in ``seen`` or earlier in this
    window is dropped and is not a user turn. ``stats`` then receives
    ``deduped_msgs``, ``deduped_chars`` and ``fresh`` (the window's new
    fingerprints, oldest first), to be remembered only once the window is
    captured: the offset does not move on a not-due run, and the next run
    must still see these messages. ``skip_to`` is the byte offset just past
    the window's leading run of lines that held nothing but already-seen
    copies: a not-due run may move the offset there without losing anything,
    so a 60 MB re-emission is not re-parsed on every Stop until a real turn."""
    msgs: list[tuple[str, str]] = []
    users = 0
    known = set(seen) if seen is not None else None
    fresh: list[str] = []
    dropped = dropped_chars = 0
    if stats is not None:
        stats.update(deduped_msgs=0, deduped_chars=0, fresh=fresh, skip_to=offset)
    try:
        size = path.stat().st_size
    except OSError:
        return msgs, offset, 0
    if offset > size:  # file truncated / rotated: start over
        offset = 0
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read() if end_offset is None else f.read(max(0, end_offset - offset))
    end = offset + len(data)
    if data and not data.endswith(b"\n"):  # partial trailing line: leave it for next time
        cut = data.rfind(b"\n")
        data, end = (data[:cut + 1], offset + cut + 1) if cut >= 0 else (b"", offset)
    parsed: list[tuple[dict, str, str, int]] = []
    line_end = offset
    for raw_bytes in data.split(b"\n"):
        line_end += len(raw_bytes) + 1
        raw = raw_bytes.decode("utf-8", "replace").strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("isMeta"):
            continue
        parsed.extend((d, role, text, min(line_end, end)) for role, text in _parse_line(d))
    # Deduplicate only paired representations, never unrelated response-only
    # turns or repeated messages from a single representation.
    from collections import Counter

    event_copies = Counter((r, t.strip()) for d, r, t, _ in parsed
                           if r in ("user", "assistant") and d.get("type") == "event_msg")
    items: list[tuple[dict, str, str, int]] = []
    for d, role, text, line_end in parsed:
        if role.startswith("codex:"):
            role = role[6:]
            if event_copies[(role, text.strip())]:
                event_copies[(role, text.strip())] -= 1
                continue
        # ACP chunks include meaningful standalone spaces and newlines. Clean
        # the assembled turn once, instead of stripping individual fragments.
        if (isinstance(d.get("params"), dict) and items and items[-1][1] == role
                and isinstance(items[-1][0].get("params"), dict)):
            previous = items[-1]
            items[-1] = (previous[0], role, previous[2] + text, line_end)
            continue
        items.append((d, role, text, line_end))
    # An ordered digest proves an entire copied prefix, including short turns
    # and tool markers. A text set cannot distinguish an old "yes" from a new
    # one. Only skip this prefix after a file reset and an exact sequence match.
    checkpoints: list[dict | None] = []
    progress = dict(prefix) if prefix is not None else (
        {"count": 0, "digest": ""} if offset == 0 else None)
    replay_end = 0
    for index, (_, role, text, _) in enumerate(items, start=1):
        if progress is not None:
            progress = {"count": progress["count"] + 1, "digest": hashlib.sha256(
                f"{progress['digest']}\n{role}\n{text}".encode("utf-8")).hexdigest()}
            if replay_prefix and progress == replay_prefix:
                replay_end = index
        checkpoints.append(progress)
    if stats is not None:
        stats["prefix"] = progress
    if replay_end:
        dropped = replay_end
        dropped_chars = sum(len(text) for _, _, text, _ in items[:replay_end])
        if stats is not None:
            stats.update(skip_to=items[replay_end - 1][3], skip_prefix=checkpoints[replay_end - 1])
    items = items[replay_end:]
    checkpoints = checkpoints[replay_end:]
    last_user_counted = False   # the open user block already holds a real turn
    head_open = True            # every message so far was an already-seen copy
    # After a leading already-seen long turn, tool markers and short copies in
    # the same re-emitted prefix are not new turns (compaction appends them).
    skipping_copies = False
    for (d, role, text, line_end), checkpoint in zip(items, checkpoints):
        real = False
        if role == "user":
            text = _clean_user_text(text)
            if not text.strip():
                continue
            stand_in = _machine_user_message(text)
            real = stand_in is None
            if stand_in is not None:
                text = stand_in
        if role == "tool":
            if head_open and skipping_copies:
                dropped += 1
                if stats is not None:
                    stats["skip_to"] = line_end
                    stats["skip_prefix"] = checkpoint
                continue
            msgs.append(("tool", text))
            head_open = False
            continue
        streamed = isinstance(d.get("params"), dict)
        if known is not None and not streamed and len(text) >= FINGERPRINT_MIN_CHARS:
            # ACP chunks are fragments of one message (" the", "\n"), never
            # whole messages, so they are not fingerprinted.
            fp = text_fingerprint(role, text)
            if fp in known:
                dropped += 1
                dropped_chars += len(text)
                if head_open:
                    skipping_copies = True
                    if stats is not None:
                        stats["skip_to"] = line_end
                        stats["skip_prefix"] = checkpoint
                continue
            known.add(fp)
            fresh.append(fp)
        elif (head_open and skipping_copies and not streamed
              and len(text) < FINGERPRINT_MIN_CHARS):
            dropped += 1
            dropped_chars += len(text)
            if stats is not None:
                stats["skip_to"] = line_end
                stats["skip_prefix"] = checkpoint
            continue
        if head_open and d.get("isCompactSummary") and stats is not None:
            # Claude Code's own summary sits ahead of the copies it re-emits;
            # it leaves the head open and rides along (``carry``) when the
            # offset later steps past both.
            stats.setdefault("carry", []).append((role, text))
        else:
            head_open = False
        if msgs and msgs[-1][0] == role:
            # ACP streams chunks (concatenate); the others emit whole
            # messages (join on a newline).
            sep = "" if streamed else "\n"
            msgs[-1] = (role, msgs[-1][1] + sep + text)
        else:
            msgs.append((role, text))
            if role == "user":
                last_user_counted = False
        if real and not last_user_counted:
            users += 1
            last_user_counted = True
    if stats is not None:
        stats.update(deduped_msgs=dropped, deduped_chars=dropped_chars)
    return msgs, end, users


def _reset_transcript_offset(st: dict, path: Path) -> None:
    """Byte offsets belong to a particular file, unlike text fingerprints."""
    info = path.stat()
    identity = [info.st_dev, info.st_ino]
    reset = ((st.get("transcript_path") and st["transcript_path"] != str(path))
            or st.get("transcript_identity") not in (None, identity)
            or int(st.get("offset", 0)) > info.st_size)
    if (not reset and not st.get("transcript_prefix") and not st.get("replay_prefix")
            and int(st.get("offset", 0)) > 0 and st.get("transcript_identity") == identity):
        # Upgrade existing offsets while the original file is still present.
        # Once it has been replaced, its uncached short history is unprovable.
        prior: dict = {}
        read_window(path, 0, stats=prior, end_offset=int(st["offset"]))
        st["transcript_prefix"] = prior.get("prefix")
    if reset:
        # A reset can reuse an earlier 0:N range. Namespace every later window
        # by this file generation so fresh text is not an exact-window replay.
        # Deterministic from persisted state + file identity: a crash between
        # enqueue and state-save produces the same generation on retry.
        st["transcript_generation"] = hashlib.sha256(json.dumps(
            [st.get("transcript_generation"), identity, str(path)],
            separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        st["offset"] = 0
        if st.get("transcript_prefix"):
            st["replay_prefix"] = st.pop("transcript_prefix")
        st.pop("carry", None)
        st.pop("sweep_checked_mtime", None)
    st["transcript_identity"] = identity


def render(msgs: list[tuple[str, str]], *, max_chars: int = MAX_TRANSCRIPT) -> str:
    lines = []
    for role, text in msgs:
        lines.append(f"[tool] {text}" if role == "tool" else f"{role.upper()}: {text}")
    out = "\n\n".join(lines)
    if len(out) > max_chars:  # keep the tail, on a message boundary
        out = out[-max_chars:]
        nl = out.find("\n\n")
        out = out[nl + 2:] if 0 <= nl < 2000 else out
    return out


# ---- window split (K3, lossless by default) --------------------------------------
#
# A window over max_chars used to be tail-clipped by render() — the middle was
# gone, unrecorded. Now it is split on MESSAGE boundaries into several parts,
# each queued as its own job (chronological, oldest first); every part is
# extracted into its own episode. Oversize single messages are fragmented too.
#
# Lossless by default: every part is queued. MAX_PARTS_PER_CAPTURE
# (KHIPU_CAPTURE_MAX_PARTS) is the owner's opt-in cap: when set,
# _enqueue_window queues only the newest parts, records the dropped chars as
# truncated_chars on the first kept part, logs it, and still advances the
# offset past the whole window. Spend is bounded instead by the synth daily
# call ceiling (khipu.embed scope "synth", `khipu budget`), which delays
# captures rather than dropping them; on 2026-10-13 one orchestrator session
# queued 89 and then 104 parts in a morning and ran the Google project into its
# monthly spending cap.


def _split_messages(msgs: list[tuple[str, str]], max_chars: int) -> list[list[tuple[str, str]]]:
    """Group messages into bounded, lossless rendered parts."""
    parts: list[list[tuple[str, str]]] = []
    cur: list[tuple[str, str]] = []
    cur_len = 0
    for role, text in msgs:
        prefix = "[tool] " if role == "tool" else f"{role.upper()}: "
        body = text
        room = max(1, max_chars - len(prefix))
        fragments = [body[i:i + room] for i in range(0, len(body), room)] or [""]
        for fragment in fragments:
            piece_len = len(prefix) + len(fragment)
            add = piece_len + (2 if cur else 0)
            if cur and cur_len + add > max_chars:
                parts.append(cur)
                cur, cur_len, add = [], 0, piece_len
            cur.append((role, fragment))
            cur_len += add
    if cur:
        parts.append(cur)
    return parts


def _window_parts(msgs: list[tuple[str, str]], max_chars: int) -> list[dict[str, Any]]:
    """Chronological parts, each ``{"text": ..., "truncated_chars": ...}``.
    Lossless; the opt-in part cap is applied by ``_enqueue_window``."""
    groups = _split_messages(msgs, max_chars)
    return [{"text": render(g, max_chars=max_chars), "truncated_chars": 0} for g in groups]


# ---- state + cadence ------------------------------------------------------------

def _safe(sid: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in sid)[:120] or "unknown"


def _state_file(harness: str, sid: str) -> Path:
    return state_dir() / f"{_safe(harness)}--{_safe(sid)}.json"


def load_state(harness: str, sid: str) -> dict:
    try:
        return json.loads(_state_file(harness, sid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"offset": 0, "last_ts": 0.0, "captures": 0}


def save_state(harness: str, sid: str, st: dict, *, preserve_mtime: bool = False) -> None:
    p = _state_file(harness, sid)
    p.parent.mkdir(parents=True, exist_ok=True)
    previous = p.stat() if preserve_mtime else None
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st), encoding="utf-8")
    os.replace(tmp, p)
    if previous is not None:
        os.utime(p, ns=(previous.st_atime_ns, previous.st_mtime_ns))


# The login folder a conversation ran under (settings clarity phase 9,
# discovery source 2): the value of its CLI's folder variable in the hook's own
# environment, and nothing else. Grok-native packs use the same Grok home.
LOGIN_HOME_VARS = {"claude_code": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME",
                   "aegis": "GROK_HOME", "grok_build": "GROK_HOME"}
LOGIN_HOME_FIELD = "login_home"
# Cleared from a session's state this long after its ``seen_ts``; discovery
# counts conversations over 7 days, so nothing that counts is lost.
LOGIN_HOME_KEEP_S = 30 * 86400
LOGIN_HOME_SWEEP_S = 86400
_LOGIN_SWEEP_STAMP = "login-homes.swept"
# When the owner last pressed Forget: discovery ignores every session seen at
# or before it (its recorded folder and the one its transcript implies).
LOGIN_FORGOTTEN_STAMP = "login-homes.forgotten"


def login_home(harness: str) -> str | None:
    """The hook's own login-folder variable for ``harness``, or None."""
    name = LOGIN_HOME_VARS.get(harness)
    value = (os.environ.get(name) or "").strip() if name else ""
    return value[:1024] if value and value.isprintable() else None


# Every read and write of the login-home fields goes through a descriptor for
# the capture state folder, opened from the capture root with O_NOFOLLOW at each
# step, and each entry is opened relative to it with O_NOFOLLOW: no symlink
# below the root (the root's own name, ``state``, an entry) can redirect a read
# or a write into a login folder, even one swapped in after the listing.
_O_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
STATE_READ_MAX = 16 * 1024 * 1024
LOGIN_PENDING = "login-homes.pending"


@contextmanager
def _state_dir_fd(*, create: bool = False):
    """A descriptor for the capture state folder, or None when it is missing
    or any step to it is a symlink. ``create`` makes a missing folder first."""
    fd = None
    for attempt in range(2):
        root_fd = None
        try:
            root_fd = os.open(khipu_home(), _O_DIR)
            fd = os.open("state", _O_DIR, dir_fd=root_fd)
            break
        except FileNotFoundError:
            if not create or attempt:
                break
            try:
                state_dir().mkdir(parents=True, exist_ok=True)
            except OSError:
                break
        except OSError:
            break
        finally:
            if root_fd is not None:
                os.close(root_fd)
    try:
        yield fd
    finally:
        if fd is not None:
            os.close(fd)


def _state_names(dfd: int) -> list[str]:
    try:
        with os.scandir(dfd) as entries:
            return sorted(e.name for e in entries
                          if e.name.endswith(".json") and e.is_file(follow_symlinks=False))
    except OSError:
        return []


def _read_state_at(dfd: int, name: str) -> str | None:
    """One regular file in the state folder, never through a symlink."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > STATE_READ_MAX:
            return None
        chunks, size = [], 0
        while size <= STATE_READ_MAX:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks).decode("utf-8") if size <= STATE_READ_MAX else None
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)


def _write_at(dfd: int, name: str, text: str, *, mode: int = 0o600, times_ns=None) -> None:
    """Replace ``name`` in the state folder atomically, never through a symlink."""
    tmp = f"{name}.login.tmp"
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp, dir_fd=dfd)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=dfd)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
    if times_ns is not None:
        os.utime(name, ns=times_ns, dir_fd=dfd, follow_symlinks=False)


@contextmanager
def _lock_at(dfd: int, lock: str):
    """``_path_lock`` relative to the state folder's descriptor: the same
    directory, so it excludes the hook's own lock for the file."""
    acquired = None
    try:
        for _ in range(2):
            try:
                os.mkdir(lock, dir_fd=dfd)
                acquired = os.stat(lock, dir_fd=dfd, follow_symlinks=False)
                break
            except FileExistsError:
                try:
                    if time.time() - os.stat(lock, dir_fd=dfd, follow_symlinks=False).st_mtime <= 60:
                        break
                    os.rmdir(lock, dir_fd=dfd)
                except FileNotFoundError:
                    continue
                except OSError:
                    break
            except OSError:
                break
        yield acquired is not None
    finally:
        if acquired is not None:
            with contextlib.suppress(OSError):
                current = os.stat(lock, dir_fd=dfd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (acquired.st_dev, acquired.st_ino):
                    os.rmdir(lock, dir_fd=dfd)


def iter_states():
    """``(file name, state)`` for every readable session state file, read
    through the state folder's descriptor (see ``_state_dir_fd``)."""
    with _state_dir_fd() as dfd:
        if dfd is None:
            return
        for name in _state_names(dfd):
            text = _read_state_at(dfd, name)
            if text is None:
                continue
            try:
                st = json.loads(text)
            except ValueError:
                continue
            if isinstance(st, dict):
                yield name, st


def _clear_at(dfd: int, name: str, *, older_than: float | None) -> str:
    """Drop the login folder from one state file under the hook's own lock
    for it, keeping everything else, its mode and its mtime. Returns
    ``cleared``, ``busy`` (the lock is held), ``failed`` (the rewrite could
    not be written) or ``unchanged`` (no field, too recent, corrupt, gone or
    not a regular file: never rewritten)."""
    lock = name[: -len(".json")] + ".lock"
    with _lock_at(dfd, lock) as acquired:
        if not acquired:
            return "busy"
        text = _read_state_at(dfd, name)
        try:
            st = json.loads(text) if text is not None else None
        except ValueError:
            st = None
        if not isinstance(st, dict) or LOGIN_HOME_FIELD not in st:
            return "unchanged"
        if older_than is not None:
            try:
                seen = float(st.get("seen_ts") or 0)
            except (TypeError, ValueError):
                seen = 0.0
            if seen >= older_than:
                return "unchanged"
        st.pop(LOGIN_HOME_FIELD, None)
        try:
            info = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                return "unchanged"
        except OSError:
            return "unchanged"
        try:
            _write_at(dfd, name, json.dumps(st), mode=stat.S_IMODE(info.st_mode),
                      times_ns=(info.st_atime_ns, info.st_mtime_ns))
        except OSError:
            return "failed"
        return "cleared"


def _pending_at(dfd: int) -> list[str]:
    text = _read_state_at(dfd, LOGIN_PENDING)
    try:
        names = json.loads(text) if text else []
    except ValueError:
        names = []
    return [n for n in names if isinstance(n, str) and n.endswith(".json") and "/" not in n] \
        if isinstance(names, list) else []


def _save_pending_at(dfd: int, names) -> None:
    names = sorted(set(names))
    if names:
        _write_at(dfd, LOGIN_PENDING, json.dumps(names))
    else:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(LOGIN_PENDING, dir_fd=dfd)


def _pending_erasures_clear() -> None:
    with _state_dir_fd() as dfd:
        if dfd is not None:
            _save_pending_at(dfd, [])


def login_homes_pending() -> int:
    """How many busy sessions a Forget still has to clear."""
    with _state_dir_fd() as dfd:
        return len(_pending_at(dfd)) if dfd is not None else 0


def _apply_pending(dfd: int) -> int:
    pending = _pending_at(dfd)
    if not pending:
        return 0
    cleared, busy = 0, []
    for name in pending:
        result = _clear_at(dfd, name, older_than=None)
        cleared += result == "cleared"
        if result in ("busy", "failed"):
            busy.append(name)
    with contextlib.suppress(OSError):
        _save_pending_at(dfd, busy)
    return cleared


def sweep_login_homes(*, now: float | None = None, force: bool = False) -> int:
    """Apply Forget's pending erasures (every call), then clear login folders
    recorded more than ``LOGIN_HOME_KEEP_S`` ago (at most once a day; ``force``
    ignores that). The hook calls it after releasing its own lock; never
    raises."""
    try:
        now = time.time() if now is None else now
        with _state_dir_fd() as dfd:
            if dfd is None:
                return 0
            cleared = _apply_pending(dfd)
            if not force:
                with contextlib.suppress(OSError):
                    if now - os.stat(_LOGIN_SWEEP_STAMP, dir_fd=dfd, follow_symlinks=False).st_mtime \
                            < LOGIN_HOME_SWEEP_S:
                        return cleared
            older = now - LOGIN_HOME_KEEP_S
            cleared += sum(_clear_at(dfd, n, older_than=older) == "cleared" for n in _state_names(dfd))
            # The owner-turn retention, as a backstop to the replica-refresh timer.
            from khipu import owner_turns

            owner_turns.sweep(now)
            with contextlib.suppress(OSError):
                _write_at(dfd, _LOGIN_SWEEP_STAMP, "", times_ns=(int(now * 1e9), int(now * 1e9)))
            return cleared
    except Exception:  # noqa: BLE001 — retention never fails a capture
        return 0


def forget_login_homes(*, now: float | None = None) -> dict:
    """``khipu usage --forget-login-paths``: clear the recorded login folder
    from every session state file (the rest of each state is untouched) and
    stamp the time, so discovery also stops deriving a folder for any session
    seen at or before it. A session whose hook holds its lock is recorded as
    pending and cleared by the next sweep (the hook runs one after releasing
    its lock); ``pending`` says how many are left. ``failed`` counts files
    that could not be rewritten (and the stamp, if it could not be written),
    and ``unreachable`` says the state folder could not be opened at all:
    either means the paths may still be on disk."""
    now = time.time() if now is None else now
    with _state_dir_fd(create=True) as dfd:
        if dfd is None:
            return {"cleared": 0, "pending": 0, "failed": 0, "unreachable": True}
        failed = 0
        try:
            _write_at(dfd, LOGIN_FORGOTTEN_STAMP, repr(float(now)))
        except OSError:
            failed += 1
        cleared, busy, retry = 0, [], []
        for name in _state_names(dfd):
            result = _clear_at(dfd, name, older_than=None)
            cleared += result == "cleared"
            if result == "busy":
                busy.append(name)
            elif result == "failed":
                retry.append(name)
        failed += len(retry)
        try:
            # A file that failed is retried by the next sweep, like a busy one.
            _save_pending_at(dfd, busy + retry)
        except OSError:
            failed += 1
        return {"cleared": cleared, "pending": len(busy), "failed": failed, "unreachable": False}


def login_homes_forgotten_at() -> float | None:
    """When the owner last pressed Forget, or None."""
    with _state_dir_fd() as dfd:
        if dfd is None:
            return None
        text = _read_state_at(dfd, LOGIN_FORGOTTEN_STAMP)
        try:
            value = float((text or "").strip())
        except ValueError:
            try:
                return os.stat(LOGIN_FORGOTTEN_STAMP, dir_fd=dfd, follow_symlinks=False).st_mtime
            except OSError:
                return None
        return value if math.isfinite(value) else None


@contextmanager
def _state_lock(harness: str, sid: str):
    """Serialize offset writers without delaying a harness Stop."""
    with _path_lock(_state_file(harness, sid).with_suffix(".lock")) as acquired:
        yield acquired


@contextmanager
def _path_lock(lock: Path):
    """The state lock at ``lock`` (a directory made with mkdir): never
    waits; a lock older than 60 s is stale and taken over."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    acquired = None
    try:
        for _ in range(2):
            try:
                lock.mkdir()
                acquired = lock.stat()
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime <= 60:
                        break
                    lock.rmdir()
                except FileNotFoundError:
                    continue
                except OSError:
                    break
        yield acquired is not None
    finally:
        if acquired is not None:
            try:
                current = lock.stat()
                if (current.st_dev, current.st_ino) == (acquired.st_dev, acquired.st_ino):
                    lock.rmdir()
            except FileNotFoundError:
                pass


# ---- capture now (K1) ------------------------------------------------------------
#
# A flag file per (harness, session_id), consumed the first time decide() sees
# it. Two writers: `khipu capture now` (CLI) and the MCP `khipu_capture` tool
# on a hook-owned install, where a direct write would double-capture — this is
# what it does instead of refusing outright.

def _capture_now_flag_path(harness: str, sid: str) -> Path:
    return state_dir() / f"{_safe(harness)}--{_safe(sid)}.capture-now.json"


@contextmanager
def _capture_now_lock(harness: str, sid: str):
    """Keep request replacement atomic with durable enqueue acknowledgement."""
    lock = _capture_now_flag_path(harness, sid).with_suffix(".lock")
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        stream = lock.open("a+")
    except OSError:
        yield False
        return
    with stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def request_capture_now(harness: str, sid: str, note: str | None = None) -> Path:
    p = _capture_now_flag_path(harness, sid)
    with _capture_now_lock(harness, sid) as acquired:
        if not acquired:
            raise OSError("capture request lock unavailable")
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        pending = _pending_capture_now(harness, sid)
        text = str(pending.get("note") or "") if pending else ""
        # End offsets preserve exact request boundaries, including multiline
        # notes, while hashes recognize repeats after the oldest text is cut.
        parts = pending.get("note_parts") if pending else None
        if not (isinstance(parts, list) and parts
                and all(isinstance(part, dict) and isinstance(part.get("end"), int)
                        and isinstance(part.get("hash"), str) for part in parts)
                and parts[-1]["end"] == len(text)):
            parts = [{"end": len(text), "hash": hashlib.sha256(text.encode()).hexdigest()}] if text else []
        if note and hashlib.sha256(note.encode()).hexdigest() not in {part["hash"] for part in parts}:
            text += ("\n\n" if text else "") + note
            parts.append({"end": len(text), "hash": hashlib.sha256(note.encode()).hexdigest()})
        cut = max(0, len(text) - MAX_CAPTURE_NOTE)
        parts = [dict(part, end=part["end"] - cut) for part in parts if part["end"] > cut]
        tmp.write_text(json.dumps({"note": text[cut:] or None, "note_parts": parts, "requested_at": _mint_ts(),
                                   "request_id": uuid.uuid4().hex}), encoding="utf-8")
        os.replace(tmp, p)
    return p


def _consume_capture_now(harness: str, sid: str) -> dict | None:
    """Read-and-delete: a request fires exactly once. None when there is none."""
    p = _capture_now_flag_path(harness, sid)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        p.unlink()
    except OSError:
        pass
    return data if isinstance(data, dict) else {}


def _pending_capture_now(harness: str, sid: str) -> dict | None:
    """Read without acknowledging: only a durable queued job consumes it."""
    try:
        data = json.loads(_capture_now_flag_path(harness, sid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and _capture_request_id(data) == load_state(harness, sid).get("queued_capture_request_id"):
        return None
    return data if isinstance(data, dict) else {}


def _capture_request_id(request: dict) -> str:
    return str(request.get("request_id") or
               hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest())


def _ack_capture_now(harness: str, sid: str, expected: dict) -> None:
    try:
        p = _capture_now_flag_path(harness, sid)
        current = json.loads(p.read_text(encoding="utf-8"))
        if current != expected:
            return
        p.unlink()
    except (OSError, ValueError):
        pass


# A capture-now request has to name a session that is actually being captured,
# and its caller (the MCP server, the CLI) is not the hook. Claude supplies an
# inherited session id; otherwise each hook's recorded working directory and
# process ancestry let resolve_session_ref match the caller to a session.
ANCESTOR_LOOKUP_TIMEOUT_S = 1
ONLY_ACTIVE_WINDOW_S = 30 * 60
PROCESS_MATCH_MAX_AGE_S = 12 * 60 * 60
CAPTURE_AMBIGUITY_WINDOW_S = 30 * 60


def ancestor_pids(pid: int | None = None) -> list[int]:
    """Ancestor process ids of ``pid`` (default: this process), nearest first,
    pid 1 and the process itself excluded. One ``ps`` call. Fails open to an
    empty list: it runs inside a hook, which must never raise or wait on it."""
    try:
        start = os.getpid() if pid is None else int(pid)
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid="], capture_output=True, text=True,
            timeout=ANCESTOR_LOOKUP_TIMEOUT_S, check=False,
        ).stdout
        parent: dict[int, int] = {}
        for line in out.splitlines():
            cols = line.split()
            if len(cols) == 2:
                parent[int(cols[0])] = int(cols[1])
        chain: list[int] = []
        cur = parent.get(start, 0)
        while cur > 1 and cur not in chain and len(chain) < 64:
            chain.append(cur)
            cur = parent.get(cur, 0)
        return chain
    except Exception:  # noqa: BLE001 — an identity hint must never fail a hook
        return []


def _same_dir(a: Any, b: Any) -> bool:
    try:
        return bool(a) and bool(b) and Path(str(a)).resolve() == Path(str(b)).resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def _dir_after(parts: tuple[str, ...], marker: str) -> str | None:
    """The path component right after the last ``marker`` component."""
    idx = [i for i, p in enumerate(parts) if p == marker]
    return parts[idx[-1] + 1] if idx and idx[-1] + 1 < len(parts) else None


def _transcript_project_matches(harness: str, tpath: Any, cwd: str) -> bool:
    """For a state file that predates the recorded ``cwd``: does the project
    directory encoded in its transcript path name the caller's directory?
    Only where the encoding can be checked exactly: Claude Code's project
    directory is the working directory with every non-alphanumeric character
    replaced by '-', and Grok's session directory is the percent-encoded
    working directory. The other harnesses give no such handle, so no match."""
    try:
        parts = Path(str(tpath)).parts
        if harness == "claude_code":
            return _dir_after(parts, "projects") == re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd).resolve()))
        if harness in ("aegis", "grok_build"):
            enc = _dir_after(parts, "sessions")
            return enc is not None and _same_dir(urllib.parse.unquote(enc), cwd)
    except (OSError, RuntimeError, ValueError):
        pass
    return False


def _capture_candidates() -> list[dict]:
    """State files of sessions a capture-now request can target: the real
    capture harnesses only. Anything else in the directory (the per-prompt
    recall dedup files, a subagent's own tracking file) is not a session."""
    out: list[dict] = []
    try:
        files = list(state_dir().glob("*--*.json"))
    except OSError:
        return out
    for p in files:
        if p.name.endswith(".capture-now.json"):
            continue
        harness, _, sid = p.name[: -len(".json")].partition("--")
        if harness not in HARNESSES or not sid:
            continue
        try:
            mtime = p.stat().st_mtime
            st = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(st, dict) or st.get("subagent") or "_agent_" in sid:
            continue
        # A sweep saves capture progress, but only a hook establishes activity.
        try:
            mtime = float(st["seen_ts"]) if st.get("seen_ts") is not None else mtime
        except (TypeError, ValueError):
            continue
        out.append({"harness": harness, "sid": sid, "mtime": mtime, "state": st})
    return out


def _latest_capture_turn(candidate: dict) -> dict:
    from khipu import owner_turns

    sid = candidate["state"].get("session_id") or candidate["sid"]
    try:
        rows = owner_turns._read_lines(owner_turns.session_file(sid))
        latest = next((r for r in reversed(rows)
                       if r.get("session") == sid and r.get("harness") == candidate["harness"]), {})
        return latest
    except (OSError, ValueError, TypeError):
        return {}


def _newest_capture_candidate(candidates: list[dict], now: float, *, process: bool = False) -> dict | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # A session's prompt time outranks its capture progress. In the folder
    # fallback, state times put harnesses without prompts on the same clock.
    has_turns = any(c.get("turn_ts") is not None for c in candidates)
    if process and not has_turns:
        return None
    def activity(candidate: dict) -> float:
        return candidate.get("turn_ts") or (0 if process else candidate["mtime"])

    ranked = sorted(candidates, key=activity, reverse=True)
    first, second = [activity(c) for c in ranked[:2]]
    if first == second:
        return None
    if (not process and now - second <= CAPTURE_AMBIGUITY_WINDOW_S
            and first - second <= CAPTURE_AMBIGUITY_WINDOW_S):
        return None
    return ranked[0]


def _capture_ambiguity_error(candidates: list[dict]) -> ValueError:
    names = ", ".join(f"{c['harness']}:{c['sid'][:8]}"
                      for c in sorted(candidates, key=lambda c: -c["mtime"]))
    return ValueError(
        f"cannot tell which session is yours among {names}; "
        "pass session_id='<harness>:<id>' (the full session id)."
    )


def resolve_session_ref(harness: str | None = None, sid: str | None = None, *,
                        host_pids: list[int] | None = None,
                        cwd: str | None = None) -> tuple[str, str, str]:
    """(harness, session id, how) for a capture-now request; ``how`` is
    "explicit" | "session_env" | "process" | "cwd" | "only_active". Raises
    ValueError rather than guess between two live sessions.

    An explicit harness and session id are taken as given, the harness having
    to be a real one. Otherwise the caller is matched to what the hooks
    recorded: Claude's inherited session id, a matching issuer (pid and start
    time), legacy ancestry, the working directory and prompt times, then the
    only session active in the last half hour. Shared issuers are narrowed by
    directory; two recently prompted holders refuse. Folder fallback also
    refuses between sessions active in the last half hour. The caller's
    ancestry and directory default to this process's. Conflicting harness
    markers require the nearest issuer's verified pid and start time."""
    if harness and harness not in HARNESSES:
        raise ValueError(f"unknown capture harness {harness!r}; expected one of {', '.join(HARNESSES)}")
    if harness and sid:
        return harness, sid, "explicit"
    cands = [c for c in _capture_candidates() if not harness or c["harness"] == harness]
    # The own-session marker is authoritative even before its first hook.
    # An unmatched marker cannot fall through to another session's heuristics.
    codex_tid = os.environ.get("CODEX_THREAD_ID")
    claude_sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    grok_marker = any(k.startswith("GROK_HOOK_") for k in os.environ)
    mixed_markers = sum(bool(marker) for marker in (codex_tid, claude_sid, grok_marker)) > 1
    if (not sid and codex_tid and harness in (None, "codex")
            and not mixed_markers):
        own = [c for c in cands if c["harness"] == "codex"
               and (c["state"].get("session_id") or c["sid"]) == codex_tid]
        if len(own) == 1:
            return "codex", own[0]["sid"], "session_env"
        raise ValueError("this Codex session has no matching capture state yet; "
                         "wait for its first hook or pass its full session id explicitly")
    if sid:
        cands = [c for c in cands if c["sid"] == _safe(sid)]
        if len({c["harness"] for c in cands}) != 1:
            raise ValueError(f"cannot tell the harness of session {sid!r}; pass session_id='<harness>:<id>'")
        return cands[0]["harness"], sid, "explicit"
    if not cands:
        raise ValueError(
            "no capture session found: no per-session state file exists yet (the capture "
            "hook has not run once in this session). Pass session_id='<harness>:<id>' "
            "explicitly, or wait for the hook's first run and retry."
        )
    from khipu import owner_turns

    now = time.time()
    if cwd is None:
        try:
            cwd = os.getcwd()
        except OSError:
            cwd = ""
    same = [c for c in cands if cwd and (
        _same_dir(c["state"].get("cwd"), cwd)
        or (not c["state"].get("cwd")
            and _transcript_project_matches(c["harness"], c["state"].get("transcript_path"), cwd)))]
    issuers = []
    for c in cands:
        turn = _latest_capture_turn(c)
        try:
            ts = float(turn.get("ts"))
            c["turn_ts"] = ts if math.isfinite(ts) and ts > 0 else None
        except (TypeError, ValueError):
            c["turn_ts"] = None
        issuer = turn.get("issuer")
        issuers.append((c, issuer if isinstance(issuer, dict) else {}))
    # Nested harnesses inherit each other's markers in either direction.
    # No environment variable can establish which process is nearer.
    env_sid = None if mixed_markers or grok_marker else claude_sid
    if env_sid:
        for candidate, issuer in issuers:
            if (candidate["harness"] != "claude_code"
                    or (candidate["state"].get("session_id") or candidate["sid"]) != env_sid):
                continue
            # /clear and resume retain the MCP server's launch environment.
            # Only a newer prompt from that same process can supersede it.
            if (candidate["turn_ts"] is not None and issuer.get("pid") is not None
                    and issuer.get("start") is not None):
                successors = [other for other, other_issuer in issuers
                              if other["harness"] == "claude_code"
                              and other_issuer.get("pid") == issuer["pid"]
                              and other_issuer.get("start") == issuer["start"]
                              and (other.get("turn_ts") or 0) > candidate["turn_ts"]]
                if successors:
                    best = _newest_capture_candidate(successors, now, process=True)
                    if best is None:
                        raise _capture_ambiguity_error(cands)
                    candidate = best
            return candidate["harness"], candidate["sid"], "session_env"
    pids = ancestor_pids() if host_pids is None else host_pids
    if mixed_markers:
        from khipu.owner_direction import _nearest_harness

        # Stop at the nearest actual harness, even if it has no prompt record
        # yet. Skipping it could send a child's note to its recorded parent.
        chain = []
        for pid in pids:
            info = owner_turns._proc_info(pid)
            if info is None:
                raise _capture_ambiguity_error(cands)
            chain.append((pid, info[2], os.path.basename(os.path.realpath(info[1]))))
            nearest = _nearest_harness(chain)
            if nearest is not None:
                holders = [c for c, issuer in issuers
                           if (issuer.get("pid"), issuer.get("start")) == nearest]
                if len(holders) == 1:
                    return holders[0]["harness"], holders[0]["sid"], "process"
                raise _capture_ambiguity_error(cands)
        raise _capture_ambiguity_error(cands)
    mismatched = set()
    ambiguous_process = False
    # Prompt records stay current even when the capture state has been idle.
    # The kernel start time binds an issuer to its process, not a reused pid.
    for pid in pids:
        recorded = [(c, issuer) for c, issuer in issuers if issuer.get("pid") == pid]
        if not recorded:
            continue
        info = owner_turns._proc_info(pid)
        holders = [c for c, issuer in recorded if info is not None
                   and issuer.get("start") is not None and issuer["start"] == info[2]]
        mismatched.update((c["harness"], c["sid"], pid) for c, issuer in recorded
                          if c not in holders)
        if len(holders) == 1:
            return holders[0]["harness"], holders[0]["sid"], "process"
        if holders:
            folder_holders = [c for c in holders if c in same]
            recent_holders = [c for c in folder_holders if c.get("turn_ts") is not None
                              and now - c["turn_ts"] <= CAPTURE_AMBIGUITY_WINDOW_S]
            if len(recent_holders) < 2:
                best = _newest_capture_candidate(folder_holders, now, process=True)
                if best is not None:
                    return best["harness"], best["sid"], "process"
            ambiguous_process = True
            break
    # A process id is reused once its owner is gone, so a session that has
    # been quiet for half a day no longer vouches for the ids it recorded.
    live = [c for c in cands if now - c["mtime"] <= PROCESS_MATCH_MAX_AGE_S]
    for pid in ([] if ambiguous_process else pids):
        # A known issuer mismatch cannot become a match through pid-only state.
        holders = [c for c in live if pid in (c["state"].get("host_pids") or [])
                   and (c["harness"], c["sid"], pid) not in mismatched]
        if holders and len(holders) < len(live):
            if len(holders) == 1:
                return holders[0]["harness"], holders[0]["sid"], "process"
            break
    if not ambiguous_process:
        best = _newest_capture_candidate(same, now)
        if best is not None:
            return best["harness"], best["sid"], "cwd"
    recent = [c for c in cands if now - (c.get("turn_ts") or c["mtime"]) <= ONLY_ACTIVE_WINDOW_S]
    if len(recent) == 1 and not ambiguous_process:
        return recent[0]["harness"], recent[0]["sid"], "only_active"
    raise _capture_ambiguity_error(cands)


# ---- high-value turn trigger (K1) ------------------------------------------------
#
# A single turn worth an immediate capture, overriding the periodic cadence.
# Scanned from the rendered window (role-prefixed blocks from render()), so no
# second parse of the transcript is needed.

# The user asked outright — the highest-confidence capture trigger there is.
_HV_EXPLICIT_ASK = re.compile(r"\b(remember this|save this|note this|capture this|write that down)\b", re.I)
# The user is telling us we got something wrong; miss this window and the
# wrong version is what memory keeps.
_HV_CORRECTION = re.compile(r"\b(that'?s wrong|we already|no,|not what i|you missed)\b", re.I)
# A decision or standing instruction, from either side of the conversation.
_HV_DECISION = re.compile(r"\b(decided|approved|go with|the plan is|from now on)\b", re.I)
HIGH_VALUE_ASSISTANT_CHARS = 6_000


def _high_value_reason(window_text: str) -> str | None:
    if not window_text:
        return None
    for block in window_text.split("\n\n"):
        if block.startswith("USER: "):
            body = block[len("USER: "):]
            if _HV_EXPLICIT_ASK.search(body):
                return "high-value: explicit capture request"
            if _HV_CORRECTION.search(body):
                return "high-value: user correction"
            if _HV_DECISION.search(body):
                return "high-value: decision phrase"
        elif block.startswith("ASSISTANT: "):
            body = block[len("ASSISTANT: "):]
            if _HV_DECISION.search(body):
                return "high-value: decision phrase"
            if len(body) > HIGH_VALUE_ASSISTANT_CHARS:
                return "high-value: long assistant turn"
    return None


def decide(event: str, *, user_turns: int, chars: int, elapsed_s: float, stop_hook_active: bool,
           window_text: str = "", capture_requested: bool = False) -> tuple[bool, str]:
    if stop_hook_active:
        return False, "stop_hook_active"
    if capture_requested:
        return True, "requested"
    hv_reason = _high_value_reason(window_text)
    if hv_reason:
        return True, hv_reason
    assistant_only = any(block.startswith("ASSISTANT: ") for block in window_text.split("\n\n"))
    if chars < MIN_CHARS or (user_turns < 1 and not assistant_only):
        return False, f"nothing new (turns={user_turns}, chars={chars})"
    if event in ("precompact", "sessionend", "subagentstop"):
        return True, event
    if event == "stop":
        if user_turns >= MIN_TURNS:
            return True, f"turns>={MIN_TURNS}"
        if elapsed_s >= MIN_MINUTES * 60:
            return True, f"elapsed>={MIN_MINUTES:g}m"
        return False, f"not due (turns={user_turns}, elapsed={int(elapsed_s)}s)"
    return False, f"event {event!r} not a capture trigger"


# ---- queue ----------------------------------------------------------------------

class JobForgotten(OSError):
    """A queued job's source was forgotten; it must not be written again."""


def job_source_identity(job: dict) -> dict | None:
    """The source identity fields the capture of this job will carry, built
    the way ``drain`` builds its payload, or None when the job has no
    natural key (no session id or transcript range). Forget matches native
    jobs on this, and checkpoints refuse a job whose key is forgotten."""
    if not isinstance(job, dict):
        return None
    harness = str(job.get("harness") or "aegis")
    sid = job.get("session_id")
    span = job.get("transcript_range") or (
        f"{job.get('offset_before')}:{job.get('offset_after')}"
        if job.get("offset_before") is not None and job.get("offset_after") is not None
        else None)
    if not sid or not span:
        return None
    return {"harness": harness, "session_id": f"{harness}:{sid}", "transcript_range": span,
            "window_id": job.get("window_id") or None, "part": job.get("part") or None}


def enqueue(job: dict) -> Path:
    """Write a job durably (temp + rename) so a drain can never read a partial one."""
    q = queue_dir()
    q.mkdir(parents=True, exist_ok=True)
    stamp = job["ts"].replace(":", "").replace("-", "")
    dst = q / f"{stamp}-{_safe(job.get('harness', 'x'))}-{_safe(job['session_id'])[:40]}-{uuid.uuid4().hex[:8]}.json"
    tmp = dst.with_suffix(".tmp")
    # No memory lock here, on purpose: enqueue runs inside the Aegis sandbox,
    # which cannot write the data folder, and it holds the transcript, not an
    # episode. Forget's native-job scan takes the same claim lock, so an
    # enqueue lands either before the scan (and is removed) or after it, where
    # its drain is refused by the checkpoint and the hub.
    with _claim_lock() as acquired:
        if not acquired:
            raise OSError("queue transition lock unavailable")
        tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, dst)
    return dst


def queued_jobs() -> list[Path]:
    try:
        paths = [p for p in queue_dir().glob("*.json") if not p.name.endswith(".tmp")]
    except OSError:
        return []
    def key(path: Path) -> tuple[str, str, int, str]:
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            job = {}
        if not isinstance(job, dict):
            job = {}
        return (str(job.get("ts") or ""), str(job.get("window_id") or path.name),
                _part_index(job) or 0, path.name)
    return sorted(paths, key=key)


CLAIM_STALE_S = 900
# Five failed extraction/delivery attempts, spaced 5/10/20/40 minutes apart,
# then held for the owner's existing `outbox retry-dead` recovery action.
CAPTURE_MAX_ATTEMPTS = 5
CAPTURE_RETRY_BASE_S = 5 * 60
CAPTURE_RETRY_CAP_S = 60 * 60
# A held capture is retried automatically once per this cool-down (about once
# per nightly), with one attempt left, so a poison item costs one call per cycle.
CAPTURE_HELD_COOLDOWN_S = 20 * 3600


def _part_index(job: dict) -> int | None:
    try:
        return int(str(job.get("part") or "").split("/", 1)[0])
    except (TypeError, ValueError):
        return None


def _has_earlier_sibling(job: dict) -> bool:
    """A later window part waits until every earlier sibling is delivered."""
    window_id, index = job.get("window_id"), _part_index(job)
    if not window_id or index is None or index <= 1:
        return False
    try:
        files = list(queue_dir().glob("*.json")) + list(queue_dir().glob("*.claimed.*"))
        for path in files:
            try:
                sibling = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(sibling, dict):
                continue
            if sibling.get("window_id") == window_id and (_part_index(sibling) or index) < index:
                return True
    except OSError:
        pass
    return False


@contextmanager
def _claim_lock():
    """Kernel-owned lock for every queue-name transition."""
    lock = queue_dir() / ".claim.lock"
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        stream = lock.open("a+")
    except OSError:
        yield False
        return
    with stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _claim(job: Path, *, retry_held: bool = False) -> Path | None:
    """Take exclusive ownership of a job by renaming it. rename() is atomic, so
    of two concurrent drains exactly one wins and the loser sees FileNotFound.

    Needed because every unsandboxed Stop hook, the nightly, the app and the CLI
    can all drain at once, and each drain runs its OWN model extraction — two
    winners would mean two different summaries, i.e. two episodes for one
    session that no identity upsert can dedup (audit 2026-08-17: reproduced)."""
    from khipu import memory_lock

    with memory_lock.shared(), _claim_lock() as acquired:
        if not acquired:
            return None
        try:
            contents = json.loads(job.read_text(encoding="utf-8"))
        except OSError:
            return None
        except ValueError:
            contents = {}
        if isinstance(contents, dict):
            state = _retry_state(job)
            if retry_held:
                if state.get("disposition") != "held":
                    return None
            elif (state.get("disposition") == "held" or
                  (_parse_ts(state.get("next_attempt_at")) or 0) > time.time() or
                  _has_earlier_sibling(contents)):
                return None
        claimed = job.with_suffix(f".claimed.{os.getpid()}")
        try:
            start = _claim_process_start(os.getpid())
            # Ownership is bookkeeping, never a mutation of queued evidence.
            # A restricted process lookup falls back to conservative legacy
            # ownership, which is reclaimed only after the PID is gone.
            if start is not None:
                _write_claim_owner(claimed, {"pid": os.getpid(), "start": start})
            else:
                _claim_owner_path(claimed).unlink(missing_ok=True)
            os.rename(job, claimed)
            fd = os.open(queue_dir(), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            return claimed
        except OSError:
            if not claimed.exists():
                _claim_owner_path(claimed).unlink(missing_ok=True)
            return None


def _claim_owner_path(claimed: Path) -> Path:
    # Neither *.json nor *.claimed.* may match these metadata files.
    key = hashlib.sha256(claimed.name.encode("utf-8")).hexdigest()
    return claimed.parent / f".owner-{key}.meta"


def _write_claim_owner(claimed: Path, owner: dict) -> None:
    """Atomically persist PID/birth bookkeeping; contains no capture text.

    Caller holds _claim_lock. The sidecar survives job checkpoint replacements
    and is removed under that lock on every terminal/release transition.
    """
    path = _claim_owner_path(claimed)
    tmp = path.with_name(f".owner-{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump(owner, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _release_unlocked(claimed: Path) -> bool:
    try:
        os.rename(claimed, claimed.with_suffix("").with_suffix(".json"))
    except OSError:
        return False
    _claim_owner_path(claimed).unlink(missing_ok=True)
    return True


def _release(claimed: Path) -> bool:
    """Put a job back for a later retry (extraction/capture failed)."""
    from khipu import memory_lock

    with memory_lock.shared(), _claim_lock() as acquired:
        if acquired:
            return _release_unlocked(claimed)
    return False


def _discard_claim(claimed: Path) -> None:
    """Remove a delivered claim while sibling inspection is excluded."""
    from khipu import memory_lock

    with memory_lock.shared(), _claim_lock() as acquired:
        if acquired:
            claimed.unlink(missing_ok=True)
            _claim_owner_path(claimed).unlink(missing_ok=True)
            _retry_state_path(claimed).unlink(missing_ok=True)


def _save_claimed_job(claimed: Path, job: dict) -> None:
    """Durably checkpoint model output before attempting delivery.

    Raises ``JobForgotten`` instead of writing when the job's source is
    forgotten, or when a forget already removed the claim: a checkpoint must
    never recreate a copy a completed forget erased."""
    from khipu import memory_lock
    from khipu.tombstones import write_gate

    # Keep partial checkpoints outside ``*.claimed.*`` discovery: a crash after
    # this write must not create an immortal fake claimant that blocks siblings.
    tmp = claimed.with_name(f".extract-{uuid.uuid4().hex}.tmp")
    with memory_lock.shared(), write_gate() as gate:
        ident = job_source_identity(job)
        if ident is not None and not gate.allows(ident):
            _discard_claim(claimed)
            raise JobForgotten(f"forgotten source: {claimed.name}")
        if not claimed.exists():
            _discard_claim(claimed)
            raise JobForgotten(f"claim removed: {claimed.name}")
        try:
            with tmp.open("w", encoding="utf-8") as stream:
                json.dump(job, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, claimed)
            fd = os.open(claimed.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass


def _job_original(path: Path) -> Path:
    """The queued name of a job, whether it is queued or claimed."""
    return path if path.name.endswith(".json") else path.with_suffix("").with_suffix(".json")


def _retry_state_path(path: Path) -> Path:
    # Beside the job, never in it: a failed attempt must leave the job's bytes
    # untouched. Holds no capture text; neither *.json nor *.claimed.* match.
    key = hashlib.sha256(_job_original(path).name.encode("utf-8")).hexdigest()
    return path.parent / f".retry-{key}.meta"


def _retry_state(path: Path) -> dict:
    try:
        state = json.loads(_retry_state_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_retry_state(path: Path, state: dict) -> None:
    target = _retry_state_path(path)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _checkpoint_retry(claimed: Path, job: dict, state: dict) -> None:
    """Write retry state like a job checkpoint: raises ``JobForgotten`` rather
    than writing for a forgotten source or a claim a forget already removed."""
    from khipu import memory_lock
    from khipu.tombstones import write_gate

    with memory_lock.shared(), write_gate() as gate:
        ident = job_source_identity(job)
        if ident is not None and not gate.allows(ident):
            _discard_claim(claimed)
            raise JobForgotten(f"forgotten source: {claimed.name}")
        if not claimed.exists():
            _discard_claim(claimed)
            raise JobForgotten(f"claim removed: {claimed.name}")
        _write_retry_state(claimed, state)


def _iso_not_after(ts: float) -> str:
    """ISO time truncated, never rounded, to the microsecond: a rounded-up
    ``next_attempt_at`` would still read as future at the exact due time."""
    micros = int(Fraction(ts) * 1_000_000 // 1)
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=micros)).isoformat()


def _capture_failure(claimed: Path, job: dict, error: str, out: dict, *, free_first: bool = False) -> None:
    """Checkpoint the retry beside the job before releasing it. ``free_first``
    retries a first failure at the next drain (a delivery that returned a
    failure code spends no model call); every later one backs off. ``error`` is a kind, not text."""
    from khipu import memory_lock

    state = _retry_state(claimed)
    attempts = state.get("attempts", 0)
    attempts = attempts if isinstance(attempts, int) and not isinstance(attempts, bool) and attempts >= 0 else 0
    attempts += 1
    now = time.time()
    state = {"attempts": attempts, "last_error": error,
             "last_attempt_at": datetime.fromtimestamp(now, timezone.utc).isoformat()}
    if attempts >= CAPTURE_MAX_ATTEMPTS:
        state["disposition"] = "held"
    elif not (free_first and attempts == 1):
        delay = min(CAPTURE_RETRY_CAP_S, CAPTURE_RETRY_BASE_S * 2 ** (attempts - 1))
        state["next_attempt_at"] = _iso_not_after(now + delay)
    try:
        with memory_lock.shared():
            _checkpoint_retry(claimed, job, state)
            _release(claimed)
        if state.get("disposition") == "held":
            out["held"] = out.get("held", 0) + 1
    except JobForgotten:
        out["forgotten"] = out.get("forgotten", 0) + 1
    except OSError as exc:
        # Never put unchanged retry metadata straight back into paid work.
        # Leave the exclusive claim intact for recovery after the writer exits.
        _log(f"drain: retry checkpoint refused for {claimed.name}: {type(exc).__name__}")


def held_jobs(min_age_s: float | None = None) -> list[Path]:
    """Native dead letters stay in their queue, keeping later parts blocked.
    ``min_age_s`` keeps only those whose last attempt is at least that old."""
    held = []
    for path in queued_jobs():
        state = _retry_state(path)
        if state.get("disposition") == "held":
            age = _age(state.get("last_attempt_at"))
            if min_age_s is None or age is None or age >= min_age_s:
                held.append(path)
    return held


def retry_held(*, auto: bool = False) -> int:
    """Owner recovery used by `outbox retry-dead`; never replay a forgotten job.
    ``auto`` is the slow drain-driven retry: only held jobs past the cool-down,
    each left one attempt so another failure re-holds it."""
    return sum(_thaw_held(path, auto=auto)
               for path in held_jobs(CAPTURE_HELD_COOLDOWN_S if auto else None))


def _thaw_held(path: Path, *, auto: bool) -> bool:
    """Take one held job out of hold; True when it is queued again."""
    from khipu import memory_lock

    with memory_lock.shared():
        claimed = _claim(path, retry_held=True)
        if claimed is None:
            return False
        try:
            state = _retry_state(claimed)
            if auto:
                # Another drain may have retried and renewed this hold after
                # held_jobs checked its age but before we won the claim.
                age = _age(state.get("last_attempt_at"))
                if age is not None and age < CAPTURE_HELD_COOLDOWN_S:
                    _release(claimed)
                    return False
            state.pop("disposition", None)
            state.pop("next_attempt_at", None)
            state["attempts"] = CAPTURE_MAX_ATTEMPTS - 1 if auto else 0
            job = json.loads(claimed.read_text(encoding="utf-8"))
            _checkpoint_retry(claimed, job, state)
            return _release(claimed)
        except JobForgotten:
            return False
        except (OSError, ValueError) as exc:
            _log(f"retry-dead: native retry refused for {path.name}: {type(exc).__name__}")
            return False


def _archive_empty(claimed: Path, job: dict) -> None:
    job["disposition"] = "empty"
    from khipu import memory_lock

    with memory_lock.shared():
        _save_claimed_job(claimed, job)
        _archive_claimed(claimed)


def _archive_claimed(claimed: Path) -> Path:
    with _claim_lock() as acquired:
        if not acquired:
            raise OSError("queue transition lock unavailable")
        archive = empty_dir()
        archive.mkdir(parents=True, exist_ok=True)
        destination = archive / claimed.with_suffix("").with_suffix(".json").name
        if destination.exists():
            destination = destination.with_stem(f"{destination.stem}-{uuid.uuid4().hex}")
        with claimed.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(claimed, destination)
        _claim_owner_path(claimed).unlink(missing_ok=True)
        _retry_state_path(claimed).unlink(missing_ok=True)
        for directory in (archive, queue_dir()):
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return destination


def _quarantine_claim(claimed: Path, reason: str) -> None:
    """Keep malformed bytes in the existing archive, outside eligible work.

    The archive is already covered by Forget and retention. A sidecar records
    why parsing failed without rewriting or discarding the original evidence.
    """
    from khipu import memory_lock

    with memory_lock.shared():
        destination = _archive_claimed(claimed)
        destination.with_suffix(".error").write_text(
            json.dumps({"disposition": "malformed", "reason": reason}), encoding="utf-8")


def _claim_process_start(pid: int) -> str | None:
    """Kernel process birth identity, stable across checkpoints and PID reuse."""
    from khipu.owner_turns import _proc_info

    info = _proc_info(pid)
    if info is not None:
        return f"kernel:{info[2]}"
    # Non-macOS hosts (and restricted libproc callers) still have ps. lstart
    # has second precision, enough to distinguish previous boots/reused PIDs.
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                                capture_output=True, text=True, timeout=2,
                                env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
        value = result.stdout.strip()
        return f"ps:{value}" if result.returncode == 0 and value else None
    except (OSError, subprocess.SubprocessError):
        return None


def _claimant_gone(claimed: Path) -> bool:
    """True when the original owner is gone, including a reused numeric PID.

    A killed drainer can leave a claim with nobody working on it.
    Unreadable process identity keeps the claim. Live legacy claims without
    trusted sidecar ownership are retained: enqueue mtime survives rename and
    can predate a genuine drainer, so it cannot prove PID reuse."""
    try:
        pid = int(claimed.name.rsplit(".", 1)[-1])
    except ValueError:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    start = _claim_process_start(pid)
    if start is None:
        return False
    try:
        sidecar = _claim_owner_path(claimed)
        if sidecar.exists():
            owner = json.loads(sidecar.read_text(encoding="utf-8"))
            if not isinstance(owner, dict) or owner.get("pid") != pid or not owner.get("start"):
                return False
        else:
            owner = None  # legacy jobs carry no trusted process-birth evidence
        if isinstance(owner, dict) and owner.get("pid") == pid and owner.get("start"):
            if str(owner["start"]).partition(":")[0] != start.partition(":")[0]:
                return False  # cannot compare a fallback clock to kernel birth
            return str(owner["start"]) != start
        return False
    except (OSError, ValueError):
        return False


def _reclaim_stale() -> int:
    """A drain that died mid-job leaves a .claimed file; give it back."""
    n = 0
    with _claim_lock() as acquired:
        if not acquired:
            return 0
        try:
            for c in queue_dir().glob("*.claimed.*"):
                try:
                    # File age is not ownership evidence: a slow provider call can
                    # outlive the lease. Reclaim only after its claimant is gone.
                    if _claimant_gone(c):
                        _release_unlocked(c)
                        n += 1
                except OSError:
                    continue
        except OSError:
            return 0
    return n


# ---- heartbeat (per harness) -----------------------------------------------------

def _read_beat(harness: str) -> dict:
    try:
        d = json.loads(dispatch_path(harness).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        pass
    if harness == "aegis":   # heartbeat layout before 2026-08-17 pm: one file, Aegis only
        try:
            d = json.loads((khipu_home() / "last-dispatch.json").read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            pass
    return {}


def _write_beat(harness: str, beat: dict) -> None:
    tmp: Path | None = None
    try:
        p = dispatch_path(harness)
        p.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=p.parent, prefix=f".{p.name}.", suffix=".tmp", delete=False,
        ) as stream:
            json.dump(beat, stream)
            tmp = Path(stream.name)
        os.replace(tmp, p)
    except OSError:
        pass
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


@contextmanager
def _beat_lock(harness: str):
    """Kernel-owned per-harness lock for heartbeat read-modify-write."""
    lock = dispatch_path(harness).with_suffix(".lock")
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        stream = lock.open("a+")
    except OSError:
        yield False
        return
    with stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _heartbeat_unlocked(harness: str, out: dict) -> None:
    """Record that the harness really invoked us, and keep the running facts
    liveness needs. `verify` reads this instead of assuming dispatch — the
    assumption that hid a silent failure once."""
    beat = _read_beat(harness)
    # Per-run fields are replaced wholesale: a "no transcript" run must not
    # inherit the previous run's new_turns/queued and look like it captured.
    for k in ("event", "session_id", "due", "reason", "new_turns", "new_chars", "queued", "error",
              "deduped_msgs", "deduped_chars"):
        beat.pop(k, None)
    beat.update({k: v for k, v in out.items()
                 if k not in ("transcript", "transcript_missing", "transcript_never_written",
                              "subagent_unsupported")})
    beat["harness"] = harness
    beat["dispatches"] = int(beat.get("dispatches", 0)) + 1
    turns = int(out.get("new_turns") or 0)
    if out.get("due") and out.get("queued"):
        beat["last_due_at"] = out["at"]
        beat["last_queued_at"] = out["at"]
        beat["pending_turns"] = 0
        beat.pop("pending_since", None)
    elif turns:
        # new_turns is already the whole uncaptured window of THIS session
        # (the offset only advances on a due capture), so it is a level, not a
        # delta: summing it across runs double-counted every not-due Stop
        # (1+2+3+4 for one four-turn session) and carried abandoned sessions'
        # turns forever, which read as "cadence not firing" with nothing wrong.
        beat["pending_turns"] = turns
        beat["pending_since"] = out.get("pending_since") or out["at"]
    if out.get("error"):
        beat["last_error"] = out["error"]
        beat["last_error_at"] = out["at"]
    # K7: accumulated, never overwritten by the per-run wholesale replace above
    # — a "transcript missing" run is otherwise indistinguishable from any
    # other not-due run once the next hook fires.
    if out.get("transcript_missing"):
        beat["transcript_missing"] = int(beat.get("transcript_missing", 0)) + 1
        beat["last_transcript_missing_at"] = out["at"]
        # The lifetime total above cannot say "in the last 24 h"; this can.
        recent = [t for t in beat.get("transcript_missing_recent") or []
                  if (_age(t) if _age(t) is not None else 1 << 30) <= 24 * 3600]
        beat["transcript_missing_recent"] = (recent + [out["at"]])[-200:]
    if out.get("transcript_never_written"):
        beat["transcript_never_written"] = int(beat.get("transcript_never_written", 0)) + 1
        beat["last_transcript_never_written_at"] = out["at"]
    if out.get("subagent_unsupported"):
        beat["subagent_unsupported"] = int(beat.get("subagent_unsupported", 0)) + 1
        beat["last_subagent_unsupported_at"] = out["at"]
    _write_beat(harness, beat)


def _heartbeat(harness: str, out: dict) -> None:
    with _beat_lock(harness) as acquired:
        if acquired:
            _heartbeat_unlocked(harness, out)


def last_dispatch(harness: str = "aegis") -> dict | None:
    b = _read_beat(harness)
    return b or None


def _record_drain_unlocked(harness: str, *, captured: bool, error: str | None = None, empty: bool = False) -> None:
    beat = _read_beat(harness)
    now = _mint_ts()
    if captured:
        beat["last_captured_at"] = now
        beat["captures"] = int(beat.get("captures", 0)) + 1
        # D3: a day-bucketed counter for Home's "captured today" — resets
        # itself on the first capture of a new UTC day rather than needing a
        # separate cron/rollover job.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if beat.get("captures_today_date") != today:
            beat["captures_today_date"] = today
            beat["captures_today"] = 0
        beat["captures_today"] = int(beat.get("captures_today", 0)) + 1
        beat.pop("last_drain_error", None)
    elif empty:
        beat["last_empty_at"] = now
    if error:
        beat["last_drain_error"] = error
        beat["last_drain_error_at"] = now
    beat.setdefault("harness", harness)
    _write_beat(harness, beat)


def _record_drain(harness: str, *, captured: bool, error: str | None = None, empty: bool = False) -> None:
    with _beat_lock(harness) as acquired:
        if acquired:
            _record_drain_unlocked(harness, captured=captured, error=error, empty=empty)


def _redact_messages(msgs: list[tuple[str, str]], harness: str, sid: str) -> list[tuple[str, str]]:
    from khipu.redact import redact_secrets

    total_redacted = 0
    redacted_msgs: list[tuple[str, str]] = []
    for role, mtext in msgs:
        r, n = redact_secrets(mtext)
        total_redacted += n
        redacted_msgs.append((role, r))
    if total_redacted:
        _log(f"[{harness}] {sid} redacted {total_redacted} secret(s) before summarising")
    return redacted_msgs


def _enqueue_window(*, harness: str, sid: str, track_sid: str, cwd: str, event: str,
                    path: Path, st: dict, msgs: list[tuple[str, str]], new_off: int,
                    turns: int, job_parent: str | None = None, capture_note: str = "",
                    preserve_mtime: bool = False, fresh_seen: list[str] | None = None,
                    capture_request_id: str = "") -> list[str]:
    """Queue every part before advancing the shared capture offset."""
    off_before = int(st.get("offset", 0))
    # W1.2/W1.3: resolve identity from the cwd, not the model. Never
    # raises (identity.resolve_repo_root is itself fail-open); this
    # is the one extra bit of work the hook does before enqueueing,
    # and it must stay cheap/sandbox-safe (only `git`, 3s timeout).
    try:
        from khipu.identity import resolve_repo_root

        ident = resolve_repo_root(cwd)
    except Exception:  # noqa: BLE001 — identity is best-effort, never fatal
        ident = {"repo_root": None, "project": None, "is_worktree": False}
    # What this checkout looks like right now, observed only: no database and
    # no memory of a last project here. The hub compares observations and
    # decides whether a rename happened (capture.record_checkout_observation).
    try:
        from khipu.identity import observe_checkout

        checkout = observe_checkout(cwd, ident.get("repo_root"), ident.get("project"),
                                    khipu_home() / "checkouts")
    except Exception:  # noqa: BLE001
        checkout = None
    # K3: split on message boundaries instead of tail-clipping — every
    # part is queued and extracted into its own episode.
    parts = _window_parts(msgs, MAX_TRANSCRIPT)
    if MAX_PARTS_PER_CAPTURE > 0 and len(parts) > MAX_PARTS_PER_CAPTURE:
        dropped = parts[:-MAX_PARTS_PER_CAPTURE]
        dropped_chars = sum(len(p["text"]) + int(p["truncated_chars"]) for p in dropped)
        parts = [dict(p) for p in parts[-MAX_PARTS_PER_CAPTURE:]]
        parts[0]["truncated_chars"] = int(parts[0]["truncated_chars"]) + dropped_chars
        _log(f"[{harness}] {sid} window of {len(dropped) + len(parts)} parts clipped to "
             f"{len(parts)} (dropped {dropped_chars:,} chars)")
    # A retry after a crash must identify the same logical window at delivery.
    window_id = hashlib.sha256(
        f"{harness}:{sid}:{off_before}:{new_off}".encode("utf-8")
    ).hexdigest() if len(parts) > 1 else None
    if st.get("transcript_generation"):
        window_id = hashlib.sha256(
            f"transcript-generation:{st['transcript_generation']}:{harness}:{sid}:{off_before}:{new_off}"
            .encode("utf-8")
        ).hexdigest()
    if off_before == new_off and capture_request_id:
        window_id = hashlib.sha256(
            f"capture-request:{harness}:{sid}:{capture_request_id}".encode("utf-8")
        ).hexdigest()
    queued_names: list[str] = []
    thread = cache_thread(st, sid)
    for i, part in enumerate(parts, start=1):
        job = {"harness": harness, "session_id": sid, "cwd": cwd, "event": event,
               "ts": _mint_ts(), "turns": turns, "transcript": part["text"],
               "transcript_path": str(path), "offset_before": off_before,
               "offset_after": new_off,
               "repo_root": ident.get("repo_root"), "project": ident.get("project"),
               "parent_session_id": job_parent,
               "transcript_range": f"{off_before}:{new_off}",
               "truncated_chars": part["truncated_chars"]}
        if st.get("origin") == "job" and not job_parent:
            # KHIPU_JOB=1 with no lineage; lineage alone already marks a job.
            job["origin"] = "job"
        if checkout:
            job["checkout"] = checkout
        if window_id:
            job["window_id"] = window_id
            job["part"] = f"{i}/{len(parts)}"
        if thread:
            job.update(t3_thread_id=thread, via="t3")
        if capture_note:
            job["capture_note"] = capture_note
        if os.environ.get("KHIPU_GATE") == "1":
            # The promotion gate's own end-to-end capture (khipu.promote):
            # it is forgotten as soon as it lands.
            job["gate"] = True
        p = enqueue(job)
        queued_names.append(p.name)
    # Advance only after every part is on disk: a crash partway
    # re-queues the same window (dedup at drain) rather than losing it.
    now = time.time()
    st.update(offset=new_off, last_ts=now, window_started_ts=now,
              queued=int(st.get("queued", 0)) + len(queued_names))
    if queued_names and capture_request_id:
        st["queued_capture_request_id"] = capture_request_id
    remember_seen(st, fresh_seen or [])
    save_state(harness, track_sid, st, preserve_mtime=preserve_mtime)
    return queued_names


def _job_origin(job: dict) -> str:
    """A queued job's origin; a job queued before origin existed is a job's
    when it carries lineage, as commitments.capture_origin reads it."""
    if str(job.get("origin") or "").strip().lower() == "job" or job.get("parent_session_id"):
        return "job"
    return "conversation"


# ---- hook entrypoint (SANDBOX-SAFE) ----------------------------------------------

_SUBAGENTSTOP_SHAPE_LOGGED = "_subagentstop_shape_logged"


def _log_subagentstop_shape_once(env: dict) -> None:
    """K4: the SubagentStop payload shape is not fully documented across
    harnesses — record it the first time one actually arrives, instead of
    guessing further field names."""
    marker = state_dir() / _SUBAGENTSTOP_SHAPE_LOGGED
    if marker.exists():
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(_mint_ts(), encoding="utf-8")
    except OSError:
        pass
    _log(f"subagentstop: first sighting, payload keys = {sorted(env.keys())}")


def hook_main(raw: str, harness: str | None = None) -> dict:
    try:
        env = json.loads(raw or "{}")
    except ValueError:
        env = {}
    if not isinstance(env, dict):
        env = {}
    from khipu.synth_cli import session_ids

    synth_sessions = session_ids()
    if session_id(env) in synth_sessions:
        return {"reason": "synth-cli", "skipped": "synth-cli", "due": False,
                "session_id": session_id(env)}
    if is_helper_session(session_cwd(env)):
        # T3's thread-title helper: nothing to record, and no heartbeat either
        # (a beat for a session that never wrote a line reads as liveness noise).
        return {"skipped": "t3 helper session", "due": False}
    harness = harness or infer_harness(env)
    sid = session_id(env)
    event = norm_event(_get(env, "hookEventName", "hook_event_name"))
    # K4: a SubagentStop's own `session_id` names the PARENT session, not the
    # subagent — reusing it as-is for state/offset tracking would collide with
    # the parent's own Stop-hook cadence (two different transcript files, one
    # offset). track_sid distinguishes the subagent's own state file; the job
    # still carries the parent id separately, as parent_session_id.
    is_subagent_stop = event == "subagentstop"
    if is_subagent_stop:
        _log_subagentstop_shape_once(env)
        agent_ref = str(_get(env, "agent_id", "agentId", "turn_id", "turnId", default="") or "x")
        track_sid = f"{sid}:agent:{agent_ref}" if sid else ""
    else:
        track_sid = sid
    out: dict[str, Any] = {"harness": harness, "event": event, "session_id": sid, "due": False, "at": _mint_ts()}
    try:
        if not sid:
            out["reason"] = "no session id"
            return out
        if is_subagent_stop:
            # Codex names the child transcript agent_transcript_path; prefer it
            # when present, else fall back to the generic transcriptPath every
            # other event uses (Claude Code / Cursor).
            agent_tp = _get(env, "agent_transcript_path", "agentTranscriptPath")
            path = Path(str(agent_tp)) if agent_tp else transcript_path(env, harness, synth_sessions=synth_sessions)
        else:
            path = transcript_path(env, harness, synth_sessions=synth_sessions)
        if path is None:
            if is_subagent_stop:
                out["reason"] = "subagentstop: no transcript in payload"
                out["subagent_unsupported"] = True
            else:
                out["reason"] = "no transcript path in payload"
            return out
        # A resumed session may leave the old file intact. Resolve the
        # current config home's copy before trusting that stale pointer.
        if not is_subagent_stop and harness == "claude_code" and track_sid \
                and _state_file(harness, track_sid).exists():
            relocated = _relocated_claude_transcript(path, sid)
            if relocated is not None:
                path = relocated
        if not path.is_file():
            # Only a known session has earlier turns to lose; a new session
            # or subagent may never have persisted a transcript at all.
            if is_subagent_stop:
                out["reason"] = f"subagent transcript not written: {path}"
            elif event in ("stop", "sessionend") and track_sid and _state_file(harness, track_sid).exists():
                out["reason"] = f"transcript missing after earlier turns: {path}"
                out["transcript_missing"] = True
            else:
                out["reason"] = f"transcript never written: {path}"
                if event in ("stop", "sessionend"):
                    out["transcript_never_written"] = True
            return out
        with _state_lock(harness, track_sid) as acquired:
            if not acquired:
                out["reason"] = "locked"
                return out
            first_sight = not _state_file(harness, track_sid).exists()
            if first_sight:
                # Start the elapsed clock now, so a one-question session does not
                # become an episode on its first Stop. PreCompact/SessionEnd still do.
                # A transcript that is already large (an in-flight session at install
                # time) is joined near its end: the job carries a bounded tail anyway.
                try:
                    start = max(0, path.stat().st_size - FIRST_SIGHT_TAIL)
                except OSError:
                    start = 0
                save_state(harness, track_sid, {"offset": start, "last_ts": 0.0,
                                                 "window_started_ts": time.time(), "captures": 0,
                                                 "transcript_path": str(path)})
            st = load_state(harness, track_sid)
            _reset_transcript_offset(st, path)
            dedupe: dict[str, Any] = {}
            msgs, new_off, turns = read_window(path, int(st.get("offset", 0)),
                                               seen=st.get("seen_text") or (), stats=dedupe,
                                               prefix=st.get("transcript_prefix"),
                                               replay_prefix=st.get("replay_prefix"))
            if dedupe.get("deduped_msgs"):
                out.update(deduped_msgs=dedupe["deduped_msgs"], deduped_chars=dedupe["deduped_chars"])
            carried = [tuple(m) for m in st.pop("carry", None) or []]
            msgs = carried + msgs
            # Secrets never reach the summariser: a pasted key or a password in a
            # DSN is masked here, per message, before the window is queued to disk.
            from khipu.redact import redact_secrets

            msgs = _redact_messages(msgs, harness, sid)
            # K3: the full window, unclipped — decide() and the high-value scan see
            # everything; only the job-splitting step below ever bounds it.
            full_text = render(msgs, max_chars=10**9)
            with _capture_now_lock(harness, track_sid) as request_locked:
                capture_now = _pending_capture_now(harness, track_sid) if request_locked else None
                capture_note = ""
                if capture_now:
                    capture_note, note_redacted = redact_secrets(str(capture_now.get("note") or ""))
                    if note_redacted:
                        _log(f"[{harness}] {sid} redacted {note_redacted} secret(s) from a capture-now note")
                    if not msgs:
                        # A direct capture request is itself durable user intent;
                        # retain it even when the transcript writer has no unread
                        # turn yet. `turns` remains zero: this does not fabricate
                        # cadence history.
                        msgs = [("user", f"Capture note: {capture_note or 'capture requested'}")]
                        full_text = render(msgs, max_chars=10**9)
                due, reason = decide(
                    "subagentstop" if is_subagent_stop else event,
                    user_turns=turns, chars=len(full_text),
                    elapsed_s=time.time() - float(st.get("last_ts") or st.get("window_started_ts") or time.time()),
                    stop_hook_active=bool(_get(env, "stopHookActive", "stop_hook_active", default=False)),
                    window_text=full_text, capture_requested=capture_now is not None,
                )
                out.update(due=due, reason=reason, new_turns=turns, new_chars=len(full_text))
                # Where the hook's parse reached, and when — liveness compares the
                # transcript against THIS, not its mtime: Aegis writes housekeeping
                # (workflow_updated) into idle sessions' updates.jsonl, so mtime moves
                # with no turn in it (2026-08-24 false red).
                st.update(seen_end=new_off, seen_ts=time.time(), transcript_path=str(path),
                          host_pids=ancestor_pids())
                # Retention, Forget and discovery never follow a capture home
                # that is a symlink, so a path recorded through one could
                # never be cleared: record none there.
                recorded = None if os.path.islink(khipu_home()) else login_home(harness)
                if recorded:
                    st[LOGIN_HOME_FIELD] = recorded
                if session_cwd(env):
                    st["cwd"] = session_cwd(env)
                st["session_id"] = track_sid if is_subagent_stop else sid
                st["parent_session_id"] = sid if is_subagent_stop else (
                    parent_session_id(env, harness=harness, sid=sid) or None)
                # A subagent is its parent's job; otherwise the session's markers decide.
                st["origin"] = "job" if is_subagent_stop else capture_origin(
                    env, harness=harness, sid=sid)
                if is_subagent_stop:
                    st["subagent"] = True
                if not due and turns:
                    # When this session's uncaptured window began (its last capture, or
                    # first sight) — liveness ages pending turns from here.
                    last = float(st.get("last_ts") or st.get("window_started_ts") or 0)
                    out["pending_since"] = datetime.fromtimestamp(last, tz=timezone.utc).isoformat(
                        timespec="seconds").replace("+00:00", "Z") if last else out["at"]
                if due:
                    st["transcript_prefix"] = dedupe.get("prefix")
                    st.pop("replay_prefix", None)
                    # Carry was stepped past on a not-due run and never entered
                    # ``fresh``; fingerprint it now that it joins the capture.
                    fresh_seen = list(dedupe.get("fresh") or [])
                    for fp in _fingerprints_for_msgs(carried):
                        if fp not in fresh_seen:
                            fresh_seen.append(fp)
                    queued_names = _enqueue_window(
                        harness=harness, sid=track_sid if is_subagent_stop else sid, track_sid=track_sid,
                        cwd=session_cwd(env), event=event, path=path, st=st, msgs=msgs,
                        new_off=new_off, turns=turns, job_parent=st["parent_session_id"],
                        capture_note=capture_note, fresh_seen=fresh_seen,
                        capture_request_id=_capture_request_id(capture_now) if capture_now is not None else "",
                    )
                    out["queued"] = queued_names[0] if len(queued_names) == 1 else queued_names
                    if queued_names and capture_now is not None:
                        _ack_capture_now(harness, track_sid, capture_now)
                    deduped = (f", deduped={dedupe['deduped_msgs']} msgs/{dedupe['deduped_chars']} chars"
                               if dedupe.get("deduped_msgs") else "")
                    _log(f"{harness}:{sid}: {event} due ({reason}) -> queued {len(queued_names)} part(s) "
                         f"({turns} turns, {len(full_text)} chars{deduped})")
                else:
                    # Leading already-seen copies hold nothing to capture: step
                    # past them so the next run does not re-parse them.
                    if int(dedupe.get("skip_to") or 0) > int(st.get("offset", 0)):
                        st["offset"] = dedupe["skip_to"]
                        st["transcript_prefix"] = dedupe.get("skip_prefix")
                        st.pop("replay_prefix", None)
                        carried += dedupe.get("carry") or []
                    if carried:
                        st["carry"] = carried
                    save_state(harness, track_sid, st)
    except Exception as e:  # noqa: BLE001 — a hook must never fail a session
        out["error"] = f"{type(e).__name__}: {e}"
        _log(f"{harness}:{sid or '?'}: hook error {out['error']}")
    finally:
        _heartbeat(harness, out)
        # After the state lock is released, so a Forget left pending on this
        # very session is applied now.
        sweep_login_homes()
    return out


SWEEP_LIMIT = 25
SWEEP_TIMEOUT_S = 10


def sweep_idle(*, now: float | None = None, limit: int = SWEEP_LIMIT,
               dry_run: bool = False) -> dict:
    """Queue idle unread windows without impersonating a harness hook.

    The limit bounds transcript reads, including windows too small to queue.
    The time budget is checked between files and before enqueueing.
    """
    out = {"scanned": 0, "read": 0, "swept": 0, "jobs": 0, "skipped": {}, "dry_run": dry_run}
    from khipu.synth_cli import session_ids

    synth_sessions = session_ids()
    if dry_run:
        out["sessions"] = []

    def skip(reason: str) -> None:
        out["skipped"][reason] = out["skipped"].get(reason, 0) + 1

    def transcript_mtime(f: Path) -> float:
        try:
            st = json.loads(f.read_text(encoding="utf-8"))
            return Path(st["transcript_path"]).stat().st_mtime
        except (OSError, ValueError, TypeError, KeyError):
            return 0.0

    try:
        now = time.time() if now is None else float(now)
        limit = max(0, int(limit))
        deadline = time.monotonic() + SWEEP_TIMEOUT_S
        files = sorted(state_dir().glob("*--*.json"),
                       key=lambda f: (-transcript_mtime(f), f.name))
        for f in files:
            if time.monotonic() >= deadline:
                skip("time_budget")
                break
            out["scanned"] += 1
            harness, _, track_sid = f.stem.partition("--")
            if harness not in HARNESSES:
                skip("unknown_harness")
                continue
            if f.name.endswith(".capture-now.json") or not track_sid:
                skip("flag")
                continue
            if track_sid in synth_sessions:
                skip("synth-cli")
                continue
            # A malformed state cannot safely carry its own retry stamp.
            error_file = f.with_suffix(".sweep-error")
            try:
                if now - float(error_file.read_text()) < 3600:
                    skip("error_cached")
                    continue
            except (OSError, ValueError):
                pass
            try:
                st = json.loads(f.read_text(encoding="utf-8"))
                if not isinstance(st, dict):
                    raise ValueError("session state is not an object")
                if str(st.get("session_id") or "") in synth_sessions:
                    skip("synth-cli")
                    continue
                if st.get("sweep_error") is not None and now - float(st["sweep_error"]) < 3600:
                    skip("error_cached")
                    continue
                if st.get("subagent") or "_agent_" in track_sid:
                    skip("subagent")
                    continue
                if not st.get("transcript_path"):
                    skip("no_transcript")
                    continue
                path = Path(st["transcript_path"])
                try:
                    info = path.stat()
                except FileNotFoundError:
                    skip("missing_transcript")
                    continue
                offset = int(st.get("offset", 0))
                replaced = st.get("transcript_identity") not in (
                    None, [info.st_dev, info.st_ino])
                if offset == info.st_size and not replaced:
                    skip("nothing_unread")
                    continue
                cwd = str(st.get("cwd") or "")
                if is_helper_session(cwd):
                    skip("helper")
                    continue
                seen = st.get("seen_ts")
                if seen is None or now - float(seen) < MIN_MINUTES * 60 \
                        or now - info.st_mtime < MIN_MINUTES * 60:
                    skip("not_idle")
                    continue
                if st.get("sweep_checked_mtime") == info.st_mtime:
                    skip("unchanged")
                    continue
                if out["read"] >= limit:
                    skip("limit")
                    break
                with _state_lock(harness, track_sid) as acquired:
                    if not acquired:
                        skip("locked")
                        continue
                    latest_state = load_state(harness, track_sid)
                    if latest_state.get("offset") != st.get("offset") \
                            or latest_state.get("seen_ts") != seen:
                        skip("changed")
                        continue
                    st = latest_state
                    snapshot_state = dict(st)
                    _reset_transcript_offset(st, path)
                    offset = int(st.get("offset", 0))
                    if st.get("sweep_error") is not None and now - float(st["sweep_error"]) < 3600:
                        skip("error_cached")
                        continue
                    if st.get("sweep_checked_mtime") == info.st_mtime:
                        skip("unchanged")
                        continue
                    try:
                        out["read"] += 1
                        dedupe: dict[str, Any] = {}
                        msgs, new_off, turns = read_window(path, offset, seen=st.get("seen_text") or (),
                                                           stats=dedupe, prefix=st.get("transcript_prefix"),
                                                           replay_prefix=st.get("replay_prefix"))
                        sid = str(st.get("session_id") or track_sid)
                        carried = [tuple(m) for m in st.get("carry") or []]
                        msgs = _redact_messages(carried + msgs, harness, sid)
                        chars = len(render(msgs, max_chars=10**9))
                        latest = path.stat()
                        if (latest.st_mtime_ns, latest.st_size) != (info.st_mtime_ns, info.st_size) \
                                or load_state(harness, track_sid) != snapshot_state:
                            skip("changed")
                            continue
                        if time.monotonic() >= deadline:
                            skip("time_budget")
                            break
                        st.pop("sweep_error", None)
                        st["sweep_checked_mtime"] = info.st_mtime
                        idle_due, _ = decide("idle_sweep", user_turns=turns, chars=chars,
                                             elapsed_s=0, stop_hook_active=False,
                                             window_text=render(msgs, max_chars=10**9))
                        assistant_only = any(role == "assistant" for role, _ in msgs)
                        if not (idle_due or (chars >= MIN_CHARS and (turns or assistant_only))):
                            skip("nothing_new")
                            if not dry_run:
                                if not dedupe.get("carry"):
                                    st["offset"] = max(offset, int(dedupe.get("skip_to") or 0))
                                    if st["offset"] > offset:
                                        st["transcript_prefix"] = dedupe.get("skip_prefix")
                                        st.pop("replay_prefix", None)
                                save_state(harness, track_sid, st, preserve_mtime=True)
                            continue
                        if dry_run:
                            out["sessions"].append({
                                "harness": harness, "session_id": sid, "unread_turns": turns,
                                "unread_chars": chars,
                                "idle_minutes": (now - max(float(seen), info.st_mtime)) / 60,
                            })
                        else:
                            st.pop("carry", None)  # now part of msgs
                            st["transcript_prefix"] = dedupe.get("prefix")
                            st.pop("replay_prefix", None)
                            fresh_seen = list(dedupe.get("fresh") or [])
                            for fp in _fingerprints_for_msgs(carried):
                                if fp not in fresh_seen:
                                    fresh_seen.append(fp)
                            names = _enqueue_window(
                                harness=harness, sid=sid, track_sid=track_sid, cwd=cwd,
                                event="idle_sweep", path=path, st=st, msgs=msgs,
                                new_off=new_off, turns=turns, job_parent=st.get("parent_session_id"),
                                preserve_mtime=True, fresh_seen=fresh_seen,
                            )
                            out["jobs"] += len(names)
                            _log(f"sweep {harness}:{sid}: queued {len(names)} part(s) "
                                 f"({turns} turns, {chars} chars)")
                        out["swept"] += 1
                    except Exception:
                        if not dry_run:
                            latest_state = load_state(harness, track_sid)
                            latest_state["sweep_error"] = now
                            save_state(harness, track_sid, latest_state, preserve_mtime=True)
                        raise
            except Exception as exc:  # noqa: BLE001 — one broken state cannot stop the sweep
                skip("error")
                if not dry_run:
                    try:
                        error_file.write_text(str(now), encoding="utf-8")
                    except OSError:
                        pass
                _log(f"sweep: skipped {f.name}: {type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 — the backstop must not block the drainer
        skip("error")
        _log(f"sweep: {type(exc).__name__}: {exc}")
    if out["swept"] or os.environ.get("KHIPU_SWEEP_VERBOSE") == "1":
        _log(f"sweep summary: {json.dumps(out, sort_keys=True)}")
    return out


# ---- drain (RUNS OUTSIDE ANY SANDBOX) -------------------------------------------

def drain(*, limit: int | None = None, dry_run: bool = False, sweep: bool = False,
          time_budget_s: float | None = None) -> dict:
    """Turn queued jobs into episodes. Safe to call from anywhere unsandboxed —
    the nightly, the app, the CLI. A job leaves the queue only once its episode
    is captured or its successful empty disposition and source are archived.
    Only scheduled/manual callers opt into the idle sweep; hooks stay cheap.
    """
    # In-flight provider calls finish normally; the budget bounds starting more jobs.
    deadline = time.monotonic() + max(0, time_budget_s) if time_budget_s is not None else None
    sweep_out = sweep_idle(dry_run=dry_run) if sweep else None
    reclaimed = _reclaim_stale()
    jobs = queued_jobs()
    out = {"jobs": len(jobs), "captured": 0, "empty": 0, "failed": 0, "skipped_claimed": 0}
    if sweep_out is not None:
        out["sweep"] = sweep_out
    if reclaimed:
        out["reclaimed_stale"] = reclaimed
    if sweep and not dry_run:
        from khipu.outbox import drain as drain_outbox

        remaining = max(0, deadline - time.monotonic()) if deadline is not None else 60
        try:
            out["outbox"] = drain_outbox(limit=min(20, limit) if limit is not None else 20,
                                         time_budget_s=min(60, remaining))
            out["failed"] += out["outbox"].get("failed", 0)
        except Exception as exc:  # noqa: BLE001 — session jobs must still get a turn
            out["outbox"] = {"error": f"{type(exc).__name__}: {exc}"}
            out["failed"] += 1
        try:
            # Owner writes made while the hub was away (closing an owed item).
            from khipu import owner_writes

            if owner_writes.waiting():
                out["owner_writes"] = owner_writes.drain()
        except Exception as exc:  # noqa: BLE001 — never holds up the session jobs
            out["owner_writes"] = {"error": f"{type(exc).__name__}: {exc}"}
            out["failed"] += 1
    if not jobs:
        return out
    from khipu.capture import capture
    from khipu.config import capture_mode
    from khipu.embed import BudgetExhausted
    from khipu.extract import extract_memory

    mode = capture_mode()
    started = 0
    for idx, original in enumerate(jobs):
        if limit is not None and started >= limit:
            break
        if deadline is not None and time.monotonic() >= deadline:
            out["time_budget_exhausted"] = True
            break
        if not dry_run:
            # Thaw only a job this pass is about to claim: a pass that stops
            # early must not spend a held job's single retry.
            try:
                if _retry_state(original).get("disposition") == "held":
                    age = _age(_retry_state(original).get("last_attempt_at"))
                    if age is None or age >= CAPTURE_HELD_COOLDOWN_S:
                        _thaw_held(original, auto=True)
            except Exception as exc:  # noqa: BLE001 — a thaw problem never blocks the drain
                _log(f"drain: held-capture retry skipped ({type(exc).__name__}: {exc})")
        jp = _claim(original)
        if jp is None:                       # another drain got it first
            out["skipped_claimed"] += 1
            continue
        started += 1
        try:
            job = json.loads(jp.read_text(encoding="utf-8"))
            if not isinstance(job, dict):
                raise ValueError("queued job must be a JSON object")
        except (OSError, ValueError) as e:
            out["failed"] += 1
            # Permanent parse errors must not monopolize every bounded pass.
            # Keep the exact input and a separate reason for owner inspection.
            if isinstance(e, ValueError):
                try:
                    _quarantine_claim(jp, str(e))
                    out["quarantined"] = out.get("quarantined", 0) + 1
                except OSError:
                    _release(jp)
            else:
                _release(jp)
            _log(f"drain: unreadable job {original.name}: {e}")
            continue
        harness = str(job.get("harness") or "aegis")
        try:
            tp = job.get("transcript_path")
            if tp:
                land = land_transcript_images(
                    Path(str(tp)),
                    start_offset=int(job.get("offset_before") or 0),
                    end_offset=job.get("offset_after"),
                )
                if land.get("landed") or land.get("skipped_existing"):
                    out.setdefault("images", {"landed": 0, "skipped_existing": 0, "skipped": 0})
                    for k in ("landed", "skipped_existing", "skipped"):
                        out["images"][k] = out["images"].get(k, 0) + int(land.get(k) or 0)
                    _log(f"drain: land-images {original.name} {land}")
        except Exception as e:  # noqa: BLE001 — never block episode capture
            _log(f"drain: land-images skipped for {original.name}: {type(e).__name__}: {e}")
        # K2: regex-extracted BEFORE the model call — never model-summarized,
        # never lost when the model decides "nothing durable".
        from khipu.extract import extract_deliverables, extract_verbatim

        verbatim = extract_verbatim(job.get("transcript", ""))
        # O4: same tier as verbatim above — paths written/created, PR/issue
        # URLs, release tags, never lost to summarisation either.
        deliverables = extract_deliverables(job.get("transcript", ""))
        cached = job.get("extracted_payload")
        if job.get("disposition") == "empty":
            payload = None
        elif isinstance(cached, dict):
            payload = dict(cached)
        else:
            try:
                # A job's transcript is told so: its "user" is the orchestrator.
                payload = extract_memory(job.get("transcript", ""), cwd=job.get("cwd", ""),
                                         **({"job": True} if _job_origin(job) == "job" else {}))
            except BudgetExhausted as e:
                # Every later job would be refused the same way: stop here,
                # keep them all, and say so once instead of once per job.
                _release(jp)
                waiting = len(jobs) - idx
                from khipu.embed import budget_status

                b = budget_status("synth")
                out["synth_budget_exhausted"] = True
                out["waiting"] = waiting
                _record_drain(harness, captured=False, error=f"extract: {type(e).__name__}: {e}")
                _log(f"drain: synth call budget exhausted for today ({b.get('calls')}/{b.get('cap')}); "
                     f"{waiting} job(s) wait")
                break
            except Exception as e:  # noqa: BLE001 — model/transport: keep the job, retry later
                out["failed"] += 1
                _capture_failure(jp, job, f"extract: {type(e).__name__}", out)
                _record_drain(harness, captured=False, error=f"extract: {type(e).__name__}: {e}")
                _log(f"drain: extraction failed for {original.name}, kept: {e}")
                continue
        if payload is None and job.get("capture_note"):
            # An explicit owner's note is durable even when the model finds
            # nothing to extract. The full note lands in verbatim.note below.
            payload = {"summary": f"Capture note: {job['capture_note']}", "topics": []}
        if payload is None:
            if dry_run:
                _release(jp)
            else:
                # An empty extraction still saw the checkout: the observation
                # lands before the job is archived (best-effort, no hub = skipped).
                if job.get("checkout") and mode != "legacy":
                    from khipu.capture import record_checkout_observation

                    record_checkout_observation(job["checkout"])
                try:
                    _archive_empty(jp, job)
                except JobForgotten:
                    out["forgotten"] = out.get("forgotten", 0) + 1
                    continue
                except OSError as e:
                    out["failed"] += 1
                    # A saved "empty" checkpoint makes the retry free (no model
                    # call); only a failed checkpoint backs off.
                    try:
                        saved = json.loads(jp.read_text(encoding="utf-8")).get("disposition")
                    except (OSError, ValueError, AttributeError):
                        saved = None
                    if saved == "empty":
                        _release(jp)
                    else:
                        _capture_failure(jp, job, "empty checkpoint: OSError", out)
                    _record_drain(harness, captured=False, error=f"empty checkpoint: {e}")
                    continue
                _record_drain(harness, captured=False, empty=True)
            out["empty"] += 1
            continue
        if not isinstance(cached, dict):
            # A copy: the delivery fields added below must not ride into a
            # later failure record.
            job["extracted_payload"] = dict(payload)
            try:
                _save_claimed_job(jp, job)
            except JobForgotten:
                out["forgotten"] = out.get("forgotten", 0) + 1
                continue
            except OSError as e:
                out["failed"] += 1
                _capture_failure(jp, job, "extract checkpoint: OSError", out)
                _record_drain(harness, captured=False, error=f"extract checkpoint: {e}")
                continue
        payload["session_id"] = f"{harness}:{job.get('session_id')}"
        payload["scope"] = payload.get("scope") or f"{harness} {job.get('event')}"
        # The trigger itself, not just its trace in `scope`: commitments'
        # session-plan closure retires a session's own in-flight/plan items
        # when that session ENDS, and it needs to know that this capture is
        # the sessionend one (khipu.commitments.close_session_plan).
        payload["event"] = job.get("event")
        # The session's own time, not the drain's — a job may sit queued for hours.
        payload["ts"] = job.get("ts") or _mint_ts()
        # W1.3: identity resolved in-hook rides straight through to capture —
        # this is a legacy-payload-shaped job too (older queued jobs before
        # this field existed simply carry None here, same as any harness that
        # never resolves identity).
        payload["harness"] = harness
        payload["repo_root"] = job.get("repo_root")
        payload["project"] = job.get("project")
        if job.get("checkout"):
            payload["checkout"] = job["checkout"]
        payload["parent_session_id"] = job.get("parent_session_id")
        if _job_origin(job) == "job" and not job.get("parent_session_id"):
            # Not `origin`: that key is the episode's server-stamped owner and
            # capture drops it from every payload.
            payload["session_origin"] = "job"
        if job.get("t3_thread_id"):
            payload.update(t3_thread_id=job["t3_thread_id"], via="t3")
        payload["transcript_range"] = job.get("transcript_range") or (
            f"{job.get('offset_before')}:{job.get('offset_after')}"
            if job.get("offset_before") is not None and job.get("offset_after") is not None
            else None
        )
        # K1: a capture-now caller's text lands in verbatim.note regardless of
        # what the regex tiers found.
        note = job.get("capture_note")
        if note:
            verbatim["note"] = note
        if verbatim:
            payload["verbatim"] = verbatim
        if deliverables:
            payload["deliverables"] = deliverables
        # K3: 0 unless MAX_PARTS_PER_CAPTURE dropped the window's oldest parts.
        payload["truncated_chars"] = int(job.get("truncated_chars") or 0)
        if job.get("window_id"):
            payload["window_id"] = job["window_id"]
        if job.get("part"):
            payload["part"] = job["part"]
        if dry_run:
            print(wire.dumps(payload, indent=2, ensure_ascii=True))
            out["captured"] += 1
            _release(jp)
            continue
        # Section 13 E: routing directions in this window's owner turns,
        # counted from the transcript and joined to applied owner actions.
        # Deterministic only — no prompt text goes to any model — and a
        # dispatched job's turns are never the owner's.
        try:
            from khipu import routing_directions

            if _job_origin(job) != "job" and job.get("session_id") and job.get("transcript"):
                texts = re.findall(r"^USER: (.*?)(?=^(?:USER|ASSISTANT): |\Z)",
                                     job["transcript"], re.M | re.S)
                if texts:
                    routing_directions.capture_scan(str(job["session_id"]), texts)
        except Exception as e:  # noqa: BLE001 — a direction scan never blocks capture
            _log(f"drain: routing direction scan skipped for {original.name}: {type(e).__name__}: {e}")
        try:
            rc = capture(payload, mode=mode)
        except BudgetExhausted:
            _release(jp)
            out["synth_budget_exhausted"] = True
            break
        except Exception as e:  # noqa: BLE001 — retain the checkpointed payload
            out["failed"] += 1
            error = f"capture: {type(e).__name__}: {e}"
            _capture_failure(jp, job, f"capture: {type(e).__name__}", out)
            _record_drain(harness, captured=False, error=error)
            _log(f"drain: capture failed for {original.name}, kept: {type(e).__name__}")
            continue
        if rc != 0:
            out["failed"] += 1
            _capture_failure(jp, job, f"capture rc={rc}", out, free_first=True)
            _record_drain(harness, captured=False, error=f"capture rc={rc}")
            _log(f"drain: capture rc={rc} for {original.name}; kept")
            continue
        _discard_claim(jp)
        out["captured"] += 1
        _record_drain(harness, captured=True)
        # Never the summary itself: the log is not in forget's scan
        # (khipu/capture_stores.py), so it must hold no capture text.
        _log(f"drain: captured {original.name} ({len(payload['summary'])} chars)")
    if deadline is not None and time.monotonic() >= deadline:
        out["time_budget_exhausted"] = True
    if out["captured"] and not dry_run and not out.get("time_budget_exhausted"):
        # Phase 2, session B: "corrections made elsewhere" — a supersession
        # made on a DIFFERENT machine reaches THIS machine's local replica
        # here, piggybacking on a drain that already talked to the hub for
        # its own captures, rather than a dedicated sync job (no hook may
        # open a hub connection on its own for this). Best-effort: a sync
        # failure must never surface as a drain failure.
        try:
            from khipu import hub_snapshot

            synced = hub_snapshot.sync_decision_changes()
            if synced.get("ok") and synced.get("decisions"):
                out["decisions_synced"] = synced["decisions"]
        except Exception as exc:  # noqa: BLE001 — best-effort, never blocks capture
            _log(f"drain: decision sync skipped ({type(exc).__name__}: {exc})")
    return out


# ---- status + liveness -------------------------------------------------------------

def _age(ts: Any) -> int | None:
    t = _parse_ts(ts)
    return int(time.time() - t) if t is not None else None


def _queue_by_harness() -> dict[str, dict]:
    out: dict[str, dict] = {}
    now = time.time()
    try:
        paths = [(p, False) for p in queued_jobs()] + [
            (p, True) for p in queue_dir().glob("*.claimed.*")
        ]
    except OSError:
        paths = []
    records = []
    held_windows: dict[str, int] = {}
    for p, claimed in paths:
        try:
            job = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            job = {}
        if not isinstance(job, dict):
            job = {}
        job = {**job, **{k: v for k, v in _retry_state(p).items() if k in ("disposition", "last_error")}}
        records.append((p, claimed, job))
        if job.get("disposition") == "held" and job.get("window_id"):
            window = str(job["window_id"])
            held_windows[window] = min(held_windows.get(window, 1 << 30), _part_index(job) or 0)
    for p, claimed, job in records:
        parts = p.name.split("-")
        h = str(job.get("harness") or (parts[1] if len(parts) > 2 else "aegis"))
        try:
            st = p.stat()
            age = int(now - st.st_mtime)
            # Claiming is a rename: it moves ctime and leaves mtime at queue
            # time, so the newer of the two is when this claim was taken.
            claim_age = int(now - max(st.st_mtime, st.st_ctime))
        except OSError:
            # Gone between the listing and here: the claimant finished it.
            age = claim_age = 0
        d = out.setdefault(h, {"depth": 0, "held_depth": 0, "claimed_depth": 0, "dead_claims": 0,
                               "dead_claims_stale": 0,
                               "waiting_depth": 0, "waiting_age_s": 0, "held_errors": [],
                               "oldest_age_s": 0, "oldest_job": None})
        d["depth"] += 1
        if job.get("disposition") == "held":
            d["held_depth"] += 1
            d["held_errors"].append(job.get("last_error"))
        elif not (str(job.get("window_id") or "") in held_windows and
                  (_part_index(job) or 0) > held_windows[str(job["window_id"])]):
            d["waiting_depth"] += 1
            d["waiting_age_s"] = max(d["waiting_age_s"], age)
        if claimed:
            d["claimed_depth"] += 1
            if _claimant_gone(p):
                d["dead_claims"] += 1
                if claim_age >= DEAD_CLAIM_GRACE_S:
                    d["dead_claims_stale"] += 1
        if age >= d["oldest_age_s"]:
            d["oldest_age_s"], d["oldest_job"] = age, p.name
    return out


def status(harness: str = "aegis") -> dict:
    """Single-harness status (queue + last dispatch), as the Aegis pane used it."""
    q = _queue_by_harness().get(harness, {"depth": 0, "claimed_depth": 0, "dead_claims": 0,
                                                 "oldest_age_s": None, "oldest_job": None})
    ld = last_dispatch(harness)
    return {"home": str(khipu_home()), "harness": harness,
            "queue_depth": q["depth"], "oldest_job": q["oldest_job"],
            "claimed_depth": q["claimed_depth"], "dead_claims": q["dead_claims"],
            "last_dispatch": ld, "last_dispatch_age_s": _age(ld.get("at")) if ld else None,
            "sessions_tracked": len(list(state_dir().glob(f"{_safe(harness)}--*.json"))) if state_dir().is_dir() else 0}


def _stopped_hook_evidence(harness: str) -> tuple[str | None, int | None]:
    """(reason, seconds) when a session's transcript gained a USER turn the
    hook never saw, long after it last looked (its seen_ts/seen_end marker),
    AND has then sat quiet long enough that a Stop must have had its chance —
    a dead hook sits on the user's prompts. (None, age) when the change is
    housekeeping, assistant-only, or still in flight. Sessions without a
    marker (pre-upgrade state files, so nothing records where the hook's
    parse reached) are skipped rather than guessed at.

    Both halves are required (audit 2026-09-04). ``mtime - seen_ts`` alone
    measured how STALE the marker was, not whether the hook had failed: a
    session idle for 19 h and then resumed with one user prompt read as
    "transcript changed 1176 min after the hook last ran" and went red
    instantly — before any Stop event could possibly have fired. The second
    half (``now - mtime``) is what says a Stop was actually due and missing."""
    states = []
    try:
        for f in state_dir().glob(f"{_safe(harness)}--*.json"):
            try:
                st = json.loads(f.read_text(encoding="utf-8"))
                seen = float(st.get("seen_ts") or 0)
                states.append((seen, st))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
    except OSError:
        return None, None
    states.sort(key=lambda item: item[0], reverse=True)
    worst: tuple[int, str, int] | None = None
    newer_s: int | None = None
    now = time.time()
    for _, st in states[:ACTIVITY_SCAN_LIMIT]:
        tp, seen_ts = st.get("transcript_path"), st.get("seen_ts")
        if not tp or not seen_ts:
            continue
        try:
            mtime = Path(tp).stat().st_mtime
            age = int(mtime - float(seen_ts))
        except (OSError, TypeError, ValueError):
            continue
        if age <= HOOK_SILENT_S:
            continue
        _, _, users = read_window(Path(tp), int(st.get("seen_end", 0)))
        if not users:
            # No user prompt past the marker. An assistant-only tail is the
            # stop hook racing the writer — Aegis flushes the turn's final
            # chunk and turn_completed after the hook has read the file
            # (2026-08-25 false red) — while a dead hook would be sitting
            # on user prompts too. Housekeeping-only tails land here as well.
            newer_s = max(newer_s or 0, age)
            continue
        quiet = int(now - mtime)
        if quiet <= HOOK_SILENT_S:
            # The turn is RECENT. A session that sat idle for hours and then
            # got one prompt has a huge `age` and a tiny `quiet`; no Stop has
            # had its chance yet, so there is nothing to have stopped firing
            # (audit 2026-09-04 false red).
            newer_s = max(newer_s or 0, age)
            continue
        if worst is None or age > worst[0]:
            worst = (age, tp, quiet)
    if worst:
        return (f"transcript gained a user turn {worst[0] // 60} min after the hook "
                f"last ran and has been quiet {worst[2] // 60} min since "
                f"({worst[1]}) — the hook has stopped firing"), worst[0]
    return None, newer_s


def liveness(harness: str) -> dict:
    """Is this harness actually being recorded? Red only on evidence of a
    failure, never on mere idleness (an unused harness is fine).

      ok=False when
        * the last hook run raised (the hook itself is broken), or
        * the drain's last attempt for this harness failed and nothing has
          landed since (extraction / PG not landing), or
        * a job for this harness has waited past QUEUE_STALE_S (nothing is
          draining), or
        * the hook keeps running and seeing turns but never decides "due" for
          STUCK_TURNS turns / STUCK_MINUTES (the cadence is not firing).
    """
    beat = _read_beat(harness)
    q = _queue_by_harness().get(harness, {"depth": 0, "dead_claims": 0,
                                                 "oldest_age_s": None, "oldest_job": None})
    reasons: list[str] = []
    if not beat:
        return {"harness": harness, "ok": True, "seen": False, "reasons": [],
                "queue_depth": 0, "note": "no real session has run the hook yet"}
    err_at = _parse_ts(beat.get("last_error_at")) or 0
    if beat.get("last_error") and err_at >= (_parse_ts(beat.get("last_queued_at")) or 0):
        reasons.append(f"hook error on last run: {str(beat['last_error'])[:120]}")
    if beat.get("last_drain_error") and \
            (_parse_ts(beat.get("last_drain_error_at")) or 0) > (_parse_ts(beat.get("last_captured_at")) or 0) and \
            not any(str(beat["last_drain_error"]).startswith(str(k)) for k in q.get("held_errors", []) if k):
        reasons.append(f"last capture attempt failed: {str(beat['last_drain_error'])[:120]}")
    # Held work (and its waiting siblings) is surfaced once by outbox.status's
    # existing doctor dead-letter gate, with the owner's retry-dead action.
    if q.get("waiting_depth") and (q.get("waiting_age_s") or 0) > QUEUE_STALE_S:
        reasons.append(f"{q['waiting_depth']} job(s) waiting {q['waiting_age_s'] // 60} min — nothing is draining them")
    if q.get("dead_claims_stale"):
        reasons.append(
            f"{q['dead_claims_stale']} claimed job(s) abandoned for over "
            f"{DEAD_CLAIM_GRACE_S // 60} min — nothing is reclaiming them"
        )
    pend = int(beat.get("pending_turns") or 0)
    since = _age(beat.get("pending_since"))
    # "The hook keeps running and never decides due" requires evidence the hook
    # KEPT RUNNING. This measured only wall-clock age of pending_since, which
    # clears only on a due capture — so a harness the user simply stopped using
    # kept aging its leftover pending turns and went red on nothing but the
    # passage of time. That is red-on-idleness, which this check exists to avoid
    # (2026-08-18: Cursor red at 5 turns / 121 min after 17 h unused, and Aegis
    # red on a quiet stretch that cleared itself the moment it was used again).
    # The hook's own last run is the gate: no recent dispatch means nobody is
    # failing to decide, there is just nothing to decide about.
    hook_ran_recently = (_age(beat.get("at")) or 0) <= STUCK_MINUTES * 60
    # The beat is per harness, not per session: one session's leftover turn
    # keeps pending_since old while other sessions capture. A capture landing
    # after pending_since is proof the cadence fires.
    captured_since = since is not None and _age(beat.get("last_captured_at")) is not None \
        and _age(beat.get("last_captured_at")) < since
    if hook_ran_recently and (
        pend >= STUCK_TURNS
        or (pend >= 1 and since is not None and since >= STUCK_MINUTES * 60 and not captured_since)
    ):
        reasons.append(f"{pend} turn(s) over {(since or 0) // 60} min without a capture being due — cadence not firing")
    # Has the harness been used since the hook last ran? The transcript is the
    # one signal that does not come from the hook itself, so it is the only
    # thing that can catch the hook having stopped. mtime alone is not enough:
    # Aegis writes housekeeping (workflow_updated) into idle sessions, so the
    # file moves with no turn in it. Red requires content past the hook's own
    # seen_end marker that actually parses to messages.
    stopped, newer_s = _stopped_hook_evidence(harness)
    if stopped:
        reasons.append(stopped)
    # K7: a session Khipu had already read turns from lost its transcript —
    # those turns past the last capture are gone. Counted over the last 24 h
    # only; sessions that never wrote a transcript are not counted at all.
    missing = sum(
        1 for t in beat.get("transcript_missing_recent") or []
        if (_age(t) if _age(t) is not None else 1 << 30) <= 24 * 3600
    )
    if missing:
        reasons.append(
            f"{missing} session(s) lost their transcript after earlier turns in the last 24 h — "
            "the harness deleted or moved the transcript; check its transcript setting"
        )
    warnings: list[str] = []
    # K4: a warning, not red — SubagentStop firing with no transcript to read
    # is a harness/payload gap Khipu cannot fix, not evidence the hook itself
    # is broken.
    subagent_unsupported = int(beat.get("subagent_unsupported") or 0)
    if subagent_unsupported:
        warnings.append(
            f"{subagent_unsupported} SubagentStop event(s) carried no readable transcript "
            "(subagent_unsupported) — this harness's SubagentStop payload shape may not be "
            "understood yet; see the stop-hook log's 'subagentstop: first sighting' line"
        )
    return {"harness": harness, "ok": not reasons, "seen": True, "reasons": reasons, "warnings": warnings,
            "last_dispatch_at": beat.get("at"), "last_dispatch_age_s": _age(beat.get("at")),
            "last_event": beat.get("event"), "last_reason": beat.get("reason"),
            "last_captured_at": beat.get("last_captured_at"),
            "last_captured_age_s": _age(beat.get("last_captured_at")),
            "captures": int(beat.get("captures", 0)), "dispatches": int(beat.get("dispatches", 0)),
            "pending_turns": pend, "queue_depth": q["depth"], "queue_oldest_age_s": q.get("oldest_age_s"),
            "transcript_newer_than_hook_s": newer_s}


def liveness_all() -> dict:
    per = {h: liveness(h) for h in HARNESSES}
    red = [h for h, v in per.items() if not v["ok"]]
    return {"ok": not red, "red": red, "harnesses": per,
            "queue_depth": sum(v.get("queue_depth", 0) for v in per.values()),
            "captured_today": captured_today()}


def captured_today() -> int:
    """D3: how many captures landed today, across every harness — summed
    from each harness's own day-bucketed counter (see `_record_drain`), so
    Home's 'captured today' figure needs no DB round trip."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = 0
    for h in HARNESSES:
        beat = _read_beat(h)
        if beat.get("captures_today_date") == today:
            total += int(beat.get("captures_today") or 0)
    return total


def unknown_harness_heartbeats() -> dict:
    """K8: a heartbeat file under the dispatch dir from a harness Khipu does
    not recognise (``HARNESSES``) — evidence a hook is firing from
    somewhere unaccounted for. Warning, not red: the hook itself may be
    working fine, it's just unidentified, so this must never gate doctor's
    aggregate `ok` the way a real capture failure does."""
    warnings: list[dict] = []
    try:
        files = sorted(dispatch_dir().glob("*.json"))
    except OSError:
        files = []
    for f in files:
        name = f.stem
        if name in HARNESSES:
            continue
        try:
            beat = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            beat = {}
        warnings.append({
            "harness": name,
            "dispatches": int(beat.get("dispatches") or 0),
            "reason": beat.get("last_error") or beat.get("reason") or beat.get("event") or "unknown",
            "last_dispatch_at": beat.get("at"),
        })
    return {"warnings": warnings}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--drain":
        dry = (os.environ.get("KHIPU_CAPTURE_DRY") or os.environ.get("KHIPU_AEGIS_DRY")) == "1"
        print(wire.dumps(drain(dry_run=dry), indent=2, ensure_ascii=True))
        return 0
    if argv and argv[0] == "--status":
        print(wire.dumps(liveness_all(), indent=2, ensure_ascii=True))
        return 0
    harness = argv[1] if len(argv) >= 2 and argv[0] == "--harness" else None
    out = hook_main(wire.read_stdin(), harness)
    if os.environ.get("KHIPU_CAPTURE_PROBE") == "1" or os.environ.get("KHIPU_AEGIS_PROBE") == "1":
        print(wire.dumps(out, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        _log(f"fatal: {type(e).__name__}: {e}")
        sys.exit(0)
