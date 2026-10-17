"""What part of a user turn the person typed: the one stripping path that
capture (``session_capture``) and the owner-turn recorder (``owner_turns``)
share. Standard library plus ``khipu.t3`` only, so the prompt hook's
recorder can import it without the rest of the package."""
from __future__ import annotations

import re

from khipu.t3 import strip_handoff

# Blocks a harness writes into a user turn to carry hook output and other
# context the person never typed. One entry per harness tag, so a new harness's
# wrapper is one more name here, not a second stripping path:
#   system-reminder  Claude Code (hook context, reminders)
#   hooks_context    Cursor (sessionStart additional_context, recorded in the
#                    agent transcript with a description="..." attribute)
INJECTED_CONTEXT_TAGS = ("system-reminder", "hooks_context")
# What the owner-turn recorder also drops: text that reached the turn from
# somewhere else (a paste the harness wrapped, a tool's output). Capture keeps
# these, since they are part of what the conversation was about; they are
# never the owner's own words.
NOT_TYPED_TAGS = INJECTED_CONTEXT_TAGS + ("pasted_content", "tool_result", "function_results")
_CURSOR_WRAP = re.compile(r"</?(?:user_query|timestamp)>")

# "User" messages the harness writes, not the person: background-task
# notifications, messages relayed between sessions, slash-command wrappers.
MACHINE_USER_TAGS = (
    "<task-notification",
    "<cross-session-message",
    "<cross-session-idle",
    "<cross-session-delivery",
    "<local-command-",
    "<command-name>",
)


def _blocks(tags: tuple[str, ...]) -> re.Pattern:
    return re.compile(r"<(" + "|".join(re.escape(t) for t in tags) + r")(?:\s[^>]*)?>.*?</\1>", re.S)


_INJECTED_CONTEXT = _blocks(INJECTED_CONTEXT_TAGS)
_NOT_TYPED = _blocks(NOT_TYPED_TAGS)


def clean_user_text(text: str, *, owner: bool = False) -> str:
    """Strip harness-injected scaffolding from a user turn: the injected
    context blocks in ``INJECTED_CONTEXT_TAGS`` (Claude Code's <system-reminder>,
    Cursor's <hooks_context>; thousands of chars of hook context, none of it the
    user), Cursor's <user_query>/<timestamp> wrappers, and the hand-over T3 Code
    glues in front of the first message after a provider switch. ``owner``
    also drops ``NOT_TYPED_TAGS`` (pasted and tool text)."""
    cleaned = (_NOT_TYPED if owner else _INJECTED_CONTEXT).sub("", text)
    # A Claude system-reminder can leave its separator newline before a real
    # T3 header. The public stripper stays strict; this harness cleanup owns
    # that injected prefix.
    if cleaned != text and cleaned.startswith("\n"):
        candidate = cleaned.lstrip("\n")
        stripped = strip_handoff(candidate)
        text = stripped if stripped != candidate else cleaned
    else:
        text = strip_handoff(cleaned)
    return _CURSOR_WRAP.sub("", text)   # no strip: ACP user text arrives in chunks that must re-join exactly


# What makes a tag wrapper-like: a name a harness wraps text in, or any
# name in the snake_case or kebab-case style harnesses use for them. A plain
# HTML name (<b>, <div>) is something the owner can type.
_TAG = re.compile(r"</?([A-Za-z][\w-]*)(?:\s[^<>]*)?/?>")
_HARNESS_STYLE = re.compile(r"[a-z][a-z0-9]*(?:[_-][a-z0-9]+)+")
_KNOWN_WRAPPERS = frozenset(NOT_TYPED_TAGS) | {"user_query", "timestamp"} | {
    t.lstrip("<").rstrip(">-") for t in MACHINE_USER_TAGS}


def _wrapper_like(name: str) -> bool:
    name = name.lower()
    return name in _KNOWN_WRAPPERS or _HARNESS_STYLE.fullmatch(name) is not None


def _open_block(text: str, tags: tuple[str, ...]) -> re.Match | None:
    return re.match(r"<(" + "|".join(re.escape(t) for t in tags) + r")(?:\s[^<>]*)?>", text)


def owner_text(text: str) -> str | None:
    """The words the owner typed in a prompt field, or None when they can't
    be told apart from text that came from somewhere else.

    Harness wrappers count only at the places a harness puts them: blocks of
    ``NOT_TYPED_TAGS`` at the start (hook context, a paste or a tool's output
    the turn opens with), blocks of ``INJECTED_CONTEXT_TAGS`` at the end
    (hook context appended), and T3's hand-over in front, of which only the
    text after its last "User message" marker is the current turn (a
    historical item can hold that marker too, never after T3's own). A
    block ends at its first closing tag, never at one the reader would pick:
    any wrapper-like tag left anywhere in the rest, a closing tag inside a
    block included, means the whole turn does not count."""
    if not isinstance(text, str):
        return None
    rest = text.replace("\r\n", "\n")
    while True:
        head = rest.lstrip()
        m = _open_block(head, NOT_TYPED_TAGS)
        if m is None:
            break
        close = head.find(f"</{m.group(1)}>", m.end())
        if close < 0:
            return None
        rest = head[close + len(m.group(1)) + 3:]
    while True:
        tail = rest.rstrip()
        m = re.search(r"</(" + "|".join(re.escape(t) for t in INJECTED_CONTEXT_TAGS) + r")>$", tail)
        if m is None:
            break
        opened = [o for o in re.finditer(r"<" + re.escape(m.group(1)) + r"(?:\s[^<>]*)?>", tail)]
        if not opened:
            return None
        rest = tail[:opened[-1].start()]
    rest = strip_handoff(rest.lstrip("\n"), prefer_last=True)
    if any(_wrapper_like(m.group(1)) for m in _TAG.finditer(rest)):
        return None
    return rest.strip()


def is_machine(text: str, *, anywhere: bool = False) -> bool:
    """True when a cleaned user turn is a message the harness wrote: it opens
    with one of ``MACHINE_USER_TAGS``. ``anywhere`` (the owner-turn recorder):
    one of them anywhere in the turn, since text inside a wrapper is never
    the owner's and a turn that mixes the two can't say which words are."""
    if anywhere:
        return any(tag in text for tag in MACHINE_USER_TAGS)
    return text.lstrip().startswith(MACHINE_USER_TAGS)
