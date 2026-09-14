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
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HARNESSES = ("claude_code", "cursor", "codex", "aegis")


def _env_int(new: str, old: str, default: str) -> str:
    return os.environ.get(new) or os.environ.get(old) or default


MIN_TURNS = int(_env_int("KHIPU_CAPTURE_MIN_TURNS", "KHIPU_AEGIS_MIN_TURNS", "5"))
MIN_MINUTES = float(_env_int("KHIPU_CAPTURE_MIN_MINUTES", "KHIPU_AEGIS_MIN_MINUTES", "20"))
MIN_CHARS = 200          # a window smaller than this is never worth a model call
MAX_TRANSCRIPT = 14_000  # chars carried in a job (tail of the window)
FIRST_SIGHT_TAIL = 1_000_000  # bytes: first sight of a long transcript starts this far from its end

# Liveness thresholds — "the hook is running but nothing is landing".
STUCK_TURNS = MIN_TURNS * 3          # turns seen with no capture decided due
STUCK_MINUTES = MIN_MINUTES * 3      # minutes with pending turns and nothing due
QUEUE_STALE_S = 30 * 60
# A hook that stops firing is invisible to every other check here: pending_turns
# resets to 0 on a successful capture, so a hook that dies right after one leaves
# a heartbeat with no error, no queue and nothing pending — green forever (audit
# 2026-08-17, the same shape as B15 one level up). The only honest evidence is
# the harness's own transcript: if it grew well after the hook last ran, the
# harness was in use and the hook did not fire. The margin must clear the longest
# plausible single turn (an agentic turn can run over an hour) — this is a
# stopped-hook detector, not a latency alarm.
HOOK_SILENT_S = int(_env_int("KHIPU_CAPTURE_HOOK_SILENT_MIN", "KHIPU_AEGIS_HOOK_SILENT_MIN", "120")) * 60
# Per-session state files are never pruned, so cap how many the activity scan
# reads on each `khipu doctor`; the live session is always among the newest.
ACTIVITY_SCAN_LIMIT = 20              # a job this old means the drain is not landing


def khipu_home() -> Path:
    """Khipu's working area for native capture. `~/.grok/khipu` because Aegis's
    sandbox can write there and nowhere else useful; the other harnesses share
    it so there is exactly one queue and one drainer."""
    return Path(os.environ.get("KHIPU_CAPTURE_HOME") or os.environ.get("KHIPU_AEGIS_HOME")
                or str(Path.home() / ".grok" / "khipu"))


def state_dir() -> Path:
    return khipu_home() / "state"


def queue_dir() -> Path:
    return khipu_home() / "queue"


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
        return f"{harness or 'claude_code'}:{host_id}"
    return str(_get(
        env, "parentSessionId", "parent_session_id",
        "agentSessionId", "agent_session_id", "parentId", "parent_id",
        default="",
    ) or "")


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
    if "/.codex/" in tp or osenv.get("CODEX_HOME") or osenv.get("CODEX_SANDBOX"):
        return "codex"
    if "/.claude/" in tp or osenv.get("CLAUDE_PROJECT_DIR"):
        return "claude_code"
    return "unknown"


def transcript_path(env: dict, harness: str = "") -> Path | None:
    p = _get(env, "transcriptPath", "transcript_path")
    if p:
        return Path(str(p))
    sid = session_id(env)
    cwd = session_cwd(env)
    # Aegis omits the pointer only when updates.jsonl does not exist yet; the
    # session dir is ~/.grok/sessions/<percent-encoded cwd>/<sessionId>/.
    if cwd and sid and harness in ("aegis", ""):
        cand = Path.home() / ".grok" / "sessions" / urllib.parse.quote(str(cwd), safe="") / str(sid) / "updates.jsonl"
        if cand.is_file():
            return cand
    # Cursor keeps one JSONL per conversation under its per-project tree.
    if sid and harness in ("cursor", ""):
        hits = glob.glob(str(Path.home() / ".cursor" / "projects" / "*" / "agent-transcripts" / sid / f"{sid}.jsonl"))
        if hits:
            return Path(hits[0])
    return None


# ---- transcript readers ---------------------------------------------------------

_SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_CURSOR_WRAP = re.compile(r"</?(?:user_query|timestamp)>")


def _clean_user_text(text: str) -> str:
    """Strip harness-injected scaffolding from a user turn: Claude Code's
    <system-reminder> blocks (thousands of chars of hook context, none of it the
    user) and Cursor's <user_query>/<timestamp> wrappers."""
    text = _SYSTEM_REMINDER.sub("", text)
    return _CURSOR_WRAP.sub("", text)   # no strip: ACP user text arrives in chunks that must re-join exactly


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
            # provisional role; read_window keeps them only when the window
            # has no event_msg turns, so a rollout carrying both is not
            # double counted. Injected context parts are dropped here.
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
    for raw_line in data.decode("utf-8", "replace").splitlines():
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


def read_window(path: Path, offset: int) -> tuple[list[tuple[str, str]], int, int]:
    """Parse the transcript from ``offset``. Returns (messages, new_offset,
    user_turns). Consecutive same-role text is coalesced; tool calls become
    one-line markers; thoughts / reasoning / meta lines are dropped."""
    msgs: list[tuple[str, str]] = []
    users = 0
    try:
        size = path.stat().st_size
    except OSError:
        return msgs, offset, 0
    if offset > size:  # file truncated / rotated: start over
        offset = 0
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read()
    end = offset + len(data)
    if data and not data.endswith(b"\n"):  # partial trailing line: leave it for next time
        cut = data.rfind(b"\n")
        data, end = (data[:cut + 1], offset + cut + 1) if cut >= 0 else (b"", offset)
    parsed: list[tuple[dict, str, str]] = []
    for raw in data.decode("utf-8", "replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("isMeta"):
            continue
        parsed.extend((d, role, text) for role, text in _parse_line(d))
    # Codex rollouts: event_msg turns win; response_item turns are the
    # fallback for rollouts that carry no event_msg at all.
    has_event_turns = any(r in ("user", "assistant") and d.get("type") == "event_msg" for d, r, _ in parsed)
    items: list[tuple[dict, str, str]] = []
    for d, role, text in parsed:
        if role.startswith("codex:"):
            if has_event_turns:
                continue
            role = role[6:]
        items.append((d, role, text))
    for d, role, text in items:
        if role == "user":
            text = _clean_user_text(text)
            if not text.strip():
                continue
        if role == "tool":
            msgs.append(("tool", text))
            continue
        if msgs and msgs[-1][0] == role:
            # ACP streams chunks (concatenate); the others emit whole
            # messages (join on a newline).
            sep = "" if isinstance(d.get("params"), dict) else "\n"
            msgs[-1] = (role, msgs[-1][1] + sep + text)
        else:
            msgs.append((role, text))
            if role == "user":
                users += 1
    return msgs, end, users


def render(msgs: list[tuple[str, str]], *, max_chars: int = MAX_TRANSCRIPT) -> str:
    lines = []
    for role, text in msgs:
        lines.append(f"[tool] {text}" if role == "tool" else f"{role.upper()}: {text.strip()}")
    out = "\n\n".join(lines)
    if len(out) > max_chars:  # keep the tail, on a message boundary
        out = out[-max_chars:]
        nl = out.find("\n\n")
        out = out[nl + 2:] if 0 <= nl < 2000 else out
    return out


# ---- window split (K3, no tail-clip) ---------------------------------------------
#
# A window over max_chars used to be tail-clipped by render() — the middle was
# gone, unrecorded. Now it is split on MESSAGE boundaries into several parts,
# each queued as its own job (chronological, oldest first); every part is
# extracted into its own episode. MAX_WINDOW_PARTS bounds how many jobs one
# Stop can create — beyond it, the oldest overflow is collapsed into a single
# clipped part and truncated_chars records exactly what was dropped, so loss
# is recorded instead of silent.

MAX_WINDOW_PARTS = 8


def _split_messages(msgs: list[tuple[str, str]], max_chars: int) -> list[list[tuple[str, str]]]:
    """Group msgs so each group renders to at most max_chars, breaking only
    between messages (never mid-message)."""
    parts: list[list[tuple[str, str]]] = []
    cur: list[tuple[str, str]] = []
    cur_len = 0
    for role, text in msgs:
        piece = f"[tool] {text}" if role == "tool" else f"{role.upper()}: {text.strip()}"
        add = len(piece) + (2 if cur else 0)  # "\n\n" joiner, same as render()
        if cur and cur_len + add > max_chars:
            parts.append(cur)
            cur, cur_len, add = [], 0, len(piece)
        cur.append((role, text))
        cur_len += add
    if cur:
        parts.append(cur)
    return parts


def _window_parts(msgs: list[tuple[str, str]], max_chars: int) -> list[dict[str, Any]]:
    """Chronological parts, each ``{"text": ..., "truncated_chars": ...}``.
    truncated_chars is 0 for every part unless there are more than
    MAX_WINDOW_PARTS groups, in which case the oldest overflow is merged into
    one part and tail-clipped (render()'s old behaviour), with the exact drop
    recorded rather than silently lost."""
    groups = _split_messages(msgs, max_chars)
    if len(groups) <= MAX_WINDOW_PARTS:
        return [{"text": render(g, max_chars=max_chars), "truncated_chars": 0} for g in groups]
    keep = MAX_WINDOW_PARTS - 1
    split_at = len(groups) - keep
    oldest_msgs = [m for g in groups[:split_at] for m in g]
    full = render(oldest_msgs, max_chars=10**9)
    clipped = render(oldest_msgs, max_chars=max_chars)
    merged = {"text": clipped, "truncated_chars": max(0, len(full) - len(clipped))}
    rest = [{"text": render(g, max_chars=max_chars), "truncated_chars": 0} for g in groups[split_at:]]
    return [merged, *rest]


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


def save_state(harness: str, sid: str, st: dict) -> None:
    p = _state_file(harness, sid)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st), encoding="utf-8")
    os.replace(tmp, p)


# ---- capture now (K1) ------------------------------------------------------------
#
# A flag file per (harness, session_id), consumed the first time decide() sees
# it. Two writers: `khipu capture now` (CLI) and the MCP `khipu_capture` tool
# on a hook-owned install, where a direct write would double-capture — this is
# what it does instead of refusing outright.

def _capture_now_flag_path(harness: str, sid: str) -> Path:
    return state_dir() / f"{_safe(harness)}--{_safe(sid)}.capture-now.json"


def request_capture_now(harness: str, sid: str, note: str | None = None) -> Path:
    p = _capture_now_flag_path(harness, sid)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"note": note or None, "requested_at": _mint_ts()}), encoding="utf-8")
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


def newest_session_ref() -> tuple[str, str] | None:
    """(harness, sid) of the most recently touched per-session state file —
    for a caller with no session id of its own to go on (`khipu capture now`
    with no explicit --harness/--session-id, the MCP khipu_capture tool on a
    hook-owned install). Approximate, like the queue-file parsing in
    _queue_by_harness: state file names are already lossy (_safe()), so this
    is "the session someone just used," not an identity lookup."""
    try:
        files = [p for p in state_dir().glob("*--*.json") if not p.name.endswith(".capture-now.json")]
    except OSError:
        return None
    if not files:
        return None
    try:
        newest = max(files, key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    stem = newest.name[: -len(".json")]
    if "--" not in stem:
        return None
    harness, sid = stem.split("--", 1)
    return harness, sid


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
    if user_turns < 1 or chars < MIN_CHARS:
        return False, f"nothing new (turns={user_turns}, chars={chars})"
    hv_reason = _high_value_reason(window_text)
    if hv_reason:
        return True, hv_reason
    if event in ("precompact", "sessionend"):
        return True, event
    if event == "stop":
        if user_turns >= MIN_TURNS:
            return True, f"turns>={MIN_TURNS}"
        if elapsed_s >= MIN_MINUTES * 60:
            return True, f"elapsed>={MIN_MINUTES:g}m"
        return False, f"not due (turns={user_turns}, elapsed={int(elapsed_s)}s)"
    return False, f"event {event!r} not a capture trigger"


# ---- queue ----------------------------------------------------------------------

def enqueue(job: dict) -> Path:
    """Write a job durably (temp + rename) so a drain can never read a partial one."""
    q = queue_dir()
    q.mkdir(parents=True, exist_ok=True)
    stamp = job["ts"].replace(":", "").replace("-", "")
    dst = q / f"{stamp}-{_safe(job.get('harness', 'x'))}-{_safe(job['session_id'])[:40]}-{uuid.uuid4().hex[:8]}.json"
    tmp = dst.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, dst)
    return dst


def queued_jobs() -> list[Path]:
    try:
        return sorted(p for p in queue_dir().glob("*.json") if not p.name.endswith(".tmp"))
    except OSError:
        return []


CLAIM_STALE_S = 900


def _claim(job: Path) -> Path | None:
    """Take exclusive ownership of a job by renaming it. rename() is atomic, so
    of two concurrent drains exactly one wins and the loser sees FileNotFound.

    Needed because every unsandboxed Stop hook, the nightly, the app and the CLI
    can all drain at once, and each drain runs its OWN model extraction — two
    winners would mean two different summaries, i.e. two episodes for one
    session that no identity upsert can dedup (audit 2026-08-17: reproduced)."""
    claimed = job.with_suffix(f".claimed.{os.getpid()}")
    try:
        os.rename(job, claimed)
        return claimed
    except OSError:
        return None


def _release(claimed: Path) -> None:
    """Put a job back for a later retry (extraction/capture failed)."""
    try:
        os.rename(claimed, claimed.with_suffix("").with_suffix(".json"))
    except OSError:
        pass


def _reclaim_stale() -> int:
    """A drain that died mid-job leaves a .claimed file; give it back."""
    n = 0
    now = time.time()
    try:
        for c in queue_dir().glob("*.claimed.*"):
            try:
                if now - c.stat().st_mtime > CLAIM_STALE_S:
                    _release(c)
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
    try:
        p = dispatch_path(harness)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(beat), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def _heartbeat(harness: str, out: dict) -> None:
    """Record that the harness really invoked us, and keep the running facts
    liveness needs. `verify` reads this instead of assuming dispatch — the
    assumption that hid a silent failure once."""
    beat = _read_beat(harness)
    # Per-run fields are replaced wholesale: a "no transcript" run must not
    # inherit the previous run's new_turns/queued and look like it captured.
    for k in ("event", "session_id", "due", "reason", "new_turns", "new_chars", "queued", "error"):
        beat.pop(k, None)
    beat.update({k: v for k, v in out.items()
                 if k not in ("transcript", "transcript_missing", "subagent_unsupported")})
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
    if out.get("subagent_unsupported"):
        beat["subagent_unsupported"] = int(beat.get("subagent_unsupported", 0)) + 1
        beat["last_subagent_unsupported_at"] = out["at"]
    _write_beat(harness, beat)


def last_dispatch(harness: str = "aegis") -> dict | None:
    b = _read_beat(harness)
    return b or None


def _record_drain(harness: str, *, captured: bool, error: str | None = None, empty: bool = False) -> None:
    beat = _read_beat(harness)
    now = _mint_ts()
    if captured:
        beat["last_captured_at"] = now
        beat["captures"] = int(beat.get("captures", 0)) + 1
        beat.pop("last_drain_error", None)
    elif empty:
        beat["last_empty_at"] = now
    if error:
        beat["last_drain_error"] = error
        beat["last_drain_error_at"] = now
    beat.setdefault("harness", harness)
    _write_beat(harness, beat)


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
            path = Path(str(agent_tp)) if agent_tp else transcript_path(env, harness)
        else:
            path = transcript_path(env, harness)
        if path is None:
            if is_subagent_stop:
                out["reason"] = "subagentstop: no transcript in payload"
                out["subagent_unsupported"] = True
            else:
                out["reason"] = "no transcript path in payload"
            return out
        if not path.is_file():
            # Real and benign: Claude Code fires SessionEnd for sessions that
            # never wrote a line (headless -p runs, the desktop helper). Recorded
            # by name so a wrong path on a real session is diagnosable.
            out["reason"] = f"transcript missing: {path}"
            if event in ("stop", "sessionend"):
                out["transcript_missing"] = True
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
            save_state(harness, track_sid, {"offset": start, "last_ts": time.time(), "captures": 0,
                                             "transcript_path": str(path)})
        st = load_state(harness, track_sid)
        msgs, new_off, turns = read_window(path, int(st.get("offset", 0)))
        # Secrets never reach the summariser: a pasted key or a password in a
        # DSN is masked here, per message, before the window is queued to disk.
        from khipu.redact import redact_secrets

        total_redacted = 0
        redacted_msgs: list[tuple[str, str]] = []
        for role, mtext in msgs:
            r, n = redact_secrets(mtext)
            total_redacted += n
            redacted_msgs.append((role, r))
        msgs = redacted_msgs
        if total_redacted:
            _log(f"[{harness}] {sid} redacted {total_redacted} secret(s) before summarising")
        # K3: the full window, unclipped — decide() and the high-value scan see
        # everything; only the job-splitting step below ever bounds it.
        full_text = render(msgs, max_chars=10**9)
        capture_now = _consume_capture_now(harness, track_sid)
        capture_note = ""
        if capture_now:
            capture_note, note_redacted = redact_secrets(str(capture_now.get("note") or ""))
            if note_redacted:
                _log(f"[{harness}] {sid} redacted {note_redacted} secret(s) from a capture-now note")
        due, reason = decide(
            "stop" if is_subagent_stop else event,
            user_turns=turns, chars=len(full_text),
            elapsed_s=time.time() - float(st.get("last_ts") or 0),
            stop_hook_active=bool(_get(env, "stopHookActive", "stop_hook_active", default=False)),
            window_text=full_text, capture_requested=capture_now is not None,
        )
        out.update(due=due, reason=reason, new_turns=turns, new_chars=len(full_text))
        # Where the hook's parse reached, and when — liveness compares the
        # transcript against THIS, not its mtime: Aegis writes housekeeping
        # (workflow_updated) into idle sessions' updates.jsonl, so mtime moves
        # with no turn in it (2026-08-24 false red).
        st.update(seen_end=new_off, seen_ts=time.time(), transcript_path=str(path))
        if not due and turns:
            # When this session's uncaptured window began (its last capture, or
            # first sight) — liveness ages pending turns from here.
            last = float(st.get("last_ts") or 0)
            out["pending_since"] = datetime.fromtimestamp(last, tz=timezone.utc).isoformat(
                timespec="seconds").replace("+00:00", "Z") if last else out["at"]
        if due:
            off_before = int(st.get("offset", 0))
            cwd = session_cwd(env)
            # W1.2/W1.3: resolve identity from the cwd, not the model. Never
            # raises (identity.resolve_repo_root is itself fail-open); this
            # is the one extra bit of work the hook does before enqueueing,
            # and it must stay cheap/sandbox-safe (only `git`, 3s timeout).
            try:
                from khipu.identity import resolve_repo_root

                ident = resolve_repo_root(cwd)
            except Exception:  # noqa: BLE001 — identity is best-effort, never fatal
                ident = {"repo_root": None, "project": None, "is_worktree": False}
            job_sid = track_sid if is_subagent_stop else sid
            job_parent = sid if is_subagent_stop else (parent_session_id(env, harness=harness, sid=sid) or None)
            # K3: split on message boundaries instead of tail-clipping — every
            # part is queued and extracted into its own episode.
            parts = _window_parts(msgs, MAX_TRANSCRIPT)
            window_id = uuid.uuid4().hex if len(parts) > 1 else None
            queued_names: list[str] = []
            for i, part in enumerate(parts, start=1):
                job = {"harness": harness, "session_id": job_sid, "cwd": cwd, "event": event,
                       "ts": _mint_ts(), "turns": turns, "transcript": part["text"],
                       "transcript_path": str(path), "offset_before": off_before,
                       "offset_after": new_off,
                       "repo_root": ident.get("repo_root"), "project": ident.get("project"),
                       "parent_session_id": job_parent,
                       "transcript_range": f"{off_before}:{new_off}",
                       "truncated_chars": part["truncated_chars"]}
                if window_id:
                    job["window_id"] = window_id
                    job["part"] = f"{i}/{len(parts)}"
                if capture_note:
                    job["capture_note"] = capture_note
                p = enqueue(job)
                queued_names.append(p.name)
            # Advance only after every part is on disk: a crash partway
            # re-queues the same window (dedup at drain) rather than losing it.
            st.update(offset=new_off, last_ts=time.time(), queued=int(st.get("queued", 0)) + len(queued_names))
            out["queued"] = queued_names[0] if len(queued_names) == 1 else queued_names
            _log(f"{harness}:{sid}: {event} due ({reason}) -> queued {len(queued_names)} part(s) "
                 f"({turns} turns, {len(full_text)} chars)")
        save_state(harness, track_sid, st)
    except Exception as e:  # noqa: BLE001 — a hook must never fail a session
        out["error"] = f"{type(e).__name__}: {e}"
        _log(f"{harness}:{sid or '?'}: hook error {out['error']}")
    finally:
        _heartbeat(harness, out)
    return out


# ---- drain (RUNS OUTSIDE ANY SANDBOX) -------------------------------------------

def drain(*, limit: int | None = None, dry_run: bool = False) -> dict:
    """Turn queued jobs into episodes. Safe to call from anywhere unsandboxed —
    the harnesses' Stop hooks, the nightly, the app, the CLI. A job is removed
    only once its episode is captured; anything else leaves it queued."""
    reclaimed = _reclaim_stale()
    jobs = queued_jobs()
    if limit is not None:
        jobs = jobs[:limit]
    out = {"jobs": len(jobs), "captured": 0, "empty": 0, "failed": 0, "skipped_claimed": 0}
    if reclaimed:
        out["reclaimed_stale"] = reclaimed
    if not jobs:
        return out
    from khipu.capture import capture
    from khipu.config import capture_mode
    from khipu.extract import extract_memory

    mode = capture_mode()
    for original in jobs:
        jp = _claim(original)
        if jp is None:                       # another drain got it first
            out["skipped_claimed"] += 1
            continue
        try:
            job = json.loads(jp.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            out["failed"] += 1
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
        from khipu.extract import extract_verbatim

        verbatim = extract_verbatim(job.get("transcript", ""))
        try:
            payload = extract_memory(job.get("transcript", ""), cwd=job.get("cwd", ""))
        except Exception as e:  # noqa: BLE001 — model/transport: keep the job, retry later
            out["failed"] += 1
            _release(jp)
            _record_drain(harness, captured=False, error=f"extract: {type(e).__name__}: {e}")
            _log(f"drain: extraction failed for {original.name}, kept: {e}")
            continue
        if payload is None:
            out["empty"] += 1
            jp.unlink(missing_ok=True)
            _record_drain(harness, captured=False, empty=True)
            _log(f"drain: nothing durable in {original.name}; dropped")
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
        payload["parent_session_id"] = job.get("parent_session_id")
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
        # K3: 0 unless the part-count ceiling forced a lossy merge.
        payload["truncated_chars"] = int(job.get("truncated_chars") or 0)
        if job.get("window_id"):
            payload["window_id"] = job["window_id"]
        if dry_run:
            print(json.dumps(payload, indent=2))
            out["captured"] += 1
            _release(jp)
            continue
        rc = capture(payload, mode=mode)
        if rc != 0:
            out["failed"] += 1
            _release(jp)
            _record_drain(harness, captured=False, error=f"capture rc={rc}")
            _log(f"drain: capture rc={rc} for {original.name}; kept")
            continue
        jp.unlink(missing_ok=True)
        out["captured"] += 1
        _record_drain(harness, captured=True)
        _log(f"drain: captured {original.name} -> {payload['summary'][:90]!r}")
    return out


# ---- status + liveness -------------------------------------------------------------

def _age(ts: Any) -> int | None:
    t = _parse_ts(ts)
    return int(time.time() - t) if t is not None else None


def _queue_by_harness() -> dict[str, dict]:
    out: dict[str, dict] = {}
    now = time.time()
    for p in queued_jobs():
        parts = p.name.split("-")
        h = parts[1] if len(parts) > 2 else "aegis"
        try:
            age = int(now - p.stat().st_mtime)
        except OSError:
            age = 0
        d = out.setdefault(h, {"depth": 0, "oldest_age_s": 0, "oldest_job": None})
        d["depth"] += 1
        if age >= d["oldest_age_s"]:
            d["oldest_age_s"], d["oldest_job"] = age, p.name
    return out


def status(harness: str = "aegis") -> dict:
    """Single-harness status (queue + last dispatch), as the Aegis pane used it."""
    q = _queue_by_harness().get(harness, {"depth": 0, "oldest_age_s": None, "oldest_job": None})
    ld = last_dispatch(harness)
    return {"home": str(khipu_home()), "harness": harness,
            "queue_depth": q["depth"], "oldest_job": q["oldest_job"],
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
    try:
        files = list(state_dir().glob(f"{_safe(harness)}--*.json"))
    except OSError:
        return None, None
    if len(files) > ACTIVITY_SCAN_LIMIT:
        try:
            files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        except OSError:
            pass
        files = files[:ACTIVITY_SCAN_LIMIT]
    worst: tuple[int, str, int] | None = None
    newer_s: int | None = None
    now = time.time()
    for f in files:
        try:
            st = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
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
    q = _queue_by_harness().get(harness, {"depth": 0, "oldest_age_s": None, "oldest_job": None})
    reasons: list[str] = []
    if not beat:
        return {"harness": harness, "ok": True, "seen": False, "reasons": [],
                "queue_depth": 0, "note": "no real session has run the hook yet"}
    err_at = _parse_ts(beat.get("last_error_at")) or 0
    if beat.get("last_error") and err_at >= (_parse_ts(beat.get("last_queued_at")) or 0):
        reasons.append(f"hook error on last run: {str(beat['last_error'])[:120]}")
    if beat.get("last_drain_error") and \
            (_parse_ts(beat.get("last_drain_error_at")) or 0) > (_parse_ts(beat.get("last_captured_at")) or 0):
        reasons.append(f"last capture attempt failed: {str(beat['last_drain_error'])[:120]}")
    if q["depth"] and (q.get("oldest_age_s") or 0) > QUEUE_STALE_S:
        reasons.append(f"{q['depth']} job(s) waiting {q['oldest_age_s'] // 60} min — nothing is draining them")
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
    if hook_ran_recently and (
        pend >= STUCK_TURNS or (pend >= 1 and since is not None and since >= STUCK_MINUTES * 60)
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
    # K7: a SessionEnd/Stop that could not find a readable transcript at all —
    # a silent zero-capture otherwise. Red only while it's recent (24h); the
    # accumulated count is a lifetime total, so a one-off months ago must not
    # keep a harness red forever.
    missing = int(beat.get("transcript_missing") or 0)
    missing_age = _age(beat.get("last_transcript_missing_at"))
    if missing and missing_age is not None and missing_age <= 24 * 3600:
        reasons.append(
            f"{missing} session(s) ended without a readable transcript in the last 24 h — "
            "the harness deleted or never wrote the transcript; check its transcript setting"
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
            "queue_depth": sum(v.get("queue_depth", 0) for v in per.values())}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--drain":
        dry = (os.environ.get("KHIPU_CAPTURE_DRY") or os.environ.get("KHIPU_AEGIS_DRY")) == "1"
        print(json.dumps(drain(dry_run=dry), indent=2))
        return 0
    if argv and argv[0] == "--status":
        print(json.dumps(liveness_all(), indent=2))
        return 0
    harness = argv[1] if len(argv) >= 2 and argv[0] == "--harness" else None
    out = hook_main(sys.stdin.read(), harness)
    if os.environ.get("KHIPU_CAPTURE_PROBE") == "1" or os.environ.get("KHIPU_AEGIS_PROBE") == "1":
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        _log(f"fatal: {type(e).__name__}: {e}")
        sys.exit(0)
