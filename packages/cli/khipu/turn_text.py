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


def is_machine(text: str, *, anywhere: bool = False) -> bool:
    """True when a cleaned user turn is a message the harness wrote: it opens
    with one of ``MACHINE_USER_TAGS``. ``anywhere`` (the owner-turn recorder):
    one of them anywhere in the turn, since text inside a wrapper is never
    the owner's and a turn that mixes the two can't say which words are."""
    if anywhere:
        return any(tag in text for tag in MACHINE_USER_TAGS)
    return text.lstrip().startswith(MACHINE_USER_TAGS)
