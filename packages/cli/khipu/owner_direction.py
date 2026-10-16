"""Owner direction: an owner action taken on the owner's own typed words.

The third way past an owner gate, beside the app and a yes typed on a
terminal (scope: docs/plans/2026-10-16-owner-direction.md, section 2 and the
decisions in section 4). A conversation submits the action with the owner's
words quoted verbatim (``--owner-words``) and, where its harness shows hook
output, the turn token the prompt hook pushed (``--owner-turn``). ``check``
finds those words in a turn Khipu's own prompt hook recorded for the calling
session (``owner_turns``) and checks, by a fixed table in this file, that
they ask for this action on this target. Nothing here calls a model: this is
a permission decision.

The session is never an argument: it is read from the calling process's
environment, as ``routing.conversation_author`` reads it. A forged session
id points only at another session's real turns, and Claude Code and Codex
turns also need the token, which only that session's context ever held.

Every owner action through a gate (the app, a terminal, a direction) is a
line in ``<data dir>/owner-actions.jsonl`` with what it changed; a direction
is applied only when it can be undone from that line. A purge by direction
is scheduled a day out, and Undo cancels it until then.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from khipu import owner_turns

MIN_WORDS = 3
PURGE_DELAY_S = 24 * 3600
LOG_NAME = "owner-actions.jsonl"
PENDING_NAME = "owner-direction-pending.json"
CONFIRM_WORDS = ("yes", "confirm", "confirmed", "do it", "go ahead", "approve", "approved")
# A later turn that is only one of these confirms a pending widening, though
# shorter than ``MIN_WORDS`` (decision 1: a second "yes" turn).
WHOLE_TURN_CONFIRMS = ("yes", "yes please", "confirm", "do it", "go ahead", "approve", "yes do it")
_SESSION_VARS = ("KHIPU_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")
_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
                         "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"'})
_EDGE = " \t\n\"'.,;:!?()[]{}<>-–—*_`~"

_clock = time.time


class DirectionRefused(Exception):
    """The direction was not accepted; ``message`` is plain English the agent
    can relay to the owner."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def refusal(self) -> dict:
        return {"ok": False, "error": self.message, "code": self.code}


class AlreadyDone(Exception):
    """This turn already took this action on this target: the first result."""

    def __init__(self, result: dict):
        super().__init__("already done")
        self.result = result

    def answer(self) -> dict:
        return dict(self.result or {}, ok=True, already_done=True)


# ---- the action table --------------------------------------------------------------------
#
# Each owner action: the verb phrases that ask for it (any one; "a+b" needs
# both words), phrases that rule it out, how its target is named (``_names``),
# and whether it can be undone from its log line (``_UNDO``). A verb or a
# name matches whole words only, after ``normalise``.

ACTIONS: dict[str, dict[str, Any]] = {
    "route.pause": {"verbs": ("pause", "stop using", "hold off", "don't use", "do not use", "stop sending"),
                    "target": "account"},
    "route.lift": {"verbs": ("lift", "unpause", "resume", "back on", "start using", "use again"),
                   "target": "account"},
    "route.clear": {"verbs": ("clear", "dismiss", "ignore the report"), "target": "account"},
    "route.note.set": {"verbs": ("note",), "target": "note", "stores": True},
    "route.note.end": {"verbs": ("end", "clear", "remove", "drop", "delete"), "target": "note"},
    "route.rule.add": {"verbs": ("rule+add", "rule+new", "make it a rule", "make that a rule"), "target": "none",
                       "stores": True},
    "route.rule.edit": {"verbs": ("rule+change", "rule+edit", "rule+reword", "rule+update", "rule+say"),
                        "target": "rule", "stores": True},
    "route.rule.retire": {"verbs": ("retire", "remove", "drop", "delete", "get rid of"), "target": "rule"},
    "route.rule.restore": {"verbs": ("restore", "bring back", "put back"), "target": "rule"},
    "route.rule.confirm": {"verbs": ("confirm", "keep", "approve", "yes"), "target": "rule"},
    "route.rule.undo": {"verbs": ("undo", "put back", "revert"), "target": "rule"},
    "route.rule.dismiss": {"verbs": ("dismiss", "drop", "reject", "remove"), "target": "rule"},
    "route.hold.add": {"verbs": ("hold",), "target": "hold_scope", "stores": True},
    "route.hold.end": {"verbs": ("hold+end", "hold+lift", "hold+remove", "hold+drop", "hold+clear"),
                       "target": "given"},
    "route.priority.set": {"verbs": ("priority", "first", "before", "after", "behind", "ahead of", "prioritize",
                                     "prioritise", "order"),
                           "target": "accounts"},
    "route.priority.confirm": {"verbs": ("confirm", "keep", "approve", "yes"), "target": "priority"},
    "route.priority.undo": {"verbs": ("undo", "put back", "revert"), "target": "priority"},
    "route.priority.clear": {"verbs": ("clear", "remove", "drop", "end"), "target": "priority"},
    "accounts.track": {"verbs": ("track", "start tracking"), "not": ("stop tracking",), "target": "given"},
    "accounts.rename": {"verbs": ("rename", "call it", "name it"), "target": "account", "stores": True},
    "accounts.stop": {"verbs": ("stop tracking", "untrack"), "target": "account"},
    "accounts.move": {"verbs": ("move", "moved"), "target": "account", "stores": True},
    "accounts.decline": {"verbs": ("don't offer", "do not offer", "never offer", "stop offering", "decline",
                                   "not this one"), "target": "harness"},
    "accounts.offer_again": {"verbs": ("offer+again",), "not": ("don't offer", "do not offer", "never offer"),
                             "target": "harness"},
    "accounts.auto_track": {"verbs": ("auto track", "auto-track", "autotrack", "track automatically",
                                      "automatic tracking"), "target": "switch"},
    "accounts.request.confirm": {"verbs": ("confirm", "keep", "approve", "yes"), "target": "given"},
    "accounts.request.undo": {"verbs": ("undo", "put back", "revert"), "target": "given"},
    "config.usage_accounts": {"verbs": ("track", "account", "accounts", "rename"), "target": "given"},
    "config.routing_setting": {"verbs": ("set", "change", "make", "raise", "lower", "default", "reset",
                                         "turn on", "turn off", "switch"), "target": "setting", "stores": True},
    "config.answer_request.confirm": {"verbs": ("confirm", "keep", "approve", "yes"), "target": "setting"},
    "config.answer_request.undo": {"verbs": ("undo", "put back", "revert"), "target": "setting"},
    "promote.gate_ignore": {"verbs": ("update anyway", "skip", "ignore"), "target": "checks"},
    "promote.gate_ignore.cancel": {"verbs": ("cancel", "drop", "clear", "remove"), "target": "given"},
    "ledger.import": {"verbs": ("import",), "target": "file"},
    "ledger.restore": {"verbs": ("restore", "bring back", "put back", "undelete"), "target": "row"},
    "ledger.purge": {"verbs": ("purge", "for good", "permanently"), "target": "row"},
    "ledger.add": {"verbs": ("add", "new row", "record"), "target": "row", "stores": True},
    "ledger.set": {"verbs": ("set", "mark", "change", "update", "move"), "target": "row", "stores": True},
    "ledger.remove": {"verbs": ("remove", "delete", "drop"), "target": "row"},
    "work.hide": {"verbs": ("hide",), "target": "commitment"},
    "work.unhide": {"verbs": ("unhide", "show again", "show it again", "bring back"), "target": "commitment"},
    "work.same": {"verbs": ("same", "merge", "one thing"), "not": ("not the same", "not same", "aren't the same",
                                                                    "are not the same"), "target": "pair"},
    "work.not_same": {"verbs": ("not the same", "not same", "aren't the same", "are not the same", "different",
                                "split"),
                      "target": "pair"},
    "direction.off": {"verbs": ("off", "turn off", "disable", "stop"), "target": "direction"},
    # Trusting and untrusting a front end take their own verbs (TRUST_VERBS, UNTRUST_VERBS).
    "direction.set": {"verbs": ("set", "change", "make", "shorten", "lengthen", "turn on", "turn off"),
                      "target": "direction_setting", "stores": True},
    "direction.undo": {"verbs": ("undo", "put back", "revert", "reverse", "take back", "cancel"),
                       "target": "given"},
}

# direction.set's verbs follow its key: "trust X" widens what counts as the
# owner's word and "untrust X" narrows it, so neither may read as the other.
TRUST_VERBS = ("trust", "start trusting", "count", "allow", "accept")
UNTRUST_VERBS = ("untrust", "stop trusting", "don't trust", "do not trust", "dont trust", "no longer trust",
                 "distrust", "remove")

_DIRECTION_NAMES = {
    "latest_window_h": ["latest window", "latest turn window", "latest owner turn window"],
    "earlier_window_min": ["earlier window", "earlier turn window", "earlier owner turn window"],
    "session_binding": ["session binding", "session-only binding", "session only binding"],
}


# ---- the words --------------------------------------------------------------------------


def normalise(text: str) -> str:
    """Lowercase, straight quotes, whitespace collapsed."""
    return " ".join(str(text or "").translate(_QUOTES).lower().split())


def _quote(text: str) -> str:
    return normalise(text).strip(_EDGE)


def _has(haystack: str, needle: str) -> bool:
    """``needle`` (normalised, edges trimmed) as whole words of ``haystack``."""
    needle = _quote(needle)
    if not needle:
        return False
    return re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", haystack) is not None


def quote_in(quote: str, text: str) -> bool:
    """The quote, normalised, is a contiguous whole-word part of ``text``."""
    return _has(normalise(text), quote)


def _verb_in(words: str, verb: str) -> bool:
    return all(_has(words, part) for part in verb.split("+"))


def session_of(env=None) -> str:
    env = os.environ if env is None else env
    for name in _SESSION_VARS:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


# ---- naming targets --------------------------------------------------------------------------


def account_names(key: str) -> tuple[list[str], list[str]]:
    """The words that name an account (its label, its id, the CLI's name for a
    default login), and the other accounts' names that contain one of them
    (removed from the words first, so "Claude second" never names "Claude")."""
    from khipu import routing, usage

    known = routing.accounts()
    entry = next((a for a in known if a["key"] == key), None)
    harness, _, home = key.partition("|")
    names = [entry["label"]] if entry is not None else []
    if not home:
        names += [usage.LABELS.get(harness, harness), harness]
    names.append(routing.account_id(key))
    names = list(dict.fromkeys(_quote(n) for n in names if n))
    others = []
    for a in known:
        if a["key"] == key:
            continue
        label = _quote(a["label"])
        if any(_has(label, n) for n in names) and label not in names:
            others.append(label)
    return names, others


def _setting_names(key: str) -> list[str]:
    bare = key[len("routing_"):] if key.startswith("routing_") else key
    return list(dict.fromkeys([key, key.replace("_", " "), bare.replace("_", " "), bare.replace("_", "-")]))


def _row_names(target: str) -> list[str]:
    project, _, row = target.partition("#")
    return [row, target] if row else [project]


def _names(action: str, target: str, given) -> tuple[list[list[str]], list[str]]:
    """(groups, exclude): every group must be named by one of its names."""
    kind = ACTIONS[action]["target"]
    if given is not None:
        return [list(g) for g in given], []
    if kind == "account":
        names, others = account_names(target)
        return [names], others
    if kind == "accounts":
        groups, exclude = [], []
        for key in [k for k in target.split(",") if k]:
            names, others = account_names(key)
            groups.append(names)
            exclude += others
        return groups, [e for e in exclude if not any(e in g for g in groups)]
    if kind == "hold_scope":
        if target.startswith("account:"):
            names, others = account_names(target[len("account:"):])
            return [names], others
        return ([["machine", "mac", "this mac", "computer"]] if target == "machine"
                else [["all", "everything", "all work"]]), []
    if kind == "rule":
        return [[f"rule {target}", f"rule number {target}", f"rule #{target}"]], []
    if kind == "note":
        return [["note"]], []
    if kind == "priority":
        return [["priority", "order"]], []
    if kind == "setting":
        return [_setting_names(target)], []
    if kind == "checks":
        return [[c, c.replace("_", " "), c.replace("-", " ")] for c in target.split(",") if c], []
    if kind == "file":
        base = Path(target).name
        return [[base, Path(base).stem]], []
    if kind == "row":
        return [_row_names(target)], []
    if kind == "commitment":
        return [[f"commitment {target}", target]], []
    if kind == "pair":
        row, _, commitment = target.partition("|")
        return [_row_names(row), [commitment]], []
    if kind == "harness":
        from khipu import usage

        return [[target, usage.LABELS.get(target, target)]], []
    if kind == "switch":
        return [[target]], []
    if kind == "direction":
        return [["owner direction", "direction"]], []
    if kind == "direction_setting":
        if target in _DIRECTION_NAMES:
            return [_DIRECTION_NAMES[target]], []
        entry = target.partition(" ")[2] or target
        return [[entry, entry.replace(":", " ").replace("_", " ")]], []
    if kind == "none":
        return [], []
    return [[target]], []


def _without(words: str, phrases: list[str]) -> str:
    for phrase in sorted(phrases, key=len, reverse=True):
        words = re.sub(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", " | ", words)
    return words


# ---- the parser: which action, on what, asked for or against -------------------------------
#
# The words are cut into statements at these separators; a statement with no
# verb inherits the verb of the one before it ("pause Codex and Grok"). Verb
# phrases are found over one lexicon of every action's phrases, longest first
# and never overlapping, so "stop trusting" is never "trust" and "unpause"
# never "pause". A verb after a negator in its statement asks for nothing.

_SEPARATORS = re.compile(r"\s*(?:[,;]|\.(?=\s|$)|(?<!\w)(?:and then|then|and|but|also)(?!\w))\s*")
_NEGATOR = re.compile(r"(?<!\w)(?:no need to|do not|don't|dont|won't|wont|shouldn't|never|not|no|without)(?!\w)")
# "lift the pause on Codex": after one of these a verb of an account action
# is a noun, and asks for nothing.
_DETERMINERS = frozenset(("the", "a", "an", "that", "this", "my", "your", "its", "their", "any", "each"))
_PART = "+part"  # the first word of a two-word verb ("rule+add"): starts a statement, credits nothing alone
_ORDER_REVERSED = re.compile(r"(?<!\w)(?:after|behind|below|under)(?!\w)")


def _verbs(action: str, target: str) -> tuple[str, ...]:
    if action == "direction.set" and target.startswith("trust "):
        return TRUST_VERBS
    if action == "direction.set" and target.startswith("untrust "):
        return UNTRUST_VERBS
    return tuple(ACTIONS[action]["verbs"])


_LEXICON: dict[str, frozenset[str]] = {}


def _lexicon() -> dict[str, frozenset[str]]:
    """Every contiguous verb phrase, with the actions it asks for."""
    if not _LEXICON:
        lex: dict[str, set[str]] = {}
        for action, spec in ACTIONS.items():
            for verb in spec["verbs"]:
                if "+" in verb:
                    lex.setdefault(_quote(verb.split("+")[0]), set()).add(_PART)
                else:
                    lex.setdefault(_quote(verb), set()).add(action)
            for verb in spec.get("not", ()):
                lex.setdefault(_quote(verb), set())
        for verb in TRUST_VERBS + UNTRUST_VERBS:
            lex.setdefault(_quote(verb), set()).add("direction.set")
        _LEXICON.update({k: frozenset(v) for k, v in lex.items()})
    return _LEXICON


def _occurrences(text: str) -> list[tuple[int, int, str]]:
    found = []
    for phrase in _lexicon():
        for m in re.finditer(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text):
            found.append((m.start(), m.end(), phrase))
    found.sort(key=lambda o: (o[0] - o[1], o[0]))
    taken: list[tuple[int, int, str]] = []
    for occ in found:
        if all(occ[1] <= t[0] or occ[0] >= t[1] for t in taken):
            taken.append(occ)
    return sorted(taken)


def _kinds(actions) -> set[str]:
    return {ACTIONS[a]["target"] for a in actions if a in ACTIONS}


def _clauses(words: str) -> list[dict]:
    """The words as clauses: each with its verb occurrences, whether each is
    a noun here, and whether a negator stands before it."""
    lex = _lexicon()
    out = []
    for statement in _SEPARATORS.split(words):
        statement = statement.strip()
        if not statement:
            continue
        occs = []
        prev_end = 0
        for start, end, phrase in _occurrences(statement):
            before = statement[:start].split()
            occs.append({"start": start, "phrase": phrase, "acts": lex[phrase],
                         "noun": bool(before) and before[-1] in _DETERMINERS,
                         "neg": _NEGATOR.search(statement, prev_end, start) is not None,
                         "window": prev_end})
            prev_end = end
        # One statement with verbs of different actions on the same kind of
        # target ("lift Codex pause Grok") is cut before each such verb (and
        # before any negator that leads it).
        cuts, last = [0], None
        for occ in occs:
            acts = occ["acts"] - {_PART}
            if occ["noun"] or not acts:
                continue
            if last is not None and not (acts & last) and _kinds(acts) & _kinds(last):
                neg = _NEGATOR.search(statement, occ["window"], occ["start"])
                cuts.append(neg.start() if neg else occ["start"])
            last = acts
        cuts.append(len(statement))
        for a, b in zip(cuts, cuts[1:]):
            mine = [dict(o, start=o["start"] - a) for o in occs if a <= o["start"] < b]
            text = statement[a:b].strip()
            if text:
                out.append({"text": statement[a:b], "occs": mine,
                            "verbal": any(o["acts"] and not o["noun"] for o in mine)})
    return out


def _chains(words: str) -> list[dict]:
    """Clauses grouped by the verb they share: a verb clause, then the
    verbless clauses after it. A verbless clause with a negator ("but not
    Grok") inherits the verb as a refusal; verbless clauses before any verb
    lead the first verb clause."""
    chains: list[dict] = []
    lead: list[dict] = []
    for clause in _clauses(words):
        if clause["verbal"]:
            chains.append({"clauses": lead + [clause], "neg": False})
            lead = []
        elif _NEGATOR.search(clause["text"]):
            if chains:
                chains.append({"clauses": [clause], "neg": True})
        elif chains:
            chains[-1]["clauses"].append(clause)
        else:
            lead.append(clause)
    if not chains and lead:  # no verb anywhere: two-word verbs alone
        chains.append({"clauses": lead, "neg": False})
    for chain in chains:
        chain["text"] = " | ".join(c["text"] for c in chain["clauses"])
    return chains


def _verdict(chain: dict, action: str, target: str) -> str | None:
    """'ok' when ``chain`` asks for ``action``, 'neg' when it asks against
    it, 'not:<phrase>' when one of its phrases rules it out, else None."""
    spec, verbs = ACTIONS[action], _verbs(action, target)
    text = chain["text"]
    for phrase in spec.get("not", ()):
        if _verb_in(text, phrase):
            return f"not:{phrase}"
    plain = {_quote(v) for v in verbs if "+" not in v}
    noun_ok = spec["target"] != "account"
    hits = [o for c in chain["clauses"] for o in c["occs"] if o["phrase"] in plain and (noun_ok or not o["noun"])]
    if chain["neg"] and (hits or any(_verb_in(text, v) for v in verbs if "+" in v)):
        return "neg"
    if hits:
        return "neg" if any(o["neg"] for o in hits) else "ok"
    for verb in [v for v in verbs if "+" in v]:
        if _verb_in(text, verb):
            first = min(re.search(r"(?<!\w)" + re.escape(_quote(p)) + r"(?!\w)", text).start()
                        for p in verb.split("+"))
            return "neg" if _NEGATOR.search(text, 0, first) else "ok"
    return None


def _named_at(plain: str, group: list[str]) -> int | None:
    spots = [m.start() for n in group if n
             for m in [re.search(r"(?<!\w)" + re.escape(_quote(n)) + r"(?!\w)", plain)] if m]
    return min(spots) if spots else None


def _order_ok(plain: str, groups: list[list[str]]) -> bool:
    """The groups, in the order the command gives them, are the one order
    the words give: mention order, with "after", "behind", "below" or
    "under" between two names putting the later-named first."""
    import itertools

    if len(groups) < 2:
        return True
    if len(groups) > 7:
        return False
    spots = [_named_at(plain, g) for g in groups]
    if any(s is None for s in spots):
        return False
    mentioned = sorted(range(len(groups)), key=lambda i: spots[i])
    rules = []
    for a, b in zip(mentioned, mentioned[1:]):
        rules.append((b, a) if _ORDER_REVERSED.search(plain, spots[a], spots[b]) else (a, b))
    fits = [p for p in itertools.permutations(range(len(groups)))
            if all(p.index(x) < p.index(y) for x, y in rules)]
    return fits == [tuple(range(len(groups)))]


def _asks_for(action: str, target: str, quote: str, *, stored, names, exclude) -> str:
    """Step 4: the words ask for this action on this target, and any words
    the action stores are the owner's own. The words that asked for it."""
    spec = ACTIONS[action]
    words = _quote(quote)
    kept = [_quote(t) for t in (stored or []) if isinstance(t, str) and t.strip()]
    # The words an action stores are not the owner asking ("add a rule: never
    # run the fast tier" does not negate "add"), nor do they name its target
    # ("rename Grok to Codex main" does not name Codex); nor do other
    # accounts' longer names.
    chains = _chains(_without(words, kept))
    groups, auto_exclude = _names(action, target, names)
    others = [_quote(e) for e in list(exclude or []) + auto_exclude]
    verdicts = [(c, _verdict(c, action, target)) for c in chains]
    asked = [c for c, v in verdicts if v == "ok"]
    first = _verbs(action, target)[0].replace("+", " ")
    if not asked:
        blocked = next((v[4:] for _c, v in verdicts if v and v.startswith("not:")), None)
        if blocked:
            raise DirectionRefused("words_do_not_ask_for_this",
                                   f"The owner's words say \"{blocked}\", which does not ask for this. Ask the owner "
                                   "to say what they want in their own words.")
        if any(v == "neg" for _c, v in verdicts):
            raise DirectionRefused("words_do_not_ask_for_this",
                                   f"The owner's words say not to {first}. Ask the owner to say what they want in "
                                   "their own words.")
        raise DirectionRefused("words_do_not_ask_for_this",
                               f"The owner's words do not ask to {first}. Quote the part where the owner asks for "
                               "it, or ask the owner to say it again.")
    missing = None
    for chain in asked:
        plain = _without(chain["text"], others)
        gap = next((g for g in groups if not any(_has(plain, n) for n in g if n)), None)
        if gap is None:
            if spec["target"] == "accounts" and not _order_ok(plain, groups):
                raise DirectionRefused("words_do_not_ask_for_this",
                                       "The owner's words give a different order. Pass the accounts in the order "
                                       "the owner said them, or ask the owner to say the order again.")
            break
        missing = missing or gap
    else:
        group = missing or [target]
        shown = next((n for n in group if n and not re.fullmatch(r"[0-9a-f]{12,}", n)), group[0] if group
                     else target)
        raise DirectionRefused("words_do_not_ask_for_this",
                               f"The words do not name {_title(shown)}. Quote the part where the owner "
                               "names it, or ask the owner to say it again.")
    for text in [s for s in (stored or []) if isinstance(s, str) and s.strip()]:
        if not _has(words, text):
            raise DirectionRefused("words_do_not_ask_for_this",
                                   f"\"{text.strip()}\" is not in the owner's words. What Khipu keeps must be the owner's "
                                   "own words: use them exactly, or ask the owner to say it again.")
    return " | ".join([chain["text"]] + kept)


# ---- times: a timed change keeps the owner's time ------------------------------------------

_COUNT = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
          "thirty": 30, "forty five": 45, "forty-five": 45, "ninety": 90}
_UNIT_S = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400, "week": 604800}
_FOR = re.compile(r"(?<!\w)for (?:(?P<half>half an? hour|a half hour)|(?P<n>\d+(?:\.\d+)?|"
                  + "|".join(re.escape(k) for k in sorted(_COUNT, key=len, reverse=True))
                  + r")(?P<and_half> and a half)? (?P<unit>minute|min|hour|hr|day|week)s?)(?!\w)")
_UNTIL = re.compile(r"(?<!\w)(?:until|till|til|'til)\s+"
                    r"(?:(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<half>am|pm|a\.m\.|p\.m\.)"
                    r"|(?P<h24>\d{1,2}):(?P<m24>\d{2})|(?P<named>noon|midday|midnight)|(?P<vague>tomorrow))(?!\w)")
TIME_SLACK_S = 300
# Times Khipu can't resolve still name an end: the command must then carry
# one. "For now" and "for good" name none.
_VAGUE = re.compile(r"(?<!\w)(?:(?:until|till|til|'til|through)\s+\S+"
                    r"|for (?!now(?!\w)|good(?!\w))(?:a bit|a while|a little|the day|the night|the weekend|the week"
                    r"|the afternoon|the evening|the morning|today|tonight|(?:\d+(?:\.\d+)?|"
                    + "|".join(re.escape(k) for k in sorted(_COUNT, key=len, reverse=True) if k not in ("a", "an"))
                    + r"|a few|a couple of|a couple|an?)\s+\S+)"
                    r"|tonight|tomorrow|this (?:morning|afternoon|evening)|later today|today)(?!\w)")


def word_time(text: str, now: float) -> tuple[str | None, float | None]:
    """The time the words name, as (the words, the epoch time it resolves
    to, or None when Khipu can't resolve it); (None, None) when they name
    none. "For now" and "for good" name no time."""
    m = _FOR.search(text)
    if m:
        if m.group("half"):
            return m.group(0), now + 1800
        n = float(m.group("n")) if m.group("n")[0].isdigit() else float(_COUNT[m.group("n")])
        if m.group("and_half"):
            n += 0.5
        return m.group(0), now + n * _UNIT_S[m.group("unit")]
    m = _UNTIL.search(text)
    if not m:
        vague = _VAGUE.search(text)
        return (vague.group(0), None) if vague else (None, None)
    if m.group("vague"):
        return m.group(0), None
    if m.group("named"):
        hour, minute = (0, 0) if m.group("named") == "midnight" else (12, 0)
    elif m.group("h24"):
        hour, minute = int(m.group("h24")), int(m.group("m24"))
    else:
        hour, minute = int(m.group("h")), int(m.group("m") or 0)
        if not 1 <= hour <= 12:
            return m.group(0), None
        hour = hour % 12 + (12 if m.group("half").startswith("p") else 0)
    if hour > 23 or minute > 59:
        return m.group(0), None
    from datetime import timedelta

    local = datetime.fromtimestamp(now).astimezone()
    then = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if then <= local:
        then += timedelta(days=1)
    return m.group(0), then.timestamp()


def _same_time(asked: str, until, now: float) -> None:
    """A timed change (a pause, a hold, a priority) carries ``--until`` when
    the owner's words name a time, agreeing with it, and none when they
    don't: an agent can neither drop the owner's time nor add its own."""
    said, when = word_time(asked, now)
    given = str(until).strip() if until is not None else ""
    if not given:
        if said:
            raise DirectionRefused("time_not_the_owners",
                                   f"The owner said \"{said}\", so pass that time with --until (for example 2h or "
                                   "an ISO time); without it the change would never end.")
        return
    if not said:
        raise DirectionRefused("time_not_the_owners",
                               "The owner's words do not name a time, so leave --until out, or ask the owner how "
                               "long it should last.")
    if when is None:
        return
    from khipu import routing

    try:
        cmd = routing.parse_until(given, datetime.fromtimestamp(now, timezone.utc)).timestamp()
    except routing.RouteError:
        return  # the command itself refuses a time it can't read
    if abs(cmd - when) > TIME_SLACK_S:
        raise DirectionRefused("time_not_the_owners",
                               f"The owner said \"{said}\", which is not the time given with --until. Pass the "
                               "owner's time.")


def _title(name: str) -> str:
    return name[:1].upper() + name[1:] if name and name.islower() and " " not in name else name


# ---- the check ---------------------------------------------------------------------------


def check(action: str, target: str, words: str, *, turn_token: str | None = None, stored=None, names=None,
          exclude=None, env=None, now: float | None = None, widening: bool = False, value=None,
          owner_session: str | None = None, timed: bool = False, until=None) -> dict:
    """The owner-direction check (scope 2.3 with decision 1). The auth stamp
    for the record, or ``DirectionRefused``; ``AlreadyDone`` with the first
    result when this turn already took this action on this target.

    ``owner_session`` (a dispatched job, scope 2.4): the session the job was
    started from, whose turns the words must then be in. Accepted only when
    a turn of the caller's own session is that session's recorded brief.

    ``timed`` (a pause, a hold, a priority): the change can carry an end,
    ``until`` (its ``--until``), which must keep the owner's time."""
    if action not in ACTIONS:
        raise DirectionRefused("not_by_direction", "That change can't be made by direction.")
    now = _clock() if now is None else now
    conf = owner_turns.settings()
    if not conf["enabled"]:  # step 0
        raise DirectionRefused("direction_off", "Owner direction is off, so only the Khipu app or a yes typed on a "
                                                "terminal can make this change.")
    session = session_of(env)
    if not session:
        raise DirectionRefused("no_recorded_turn", "Khipu can't tell which conversation this is, so it can't find "
                                                   "the owner's words.")
    via_session = None
    if owner_session is not None:
        session, via_session = _parent_of(session, str(owner_session).strip(), now), session
    quote = str(words or "")
    turn = _the_turn(session, turn_token, quote, conf, now, short_ok=widening)  # steps 1 to 3
    _from_the_hook(turn, ancestry=via_session is None)
    if widening:  # step 6 (decision 1): a later turn confirms it
        if _confirms_pending(action, target, value, session, turn, quote, conf, now,
                             asks={"stored": stored, "names": names, "exclude": exclude}):
            return _stamp(turn, session, quote, turn_token, confirms=True, via_session=via_session)
        if len(_quote(quote).split()) < MIN_WORDS:
            _verbatim(quote, {"text": ""})
    asked = _asks_for(action, target, quote, stored=stored, names=names, exclude=exclude)  # step 4
    if timed:
        _same_time(asked, until, now)
    done = find_done(session, turn["turn"], action, target)  # step 5
    if done is not None:
        raise AlreadyDone(done.get("result") or {})
    if widening:
        _note_pending(action, target, value, session, turn, quote, now)
        raise DirectionRefused("needs_second_turn",
                               "This widens what counts as the owner's word, so it needs the owner's confirmation in a "
                               "later message of this conversation. Ask the owner to confirm (for example \"yes, do "
                               "it\"), then submit the same change again with their reply.")
    return _stamp(turn, session, quote, turn_token, via_session=via_session)


def _stamp(turn: dict, session: str, quote: str, token, *, confirms: bool = False,
           via_session: str | None = None) -> dict:
    stamp = {"auth": "direction", "harness": turn.get("harness"), "session": session, "turn": turn["turn"],
             "words": quote.strip(), "binding": "token" if token else "session"}
    if via_session:
        stamp["via_session"] = via_session
    if confirms:
        stamp["confirms"] = True
    return stamp


def _parent_of(caller: str, named: str, now: float) -> str:
    """The session a dispatched job names as its parent, when the job's own
    session has a recorded turn that is that session's brief (the delegation
    hook noted it, ``owner_turns.note_brief``). Anything else is refused: a
    job can never point at a session it was not started from."""
    linked = named and named != caller and any(
        t.get("delegated") and t.get("parent") == named for t in owner_turns.turns(caller, now))
    if not linked:
        raise DirectionRefused("no_recorded_turn",
                               "This conversation was not started from that one, so it can't act on the owner's "
                               "words there. Ask the owner to say it again here, or leave it to the conversation "
                               "they said it in.")
    return named


def _the_turn(session: str, token, quote: str, conf: dict, now: float, *, short_ok: bool = False) -> dict:
    """Steps 1 to 3: the recorded, eligible, fresh turn that holds the quote."""
    recorded = owner_turns.turns(session, now)
    usable = [t for t in recorded if owner_turns.eligible(t, conf)[0]]
    if token:
        turn = next((t for t in recorded if t.get("turn") == str(token).strip()), None)
        if turn is None:
            raise DirectionRefused("no_recorded_turn",
                                   "Khipu has no record of that owner turn in this conversation. Use the "
                                   "\"Owner turn\" token from the owner's latest message.")
        ok, why = owner_turns.eligible(turn, conf)
        if not ok:
            raise DirectionRefused("no_recorded_turn", f"That turn can't carry the owner's word: {why}.")
        _fresh(turn, usable, conf, now)
        _verbatim(quote, turn, short_ok=short_ok)
        return turn
    if not usable:
        raise DirectionRefused("no_recorded_turn",
                               "Khipu has no recorded owner turn in this conversation (its prompt hook may not "
                               "have run). Ask the owner to say it again here.")
    if any(t.get("harness") not in owner_turns.SESSION_ONLY_HARNESSES for t in usable):
        raise DirectionRefused("no_recorded_turn",
                               "This conversation's turns need their token: pass the \"Owner turn\" token "
                               "from the owner's message with --owner-turn.")
    stale = None
    for turn in reversed(usable):
        try:
            _fresh(turn, usable, conf, now)
        except DirectionRefused as err:
            stale = stale or err
            continue
        try:
            _verbatim(quote, turn, short_ok=short_ok)
        except DirectionRefused:
            continue
        return turn
    fresh_any = any(_is_fresh(t, usable, conf, now) for t in usable)
    if not fresh_any and stale is not None:
        raise stale
    _verbatim(quote, {"text": ""})
    raise AssertionError("unreachable")


_CHAIN_MOST = 16


def _caller_chain() -> list[tuple[int, float]]:
    """This process's ancestors, parent first, as (pid, start time). Stops
    where a parent started after its child (its number was reused)."""
    me = owner_turns._proc_info(os.getpid())
    if me is None:
        return []
    pid, born = me[0], me[2]
    out: list[tuple[int, float]] = []
    for _ in range(_CHAIN_MOST):
        info = owner_turns._proc_info(pid)
        if info is None or info[2] > born:
            break
        out.append((pid, info[2]))
        pid, born = info[0], info[2]
        if pid <= 1:
            break
    return out


def _from_the_hook(turn: dict, *, ancestry: bool = True) -> None:
    """The turn was recorded by a hook its harness ran: its issuer is not an
    agent's shell or a script, and (``ancestry``) is above this call, as the
    harness is above every command its conversation runs. A dispatched job
    acting on its parent's turn skips the ancestry test only: a job T3
    started is not a descendant of the parent conversation's harness."""
    issuer = turn.get("issuer")
    refused = not isinstance(issuer, dict) or "refused" in issuer or not isinstance(issuer.get("pid"), int)
    if not refused and ancestry:
        refused = (issuer["pid"], issuer.get("start")) not in _caller_chain()
    if refused:
        raise DirectionRefused("not_from_the_hook",
                               "That turn was not recorded by the prompt hook of the conversation making this call, "
                               "so it can't carry the owner's word. Ask the owner to say it again here.")


def _is_fresh(turn: dict, usable: list[dict], conf: dict, now: float) -> bool:
    latest = usable[-1] if usable else None
    age = now - float(turn.get("ts") or 0)
    if latest is not None and turn.get("turn") == latest.get("turn"):
        return age <= conf["latest_window_h"] * 3600
    return age <= conf["earlier_window_min"] * 60


def _fresh(turn: dict, usable: list[dict], conf: dict, now: float) -> None:
    if not _is_fresh(turn, usable, conf, now):
        raise DirectionRefused("stale",
                               "Those words are too old to act on (the owner's latest message counts for "
                               f"{conf['latest_window_h']:g} hours, an earlier one for {conf['earlier_window_min']:g} "
                               "minutes). Ask the owner to say it again.")


def _verbatim(quote: str, turn: dict, *, short_ok: bool = False) -> None:
    """Step 3. ``short_ok`` (a widening's confirmation only): a quote under
    ``MIN_WORDS`` counts when it is the whole turn and a confirmation."""
    q = _quote(quote)
    if short_ok and q in WHOLE_TURN_CONFIRMS and q == _quote(turn.get("text") or ""):
        return
    if len(q.split()) < MIN_WORDS or not _has(normalise(turn.get("text") or ""), q):
        raise DirectionRefused("not_the_owners_words",
                               f"Those are not the owner's words as they typed them. Quote at least {MIN_WORDS} "
                               "words of their message exactly, or ask the owner to say it again.")


# ---- step 6: widening needs a second turn ---------------------------------------------------


def _pending_path() -> Path:
    from khipu.paths import data_dir

    return data_dir() / PENDING_NAME


def _pending() -> list[dict]:
    try:
        data = json.loads(_pending_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []


def _save_pending(rows: list[dict]) -> None:
    _write_private(_pending_path(), json.dumps(rows, indent=2) + "\n")


def _note_pending(action, target, value, session, turn, quote, now) -> None:
    rows = [p for p in _pending() if now - float(p.get("ts") or 0) < owner_turns.RETENTION_S
            and not (p.get("session") == session and p.get("action") == action and p.get("target") == target)]
    rows.append({"action": action, "target": target, "value": value, "session": session, "turn": turn["turn"],
                 "turn_ts": turn.get("ts"), "ts": now, "words": quote.strip()})
    _save_pending(rows)


# A whole turn that is a confirmation, once these trailing words are dropped
# ("yes do it now", "Yes please, thanks").
_POLITE = re.compile(r"(?:\s+(?:please|now|thanks|thank you|ok|okay))+$")


def _whole_turn_confirm(quote: str, turn: dict) -> bool:
    q = _quote(quote)
    return q == _quote(turn.get("text") or "") and _POLITE.sub("", q).strip(_EDGE) in WHOLE_TURN_CONFIRMS


def _confirms_pending(action, target, value, session, turn, quote, conf, now, *, asks: dict) -> bool:
    """Whether this turn confirms the widening it was asked about: the very
    next owner turn after the one that asked for it, with that widening the
    only one noted between the two turns, and words that are a whole-turn
    confirmation or a confirmation that itself asks for this change."""
    words = _quote(quote)
    if not any(_has(words, w) for w in CONFIRM_WORDS):
        return False
    usable = [t for t in owner_turns.turns(session, now) if owner_turns.eligible(t, conf)[0]]
    at = next((i for i, t in enumerate(usable) if t.get("turn") == turn["turn"]), None)
    if not at:
        return False
    asked_in = usable[at - 1]
    since, until = float(asked_in.get("ts") or 0), float(turn.get("ts") or 0)
    rows = _pending()
    waiting = [p for p in rows if p.get("session") == session and since <= float(p.get("ts") or 0) <= until]
    if len(waiting) > 1:
        raise DirectionRefused("needs_second_turn",
                               "More than one change is waiting for the owner's yes, so a yes can't say which. Ask "
                               "the owner to confirm in words that name the change.")
    if not waiting:
        return False
    p = waiting[0]
    if p.get("turn") != asked_in.get("turn") or (p.get("action"), p.get("target"), p.get("value")) != (
            action, target, value):
        return False
    if now - since > conf["earlier_window_min"] * 60:
        return False
    if not _whole_turn_confirm(quote, turn):
        try:
            _asks_for(action, target, quote, **asks)
        except DirectionRefused:
            return False
    _save_pending([r for r in rows if r is not p])
    return True


# ---- the owner-actions log -------------------------------------------------------------------


def log_path() -> Path:
    from khipu.paths import data_dir

    return data_dir() / LOG_NAME


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


# The log is hash-chained (scope 2.8): each line carries ``prev``, the hash
# of the line before it, and ``hash``, over its own canonical form. The words
# are in that form only as ``words_sha``, so forgetting an episode can scrub a
# line's words (``"words": null, "scrubbed": true``) without breaking the
# chain. Lines from before the chain (no hash on any of them) may lead the
# log; the first chained line's ``prev`` is then ``LEGACY`` plus the digest
# of those lines, so changing them breaks the chain too.
_UNHASHED = ("hash", "words", "scrubbed")
LEGACY = "legacy:"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _line_hash(line: dict) -> str:
    body = {k: v for k, v in line.items() if k not in _UNHASHED}
    return _sha(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str))


def _legacy_prev(lines: list[str]) -> str:
    return LEGACY + _sha("".join(text + "\n" for text in lines))


def _prev_of(raw: str) -> str:
    lines = raw.splitlines()
    last = next((text for text in reversed(lines) if text.strip()), None)
    if last is None:
        return ""
    try:
        row = json.loads(last)
    except ValueError:
        row = None
    if isinstance(row, dict) and isinstance(row.get("hash"), str):
        return row["hash"]
    return _legacy_prev(lines)


def _append(line: dict) -> dict:
    """Append one line to the chain, under a lock so two writers never
    chain to the same line, and on disk before it returns."""
    import fcntl

    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(path.with_name(f".{path.name}.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raw = ""
        line = json.loads(json.dumps(line, ensure_ascii=False, default=str))
        for key in ("prev", "hash", "scrubbed"):
            line.pop(key, None)
        if isinstance(line.get("words"), str):
            line["words_sha"] = _sha(line["words"])
        else:
            line.pop("words_sha", None)
        line["prev"] = _prev_of(raw)
        line["hash"] = _line_hash(line)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(("\n" if raw and not raw.endswith("\n") else "") + json.dumps(line, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        os.close(lock_fd)
    return line


def verify_log() -> dict:
    """Whether the log's hash chain holds: ``first_bad`` names the first line
    that has no hash or a wrong one, does not follow the line before it, or
    whose words do not match their hash (line numbers count from 1)."""
    try:
        raw = log_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        raw = ""
    except OSError as err:
        return {"ok": False, "lines": 0, "first_bad": {"line": 0, "id": None, "kind": None,
                                                        "why": f"the log can't be read ({err.strerror})"}}
    texts = raw.splitlines()
    rows: list = []
    for text in texts:
        try:
            rows.append(json.loads(text) if text.strip() else None)
        except ValueError:
            rows.append(ValueError)
    count = sum(1 for r in rows if r is not None)
    first = next((i for i, r in enumerate(rows) if isinstance(r, dict) and "hash" in r), None)
    if first is None and all(r is None or isinstance(r, dict) for r in rows):
        out = {"ok": True, "lines": count, "first_bad": None}
        if count:
            out["note"] = "Every line was written before the log was chained."
        return out

    def bad(i: int, why: str) -> dict:
        row = rows[i] if isinstance(rows[i], dict) else {}
        return {"ok": False, "lines": count, "first_bad": {"line": i + 1, "id": row.get("id"),
                                                           "kind": row.get("kind"), "why": why}}

    start = first if first is not None else len(rows)
    for i in range(start):
        if rows[i] is ValueError:
            return bad(i, "it is not a readable line")
    if first is None:
        return {"ok": True, "lines": count, "first_bad": None}
    prev = _legacy_prev(texts[:first]) if first else ""
    for i in range(first, len(rows)):
        row = rows[i]
        if row is None:
            continue
        if not isinstance(row, dict):
            return bad(i, "it is not a readable line")
        if not isinstance(row.get("hash"), str):
            return bad(i, "it has no hash, so Khipu did not write it")
        if row.get("prev") != prev:
            if first and i == first and str(row.get("prev") or "").startswith(LEGACY):
                return bad(i, "a line written before the log was chained was changed")
            return bad(i, "it does not follow the line before it: a line is missing or was changed")
        if _line_hash(row) != row["hash"]:
            return bad(i, "it was changed after Khipu wrote it")
        words = row.get("words")
        if words is not None and (not isinstance(words, str) or row.get("words_sha") != _sha(words)):
            return bad(i, "its words do not match their hash")
        prev = row["hash"]
    out = {"ok": True, "lines": count, "first_bad": None}
    if first:
        out["note"] = f"The first {first} lines were written before the log was chained."
    return out


def doctor_block() -> dict:
    """Doctor's "owner actions log" row: red when the hash chain is broken,
    naming the first bad line."""
    chain = verify_log()
    bad = chain.get("first_bad")
    if bad:
        what = f"line {bad['line']}" + (f" ({bad['id']})" if bad.get("id") else "")
        summary = (f"The owner-actions log was changed outside Khipu at {what}: {bad['why']}. "
                   "Check that entry in Owner actions.")
        return {"ok": False, "chain": chain, "warnings": [summary], "summary": summary}
    summary = f"Owner-actions log: {chain['lines']} lines, chain intact."
    if chain.get("note"):
        summary += " " + chain["note"]
    return {"ok": True, "chain": chain, "warnings": [], "summary": summary}


def read_log() -> list[dict]:
    try:
        raw = log_path().read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for text in raw.splitlines():
        try:
            row = json.loads(text)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    return secrets.token_hex(4)


def find_done(session: str, turn: str, action: str, target: str) -> dict | None:
    for line in read_log():
        if (line.get("kind") == "action" and line.get("auth") == "direction" and line.get("session") == session
                and line.get("turn") == turn and line.get("action") == action and line.get("target") == target):
            return line
    return None


def line_state(line_id: str) -> str | None:
    """A logged action's state now: ``applied``, ``scheduled``, ``done``,
    ``cancelled``, ``undone``, or None when there is no such line."""
    state = None
    for line in read_log():
        if line.get("kind") == "action" and line.get("id") == line_id:
            state = line.get("state") or "applied"
        elif line.get("kind") == "state" and line.get("target") == line_id:
            state = line.get("state")
        elif line.get("kind") == "undo" and line.get("undoes") == line_id and state != "cancelled":
            state = "undone"
    return state


def _find(line_id: str) -> dict | None:
    return next((x for x in read_log() if x.get("kind") == "action" and x.get("id") == line_id), None)


# ---- around an owner write (every gate) --------------------------------------------------------

_PENDING: contextvars.ContextVar[dict | None] = contextvars.ContextVar("khipu_owner_action", default=None)


def intent(action: str, target: str, *, stored=None, names=None, exclude=None, widening: bool = False,
           value=None, facts: dict | None = None, timed: bool = False, until=None) -> dict:
    """What an owner command is about to do, for the check and the log.
    ``timed``: the command can carry an end, ``until`` as it was given."""
    return {"action": action, "target": str(target), "stored": stored, "names": names, "exclude": exclude,
            "widening": widening, "value": value, "facts": dict(facts or {}), "timed": timed, "until": until}


def gate(args, want: dict | None, *, env=None, now: float | None = None) -> dict:
    """The direction branch of an owner gate: the auth stamp, after the check
    passed and the action can be undone. Raises ``DirectionRefused`` or
    ``AlreadyDone``."""
    if want is None:
        raise DirectionRefused("not_by_direction",
                               "That change can't be made by direction: it needs the Khipu app or a yes typed on a "
                               "terminal.")
    stamp = check(want["action"], want["target"], getattr(args, "owner_words", None) or "",
                  turn_token=getattr(args, "owner_turn", None), stored=want.get("stored"), names=want.get("names"),
                  exclude=want.get("exclude"), env=env, now=now, widening=bool(want.get("widening")),
                  value=want.get("value"), owner_session=getattr(args, "owner_session", None),
                  timed=bool(want.get("timed")), until=want.get("until"))
    before = _before(want)
    if before is _UNREADABLE:
        raise DirectionRefused("before_unreadable",
                               "Khipu can't read what this would change right now, so it could not be undone; "
                               "nothing was changed. Try again when the database is back.")
    _PENDING.set({"intent": want, "stamp": stamp, "before": before})
    return stamp


def noted(want: dict | None, stamp: dict) -> None:
    """The app's or a terminal's owner action, to be logged once it is done."""
    if want is None:
        _PENDING.set(None)
        return
    try:
        before = _before(want)
    except Exception:  # noqa: BLE001 - the log never stops the owner's own change
        before = None
    _PENDING.set({"intent": want, "stamp": dict(stamp), "before": before})


def pending_direction() -> dict | None:
    pend = _PENDING.get()
    return pend["stamp"] if pend and pend["stamp"].get("auth") == "direction" else None


def finish(result: dict | None, **extra) -> dict | None:
    """Log the owner action ``gate``/``noted`` set up, now that it is done;
    the result, with the log line's id when it was a direction."""
    pend = _PENDING.get()
    if pend is None:
        return result
    _PENDING.set(None)
    want, stamp = pend["intent"], pend["stamp"]
    before = pend["before"]
    why = _no_undo(want["action"], before)
    line = {"kind": "action", "id": _new_id(), "at": _iso(_clock()), "action": want["action"],
            "target": want["target"], "before": None if before in (_NO_UNDO, _UNREADABLE) else before,
            "after": _after(want, result), "auth": stamp.get("auth")}
    if why:
        line.update(undo=None, no_undo=why)
    if want.get("facts"):
        line["facts"] = want["facts"]
    for key in ("words", "harness", "session", "via_session", "turn", "binding", "auth_chain"):
        if stamp.get(key) is not None:
            line[key] = stamp[key]
    line.update(extra)
    if isinstance(result, dict):
        line["result"] = {k: v for k, v in result.items() if k != "owner_action"}
    try:
        _append(line)
    except OSError:
        return result
    if stamp.get("auth") == "direction" and isinstance(result, dict):
        return dict(result, owner_action=line["id"])
    return result


def stamp_of(via: str | None) -> dict:
    """The routing stamp for an owner gate that ran (``routing._OWNER_AUTH``)."""
    from khipu import routing

    return dict(routing._OWNER_AUTH.get() or {"auth": {"app": "app", "cli": "terminal"}.get(via or "", via)})


# ---- before, after and undo, per action ----------------------------------------------------

_NO_UNDO = object()  # the change has no way back (logged with its reason)
_UNREADABLE = object()  # what it changes can't be read now: a direction is refused

# Why a change has no undo, in words the app shows beside it.
_NO_UNDO_WHY = {
    "route.clear": "A clear or a dismissed report can't be undone; a new report about the account replaces it.",
    "ledger.import": "An import can't be undone; the rows it brought in can be removed one by one.",
    "route.rule.confirm": "Confirming a seeded rule can't be undone; the rule can be retired.",
}


def _no_undo(action: str, before) -> str | None:
    if action in ("ledger.purge", "direction.undo"):
        return None
    if before is _UNREADABLE:
        return "What this changed could not be read at the time, so it can't be undone."
    if before is _NO_UNDO or action not in _UNDO:
        return _NO_UNDO_WHY.get(action, "This change can't be undone from its record line.")
    return None


def _entry(key: str) -> dict:
    from khipu import routing

    return next((a for a in routing.read_view()["accounts"] if a["key"] == key), {}) or {}


def _rule_now(n) -> dict:
    from khipu import routing

    return next((r for r in routing.read_view()["rules"] if str(r["n"]) == str(n)), {}) or {}


def _priority_rows(held: dict | None) -> list[dict] | None:
    if not held:
        return None
    return [{"account": e["account"], "role": e.get("role"), "until": e.get("until")} for e in held["entries"]]


def _before(want: dict):
    """What an owner action changes, read before it does (``before`` on its
    log line); ``_NO_UNDO`` when it can't be undone from the line."""
    from khipu import routing

    action, target = want["action"], want["target"]
    if action == "route.pause":
        entry = _entry(target)
        paused = entry.get("state") == routing.PAUSED
        return {"paused": paused, "until": entry.get("until") if paused else None}
    if action == "route.lift":
        return {"until": _entry(target).get("until")}
    if action in ("route.note.set", "route.note.end"):
        note = routing.read_view().get("note")
        return {"text": note["text"] if note else None}
    if action in ("route.rule.edit", "route.rule.undo"):
        return {"text": _rule_now(target).get("text")}
    if action == "route.rule.confirm":
        rule = _rule_now(target)
        if not rule.get("pending"):
            return _NO_UNDO
        return {"pending_kind": rule.get("pending_kind"), "prior_text": rule.get("prior_text")}
    if action == "route.hold.end":
        hold = next((h for h in routing.read_view()["holds"] if h["id"] == target), None)
        if hold is None:
            return None
        return {"text": hold.get("text"), "reason": hold.get("reason"), "scope": hold.get("scope"),
                "until": hold.get("until")}
    if action.startswith("route.priority."):
        held = routing.read_view().get("priority")
        if action == "route.priority.confirm":
            replaced = (held or {}).get("replaced")
            return {"entries": _priority_rows(replaced), "until": (replaced or {}).get("until")}
        return {"entries": _priority_rows(held), "until": (held or {}).get("until")}
    if action == "accounts.rename":
        return {"label": next((a["label"] for a in routing.accounts() if a["key"] == target), None)}
    if action == "accounts.stop":
        entry = next((a for a in routing.accounts() if a["key"] == target), None)
        return {"harness": entry["harness"], "home": entry.get("home"), "label": entry["label"]} if entry else None
    if action == "accounts.move":
        return {"home": target.partition("|")[2] or None}
    if action == "accounts.auto_track":
        from khipu import config

        return {"on": bool(config.auto_track())}
    if action in ("accounts.request.confirm", "accounts.request.undo", "config.usage_accounts", "accounts.track"):
        from khipu import config

        data = config.load_config()
        request = data.get("usage_accounts_request") if isinstance(data.get("usage_accounts_request"), dict) else {}
        stored = data.get("usage_accounts")
        if action == "accounts.request.confirm":
            return {"list": request.get("prior") if isinstance(request.get("prior"), list) else None}
        return {"list": stored if isinstance(stored, list) else None}
    if action == "config.routing_setting":
        from khipu import config

        data = config.load_config()
        return {"value": data.get(target), "set": target in data}
    if action in ("config.answer_request.confirm", "config.answer_request.undo"):
        from khipu import config

        data = config.load_config()
        waiting = routing._setting_requests(data).get(target) or {}
        if action == "config.answer_request.confirm":
            return {"value": waiting.get("prior"), "set": bool(waiting.get("prior_set"))}
        return {"value": data.get(target), "set": target in data}
    if action in ("promote.gate_ignore", "promote.gate_ignore.cancel"):
        from khipu import promote

        return {"checks": list(promote.gate_ignore_status().get("value") or [])}
    if action in ("work.hide", "work.unhide"):
        from khipu import work

        return {"hidden": work.read_hidden().get(target)}
    if action in ("work.same", "work.not_same"):
        from khipu import work

        row, _, commitment = target.partition("|")
        project, _, row_id = row.partition("#")
        pair = [project, row_id, commitment]
        links = work.read_links()
        return {"link": "same" if pair in links["same"] else "not_same" if pair in links["not_same"] else None}
    if action == "ledger.set":
        from khipu import ledger

        project, _, row_id = target.partition("#")
        try:
            row = ledger.get(project, row_id)["row"]
        except Exception:  # noqa: BLE001 - a row Khipu can't read now can't be put back
            return _UNREADABLE
        return {"fields": {k: row.get(k) for k in ("state", "evidence", "owner", "title", "link")}}
    if action == "direction.off":
        return {"enabled": owner_turns.settings()["enabled"]}
    if action == "direction.set":
        return {"settings": {k: owner_turns.settings()[k] for k in ("latest_window_h", "earlier_window_min",
                                                                   "trusted", "session_binding")}}
    if action in _UNDO or action in ("ledger.purge", "direction.undo"):
        return None
    return _NO_UNDO


def _after(want: dict, result) -> Any:
    if not isinstance(result, dict):
        return None
    keep = ("id", "n", "account", "until", "entries", "version", "state", "runs_at", "ended", "label", "accounts")
    return {k: result[k] for k in keep if k in result} or None


def _restore_setting(key: str, value, was_set: bool) -> None:
    from khipu import routing
    from khipu.config import mutate_config

    with routing._lock(), mutate_config() as data:
        if was_set:
            data[key] = value
        else:
            data.pop(key, None)
        requests = routing._setting_requests(data)
        requests.pop(key, None)
        if requests:
            data[routing.SETTINGS_REQUEST_KEY] = requests
        else:
            data.pop(routing.SETTINGS_REQUEST_KEY, None)


def _set_priority_back(before: dict, via: str) -> dict:
    from khipu import routing

    if before.get("entries"):
        return routing.set_priority(before["entries"], until=before.get("until"), via=via,
                                    expect=routing.priority_version())
    if routing.read_view().get("priority"):
        return routing.clear_priority(via=via, expect=routing.priority_version())
    return {"ok": True}


def _ledger_write(verb: str, target: str, fields: dict | None = None) -> dict:
    from khipu import ledger

    project, _, row_id = target.partition("#")
    op = ledger.new_op(verb, project, row_id, fields=fields or {}, from_app=True)
    op["source"] = "direction"
    return ledger.write(op)


def _undo_pause(line, via):
    from khipu import routing

    before = line.get("before") or {}
    if before.get("paused"):
        return routing.pause(line["target"], until=before.get("until"), via=via)
    return routing.lift(line["target"], via=via)


def _undo_hold_end(line, via):
    from khipu import routing

    before = line.get("before") or {}
    scope = before.get("scope") or {}
    kind = scope.get("kind")
    return routing.add_hold(before.get("text") or "", reason=before.get("reason") or "other", scope=kind,
                            account=scope.get("account"), until=before.get("until"), owner=True, via=via)


def _undo_track(line, via):
    from khipu import accounts, config

    before = (line.get("before") or {}).get("list")
    return accounts.save_list(json.dumps(before) if before else None, expect=config.accounts_version(), via=via)


def _undo_stop(line, via):
    from khipu import accounts, config

    b = line.get("before") or {}
    return accounts.track([{"harness": b.get("harness"), "home": b.get("home"), "label": b.get("label")}],
                          expect=config.accounts_version(), via=via)


def _undo_move(line, via):
    from khipu import accounts, config, usage

    new_key = usage._key({"harness": line["target"].partition("|")[0],
                          "home": (line.get("facts") or {}).get("folder")})
    return accounts.move(new_key, (line.get("before") or {}).get("home"), expect=config.accounts_version(), via=via)


def _undo_rename(line, via):
    from khipu import accounts, config

    return accounts.rename(line["target"], (line.get("before") or {}).get("label"), expect=config.accounts_version(),
                           via=via)


def _undo_hidden(line, via):
    from khipu import work

    hidden = work.read_hidden()
    mark = (line.get("before") or {}).get("hidden")
    if mark is None:
        hidden.pop(line["target"], None)
    else:
        hidden[line["target"]] = mark
    work._write_json(work._hidden_file(), hidden)
    return {"ok": True}


def _undo_link(line, via):
    from khipu import work

    row, _, commitment = line["target"].partition("|")
    project, _, row_id = row.partition("#")
    pair = [project, row_id, commitment]
    links = work.read_links()
    for key in ("same", "not_same"):
        links[key] = [x for x in links[key] if x != pair]
    was = (line.get("before") or {}).get("link")
    if was:
        links[was].append(pair)
    work._write_json(work._links_file(), links)
    return {"ok": True}


def _undo_rule_confirm(line, via):
    from khipu import routing

    before = line.get("before") or {}
    if before.get("pending_kind") == "changed" and isinstance(before.get("prior_text"), str):
        return routing.edit_rule(line["target"], before["prior_text"], via=via)
    return routing.retire_rule(line["target"], via=via)


def _undo_ledger_set(line, via):
    fields = (line.get("before") or {}).get("fields") or {}
    return _ledger_write("set", line["target"], fields)


def _undo_direction(line, via):
    before = line.get("before") or {}
    if line["action"] == "direction.off":
        return {"ok": True, "settings": owner_turns.write_settings({"enabled": bool(before.get("enabled"))})}
    return {"ok": True, "settings": owner_turns.write_settings(dict(before.get("settings") or {}))}


def _b(line: dict) -> dict:
    return line.get("before") or {}


def _undo_lift(line, via):
    from khipu import routing

    return routing.pause(line["target"], until=_b(line).get("until"), via=via)


def _undo_note_set(line, via):
    from khipu import routing

    text = _b(line).get("text")
    return routing.set_note(text, via=via) if text else routing.end_note(via=via)


def _undo_note_end(line, via):
    from khipu import routing

    return routing.set_note(_b(line).get("text") or "", via=via)


def _undo_rule_words(line, via):
    from khipu import routing

    return routing.edit_rule(line["target"], _b(line).get("text") or "", via=via)


def _retire_rule(n_of):
    def run(line, via):
        from khipu import routing

        return routing.retire_rule(n_of(line), via=via)
    return run


def _undo_restore_rule(line, via):
    from khipu import routing

    return routing.restore_rule(line["target"], via=via)


def _undo_hold_add(line, via):
    from khipu import routing

    return routing.end_hold((line.get("after") or {}).get("id"), owner=True, via=via)


def _undo_priority(line, via):
    return _set_priority_back(_b(line), via)


def _undo_decline(line, via):
    from khipu import accounts

    return accounts.offer_again(line["target"], (line.get("facts") or {}).get("folder"))


def _undo_offer_again(line, via):
    from khipu import accounts

    return accounts.decline(line["target"], (line.get("facts") or {}).get("folder"))


def _undo_auto_track(line, via):
    from khipu import accounts

    return accounts.set_auto_track(bool(_b(line).get("on")))


def _undo_setting(line, via):
    _restore_setting(line["target"], _b(line).get("value"), bool(_b(line).get("set")))
    return {"ok": True}


def _undo_gate_ignore(line, via):
    from khipu import promote

    promote.set_gate_ignore(_b(line).get("checks") or None, by=_by(via))
    return {"ok": True}


def _ledger_undo(verb: str):
    def run(line, via):
        return _ledger_write(verb, line["target"])
    return run


_UNDO = {
    "route.pause": _undo_pause,
    "route.lift": _undo_lift,
    "route.note.set": _undo_note_set,
    "route.note.end": _undo_note_end,
    "route.rule.add": _retire_rule(lambda line: (line.get("after") or {}).get("n")),
    "route.rule.edit": _undo_rule_words,
    "route.rule.retire": _undo_restore_rule,
    "route.rule.restore": _retire_rule(lambda line: line["target"]),
    "route.rule.confirm": _undo_rule_confirm,
    "route.rule.undo": _undo_rule_words,
    "route.rule.dismiss": _undo_restore_rule,
    "route.hold.add": _undo_hold_add,
    "route.hold.end": _undo_hold_end,
    "route.priority.set": _undo_priority,
    "route.priority.confirm": _undo_priority,
    "route.priority.undo": _undo_priority,
    "route.priority.clear": _undo_priority,
    "accounts.track": _undo_track,
    "accounts.rename": _undo_rename,
    "accounts.stop": _undo_stop,
    "accounts.move": _undo_move,
    "accounts.decline": _undo_decline,
    "accounts.offer_again": _undo_offer_again,
    "accounts.auto_track": _undo_auto_track,
    "accounts.request.confirm": _undo_track,
    "accounts.request.undo": _undo_track,
    "config.usage_accounts": _undo_track,
    "config.routing_setting": _undo_setting,
    "config.answer_request.confirm": _undo_setting,
    "config.answer_request.undo": _undo_setting,
    "promote.gate_ignore": _undo_gate_ignore,
    "promote.gate_ignore.cancel": _undo_gate_ignore,
    "ledger.restore": _ledger_undo("remove"),
    "ledger.add": _ledger_undo("remove"),
    "ledger.set": _undo_ledger_set,
    "ledger.remove": _ledger_undo("restore"),
    "work.hide": _undo_hidden,
    "work.unhide": _undo_hidden,
    "work.same": _undo_link,
    "work.not_same": _undo_link,
    "direction.off": _undo_direction,
    "direction.set": _undo_direction,
}


def _by(via: str | None) -> str:
    return {"app": "app", "cli": "terminal"}.get(via or "", via or "?")


def _widens(before: dict | None, after: dict) -> bool:
    """Whether settings ``after`` count more as the owner's word than ``before``."""
    before = before or owner_turns.settings()
    if after.get("enabled") and not before.get("enabled", True):
        return True
    for key in ("latest_window_h", "earlier_window_min"):
        if key in after and float(after[key]) > float(before.get(key) or 0):
            return True
    if after.get("session_binding") and not before.get("session_binding"):
        return True
    return bool(set(after.get("trusted") or []) - set(before.get("trusted") or []))


# ---- the scheduled purge --------------------------------------------------------------------


def schedule_purge(project: str, row_id: str) -> dict:
    """A purge by direction (decision 3): logged as scheduled a day out."""
    runs_at = _clock() + PURGE_DELAY_S
    out = {"ok": True, "verb": "purge", "id": row_id, "project": project, "state": "scheduled",
           "runs_at": _iso(runs_at),
           "line": f"{row_id} will be removed for good at {_iso(runs_at)}; Undo cancels it until then"}
    return finish(out, state="scheduled", runs_at=_iso(runs_at), runs_at_ts=runs_at) or out


def run_due(now: float | None = None) -> list[str]:
    """Run each scheduled purge whose day has passed. Never raises: one that
    fails stays scheduled and is tried next time."""
    now = _clock() if now is None else now
    done: list[str] = []
    try:
        lines = read_log()
    except Exception:  # noqa: BLE001
        return done
    for line in lines:
        if line.get("kind") != "action" or line.get("action") != "ledger.purge" or line.get("state") != "scheduled":
            continue
        if line_state(line["id"]) != "scheduled" or float(line.get("runs_at_ts") or 0) > now:
            continue
        try:
            from khipu import ledger

            project, _, row_id = str(line["target"]).partition("#")
            conn = ledger._connect()
            try:
                ledger.purge(conn, project, row_id)
            finally:
                conn.close()
            _append({"kind": "state", "id": _new_id(), "at": _iso(now), "target": line["id"], "state": "done"})
            done.append(line["id"])
        except Exception:  # noqa: BLE001 - the ledger command that called this goes on
            continue
    return done


# ---- undo ----------------------------------------------------------------------------------


def undo_names(line: dict) -> list[list[str]]:
    """What names a logged action in an owner's "undo that": its id, its
    target as the action names it, or "that"/"it" when it is the newest."""
    names = [line["id"]]
    try:
        groups, _ex = _names(line["action"], line["target"], None)
        for group in groups:
            names += group
    except Exception:  # noqa: BLE001
        pass
    newest = [x for x in read_log() if x.get("kind") == "action"]
    if newest and newest[-1].get("id") == line["id"]:
        names += ["that", "it", "the last one"]
    return [names]


def undo(line_id: str, via: str) -> dict:
    """Apply the inverse of a logged action and log it (``undoes``)."""
    line = _find(line_id)
    if line is None:
        raise ValueError(f"no owner action {line_id} is in the log")
    state = line_state(line_id)
    if state in ("undone", "cancelled"):
        raise ValueError(f"owner action {line_id} was already undone")
    if line["action"] == "ledger.purge":
        if state != "scheduled":
            raise ValueError(f"the purge {line_id} already ran; a row removed for good can't come back")
        _append({"kind": "state", "id": _new_id(), "at": _iso(_clock()), "target": line_id, "state": "cancelled"})
        return finish({"ok": True, "undid": line_id, "state": "cancelled"}) or {"ok": True}
    fn = _UNDO.get(line["action"])
    if fn is None or "no_undo" in line:
        raise ValueError(line.get("no_undo") or f"owner action {line_id} ({line['action']}) can't be undone")
    out = fn(line, via)
    result = dict(out) if isinstance(out, dict) else {"ok": True}
    result.update(ok=True, undid=line_id)
    return result


# ---- the CLI: khipu owner-direction --------------------------------------------------------


def _print(data: dict) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _set_values(key: str, raw: str) -> dict:
    current = owner_turns.settings()
    if key in ("latest_window_h", "earlier_window_min"):
        try:
            value = float(raw)
        except ValueError:
            raise ValueError(f"{key} takes a number") from None
        if not value > 0:
            raise ValueError(f"{key} must be above 0")
        return {key: value}
    if key == "session_binding":
        if raw not in ("on", "off"):
            raise ValueError("session_binding takes on or off")
        return {key: raw == "on"}
    if key in ("trust", "untrust"):
        if not re.fullmatch(r"[a-z0-9_]+:[A-Za-z0-9_.-]+", raw or ""):
            raise ValueError(f"{key} takes a front end as harness:entrypoint")
        trusted = [t for t in current["trusted"] if t != raw] + ([raw] if key == "trust" else [])
        return {"trusted": trusted}
    raise ValueError("set takes latest_window_h, earlier_window_min, session_binding, trust or untrust")


def run_cli(args) -> int:
    """``khipu owner-direction``: show, set, on, off, actions, undo."""
    from khipu import routing

    cmd = getattr(args, "od_cmd", None) or "show"
    if cmd == "show":
        conf = owner_turns.settings()
        lines = [x for x in read_log() if x.get("kind") == "action"]
        _print({"ok": True, "settings": conf, "turns": owner_turns.counts(),
                "actions": len(lines), "by_direction": sum(1 for x in lines if x.get("auth") == "direction")})
        return 0
    if cmd == "actions":
        run_due()
        rows = [dict(x, state=line_state(x["id"])) for x in read_log() if x.get("kind") == "action"]
        rows.reverse()
        chain = verify_log()
        if getattr(args, "json", False):
            _print({"ok": True, "actions": rows, "chain": chain})
        else:
            if chain["first_bad"]:
                print(doctor_block()["summary"])
            for x in rows:
                print(f"{x['id']}  {x['at']}  {x['action']} {x['target']}  ({x.get('auth')}, {x['state']})"
                      + (f"  \"{x['words']}\"" if x.get("words") else ""))
            if not rows:
                print("No owner actions recorded.")
        return 0
    auth = routing._OWNER_AUTH.set(None)
    try:
        if cmd == "undo":
            line = _find(args.id)
            if line is None:
                _print({"ok": False, "error": f"no owner action {args.id} is in the log", "code": "not_found"})
                return 2
            if line_state(args.id) in ("undone", "cancelled"):
                _print({"ok": False, "error": f"owner action {args.id} was already undone", "code": "undone"})
                return 2
            widening = False
            if line["action"] in ("direction.set", "direction.off"):
                widening = _widens(None, (line.get("before") or {}).get("settings")
                                   or {"enabled": (line.get("before") or {}).get("enabled")})
            want = intent("direction.undo", args.id, names=undo_names(line), widening=widening, value=args.id)
            via = routing._owner_via(args, want)
            if via is None:
                _print({"ok": False, "error": routing._REFUSED, "code": "owner_only"})
                return 2
            out = undo(args.id, via)
            _PENDING.set(None)
            _append({"kind": "undo", "id": _new_id(), "at": _iso(_clock()), "undoes": args.id,
                     "action": line["action"], "target": line["target"], **{k: v for k, v in stamp_of(via).items()
                                                                            if k != "confirms"}})
            _print(out)
            return 0
        if cmd == "on":
            via = routing._owner_via(args, None)
            if via is None:
                _print({"ok": False, "error": routing._REFUSED, "code": "owner_only"})
                return 2
            _print({"ok": True, "settings": owner_turns.write_settings({"enabled": True})})
            return 0
        if cmd == "off":
            want = intent("direction.off", "direction")
            via = routing._owner_via(args, want)
            if via is None:
                _print({"ok": False, "error": routing._REFUSED, "code": "owner_only"})
                return 2
            out = {"ok": True, "settings": owner_turns.write_settings({"enabled": False})}
            _print(finish(out) or out)
            return 0
        if cmd == "set":
            try:
                values = _set_values(args.key, args.value)
            except ValueError as err:
                _print({"ok": False, "error": str(err), "code": "bad_value"})
                return 2
            target = args.key if args.key not in ("trust", "untrust") else f"{args.key} {args.value}"
            stored = [args.value] if args.key in ("latest_window_h", "earlier_window_min") else None
            want = intent("direction.set", target, stored=stored, widening=_widens(None, values),
                          value=json.dumps(values, sort_keys=True))
            via = routing._owner_via(args, want)
            if via is None:
                _print({"ok": False, "error": routing._REFUSED, "code": "owner_only"})
                return 2
            out = {"ok": True, "settings": owner_turns.write_settings(values)}
            _print(finish(out) or out)
            return 0
        _print({"ok": False, "error": f"unknown owner-direction command {cmd!r}", "code": "bad_command"})
        return 2
    except DirectionRefused as err:
        _print(err.refusal())
        return 2
    except AlreadyDone as done:
        _print(done.answer())
        return 0
    except (ValueError, routing.RouteError) as err:
        _print({"ok": False, "error": str(err), "code": getattr(err, "code", None) or "refused"})
        return 2
    finally:
        _PENDING.set(None)
        routing._OWNER_AUTH.reset(auth)


def add_owner_flags(parser) -> None:
    """``--owner-words``, ``--owner-turn`` and ``--owner-session`` on an owner
    command."""
    parser.add_argument("--owner-words", default=None, metavar="WORDS",
                        help="The owner's words, quoted exactly from their message, that ask for this change")
    parser.add_argument("--owner-turn", default=None, metavar="TOKEN",
                        help="The \"Owner turn\" token from the owner's message")
    parser.add_argument("--owner-session", default=None, metavar="SESSION",
                        help="A dispatched job: the session it was started from, where the owner typed the words")


def add_parser(sub) -> None:
    p = sub.add_parser("owner-direction", help="Owner direction: its settings, the owner-actions log, and Undo")
    p.set_defaults(func=run_cli, od_cmd=None)
    verbs = p.add_subparsers(dest="od_cmd")
    verbs.add_parser("show", help="The settings and how many turns and actions are recorded")
    act = verbs.add_parser("actions", help="The owner-actions log, newest first")
    act.add_argument("--json", action="store_true")
    for name, words in (("on", "Turn owner direction on (the app, or a typed yes)"),
                        ("off", "Turn owner direction off")):
        sp = verbs.add_parser(name, help=words)
        sp.add_argument("--from-app", action="store_true", help="==SUPPRESS==")
        add_owner_flags(sp)
    st = verbs.add_parser("set", help="Change a setting: latest_window_h, earlier_window_min, session_binding, "
                                      "trust or untrust")
    st.add_argument("key")
    st.add_argument("value")
    st.add_argument("--from-app", action="store_true", help="==SUPPRESS==")
    add_owner_flags(st)
    un = verbs.add_parser("undo", help="Undo one owner action by its id")
    un.add_argument("id")
    un.add_argument("--from-app", action="store_true", help="==SUPPRESS==")
    add_owner_flags(un)
