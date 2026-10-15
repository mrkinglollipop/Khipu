"""The routing push at the moment of delegation (routing truth, phase 6).

Two hooks, both behind ``routing_push`` and ``routing_push_delegation``:

- PreToolUse, matched only to delegation calls: the Agent tool (``Task`` in
  older Claude Code), Codex's ``spawn_agent``, ``delegate_task`` (bare or as
  an MCP tool), and shell
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
import time
from typing import Any

from khipu import wire

# Claude Code's subagent tool (``Task`` in older versions) and Codex's native
# one (``spawn_agent``).
AGENT_TOOLS = frozenset({"Agent", "Task", "spawn_agent"})
# Each shell tool and the field its command arrives in: Claude Code's Bash and
# Codex's shell_command take a string in ``command``, Codex's shell and
# local_shell an argv list in ``command``, Codex's exec_command a string in ``cmd``.
SHELL_TOOLS = {"Bash": "command", "shell": "command", "shell_command": "command", "local_shell": "command",
               "exec_command": "cmd"}
_BUILT_IN = ("claude", "codex", "grok", "muse")
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
# A wrapper's options that take an operand, short (attached or the next word)
# and long (``--x=v`` or the next word). After them, timeout takes a duration.
_WRAPPERS = {
    "env": ("uCPS", frozenset({"--unset", "--chdir", "--split-string"})),
    "timeout": ("sk", frozenset({"--signal", "--kill-after"})),
    "gtimeout": ("sk", frozenset({"--signal", "--kill-after"})),
    "nohup": ("", frozenset()),
    "exec": ("a", frozenset()),
    "time": ("of", frozenset({"--output", "--format"})),
    "command": ("", frozenset()),
    "builtin": ("", frozenset()),
    "caffeinate": ("tw", frozenset()),
}
_INTERPRETER = re.compile(r"(?:python(?:\d+(?:\.\d+)*)?|node|ruby|perl|deno|bun|sh|bash|zsh)")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)
_DEPTH_MOST = 2
# The hook runs before every matched tool call, so reading a command is
# bounded: only its first _SCAN_MOST characters, and only for _SCAN_BUDGET_S.
_SCAN_MOST = 200_000
_SCAN_BUDGET_S = 0.15
_CHECK_EVERY = 256

# One shell token. Operators and redirections come only from unquoted text,
# so a quoted ";" stays a word. A word keeps its quotes here; _unquote strips them.
_LEX = re.compile(r"""
    (?P<space>(?:[ \t\r]|\\\n)+)
  | (?P<op>&&|\|\||;;|\|&|[;&|()\n])
  | (?P<heredoc><<-?(?!<))
  | (?P<redir>[0-9]*(?:<<<|<>|>>|>&|<&|>\||[<>]))
  | (?P<word>(?:'[^']*'?|\$'(?:\\.|[^'\\])*'?|"(?:\\.|[^"\\])*"?|\\.|[^\s;&|()<>'"\\])+)
  | (?P<other>.)
""", re.X | re.S)
_PIECE = re.compile(r"""'([^']*)'?|\$'((?:\\.|[^'\\])*)'?|"((?:\\.|[^"\\])*)"?|\\(.)|([^'"\\$]+|\$)""", re.S)
_DQ_ESCAPE = re.compile(r'\\([\\"$`\n])')


class _OutOfTime(Exception):
    pass


def _aegis(environ) -> bool:
    """Aegis's runner is calling: its hooks discard what a hook prints, and
    Khipu runs there only through Aegis's own pack."""
    return bool((environ.get("GROK_HOOK_NAME") or "") + (environ.get("GROK_HOOK_EVENT") or "")) \
        or environ.get("KHIPU_HARNESS") == "aegis"


def _unquote(word: str) -> str:
    out = []
    for m in _PIECE.finditer(word):
        single, ansi, double, escaped, plain = m.groups()
        if single is not None:
            out.append(single)
        elif ansi is not None:
            out.append(re.sub(r"\\(.)", r"\1", ansi, flags=re.S))
        elif double is not None:
            out.append(_DQ_ESCAPE.sub(lambda e: "" if e.group(1) == "\n" else e.group(1), double))
        elif escaped is not None:
            out.append("" if escaped == "\n" else escaped)
        else:
            out.append(plain)
    return "".join(out)


def _segments(command: str, deadline: float) -> list[list[str]]:
    """The simple commands of a shell script, each as its words, unquoted.
    Comments, heredoc bodies and redirections (with their targets) are
    left out; operators split commands only where the shell would see them."""
    text = command[:_SCAN_MOST]
    out: list[list[str]] = []
    cur: list[str] = []
    heredocs: list[tuple[str, bool]] = []
    pending: str | None = None  # "redir" or "heredoc": the next word is its target
    dash = False
    pos, n, count = 0, len(text), 0
    while pos < n:
        count += 1
        if count % _CHECK_EVERY == 0 and time.monotonic() > deadline:
            raise _OutOfTime
        m = _LEX.match(text, pos)
        kind, tok = m.lastgroup, m.group()
        pos = m.end()
        if kind == "space" or kind == "other":
            continue
        if kind == "word":
            if pending == "heredoc":
                heredocs.append((_unquote(tok), dash))
            elif pending is None:
                if tok.startswith("#"):
                    nl = text.find("\n", pos)
                    pos = n if nl < 0 else nl
                    continue
                cur.append(_unquote(tok))
            pending = None
            continue
        pending = None
        if kind == "heredoc":
            pending, dash = "heredoc", tok == "<<-"
            continue
        if kind == "redir":
            pending = "redir"
            continue
        if cur:  # an operator
            out.append(cur)
            cur = []
        if tok == "\n":
            for delim, strip_tabs in heredocs:
                end = re.compile("^" + ("\t*" if strip_tabs else "") + re.escape(delim) + "$", re.M).search(text, pos)
                if end is None:
                    pos = n
                    break
                pos = end.end()
            heredocs = []
    if cur:
        out.append(cur)
    return out


def _skip_options(argv: list[str], i: int, short: str, long: frozenset[str]) -> int:
    """The index past a wrapper's options (and their operands) from ``i``."""
    while i < len(argv):
        word = argv[i]
        if word == "--":
            return i + 1
        if word.startswith("--"):
            i += 1
            if word in long:
                i += 1  # --signal KILL
        elif word.startswith("-") and len(word) > 1:
            i += 1
            for j, ch in enumerate(word[1:], 1):
                if ch in short:
                    if j == len(word) - 1:
                        i += 1  # -u FOO; -uFOO carries it attached
                    break
        else:
            return i
    return i


def _strip_prefix(argv: list[str]) -> list[str]:
    """``argv`` without leading ``VAR=value`` words and wrappers such as
    ``env -u FOO``, ``nohup`` or ``timeout -s KILL 600``, so
    ``CLAUDE_CONFIG_DIR=x claude -p`` is seen as ``claude -p``."""
    i = 0
    while i < len(argv):
        word = argv[i]
        base = os.path.basename(word)
        if _ASSIGNMENT.fullmatch(word):
            i += 1
        elif base in _WRAPPERS:
            short, long = _WRAPPERS[base]
            i = _skip_options(argv, i + 1, short, long)
            if base in ("timeout", "gtimeout"):
                i += 1  # the duration
        else:
            break
    return argv[i:]


def _prints(args: list[str]) -> bool:
    """``claude`` asked to print and exit: ``-p``, ``-p'prompt'``, ``--print``
    or ``--print=prompt``."""
    return any(a == "--print" or a.startswith("--print=") or (a.startswith("-p") and not a.startswith("--"))
               for a in args)


def _argv_kind(argv: list[str], scripts: frozenset[str], depth: int, deadline: float) -> str | None:
    argv = _strip_prefix(argv)
    if not argv:
        return None
    base, args = os.path.basename(argv[0]), argv[1:]
    if base in _SHELLS and depth < _DEPTH_MOST:
        for i, arg in enumerate(args[:-1]):
            if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
                return _shell_kind(args[i + 1], scripts, depth + 1, deadline)
    if base == "claude" and _prints(args):
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


def _shell_kind(command: Any, scripts: frozenset[str], depth: int, deadline: float) -> str | None:
    if isinstance(command, list) and all(isinstance(w, str) for w in command):
        return _argv_kind(list(command), scripts, depth, deadline)
    if not isinstance(command, str) or not command.strip():
        return None
    for argv in _segments(command, deadline):
        kind = _argv_kind(argv, scripts, depth, deadline)
        if kind:
            return kind
    return None


def shell_kind(command: Any, scripts: frozenset[str] = frozenset(), depth: int = 0,
               deadline: float | None = None) -> str | None:
    """Which delegation a shell command starts ("claude -p", "codex exec",
    "grok", "muse exec" or "dispatch script"), or None. Each command of a
    list (``cd x && claude -p ...``) is looked at, and the script of a
    ``bash -lc`` is read in turn. A command given as argv (Codex's shell
    tool) is one command. A command that takes longer than the scan budget
    to read counts as none."""
    try:
        return _shell_kind(command, scripts, depth,
                           time.monotonic() + _SCAN_BUDGET_S if deadline is None else deadline)
    except _OutOfTime:
        return None


def _mentions(command: Any, names) -> bool:
    """A cheap test that can only say no: whether any of ``names`` appears
    anywhere in the command's text."""
    if isinstance(command, list):
        command = " ".join(w for w in command if isinstance(w, str))
    if not isinstance(command, str):
        return False
    head = command[:_SCAN_MOST]
    return any(name in head for name in names)


def _configured_script_names() -> list[str]:
    """The owner's dispatch scripts as stored, unchecked: only a cheap
    prefilter, so routing (and its checks) is not imported for a command
    that names none of them."""
    try:
        from khipu.config import load_config

        raw = load_config().get("routing_dispatch_scripts")
    except Exception:  # noqa: BLE001
        return []
    if isinstance(raw, str):
        raw = raw.split(",")
    return [n.strip() for n in raw if isinstance(n, str) and n.strip()] if isinstance(raw, list) else []


def _dispatch_scripts() -> frozenset[str]:
    try:
        from khipu import routing

        return frozenset(routing.settings()["routing_dispatch_scripts"])
    except Exception:  # noqa: BLE001 - an unreadable config: the built-in commands only
        return frozenset()


def _tool_name(payload: dict) -> str | None:
    name = payload.get("tool_name") or payload.get("toolName")
    return name if isinstance(name, str) else None


def _command(payload: dict, name: str) -> Any:
    tool_input = payload.get("tool_input") or payload.get("toolInput")
    return tool_input.get(SHELL_TOOLS[name]) if isinstance(tool_input, dict) else None


def might_delegate(payload: dict) -> bool:
    """Whether this call's tool can hand work on at all, from its name
    alone: no file, setting or module is read."""
    name = _tool_name(payload)
    return name is not None and (name in AGENT_TOOLS or name in SHELL_TOOLS or name == "delegate_task"
                                 or name.endswith("__delegate_task"))


def delegation_kind(payload: dict, scripts: frozenset[str] | None = None,
                    deadline: float | None = None) -> str | None:
    """What kind of delegation this PreToolUse call is, or None. ``scripts``
    defaults to the owner's ``routing_dispatch_scripts``, read only for a
    shell command that matches nothing built in and names one of them."""
    name = _tool_name(payload)
    if name is None:
        return None
    if name in AGENT_TOOLS:
        return "agent"
    if name == "delegate_task" or name.endswith("__delegate_task"):
        return "delegate_task"
    if name not in SHELL_TOOLS:
        return None
    command = _command(payload, name)
    if command is None:
        return None
    deadline = time.monotonic() + _SCAN_BUDGET_S if deadline is None else deadline
    kind = shell_kind(command, deadline=deadline) if _mentions(command, _BUILT_IN) else None
    if kind is None:
        if scripts is None:
            names = _configured_script_names()
            found = _dispatch_scripts() if names and _mentions(command, names) else frozenset()
        else:
            found = scripts if _mentions(command, scripts) else frozenset()
        if found:
            kind = shell_kind(command, found, deadline=deadline)
    return kind


def _payload(raw: str) -> dict:
    try:
        data = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _event(payload: dict) -> str:
    return str(payload.get("hook_event_name") or payload.get("hookEventName") or "")


def _switched_off(environ) -> bool:
    """The push's cheap switches, read before anything else: ``ROUTING_NOW_OFF``
    and the ``routing_push`` feature (off by default). The rest (the bypass
    file, ``routing_push_delegation``) are decided with the read itself."""
    from khipu import features

    env = os.environ if environ is None else environ
    if features.parse_bool(env.get("ROUTING_NOW_OFF") or "") is True:
        return True
    return not features.enabled("routing_push", environ=env)


def _helper(payload: dict) -> bool:
    from khipu.t3 import is_helper_session

    return is_helper_session(payload.get("cwd"))


def pre_tool_use(payload: dict, environ=None) -> tuple[dict, dict | None, str | None]:
    """(what to print, what to record once it is printed, the call's kind)
    for one PreToolUse call. Never a permission decision of any kind. Most
    tool calls end at the first test, which reads nothing."""
    if payload.get("agent_id") or payload.get("agentId"):
        return {}, None, None  # inside a subagent: it heard the summary at its start
    if not might_delegate(payload) or _switched_off(environ):
        return {}, None, None
    kind = delegation_kind(payload)
    if kind is None or _helper(payload):
        return {}, None, None
    from khipu import recall_prompt

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
    event = _event(payload)
    if event == "SubagentStart":
        return ({} if _helper(payload) else subagent_start(payload)), None, None
    if event and event != "PreToolUse":
        return {}, None, None
    return pre_tool_use(payload, environ)


def main() -> None:
    """Entry point for ``bin/khipu-delegation-hook``. Never raises and never
    fails the call: any error prints ``{}``."""
    try:
        raw = wire.read_stdin()
    except Exception:  # noqa: BLE001
        raw = ""
    info, kind, out = None, None, {}
    try:
        out, info, kind = hook_main(raw)
    except Exception:  # noqa: BLE001 - the one thing this must never do is fail the call
        out = {}
    delivered = True
    try:
        print(wire.dumps(out, ensure_ascii=True), flush=True)
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
