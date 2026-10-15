"""The routing push at the moment of delegation (routing truth, phase 6).

Two hooks, both behind ``routing_push`` and ``routing_push_delegation``:

- PreToolUse, matched only to delegation calls: the Agent tool (``Task`` in
  older Claude Code), ``delegate_task`` (bare or as an MCP tool), and shell
  commands that start ``claude -p``, ``codex exec``, ``grok``, ``muse exec``
  or one of the owner's dispatch scripts (``routing_dispatch_scripts``, by
  file name). It sends what the per-prompt push would send at this moment:
  the same read (``recall_prompt._RoutingAtPrompt``), the same budget
  (``routing_prompt_budget_ms``, ``routing_prompt_chars``) and the same
  seen marker, moved only once the block was written. So a block one push
  delivered is never repeated by the other. A call inside a subagent (its
  payload carries ``agent_id``) gets nothing: the subagent heard the
  summary at its start, and the parent's marker is not the subagent's.
- SubagentStart (Claude Code): the full summary, for the subagent alone. The
  parent's marker does not move, since the parent never saw it.

Neither ever denies, asks or changes a call: the answer carries only
``additionalContext``, and the context reaches the model with the tool's
result, after the call has gone out (phase 0 results). Khipu reports; it
does not route. Inside Aegis both refuse, as Khipu's other hook scripts do.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from typing import Any

AGENT_TOOLS = frozenset({"Agent", "Task"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_WRAPPERS = frozenset({"env", "nohup", "exec", "time", "command", "caffeinate", "builtin"})
_INTERPRETER = re.compile(r"(?:python(?:\d+(?:\.\d+)*)?|node|ruby|perl|deno|bun|sh|bash|zsh)")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)
_OPERATOR_CHARS = frozenset(";&|()\n")
_DEPTH_MOST = 2


def _aegis(environ) -> bool:
    """Aegis's runner is calling: its hooks discard what a hook prints, and
    Khipu runs there only through Aegis's own pack."""
    return bool((environ.get("GROK_HOOK_NAME") or "") + (environ.get("GROK_HOOK_EVENT") or "")) \
        or environ.get("KHIPU_HARNESS") == "aegis"


def _tokens(command: str) -> list[str]:
    """Words and the shell's control operators (newline included), quotes
    honoured; plain whitespace splitting when the quoting does not close."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError:
        for op in (";", "&&", "||", "|", "\n"):
            command = command.replace(op, f" {op} ")
        return command.split(" ")


def _segments(command: str) -> list[list[str]]:
    out: list[list[str]] = []
    cur: list[str] = []
    for tok in _tokens(command):
        if tok and set(tok) <= _OPERATOR_CHARS:
            if cur:
                out.append(cur)
            cur = []
        elif tok:
            cur.append(tok)
    if cur:
        out.append(cur)
    return out


def _strip_prefix(argv: list[str]) -> list[str]:
    """``argv`` without leading ``VAR=value`` words and wrappers such as
    ``env``, ``nohup`` or ``timeout 600``, so ``CLAUDE_CONFIG_DIR=x claude -p``
    is seen as ``claude -p``."""
    i = 0
    while i < len(argv):
        word = argv[i]
        base = os.path.basename(word)
        if _ASSIGNMENT.fullmatch(word):
            i += 1
        elif base in _WRAPPERS:
            i += 1
            while i < len(argv) and argv[i].startswith("-"):
                i += 1
        elif base in ("timeout", "gtimeout"):
            i += 1
            while i < len(argv) and argv[i].startswith("-"):
                i += 1
            i += 1  # the duration
        else:
            break
    return argv[i:]


def _argv_kind(argv: list[str], scripts: frozenset[str], depth: int) -> str | None:
    argv = _strip_prefix(argv)
    if not argv:
        return None
    base, args = os.path.basename(argv[0]), argv[1:]
    if base in _SHELLS and depth < _DEPTH_MOST:
        for i, arg in enumerate(args[:-1]):
            if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
                return shell_kind(args[i + 1], scripts, depth + 1)
    if base == "claude" and ("-p" in args or "--print" in args):
        return "claude -p"
    if base == "codex" and "exec" in args:
        return "codex exec"
    if base == "grok":
        return "grok"
    if base == "muse" and "exec" in args:
        return "muse exec"
    if base in scripts:
        return "dispatch script"
    if scripts and _INTERPRETER.fullmatch(base):
        script = next((a for a in args if not a.startswith("-")), None)
        if script is not None and os.path.basename(script) in scripts:
            return "dispatch script"
    return None


def shell_kind(command: Any, scripts: frozenset[str] = frozenset(), depth: int = 0) -> str | None:
    """Which delegation a shell command starts ("claude -p", "codex exec",
    "grok", "muse exec" or "dispatch script"), or None. Each command of a
    list (``cd x && claude -p ...``) is looked at, and the script of a
    ``bash -lc`` is read in turn. A command given as argv (Codex's shell
    tool) is one command."""
    if isinstance(command, list) and all(isinstance(w, str) for w in command):
        return _argv_kind(list(command), scripts, depth)
    if not isinstance(command, str) or not command.strip():
        return None
    for argv in _segments(command):
        kind = _argv_kind(argv, scripts, depth)
        if kind:
            return kind
    return None


def _dispatch_scripts() -> frozenset[str]:
    try:
        from khipu import routing

        return frozenset(routing.settings()["routing_dispatch_scripts"])
    except Exception:  # noqa: BLE001 - an unreadable config: the built-in commands only
        return frozenset()


def delegation_kind(payload: dict, scripts: frozenset[str] | None = None) -> str | None:
    """What kind of delegation this PreToolUse call is, or None. ``scripts``
    defaults to the owner's ``routing_dispatch_scripts``, read only for a
    shell command that matches nothing built in."""
    name = payload.get("tool_name") or payload.get("toolName")
    if not isinstance(name, str):
        return None
    if name in AGENT_TOOLS:
        return "agent"
    if name == "delegate_task" or name.endswith("__delegate_task"):
        return "delegate_task"
    tool_input = payload.get("tool_input") or payload.get("toolInput")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if command is None:
        return None
    kind = shell_kind(command)
    if kind is None:
        found = _dispatch_scripts() if scripts is None else scripts
        if found:
            kind = shell_kind(command, found)
    return kind


def _payload(raw: str) -> dict:
    try:
        data = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _event(payload: dict) -> str:
    return str(payload.get("hook_event_name") or payload.get("hookEventName") or "")


def pre_tool_use(payload: dict, environ=None) -> tuple[dict, dict | None, str | None]:
    """(what to print, what to record once it is printed, the call's kind)
    for one PreToolUse call. Never a permission decision of any kind."""
    from khipu import recall_prompt

    if payload.get("agent_id") or payload.get("agentId"):
        return {}, None, None  # inside a subagent: it heard the summary at its start
    kind = delegation_kind(payload)
    if kind is None:
        return {}, None, None
    session_id = payload.get("session_id") or payload.get("sessionId")
    routing = recall_prompt._RoutingAtPrompt().begin(session_id, environ, moment="delegation")
    try:
        ctx, info = routing.add_to("", routing.collect())
    except Exception:  # noqa: BLE001 - routing never breaks the hook
        ctx, info = "", {"status": "skipped:error"}
    if not ctx:
        return {}, info, kind
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": ctx}}, info, kind


def subagent_start(payload: dict) -> dict:
    """The full summary for a Claude Code subagent at its start."""
    from khipu import recall_rule

    text = recall_rule._RoutingAtStart(moment="delegation").begin().text()
    if not text or recall_rule._utf16_len(text) > recall_rule._HOOK_CONTEXT_BUDGET:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": text}}


def hook_main(raw: str, environ=None) -> tuple[dict, dict | None, str | None]:
    """(what to print, the routing record to keep, the delegation's kind)."""
    env = os.environ if environ is None else environ
    if _aegis(env):
        return {}, None, None
    payload = _payload(raw)
    from khipu.t3 import is_helper_session

    if is_helper_session(payload.get("cwd")):
        return {}, None, None
    event = _event(payload)
    if event == "SubagentStart":
        return subagent_start(payload), None, None
    if event and event != "PreToolUse":
        return {}, None, None
    return pre_tool_use(payload, environ)


def main() -> None:
    """Entry point for ``bin/khipu-delegation-hook``. Never raises and never
    fails the call: any error prints ``{}``."""
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        raw = ""
    info, kind, out = None, None, {}
    try:
        out, info, kind = hook_main(raw)
    except Exception:  # noqa: BLE001 - the one thing this must never do is fail the call
        out = {}
    delivered = True
    try:
        print(json.dumps(out), flush=True)
    except OSError:
        delivered = False
    if kind is None:
        return
    try:
        from khipu import recall_prompt

        routed = recall_prompt._routing_delivered(info, delivered)
        session = _payload(raw).get("session_id") or "?"
        # Not the per-prompt summary line's shape, so the lane's outcome rate never counts it.
        recall_prompt._log(f"delegation session={session} call={kind.replace(' ', '_')}{routed}")
    except Exception:  # noqa: BLE001
        pass
