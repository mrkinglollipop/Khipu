# --bypass-harness (sonnet lane)
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
_AGENT_RE = re.compile(
    r"\b(?:sub-?agents?|agents?|agent's)\b"
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
    ("agent-or-infra", _AGENT_RE),
    ("coordination", _COORDINATION_RE),
    ("status-sentence", _STATUS_SENTENCE_RE),
)

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

# "when/once/after <someone/something> <does something>", plus the explicit
# next-session phrasings. Gaps allow hyphens/quotes so "after the oracle-speed
# wave merges" matches.
_GAP = r"(?:[\w'’`./-]+\s+){0,4}?"
_FUTURE_TRIGGER_RE = re.compile(
    r"\b(?:next|future|later|another|a\s+future|a\s+later)\s+session\b"
    r"|\bnext\s+time\b"
    r"|\bin\s+a\s+(?:future|later|new)\s+session\b"
    r"|\bwhen\s+" + _GAP + r"(?:says?|said|tells?|confirms?|approves?|answers?|"
    r"replies|responds?|returns?|merges?|ships?|lands?|finishes?|completes?|"
    r"arrives?|comes?|decides?|picks?|chooses?|gives?|gets?|re-?auths?|"
    r"re-?authed|is|are|has|have)\b"
    r"|\bonce\s+" + _GAP + r"(?:says?|confirms?|approves?|merges?|merged|ships?|"
    r"shipped|lands?|landed|finishes?|finished|completes?|completed|returns?|"
    r"passes?|passed|re-?auth\w*|is|are|has|have)\b"
    r"|\bafter\s+" + _GAP + r"(?:merges?|merged|ships?|shipped|lands?|landed|"
    r"approves?|approved|confirms?|confirmed|completes?|completed|finishes?|"
    r"finished|returns?|passes?|passed|re-?auth\w*)\b"
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


def has_future_trigger(text: str) -> bool:
    """True when the text carries an explicit CROSS-SESSION condition — the
    one thing that makes an assistant promise outlive its own session."""
    return bool(_FUTURE_TRIGGER_RE.search((text or "").strip()))


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


def resolve_owner(text: str, *, kind: str | None = None, declared: Any = None,
                  future_trigger: bool | None = None) -> str:
    """'user' or 'assistant' for one commitment.

    User when the text names the user/Matt as the actor, asks for a decision /
    approval / confirmation / review / merge / test by the user, or is a
    question. Assistant otherwise. The deterministic signal WINS: a
    model-supplied ``declared`` owner is consulted only when the text carries
    no signal at all and no future trigger (a trigger clause is exactly where
    a model picks up the wrong name — "When Matt says …" is not Matt's item).
    """
    if future_trigger is None:
        future_trigger = has_future_trigger(text)
    if _user_signal(text, kind=kind, future_trigger=future_trigger):
        return OWNER_USER
    if not future_trigger:
        norm = normalize_owner(declared)
        if norm:
            return norm
    return OWNER_ASSISTANT


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
    if not _has_action_verb(s):
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
    return any(pattern.search(s) for _, pattern in _EXCLUSION_RULES)


def content_hash(scope: str | None, text: str) -> str:
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha256(f"{scope or ''}\x00{norm}".encode("utf-8")).hexdigest()


def _coalesce_scope(payload: dict[str, Any]) -> str | None:
    """W3.3 grouping key (fix 3): ``project``, else ``parent_session_id``,
    else ``session_id`` — a capture with no resolved project (a scratchpad/
    `/tmp` cwd, a dispatched child session) still dedups/closes/lists against
    its OWN prior commitments instead of every such writer competing for one
    unscoped NULL bucket. Stored as the row's ``project`` column so
    ``list_owed``/``auto_close`` use the exact same key back."""
    for key in ("project", "parent_session_id", "session_id"):
        val = payload.get(key)
        if val:
            return str(val)
    return None


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
                "future_trigger": None}
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
        "AND project IS NOT DISTINCT FROM %s LIMIT 1",
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


def _touch_seen(cur, commitment_id: int) -> None:
    """Record that a later capture restated an already-open commitment.

    This is what makes ``mark_stale`` an expiry-by-SILENCE rule rather than an
    expiry-by-age one: a commitment that keeps being restated stays open, and
    only one nobody has mentioned for the window ages out.
    """
    try:
        cur.execute(
            "UPDATE commitments SET last_seen_at = now(), seen_count = seen_count + 1 "
            "WHERE id = %s AND status = 'open'",
            (commitment_id,),
        )
    except Exception as exc:  # noqa: BLE001 — never fail a capture over a touch
        _log(f"seen touch failed for {commitment_id} ({type(exc).__name__}: {exc})")


def _near_duplicate_open(cur, scope: str | None, text: str) -> int | None:
    """id of an already-OPEN commitment in the same scope that says the same
    thing, or None.

    Zero exact duplicates were measured on the live hub — the noise is
    PARAPHRASE: successive captures of one session restate the same item in
    slightly different words, and ``content_hash`` (an exact-text hash) never
    fires. Token containment via the existing ``_match_score`` is the cheap
    signal; cosine against the stored commitment embeddings is the semantic
    one, at the same bar ``auto_close`` uses to close on a paraphrase.
    """
    rows = _open_commitments(cur, scope)
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

    ``scope`` is ``project`` if known else ``parent_session_id``/``session_id``
    (fix 3) — stored in the row's ``project`` column so every later read uses
    the same key.
    """
    items = payload.get("open_loops") or []
    if not isinstance(items, list) or not items:
        return 0
    scope = _coalesce_scope(payload)
    seen_ready = _seen_columns_ready(cur)
    trigger_ready = _future_trigger_ready(cur)
    inserted = 0
    rejected = 0
    for raw in items:
        norm = _normalize_open_loop(raw)
        if norm is None:
            continue
        # Owner and future_trigger are decided HERE, deterministically, before
        # anything else looks at the item: the reporting rule (rule 3) is
        # owner-dependent, and both fields are stored on the row.
        trigger = has_future_trigger(norm["text"])
        owner = resolve_owner(
            norm["text"], kind=norm["kind"], declared=norm["owner"], future_trigger=trigger
        )
        reason = rejection_reason(norm["text"], owner=owner, kind=norm["kind"])
        if reason is not None:
            rejected += 1
            _log(f"rejected open_loop ({reason}): {norm['text'][:120]!r}")
            continue
        # content_hash and dedup key off the UNDECORATED text — the due
        # phrase is presentational, not part of the commitment's identity.
        h = content_hash(scope, norm["text"])
        existing = _open_id_for_hash(cur, scope, h)
        if existing is None:
            existing = _near_duplicate_open(cur, scope, norm["text"])
        if existing is not None:
            if seen_ready:
                _touch_seen(cur, existing)
            continue
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
        cur.execute(
            f"""
            INSERT INTO commitments
              ({cols})
            VALUES ({vals})
            ON CONFLICT (project, content_hash) WHERE status = 'open' DO NOTHING
            """,
            params,
        )
        if cur.rowcount > 0:
            inserted += 1
    if inserted or rejected:
        _log(
            f"opened {inserted} commitment(s), rejected {rejected}, for episode "
            f"{episode_id} (scope={scope!r})"
        )
    return inserted


def _open_commitments(cur, scope: str | None) -> list[dict[str, Any]]:
    cur.execute(
        "SELECT id, text FROM commitments WHERE status = 'open' AND project IS NOT DISTINCT FROM %s",
        (scope,),
    )
    return [{"id": r[0], "text": r[1]} for r in cur.fetchall()]


def _candidate_close_texts(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """(text, kind) pairs from closed_loops and decisions, where kind is
    ``"done"`` (a ``done:``-prefixed statement — matched by text only, never
    an API call) or ``"loop"`` (a closed_loop the extractor listed as a loop
    that closed — matched by text OR, when commitments are embedded, by
    cosine). Only ``done:``-prefixed decisions are candidates; a plain
    decision never closes a commitment so an unrelated decision cannot close
    one on a text or paraphrase fluke."""
    out: list[tuple[str, str]] = []
    for item in payload.get("closed_loops") or []:
        text = item.get("text") if isinstance(item, dict) else item
        text = str(text or "").strip()
        if not text:
            continue
        m = _DONE_PREFIX_RE.match(text)
        out.append((_DONE_PREFIX_RE.sub("", text), "done" if m else "loop"))
    for item in payload.get("decisions") or []:
        text = str(item or "").strip()
        if not text:
            continue
        if _DONE_PREFIX_RE.match(text):
            out.append((_DONE_PREFIX_RE.sub("", text), "done"))
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
    open_rows = _open_commitments(cur, scope)
    if not open_rows:
        return 0
    from khipu.config import float_setting

    threshold = float_setting("commitment_close_similarity")
    ids = [r["id"] for r in open_rows]
    already_closed: set[int] = set()
    closed = 0
    for text, kind in candidates:
        remaining_ids = [i for i in ids if i not in already_closed]
        # Cosine (semantic paraphrase) is available for a closed_loop when the
        # scope's commitments are embedded; a "done:" close is text-only (fix
        # 5a) and never spends an API call, and with no stored commitment
        # embeddings (fix 5b) there is nothing to score against.
        if kind != "done" and remaining_ids and _has_commitment_embeddings(cur, remaining_ids):
            cosine = _cosine_scores(cur, text, remaining_ids)
        else:
            cosine: dict[int, float] = {}
        best_id = None
        best_score = 0.0
        best_via = ""
        for row in open_rows:
            cid = row["id"]
            if cid in already_closed:
                continue
            # Two signals, two bars. A containment/lexical text match closes at
            # the low DONE_MATCH_MIN_SCORE bar — a short closed_loop phrase is a
            # subset of a longer commitment, so containment (folded into
            # _match_score), not raw Jaccard, is what matches it. A cosine match
            # needs the strict commitment_close_similarity bar so a merely
            # related paraphrase cannot close the wrong item.
            text_score = _match_score(text, row["text"])
            cos = cosine.get(cid)
            score = 0.0
            via = ""
            if text_score >= DONE_MATCH_MIN_SCORE:
                score = text_score
                via = "explicit done (best match)" if kind == "done" else "text"
            if cos is not None and cos >= threshold and cos > score:
                score, via = cos, "cosine"
            if via and score > best_score:
                best_id, best_score, best_via = cid, score, via
        if best_id is None:
            continue
        cur.execute(
            """
            UPDATE commitments
            SET status = 'closed', closed_episode = %s, closed_at = now(),
                close_reason = %s
            WHERE id = %s AND status = 'open'
            """,
            (episode_id, f"{best_via} match ({best_score:.2f}): {text[:200]!r}", best_id),
        )
        if cur.rowcount > 0:
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
    cur.execute(stale_sql(has_last_seen=_seen_columns_ready(cur)))
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
        """
        SELECT c.id, c.text, c.kind, c.owner
        FROM commitments c
        JOIN episodes e ON e.id = c.opened_episode
        WHERE c.status = 'open' AND e.session_id = %s
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
    mentions = [t for t, _ in _candidate_close_texts(payload)]
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
        owner = resolve_owner(
            row["text"], kind=row.get("kind"), declared=row.get("owner"),
            future_trigger=trigger,
        )
        if _is_user_owed(row) or owner == OWNER_USER:
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
                (m for m in mentions if _match_score(m, row["text"]) >= DONE_MATCH_MIN_SCORE),
                None,
            )
            if hit is None:
                continue
            reason = f"session-plan (mentioned): {hit[:150]!r}"
        else:
            continue
        cur.execute(
            """
            UPDATE commitments
            SET status = 'closed', closed_episode = %s, closed_at = now(),
                close_reason = %s
            WHERE id = %s AND status = 'open'
            """,
            (episode_id, reason, row["id"]),
        )
        if cur.rowcount > 0:
            closed += 1
    if closed:
        _log(f"closed {closed} session-end commitment(s) for {session_id}")
    return closed


def list_owed(cur, *, project: str | None = None, parent_session_id: str | None = None,
              session_id: str | None = None, status: str = "open",
              limit: int = 50) -> list[dict[str, Any]]:
    """``project`` if given, else ``parent_session_id``/``session_id`` (fix
    3) — the same coalesced key ``open_from_episode``/``auto_close`` store
    commitments under, so a caller with only session context (no resolved
    project) can still find its own scope's commitments."""
    scope = project or parent_session_id or session_id
    clauses = ["status = %s"]
    params: list[Any] = [status]
    if scope:
        clauses.append("project = %s")
        params.append(scope)
    params.append(limit)
    cols = ["id", "text", "project", "owner", "kind", "opened_episode", "opened_at",
            "due_after", "status", "closed_episode", "closed_at", "close_reason"]
    # Additive only (migration 0012). A pre-migration hub still answers, with
    # last_seen_at NULL and seen_count 1 — the desktop reads the same shape
    # either way and never has to branch on schema version.
    extra = _seen_columns_ready(cur)
    select_cols = list(cols) + (["last_seen_at", "seen_count"] if extra else [])
    if _future_trigger_ready(cur):
        select_cols.append("future_trigger")
    cur.execute(
        f"""
        SELECT {', '.join(select_cols)}
        FROM commitments
        WHERE {' AND '.join(clauses)}
        ORDER BY opened_at DESC
        LIMIT %s
        """,
        params,
    )
    rows = [dict(zip(select_cols, row)) for row in cur.fetchall()]
    for row in rows:
        row.setdefault("last_seen_at", None)
        row["seen_count"] = int(row.get("seen_count") or 1)
        # owner / future_trigger are RESOLVED here, not read raw: rows opened
        # before migration 0013 carry a model-supplied owner ("Peer 1",
        # "ASSISTANT", NULL) and no future_trigger at all, and the desktop's
        # "Needs you" section is exactly `owner == 'user'`. Deterministic
        # detection wins, so a legacy row lists the same way a new one does.
        trigger = has_future_trigger(row.get("text") or "") or bool(row.get("future_trigger"))
        row["future_trigger"] = trigger
        row["owner"] = resolve_owner(
            row.get("text") or "", kind=row.get("kind"),
            declared=row.get("owner"), future_trigger=trigger,
        )
        # Sort rank for the Owed surface: user-owed first (blocker → question →
        # everything else the user owes), then assistant promises with an
        # explicit future trigger, then the rest by kind. Computed here so the
        # desktop/gateway never re-derive (and never disagree with) it.
        row["priority"] = owed_priority(row["owner"], row.get("kind"), trigger)
    # Stable: the SQL order (opened_at DESC) survives inside each rank.
    rows.sort(key=lambda r: r["priority"])
    return rows


def set_status(cur, commitment_id: int, status: str) -> bool:
    if status not in ("open", "closed", "stale", "dropped"):
        raise ValueError(f"status must be open/closed/stale/dropped, got {status!r}")
    if status == "open":
        cur.execute(
            "UPDATE commitments SET status = 'open', closed_episode = NULL, "
            "closed_at = NULL, close_reason = NULL WHERE id = %s",
            (commitment_id,),
        )
    else:
        extra = ", closed_at = now()" if status == "closed" else ""
        cur.execute(
            f"UPDATE commitments SET status = %s{extra} WHERE id = %s",
            (status, commitment_id),
        )
    return cur.rowcount > 0
