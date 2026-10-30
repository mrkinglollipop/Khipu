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
  payload carries ``agent_id``) is not a delegation: it gets the next
  deferred page from the subagent's own ledger (slice 8a), and the
  parent's marker is not the subagent's.
- SubagentStart: the one bounded child block
  (``routing_delivery.child_offer``, slice 8a), for the child alone. The
  parent's marker does not move, since the parent never saw it. A Claude
  Code child keeps what the block leaves out owed in its own
  ``agent:<agent_id>`` ledger, arriving as pages on its own tool results
  (its PreToolUse, below). A Codex native child records the same block
  as a developer message before its first call; it is its own
  conversation, so nothing is reserved in the parent's ledger and its
  deferred routing reaches it only on its own Khipu tool calls, under
  its own ``CODEX_THREAD_ID`` identity (slice 7 pull delivery). When
  Codex's SubagentStart hook is absent and a ``spawn_agent`` brief
  carries no routing, the parent's PreToolUse result carries one
  advisory line naming ``khipu route --brief``; nothing is added to the
  brief itself. A T3 ``delegate_task`` child whose provider has no hooks
  gets the same advisory line by its own check (slice 8c).

Neither ever denies, asks or changes a call: the answer carries only
``additionalContext``, and the context reaches the model with the tool's
result, after the call has gone out (phase 0 results). Khipu reports; it
does not route. Inside Aegis both refuse, as Khipu's other hook scripts do.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import shlex
import time
from typing import Any

from khipu import hook_shape, wire

# Claude Code's subagent tool (``Task`` in older versions) and Codex's native
# one (``spawn_agent``).
AGENT_TOOLS = frozenset({"Agent", "Task", "spawn_agent"})
# Each shell tool and the field its command arrives in: Cursor's Shell and Claude Code's Bash and
# Codex's shell_command take a string in ``command``, Codex's shell and
# local_shell an argv list in ``command``, Codex's exec_command a string in ``cmd``.
SHELL_TOOLS = {"Bash": "command", "shell": "command", "shell_command": "command", "local_shell": "command",
               "exec_command": "cmd", "Shell": "command"}
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
# An argv command (Codex's shell tool) is read for its first _ARGV_MOST words.
_ARGV_MOST = 4096

# One shell token. Operators and redirections come only from unquoted text,
# so a quoted ";" stays a word. A word keeps its quotes here; _unquote strips them.
_LEX = re.compile(r"""
    (?P<space>(?:[ \t\r]|\\\n)+)
  | (?P<op>&&|\|\||;;|\|&|[;&|()\n])
  | (?P<heredoc>[0-9]*<<-?(?!<))
  | (?P<redir>[0-9]*(?:<<<|<>|>>|>&|<&|>\||[<>]))
  | (?P<word>(?:'[^']*'?|\$'(?:\\.|[^'\\])*'?|"(?:\\.|[^"\\])*"?|\\.|[^\s;&|()<>'"\\])+)
  | (?P<other>.)
""", re.X | re.S)
_PIECE = re.compile(r"""'([^']*)'?|\$'((?:\\.|[^'\\])*)'?|"((?:\\.|[^"\\])*)"?|\\(.)|([^'"\\$]+|\$)""", re.S)
_DQ_ESCAPE = re.compile(r'\\([\\"$`\n])')
# Quote and escape characters, which the shell removes from a word.
_QUOTING = re.compile(r"""['"\\]""")


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
    left out; operators split commands only where the shell would see them.
    A heredoc whose delimiter is unquoted still runs its ``$(...)``, so those
    are read."""
    return _scan(command[:_SCAN_MOST], 0, deadline, False, 0)[0]


def _scan(text: str, pos: int, deadline: float, nested: bool, depth: int) -> tuple[list[list[str]], int]:
    """``_segments`` from ``pos``. ``nested``: the inside of a ``$(``, which
    ends at its unmatched ``)``; returns the position past it."""
    out: list[list[str]] = []
    cur: list[str] = []
    heredocs: list[tuple[str, bool, bool]] = []  # (delimiter, strips tabs, expands)
    pending: str | None = None  # "redir" or "heredoc": the next word is its target
    dash = False
    parens = 0
    n, count = len(text), 0
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
                heredocs.append((_unquote(tok), dash, _QUOTING.search(tok) is None))
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
            pending, dash = "heredoc", tok.endswith("<<-")
            continue
        if kind == "redir":
            pending = "redir"
            continue
        if nested and tok == ")" and parens == 0:
            if cur:
                out.append(cur)
            return out, pos
        if nested and tok in ("(", ")"):
            parens += 1 if tok == "(" else -1
        if cur:  # an operator
            out.append(cur)
            cur = []
        if tok == "\n":
            for delim, strip_tabs, expands in heredocs:
                end = re.compile("^" + ("\t*" if strip_tabs else "") + re.escape(delim) + "$", re.M).search(text, pos)
                if expands and depth < _DEPTH_MOST:
                    out.extend(_substitutions(text[pos:n if end is None else end.start()], deadline, depth + 1))
                if end is None:
                    pos = n
                    break
                pos = end.end()
            heredocs = []
    if cur:
        out.append(cur)
    return out, pos


def _substitutions(body: str, deadline: float, depth: int) -> list[list[str]]:
    """The commands of each ``$(...)`` in an expandable heredoc body; the
    rest of the body is data. An escaped ``\\$(`` and arithmetic ``$((``
    run nothing."""
    out: list[list[str]] = []
    i = body.find("$(")
    while i >= 0:
        if time.monotonic() > deadline:
            raise _OutOfTime
        j = i
        while j > 0 and body[j - 1] == "\\":
            j -= 1
        if (i - j) % 2 or body.startswith("$((", i):
            i = body.find("$(", i + 2)
            continue
        found, end = _scan(body, i + 2, deadline, True, depth)
        out.extend(found)
        i = body.find("$(", end)
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


def _strip_prefix(argv: list[str], deadline: float) -> list[str]:
    """``argv`` without leading ``VAR=value`` words and wrappers such as
    ``env -u FOO``, ``nohup`` or ``timeout -s KILL 600``, so
    ``CLAUDE_CONFIG_DIR=x claude -p`` is seen as ``claude -p``."""
    i = 0
    while i < len(argv):
        if i % _CHECK_EVERY == 0 and time.monotonic() > deadline:
            raise _OutOfTime
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
    argv = _strip_prefix(argv, deadline)
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


def _bounded_argv(command: list, deadline: float) -> list[str] | None:
    """An argv command's first _ARGV_MOST words, cut at _SCAN_MOST
    characters in all, or None if one of them is not a string."""
    if time.monotonic() > deadline:
        raise _OutOfTime
    words = list(itertools.islice(command, _ARGV_MOST))
    if not all(isinstance(word, str) for word in words):
        return None
    out: list[str] = []
    size = 0
    for word in words:
        out.append(word[:_SCAN_MOST - size])
        size += len(word) + 1
        if size >= _SCAN_MOST:
            break
    return out


def _shell_kind(command: Any, scripts: frozenset[str], depth: int, deadline: float) -> str | None:
    if time.monotonic() > deadline:
        raise _OutOfTime
    if isinstance(command, list):
        argv = _bounded_argv(command, deadline)
        return None if argv is None else _argv_kind(argv, scripts, depth, deadline)
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


def _mentions(command: Any, names, deadline: float) -> bool:
    """A cheap test that can only say no: whether any of ``names`` appears
    in the command's first _SCAN_MOST characters, as written or with its
    quotes and escapes removed."""
    if isinstance(command, list):
        words = _bounded_argv(command, deadline)
        if words is None:
            return False
        command = " ".join(words)
    if not isinstance(command, str):
        return False
    head = command[:_SCAN_MOST]
    if any(name in head for name in names):
        return True
    # The shell runs co'dex' or c\odex as codex, so look again without the
    # quotes and escapes (str.replace: a long command costs next to nothing).
    bare = head.replace("\\\n", "").replace("\\", "").replace("'", "").replace('"', "")
    return len(bare) < len(head) and any(name in bare for name in names)


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
    try:
        if isinstance(command, list):
            command = _bounded_argv(command, deadline)
        kind = _shell_kind(command, frozenset(), 0, deadline) if _mentions(command, _BUILT_IN, deadline) else None
        if kind is None:
            if scripts is None:
                names = _configured_script_names()
                found = _dispatch_scripts() if names and _mentions(command, names, deadline) else frozenset()
            else:
                found = scripts if _mentions(command, scripts, deadline) else frozenset()
            if found:
                kind = _shell_kind(command, found, 0, deadline)
    except _OutOfTime:
        return None
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


def provider_advisory(payload: dict) -> list[str]:
    """Read model/provider facts for this dispatch; return context only."""
    from khipu import routing
    from khipu.routing_providers import advisory
    from khipu.routing_sources import model_family, safe_name

    name = _tool_name(payload)
    data = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(data, dict):
        return []
    specs = []
    if name in AGENT_TOOLS or name == "delegate_task" or str(name).endswith("__delegate_task"):
        specs.append({k: data[k] for k in ("provider", "model", "account", "budget_account") if k in data})
    elif name in SHELL_TOOLS:
        command = _command(payload, name)
        deadline = time.monotonic() + _SCAN_BUDGET_S
        try:
            argvs = [_bounded_argv(command, deadline) or []] if isinstance(command, list) else _segments(command, deadline)
            for argv in argvs:
                assignments = dict(w.split("=", 1) for w in argv if _ASSIGNMENT.fullmatch(w))
                argv = _strip_prefix(argv, deadline)
                if not argv:
                    continue
                base = os.path.basename(argv[0])
                if base not in {"claude", "codex", "grok", "muse", "cursor-agent"}:
                    continue
                model = None
                for i, word in enumerate(argv[1:], 1):
                    if word in {"-m", "--model"} and i + 1 < len(argv):
                        model = argv[i + 1]
                    elif word.startswith("--model="):
                        model = word.partition("=")[2]
                spec = {"provider": "grok" if base == "cursor-agent" else base, "model": model}
                home = assignments.get({"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "grok": "GROK_HOME"}.get(base))
                if home:
                    # A Claude-compatible transport can point at a GLM login.
                    spec = {"model": model, **routing.login_binding({"login_home": home}, routing.accounts())}
                specs.append(spec)
        except _OutOfTime:
            return []
    view = routing.read_view()
    providers = (view.get("definitions") or {}).get("providers", {})
    known = routing.accounts()
    warnings = []
    for spec in specs:
        match = routing.resolve_lane("", "", {}, known, spec, providers)
        account = next((a for a in known if a["key"] == match["key"]), None)
        fact = providers.get(account["harness"]) if account else providers.get(spec.get("provider"))
        if fact is None and not spec.get("provider"):
            family = model_family(spec.get("model"))
            choices = [p for p in providers.values() if family != "unknown" and p.get("family") == family]
            fact = choices[0] if len(choices) == 1 else None
        if fact:
            warnings += advisory(fact, safe_name(spec.get("model")))
    return list(dict.fromkeys(warnings))


CHILD_NO_ROUTING_ADVISORY = "This child got no routing: add `khipu route --brief` to its brief."


def _t3_child_advisory(payload: dict) -> str | None:
    """The advisory line for a T3 ``delegate_task`` child whose provider gives
    it no Khipu start block (no hooks, or a harness like T3's Antigravity
    runtime that ignores injected context): a brief without routing leaves it
    with nothing. Nothing is added to the brief itself."""
    name = _tool_name(payload)
    if name is None or not (name == "delegate_task" or name.endswith("__delegate_task")):
        return None
    data = payload.get("tool_input") or payload.get("toolInput")
    if not isinstance(data, dict):
        return None
    target = data.get("target") if isinstance(data.get("target"), dict) else {}
    instance_id = target.get("providerInstanceId") or target.get("provider_instance_id") \
        or data.get("providerInstanceId")
    if not isinstance(instance_id, str) or not instance_id:
        return None
    briefs = briefs_of(payload)
    if not briefs or any(_brief_has_routing(brief) for brief in briefs):
        return None
    from khipu import t3

    instance = next((inst for inst in t3.all_provider_instances(t3.settings_path())
                     if inst["id"] == instance_id and inst["enabled"]), None)
    if instance is None or t3.child_gets_start_block(instance):
        return None
    return CHILD_NO_ROUTING_ADVISORY


def _rule12_line(payload: dict) -> str | None:
    """The rule 12 tier line for this dispatch, or None. Context only."""
    from khipu.routing_advisory import format_line, mismatch

    model, brief = _dispatch_model_brief(payload)
    found = mismatch(model, brief)
    if found is None:
        return None
    _keep_mismatch(payload, found, model)
    return format_line(found["task"], found["expected"], found["actual"], found["guessed"])


def _keep_mismatch(payload: dict, found: dict, model: str | None) -> None:
    """Keep the mismatch for the owner's Checks list (labels only: no brief
    text, session id or path). Never fails the dispatch."""
    try:
        from khipu import routing, routing_mismatches

        routing_mismatches.record(dict(found, account=_account_label(_dispatch_config_dir(payload)), model=model,
                                       project=routing._project_name(payload.get("cwd"))))
    except Exception:  # noqa: BLE001 - a record that cannot be kept never changes the advisory
        pass


def _account_label(home: str | None) -> str | None:
    """Khipu's own label for the account a ``claude`` dispatch runs on: plain
    "Claude" with no login folder, else the label of the account whose login
    folder it is. A folder no account holds is None (never its name)."""
    if not home:
        return "Claude"
    from khipu import routing, usage

    key = usage._key({"harness": "claude", "home": home})
    for account in routing.read_view().get("accounts") or []:
        if isinstance(account, dict) and account.get("key") == key:
            return account.get("label")
    return None


def _dispatch_config_dir(payload: dict) -> str | None:
    """The CLAUDE_CONFIG_DIR a ``claude -p`` dispatch names, else None."""
    name = _tool_name(payload)
    data = payload.get("tool_input") or payload.get("toolInput") or {}
    if name not in SHELL_TOOLS or not isinstance(data, dict):
        return None
    return _claude_parse(_text(data.get(SHELL_TOOLS[name])))[2]


def _text(value) -> str | None:
    return value if isinstance(value, str) else None


def _dispatch_model_brief(payload: dict) -> tuple[str | None, str | None]:
    """(model, brief) from an Agent, Task, spawn_agent or claude -p call.

    An explicit ``--model`` or ``-m`` wins over the model named in
    ``CLAUDE_CONFIG_DIR``'s settings file. That file contributes its model
    field only.
    """
    name = _tool_name(payload)
    data = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(data, dict):
        return None, None
    if name in {"Agent", "Task"}:
        return _text(data.get("model")), _text(data.get("prompt"))
    if name == "spawn_agent":
        return _text(data.get("model")), _text(data.get("message"))
    if name not in SHELL_TOOLS:
        return None, None
    return _claude_parse(_text(data.get(SHELL_TOOLS[name])))[:2]


def _claude_parse(command: str | None) -> tuple[str | None, str | None, str | None]:
    """(model, brief, CLAUDE_CONFIG_DIR) of a ``claude`` command line."""
    if not command or len(command) > _SCAN_MOST:
        return None, None, None
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return None, None, None
    env: dict[str, str] = {}
    index = 0
    while index < len(argv) and _ASSIGNMENT.fullmatch(argv[index]):
        key, _, value = argv[index].partition("=")
        env[key] = value
        index += 1
    if index >= len(argv) or os.path.basename(argv[index]) != "claude":
        return None, None, None
    model, explicit, brief = None, False, None
    cursor = index + 1
    while cursor < len(argv):
        word = argv[cursor]
        if word in {"-m", "--model"} and cursor + 1 < len(argv):
            model, explicit, cursor = argv[cursor + 1], True, cursor + 2
            continue
        if word.startswith("--model="):
            model, explicit, cursor = word.split("=", 1)[1], True, cursor + 1
            continue
        if word in {"-p", "--print"} and cursor + 1 < len(argv):
            brief, cursor = argv[cursor + 1], cursor + 2
            continue
        if word.startswith("--print="):
            brief, cursor = word.split("=", 1)[1], cursor + 1
            continue
        if word.startswith("-p") and not word.startswith("--") and len(word) > 2:
            brief = word[2:]
        cursor += 1
    if not explicit and env.get("CLAUDE_CONFIG_DIR"):
        from khipu.routing_advisory import settings_model

        model = settings_model(env["CLAUDE_CONFIG_DIR"])
    return model, brief, env.get("CLAUDE_CONFIG_DIR") or None


# The one advisory line both child checks (Codex ``spawn_agent``, slice
# 8b, and T3 ``delegate_task``, slice 8c) put on the parent's result.
ADVISORY = CHILD_NO_ROUTING_ADVISORY


def _brief_has_routing(brief: str) -> bool:
    """Whether a brief already carries a routing block, as opposed to
    merely mentioning routing in prose. Only the exact headings the
    pushes and ``khipu route --brief`` print count: a line opening
    ``## Routing at`` / ``## Routing changes at`` (the pushes), or
    ``Routing at`` (the --brief form), each naming Khipu's routing
    record in its parenthetical. This is also the T3 check's original
    pair of markers (``## Routing at`` and the record's name), with the
    record's name required in the heading rather than anywhere."""
    for line in brief.splitlines():
        if "(Khipu's routing record" not in line:
            continue
        if line.startswith(("## Routing at ", "## Routing changes at ", "Routing at ")):
            return True
    return False


def _codex_subagent_hook_installed(environ=None) -> bool:
    """Whether Codex's hooks file carries Khipu's SubagentStart hook.

    Read-only and bounded to that one file: a missing or unreadable file
    is absent. This is the pre-spawn evidence available at PreToolUse;
    a delivery recorded for a child cannot exist before that child is
    spawned, so installation is the proxy the parent can act on.
    Follow-up (not built): a "no SubagentStart delivery recorded"
    trigger would also catch a hook that is installed but times out;
    until it exists, that case gives no advisory.

    Only an exact Khipu delegation command counts
    (``integrations._is_our_delegation``), and a disabled entry or hook
    does not."""
    env = os.environ if environ is None else environ
    home = env.get("CODEX_HOME") or (os.path.join(env["HOME"], ".codex") if env.get("HOME") else os.path.expanduser("~/.codex"))
    try:
        with open(os.path.join(home, "hooks.json"), encoding="utf-8") as handle:
            hooks = json.load(handle)
        from khipu.integrations import _is_our_delegation

        if not isinstance(hooks, dict) or not isinstance(hooks.get("hooks"), dict):
            return False
        entries = hooks["hooks"].get("SubagentStart")
        if not isinstance(entries, list):
            return False
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("enabled") is False:
                continue
            nested = entry.get("hooks")
            if not isinstance(nested, list):
                continue
            for hook in nested:
                if isinstance(hook, dict) and hook.get("enabled") is not False \
                        and _is_our_delegation(hook.get("command")):
                    return True
        return False
    except (OSError, ValueError, AttributeError):
        return False


def _spawn_advisory(payload: dict, environ=None) -> str | None:
    """The advisory line for a Codex ``spawn_agent`` whose brief carries
    no routing while Khipu's SubagentStart hook is absent, else None.
    Context only: nothing is added to the brief itself."""
    if _tool_name(payload) != "spawn_agent":
        return None
    briefs = briefs_of(payload)
    if not briefs or any(_brief_has_routing(brief) for brief in briefs):
        return None
    return None if _codex_subagent_hook_installed(environ) else ADVISORY


def _payload_harness(payload) -> str | None:
    """The harness a hook payload itself names, or None: an explicit
    ``harness`` field, else the transcript path's home (``.codex`` /
    ``.claude``), the same markers ``owner_turns.harness_of`` reads.
    The path is only compared as a string, never opened."""
    if not isinstance(payload, dict):
        return None
    named = payload.get("harness")
    if isinstance(named, str) and named.strip():
        named = named.strip().lower()
        if named == "codex":
            return "codex"
        if named in ("claude", "claude_code", "claude-code"):
            return "claude_code"
    transcript = payload.get("transcript_path") or payload.get("transcriptPath") or ""
    if isinstance(transcript, str):
        if "/.codex/" in transcript:
            return "codex"
        if "/.claude" in transcript:
            return "claude_code"
    return None


def _is_codex_child(environ=None, payload=None) -> bool:
    """Whether this call fires for a Codex native child. The payload's
    own harness decides when it names one: a stray Codex variable in a
    Claude subagent's environment (``CODEX_SANDBOX``, say) must not
    strip its deferred pages, and a Codex payload carrying ``agent_id``
    must not be routed down the Claude subagent path. Otherwise the
    environment decides the way ``routing_delivery.canonical_identity``
    does: an explicit ``KHIPU_HARNESS``, then Claude Code's variables
    over Codex's, then any of ``CODEX_THREAD_ID``, ``CODEX_SANDBOX``
    or ``CODEX_HOME`` (the 2b rig sets only the last).

    A Codex child is its own conversation (section 5), so its start
    block is a snapshot and its deferred routing comes only through
    pull delivery under its own ``CODEX_THREAD_ID`` identity."""
    env = os.environ if environ is None else environ
    harness = (env.get("KHIPU_HARNESS") or "").strip().lower()
    if harness:
        return harness == "codex"
    named = _payload_harness(payload)
    if named:
        return named == "codex"
    if env.get("CLAUDE_CODE_SESSION_ID") or env.get("CLAUDECODE") or env.get("CLAUDE_CODE_ENTRYPOINT"):
        return False
    return bool(env.get("CODEX_THREAD_ID") or env.get("CODEX_SANDBOX") or env.get("CODEX_HOME"))


def _agent_tool_use(payload: dict, environ, agent_id: str) -> tuple[dict, dict | None, str | None]:
    """The next deferred routing page on a subagent's own tool result.

    The subagent's PreToolUse is its delivery point: the page comes from
    its ``agent:<agent_id>`` ledger, reserved until this hook's caller
    records it delivered, and the parent's ledger never moves. Context
    only, never a permission decision."""
    if _switched_off(environ) or _helper(payload):
        return {}, None, None
    if _is_codex_child(environ, payload):
        # A Codex native child is its own conversation: no page from a
        # per-agent ledger. Its deferred routing comes only through
        # pull delivery on its own Khipu tool calls (slice 7).
        return {}, None, None
    session_id = hook_shape.session_id(payload) or None
    if not isinstance(session_id, str) or not session_id:
        return {}, None, None
    from khipu import routing

    try:
        page = routing.child_page_push(session_id, agent_id)
    except Exception:  # noqa: BLE001 - routing never breaks the hook
        return {}, None, None
    if not page or not page.get("routing_changes") or not isinstance(page.get("text"), str):
        return {}, None, None
    info = {"status": "changes:1", "pending": {
        "session": page["session"], "version": page["version"], "epoch": page.get("epoch"),
        "full": False, "unrefreshed": None, "at": None, "based_on": None,
        "owed": page.get("owed_entities"), "shown": page.get("shown_entities"),
        "notices": [], "routing_changes": page["routing_changes"]}}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "additionalContext": page["text"]}}, info, "agent"


def pre_tool_use(payload: dict, environ=None) -> tuple[dict, dict | None, str | None]:
    """(what to print, what to record once it is printed, the call's kind)
    for one PreToolUse call. Never a permission decision of any kind. Most
    tool calls end at the first test, which reads nothing."""
    agent_id = payload.get("agent_id") or payload.get("agentId")
    if agent_id:
        return _agent_tool_use(payload, environ, str(agent_id))
    if not might_delegate(payload) or _switched_off(environ):
        return {}, None, None
    kind = delegation_kind(payload)
    if kind is None or _helper(payload):
        return {}, None, None
    from khipu import recall_prompt

    session_id = hook_shape.session_id(payload) or None
    routing = recall_prompt._RoutingAtPrompt().begin(session_id, environ, moment="delegation")
    try:
        ctx, info = routing.add_to("", routing.collect())
    except Exception:  # noqa: BLE001 - routing never breaks the hook
        ctx, info = "", {"status": "skipped:error"}
    try:
        warnings = provider_advisory(payload)
    except Exception:  # noqa: BLE001 - advisory failure never fails a dispatch
        warnings = []
    if warnings:
        ctx += ("\n" if ctx else "") + "Provider advisory (guidance, not a gate): " + " ".join(warnings)
    try:
        line = _rule12_line(payload)
    except Exception:  # noqa: BLE001 - a tier check never fails a dispatch
        line = None
    if line:
        ctx += ("\n" if ctx else "") + line
    try:
        advisory = _t3_child_advisory(payload)
    except Exception:  # noqa: BLE001 - an advisory never fails a dispatch
        advisory = None
    if advisory:
        ctx += ("\n" if ctx else "") + advisory
    try:
        advisory = _spawn_advisory(payload, environ)
    except Exception:  # noqa: BLE001 - an advisory never fails a dispatch
        advisory = None
    if advisory:
        ctx += ("\n" if ctx else "") + advisory
    if not ctx:
        return {}, info, kind
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": ctx}}, info, kind


def _subagent_start(payload: dict, environ=None) -> tuple[dict, dict | None, str | None]:
    """The bounded child block for a child at its start, plus what to
    record once it is printed. One block for every harness, built by
    ``routing_delivery.child_offer`` through ``routing.start_push``:
    accounts and holds first, then each standing rule and the note whole
    if they fit, otherwise their openings, bounded by
    ``routing_child_chars`` and the hook-context cap. A child receives a
    snapshot, not its parent conversation's notices, and the parent's
    marker never moves.

    A Claude Code subagent's remainder is owed in its own
    ``agent:<agent_id>`` ledger and arrives as pages on its own tool
    results. A Codex native child is its own conversation: the block is
    reserved nowhere in the parent, and its deferred routing reaches it
    only on its own Khipu tool calls, under its own ``CODEX_THREAD_ID``
    identity (slice 7 pull delivery)."""
    from khipu import recall_rule

    session_id = hook_shape.session_id(payload)
    agent_id = payload.get("agent_id") or payload.get("agentId") or ""
    if _is_codex_child(environ, payload):
        # Snapshot only: no per-agent ledger under the parent. The
        # unidentified-child component reserves nowhere (see
        # ``_RoutingAtStart``); the child's own pull identity is
        # separate and will be offered what this block left out.
        at = recall_rule._RoutingAtStart(moment="delegation", session_id=session_id,
                                         agent_id=None, child_component="agent:unknown").begin()
    else:
        at = recall_rule._RoutingAtStart(moment="delegation", session_id=session_id,
                                         agent_id=str(agent_id) if agent_id else None,
                                         child_component=None if agent_id else "agent:unknown").begin()
    text = at.text()
    if not text:
        return {}, None, None
    if recall_rule._utf16_len(text) > recall_rule._HOOK_CONTEXT_BUDGET:
        return {}, None, None  # the hook cap wins over any child setting
    out = {"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": text}}
    stored = at.out if isinstance(at.out, dict) else {}
    if not stored.get("routing_changes") or not at.session:
        return out, None, None
    info = {"status": "full", "pending": {
        "session": at.session, "version": stored["version"], "epoch": stored.get("epoch"),
        "full": False, "unrefreshed": None, "at": None, "based_on": None,
        "owed": stored.get("owed_entities"), "shown": stored.get("shown_entities"),
        "notices": [], "routing_changes": stored["routing_changes"]}}
    return out, info, "subagent"


def subagent_start(payload: dict, environ=None) -> dict:
    """The bounded child block for a child agent at its start."""
    return _subagent_start(payload, environ)[0]


def hook_main(raw: str, environ=None) -> tuple[dict, dict | None, str | None]:
    """(what to print, the routing record to keep, the delegation's kind)."""
    env = os.environ if environ is None else environ
    if _aegis(env):
        return {}, None, None
    payload = _payload(raw)
    event = _event(payload)
    if event == "SubagentStart":
        return ({}, None, None) if _helper(payload) else _subagent_start(payload, env)
    # Cursor's postToolUse is the one non-Claude event that carries a delegation
    # (its docs accept additional_context there). Only a Cursor-shaped payload
    # qualifies, and the answer is context alone: Cursor reads no permission here.
    cursor_post = event == "postToolUse" and hook_shape.detect(payload, env)[1] == hook_shape.CURSOR
    if event and event != "PreToolUse" and not cursor_post:
        return {}, None, None
    out = pre_tool_use(payload, environ)
    # After the push, so noting the brief never spends the push's time budget.
    note_brief(payload, env)
    return out


# The field a delegation tool's brief arrives in: Claude Code's Agent (and
# Task) ``prompt``, Codex's spawn_agent ``message``. T3's delegate_task has no
# fixture yet, so every text value in its input long enough to be a brief is
# noted: a field under any name still marks the job's first turn.
_BRIEF_FIELDS = {"Agent": ("prompt",), "Task": ("prompt",), "spawn_agent": ("message",)}


def _texts(value: Any, depth: int = 0) -> list[str]:
    if isinstance(value, str):
        return [value] if len(value.split()) >= 4 else []
    if depth >= 3:
        return []
    items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    return [t for v in items for t in _texts(v, depth + 1)]
# What a dispatch command's options look like, to find its prompt word. An
# option not listed here (or one that takes several words) means the prompt
# cannot be told apart safely, and the brief is not noted.
_CLAUDE_FLAGS = frozenset({"-p", "--print", "--verbose", "--dangerously-skip-permissions", "-c", "--continue",
                           "--include-partial-messages", "--strict-mcp-config", "--no-session-persistence"})
_CLAUDE_VALUED = frozenset({"--model", "--output-format", "--input-format", "--append-system-prompt",
                            "--system-prompt", "--permission-mode", "--max-turns", "--fallback-model",
                            "--session-id", "--settings", "--setting-sources", "--permission-prompt-tool",
                            "--effort", "--agent", "--json-schema", "--max-budget-usd"})
_CODEX_FLAGS = frozenset({"--json", "--skip-git-repo-check", "--full-auto", "--oss", "--ephemeral",
                          "--dangerously-bypass-approvals-and-sandbox", "--experimental-json"})
_CODEX_VALUED = frozenset({"-m", "--model", "-c", "--config", "-C", "--cd", "-s", "--sandbox", "-p", "--profile",
                           "-o", "--output-last-message", "--color", "--output-schema", "--local-provider"})


def _prompt_word(args: list[str], flags: frozenset[str], valued: frozenset[str]) -> str | None:
    """The one prompt word of a dispatch command's arguments, or None."""
    i = 0
    while i < len(args):
        word = args[i]
        if word in flags:
            i += 1
        elif word in valued:
            i += 2
        elif word.startswith("--") and word.split("=", 1)[0] in valued and "=" in word:
            i += 1
        elif word.startswith("-"):
            return None
        else:
            # A word the shell would still expand is not the text the job got.
            return None if ("$(" in word or "`" in word) else word
    return None


def shell_briefs(command: Any, deadline: float | None = None) -> list[str] | None:
    """The prompts of the ``claude -p`` and ``codex exec`` commands a shell
    command starts, or None when one of them cannot be read safely (stdin,
    an option this does not know, a command substitution)."""
    deadline = time.monotonic() + _SCAN_BUDGET_S if deadline is None else deadline
    try:
        if isinstance(command, list):
            argvs = [_bounded_argv(command, deadline) or []]
        elif isinstance(command, str):
            argvs = _segments(command, deadline)
        else:
            return None
        out = []
        for argv in argvs:
            argv = _strip_prefix(argv, deadline)
            if not argv:
                continue
            base, args = os.path.basename(argv[0]), argv[1:]
            if base == "claude" and _prints(args):
                word = _prompt_word(args, _CLAUDE_FLAGS, _CLAUDE_VALUED)
            elif base == "codex" and "exec" in args:
                word = _prompt_word(args[args.index("exec") + 1:], _CODEX_FLAGS, _CODEX_VALUED)
                if word in ("resume", "review", "-"):
                    word = None
            else:
                continue
            if word is None:
                return None
            out.append(word)
        return out
    except _OutOfTime:
        return None


def briefs_of(payload: dict) -> list[str]:
    """The briefs a delegation call hands on, where they can be read."""
    name = _tool_name(payload)
    tool_input = payload.get("tool_input") or payload.get("toolInput")
    if name is None or not isinstance(tool_input, dict):
        return []
    if name == "delegate_task" or name.endswith("__delegate_task"):
        return _texts(tool_input)
    if name in _BRIEF_FIELDS:
        for field in _BRIEF_FIELDS[name]:
            if isinstance(tool_input.get(field), str):
                return [tool_input[field]]
        return []
    if name in SHELL_TOOLS:
        command = tool_input.get(SHELL_TOOLS[name])
        if not _mentions(command, ("claude", "codex"), time.monotonic() + _SCAN_BUDGET_S):
            return []
        return shell_briefs(command) or []
    return []


def note_brief(payload: dict, env) -> None:
    """Owner direction: note each brief this call hands on, so the job's
    first turn (the brief) is never taken for the owner's (``owner_turns``).
    Runs whatever the routing push's switches say. Never raises."""
    try:
        if not might_delegate(payload):
            return
        from khipu import owner_turns

        parent = str(hook_shape.session_id(payload))
        harness = owner_turns.harness_of(env, payload)
        for brief in briefs_of(payload):
            owner_turns.note_brief(brief, parent=parent, harness=harness)
    except Exception:  # noqa: BLE001 - noting a brief never fails the call
        pass


def main() -> None:
    """Entry point for ``bin/khipu-delegation-hook``. Never raises and never
    fails the call: any error prints ``{}``."""
    started = time.monotonic()
    try:
        raw = wire.read_stdin()
    except Exception:  # noqa: BLE001
        raw = ""
    info, kind, out = None, None, {}
    outcome = "pass"
    try:
        out, info, kind = hook_main(raw)
        # The answer is built Claude-shaped; Cursor and Antigravity read their own.
        out = hook_shape.reshape(out, hook_shape.detect(_payload(raw), os.environ)[1])
    except Exception as exc:  # noqa: BLE001 - the one thing this must never do is fail the call
        out = {}
        outcome = f"error:{type(exc).__name__}"
    delivered = True
    try:
        print(wire.dumps(out, ensure_ascii=True), flush=True)
    except OSError:
        delivered = False
    routed = ""
    if kind is not None:
        outcome = "pushed" if out else "delegation"
        try:
            from khipu import recall_prompt

            routed = recall_prompt._routing_delivered(info, delivered)
        except Exception:  # noqa: BLE001
            pass
    _log_call(raw, outcome, kind, routed, started)


def _log_call(raw: str, outcome: str, kind: str | None, routed: str, started: float) -> None:
    """One line per call, in prompt-recall.log under its own tag (so the
    per-prompt lane's outcome rate never counts it): what the hook cost and
    what it did. Runs on every tool call, so it imports nothing heavy."""
    try:
        from datetime import datetime, timezone

        from khipu.paths import capture_home

        payload = _payload(raw)
        session = hook_shape.session_id(payload) or "?"
        event = _event(payload) or "?"
        tool = str(payload.get("tool_name") or payload.get("toolName") or "-").replace(" ", "_")[:40]
        call = f" call={kind.replace(' ', '_')}" if kind else ""
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = (f"{stamp} [khipu-delegation-hook] session={session} event={event} tool={tool} "
                f"outcome={outcome}{call}{routed} ms={(time.monotonic() - started) * 1000:.1f}\n")
        path = capture_home() / "logs" / "prompt-recall.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001 - logging never fails the call
        pass
