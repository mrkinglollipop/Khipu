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

# Plain first-person "user" turns the Claude desktop app writes when it resumes
# work by itself (after a usage limit resets, the Mac sleeps, the app quits,
# the context window fills, Claude Code restarts on a remote host). The owner
# never typed them, but nothing marks them: a turn whose whole text is one of
# these is the app's. Copied from the app's bundle (Claude 4.23118.1,
# app.asar, 2026-11-10); a new or reworded string is one more entry here.
_RESTART_RUNNING = (
    "Commands that you started before the restart may still be running, even foreground ones and even if they "
    "read as interrupted or stopped. Your process list may be blind to them: if it shows only your own processes, "
    "a missing process proves nothing. So before you start one again in any form, always look twice at what it "
    "writes or changes, at least a minute apart, longer if it writes rarely. If you see its process or a change, "
    "it still runs: start no second copy; if you need it finished, keep checking until it stops. If you see "
    "neither and your list is blind, you cannot tell: say so first, and run it again only if the task allows two "
    "copies and they surely do no harm; otherwise leave it and do what does not need it.")
APP_AUTO_CONTINUE = (
    "I hit my usage limit while you were working, but it has reset now. Please continue from where you left off.",
    "My computer went to sleep while you were working. Please continue from where you left off.",
    "The app was quit while you were working. Please continue from where you left off.",
    "The context window filled up while you were working, so the conversation was compacted. "
    "Please continue from where you left off.",
    f"Claude Code was restarted on the remote host while you were working. {_RESTART_RUNNING} "
    "Then continue from where you left off.",
    "Claude Code was restarted on the remote host while you were working. If your last tool call reads as refused "
    "or interrupted by the user, that was written by the restart: nobody refused it, and it may have run in part "
    f"before it was cut off. {_RESTART_RUNNING} Check what your last tool call did before you run it again, then "
    "continue from where you left off.",
)
_APP_AUTO_CONTINUE = frozenset(" ".join(t.split()) for t in APP_AUTO_CONTINUE)


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
    with one of ``MACHINE_USER_TAGS``, or its whole text is one of the desktop
    app's ``APP_AUTO_CONTINUE`` turns. ``anywhere`` (the owner-turn recorder):
    a tag anywhere in the turn, since text inside a wrapper is never the
    owner's and a turn that mixes the two can't say which words are."""
    if " ".join(text.split()) in _APP_AUTO_CONTINUE:
        return True
    if anywhere:
        return any(tag in text for tag in MACHINE_USER_TAGS)
    return text.lstrip().startswith(MACHINE_USER_TAGS)


# A line that opens with a tagged envelope (``<task-notification>``,
# ``<scheduled-task>``, ``<system-reminder>``): a harness message, not a prompt.
# The per-prompt recall path and the "only my words" search share this one test.
HARNESS_MESSAGE_RE = re.compile(r"\s*<[A-Za-z][\w-]*>")

# Untagged openings a harness, a scheduler or another agent writes into a user
# turn. Stored ``verbatim.quotes`` keep each message's opening, so these are
# matched at the start only: a typed line that merely quotes one is the owner's.
_AUTOMATED_OPENINGS = re.compile(
    r"\s*(?:"
    r"\[SYSTEM NOTIFICATION\b"                      # background-task notices
    r"|\[notification:"                              # capture's stand-in for a machine message
    r"|Delegated task\b"                             # "Delegated task ... [completed]"
    r"|This session is being continued from a previous conversation"   # compaction summary
    r"|[^\n(\"'?]{0,40}\(scheduled(?:,|\s+(?:task|run)\b)"   # "Lane check (scheduled, not Matt)..."
    r"|Act as the [^\n.]{0,60}sub-?agent\b"           # a brief sent to a delegated job
    r")",
    re.I,
)


def is_automated(text: str) -> bool:
    """True when a stored owner line was not typed by the owner: a harness
    envelope, a machine notification, a scheduled-task prompt, a compaction
    summary or a brief another agent sent to a job."""
    if not isinstance(text, str):
        return False
    return (is_machine(text) or HARNESS_MESSAGE_RE.match(text) is not None
            or _AUTOMATED_OPENINGS.match(text) is not None)
