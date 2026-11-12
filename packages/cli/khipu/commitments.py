"""Explicit asks, manual commitment updates and archived commitment reads."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from functools import lru_cache
from typing import Any


VALID_KINDS = ("followup", "blocker", "question", "promise")
STALE_AFTER_DAYS = 30           # blockers / questions: a long-lived shape
STALE_AFTER_DAYS_SOFT = 14      # followups / promises with no due date
# A lost ON CONFLICT insert whose winner retired before it was locked is
# retried this many times in all, then the capture defers (scope 3.4, I4).
# Kind → sort rank for `khipu owed` (0 = most urgent). Surfaced as the row's
# `priority` so the desktop sorts without re-deriving the rule.
KIND_PRIORITY = {"blocker": 0, "question": 1, "promise": 2, "followup": 3}
# USER_OWNERS used to be a hardcoded frozenset naming the maintainer ("matt")
# directly — a bug in a public product where other users have other names
# (2026-09-05). It is now built from `user_aliases()` on first use; see
# `_compile_user_patterns()` below. ASSISTANT_OWNERS names the ASSISTANT side
# only (never a person), so it stays a plain constant.
ASSISTANT_OWNERS = frozenset({
    "assistant", "claude", "agent", "ai", "bot", "model", "me", "khipu",
    "cursor", "codex", "opus", "sonnet",
})
# Generic words that always mean "the user", independent of any configured
# alias — used both for the owner-normalization set and (as multi-word
# phrases) inside the actor/decision regexes.
_GENERIC_USER_OWNER_WORDS = ("user", "operator", "human", "you", "owner")
# This is the maintainer's own tool, and an owner alias decides an item for good
# when it is written, so every Mac writes with the same two names whatever its
# config holds (the configured list adds to them).
_BUILT_IN_USER_ALIASES = ("matt", "matthew")
_GENERIC_USER_REGEX_PHRASES = ("the user", "user", "the operator", "you")
OWNER_USER = "user"
OWNER_ASSISTANT = "assistant"
# A third party: the orchestrator that dispatched a job, a PR's author, the
# reviewer, a builder. Stored with ``owner_label`` (migration 0033) naming
# which; never "needs the user" and never retired by its job's session end.
OWNER_AGENT = "agent"
ORCHESTRATOR = "orchestrator"
ORIGIN_CONVERSATION = "conversation"
ORIGIN_JOB = "job"
# Who closed an item (closed_by, migration 0034).
BY_RULE = "Khipu, by rule"
BY_APP = "you, in the app"
BY_CONVERSATION = "a conversation"
_DONE_PREFIX_RE = re.compile(r"^\s*done\s*:\s*", re.I)
_ISO_Z_RE = re.compile(r"Z$")
_RELATIVE_DUE_RE = re.compile(
    r"^\s*(?:in\s+)?(\d+)\s*(day|days|week|weeks|month|months)\s*$", re.I
)


def _log(msg: str) -> None:
    import sys

    print(f"[khipu-commitments] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Extraction precision (2026-09-04). One definition, three consumers: the
# extractor prompt (khipu.extract.PROMPT), the deterministic post-filter below
# (applied before every insert), and the hygiene re-judge
# (khipu.hygiene.run_commitments_hygiene). Changing the rule means changing it
# HERE — the other two import this text so they cannot drift apart.
# ---------------------------------------------------------------------------


# Each rule is (reason, pattern). The reason is reported verbatim by
# `historical commitment cleanup` so a human reviewing a dry run can see WHY.
_IN_PROGRESS_RE = re.compile(
    r"\b(?:is|are|was|were|remains?|stays?)\s+"
    r"(?:still\s+|currently\s+|now\s+|already\s+)?"
    r"(?:running|building|compiling|processing|pending|executing|generating|"
    r"queued|underway|ongoing|in[-\s]flight|in\s+progress)\b"
    r"|\bstill\s+(?:running|building|pending|open|waiting|working|going)\b"
    r"|\bin[-\s]flight\b|\bin\s+progress\b|\bunderway\b"
    r"|\bpending\b|\bawaiting\b"
    r"|\bwait(?:ing|s)?\s+(?:on|for)\b"
    r"|\bcurrently\s+\w+ing\b",
    re.I,
)
# Waiting on another agent's output: "the agent's verdict", "a report from the
# phase 2 agent", "poll the drive". The word "agent" alone ("fix agent
# attribution") is not this shape. It never discards an item and never
# decides an owner (owners come from lineage, :func:`resolve_owner_and_label`);
# it only marks the assistant's own session-plan rows
# (:func:`is_session_plan_text`).
_AGENT_RE = re.compile(
    r"\b(?:sub-?agents?|agents?)(?:'s|s'|’s)?\s+(?:verdicts?|reports?|results?|outputs?|repl(?:y|ies)|"
    r"answers?|responses?|finish\w*|return\w*|land\w*)\b"
    r"|\b(?:from|by)\s+(?:the\s+|a\s+|an\s+)?(?:[\w-]+\s+){0,3}(?:sub-?)?agents?\b"
    r"|\bdrives?\s+\d+\b"
    r"|\bnotifications?\b"
    r"|\bpoll(?:s|ing)?\b"
    r"|\bverdicts?\b"
    r"|\bbackground\s+(?:task|job|agent|process|run)s?\b"
    r"|\b(?:report|result|response|output|answer)s?\s+"
    r"(?:from|back|arriv\w+|land\w+|com\w+|return\w+)\b"
    r"|\breceiv\w+\s+(?:the\s+|a\s+|an\s+)?(?:report|message|verdict|result|reply)",
    re.I,
)
_COORDINATION_RE = re.compile(
    r"\b(?:send|sends|sending|relay|relays|forward|forwards|deliver|delivers)\b"
    r"[^.]{0,45}\b(?:message|note|signal|ping|notice|handoff)\b"
    r"|\bscreen\s+(?:is\s+)?(?:free|busy|clear)\b"
    r"|\bping\s+(?:the\s+)?(?:\w+\s+)?(?:session|agent|worker|khipu|orchestrator)\b"
    r"|\bhand\s*off\s+to\s+(?:the\s+)?(?:\w+\s+)?(?:session|agent)\b"
    r"|\bmessage\s+(?:to|from)\s+(?:the\s+)?(?:\w+\s+)?(?:session|agent|khipu)\b",
    re.I,
)
# "The lease mechanism is running", "That check is complete" — a report about
# state, not a thing anyone owes.
_STATUS_SENTENCE_RE = re.compile(
    r"^\s*(?:the|a|an|this|that|its|their)\b[^.]{0,90}?\b(?:is|are|was|were)\b\s+"
    r"(?:still\s+|currently\s+|now\s+|already\s+)?"
    r"(?:\w+ing|pending|blocked|open|active|underway|complete|completed|done|"
    r"unknown|unclear|green|red|clean)\b",
    re.I,
)

_EXCLUSION_RULES: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("in-progress", _IN_PROGRESS_RE),
    ("coordination", _COORDINATION_RE),
    ("status-sentence", _STATUS_SENTENCE_RE),
)
_SESSION_PLAN_RULES = (*_EXCLUSION_RULES, ("agent-or-infra", _AGENT_RE))

MIN_COMMITMENT_WORDS = 4

# A concrete action has to be NAMED. Curated rather than inferred: a
# part-of-speech guess ("anything ending in -ate") admits "template", "state",
# "candidate" and the filter stops filtering. Stems only — _has_action_verb
# strips the usual inflections before the lookup.
_ACTION_STEMS = frozenset("""
add adjust answer apply approve archive ask audit backfill benchmark bring
build buy call cancel change check choose clarify clean close collect commit
compare compile confirm connect convert copy create cut decide define delete
deliver deploy design disable document draft drop enable ensure evaluate
explain export extend file fill find finish fix follow gather get give hand
implement import improve install investigate land launch link list load look
maintain make measure merge migrate move name negotiate open order patch pay
pick pin plan port post prepare price prove provide publish pull purchase push
quote raise read rebase rebuild record refactor release remove rename renew
repair replace reply report request research resolve restore retire review
revisit rewrite roll run schedule select sell send set settle share ship sign
simplify sort split start submit supply switch sync tag talk teach tell test
tidy trim try tune uninstall update upgrade upload use validate verify vet
wire write
""".split())
_OBLIGATION_RE = re.compile(
    r"\b(?:must|should|needs?\s+to|has\s+to|have\s+to|owes?|due|"
    r"waiting\s+on\s+matt|to\s+be\s+(?:decided|chosen|approved|provided))\b",
    re.I,
)


# ---------------------------------------------------------------------------
# Owner and future trigger (2026-09-04, second pass). Matt's bar: "Owed needs
# to be really good or it'll be entirely ignored." Half of what survived the
# first pass was still the ASSISTANT's own in-session promises from sessions
# that have since ended ("Reply with the SHA each PR now points at").
#
# Two fields decide whether a commitment outlives its session:
#   * ``owner``          — 'user' (the only thing the desktop's "Needs you"
#                          section shows) or 'assistant';
#   * ``future_trigger`` — the text carries an explicit cross-session
#                          condition ("when Matt says …", "next session",
#                          "after the wave merges", "if attempt six …").
# An assistant commitment with no future trigger dies with its session
# (the archived session-close pass). Detection is DETERMINISTIC here; the extractor
# prompt asks the model for both fields too, but the regex result wins on
# conflict — a model that mislabels an item cannot keep noise alive.
# ---------------------------------------------------------------------------

# "when/once/until/after/before <someone/something> <does something>", plus
# the explicit next-session phrasings and the plain deferral idioms ("deferred
# until", "parked until", "not until"). Gaps allow hyphens/quotes so "after
# the oracle-speed wave merges" matches.
#
# O1 (2026-09-14): the closing-verb list used to omit "closes"/"resolves"/
# "wraps up"/"un-defers" and the completion shapes "is closed"/"is done"/
# "is ready"/"is available" (the last four ride the bare "is"/"are"/"has"/
# "have" alternative already present, so they need no separate entry) — a
# session-plan/scope item deferred with "until the ledger closes" read as NO
# trigger at all and was closed as 'session-ended' at the very next Stop
# (the incident's cause #2). "until" now shares the when/once alternation;
# "before" gets its own (same verb set) so a plain SAME-session sequencing
# phrase like "before and after the mid-stream kill" — no completion verb
# right after "before" — still does not match.
_GAP = r"(?:[\w'’`./-]+\s+){0,4}?"
_CLOSING_VERBS = (
    r"says?|said|tells?|confirms?|approves?|answers?|replies|responds?|"
    r"returns?|merges?|merged|ships?|shipped|lands?|landed|finishes?|"
    r"finished|completes?|completed|arrives?|comes?|decides?|picks?|"
    r"chooses?|gives?|gets?|re-?auths?|re-?authed|closes?|closed|resolves?|"
    r"resolved|wraps?\s+up|wrapped\s+up|un-?defers?|un-?deferred|passes?|"
    r"passed|is|are|has|have"
)
_FUTURE_TRIGGER_RE = re.compile(
    r"\b(?:next|future|later|another|a\s+future|a\s+later)\s+session\b"
    r"|\bnext\s+time\b"
    r"|\bin\s+a\s+(?:future|later|new)\s+session\b"
    r"|\b(?:when|once|until)\s+" + _GAP + r"(?:" + _CLOSING_VERBS + r")\b"
    r"|\bafter\s+" + _GAP + r"(?:" + _CLOSING_VERBS + r"|re-?auth\w*)\b"
    r"|\bbefore\s+" + _GAP + r"(?:" + _CLOSING_VERBS + r")\b"
    r"|\bdeferred\s+(?:until|to)\s+\S"
    r"|\bparked\s+until\s+\S"
    r"|\bnot\s+until\s+\S"
    r"|\bif\s+attempt\s+\w+"
    r"|\bif\s+and\s+when\b",
    re.I,
)

# ---------------------------------------------------------------------------
# User identity (2026-09-05). Owed's owner rule used to hardcode the
# maintainer's first name ("matt") in USER_OWNERS and in the actor/decision/
# reporting regexes below — a bug in a public product where other users have
# other names. Every one of those is now BUILT from `user_aliases()` instead
# of a literal name, compiled once (`_compile_user_patterns`, cached) and
# rebuildable on demand (`reset_user_patterns`, e.g. after `khipu config
# --set user_aliases ...` or in a test that patches KHIPU_USER_ALIASES).
#
# Aliases decide an owner only when an item is WRITTEN (capture, a reassignment's
# default, the clean-up's proposals). A stored owner is a fact of the row:
# `row_owner` reads it and never re-derives it from the text, so a read gives
# the same owner on every Mac whatever aliases that Mac has configured.
# ---------------------------------------------------------------------------


def user_aliases() -> tuple[str, ...]:
    """Every name that means "the user" when an item is WRITTEN: lowercased,
    de-duplicated. The configured list (``khipu config --set user_aliases
    "priya"`` / ``KHIPU_USER_ALIASES``) after the built-in ``matt`` and
    ``matthew``, then the generic words that always mean the user.

    Only capture and the clean-up's proposals read this. A stored row's owner
    is never re-derived from it (:func:`row_owner`), and no machine detail (the
    git name, ``$USER``) is a source: two Macs reading one hub show one owner
    for an item.
    """
    from khipu.config import list_setting

    out: list[str] = []
    seen: set[str] = set()

    def _add(raw: str | None) -> None:
        val = (raw or "").strip().lower()
        if val and val not in seen:
            seen.add(val)
            out.append(val)

    for alias in (*_BUILT_IN_USER_ALIASES, *list_setting("user_aliases")):
        _add(alias)
    for word in _GENERIC_USER_OWNER_WORDS:
        _add(word)
    return tuple(out)


class _UserPatterns:
    """Compiled, alias-driven regexes + the owner set — see
    :func:`_compile_user_patterns`."""

    __slots__ = ("owners", "actor_re", "decision_re", "reporting_re")

    def __init__(self, owners, actor_re, decision_re, reporting_re):
        self.owners = owners
        self.actor_re = actor_re
        self.decision_re = decision_re
        self.reporting_re = reporting_re


@lru_cache(maxsize=1)
def _compile_user_patterns() -> "_UserPatterns":
    aliases = user_aliases()
    owners = frozenset(aliases)
    # Longest-first alternation so a multi-word phrase ("the operator") is
    # tried before a shorter alias that is its substring, and a multi-word
    # configured alias ("the ops lead") matches as one unit.
    names = sorted({re.escape(a) for a in aliases}, key=len, reverse=True)
    phrases = sorted(
        {re.escape(p) for p in (*aliases, *_GENERIC_USER_REGEX_PHRASES)},
        key=len, reverse=True,
    )
    name_alt = "|".join(names) or re.escape("\x00no-alias\x00")
    phrase_alt = "|".join(phrases) or re.escape("\x00no-alias\x00")

    # The USER named as the ACTOR — deliberately narrow. A bare name anywhere
    # in the sentence is NOT a signal ("runnable by Matt, with all commands
    # executed by the assistant" is assistant work); the name has to govern
    # a verb.
    actor_re = re.compile(
        rf"\b(?:{name_alt})(?:'s|’s)\s+"
        r"(?:action|call|decision|approval|sign-?off|move|turn|job|task)\b"
        rf"|\b(?:{phrase_alt})\s+"
        r"(?:to\s+\w+|will\s+\w+|must\b|should\b|needs?\s+to\b|needs?\s+(?:a|an|the)\b|"
        r"has\s+to\b|have\s+to\b|owes?\b|owns?\b|reviews?\b|merges?\b|decides?\b|"
        r"approves?\b|confirms?\b|picks?\b|chooses?\b|supplies?\b|provides?\b|"
        r"tests?\b|runs?\b|says?\b|answers?\b|signs?\b)"
        rf"|\b(?:owned|decided|approved|chosen|answered|confirmed|reviewed|merged|"
        rf"tested|run)\s+by\s+(?:{phrase_alt})\b"
        rf"|\bwaiting\s+on\s+(?:{phrase_alt})\b"
        rf"|\bask\s+(?:{phrase_alt})\b"
        rf"|\bup\s+to\s+(?:{phrase_alt})\b"
        rf"|\bfor\s+(?:{phrase_alt})\s+to\s+\w+",
        re.I,
    )
    # A decision/approval owed, with no actor named — the passive shapes.
    decision_re = re.compile(
        r"\b(?:a\s+)?decision\s+is\s+owed\b"
        r"|\bis\s+owed\b"
        r"|\bneeds?\s+(?:a\s+)?(?:decision|approval|sign-?off|answer|go-?ahead)\b"
        r"|\bto\s+be\s+(?:decided|approved|chosen|confirmed|reviewed|merged|signed)\b"
        rf"|\bawait(?:s|ing)?\s+(?:{phrase_alt}|approval|sign-?off|a\s+decision)\b"
        r"|\bUSER\s+or\s+ASSISTANT\b"
        r"|^\s*(?:please\s+)?(?:approve|sign\s*off|authorize|authorise)\b"
        r"|^\s*decide\s+(?:on|whether|between|if)\b"
        r"|^\s*confirm\s+whether\b"
        r"|^\s*review\s+and\s+merge\b",
        re.I,
    )
    # Rule 3: within-session REPORTING duties. "Reply with the SHA", "Tell
    # the user the moment it relaunches", "Provide SHAs and evidence" — the
    # assistant says these to the user inside one window; nothing about them
    # survives the window, and they were the single most common survivor of
    # the first pass. Never durable UNLESS the user is the one who owes the
    # report.
    reporting_re = re.compile(
        r"^\s*(?:then\s+|finally\s+)?reply\s+(?:with|to)\b"
        r"|\breply\s+with\b"
        rf"|\btell\s+(?:the\s+)?(?:{name_alt}|user)\b"
        r"|\bnotify\b"
        r"|\breport\s+back\b"
        r"|\bconfirm\b[^.]{0,60}?\bdone\b"
        r"|\bprovide\s+(?:the\s+|a\s+|an\s+)?"
        r"(?:shas?|paths?|evidence|records?|proof|row-?counts?)\b",
        re.I,
    )
    return _UserPatterns(owners, actor_re, decision_re, reporting_re)


def reset_user_patterns() -> None:
    """Force the next call into `_compile_user_patterns()` to rebuild from
    the current `user_aliases()` — call after changing the configured
    aliases (``khipu config --set user_aliases ...``) so the change takes
    effect within the same process, and from tests that patch
    ``KHIPU_USER_ALIASES``."""
    _compile_user_patterns.cache_clear()


# ---------------------------------------------------------------------------
# Third-party actors (2026-10-16, owed-and-ledger scope slice 1). In a busy
# orchestrated project most open items named neither the user nor the
# assistant but "the orchestrator", "PR #234's author", "the reviewer"; with
# only two owners they all became the user's. In a JOB's capture a third-party
# actor governing a verb makes the item an ``agent`` item with that label,
# checked before the user words, so "the owner of PR #12 to rebase" is the PR
# author's ("owner" is one of the generic user words). In the owner's own
# conversation these words decide nothing (:func:`resolve_owner_and_label`).
# ---------------------------------------------------------------------------

_ACTOR_VERBS = (
    r"(?:to\s+\w+|will\s+\w+|must\b|should\b|needs?\s+to\b|has\s+to\b|have\s+to\b|"
    r"owes?\b|owns?\b|decides?\b|approves?\b|confirms?\b|picks?\b|chooses?\b|"
    r"reviews?\b|merges?\b|rebases?\b|renumbers?\b|resolves?\b|fixes\b|answers?\b|"
    r"rules?\b|signs?\b)"
)
_PR = r"(?:PR|pull\s+request)(?:\s*#?\d+)?"
_AGENT_ACTORS: tuple[tuple[str, str], ...] = (
    ("PR author", rf"(?:the\s+)?(?:author|owner)\s+of\s+(?:the\s+)?{_PR}"
                  rf"|(?:the\s+)?{_PR}(?:'s|’s)?\s+(?:author|owner)"),
    (ORCHESTRATOR, r"(?:the\s+)?orchestrator"),
    ("reviewer", r"(?:the\s+)?reviewers?"),
    ("builder", r"(?:the\s+)?builder"),
    ("Codex", r"codex"),
    ("a job", r"(?:the|a|another|the\s+other|the\s+next)\s+job"),
)


@lru_cache(maxsize=1)
def _agent_actor_res() -> tuple[tuple[str, "re.Pattern[str]"], ...]:
    out = []
    for label, actor in _AGENT_ACTORS:
        out.append((label, re.compile(
            rf"\b(?:{actor})(?:'s|’s)\s+(?:action|call|decision|approval|sign-?off|move|turn|job|task)\b"
            rf"|\b(?:{actor})\s+{_ACTOR_VERBS}"
            rf"|\bup\s+to\s+(?:{actor})\b"
            rf"|\bfor\s+(?:{actor})\s+to\s+\w+",
            re.I,
        )))
    return tuple(out)


def agent_actor(text: str) -> str | None:
    """The label of the third party the text names as its actor, or None.
    The earliest match wins when several are named."""
    s = (text or "").strip()
    best: tuple[int, str] | None = None
    for label, pattern in _agent_actor_res():
        m = pattern.search(s)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), label)
    return best[1] if best else None


def clean_owner_label(raw: Any) -> str | None:
    """A model- or caller-supplied owner label as a short plain name, or None."""
    val = " ".join(str(raw or "").split()).strip(" .:")
    return val[:40] or None


def capture_origin(payload: dict[str, Any]) -> str:
    """``job`` for a dispatched job's capture, else ``conversation``.

    A job is a capture with lineage (``parent_session_id``: a Claude Code
    child, a SubagentStop, or a runner that set ``KHIPU_PARENT_SESSION``) or
    one its hook marked as a job (``session_origin: job``, from
    ``KHIPU_JOB=1``; not ``origin``, which is the episode's server-stamped
    owner and is dropped from every payload). Unknown stays
    ``conversation``, the behaviour before origin existed."""
    if str(payload.get("session_origin") or "").strip().lower() == ORIGIN_JOB:
        return ORIGIN_JOB
    if str(payload.get("parent_session_id") or "").strip():
        return ORIGIN_JOB
    return ORIGIN_CONVERSATION


def has_future_trigger(text: str) -> bool:
    """True when the text carries an explicit CROSS-SESSION condition — the
    one thing that makes an assistant promise outlive its own session."""
    return bool(_FUTURE_TRIGGER_RE.search((text or "").strip()))


def trigger_clause(text: str) -> str | None:
    """The future-trigger clause itself — the substring `_without_trigger_clause`
    removes — or None when the text carries no trigger. Stored as
    `commitments.trigger_text` (O1) and shown in `khipu owed` as "until: …" so
    a deferred item's condition is visible without re-reading its full text.

    Kept starting at the matched keyword (``"until the ledger closes"``, not
    just ``"the ledger closes"``) — callers that want the bare clause for
    display use :func:`until_line`, which knows how to avoid repeating that
    keyword against its own label.
    """
    s = (text or "").strip()
    m = _FUTURE_TRIGGER_RE.search(s)
    if not m:
        return None
    comma = s.find(",", m.end())
    end = comma if comma != -1 else len(s)
    clause = s[m.start():end].strip()
    return clause or None


def until_line(clause: str | None) -> str | None:
    """The `khipu owed` "until: …" display line for a trigger clause, or
    None when there is no clause.

    `trigger_clause` keeps its own leading connective (`"until the ledger
    closes"`) so `_without_trigger_clause` can still find where the clause
    starts — but that means a clause already spelled with "until" produced
    "until: until the ledger closes." here (the doubled-connective bug).
    Strip a leading "until" before labelling; a clause introduced by a
    different word (`"before legal signs off"`) is left as-is — "until:
    before …" reads as a label followed by its condition, no word repeated.
    """
    c = (clause or "").strip()
    if not c:
        return None
    c = re.sub(r"(?i)^until\b\s*", "", c).strip()
    return f"until: {c}" if c else None


def _without_trigger_clause(text: str) -> str:
    """The commitment minus its trigger clause. "When Matt says the xAI lane
    is re-authed, run oracle.sh" is the ASSISTANT's promise — Matt is named in
    the condition, not as the actor — so owner detection reads the body only.
    """
    s = (text or "").strip()
    m = _FUTURE_TRIGGER_RE.search(s)
    if not m:
        return s
    comma = s.find(",", m.end())
    tail = s[comma + 1:] if comma != -1 else s[m.end():]
    return (s[: m.start()] + " " + tail).strip()


def normalize_owner(raw: Any) -> str | None:
    """A model-supplied owner mapped onto 'user'/'assistant', or None when it
    is neither ("Peer 1", "", None)."""
    val = str(raw or "").strip().lower().rstrip(":")
    if not val:
        return None
    if val in _compile_user_patterns().owners:
        return OWNER_USER
    if val in ASSISTANT_OWNERS:
        return OWNER_ASSISTANT
    return None


# A bare instruction to run, read or check something is a step the assistant
# or the orchestrator takes ("Run the dry-run on the M1 Mac", "Run `khipu
# doctor` on the secondary Mac", "Look at the 38 routing changes", "Verify
# against live T3 ..."): the extractor marks them "user" or "question" because
# a person is in the conversation, but nobody asked the person. Only an
# instruction with no user named and no decision or personal action asked is
# one, and only a new item is read this way: a stored row keeps the owner it
# has, because an assistant-owned row may be closed by rules that never touch
# the owner's. A wrong miss leaves the item on the owner's list, as before.

_PERSON_STEP_RE = re.compile(
    r"\b(?:you|your|yours|decide|decision|approve|approval|choose|confirm|consent|"
    r"sign(?:s|ed|ing)?[\s-]*(?:in|out)|log(?:s|ged|ging)?[\s-]*(?:in|out)|login|"
    r"touch\s*id|password|passcode|credentials?|pay|payment|purchase|buy)\b", re.I)


def _machine_step(text: str, kind: str | None = None, future_trigger: bool | None = None) -> bool:
    s = (text or "").strip()
    if s.endswith("?") or str(kind or "").lower() == "blocker":
        return False
    body = _without_trigger_clause(s) if (future_trigger is not False) else s
    if not _MACHINE_STEP_RE.match(body) or _PERSON_STEP_RE.search(body):
        return False
    patterns = _compile_user_patterns()
    return not (patterns.actor_re.search(body) or patterns.decision_re.search(body))


def _user_signal(text: str, *, kind: str | None = None, future_trigger: bool | None = None,
                 new_item: bool = False) -> bool:
    s = (text or "").strip()
    if str(kind or "").lower() == "question":
        return not (new_item and _machine_step(s, kind, future_trigger))
    if s.endswith("?"):
        return True
    body = _without_trigger_clause(s) if (future_trigger is not False) else s
    patterns = _compile_user_patterns()
    return bool(patterns.actor_re.search(body) or patterns.decision_re.search(body))


def _declared(raw: Any) -> str | None:
    """The model's declared owner: 'user', 'assistant', 'other' or None."""
    if str(raw or "").strip().lower().rstrip(":") == "other":
        return "other"
    return normalize_owner(raw)


# The grammatical subject who commits (third cross-family review). An item's
# owner is whoever the sentence says will do it, as stated by its speaker,
# read with the session's lineage: the speaker's own "I will" (conditional or
# not) is the speaker's; "Bob will review" or "the reviewer will" names a
# third party, whoever says it and in any session; and in a job the "I" of
# the job's user is the orchestrator that wrote its brief.
_LEAD_IN = r"^\s*(?:(?:then|next|also|and|so|ok(?:ay)?|finally|first|later)\b[,]?\s+)*"
_FIRST_PERSON_RE = re.compile(
    _LEAD_IN + r"I(?:['’]ll\b|['’]m\s+going\s+to\b|\s+(?:will|shall|must|should|can|need\s+to|"
    r"have\s+to|am\s+going\s+to|want\s+to|plan\s+to|promise\s+to|intend\s+to|owe|still\s+owe|"
    r"still\s+need\s+to|will\s+also|also\s+need\s+to)\b)",
    re.I,
)
_NAMED_SUBJECT_RE = re.compile(
    _LEAD_IN + r"([A-Z][a-z]{1,30})\s+(?:will|must|should|needs?\s+to|has\s+to|is\s+going\s+to|"
    r"promised\s+to|owes|to)\s+(?!be\b)[a-z]+")
_NOT_NAMES = frozenset("""
    i we you he she it they this that these those there here the a an someone somebody anyone
    everyone everybody nobody nothing something then next also and so finally first later now
    please when if once after before until while who what which why how each every all both
    tests test docs doc ci it's let make run build check
""".split())

_MACHINE_STEP_RE = re.compile(
    _LEAD_IN + r"(?:run|re-?run|look\s+at|inspect|diagnose|investigate|measure|verify|read)\b", re.I)


def commit_subject(text: str) -> tuple[str, str | None] | None:
    """Who the sentence says commits, as its grammatical subject, or None
    when it names nobody (an imperative, a passive, a question):

    * ``("self", None)``: the speaker ("I will ...", "I'll ...");
    * ``("user", None)``: the user by name or alias ("Matt will ...");
    * ``("agent", label)``: a third party ("the reviewer must ...",
      "PR #12's author to ...", "Bob will ...").

    Only the subject counts: "I will ask the orchestrator to merge" is the
    speaker's, and "Merge once the reviewer approves" names nobody."""
    s = (text or "").strip()
    if _FIRST_PERSON_RE.match(s):
        return "self", None
    lead = re.match(_LEAD_IN, s)
    rest = s[lead.end():] if lead else s
    for label, pattern in _agent_actor_res():
        m = pattern.match(rest)
        if m:
            return OWNER_AGENT, label
    patterns = _compile_user_patterns()
    m = patterns.actor_re.match(rest)
    if m:
        return OWNER_USER, None
    m = _NAMED_SUBJECT_RE.match(s)
    if m:
        name = m.group(1)
        if name.lower() in patterns.owners:
            return OWNER_USER, None
        if name.lower() not in _NOT_NAMES:
            return OWNER_AGENT, name
    return None


def resolve_owner_and_label(text: str, *, kind: str | None = None, declared: Any = None,
                            future_trigger: bool | None = None,
                            origin: str | None = None,
                            declared_label: Any = None,
                            new_item: bool = False) -> tuple[str, str | None]:
    """``(owner, owner_label)`` for one commitment, deterministically. The one
    owner function: capture, the clean-up and reassignment defaults all call
    it, and ``tests/test_commitment_owner.py`` holds its table of cases.

    ``declared`` is the speaker the extractor names (``user``, ``assistant``
    or ``other`` with ``declared_label``); ``origin`` is the session's
    lineage (``job`` for a dispatched job, else the owner's conversation).
    The subject (:func:`commit_subject`) of the body, read without any
    trigger clause ("When the reviewer finishes, I will test it" is the
    speaker's), decides first:

    1. the speaker's own "I will": the user's, or in a job the orchestrator's
       (a job's user is the orchestrator that wrote its brief); the
       assistant's; or the declared third party's;
    2. a named third party, in any session: ``agent`` with its label;
    3. the user named as the subject: the user's, in a job the orchestrator's.

    With no subject, a job's third party named anywhere is that party's; a
    question or decision owed (a user signal) is the user's, in a job the
    orchestrator's; with no future trigger the declared speaker decides; and
    anything else is the assistant's.
    """
    if future_trigger is None:
        future_trigger = has_future_trigger(text)
    s = (text or "").strip()
    job = str(origin or "").strip().lower() == ORIGIN_JOB
    body = _without_trigger_clause(s) if future_trigger is not False else s
    speaker = _declared(declared)
    users = (OWNER_AGENT, ORCHESTRATOR) if job else (OWNER_USER, None)
    subject = commit_subject(body)
    if subject is not None:
        who, label = subject
        if who == "self":
            if speaker == OWNER_USER:
                return users
            if speaker == OWNER_ASSISTANT:
                return OWNER_ASSISTANT, None
            if speaker == "other":
                return OWNER_AGENT, clean_owner_label(declared_label) or (ORCHESTRATOR if job else "other")
        elif who == OWNER_AGENT:
            return OWNER_AGENT, label
        elif who == OWNER_USER:
            return users
    if job:
        label = agent_actor(body)
        if label:
            return OWNER_AGENT, label
    if _user_signal(text, kind=kind, future_trigger=future_trigger, new_item=new_item):
        return users
    if not future_trigger:
        if speaker == "other":
            return OWNER_AGENT, clean_owner_label(declared_label) or (ORCHESTRATOR if job else "other")
        if new_item and speaker == OWNER_USER and _machine_step(body, kind, future_trigger):
            return OWNER_ASSISTANT, None
        if speaker == OWNER_USER:
            return users
        if speaker:
            return speaker, None
    return OWNER_ASSISTANT, None


def resolve_owner(text: str, *, kind: str | None = None, declared: Any = None,
                  future_trigger: bool | None = None, origin: str | None = None) -> str:
    """'user', 'assistant' or 'agent' for one commitment; see
    :func:`resolve_owner_and_label`. The deterministic signal WINS: a
    model-supplied ``declared`` owner is consulted only when the text carries
    no signal at all and no future trigger (a trigger clause is exactly where
    a model picks up the wrong name — "When Matt says …" is not Matt's item).
    """
    return resolve_owner_and_label(text, kind=kind, declared=declared,
                                   future_trigger=future_trigger, origin=origin)[0]


def owner_is_user_sql(table: str = "commitments") -> str:
    """Stored-owner classification used by owner-only SQL reads, like row_owner."""
    names = ", ".join("'" + name + "'" for name in sorted(ASSISTANT_OWNERS))
    return f"lower(rtrim(btrim(coalesce({table}.owner, '')), ':')) NOT IN ({names})"


def row_owner(row: dict[str, Any]) -> tuple[str, str | None]:
    """``(owner, owner_label)`` of a stored row: the owner it was given, read
    the same on every Mac. Capture decides an owner once (:func:`resolve_owner_and_label`)
    and an explicit reassignment replaces it; both write the ``owner`` column,
    so a read never re-derives it from the text or from whose machine reads.

    A row with no recognisable stored owner (opened before migration 0013,
    where a model wrote "Peer 1" or nothing) is the user's: the one default
    that never lets an automatic rule act on an item that may be his.
    """
    owner = str(row.get("owner") or "").strip().lower().rstrip(":")
    if owner == OWNER_AGENT:
        return OWNER_AGENT, clean_owner_label(row.get("owner_label")) or ORCHESTRATOR
    if owner in ASSISTANT_OWNERS:
        return OWNER_ASSISTANT, None
    return OWNER_USER, None


def is_reporting_text(text: str) -> bool:
    """True for a within-session reporting duty (rule 3)."""
    return bool(_compile_user_patterns().reporting_re.search((text or "").strip()))


def owed_priority(owner: str | None, kind: str | None, future_trigger: bool) -> int:
    """Sort rank for the Owed surface (0 = most urgent).

    User-owed first — blocker, question, then anything else the user owes —
    then assistant promises with an explicit future trigger, then the rest by
    kind. The desktop's "Needs you" section is exactly ``owner == 'user'``.
    """
    k = str(kind or "").lower()
    if str(owner or "").lower() == OWNER_USER:
        if k == "blocker":
            return 0
        if k == "question":
            return 1
        return 2
    if future_trigger:
        return 3
    return 4 + KIND_PRIORITY.get(k, len(KIND_PRIORITY))


def _stems(token: str) -> tuple[str, ...]:
    out = [token]
    for suffix, replacement in (
        ("ing", ""), ("ing", "e"), ("ed", ""), ("ed", "e"), ("d", ""),
        ("es", ""), ("s", ""), ("ping", ""), ("ning", ""), ("ting", ""),
        ("ped", ""), ("ned", ""), ("ted", ""),
    ):
        if token.endswith(suffix) and len(token) > len(suffix) + 1:
            out.append(token[: -len(suffix)] + replacement)
    return tuple(out)


def _has_action_verb(text: str) -> bool:
    if _OBLIGATION_RE.search(text):
        return True
    for token in re.findall(r"[a-z]+", (text or "").lower()):
        if any(stem in _ACTION_STEMS for stem in _stems(token)):
            return True
    return False


def rejection_reason(text: str, *, owner: str | None = None,
                     kind: str | None = None) -> str | None:
    """Why this text is NOT a commitment, or None when it passes.

    The deterministic half of the precision fix: it runs before every insert
    (the archived capture writer) and again in the hygiene re-judge, so a model that
    ignores the prompt still cannot fill the table with status chatter.

    ``owner`` is the resolved owner (:func:`resolve_owner`) — derived from the
    text when the caller does not pass one. It gates the REPORTING rule only:
    "reply with the SHAs" is a within-session duty when the assistant owes it,
    and a real commitment when the user does.
    """
    s = (text or "").strip()
    if not s:
        return "empty"
    for reason, pattern in _EXCLUSION_RULES:
        if pattern.search(s):
            return reason
    if owner is None:
        owner = resolve_owner(s, kind=kind)
    if owner != OWNER_USER and is_reporting_text(s) and not _third_party_subject(s, owner):
        return "reporting"
    if len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'./-]*", s)) < MIN_COMMITMENT_WORDS:
        return "too-short"
    if not _has_action_verb(s) and not agent_actor(s):
        # A third party named as the actor of a verb ("PR #234 author to
        # renumber migration 115") names its action by construction.
        return "no-action"
    return None


def _third_party_subject(text: str, owner: str | None) -> bool:
    """True when a third party is the sentence's subject ("The reviewer will
    provide the evidence"): that party's commitment, never the speaker's
    within-session reporting duty."""
    if owner != OWNER_AGENT:
        return False
    subject = commit_subject(_without_trigger_clause(text) if has_future_trigger(text) else text)
    return subject is not None and subject[0] == OWNER_AGENT


def is_commitment_worthy(text: str, *, owner: str | None = None,
                         kind: str | None = None) -> bool:
    return rejection_reason(text, owner=owner, kind=kind) is None


def is_session_plan_text(text: str) -> bool:
    """True when the text is in-flight status / agent chatter / coordination —
    the shapes a session finishes (or abandons) on its own. Used by
    the archived session-close pass to retire anything of that shape that a
    pre-filter build already opened, once its session ends."""
    s = (text or "").strip()
    if not s:
        return False
    return any(pattern.search(s) for _, pattern in _SESSION_PLAN_RULES)


def content_hash(scope: str | None, text: str) -> str:
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha256(f"{scope or ''}\x00{norm}".encode("utf-8")).hexdigest()


def _coalesce_scope(payload: dict[str, Any]) -> str | None:
    """Display project; projectless lifecycle isolation is separate."""
    val = payload.get("project")
    return str(val) if val else None


def _not_forgotten_clause(cur) -> str:
    from khipu.db import has_columns

    if not has_columns(cur, "episodes", "deleted_at"):
        return "1=1"
    return ("NOT EXISTS (SELECT 1 FROM episodes forgotten "
            "WHERE forgotten.id = commitments.opened_episode AND forgotten.deleted_at IS NOT NULL)")


def _lineage_clause(cur, payload: dict[str, Any], episode_id: int | None) -> tuple[str, tuple]:
    """Projectless rows belong to a session lineage, or just their episode."""
    if _coalesce_scope(payload) is not None:
        return "1=1", ()
    from khipu.db import has_columns

    lineage = payload.get("parent_session_id") or payload.get("session_id")
    if lineage and has_columns(cur, "episodes", "session_id"):
        column = "e.session_id"
        if has_columns(cur, "episodes", "parent_session_id"):
            column = "COALESCE(NULLIF(e.parent_session_id, ''), e.session_id)"
        return (f"opened_episode IN (SELECT e.id FROM episodes e WHERE {column} = %s)",
                (lineage,))
    return "opened_episode IS NOT DISTINCT FROM %s", (episode_id,)


def _parse_due_after(raw: Any) -> tuple[str, Any]:
    """Lenient ``due_after`` parsing (fix 1): returns ``(sql_expr, param)`` to
    splice into the commitments INSERT. ISO 8601 / ``YYYY-MM-DD`` -> a literal
    timestamptz bound as a parameter. A bare ``N days|weeks|months`` or
    ``in N days`` -> ``now() + interval`` computed IN SQL (the transaction's
    own clock, not Python's). Anything else (free text like "next week" or
    "after the release", or empty) -> NULL. The model's phrase is never lost
    either way — the caller keeps it in the commitment's own ``text``/note.

    Binding unparseable text straight into the timestamptz column is exactly
    what used to raise ``InvalidDatetimeFormat`` and kill the whole
    commitments step (reproduced live, episode 11308) — this never binds
    anything but a real parsed value or NULL.
    """
    text = str(raw or "").strip()
    if not text:
        return "NULL", None
    iso_candidate = _ISO_Z_RE.sub("+00:00", text)
    try:
        datetime.fromisoformat(iso_candidate)
    except ValueError:
        pass
    else:
        return "%s::timestamptz", text
    m = _RELATIVE_DUE_RE.match(text)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        unit_sql = "days" if unit.startswith("day") else "weeks" if unit.startswith("week") else "months"
        return f"now() + interval '{n} {unit_sql}'", None
    return "NULL", None



REJECTION_WORDS = {
    "empty": "the text is empty",
    "reporting": "it reads as a status report, not something owed",
    "too-short": f"fewer than {MIN_COMMITMENT_WORDS} words, too vague to be a commitment",
    "no-action": "it names no action to take",
}


def _seen_columns_ready(cur) -> bool:
    """True when migration 0012 (last_seen_at / seen_count) has been applied.

    Fail-closed on ANY error — a pre-migration hub, or a fake cursor in a unit
    test, simply skips the touch instead of aborting the capture transaction.
    """
    try:
        from khipu.db import has_columns

        return has_columns(cur, "commitments", "last_seen_at", "seen_count")
    except Exception:  # noqa: BLE001 — introspection is best-effort
        return False


def _future_trigger_ready(cur) -> bool:
    """True when migration 0013 (commitments.future_trigger) has been applied.

    Fail-closed, same posture as :func:`_seen_columns_ready`: a pre-migration
    hub keeps working, it just computes the field in memory instead of
    storing it (every read derives it from the text anyway).
    """
    try:
        from khipu.db import has_columns

        return has_columns(cur, "commitments", "future_trigger")
    except Exception:  # noqa: BLE001 — introspection is best-effort
        return False


def _trigger_text_ready(cur) -> bool:
    """True when migration 0018 (commitments.trigger_text) has been applied.

    Fail-closed, same posture as :func:`_future_trigger_ready`: a
    pre-migration hub keeps working, it just does not persist the clause —
    every read derives it from the text via :func:`trigger_clause` anyway.
    """
    try:
        from khipu.db import has_columns

        return has_columns(cur, "commitments", "trigger_text")
    except Exception:  # noqa: BLE001 — introspection is best-effort
        return False


def _owner_columns_ready(cur) -> bool:
    """True when migration 0033 (commitments.origin / owner_label) has been
    applied. Fail-closed like the others: a pre-migration hub still stores
    the resolved owner (``agent`` included, the column is plain text) and
    answers every read; it only lacks the origin, the label and the relay."""
    try:
        from khipu.db import has_columns

        return has_columns(cur, "commitments", "origin", "owner_label")
    except Exception:  # noqa: BLE001 — introspection is best-effort
        return False


def _reassigned_column_ready(cur) -> bool:
    """True when migration 0035 (``owner_reassigned_at``) has been applied.
    Without it a reassign stores the owner and a read resolves it as before."""
    try:
        from khipu.db import has_columns

        return has_columns(cur, "commitments", "owner_reassigned_at")
    except Exception:  # noqa: BLE001 — introspection is best-effort
        return False


def _lineage_columns_ready(cur) -> bool:
    """True when migration 0037 (reassignment metadata, ``linked_ids`` and
    ``latest_text``) has been applied. Without it a reassign keeps only its
    time, and similar items of different owners stay apart unlinked."""
    try:
        from khipu.db import has_columns

        return has_columns(cur, "commitments", "owner_reassigned_by", "owner_reassign_reason", "owner_prior",
                           "owner_prior_label", "linked_ids", "latest_text")
    except Exception:  # noqa: BLE001 — introspection is best-effort
        return False


def _close_columns_ready(cur) -> bool:
    """True when migration 0034 (prs, closed_by, close_rule_key and the rule
    notes) has been applied. Fail-closed like the others: a pre-migration hub
    still opens, closes and lists; it only lacks the close rules."""
    try:
        from khipu.db import has_columns

        return has_columns(cur, "commitments", "prs", "closed_by", "close_rule_key", "close_evidence",
                           "reopened_rule_keys", "settle_suggestion", "pr_check")
    except Exception:  # noqa: BLE001 — introspection is best-effort
        return False


def _provenance_ready(cur) -> bool:
    """Whether the hub records fact claims (migration 0047). Positive-only
    (``provenance.ready``); a probe that fails reads as not ready, and the
    hub's commit gate then refuses the unprojected write so it is retried."""
    try:
        from khipu import provenance

        return provenance.ready(cur)
    except Exception:  # noqa: BLE001 — introspection is best-effort
        return False


def _lock_open(cur, commitment_id: int) -> bool:
    # forget-fence: commitment writer or write check; returns ids, flags or the locked row it writes
    """Lock a dedup target and re-check that it is still open (section 3.4):
    one that retired after it was read is not a target."""
    cur.execute(
        f"SELECT id FROM commitments WHERE id = %s AND status = 'open' AND {_not_forgotten_clause(cur)} "
        "FOR UPDATE",
        (commitment_id,),
    )
    return cur.fetchone() is not None


_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


# ---- the owner's own items --------------------------------------------------
#
# Historical owner guards still protect explicit bulk Undo.
LOOKS_DONE = "Looks done"


def guarded_owner(row: dict[str, Any]) -> str:
    """The owner an automatic rule's guard reads for a row, deliberately
    conservative: the stored owner, and the user too when the words name him
    as the one to act on an item stored for the assistant. A row stored for a
    third party or set by hand is as stored. Display (:func:`row_owner`) never
    reads the words; only a refusal to close does, so a close can be refused
    that the stored owner alone would permit."""
    stored = row.get("stored_owner", row.get("owner"))
    owner = row_owner({**row, "owner": stored})[0]
    if owner != OWNER_ASSISTANT or row.get("owner_reassigned_at"):
        return owner
    text = row.get("text") or ""
    trigger = has_future_trigger(text) or bool(row.get("future_trigger"))
    named = resolve_owner_and_label(text, kind=row.get("kind"), declared=owner, future_trigger=trigger,
                                    origin=row.get("origin"))[0]
    return OWNER_USER if named == OWNER_USER else owner


def _owner_cols(cur) -> list[str]:
    cols = ["id", "text", "owner", "kind", "status"]
    if _owner_columns_ready(cur):
        cols += ["origin", "owner_label"]
    if _future_trigger_ready(cur):
        cols.append("future_trigger")
    if _reassigned_column_ready(cur):
        cols.append("owner_reassigned_at")
    if _close_columns_ready(cur):
        # What historical close rule's pull request check reads (migration 0034).
        cols += ["project", "prs", "pr_check"]
        if _lineage_columns_ready(cur):
            cols.append("latest_text")
    return cols


def _resolved(row: dict[str, Any]) -> dict[str, Any]:
    """``row`` with its stored owner kept as ``stored_owner`` and ``owner``,
    ``owner_label`` resolved by :func:`row_owner`."""
    row["stored_owner"] = row.get("owner")
    for field in ("prs", "pr_check"):
        if isinstance(row.get(field), str):
            try:
                row[field] = json.loads(row[field])
            except ValueError:
                row[field] = None
    row["owner"], row["owner_label"] = row_owner(row)
    return row


def row_at_write(cur, commitment_id: int) -> dict[str, Any] | None:
    # forget-fence: commitment writer or write check; returns ids, flags or the locked row it writes
    """The item as it is now, its row locked for this transaction (``FOR
    UPDATE``), with the owner resolved; None when it is missing or forgotten.
    Every automatic write decides on this read, never on an earlier one."""
    cols = _owner_cols(cur)
    cur.execute(f"SELECT {', '.join(cols)} FROM commitments WHERE id = %s "
                f"AND {_not_forgotten_clause(cur)} FOR UPDATE", (commitment_id,))
    got = cur.fetchone()
    return _resolved(dict(zip(cols, got))) if got else None


def _owner_unchanged(cur, row: dict[str, Any]) -> tuple[str, tuple]:
    """A WHERE clause the automatic write adds so it changes nothing when the
    stored owner moved after :func:`row_at_write` read it (a writer outside a
    transaction holds no lock)."""
    sql, params = " AND owner IS NOT DISTINCT FROM %s", [row.get("stored_owner")]
    if "owner_reassigned_at" in row:
        sql += " AND owner_reassigned_at IS NOT DISTINCT FROM %s"
        params.append(row["owner_reassigned_at"])
    return sql, tuple(params)


def settle_suggestion_sql(cur, table: str = "commitments") -> str:
    """``settle_suggestion`` as a hub reader may return it: NULL when the
    suggestion cites a forgotten episode, or a decision of one, as its
    evidence (the forget clears those, ``forget._clear_cited_suggestions``;
    this holds for a row written meanwhile or by older code), or cites a
    decision without naming its source episode."""
    col = f"{table}.settle_suggestion"
    try:
        from khipu.db import has_columns

        if not has_columns(cur, "episodes", "deleted_at"):
            return col
        decisions = has_columns(cur, "decisions", "episode_id")
    except Exception:  # noqa: BLE001 - a failed probe reads as not migrated
        return col
    episode = _json_id_sql(col + "->>'episode'")
    forgotten = f"SELECT 1 FROM episodes fe WHERE fe.deleted_at IS NOT NULL AND fe.id = {episode}"
    if decisions:
        for decision in (col + "->>'decision_id'", col + "->'evidence'->>'decision_id'"):
            forgotten += f" UNION ALL {_forgotten_decision_sql(_json_id_sql(decision))}"
    # A suggestion quoting a decision names the episode its text came from
    # (``episode``); one without it cannot be checked, as the decision may
    # have moved to another source since, so it is not shown.
    unsourced = (f"({col}->>'decision_id' IS NOT NULL OR {col}->'evidence'->>'decision_id' IS NOT NULL) "
                 f"AND {col}->>'episode' IS NULL")
    return (f"CASE WHEN {unsourced} THEN NULL "
            f"WHEN EXISTS ({forgotten}) "
            f"THEN NULL ELSE {col} END")


def _json_id_sql(text_expr: str) -> str:
    """A JSON text id as a bigint (NULL when it is not one), so the lookup
    uses the primary key: ``id::text = ...`` scanned every decision once per
    commitment, and the open list ran past the hub's statement timeout."""
    return f"(CASE WHEN {text_expr} ~ '^[0-9]{{1,18}}$' THEN ({text_expr})::bigint END)"


def _forgotten_decision_sql(id_expr: str) -> str:
    return (f"SELECT 1 FROM decisions d JOIN episodes fe ON fe.id = d.episode_id "
            f"WHERE fe.deleted_at IS NOT NULL AND d.id = {id_expr}")


def close_reason_sql(cur, table: str = "commitments") -> str:
    """``close_reason`` as a hub reader may return it: 'forgotten' when the
    close was made by a forgotten episode or cites a decision of one (the
    forget rewrites those, ``forget._clear_cited_closes``; this holds for a
    row written meanwhile or by older code)."""
    col = f"{table}.close_reason"
    try:
        from khipu.db import has_columns

        if not has_columns(cur, "episodes", "deleted_at"):
            return col
        forgotten = f"SELECT 1 FROM episodes fe WHERE fe.deleted_at IS NOT NULL AND fe.id = {table}.closed_episode"
        if has_columns(cur, "decisions", "episode_id") and has_columns(cur, "commitments", "close_evidence"):
            forgotten += (" UNION ALL "
                          + _forgotten_decision_sql(_json_id_sql(f"{table}.close_evidence->>'decision_id'")))
    except Exception:  # noqa: BLE001 - a failed probe reads as not migrated
        return col
    return f"CASE WHEN EXISTS ({forgotten}) THEN 'forgotten' ELSE {col} END"






def _as_utc(value: Any) -> datetime | None:
    from datetime import timezone

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(_ISO_Z_RE.sub("+00:00", value.strip().replace(" ", "T", 1)))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def quiet_weeks(row: dict[str, Any], now: datetime | None = None) -> int | None:
    """Whole weeks an open item of the owner's has gone unmentioned, once
    that is past the window that would make anyone else's item stale; None
    otherwise. His items never go stale (:func:`historical aging`); this is how a
    list shows the quiet ones, after the active ones, instead of hiding them."""
    from datetime import timezone

    if row.get("status") not in (None, "open") or row.get("owner") != OWNER_USER:
        return None
    now = now or datetime.now(timezone.utc)
    due = _as_utc(row.get("due_after"))
    if due and due > now:
        return None
    seen = _as_utc(row.get("last_seen_at")) or _as_utc(row.get("opened_at"))
    if seen is None:
        return None
    seen = max(seen, due) if due else seen
    window = STALE_AFTER_DAYS_SOFT if str(row.get("kind") or "").lower() in ("followup", "promise") \
        else STALE_AFTER_DAYS
    days = (now - seen).total_seconds() / 86400
    return int(days // 7) if days > window else None


def quiet_label(weeks: int | None) -> str | None:
    return f"Quiet for {weeks} weeks" if weeks else None


def list_owed(cur, *, project: str | None = None, parent_session_id: str | None = None,
              session_id: str | None = None, status: str = "open",
              limit: int | None = 50, hide_snoozed: bool = False) -> list[dict[str, Any]]:
    """The owed rows of one scope in priority order, at most ``limit``; see
    :func:`fetch_owed_rows` for the scope."""
    rows = fetch_owed_rows(cur, project=project, parent_session_id=parent_session_id,
                           session_id=session_id, status=status, hide_snoozed=hide_snoozed)
    return finish_owed_rows(rows, limit)


def fetch_owed_rows(cur, *, project: str | None = None, parent_session_id: str | None = None,
                    session_id: str | None = None, status: str = "open",
                    hide_snoozed: bool = False) -> list[dict[str, Any]]:
    """Every raw row of one scope, newest first, owners not yet resolved.
    ``project`` if given, else ``parent_session_id``/``session_id`` (fix
    3) — the same coalesced key the archived writer store
    commitments under, so a caller with only session context (no resolved
    project) can still find its own scope's commitments.

    ``hide_snoozed`` (O3): when true, a row whose ``due_after`` is still in
    the future is excluded — this is what makes ``khipu owed --snooze``
    actually park an item instead of it reappearing everywhere immediately.
    Off by default so ``khipu owed``/the desktop still see (and can
    un-snooze) a parked row; the W4 pushed slice turns it on.
    """
    clauses = ["status = %s"]
    params: list[Any] = [status]
    canonical = None
    if project:
        # An aliased key reads as its canonical project: every member of the
        # alias group matches, whichever key the caller passed.
        from khipu.identity import project_canonical_group

        canonical, group = project_canonical_group(cur, project)
        clauses.append("project = %s" if len(group) == 1 else "project = ANY(%s)")
        params.append(group[0] if len(group) == 1 else group)
    elif parent_session_id or session_id:
        lineage, lineage_params = _lineage_clause(
            cur, {"parent_session_id": parent_session_id, "session_id": session_id}, None)
        clauses += ["project IS NULL", lineage]
        params.extend(lineage_params)
    if hide_snoozed:
        clauses.append("(due_after IS NULL OR due_after <= now())")
    clauses.append(_not_forgotten_clause(cur))
    # Owner and trigger priority are resolved in Python, including legacy rows.
    # SQL must not truncate the population before that resolution.
    params.append(None)
    cols = ["id", "text", "project", "owner", "kind", "opened_episode", "opened_at",
            "due_after", "status", "closed_episode", "closed_at", "close_reason"]
    # Additive only (migration 0012). A pre-migration hub still answers, with
    # last_seen_at NULL and seen_count 1 — the desktop reads the same shape
    # either way and never has to branch on schema version.
    extra = _seen_columns_ready(cur)
    select_cols = list(cols) + (["last_seen_at", "seen_count"] if extra else [])
    if _future_trigger_ready(cur):
        select_cols.append("future_trigger")
    if _owner_columns_ready(cur):
        select_cols += ["origin", "owner_label"]
    if _close_columns_ready(cur):
        select_cols += ["prs", "closed_by", "close_rule_key", "settle_suggestion", "pr_check"]
    if _reassigned_column_ready(cur):
        select_cols.append("owner_reassigned_at")
    if _lineage_columns_ready(cur):
        select_cols += ["owner_reassigned_by", "owner_reassign_reason", "owner_prior", "owner_prior_label",
                        "linked_ids", "latest_text"]
    exprs = [settle_suggestion_sql(cur) if c == "settle_suggestion"
             else f"{close_reason_sql(cur)} AS close_reason" if c == "close_reason" else c for c in select_cols]
    cur.execute(
        f"""
        SELECT {', '.join(exprs)}
        FROM commitments
        WHERE {' AND '.join(clauses)}
        ORDER BY opened_at DESC, id DESC
        LIMIT %s
        """,
        params,
    )
    out = _fenced_owed_rows([dict(zip(select_cols, row)) for row in cur.fetchall()])
    if canonical:
        for row in out:
            if row.get("project"):
                row["project"] = canonical
    return out


def _fenced_owed_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Owed rows with this Mac's forget list applied: the hub learns of a
    forget after the list does, so ``_not_forgotten_clause`` and
    ``settle_suggestion_sql`` alone let a just-forgotten episode's row or a
    suggestion quoting it through. As on the hub, a row goes with its
    opening episode; one closed by a forgotten episode stays, its close
    reason read as 'forgotten' (``forget._clear_cited_closes``). An
    unreadable list shows no row tied to an episode."""
    from khipu.tombstones import episode_withheld, withhold

    kept = withhold([{"episode_id": row.get("opened_episode"), "row": row} for row in rows],
                    fail_closed=False)
    rows = [k["row"] for k in kept]
    for row in rows:
        if row.get("closed_episode") is not None and row.get("close_reason") \
                and episode_withheld(row["closed_episode"]):
            row["close_reason"] = "forgotten"
        suggestion = row.get("settle_suggestion")
        if isinstance(suggestion, str):
            try:
                suggestion = json.loads(suggestion)
            except ValueError:
                suggestion = None
        if isinstance(suggestion, dict) and suggestion.get("episode") is not None \
                and episode_withheld(suggestion["episode"]):
            row["settle_suggestion"] = None
    return rows


def finish_owed_rows(rows: list[dict[str, Any]], limit: int | None) -> list[dict[str, Any]]:
    """Owner, trigger, priority and order for raw commitment rows, the same
    for a hub read and an offline read of the replica (``offline_reads``)."""
    resolve_owed_rows(rows)
    rows = [row for row in rows if row["owner"] == OWNER_USER]
    # O3: age x kind — within a priority tier (kind), the LONGEST-outstanding
    # item leads (opened_at ascending), not the newest one the SQL fetched
    # first. A row with no opened_at (should not happen; NOT NULL) sorts
    # last within its tier rather than raising.
    from datetime import datetime, timezone

    _max_dt = datetime.max.replace(tzinfo=timezone.utc)
    # The owner's quiet items follow the active ones.
    rows.sort(key=lambda r: (r["owner"] != OWNER_USER, bool(r.get("quiet_weeks")), r["priority"],
                             r.get("opened_at") or _max_dt))
    return rows[:limit]


def resolve_owed_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Owner, trigger and priority for raw commitment rows, in place."""
    for row in rows:
        if row.get("kind") == "routing_direction":
            row["owner"] = OWNER_USER
            set_quiet(row)
            continue
        row.setdefault("last_seen_at", None)
        row["seen_count"] = int(row.get("seen_count") or 1)
        # The owner is the stored one (row_owner), the same on every Mac; the
        # future trigger is read from the text, since rows opened before
        # migration 0013 carry none.
        trigger = has_future_trigger(row.get("text") or "") or bool(row.get("future_trigger"))
        row["future_trigger"] = trigger
        row["owner"], row["owner_label"] = row_owner(row)
        row.setdefault("origin", None)
        # Migration 0034's fields, the same shape on a hub without them. A
        # looser decision match is offered, never applied (design (b)).
        for key in ("prs", "closed_by", "close_rule_key", "settle_suggestion", "pr_check"):
            row.setdefault(key, None)
        suggestion = row["settle_suggestion"] if isinstance(row["settle_suggestion"], dict) else None
        # A close a rule would have made to the owner's item is kept as a
        # "Looks done" suggestion for him to close (suggest_looks_done).
        done = suggestion if suggestion and suggestion.get("looks_done") else None
        row["looks_done"] = ({k: done.get(k) for k in ("rule", "reason", "evidence")}
                             if done and row.get("status") in (None, "open") else None)
        row["looks_settled"] = (f"Looks settled by decision {suggestion['decision_id']}"
                                if suggestion and not done and suggestion.get("decision_id") else None)
        # Sort rank for the Owed surface: user-owed first (blocker → question →
        # everything else the user owes), then assistant promises with an
        # explicit future trigger, then the rest by kind. Computed here so the
        # desktop/gateway never re-derive (and never disagree with) it.
        row["priority"] = owed_priority(row["owner"], row.get("kind"), trigger)
        # O1: the deferral clause itself, recomputed from the text (same
        # "deterministic wins" posture as owner/future_trigger above) so a
        # row written before migration 0018 shows it too. `until` is the
        # human-readable line `khipu owed` prints; `trigger_text` is the bare
        # clause for callers that want to reformat it themselves.
        clause = trigger_clause(row.get("text") or "") if trigger else None
        row["trigger_text"] = clause
        row["until"] = until_line(clause)
        set_quiet(row)
    return rows


def set_quiet(row: dict[str, Any], now: datetime | None = None) -> None:
    """``quiet_weeks`` and the list ``group`` ("Looks done" first, else "Quiet
    for N weeks") of one resolved row, as of ``now``."""
    row["quiet_weeks"] = quiet_weeks(row, now)
    row["group"] = LOOKS_DONE if row.get("looks_done") else quiet_label(row["quiet_weeks"])


def add_hub_ages(rows: list[dict[str, Any]], hub_now: Any) -> list[dict[str, Any]]:
    """Adds ``opened_age_s`` and ``last_seen_age_s``: seconds from each time to
    ``hub_now``, the hub's clock at the read. A reader adds the seconds since
    it read the list on its own clock, so the two clocks never meet in one
    subtraction. None when either time is missing; never negative."""
    for row in rows:
        for key, out in (("opened_at", "opened_age_s"), ("last_seen_at", "last_seen_age_s")):
            at = row.get(key)
            try:
                row[out] = max(0.0, (hub_now - at).total_seconds())
            except (TypeError, AttributeError):
                row[out] = None
    return rows


def set_status(cur, commitment_id: int, status: str, *, reason: str | None = None,
               by: str | None = None) -> bool:
    # forget-fence: commitment writer or write check; returns ids, flags or the locked row it writes
    """Close, reopen, stale or drop one commitment by hand. A close keeps its
    ``reason`` (default "closed by hand") and, once migration 0034 is in,
    ``by`` (:data:`BY_APP`, :data:`BY_CONVERSATION`). A reopen restores the
    item and remembers the rule key that closed it, so the same rule and
    evidence never close it again (historical closure evidence)."""
    if status not in ("open", "closed", "stale", "dropped"):
        raise ValueError(f"status must be open/closed/stale/dropped, got {status!r}")
    close_ready = _close_columns_ready(cur)
    if status == "open":
        refreshed = "last_seen_at = now()" if _seen_columns_ready(cur) else "opened_at = now()"
        rule = ""
        if close_ready:
            rule = (", reopened_rule_keys = CASE WHEN close_rule_key IS NULL "
                    "OR close_rule_key = ANY(reopened_rule_keys) THEN reopened_rule_keys "
                    "ELSE array_append(reopened_rule_keys, close_rule_key) END, "
                    "closed_by = NULL, close_rule_key = NULL, close_evidence = NULL")
        cur.execute(
            "UPDATE commitments SET status = 'open', closed_episode = NULL, "
            f"closed_at = NULL, close_reason = NULL{rule}, {refreshed} WHERE id = %s "
            f"AND {_not_forgotten_clause(cur)} "
            "AND NOT EXISTS (SELECT 1 FROM commitments other "
            "WHERE other.id != commitments.id AND other.status = 'open' "
            "AND other.project IS NOT DISTINCT FROM commitments.project "
            "AND other.content_hash = commitments.content_hash)",
            (commitment_id,),
        )
        reopened = cur.rowcount > 0
        if close_ready and reopened:
            # Reopened after a close from "Looks done": that evidence was wrong,
            # so it is withdrawn and never suggested again (suggest_looks_done).
            cur.execute(
                "UPDATE commitments SET reopened_rule_keys = CASE "
                "WHEN settle_suggestion->>'key' IS NULL "
                "OR settle_suggestion->>'key' = ANY(reopened_rule_keys) THEN reopened_rule_keys "
                "ELSE array_append(reopened_rule_keys, settle_suggestion->>'key') END, "
                "settle_suggestion = NULL WHERE id = %s AND settle_suggestion IS NOT NULL",
                (commitment_id,),
            )
        return reopened
    else:
        extra = ", closed_at = now()" if status == "closed" else ""
        params: list[Any] = [status]
        if status in ("closed", "dropped"):
            stored = (reason or "").strip()[:300] or f"{status} by hand"
            extra += ", close_reason = %s"
            params.append(stored)
            if close_ready:
                # A close that quotes the "Looks done" suggestion (the app
                # sends it prefixed, "Looked done: <reason>") keeps its
                # evidence and episode, so a forget of that episode or its
                # decision still finds and clears the quote.
                quotes = ("btrim(settle_suggestion->>'reason') <> '' "
                          "AND position(btrim(settle_suggestion->>'reason') in %s) > 0")
                extra += (", closed_by = %s, close_rule_key = NULL, close_evidence = CASE "
                          f"WHEN {quotes} THEN settle_suggestion->'evidence' END, "
                          f"closed_episode = CASE WHEN {quotes} "
                          "AND settle_suggestion->>'episode' IS NOT NULL "
                          "THEN (settle_suggestion->>'episode')::bigint ELSE closed_episode END")
                params.extend([by, stored, stored])
        cur.execute(
            f"UPDATE commitments SET status = %s{extra} WHERE id = %s "
            f"AND {_not_forgotten_clause(cur)}",
            (*params, commitment_id),
        )
    return cur.rowcount > 0


# ---- paging, filters and bulk changes (design (c), slice 3) -----------------

OWNERS = (OWNER_USER, OWNER_ASSISTANT, OWNER_AGENT)
AGENT_TODOS_REMOVED = "To-do lists for agents were removed; use ask for something the owner must decide."
ORIGINS = (ORIGIN_CONVERSATION, ORIGIN_JOB)
# How the owner gate passed for a bulk change to the owner's items: the app
# or a typed yes (``bulk_owner_gate``), or the owner's direction in a
# conversation (``mcp_server.owed_change``, ``owner_direction.gate``). The hub
# accepts such a change only with one of these on the write.
OWNER_GATE_PASSES = ("app", "terminal", "direction")
BULK_OWNER_REFUSAL = ("Changing more than one of the owner's own items at once needs the owner: "
                      "do it in the Khipu app, type yes on a terminal, or ask in a conversation the owner is "
                      "typing in (khipu_owner_act with owed.change). Nothing was changed.")


def _opened_key(row: dict[str, Any]) -> tuple[Any, tuple[int, Any]]:
    from datetime import datetime, timezone

    at = row.get("opened_at")
    if isinstance(at, str):
        try:
            at = datetime.fromisoformat(_ISO_Z_RE.sub("+00:00", at))
        except ValueError:
            at = None
    if not isinstance(at, datetime):
        at = datetime.min.replace(tzinfo=timezone.utc)
    elif at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    cid = row.get("id")
    # Typed tie-breaks preserve numeric order while admitting direction IDs.
    key = (1, cid) if isinstance(cid, str) and cid.startswith("rd-") else (0, int(cid or 0))
    return at, key


def _page_key(row: dict[str, Any]) -> tuple[int, Any, tuple[int, Any]]:
    """The keyset, sorted descending: active rows (1) before the owner's quiet
    ones (0), each newest first."""
    return (0 if row.get("quiet_weeks") else 1, *_opened_key(row))


def encode_cursor(row: dict[str, Any], as_of: datetime | None = None) -> str:
    """The cursor after ``row``. ``as_of`` is the clock the first page read
    quiet items by; later pages read them by the same clock, so an item that
    turns quiet between pages neither repeats nor goes missing."""
    import base64

    active, at, _ = _page_key(row)
    parts = [at.isoformat(), row["id"], active] + ([as_of.isoformat()] if as_of else [])
    return base64.urlsafe_b64encode(json.dumps(parts).encode()).decode().rstrip("=")


def cursor_as_of(raw: str | None) -> datetime | None:
    """The clock a cursor's first page read quiet items by, or None."""
    import base64

    if not raw:
        return None
    try:
        parts = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode())
        return _as_utc(parts[3]) if len(parts) > 3 else None
    except (ValueError, TypeError, UnicodeDecodeError, IndexError):
        return None


def decode_cursor(raw: str) -> tuple[int, Any, tuple[int, Any]]:
    """``(active, opened_at, id)`` of the last row a page gave; a cursor from
    before the quiet group counts as active. ValueError otherwise."""
    import base64
    from datetime import datetime

    try:
        text = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
        at, cid, *active = json.loads(text)
        if not (isinstance(cid, str) and cid.startswith("rd-")):
            cid = int(cid)
        return _page_key({"opened_at": datetime.fromisoformat(at), "id": cid,
                          "quiet_weeks": None if (active or [1])[0] else 1})
    except (ValueError, TypeError, UnicodeDecodeError):
        raise ValueError("cursor is not one khipu_owed gave; start again without it") from None


def _row_prs(row: dict[str, Any]) -> list[int]:
    if isinstance(row.get("prs"), list):
        return [int(n) for n in row["prs"]]
    from khipu.pr_refs import parse_prs

    return parse_prs(row.get("text") or "")


def page_owed(rows: list[dict[str, Any]], *, owner: str | None = None, kind: str | None = None,
              origin: str | None = None, query: str | None = None, pr: int | None = None,
              cursor: str | None = None, limit: int | None = 50,
              looks_done: bool | None = None) -> dict[str, Any]:
    """One page of raw owed rows (``fetch_owed_rows`` or the replica's), newest
    first on the keyset (``opened_at``, ``id``), so a page never skips or
    repeats a row while new ones arrive (they sort ahead of every cursor).
    Owners are resolved first, so the owner filter sees what the app shows.
    ``total`` counts every row that matches; ``counts`` holds how many each
    owner and kind has among the rows the other filters match (the owner
    counts ignore the owner filter, the kind counts the kind filter), so a
    caller sees what the other headings hold, and ``counts["looks_done"]``
    how many of them look done. ``looks_done`` True keeps only those. The
    quiet group is read by the first page's clock throughout (``as_of`` in
    the cursor). ``limit`` None or 0: no limit."""
    from datetime import timezone

    resolve_owed_rows(rows)
    rows = [row for row in rows if row["owner"] == OWNER_USER]
    as_of = cursor_as_of(cursor) or datetime.now(timezone.utc)
    for row in rows:
        set_quiet(row, as_of)
    words = [w for w in (query or "").lower().split() if w]

    def keep(row: dict[str, Any], *, skip: str = "") -> bool:
        if skip != "owner" and owner and owner != "all" and row["owner"] != owner:
            return False
        if skip != "kind" and kind and row.get("kind") != kind:
            return False
        if origin and (row.get("origin") or ORIGIN_CONVERSATION) != origin:
            return False
        if words and not all(w in (row.get("text") or "").lower() for w in words):
            return False
        if looks_done and not row.get("looks_done"):
            return False
        return not (pr is not None and pr not in _row_prs(row))

    counts: dict[str, Any] = {"owner": {}, "kind": {}, "looks_done": 0}
    for row in rows:
        if keep(row, skip="owner"):
            counts["owner"][row["owner"]] = counts["owner"].get(row["owner"], 0) + 1
        if keep(row, skip="kind"):
            k = str(row.get("kind") or "followup")
            counts["kind"][k] = counts["kind"].get(k, 0) + 1
        if keep(row) and row.get("looks_done"):
            counts["looks_done"] += 1
    matched = sorted((r for r in rows if keep(r)), key=_page_key, reverse=True)
    after = matched
    if cursor:
        last = decode_cursor(cursor)
        after = [r for r in matched if _page_key(r) < last]
    page = after[:limit] if limit else after
    more = len(after) > len(page)
    return {"results": page, "total": len(matched), "counts": counts,
            "next_cursor": encode_cursor(page[-1], as_of) if more and page else None}


def owners_of(cur, ids: list[int], *, lock: bool = False) -> dict[int, dict[str, Any]]:
    # forget-fence: owner lookup that gates a write; the caller reads only the owner
    """``{id: row}`` with the resolved owner for the given ids (forgotten
    and missing ids left out); ``lock`` holds the rows for this transaction."""
    cols = _owner_cols(cur)
    cur.execute(f"SELECT {', '.join(cols)} FROM commitments WHERE id = ANY(%s) "
                f"AND {_not_forgotten_clause(cur)}{' FOR UPDATE' if lock else ''}", (list(ids),))
    return {int(raw[0]): _resolved(dict(zip(cols, raw))) for raw in cur.fetchall()}


def reassign(cur, commitment_id: int, owner: str, label: str | None = None, *,
             by: str | None = None, reason: str | None = None) -> bool:
    """Explicitly assign an archived item to the owner, retaining its lineage.
    Agent and assistant assignment was removed; archive ownership is still read
    unchanged for replication, provenance and owner-directed undo.
    """
    if owner in (OWNER_ASSISTANT, OWNER_AGENT):
        raise ValueError(AGENT_TODOS_REMOVED)
    if owner != OWNER_USER:
        raise ValueError("owner must be user")
    sets, params = ["owner = %s"], [owner]
    if _owner_columns_ready(cur):
        sets.append("owner_label = %s")
        params.append(None)
    if _lineage_columns_ready(cur):
        # The right-hand sides read the row as it was before this UPDATE.
        sets[:0] = ["owner_prior = owner", "owner_prior_label = owner_label",
                    "owner_reassigned_by = %s", "owner_reassign_reason = %s"]
        params[:0] = [str(by or BY_CONVERSATION)[:80], (str(reason)[:400] if reason else None)]
    if _reassigned_column_ready(cur):
        sets.append("owner_reassigned_at = now()")
    cur.execute(f"UPDATE commitments SET {', '.join(sets)} WHERE id = %s AND {_not_forgotten_clause(cur)}",
                (*params, commitment_id))
    return cur.rowcount > 0


def bulk_owner_refusal() -> str:
    from khipu import routing

    return routing.terminal_refusal(BULK_OWNER_REFUSAL)


def bulk_owner_gate(count: int) -> str | None:
    """The owner gate for a change to more than one of the owner's own items
    (decision 6): ``"app"`` when the Khipu app itself started this process,
    ``"terminal"`` after the owner's yes typed on a terminal, else None. The
    same two checks as the ledger's owner verbs (``ledger.require``), made
    through routing's own functions, never reimplemented here. The owner's
    direction through the agent is the third way past it, asked for
    explicitly (``mcp_server.owed_change(by_direction=True)``)."""
    from khipu import routing

    if routing._caller()["app"]:
        return "app"
    if routing.typed_yes(f"This changes {count} items at once, some of them the owner's own. "
                         "Type yes to confirm: "):
        return "terminal"
    return None

def _normalize_open_loop(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        text = item.strip()
        if not text:
            return None
        return {"text": text, "kind": "followup", "due_after": None, "owner": None,
                "owner_label": None, "future_trigger": None}
    if not isinstance(item, dict):
        return None
    text = str(item.get("text") or "").strip()
    if not text:
        return None
    kind = str(item.get("kind") or "followup").strip().lower()
    if kind not in VALID_KINDS:
        kind = "followup"
    return {
        "text": text,
        "kind": kind,
        "due_after": item.get("due_after") or None,
        "owner": item.get("owner") or None,
        "owner_label": item.get("owner_label") or None,
        # Asked for in the prompt, kept for the record — the deterministic
        # detector decides the stored value (see resolve_owner/has_future_trigger).
        "future_trigger": item.get("future_trigger"),
    }
