"""Which harness is calling a hook, and how it wants its answer shaped.

One place decides this for every hook (capture, session-start recall, per-prompt
recall, routing at delegation, the owner-turn recorder, the stdlib recall
client), so the hook command can be the same string in every pack
(docs/plans/2026-10-27-cursor-antigravity-hooks.md section 5.1).

Stdlib only, and nothing here imports the rest of the package at import time,
so ``bin/khipu-recall-client.py`` can load this file by path.

The rule: the harness's OWN signals win over anything it merely inherited.
Order, strongest first:

1. ``KHIPU_HARNESS`` (the pack's explicit mark), then the Grok runner variables.
2. The payload: Cursor's ``cursor_version`` (or ``conversation_id`` and
   ``workspace_roots``), Antigravity's camelCase ``conversationId`` with
   ``workspacePaths`` or ``artifactDirectoryPath``.
3. The transcript path (``/.cursor/``, a Claude home, ``/.codex/``,
   an Antigravity app data dir).
4. A running session's own environment: Codex's ``CODEX_THREAD_ID`` (a Codex
   job started from a Claude session inherits ``CLAUDECODE`` but has its own
   thread), then Claude Code's ``CLAUDECODE`` / ``CLAUDE_CODE_ENTRYPOINT`` /
   ``CLAUDE_CODE_SESSION_ID``.
5. Configuration that is merely inherited or set globally, weakest last:
   ``CODEX_HOME`` / ``CODEX_SANDBOX``, then ``CLAUDE_PROJECT_DIR``, then
   ``CURSOR_VERSION``. Many shells export ``CODEX_HOME`` for good, and Cursor
   exports ``CURSOR_VERSION`` to every hook, the Claude ones it imports
   included: neither may turn a running Claude Code turn into another
   harness, or a Claude answer into Cursor's flat shape (Claude Code drops
   ``additional_context``). A real Cursor hook names itself in its payload.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from typing import Any

CLAUDE = "claude"
CURSOR = "cursor"
ANTIGRAVITY = "antigravity"
SHAPES = (CLAUDE, CURSOR, ANTIGRAVITY)

# What a hook's added context may hold, per output shape. Claude Code keeps an
# additionalContext whole up to 10,000 characters counted as JavaScript counts
# them (UTF-16 units); Cursor's cap is the same 10,000 characters (its SDK is
# JavaScript too), so all three are held to UTF-16 units. Antigravity's real
# limit is still to be measured (slice H); it starts at the same figure.
_CONTEXT_MOST = 10_000
BUDGETS = {CLAUDE: _CONTEXT_MOST - 200, CURSOR: _CONTEXT_MOST - 200, ANTIGRAVITY: _CONTEXT_MOST - 200}

_CODEX_CONFIG_ENV = ("CODEX_HOME", "CODEX_SANDBOX")
_CLAUDE_RUNNING_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID")
# The environment detect() reads, which the recall client forwards with each
# request so the recall service judges the hook's identity, not its own.
IDENTITY_ENV = ("KHIPU_HARNESS", "GROK_HOOK_NAME", "GROK_HOOK_EVENT", "CODEX_THREAD_ID", *_CLAUDE_RUNNING_ENV,
                *_CODEX_CONFIG_ENV, "CLAUDE_PROJECT_DIR", "CURSOR_VERSION",
                "CURSOR_PROJECT_DIR")


def budget(shape: str) -> int:
    return BUDGETS.get(shape, BUDGETS[CLAUDE])


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _first(payload: dict, *names: str) -> Any:
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return value
    return None


def session_id(payload: dict) -> str:
    """The native session id: ``sessionId`` / ``session_id`` (Claude, Codex, and
    Cursor, whose executor sets it to the conversation id), ``conversation_id``
    (Cursor, older) or ``conversationId`` (Antigravity). Empty when none."""
    if not isinstance(payload, dict):
        return ""
    value = _first(payload, "sessionId", "session_id", "conversation_id", "conversationId")
    return "" if value is None else str(value)


def workspace(payload: dict) -> str:
    """The session's folder: ``cwd``, else the first Cursor ``workspace_roots``
    or Antigravity ``workspacePaths`` entry. Empty when none."""
    if not isinstance(payload, dict):
        return ""
    cwd = _first(payload, "cwd", "cwd_path")
    if isinstance(cwd, str):
        return cwd
    for key in ("workspace_roots", "workspacePaths"):
        roots = payload.get(key)
        if isinstance(roots, list) and roots and isinstance(roots[0], str) and roots[0].strip():
            return roots[0]
    return ""


def transcript_path(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    value = _first(payload, "transcriptPath", "transcript_path")
    return value if isinstance(value, str) else ""


def _is_cursor(payload: dict, tp: str) -> bool:
    return ("cursor_version" in payload or "conversation_id" in payload
            or "workspace_roots" in payload or "/.cursor/" in tp)


def _is_antigravity(payload: dict, tp: str) -> bool:
    if "conversationId" in payload and ("workspacePaths" in payload or "artifactDirectoryPath" in payload):
        return True
    return "/.gemini/antigravity" in tp or "/antigravity-acp/" in tp


def _in_claude_home(tp: str, env: Any) -> bool:
    """A transcript under ``<home>/projects/`` of any Claude home Khipu finds
    (CLAUDE_CONFIG_DIR, T3's accounts): ``/.claude`` only names the default.
    Needs the package; without it (the stdlib client) the answer is no."""
    if not tp:
        return False
    try:
        from khipu import claude_homes

        return claude_homes.transcript_home(tp, claude_homes.discover(environ=env)) is not None
    except Exception:  # noqa: BLE001 - inference is best-effort, never fatal
        return False


def shape_of_harness(harness: str) -> str:
    return harness if harness in (CURSOR, ANTIGRAVITY) else CLAUDE


def detect(payload: dict, env: Any = None) -> tuple[str, str]:
    """(harness, output shape) for one hook call. The harness is a capture
    harness name (``claude_code``, ``codex``, ``cursor``, ``antigravity``,
    ``aegis``, ...) or ``unknown``; the shape is ``claude``, ``cursor`` or
    ``antigravity``. Never raises."""
    env = os.environ if env is None else env
    payload = payload if isinstance(payload, dict) else {}
    named = str(env.get("KHIPU_HARNESS") or "").strip().lower()
    if named:
        return named, shape_of_harness(named)
    if env.get("GROK_HOOK_NAME") or env.get("GROK_HOOK_EVENT"):
        return "aegis", CLAUDE
    tp = transcript_path(payload)
    if _is_cursor(payload, tp):
        return CURSOR, CURSOR
    if _is_antigravity(payload, tp):
        return ANTIGRAVITY, ANTIGRAVITY
    # Path evidence before env-only markers: a Claude session started inside
    # Codex still writes under a Claude home, and a Codex job started inside
    # Claude still writes under /.codex/.
    if "/.claude" in tp:
        return "claude_code", CLAUDE
    if "/.codex/" in tp:
        return "codex", CLAUDE
    if _in_claude_home(tp, env):
        return "claude_code", CLAUDE
    if env.get("CODEX_THREAD_ID"):
        return "codex", CLAUDE
    if any(env.get(k) for k in _CLAUDE_RUNNING_ENV):
        return "claude_code", CLAUDE
    if any(env.get(k) for k in _CODEX_CONFIG_ENV):
        return "codex", CLAUDE
    if env.get("CLAUDE_PROJECT_DIR"):
        return "claude_code", CLAUDE
    if env.get("CURSOR_VERSION"):
        # Cursor's env with no Cursor payload key: a hook Cursor runs from the
        # Claude settings it imports, which answers in Claude's shape.
        return CURSOR, CLAUDE
    return "unknown", CLAUDE


def resolve_shape(payload: dict, env: Any = None, argv: tuple | list = ()) -> str:
    """The output shape. The payload decides; an older install's ``--cursor``
    argument only matters when the payload names no Cursor or Antigravity."""
    shape = detect(payload, env)[1]
    if shape == CLAUDE and "--cursor" in argv:
        return CURSOR
    return shape


# ---- entrypoints (owner direction) -----------------------------------------

_DATE_SHAPED = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}")


def cursor_entrypoint(payload: dict) -> str:
    """``cli`` (cursor-agent, whose version is date-shaped, 2026.10.22-...),
    ``ide`` (3.x), else ``unknown``. T3 is the caller's call (a thread lookup)."""
    version = payload.get("cursor_version") if isinstance(payload, dict) else None
    if not isinstance(version, str) or not version.strip():
        return "unknown"
    return "cli" if _DATE_SHAPED.match(version.strip()) else "ide"


def antigravity_entrypoint(payload: dict) -> str:
    """The surface, from the transcript's app data dir: ``antigravity-acp`` is
    T3's managed runtime, ``antigravity-cli`` the CLI, ``antigravity-ide`` the
    IDE, ``antigravity`` the App. ``unknown`` when the path says nothing."""
    tp = transcript_path(payload)
    if "/antigravity-acp/" in tp:
        return "t3"
    if "/antigravity-cli/" in tp:
        return "cli"
    if "/antigravity-ide/" in tp:
        return "ide"
    if "/.gemini/antigravity/" in tp:
        return "app"
    return "unknown"


# ---- output ------------------------------------------------------------------


def render(shape: str, event: str, context: str) -> dict:
    """One hook's added context in the harness's native envelope. ``event`` is
    the Claude-style event name (``SessionStart``, ``UserPromptSubmit``,
    ``PreToolUse``, ``SubagentStart``). Antigravity's ``injectSteps`` form is
    provisional until slice H confirms what the model sees: a system message at
    session start, an ephemeral message otherwise."""
    if not context:
        return {}
    if shape == CURSOR:
        return {"additional_context": context}
    if shape == ANTIGRAVITY:
        key = "systemMessage" if event == "SessionStart" else "ephemeralMessage"
        return {"injectSteps": [{key: context}]}
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}


def reshape(out: dict, shape: str) -> dict:
    """A Claude-shaped hook answer in ``shape``. Claude, and any answer that
    carries no added context (``{}``, a decision), is returned as it is."""
    if shape == CLAUDE or not isinstance(out, dict):
        return out
    inner = out.get("hookSpecificOutput")
    if not isinstance(inner, dict):
        return out
    context = inner.get("additionalContext")
    if not isinstance(context, str) or not context:
        return {}
    return render(shape, str(inner.get("hookEventName") or ""), context)


# ---- single flight -----------------------------------------------------------

SINGLE_FLIGHT_S = 60.0
# Cursor starts its own entry and the Claude entry it imports together (all
# matching hooks run at once), so the pair window is short: two real sessions
# opening in one folder seconds apart are both served.
CURSOR_PAIR_S = 15.0


def single_flight(payload: dict, env: Any, event: str, discriminator: str = "") -> bool:
    """Whether this run should answer, or is the second of a duplicate pair.

    Inside a Cursor hook (``CURSOR_VERSION`` set by Cursor's executor, and no
    running Claude Code session, which sets ``CLAUDECODE``), Cursor's own entry
    and the Claude entry it imports carry different payloads and session ids
    (Cursor's conversation id, Claude's uuid), so the pair is keyed on what
    both share: Cursor's project folder, the event and ``discriminator`` (a
    prompt's hash). Elsewhere a Cursor- or Antigravity-shaped call is keyed on
    its own session; Claude and Codex are never held. Fail-open."""
    env = os.environ if env is None else env
    payload = payload if isinstance(payload, dict) else {}
    if env.get("CURSOR_VERSION") and not any(env.get(k) for k in _CLAUDE_RUNNING_ENV):
        folder = str(env.get("CURSOR_PROJECT_DIR") or workspace(payload) or "")
        if folder:
            return claim("cursor-pair", folder, event, discriminator, ttl=CURSOR_PAIR_S)
    harness, shape = detect(payload, env)
    session = session_id(payload)
    if shape == CLAUDE or not session:
        return True
    return claim(harness, session, event, discriminator)


def claim(harness: str, session: str, event: str, discriminator: str = "", *,
          directory: Any = None, ttl: float = SINGLE_FLIGHT_S, now: float | None = None) -> bool:
    """Whether this run owns the (harness, session, event[, discriminator]) slot.
    Cursor runs both its own entry and an imported Claude entry for one event;
    the second run to ask within ``ttl`` seconds gets False and prints ``{}``.
    ``discriminator`` separates legitimately distinct calls (a prompt's hash, a
    start source). Fail-open: any file error is True, so a hook is never lost."""
    now = time.time() if now is None else now
    try:
        if directory is None:
            from khipu import session_capture

            directory = session_capture.khipu_home() / "single-flight"
        directory = str(directory)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        key = "\0".join((harness, session, event, discriminator))
        path = os.path.join(directory, hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:40])
        for _ in range(2):
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                try:
                    age = now - os.stat(path).st_mtime
                except OSError:
                    continue  # gone between the two calls: try to take it
                if age < ttl:
                    return False
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            os.close(fd)
            try:
                os.utime(path, (now, now))
            except OSError:
                pass
            _sweep(directory, now, ttl)
            return True
        return True
    except Exception:  # noqa: BLE001 - never lose a hook over its own marker
        return True


def _sweep(directory: str, now: float, ttl: float) -> None:
    """Drop markers long past their window, so the folder never grows."""
    try:
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            try:
                if now - os.stat(path).st_mtime > ttl * 10:
                    os.unlink(path)
            except OSError:
                continue
    except OSError:
        pass
