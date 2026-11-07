"""The routing push at the moment of delegation (routing truth, phase 6).

Two hooks, both behind ``routing_push`` and ``routing_push_delegation``:

- PreToolUse, matched only to the delegation tools: the Agent tool
  (``Task`` in older Claude Code), Codex's ``spawn_agent`` and
  ``delegate_task`` (bare or as an MCP tool). Shell commands are not
  matched: the shell half (``claude -p``, ``codex exec`` and the owner's
  dispatch scripts read out of every shell command) was removed on
  2026-11-07, since session-start routing already reaches those jobs. It
  sends what the per-prompt push would send at this moment:
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
  ``agent:<agent_id>`` ledger, arriving as pages on its own delegation
  tool calls (its PreToolUse, below). A Codex native child records the same block
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

import json
import os
import time
from typing import Any

from khipu import hook_shape, wire

# Claude Code's subagent tool (``Task`` in older versions) and Codex's native
# one (``spawn_agent``).
AGENT_TOOLS = frozenset({"Agent", "Task", "spawn_agent"})


def _aegis(environ) -> bool:
    """Aegis's runner is calling: its hooks discard what a hook prints, and
    Khipu runs there only through Aegis's own pack."""
    return bool((environ.get("GROK_HOOK_NAME") or "") + (environ.get("GROK_HOOK_EVENT") or "")) \
        or environ.get("KHIPU_HARNESS") == "aegis"


def _tool_name(payload: dict) -> str | None:
    name = payload.get("tool_name") or payload.get("toolName")
    return name if isinstance(name, str) else None


def _is_delegate_task(name: str) -> bool:
    return name == "delegate_task" or name.endswith("__delegate_task")


def might_delegate(payload: dict) -> bool:
    """Whether this call's tool can hand work on at all, from its name
    alone: no file, setting or module is read."""
    name = _tool_name(payload)
    return name is not None and (name in AGENT_TOOLS or _is_delegate_task(name))


def delegation_kind(payload: dict) -> str | None:
    """What kind of delegation this PreToolUse call is, or None."""
    name = _tool_name(payload)
    if name is None:
        return None
    if name in AGENT_TOOLS:
        return "agent"
    if _is_delegate_task(name):
        return "delegate_task"
    return None


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
    if not isinstance(data, dict) or name is None or not might_delegate(payload):
        return []
    specs = [{k: data[k] for k in ("provider", "model", "account", "budget_account") if k in data}]
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
    from khipu.routing_advisory import dispatch_line

    model, brief = _dispatch_model_brief(payload)
    return dispatch_line(model, brief)


def _text(value) -> str | None:
    return value if isinstance(value, str) else None


def _dispatch_model_brief(payload: dict) -> tuple[str | None, str | None]:
    """(model, brief) from an Agent, Task or spawn_agent call."""
    name = _tool_name(payload)
    data = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(data, dict):
        return None, None
    if name in {"Agent", "Task"}:
        return _text(data.get("model")), _text(data.get("prompt"))
    if name == "spawn_agent":
        return _text(data.get("model")), _text(data.get("message"))
    return None, None


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

def briefs_of(payload: dict) -> list[str]:
    """The briefs a delegation call hands on, where they can be read."""
    name = _tool_name(payload)
    tool_input = payload.get("tool_input") or payload.get("toolInput")
    if name is None or not isinstance(tool_input, dict):
        return []
    if _is_delegate_task(name):
        return _texts(tool_input)
    if name in _BRIEF_FIELDS:
        for field in _BRIEF_FIELDS[name]:
            if isinstance(tool_input.get(field), str):
                return [tool_input[field]]
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
        if outcome == "pass":  # an error stays visible
            outcome = "pushed" if out else "delegation"
        try:
            from khipu import recall_prompt

            routed = recall_prompt._routing_delivered(info, delivered)
        except Exception:  # noqa: BLE001
            pass
    _log_call(raw, outcome, kind, routed, started)


def _log_field(value: object, limit: int = 80) -> str:
    """A value as one log token: whitespace and control characters (a newline
    would start a forged record) become ``_``."""
    return "".join("_" if (c.isspace() or not c.isprintable()) else c for c in str(value))[:limit]


def _log_call(raw: str, outcome: str, kind: str | None, routed: str, started: float) -> None:
    """One line per call, in prompt-recall.log under its own tag (so the
    per-prompt lane's outcome rate never counts it): what the hook cost and
    what it did. Runs on every matched tool call, so it imports nothing heavy. ``ms``
    runs from the hook's start to just before the line is appended (it
    includes creating the folder and opening the file, not the final write)."""
    try:
        from datetime import datetime, timezone

        from khipu.paths import capture_home

        payload = _payload(raw)
        session = _log_field(hook_shape.session_id(payload) or "?")
        event = _log_field(_event(payload) or "?")
        tool = _log_field(payload.get("tool_name") or payload.get("toolName") or "-", 40)
        call = f" call={_log_field(kind)}" if kind else ""
        outcome = _log_field(outcome)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        path = capture_home() / "logs" / "prompt-recall.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{stamp} [khipu-delegation-hook] session={session} event={event} tool={tool} "
                    f"outcome={outcome}{call}{routed} ms={(time.monotonic() - started) * 1000:.1f}\n")
    except Exception:  # noqa: BLE001 - logging never fails the call
        pass
