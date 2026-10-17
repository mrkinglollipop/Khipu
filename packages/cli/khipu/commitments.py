"""First-class commitments (W3) — open loops that outlive a single episode.

``decisions`` today are immutable strings in a JSONB array: nothing can be
opened, closed, superseded (root cause D). This module gives capture-time
``open_loops`` / ``closed_loops`` a lifecycle in the ``commitments`` table
(migration 0009): opened by an episode, auto-closed by a later episode's
closed_loop / decision / explicit ``done:`` prefix in the same project, or
aged into ``stale`` after 30 days untouched.

Every function here is called from the capture write path
(``khipu.capture.write_pg``) and must stay fail-open: an exception here must
never take down an episode insert that already succeeded.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime
from functools import lru_cache
from typing import Any

from khipu.capture import _jaccard  # shared with capture's own dedup (no byte copy)

VALID_KINDS = ("followup", "blocker", "question", "promise")
STALE_AFTER_DAYS = 30           # blockers / questions: a long-lived shape
STALE_AFTER_DAYS_SOFT = 14      # followups / promises with no due date
DONE_MATCH_MIN_SCORE = 0.2  # fix 4: a minimal bar even for an explicit "done:" close
NEAR_DUP_MIN_SCORE = 0.5    # quality: token-containment bar for "already open"
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

COMMITMENT_DEFINITION = """A commitment is ONLY one of:
  (a) something the USER must decide, provide, approve, or do; or
  (b) something the ASSISTANT explicitly promised to do in a FUTURE session
      and cannot finish in this one.
Everything else is NOT a commitment. In particular, never record:
  - in-progress status of any kind: "is running", "still building", "pending",
    "in flight", "waiting on/for", "awaiting";
  - anything about agents, subagents, drives, notifications, reports arriving,
    verdicts, or polling;
  - inter-session or inter-agent coordination: "send/receive ... message",
    "screen free/busy", "ping the session";
  - steps of the assistant's own plan for THIS session (it will finish them
    before the session ends);
  - anything this same window reports as already completed;
  - vague items with no concrete action or actor."""

# Each rule is (reason, pattern). The reason is reported verbatim by
# `khipu hygiene commitments` so a human reviewing a dry run can see WHY.
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
# (:func:`close_session_plan`). Detection is DETERMINISTIC here; the extractor
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
# ---------------------------------------------------------------------------


def _git_user_first_name() -> str | None:
    """First token of `git config --global user.name`, lowercased. Fail-open
    (no git, no config, timeout) — this is a DEFAULT alias source, never a
    hard requirement."""
    try:
        out = subprocess.run(
            ["git", "config", "--global", "user.name"],
            capture_output=True, text=True, timeout=2,
        )
    except Exception:  # noqa: BLE001 — fail-open: no derived alias
        return None
    name = (out.stdout or "").strip()
    if not name:
        return None
    first = name.split()[0].strip().lower()
    return first or None


def user_aliases() -> tuple[str, ...]:
    """Every name that means "the user" on this hub: lowercased, de-duplicated.

    Order: the configured list (``khipu config --set user_aliases
    "matt,matthew"`` / ``KHIPU_USER_ALIASES``), then derived defaults (first
    token of the machine's git ``user.name``, then ``$USER``), then the
    generic words that always mean the user regardless of configuration.
    """
    from khipu.config import list_setting

    out: list[str] = []
    seen: set[str] = set()

    def _add(raw: str | None) -> None:
        val = (raw or "").strip().lower()
        if val and val not in seen:
            seen.add(val)
            out.append(val)

    for alias in list_setting("user_aliases"):
        _add(alias)
    _add(_git_user_first_name())
    _add(os.environ.get("USER"))
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


def _user_signal(text: str, *, kind: str | None = None, future_trigger: bool | None = None) -> bool:
    s = (text or "").strip()
    if str(kind or "").lower() == "question":
        return True
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


def resolve_owner_and_label(text: str, *, kind: str | None = None, declared: Any = None,
                            future_trigger: bool | None = None,
                            origin: str | None = None,
                            declared_label: Any = None) -> tuple[str, str | None]:
    """``(owner, owner_label)`` for one commitment, deterministically.

    The owner comes from who said it and in which session, never from the
    words alone. In a job (``origin == 'job'``) the speaker typing as "the
    user" is known to be the orchestrator that wrote the brief, so:

    1. a third party named as the actor ("the orchestrator must", "PR #234
       author to") is that party's: ``agent`` with its label;
    2. a user signal (a question, a decision shape, "Matt to decide X") or a
       model-declared ``"user"`` goes to the orchestrator; it asks the user
       itself if it really needs them.

    In the owner's own conversation (any other origin) the words never move
    an item away from him: his "I will ask the orchestrator to ..." is his,
    and a question to him stays his whatever it mentions. A user signal is
    the user's; with no signal and no future trigger the model's declared
    speaker decides ("other" with its label becomes ``agent``); else the
    assistant's.
    """
    if future_trigger is None:
        future_trigger = has_future_trigger(text)
    s = (text or "").strip()
    job = str(origin or "").strip().lower() == ORIGIN_JOB
    if job:
        body = _without_trigger_clause(s) if future_trigger is not False else s
        label = agent_actor(body)
        if label:
            return OWNER_AGENT, label
    if _user_signal(text, kind=kind, future_trigger=future_trigger):
        return (OWNER_AGENT, ORCHESTRATOR) if job else (OWNER_USER, None)
    if not future_trigger:
        norm = _declared(declared)
        if norm == "other":
            return OWNER_AGENT, clean_owner_label(declared_label) or (ORCHESTRATOR if job else "other")
        if norm == OWNER_USER:
            return (OWNER_AGENT, ORCHESTRATOR) if job else (OWNER_USER, None)
        if norm:
            return norm, None
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


def row_owner(row: dict[str, Any], *, future_trigger: bool | None = None) -> tuple[str, str | None]:
    """``(owner, owner_label)`` for a stored row. A stored ``agent`` owner is
    authoritative (only the rules above, or an explicit reassign, write it,
    and its text may no longer show why, as with a job's "Matt to decide X");
    anything else is re-resolved from the text, with the row's origin."""
    if str(row.get("owner") or "").strip().lower() == OWNER_AGENT:
        return OWNER_AGENT, (clean_owner_label(row.get("owner_label"))
                             or agent_actor(row.get("text") or "") or ORCHESTRATOR)
    # A reassigned owner (migration 0035) is kept as written.
    if row.get("owner_reassigned_at") and row.get("owner") in (OWNER_USER, OWNER_ASSISTANT):
        return str(row["owner"]), None
    return resolve_owner_and_label(
        row.get("text") or "", kind=row.get("kind"), declared=row.get("owner"),
        future_trigger=future_trigger, origin=row.get("origin"),
        declared_label=row.get("owner_label"),
    )


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
    (``open_from_episode``) and again in the hygiene re-judge, so a model that
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
    if owner != OWNER_USER and is_reporting_text(s):
        return "reporting"
    if len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'./-]*", s)) < MIN_COMMITMENT_WORDS:
        return "too-short"
    if not _has_action_verb(s) and not agent_actor(s):
        # A third party named as the actor of a verb ("PR #234 author to
        # renumber migration 115") names its action by construction.
        return "no-action"
    return None


def is_commitment_worthy(text: str, *, owner: str | None = None,
                         kind: str | None = None) -> bool:
    return rejection_reason(text, owner=owner, kind=kind) is None


def is_session_plan_text(text: str) -> bool:
    """True when the text is in-flight status / agent chatter / coordination —
    the shapes a session finishes (or abandons) on its own. Used by
    :func:`close_session_plan` to retire anything of that shape that a
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


def _open_id_for_hash(cur, scope: str | None, content_h: str) -> int | None:
    """id of the open commitment with this exact content hash in this scope.

    fix 3: the partial unique index ``uq_commitments_open_content(project,
    content_hash)`` cannot dedup a NULL ``project`` (standard SQL NULL != NULL
    means ON CONFLICT never fires for two NULL-scoped rows) — this in-code
    check is the fallback for exactly that case. It now returns the id rather
    than a bool so the caller can record the restatement (``last_seen_at`` /
    ``seen_count``) instead of silently dropping it.
    """
    cur.execute(
        "SELECT id FROM commitments WHERE status = 'open' AND content_hash = %s "
        "AND project IS NOT DISTINCT FROM %s "
        f"AND {_not_forgotten_clause(cur)} LIMIT 1",
        (content_h, scope),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def _has_open_duplicate(cur, scope: str | None, content_h: str) -> bool:
    return _open_id_for_hash(cur, scope, content_h) is not None


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


def _touch_seen(cur, commitment_id: int) -> None:
    """Record that a later capture restated an already-open commitment.

    This is what makes ``mark_stale`` an expiry-by-SILENCE rule rather than an
    expiry-by-age one: a commitment that keeps being restated stays open, and
    only one nobody has mentioned for the window ages out.
    """
    try:
        cur.execute(
            "UPDATE commitments SET last_seen_at = now(), seen_count = seen_count + 1 "
            f"WHERE id = %s AND status = 'open' AND {_not_forgotten_clause(cur)}",
            (commitment_id,),
        )
    except Exception as exc:  # noqa: BLE001 — never fail a capture over a touch
        _log(f"seen touch failed for {commitment_id} ({type(exc).__name__}: {exc})")


def _near_duplicate_open(cur, scope: str | None, text: str, *,
                         payload: dict[str, Any] | None = None,
                         episode_id: int | None = None,
                         rows: list[dict[str, Any]] | None = None) -> int | None:
    """id of an already-OPEN commitment in the same scope that says the same
    thing, or None.

    Zero exact duplicates were measured on the live hub — the noise is
    PARAPHRASE: successive captures of one session restate the same item in
    slightly different words, and ``content_hash`` (an exact-text hash) never
    fires. Token containment via the existing ``_match_score`` is the cheap
    signal; cosine against the stored commitment embeddings is the semantic
    one, at the same bar ``auto_close`` uses to close on a paraphrase.
    """
    if rows is None:
        rows = _open_commitments(cur, scope, payload=payload, episode_id=episode_id)
    if not rows:
        return None
    best_id, best_score = None, 0.0
    for row in rows:
        score = _match_score(text, row["text"])
        if score >= NEAR_DUP_MIN_SCORE and score > best_score:
            best_id, best_score = row["id"], score
    if best_id is not None:
        return best_id
    ids = [r["id"] for r in rows]
    if not _has_commitment_embeddings(cur, ids):
        return None
    from khipu.config import float_setting

    threshold = float_setting("commitment_close_similarity")
    cosine = _cosine_scores(cur, text, ids)
    for cid, score in sorted(cosine.items(), key=lambda kv: -kv[1]):
        if score >= threshold:
            return cid
    return None


def open_from_episode(cur, payload: dict[str, Any], episode_id: int) -> int:
    """Insert one open commitment per ``open_loops`` item. Returns the count
    actually inserted.

    Three gates, cheapest first: the deterministic precision filter
    (:func:`rejection_reason` — status chatter, agent/coordination noise and
    vague items never reach the table at all), the exact ``(scope,
    content_hash)`` dedup, and the paraphrase dedup
    (:func:`_near_duplicate_open`). A skip on either dedup is not a no-op: it
    touches ``last_seen_at``/``seen_count`` on the row already open, so the
    restatement is recorded without a second row.

    Projectless rows keep a NULL display project; dedup and closure use
    their session lineage, falling back to their own episode.
    """
    items = payload.get("open_loops") or []
    if not isinstance(items, list) or not items:
        return 0
    scope = _coalesce_scope(payload)
    seen_ready = _seen_columns_ready(cur)
    trigger_ready = _future_trigger_ready(cur)
    trigger_text_ready = _trigger_text_ready(cur)
    owner_ready = _owner_columns_ready(cur)
    close_ready = _close_columns_ready(cur)
    origin = capture_origin(payload)
    inserted = 0
    rejected = 0
    relayed = 0
    for raw in items:
        norm = _normalize_open_loop(raw)
        if norm is None:
            continue
        # Owner and future_trigger are decided HERE, deterministically, before
        # anything else looks at the item: the reporting rule (rule 3) is
        # owner-dependent, and both fields are stored on the row.
        trigger = has_future_trigger(norm["text"])
        owner, owner_label = resolve_owner_and_label(
            norm["text"], kind=norm["kind"], declared=norm["owner"], future_trigger=trigger,
            origin=origin, declared_label=norm.get("owner_label"),
        )
        reason = rejection_reason(norm["text"], owner=owner, kind=norm["kind"])
        if reason is not None:
            rejected += 1
            _log(f"rejected open_loop ({reason}): {norm['text'][:120]!r}")
            continue
        # content_hash and dedup key off the UNDECORATED text — the due
        # phrase is presentational, not part of the commitment's identity.
        lifecycle_scope = scope
        if lifecycle_scope is None:
            lineage = payload.get("parent_session_id") or payload.get("session_id")
            lifecycle_scope = f"session:{lineage}" if lineage else f"episode:{episode_id}"
        h = content_hash(lifecycle_scope, norm["text"])
        # The relay (owed scope, design (a)): a conversation's item that says
        # what an open JOB item of the same project says is the orchestrator
        # relaying it. The conversation's item is kept as its own row; the
        # job's is closed as relayed by it. Dedup for a conversation item
        # therefore looks at conversation rows only. Never the other way: a
        # job item that matches a conversation item is folded into it, as
        # before.
        relay_ids: list[int] = []
        if owner_ready and scope is not None and origin == ORIGIN_CONVERSATION:
            rows = _open_commitments(cur, scope, payload=payload, episode_id=episode_id,
                                     with_origin=True)
            job_rows = [r for r in rows if r.get("origin") == ORIGIN_JOB]
            own_rows = [r for r in rows if r.get("origin") != ORIGIN_JOB]
            exact = _open_id_for_hash(cur, scope, h)
            existing = exact if any(r["id"] == exact for r in own_rows) else None
            if existing is None:
                existing = _near_duplicate_open(cur, scope, norm["text"], rows=own_rows)
            if existing is None and job_rows:
                relay_ids = _relay_matches(cur, norm["text"], exact, scope, job_rows)
        else:
            existing = _open_id_for_hash(cur, scope, h)
            if existing is None:
                existing = _near_duplicate_open(cur, scope, norm["text"],
                                                payload=payload, episode_id=episode_id)
        if existing is not None:
            if seen_ready:
                _touch_seen(cur, existing)
            continue
        for rid in relay_ids:
            # Closed before the insert: an identical text shares the open
            # (project, content_hash) slot the unique index guards. The
            # reason names the new row once it exists (below).
            close_by_rule(cur, rid, reason="relayed-by a conversation item",
                          key=evidence_key("relayed-by", norm["text"]), episode=episode_id,
                          evidence={"rule": "relayed-by", "text": norm["text"][:300]})
        due_sql, due_param = _parse_due_after(norm["due_after"])
        stored_text = norm["text"]
        due_phrase = str(norm["due_after"] or "").strip()
        if due_sql == "NULL" and due_phrase:
            # fix 1: the phrase didn't parse to a real date — never bind it
            # into the timestamptz column, but don't lose it either; it
            # survives as a parenthetical on the commitment's own text.
            stored_text = f"{stored_text} (due: {due_phrase})"
        base = [stored_text, scope, owner, norm["kind"], episode_id]
        params = (*base, due_param, h) if due_param is not None else (*base, h)
        cols = "text, project, owner, kind, opened_episode, due_after, content_hash"
        vals = f"%s, %s, %s, %s, %s, {due_sql}, %s"
        if trigger_ready:
            cols += ", future_trigger"
            vals += ", %s"
            params = (*params, trigger)
        if trigger_text_ready:
            cols += ", trigger_text"
            vals += ", %s"
            params = (*params, trigger_clause(norm["text"]) if trigger else None)
        if owner_ready:
            cols += ", origin, owner_label"
            vals += ", %s, %s"
            params = (*params, origin, owner_label)
        if close_ready:
            from khipu.close_rules import parse_prs

            cols += ", prs"
            vals += ", %s"
            params = (*params, parse_prs(norm["text"]))
        returning = " RETURNING id" if owner_ready else ""
        cur.execute(
            f"""
            INSERT INTO commitments
              ({cols})
            VALUES ({vals})
            ON CONFLICT (project, content_hash) WHERE status = 'open' DO NOTHING{returning}
            """,
            params,
        )
        if cur.rowcount > 0:
            inserted += 1
            if relay_ids:
                row = cur.fetchone()
                if row:
                    cur.execute("UPDATE commitments SET close_reason = %s WHERE id = ANY(%s)",
                                (f"relayed-by {int(row[0])}", relay_ids))
                relayed += len(relay_ids)
    if inserted or rejected:
        _log(
            f"opened {inserted} commitment(s), rejected {rejected}, relayed {relayed}, for "
            f"episode {episode_id} (scope={scope!r}, origin={origin})"
        )
    return inserted


def _relay_matches(cur, text: str, exact: int | None, scope: str | None,
                   job_rows: list[dict[str, Any]]) -> list[int]:
    """Open job items this conversation item restates: the exact text, or
    every one at or above today's near-duplicate bar, or else the best
    cosine match at ``commitment_close_similarity`` (the same test
    :func:`_near_duplicate_open` uses)."""
    if exact is not None and any(r["id"] == exact for r in job_rows):
        return [exact]
    hits = [r["id"] for r in job_rows if _match_score(text, r["text"]) >= NEAR_DUP_MIN_SCORE]
    if hits:
        return hits
    best = _near_duplicate_open(cur, scope, text, rows=job_rows)
    return [best] if best is not None else []


def _open_commitments(cur, scope: str | None, *, payload: dict[str, Any] | None = None,
                      episode_id: int | None = None,
                      with_origin: bool = False) -> list[dict[str, Any]]:
    lineage, params = _lineage_clause(cur, payload or {"project": scope}, episode_id)
    cols = "id, text, origin" if with_origin else "id, text"
    cur.execute(
        f"SELECT {cols} FROM commitments WHERE status = 'open' AND project IS NOT DISTINCT FROM %s "
        f"AND {lineage} AND {_not_forgotten_clause(cur)}",
        (scope, *params),
    )
    if with_origin:
        return [{"id": r[0], "text": r[1], "origin": r[2]} for r in cur.fetchall()]
    return [{"id": r[0], "text": r[1]} for r in cur.fetchall()]


def _candidate_close_texts(payload: dict[str, Any]) -> list[tuple[str, str, int | None]]:
    """(text, kind, commitment_id) from closed_loops and decisions, where kind is
    ``"done"`` (a ``done:``-prefixed statement — matched by text only, never
    an API call) or ``"loop"`` (a closed_loop the extractor listed as a loop
    that closed — matched by text OR, when commitments are embedded, by
    cosine). Only ``done:``-prefixed decisions are candidates; a plain
    decision never closes a commitment so an unrelated decision cannot close
    one on a text or paraphrase fluke."""
    out: list[tuple[str, str, int | None]] = []
    for item in payload.get("closed_loops") or []:
        text = item.get("text") if isinstance(item, dict) else item
        text = str(text or "").strip()
        if not text:
            continue
        reference = item.get("commitment_id") if isinstance(item, dict) else None
        if reference is not None:
            try:
                reference = int(str(reference))
            except ValueError:
                reference = 0  # An invalid explicit target must not fall back to another row.
        m = _DONE_PREFIX_RE.match(text)
        out.append((_DONE_PREFIX_RE.sub("", text), "done" if m else "loop", reference))
    for item in payload.get("decisions") or []:
        text = str(item or "").strip()
        if not text:
            continue
        if _DONE_PREFIX_RE.match(text):
            out.append((_DONE_PREFIX_RE.sub("", text), "done", None))
    return out


def _match_score(a: str, b: str) -> float:
    """Best of Jaccard and substring/containment (fix 4) — used for the
    explicit ``done: <text>`` close, which has no similarity config of its
    own but must still match the CLOSED text against candidate commitment
    text rather than closing an arbitrary open row."""
    al, bl = a.strip().lower(), b.strip().lower()
    if al and bl and (al in bl or bl in al):
        return 1.0
    return _jaccard(a, b)


def _completion_text(text: str) -> str:
    text = " ".join(text.lower().split())
    # Only the leading action changes tense; task qualifiers retain their words.
    def action(match):
        token = match.group()
        stems = (*_stems(token), token[:-3] + "y" if token.endswith("ied") else token)
        return next((stem for stem in stems if stem in _ACTION_STEMS), token)
    return re.sub(r"^[a-z]+", action, text, count=1)


def _has_commitment_embeddings(cur, commitment_ids: list[int]) -> bool:
    """Cheap existence check (fix 5b): skip the embed API call entirely when
    no candidate commitment has a stored embedding yet — the embed catch-up
    (embed.embed_recent_missing) populates these out of band, not here."""
    if not commitment_ids:
        return False
    try:
        from khipu.embed import _active_profile

        profile = _active_profile(cur)
        cur.execute(
            "SELECT 1 FROM memory_embeddings WHERE profile = %s AND kind = 'commitment' "
            "AND chunk_idx = 0 AND ref = ANY(%s) LIMIT 1",
            (profile, [str(i) for i in commitment_ids]),
        )
        return cur.fetchone() is not None
    except Exception:  # noqa: BLE001 — fail-open to "no embeddings, use Jaccard"
        return False


def _cosine_scores(cur, text: str, commitment_ids: list[int]) -> dict[int, float]:
    """Best-effort cosine between ``text`` and each open commitment's stored
    embedding. Returns {} on any failure (no key, no embeddings yet, PG
    error) so the caller falls back to Jaccard — never raises."""
    if not commitment_ids:
        return {}
    try:
        from khipu.embed import _active_profile, _vec_literal, embed_one

        profile = _active_profile(cur)
        vec = embed_one(text, profile=profile)
        cur.execute(
            """
            SELECT ref, 1 - (embedding <=> %s::vector) AS score
            FROM memory_embeddings
            WHERE profile = %s AND kind = 'commitment' AND chunk_idx = 0
              AND ref = ANY(%s)
            """,
            (_vec_literal(vec), profile, [str(i) for i in commitment_ids]),
        )
        return {int(ref): float(score) for ref, score in cur.fetchall() if score is not None}
    except Exception:  # noqa: BLE001 — fail-open to Jaccard
        return {}


def completion_match(text: str, row_text: str, *, kind: str, threshold: float,
                     cos: float | None = None) -> tuple[float, str, bool]:
    """``(score, via, semantic)``: how ``text`` (a closed loop, a ``done:``
    statement, or a decision) completes an item reading ``row_text``. ``via``
    is empty when it does not reach the bar. The one test :func:`auto_close`
    and the decision rule (``khipu.close_rules``) both use."""
    completion = _completion_text(text)
    tokens = set(re.findall(r"[\w./-]+", completion))
    target = _completion_text(row_text)
    exact = completion == target
    if not exact and len(tokens) < 3:
        return 0.0, "", False
    # Ordinary completion must preserve the full lexical evidence;
    # substring containment is reserved for an explicit done statement.
    text_score = _match_score(text, row_text) if kind == "done" else _jaccard(completion, target)
    covers = set(re.findall(r"[\w./-]+", target)) <= tokens
    score = 0.0
    via = ""
    text_threshold = DONE_MATCH_MIN_SCORE if kind == "done" else threshold
    if text_score >= text_threshold and (kind == "done" or covers):
        score = text_score
        via = "explicit done (best match)" if kind == "done" else "text"
    # High overlap with missing words can mean a changed qualifier
    # (staging versus production); cosine must not erase that evidence.
    if cos is not None and cos >= threshold and cos > score \
            and (covers or text_score < NEAR_DUP_MIN_SCORE):
        return cos, "cosine", True
    return score, via, False


def evidence_key(rule: str, text: str) -> str:
    """A close rule key for evidence that is a piece of text: the same words
    give the same key, so a reopened item is not closed again by them."""
    norm = re.sub(r"\s+", " ", str(text or "").strip().lower())
    return f"{rule}:{hashlib.sha256(norm.encode('utf-8')).hexdigest()[:16]}"


def evidence_problem(text: str, item_text: str = "") -> str | None:
    """Why ``text`` is not evidence that closes an item reading
    ``item_text`` (``khipu.close_rules.evidence_problem``, the one check every
    close path shares), or None."""
    from khipu.close_rules import evidence_problem as check

    return check(text, item_text)


def close_by_rule(cur, commitment_id: int, *, reason: str, key: str, evidence: dict | None = None,
                  episode: int | None = None, set_extra: str = "", extra_params: tuple = ()) -> bool:
    """The one write every automatic close goes through (the PR and decision
    rules, the closed-loop match, the session-end close, the relay close, the
    hygiene re-judge and the clean-up's apply). It records the reason, who
    closed it (:data:`BY_RULE`), the rule ``key`` and the ``evidence``, and it
    refuses an item the owner reopened after that same key closed it
    (``reopened_rule_keys``). Hand closes are :func:`set_status`; a forgotten
    episode's items are closed by ``khipu.forget``. On a hub before migration
    0034 there is no key to remember, so only the reason is written."""
    reason = str(reason or "")[:300]
    if _close_columns_ready(cur):
        cur.execute(
            "UPDATE commitments SET status = 'closed', closed_at = now(), closed_episode = %s, "
            f"close_reason = %s, closed_by = %s, close_rule_key = %s, close_evidence = %s::jsonb{set_extra} "
            "WHERE id = %s AND status = 'open' AND NOT (%s = ANY(reopened_rule_keys)) "
            f"AND {_not_forgotten_clause(cur)}",
            (episode, reason, BY_RULE, key, json.dumps(evidence or {}), *extra_params, commitment_id, key),
        )
    else:
        cur.execute(
            "UPDATE commitments SET status = 'closed', closed_at = now(), closed_episode = %s, "
            f"close_reason = %s{set_extra} WHERE id = %s AND status = 'open' "
            f"AND {_not_forgotten_clause(cur)}",
            (episode, reason, *extra_params, commitment_id),
        )
    return cur.rowcount > 0


def auto_close(cur, payload: dict[str, Any], episode_id: int) -> int:
    """Match every closed_loop / done-decision against this scope's open
    commitments (``scope``: project, else parent_session_id/session_id — fix
    3); close the best match at or above the configured similarity, or for
    an explicit ``done:`` prefix, the best text match at or above
    ``DONE_MATCH_MIN_SCORE`` (fix 4 — never the first unordered row, and
    never anything if nothing clears the bar). Returns count closed.
    """
    scope = _coalesce_scope(payload)
    candidates = _candidate_close_texts(payload)
    if not candidates:
        return 0
    open_rows = _open_commitments(cur, scope, payload=payload, episode_id=episode_id)
    if not open_rows:
        return 0
    from khipu.config import float_setting

    threshold = float_setting("commitment_close_similarity")
    ids = [r["id"] for r in open_rows]
    already_closed: set[int] = set()
    closed = 0
    for text, kind, reference in candidates:
        remaining_ids = [i for i in ids if i not in already_closed]
        # Cosine (semantic paraphrase) is available for a closed_loop when the
        # scope's commitments are embedded; a "done:" close is text-only (fix
        # 5a) and never spends an API call, and with no stored commitment
        # embeddings (fix 5b) there is nothing to score against.
        if reference is None and kind != "done" and remaining_ids and _has_commitment_embeddings(cur, remaining_ids):
            cosine = _cosine_scores(cur, text, remaining_ids)
        else:
            cosine: dict[int, float] = {}
        best_id = None
        best_score = 0.0
        best_via = ""
        tied = False
        semantic_matches = 0
        for row in open_rows:
            cid = row["id"]
            if cid in already_closed:
                continue
            if reference is not None:
                if cid == reference:
                    best_id, best_score, best_via = cid, 1.0, "commitment id"
                continue
            score, via, semantic = completion_match(text, row["text"], kind=kind, threshold=threshold,
                                                    cos=cosine.get(cid))
            if semantic:
                semantic_matches += 1
            if via and score > best_score:
                best_id, best_score, best_via = cid, score, via
                tied = False
            elif via and abs(score - best_score) < 1e-9:
                tied = True
        if best_id is None or tied or (best_via == "cosine" and semantic_matches > 1):
            continue
        best_text = next(r["text"] for r in open_rows if r["id"] == best_id)
        why_not = evidence_problem(text, best_text)
        if why_not:
            _log(f"kept commitment {best_id} open: {why_not}")
            continue
        if close_by_rule(cur, best_id, reason=f"{best_via} match ({best_score:.2f}): {text[:200]!r}",
                         key=evidence_key("loop", text), episode=episode_id,
                         evidence={"rule": "loop", "text": text[:300], "match": best_via,
                                   "score": round(best_score, 3)}):
            already_closed.add(best_id)
            closed += 1
            _log(f"closed commitment {best_id} via {best_via} ({best_score:.2f}) at episode {episode_id}")
    return closed


def stale_sql(*, has_last_seen: bool) -> str:
    """The expiry-by-SILENCE UPDATE, as text so it can be unit-tested without
    a database.

    Three rules, all in one statement:
      * a ``due_after`` still in the FUTURE is never stale — the commitment is
        parked on purpose (that is what ``khipu owed --snooze`` writes);
      * followups and promises with no live due date expire after
        ``STALE_AFTER_DAYS_SOFT`` (14) days of silence;
      * blockers and questions get ``STALE_AFTER_DAYS`` (30) — they outlive a
        fortnight of quiet far more often than a followup does.
    "Silence" is measured from ``last_seen_at`` when migration 0012 is in
    (each restatement pushes it forward), else from ``opened_at``.
    """
    seen = "COALESCE(last_seen_at, opened_at)" if has_last_seen else "opened_at"
    seen = f"GREATEST({seen}, due_after)"
    return f"""
        UPDATE commitments
        SET status = 'stale'
        WHERE status = 'open'
          AND (due_after IS NULL OR due_after <= now())
          AND {seen} < now() - (
              CASE WHEN kind IN ('followup', 'promise')
                   THEN interval '{STALE_AFTER_DAYS_SOFT} days'
                   ELSE interval '{STALE_AFTER_DAYS} days'
              END)
        """


def mark_stale(cur) -> int:
    """Flip silent open commitments to 'stale' (see :func:`stale_sql`). Never
    silently dropped — stale stays queryable via `khipu owed --status stale`.
    """
    cur.execute(stale_sql(has_last_seen=_seen_columns_ready(cur))
                + f" AND {_not_forgotten_clause(cur)}")
    return cur.rowcount


# ---------------------------------------------------------------------------
# Session-plan closure. Before the precision filter shipped, a session's own
# plan steps and in-flight status lines were opened as commitments; they are
# finished (or abandoned) when the session ends, and no later capture ever
# says "done: <phrase>" about them, so auto_close never fires. Retire them
# when their OWN session ends.
# ---------------------------------------------------------------------------

SESSION_END_EVENTS = frozenset({"sessionend", "session_end", "sessionended", "session-end"})


def _payload_event(payload: dict[str, Any]) -> str:
    """Normalized capture event. Queued jobs carry it as ``event``; older
    payloads only encoded it into ``scope`` ("claude sessionend")."""
    ev = str(payload.get("event") or "").strip().lower()
    if ev:
        return ev
    scope = str(payload.get("scope") or "").strip().lower()
    for candidate in SESSION_END_EVENTS:
        if scope.endswith(candidate):
            return candidate
    return ""


def _session_open_commitments(cur, session_id: str) -> list[dict[str, Any]]:
    """Open commitments opened by an episode of THIS session.

    Joined through ``episodes`` rather than a new column so rows written
    before this change are covered too.
    """
    cur.execute(
        f"""
        SELECT c.id, c.text, c.kind, c.owner
        FROM commitments c
        JOIN episodes e ON e.id = c.opened_episode
        WHERE c.status = 'open' AND e.session_id = %s
          AND {_not_forgotten_clause(cur).replace('commitments.opened_episode', 'c.opened_episode')}
        """,
        (session_id,),
    )
    return [
        {"id": r[0], "text": r[1], "kind": r[2], "owner": r[3]}
        for r in cur.fetchall()
    ]


def _is_user_owed(row: dict[str, Any]) -> bool:
    """Never auto-retire something the USER owes — a question, or anything
    whose owner is the user. Those are the only commitments Owed exists for."""
    if str(row.get("kind") or "").lower() == "question":
        return True
    owner = str(row.get("owner") or "").strip().lower()
    return bool(owner) and owner in _compile_user_patterns().owners


def close_session_plan(cur, payload: dict[str, Any], episode_id: int) -> int:
    """Close this session's own commitments when the session ends.

    Two rules, both scoped to commitments opened by an episode of THIS
    session:

    * **session-ended** (2026-09-04, second pass) — on a ``sessionend``
      capture, every open commitment with ``owner = assistant`` and
      ``future_trigger = false`` is closed with ``close_reason
      'session-ended'``. That is the whole point of the two fields: an
      assistant promise with no explicit cross-session trigger cannot outlive
      the window it was made in. User-owed rows and future-trigger rows are
      NEVER closed this way.
    * **session-plan** (first pass, kept) — an in-flight / agent /
      coordination shaped row (:func:`is_session_plan_text`) is also closed
      when this same payload's ``closed_loops`` / ``decisions`` mention it,
      without waiting for the sessionend.

    Returns the count closed.
    """
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return 0
    is_end = _payload_event(payload) in SESSION_END_EVENTS
    mentions = [t for t, _, _ in _candidate_close_texts(payload)]
    if not is_end and not mentions:
        return 0
    try:
        rows = _session_open_commitments(cur, session_id)
    except Exception as exc:  # noqa: BLE001 — additive; never break a capture
        _log(f"session-plan lookup failed ({type(exc).__name__}: {exc})")
        return 0
    closed = 0
    for row in rows:
        trigger = has_future_trigger(row["text"])
        owner, _label = row_owner(row, future_trigger=trigger)
        # Only the assistant's own items die with its session: the user's,
        # and an agent's (a job's item for its orchestrator), outlive it.
        if _is_user_owed(row) or owner != OWNER_ASSISTANT:
            continue
        if is_end and not trigger:
            # The session-ended rule: an assistant promise with no explicit
            # cross-session trigger dies with its session, whatever shape its
            # text has.
            reason = "session-ended"
        elif is_session_plan_text(row["text"]):
            # Mid-session: only a plan-shaped row this payload explicitly
            # mentions as finished is retired. A future-trigger row is never
            # closed by the sessionend itself.
            hit = next(
                (m for m in mentions if _match_score(m, row["text"]) >= DONE_MATCH_MIN_SCORE
                 and not evidence_problem(m, row["text"])),
                None,
            )
            if hit is None:
                continue
            reason = f"session-plan (mentioned): {hit[:150]!r}"
            key, evidence = evidence_key("session-plan", hit), {"rule": "session-plan", "text": hit[:300]}
        else:
            continue
        if reason == "session-ended":
            key, evidence = f"session-ended:{session_id}", {"rule": "session-ended", "session_id": session_id}
        if close_by_rule(cur, row["id"], reason=reason, key=key, evidence=evidence, episode=episode_id):
            closed += 1
    if closed:
        _log(f"closed {closed} session-end commitment(s) for {session_id}")
    return closed


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
    3) — the same coalesced key ``open_from_episode``/``auto_close`` store
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
    if project:
        clauses.append("project = %s")
        params.append(project)
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
    cur.execute(
        f"""
        SELECT {', '.join(select_cols)}
        FROM commitments
        WHERE {' AND '.join(clauses)}
        ORDER BY opened_at DESC, id DESC
        LIMIT %s
        """,
        params,
    )
    return [dict(zip(select_cols, row)) for row in cur.fetchall()]


def finish_owed_rows(rows: list[dict[str, Any]], limit: int | None) -> list[dict[str, Any]]:
    """Owner, trigger, priority and order for raw commitment rows, the same
    for a hub read and an offline read of the replica (``offline_reads``)."""
    resolve_owed_rows(rows)
    # O3: age x kind — within a priority tier (kind), the LONGEST-outstanding
    # item leads (opened_at ascending), not the newest one the SQL fetched
    # first. A row with no opened_at (should not happen; NOT NULL) sorts
    # last within its tier rather than raising.
    from datetime import datetime, timezone

    _max_dt = datetime.max.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda r: (r["priority"], r.get("opened_at") or _max_dt))
    return rows[:limit]


def resolve_owed_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Owner, trigger and priority for raw commitment rows, in place."""
    for row in rows:
        row.setdefault("last_seen_at", None)
        row["seen_count"] = int(row.get("seen_count") or 1)
        # owner / future_trigger are RESOLVED here, not read raw: rows opened
        # before migration 0013 carry a model-supplied owner ("Peer 1",
        # "ASSISTANT", NULL) and no future_trigger at all, and the desktop's
        # "Needs you" section is exactly `owner == 'user'`. Deterministic
        # detection wins, so a legacy row lists the same way a new one does;
        # a stored `agent` owner (migration 0033's third owner) is kept.
        trigger = has_future_trigger(row.get("text") or "") or bool(row.get("future_trigger"))
        row["future_trigger"] = trigger
        row["owner"], row["owner_label"] = row_owner(row, future_trigger=trigger)
        row.setdefault("origin", None)
        # Migration 0034's fields, the same shape on a hub without them. A
        # looser decision match is offered, never applied (design (b)).
        for key in ("prs", "closed_by", "close_rule_key", "settle_suggestion", "pr_check"):
            row.setdefault(key, None)
        suggestion = row["settle_suggestion"] if isinstance(row["settle_suggestion"], dict) else None
        row["looks_settled"] = (f"Looks settled by decision {suggestion['decision_id']}"
                                if suggestion and suggestion.get("decision_id") else None)
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
    return rows


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
    """Close, reopen, stale or drop one commitment by hand. A close keeps its
    ``reason`` (default "closed by hand") and, once migration 0034 is in,
    ``by`` (:data:`BY_APP`, :data:`BY_CONVERSATION`). A reopen restores the
    item and remembers the rule key that closed it, so the same rule and
    evidence never close it again (``khipu.close_rules``)."""
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
    else:
        extra = ", closed_at = now()" if status == "closed" else ""
        params: list[Any] = [status]
        if status in ("closed", "dropped"):
            extra += ", close_reason = %s"
            params.append((reason or "").strip()[:300] or f"{status} by hand")
            if close_ready:
                extra += ", closed_by = %s, close_rule_key = NULL, close_evidence = NULL"
                params.append(by)
        cur.execute(
            f"UPDATE commitments SET status = %s{extra} WHERE id = %s "
            f"AND {_not_forgotten_clause(cur)}",
            (*params, commitment_id),
        )
    return cur.rowcount > 0


# ---- paging, filters and bulk changes (design (c), slice 3) -----------------

OWNERS = (OWNER_USER, OWNER_ASSISTANT, OWNER_AGENT)
ORIGINS = (ORIGIN_CONVERSATION, ORIGIN_JOB)
# What ``bulk_owner_gate`` returns when it passes; the hub accepts a bulk
# change to the owner's items only with one of these on the write.
OWNER_GATE_PASSES = ("app", "terminal")
BULK_OWNER_REFUSAL = ("Changing more than one of the owner's own items at once needs the owner: "
                      "do it in the Khipu app, or type yes on a terminal. Nothing was changed.")


def _opened_key(row: dict[str, Any]) -> tuple[Any, int]:
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
    return at, int(row.get("id") or 0)


def encode_cursor(row: dict[str, Any]) -> str:
    import base64

    at, cid = _opened_key(row)
    return base64.urlsafe_b64encode(json.dumps([at.isoformat(), cid]).encode()).decode().rstrip("=")


def decode_cursor(raw: str) -> tuple[Any, int]:
    """``(opened_at, id)`` of the last row a page gave; ValueError otherwise."""
    import base64
    from datetime import datetime

    try:
        text = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
        at, cid = json.loads(text)
        return _opened_key({"opened_at": datetime.fromisoformat(at), "id": int(cid)})
    except (ValueError, TypeError, UnicodeDecodeError):
        raise ValueError("cursor is not one khipu_owed gave; start again without it") from None


def _row_prs(row: dict[str, Any]) -> list[int]:
    if isinstance(row.get("prs"), list):
        return [int(n) for n in row["prs"]]
    from khipu.close_rules import parse_prs

    return parse_prs(row.get("text") or "")


def page_owed(rows: list[dict[str, Any]], *, owner: str | None = None, kind: str | None = None,
              origin: str | None = None, query: str | None = None, pr: int | None = None,
              cursor: str | None = None, limit: int | None = 50) -> dict[str, Any]:
    """One page of raw owed rows (``fetch_owed_rows`` or the replica's), newest
    first on the keyset (``opened_at``, ``id``), so a page never skips or
    repeats a row while new ones arrive (they sort ahead of every cursor).
    Owners are resolved first, so the owner filter sees what the app shows.
    ``total`` counts every row that matches; ``counts`` holds how many each
    owner and kind has among the rows the other filters match (the owner
    counts ignore the owner filter, the kind counts the kind filter), so a
    caller sees what the other headings hold. ``limit`` None or 0: no limit."""
    resolve_owed_rows(rows)
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
        return not (pr is not None and pr not in _row_prs(row))

    counts: dict[str, dict[str, int]] = {"owner": {}, "kind": {}}
    for row in rows:
        if keep(row, skip="owner"):
            counts["owner"][row["owner"]] = counts["owner"].get(row["owner"], 0) + 1
        if keep(row, skip="kind"):
            k = str(row.get("kind") or "followup")
            counts["kind"][k] = counts["kind"].get(k, 0) + 1
    matched = sorted((r for r in rows if keep(r)), key=_opened_key, reverse=True)
    after = matched
    if cursor:
        last = decode_cursor(cursor)
        after = [r for r in matched if _opened_key(r) < last]
    page = after[:limit] if limit else after
    more = len(after) > len(page)
    return {"results": page, "total": len(matched), "counts": counts,
            "next_cursor": encode_cursor(page[-1]) if more and page else None}


def owners_of(cur, ids: list[int], *, lock: bool = False) -> dict[int, dict[str, Any]]:
    """``{id: row}`` with the resolved owner for the given ids (forgotten
    and missing ids left out); ``lock`` holds the rows for this transaction."""
    cols = ["id", "text", "owner", "kind", "status"]
    if _owner_columns_ready(cur):
        cols += ["origin", "owner_label"]
    if _future_trigger_ready(cur):
        cols.append("future_trigger")
    if _reassigned_column_ready(cur):
        cols.append("owner_reassigned_at")
    cur.execute(f"SELECT {', '.join(cols)} FROM commitments WHERE id = ANY(%s) "
                f"AND {_not_forgotten_clause(cur)}{' FOR UPDATE' if lock else ''}", (list(ids),))
    out = {}
    for raw in cur.fetchall():
        row = dict(zip(cols, raw))
        trigger = has_future_trigger(row.get("text") or "") or bool(row.get("future_trigger"))
        row["owner"], row["owner_label"] = row_owner(row, future_trigger=trigger)
        out[int(row["id"])] = row
    return out


def reassign(cur, commitment_id: int, owner: str, label: str | None = None) -> bool:
    """Set one item's owner by hand: ``agent`` keeps ``label`` (default the
    orchestrator), the user and the assistant none. Once migration 0035 is in,
    a read keeps the owner as written instead of resolving it from the text."""
    if owner not in OWNERS:
        raise ValueError(f"owner must be one of {', '.join(OWNERS)}, got {owner!r}")
    sets, params = ["owner = %s"], [owner]
    if _owner_columns_ready(cur):
        sets.append("owner_label = %s")
        params.append((clean_owner_label(label) or ORCHESTRATOR) if owner == OWNER_AGENT else None)
    if _reassigned_column_ready(cur):
        sets.append("owner_reassigned_at = now()")
    cur.execute(f"UPDATE commitments SET {', '.join(sets)} WHERE id = %s AND {_not_forgotten_clause(cur)}",
                (*params, commitment_id))
    return cur.rowcount > 0


def bulk_owner_gate(count: int) -> str | None:
    """The owner gate for a change to more than one of the owner's own items
    (decision 6): ``"app"`` when the Khipu app itself started this process,
    ``"terminal"`` after the owner's yes typed on a terminal, else None. The
    same two checks as the ledger's owner verbs (``ledger.require``), made
    through routing's own functions, never reimplemented here. This is the
    one place the owner's direction through the agent
    (docs/plans/2026-10-16-owner-direction.md) plugs in when it lands."""
    from khipu import routing

    if routing._caller()["app"]:
        return "app"
    if routing.typed_yes(f"This changes {count} items at once, some of them the owner's own. "
                         "Type yes to confirm: "):
        return "terminal"
    return None
