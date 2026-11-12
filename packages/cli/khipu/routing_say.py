"""``khipu route say``: change the order or the rules by saying so, see what
it will do, then save exactly that (desktop app, Accounts > Order and rules).

``--preview "<words>"`` turns the owner's words into one change with plain
code (no model, no embedding, nothing sent anywhere) and returns what Khipu
understood, how things are now, how they would be after, and a token.
Nothing is written to the routing record, the owner-actions log or the
proposals: the only file a preview touches is the token file.
``--apply TOKEN`` runs the change the token names through the existing
owner command with ``--from-app`` (``route pause``, ``route lift``,
``route cap``, ``route priority``, ``route rule``), so the owner gate, the
owner-actions log and Undo are the ones those commands already have. This
module adds an entry for the app, not a rule and not a second way in: both
modes are refused unless the Khipu app itself started the process
(``routing._caller``), exactly as ``--from-app`` is refused everywhere else.

A token is used once and expires after ``TOKEN_TTL_S``. It holds the fixed
argument list to run and a pin: the existing ``expect`` of the thing being
changed (the priority's version, a rule's revision) or, where a command
takes none (a pause, a cap, adding a rule), the same state read again at
apply. When the record moved since the preview, apply changes nothing and
says so. The token is not a secret and grants no more than the app could
already do, so it is stored as plain JSON in Khipu's private state folder
(0700, file 0600, like the other owner state), without signing or hashing.

Times come from code, in this Mac's local time like every ``--until``: a day
with no time of day means the start of that day. A time that is ambiguous,
unreadable or already past is not guessed: the answer is "not understood"
with the reason, and nothing is offered to apply.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from khipu import owner_turns, routing, routing_directions

TOKEN_FILE = "route-say-tokens.json"
TOKEN_TTL_S = 600
WORDS_MOST = 500
KINDS = ("priority_first", "priority_first_until", "pause", "resume", "stop_at", "end_rule", "add_rule")
FALLBACK_LABEL = "Add it as a rule in your words"
CHANGED = "This changed since you saw it, so nothing was changed. Preview it again."

_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                 "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12}
_NUM = r"\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|seven|eight|nine|ten|twelve"
_UNITS = {"m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
          "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
          "d": 86400, "day": 86400, "days": 86400, "w": 604800, "week": 604800, "weeks": 604800}
_DURATION = re.compile(rf"(?:the\s+next\s+)?({_NUM})\s*(minutes?|mins?|hours?|hrs?|days?|weeks?|[mhdw])", re.I)
_WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
             "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
             "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
_MONTHS = {"january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
           "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
           "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
           "december": 12, "dec": 12}
_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))
_CLOCK = re.compile(r"(?:\s+at)?\s+(?:(?P<noon>noon)|(?P<h>\d{1,2})(?::(?P<mi>\d{2}))?\s*(?P<ap>a\.?m\.?|p\.?m\.?)"
                    r"|(?P<h24>\d{1,2}):(?P<mi24>\d{2}))$", re.I)
_LEAD = re.compile(r"^(?:please|pls|hey|ok|okay|can you|could you|would you|will you|i want you to|"
                   r"i'd like you to|i would like you to|let's|lets|go ahead and)\b[,\s]*", re.I)
_UNTIL = re.compile(r"\s+(?:until|till|til)\s+(?P<t>.+)$", re.I)
_FOR = re.compile(rf"\s+for\s+(?P<t>(?:the\s+next\s+)?(?:{_NUM})\s*(?:minutes?|mins?|hours?|hrs?|days?|weeks?|[mhdw]))$", re.I)
_OPEN_ENDED = re.compile(r"^(?:i\s+(?:resume|lift|say|tell|turn)|you\s+(?:hear|are told)|further notice|forever|ever|"
                         r"i\s+say|i\s+tell\s+you|i\s+unpause)\b", re.I)

_RULE_END = re.compile(r"^(?:end|retire|remove|delete|drop|cancel|get rid of|stop)\s+(?:the\s+|my\s+)?(?:standing\s+)?rule\b\s*(?P<ref>.*)$", re.I)
_PERCENT = re.compile(r"(?P<pct>\d+(?:\.\d+)?)\s*(?:%|percent|per\s*cent)", re.I)
_STOP_WORD = re.compile(r"\b(?:stop|cap|limit|ceiling|max(?:imum)?|no more than|at most)\b", re.I)
_RESUME = re.compile(r"^(?:resume|unpause|un-pause|lift|restart|reopen)\b", re.I)
_RESUME_AGAIN = re.compile(r"^(?:use|start using|turn|put|send work to)\b.*\b(?:again|back on)\b", re.I)
_PAUSE = re.compile(r"^(?:pause|stop using|stop sending(?: work)? to|stop|don'?t use|do not use|hold off on|rest|"
                    r"take a break from|turn off)\b", re.I)
_FIRST_MOVE = re.compile(
    r"(?:(?:put|move|make|set|bump|push|use|prefer|try|send work to)\s+)?(?P<acct>.+?)\s+"
    r"(?:to\s+)?(?:the\s+)?(?:first(?:\s+(?:choice|priority|in\s+(?:the\s+)?(?:order|list|line)))?|number\s+one|#1|1st|"
    r"top(?:\s+priority)?|front|top\s+of\s+(?:the|my)\s+(?:order|list|priority))", re.I)


# ---- the words -------------------------------------------------------------------------------


class NotUnderstood(Exception):
    """The words name a change Khipu knows, but not one it can show safely."""

    def __init__(self, reason: str, considered: list[str] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.considered = considered or []


def _clean(words: str) -> str:
    text = " ".join((words or "").split())
    while True:
        stripped = _LEAD.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    text = text.strip(" \t.!?;,")
    return re.sub(r"[,\s]*\b(?:please|pls|thanks|thank you|thx)$", "", text, flags=re.I).strip(" \t.!?;,")


def _local(dt: datetime) -> datetime:
    return dt.astimezone()


def when_words(dt: datetime, now: datetime | None = None) -> str:
    """``Fri 14 Nov, 12:00 am`` in this Mac's time (with the year when it is not ``now``'s)."""
    local = _local(dt)
    hour = local.hour % 12 or 12
    year = f" {local.year}" if now is not None and local.year != _local(now).year else ""
    return f"{local:%a} {local.day} {local:%b}{year}, {hour}:{local:%M} {'am' if local.hour < 12 else 'pm'}"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _midnight(day) -> datetime:
    return datetime(day.year, day.month, day.day).astimezone()


def resolve_time(spec: str, now: datetime) -> datetime:
    """The end time ``spec`` names, as a UTC time after ``now``, or a
    ``NotUnderstood`` that says why. Lengths ("2 hours"), a weekday, tomorrow,
    a date, with or without a time of day, or an ISO time."""
    raw = " ".join((spec or "").replace(",", " ").split()).strip(" .")
    low = raw.lower()
    if not low:
        raise NotUnderstood("There is no time after \"until\". Say when it should end, for example \"until Friday\".")
    if _OPEN_ENDED.match(low):
        raise NotUnderstood("An open-ended change is not offered here: every pause and time-bound move ends by "
                            "itself. Say when it should end, for example \"until Friday\" or \"for 2 hours\".")
    local_now = now.astimezone()
    duration = _DURATION.fullmatch(low)
    if duration:
        n = _NUMBER_WORDS.get(duration.group(1)) or float(duration.group(1))
        seconds = n * _UNITS[duration.group(2)]
        if seconds <= 0:
            raise NotUnderstood(f"\"{raw}\" is not a length of time after now.")
        return (now + timedelta(seconds=seconds)).astimezone(timezone.utc).replace(microsecond=0)
    iso = re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[Tt ]\d{2}:\d{2}(?::\d{2})?(?:[Zz]|[+-]\d{2}:?\d{2})?)?", raw)
    if iso:
        try:
            dt = datetime.fromisoformat(raw.upper().replace("Z", "+00:00"))
            dt = dt.astimezone() if dt.tzinfo is None else dt
        except (ValueError, OverflowError, OSError):
            raise NotUnderstood(f"\"{raw}\" is not a date Khipu can read.") from None
        dt = dt.astimezone(timezone.utc).replace(microsecond=0)
        if dt <= now:
            raise NotUnderstood(f"{when_words(dt, now)} has already passed. Say a time that is still ahead.")
        return dt
    clock = None
    day_text = low
    padded = " " + low
    found = _CLOCK.search(padded)
    if found:
        day_text = padded[:found.start()].strip()
        if found.group("noon"):
            clock = (12, 0)
        elif found.group("h24"):
            hour, minute = int(found.group("h24")), int(found.group("mi24"))
            if hour > 23 or minute > 59:
                raise NotUnderstood(f"\"{raw}\" is not a time of day.")
            clock = (hour, minute)
        else:
            hour, minute = int(found.group("h")), int(found.group("mi") or 0)
            if not 1 <= hour <= 12 or minute > 59:
                raise NotUnderstood(f"\"{raw}\" is not a time of day.")
            pm = found.group("ap").lower().startswith("p")
            clock = (hour % 12 + (12 if pm else 0), minute)
    today = local_now.date()
    weekday = re.fullmatch(r"(?:next\s+)?(" + "|".join(sorted(_WEEKDAYS, key=len, reverse=True)) + r")", day_text)
    month_day = re.fullmatch(rf"(?:(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<m1>{_MONTH_NAMES})|(?P<m2>{_MONTH_NAMES})\s+(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?)"
                             r"(?:\s+(?P<y>\d{4}))?", day_text)
    explicit_year = False
    if day_text in ("", "today"):
        if clock is None:
            raise NotUnderstood("\"today\" with no time of day is already under way. Say a time, for example "
                                "\"until 6 pm\".")
        day = today
    elif day_text == "tomorrow":
        day = today + timedelta(days=1)
    elif weekday:
        target = _WEEKDAYS[weekday.group(1)]
        ahead = (target - today.weekday()) % 7
        name = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")[target]
        if ahead == 0 and day_text.startswith("next"):
            ahead = 7
        elif ahead == 0 and clock is None:
            raise NotUnderstood(f"Today is already {name}, so \"until {name}\" has no end ahead. Say \"next "
                                f"{name}\", or a time such as \"until 6 pm\".")
        day = today + timedelta(days=ahead)
    elif month_day:
        month = _MONTHS[month_day.group("m1") or month_day.group("m2")]
        number = int(month_day.group("d1") or month_day.group("d2"))
        explicit_year = month_day.group("y") is not None
        year = int(month_day.group("y")) if explicit_year else today.year
        try:
            day = datetime(year, month, number).date()
        except ValueError:
            raise NotUnderstood(f"\"{raw}\" is not a real date.") from None
        if day < today and not explicit_year:
            raise NotUnderstood(f"{raw} has already passed this year. Add the year if you mean a later one.")
    elif re.fullmatch(r"\d{1,2}", day_text) and clock is None:
        raise NotUnderstood(f"\"{raw}\" could be morning or evening. Say \"{raw} am\" or \"{raw} pm\".")
    else:
        raise NotUnderstood(f"I couldn't read \"{raw}\" as a time. Try \"Friday\", \"tomorrow at 6 pm\", "
                            "\"Nov 14\" or \"2 hours\".")
    if clock is None:
        end = _midnight(day)
    else:
        end = datetime(day.year, day.month, day.day, clock[0], clock[1]).astimezone()
    end = end.astimezone(timezone.utc).replace(microsecond=0)
    if end <= now:
        raise NotUnderstood(f"{when_words(end, now)} has already passed. Say a time that is still ahead, for example "
                            "\"tomorrow at 6 pm\".")
    return end


def _split_end(text: str) -> tuple[str, str | None]:
    """(the words without their end, the end's own words or None)."""
    m = _UNTIL.search(text)
    if m:
        return text[:m.start()].strip(), m.group("t").strip()
    m = _FOR.search(text)
    if m:
        return text[:m.start()].strip(), m.group("t").strip()
    return text, None


# ---- accounts and the record ------------------------------------------------------------------


def _scan(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """``([(key, label)], leftover words)``: each distinct account the words
    name, using the entity matching ``routing_directions`` uses (whole
    phrases from the routing record, the longest at one place winning), and
    the words that name no account."""
    names = routing_directions.known_entities()["accounts"]
    norm = routing_directions._norm_entity(text)
    spans = []
    for key, canonical in names.items():
        for m in re.finditer(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", norm):
            spans.append((m.start(), m.end(), canonical))
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    chosen, reach = [], -1
    for start, end, canonical in spans:
        if start >= reach:
            chosen.append((start, end, canonical))
            reach = end
    known = routing.accounts()
    out: list[tuple[str, str]] = []
    rest, last = [], 0
    for start, end, canonical in chosen:
        rest.append(norm[last:start])
        last = end
        try:
            key = routing._resolve_account_key(canonical, known)
        except routing.RouteError:
            continue
        if key not in [k for k, _ in out]:
            out.append((key, routing._label_of(key, known)))
    rest.append(norm[last:])
    return out, " ".join(" ".join(rest).split()).split()


def _accounts_named(text: str) -> list[tuple[str, str]]:
    return _scan(text)[0]


_FILLER = frozenset({"the", "a", "an", "account", "accounts", "agent", "agents", "work", "jobs", "job", "on", "to",
                     "from", "please", "now", "using", "use", "s", "first", "make", "put", "move", "set", "stop",
                     "at", "of", "for", "its", "limit", "cap", "ceiling", "max", "maximum", "week", "weekly",
                     "five", "hour", "hours", "5", "percent", "usage", "where", "no", "more", "than", "most",
                     "back", "again", "number", "one", "1st", "top", "priority", "front", "choice", "in", "order",
                     "list", "line", "and", "start", "turn", "put", "send", "back", "on"})


def _target_of(text: str, verb: str, *, strict: bool = True) -> tuple[str, str] | None:
    """The one account a short instruction names. ``None`` when the words
    carry more than that (a condition, a reason, a task): the caller offers
    them as a rule instead. ``NotUnderstood`` for none or for two accounts,
    naming which were considered."""
    found, left = _scan(text)
    extra = [w for w in left if w not in _FILLER]
    if strict and extra and found:
        return None
    if not found:
        if len(extra) > 3:
            return None
        known = ", ".join(a["label"] for a in routing.accounts()) or "none"
        raise NotUnderstood(f"I couldn't tell which account you mean. Say its name: {known}.", [])
    if len(found) > 1:
        labels = [label for _k, label in found]
        raise NotUnderstood(f"That names more than one account ({' and '.join(labels)}), so I'm not guessing "
                            f"which to {verb}. Say it once for each.", labels)
    return found[0]


def _order_words(labels: list[str]) -> str:
    shown = [f"{i} {label}" for i, label in enumerate(labels[:8], 1)]
    return ", ".join(shown) + (" …" if len(labels) > 8 else "")


def _clip(text: str, size: int = 140) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= size else text[:size - 1].rstrip() + "…"


def _priority_state(view: dict) -> tuple[dict | None, list[dict]]:
    held = view.get("priority")
    return held, list((held or {}).get("entries") or [])


def pin_value(spec: dict, now: datetime, view: dict | None = None) -> object:
    """What a change is pinned to (JSON-able), read from ``view`` or now
    from the record. The pins with no command of their own are the
    record's (``routing.pause_pin`` and the rest), which the command
    compares again under its lock (``--expect``)."""
    view = routing.read_view(now) if view is None else view
    kind = spec["type"]
    if kind == "priority":
        return view.get("priority_version") or "p0"
    if kind == "rules":
        return routing.rules_pin(view)
    if kind == "rule":
        rule = next((r for r in view.get("rules") or [] if str(r.get("n")) == str(spec["n"])), None)
        return rule.get("revision") if rule else None
    if kind == "paused":
        return routing.pause_pin(view, spec.get("key"))
    if kind == "caps":
        return routing.cap_pin(view, spec.get("key"), spec["window"])
    return None


def _expect(value) -> str:
    return "--expect=" + routing.pin_text(value)


def _keep_holds(event: dict, now: datetime) -> dict | None:
    """The existing owner write asks, for a priority or a rule that touches an
    account with a pause or a hold, whether to end or keep each one. A spoken
    change does only what was shown, so every one of them is kept (the same
    choice Undo makes); ``None`` when none is in the way. Read-only: the
    record's own check is run on the event and its refusal is read."""
    try:
        routing._require_hold_choices(dict(event, written_by=routing.OWNER), now)
    except routing.HoldsNeeded as err:
        return {i: "keep" for i in err.ids}
    return None


_KEPT = " Any pause or hold on these accounts stays as it is."


# ---- one change per shape --------------------------------------------------------------------


def _plan(kind: str, *, understood: str, now_words: str, after_words: str, argv: list[str], pin: dict,
          receipt: str, account: str | None = None, ends: datetime | None = None, fallback: bool = False,
          hold_event: dict | None = None, holds: dict | None = None) -> dict:
    return {"kind": kind, "hold_event": hold_event, "holds": holds, "understood": understood, "now": now_words,
            "after": after_words, "argv": argv,
            "pin": pin, "receipt": receipt, "account": account, "fallback": fallback,
            "ends": _iso(ends) if ends else None, "ends_words": when_words(ends) if ends else None}


def _priority_first(key: str, label: str, end_spec: str | None, now: datetime) -> dict:
    end = resolve_time(end_spec, now) if end_spec is not None else None
    view = routing.read_view(now)
    held, entries = _priority_state(view)
    if held is None or not entries:
        raise NotUnderstood("You haven't set an order yet, so there is nothing to move it in. Set the order "
                            "first, or add this as a rule.")
    if held.get("pending"):
        raise NotUnderstood("A conversation's change to the order is waiting for you in Needs you. Answer it "
                            "first, then say this again.")
    keys = [e["account"] for e in entries]
    if key not in keys:
        raise NotUnderstood(f"{label} isn't in your order yet, so it can't be moved to the front. Add it to "
                            "the order first, or add this as a rule.", [e.get("label") or e["account"] for e in entries])
    labels = [e.get("label") or e["account"] for e in entries]
    if keys[0] == key:
        raise NotUnderstood(f"{label} is already first. Nothing to change.")
    pin = {"type": "priority"}
    version = pin_value(pin, now, view)  # the same read as the entries shown
    if end is None:
        moved = [label] + [x for x in labels if x != label]
        stored = (view.get("priority_stored") or {}).get("entries") or []
        by_key = {e["account"]: e for e in stored if not (e.get("until") and (routing._parse(e["until"]) or now) <= now)}
        order = [key] + [k for k in keys if k != key]
        entries_out = []
        for k in order:
            src = by_key.get(k) or next(e for e in entries if e["account"] == k)
            row = {"account": k}
            for field in ("role", "until", "level", "exceptions"):
                if src.get(field):
                    row[field] = src[field]
            entries_out.append(row)
        argv = ["route", "priority", "set", "--from-app", "--entries=" + json.dumps(entries_out, sort_keys=True),
                "--expect=" + str(version)]
        kept = _keep_holds({"type": "priority_set", "entries": entries_out}, now)
        if kept:
            argv.append("--holds=" + json.dumps(kept, sort_keys=True))
        return _plan("priority_first", understood=f"Put {label} first", now_words=_order_words(labels),
                     after_words=_order_words(moved) + (_KEPT if kept else ""), argv=argv,
                     hold_event={"type": "priority_set", "entries": entries_out}, holds=kept,
                     pin=dict(pin, value=version),
                     receipt=f"{label} is first in your order.", account=label)
    moved = _order_words(labels)
    words = f"{label} goes first."
    argv = ["route", "priority", "amend", key, "--from-app", "--exception=" + words, "--until=" + _iso(end),
            "--expect=" + str(version)]
    kept = _keep_holds({"type": "priority_set", "entries": entries}, now)
    if kept:
        argv.append("--holds=" + json.dumps(kept, sort_keys=True))
    return _plan("priority_first_until", understood=f"Put {label} first until {when_words(end)}",
                 now_words=moved,
                 after_words=(f"{label} goes first until {when_words(end)}, then the order goes back to: {moved}"
                              + (_KEPT if kept else "")),
                 argv=argv, pin=dict(pin, value=version),
                 hold_event={"type": "priority_set", "entries": entries}, holds=kept,
                 receipt=f"{label} goes first until {when_words(end)}. Then the order goes back.",
                 account=label, ends=end)


def _pause(key: str, label: str, end_spec: str | None, now: datetime) -> dict:
    if end_spec is None:
        raise NotUnderstood(f"Every pause ends by itself, and I didn't hear when. Say it with an end, for "
                            f"example \"pause {label} until Monday\" or \"pause {label} for 2 hours\".")
    end = resolve_time(end_spec, now)
    pin = {"type": "paused", "key": key}
    view = routing.read_view(now)
    current = pin_value(pin, now, view)
    entry = next(a for a in view["accounts"] if a["key"] == key)
    was = (f"Paused until {when_words(routing._parse(entry['until']))}" if current[0] and entry.get("until")
           else "Paused until you resume it" if current[0] else _unpaused_words(entry))
    return _plan("pause", understood=f"Pause {label} until {when_words(end)}", now_words=was,
                 after_words=f"Paused until {when_words(end)}. It opens again on its own.",
                 argv=["route", "pause", key, "--from-app", "--until=" + _iso(end), _expect(current)],
                 pin=dict(pin, value=current),
                 receipt=f"Paused {label} until {when_words(end)}.", account=label, ends=end)


def _resume(key: str, label: str, end_spec: str | None, now: datetime) -> dict:
    if end_spec is not None:
        raise NotUnderstood(f"Resuming happens now, with no time. To stop {label} for a while, say \"pause "
                            f"{label} until …\".")
    pin = {"type": "paused", "key": key}
    view = routing.read_view(now)
    current = pin_value(pin, now, view)
    if not current or not current[0]:
        raise NotUnderstood(f"{label} isn't paused, so there is nothing to resume.")
    under = (routing._account_entry(view, key) or {}).get("underlying") or {}
    after = "The pause ends." + (" " + state_words(under) if under.get("state") else "")
    return _plan("resume", understood=f"Resume {label}", now_words="Paused by you",
                 after_words=after, argv=["route", "lift", key, "--from-app", _expect(current)],
                 pin=dict(pin, value=current), receipt=f"Lifted the pause on {label}.", account=label)


def _unpaused_words(entry: dict) -> str:
    """What an account that is not paused is now: "Open to agents" only when it is."""
    state, until = entry.get("state"), routing._parse(entry["until"]) if entry.get("until") else None
    if state == routing.OPEN:
        return "Open to agents"
    if state == routing.LIMITED:
        return f"Limited until {when_words(until)}" if until else "Limited"
    if state == routing.DOWN:
        return "Down"
    return "Not paused"


def state_words(entry: dict) -> str:
    """What an account is, in a sentence: "open to agents" only when it is."""
    state, until = entry.get("state"), routing._parse(entry.get("until")) if entry.get("until") else None
    if state == routing.OPEN:
        return "It is open to agents again."
    if state == routing.LIMITED:
        return f"It is still limited until {when_words(until)}." if until else "It is still limited."
    if state == routing.PAUSED:
        return f"It is still paused until {when_words(until)}." if until else "It is still paused."
    if state == routing.PROBABLY_OPEN:
        return "Its limit's time has passed, so it is probably open; Khipu hasn't read it open yet."
    if state == routing.DOWN:
        return "It is down."
    return "Khipu doesn't know yet whether it is open."


def _stop_at(key: str, label: str, main: str, end_spec: str | None, pct_match, now: datetime) -> dict:
    pct = float(pct_match.group("pct"))
    if not 1 <= pct <= 100:
        raise NotUnderstood("Where agents stop is a share from 1 to 100 percent.")
    pct_out = int(pct) if pct == int(pct) else pct
    five = re.search(r"\b(?:five|5)[\s-]*(?:hours?|hrs?|h)\b", main, re.I)
    window, span = ("five_hour", "the five hours") if five else ("weekly", "the week")
    end = resolve_time(end_spec, now) if end_spec is not None else None
    pin = {"type": "caps", "key": key, "window": window}
    current = pin_value(pin, now)
    base = [c for c in current or [] if c[1] is None] or current or []
    was = (f"Agents stop at {_pct_words(base[-1][0])}% of {span}" if base and base[-1][0] is not None
           else "No stop level set by you")
    argv = ["route", "cap", "set", key, "--from-app", "--kind=share", f"--window={window}", f"--pct={pct_out}",
            _expect(current)]
    after = f"Agents stop at {pct_out}% of {span}"
    receipt = f"Agents stop at {pct_out}% of {span} on {label}."
    if end is not None:
        argv.append("--until=" + _iso(end))
        after += f" until {when_words(end)}, then the level before applies again"
        receipt = f"Agents stop at {pct_out}% of {span} on {label} until {when_words(end)}."
    return _plan("stop_at", understood=f"Make agents stop at {pct_out}% of {span} on {label}"
                 + (f" until {when_words(end)}" if end else ""), now_words=was, after_words=after + ".",
                 argv=argv, pin=dict(pin, value=current), receipt=receipt, account=label, ends=end)


def _pct_words(value) -> str:
    return str(int(value)) if isinstance(value, (int, float)) and value == int(value) else str(value)


def _end_rule(ref: str, now: datetime) -> dict:
    view = routing.read_view(now)
    rules = [r for r in view.get("rules") or [] if isinstance(r, dict)]
    ref = ref.strip()
    number = re.fullmatch(r"(?:#|number\s+|no\.?\s*)?(\d+)", ref, re.I)
    if number:
        picked = [r for r in rules if str(r.get("n")) == number.group(1)]
        if not picked:
            raise NotUnderstood(f"You have no rule {number.group(1)}.", [f"rule {r.get('n')}" for r in rules][:8])
    else:
        query = re.sub(r"^(?:about|on|for|that says|saying|mentioning|that mentions|with|containing|"
                       r"that contains)\s+", "", ref, flags=re.I).strip(" \"'“”‘’")
        if not query:
            raise NotUnderstood("Which rule? Say its number (\"end rule 3\") or some of its words "
                                "(\"end the rule about GLM\").", [f"rule {r.get('n')}" for r in rules][:8])
        needle = routing_directions._norm_entity(query)
        picked = [r for r in rules if needle and needle in routing_directions._norm_entity(r.get("text"))]
        if not picked:
            raise NotUnderstood(f"No rule mentions \"{query}\".", [f"rule {r.get('n')}" for r in rules][:8])
        if len(picked) > 1:
            raise NotUnderstood(f"More than one rule mentions \"{query}\": "
                                + "; ".join(f"rule {r.get('n')}" for r in picked) + ". Say which number.",
                                [f"rule {r.get('n')}: {_clip(r.get('text'), 60)}" for r in picked])
    rule = picked[0]
    if rule.get("pending"):
        raise NotUnderstood(f"Rule {rule.get('n')} is a conversation's proposal waiting for you in Needs you. "
                            "Answer it there.")
    pin = {"type": "rule", "n": rule["n"]}
    value = rule.get("revision")
    return _plan("end_rule", understood=f"End rule {rule['n']}", now_words=f"Rule {rule['n']}: {_clip(rule.get('text'))}",
                 after_words=f"Rule {rule['n']} ends. Undo brings it back.",
                 argv=["route", "rule", "retire", str(rule["n"]), "--from-app", f"--expect={value}"],
                 pin=dict(pin, value=value), receipt=f"Ended rule {rule['n']}: {_clip(rule.get('text'), 100)}")


def _add_rule(text: str, now: datetime) -> dict:
    view = routing.read_view(now)
    rules = [r for r in view.get("rules") or [] if isinstance(r, dict)]
    if text.startswith("-"):
        raise NotUnderstood("Start a rule with a word, not a dash.")
    pin = {"type": "rules"}
    value = pin_value(pin, now, view)
    count = len(rules)
    argv = ["route", "rule", "add", text, "--from-app", _expect(value)]
    kept = _keep_holds({"type": "rule_add", "text": text}, now)
    if kept:
        argv.append("--holds=" + json.dumps(kept, sort_keys=True))
    return _plan("add_rule", understood=FALLBACK_LABEL, now_words=_count_words(count),
                 after_words=f"{_count_words(count + 1)}, including: {_clip(text)}" + (_KEPT if kept else ""),
                 argv=argv, pin=dict(pin, value=value),
                 hold_event={"type": "rule_add", "text": text}, holds=kept,
                 receipt=f"Added the rule: {_clip(text, 100)}", fallback=True)


def _count_words(n: int) -> str:
    return "No rules" if n == 0 else "1 rule" if n == 1 else f"{n} rules"


def interpret(words: str, now: datetime | None = None) -> dict:
    """The one change ``words`` name (a plan), or ``NotUnderstood``.
    Deterministic, and it only reads the routing record."""
    now = now or routing._now()
    original = " ".join((words or "").split())
    if not original:
        raise NotUnderstood("Say what to change, for example \"put Codex first until Friday\".")
    if len(original) > WORDS_MOST or any(ord(c) < 32 for c in (words or "") if c not in "\n\t"):
        raise NotUnderstood(f"Keep it to a sentence or two (at most {WORDS_MOST} characters).")
    text = _clean(original)
    if not text:
        raise NotUnderstood("Say what to change, for example \"put Codex first until Friday\".")
    rule_end = _RULE_END.match(text)
    if rule_end:
        return _end_rule(rule_end.group("ref"), now)
    main, end_spec = _split_end(text)
    pct = _PERCENT.search(main)
    if pct and _STOP_WORD.search(main):
        target = _target_of(main, "limit", strict=False)
        return _stop_at(*target, main, end_spec, pct, now) if target else _add_rule(original, now)
    resume = _RESUME.match(main) or _RESUME_AGAIN.match(main)
    if resume:
        target = _target_of(main[resume.end():] if _RESUME.match(main) else main, "resume")
        return _resume(*target, end_spec, now) if target else _add_rule(original, now)
    pause = _PAUSE.match(main)
    if pause:
        target = _target_of(main[pause.end():], "pause")
        return _pause(*target, end_spec, now) if target else _add_rule(original, now)
    first = _FIRST_MOVE.fullmatch(main)
    if first:
        target = _target_of(first.group("acct"), "put first")
        if target:
            return _priority_first(*target, end_spec, now)
    return _add_rule(original, now)


# ---- tokens -----------------------------------------------------------------------------------


def _token_path():
    return owner_turns.folder() / TOKEN_FILE


@contextlib.contextmanager
def _token_lock():
    owner_turns._ensure_folder()
    fd = os.open(_token_path().with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_tokens() -> dict:
    try:
        data = json.loads(_token_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = data.get("tokens") if isinstance(data, dict) else None
    return rows if isinstance(rows, dict) else {}


def _write_tokens(rows: dict) -> None:
    owner_turns._ensure_folder()
    owner_turns._write_private(_token_path(), json.dumps({"tokens": rows}, indent=1, sort_keys=True) + "\n")


def issue(plan: dict, clock: float | None = None) -> tuple[str, float]:
    clock = time.time() if clock is None else clock
    token = secrets.token_hex(16)  # letters and digits only: never read as a flag
    with _token_lock():
        rows = {k: v for k, v in _read_tokens().items() if isinstance(v, dict) and v.get("expires", 0) > clock}
        rows[token] = {"expires": clock + TOKEN_TTL_S, "used": False, "plan": plan}
        _write_tokens(rows)
    return token, clock + TOKEN_TTL_S


def take(token: str, clock: float | None = None) -> dict:
    """The plan a token names, marking the token used. ``Refusal`` for one
    that is unknown, expired or already used (nothing is changed)."""
    clock = time.time() if clock is None else clock
    with _token_lock():
        rows = _read_tokens()
        row = rows.get(token) if isinstance(token, str) else None
        if not isinstance(row, dict) or row.get("expires", 0) <= clock or not isinstance(row.get("plan"), dict):
            raise Refusal("no_such_preview", "That preview has expired, or Khipu didn't make it. Preview the "
                          "change again.")
        if row.get("used"):
            raise Refusal("already_used", "That change was already saved. Preview it again to make it again.")
        row["used"] = True
        live = {k: v for k, v in rows.items() if isinstance(v, dict) and v.get("expires", 0) > clock}
        _write_tokens(live)
    return row["plan"]


class Refusal(Exception):
    def __init__(self, code: str, error: str, **extra):
        super().__init__(error)
        self.code, self.error, self.extra = code, error, extra

    def body(self) -> dict:
        return {"ok": False, "code": self.code, "error": self.error, **self.extra}


# ---- the two modes ----------------------------------------------------------------------------


def _from_app(args) -> None:
    if not getattr(args, "from_app", False) or not routing._caller()["app"]:
        raise Refusal("owner_only", routing.terminal_refusal())


def preview(words: str, now: datetime | None = None) -> dict:
    try:
        plan = interpret(words, now)
    except NotUnderstood as err:
        raise Refusal("not_understood", err.reason, reason=err.reason, considered=err.considered) from None
    token, expires = issue(plan)
    return {"ok": True, "understood": plan["understood"], "kind": plan["kind"], "fallback": plan["fallback"],
            "account": plan["account"], "now": plan["now"], "after": plan["after"], "ends": plan["ends"],
            "ends_words": plan["ends_words"], "token": token,
            "expires_at": _iso(datetime.fromtimestamp(expires, timezone.utc)),
            "apply": {"command": "route say", "args": ["--from-app", "--apply", token]}}


def _this_mac() -> str:
    try:
        names = routing.settings().get("routing_mac_names") or {}
        name = names.get(routing._local_mac()) if isinstance(names, dict) else None
        return name.strip() if isinstance(name, str) and name.strip() else "this Mac"
    except Exception:  # noqa: BLE001 - a name is a nicety; the fallback is true
        return "this Mac"


def apply(token: str, now: datetime | None = None) -> dict:
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from khipu import cli, owner_act

    now = now or routing._now()
    plan = take(token)
    pin = plan["pin"]
    if json.loads(json.dumps(pin_value(pin, now))) != pin.get("value"):
        raise Refusal("changed", CHANGED)
    if plan.get("hold_event") is not None:
        # A pause or hold that appeared on these accounts since the preview was not shown.
        if set(_keep_holds(plan["hold_event"], now) or {}) - set(plan.get("holds") or {}):
            raise Refusal("changed", CHANGED)
    argv = plan["argv"]
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            ns = cli.build_parser().parse_args(argv)
        except SystemExit:
            raise Refusal("bad_args", "Those arguments don't fit that change.") from None
        if not getattr(ns, "from_app", False) or getattr(ns, "by_direction", False) or ns.func is not cli.cmd_route:
            raise Refusal("bad_args", "Those arguments don't fit that change.")
        rc = int(ns.func(ns) or 0)
    result = owner_act._parsed(out.getvalue())
    if rc != 0 or result.get("ok") is False:
        code = result.get("code") or result.get("reason") or "refused"
        error = str(result.get("error") or "")
        if code in ("version_moved", "crossed", "entry_moved") or error.startswith(
                ("say end or keep in holds", "the rule changed after it was read")):
            raise Refusal("changed", CHANGED)
        raise Refusal(code, result.get("error") or err.getvalue().strip() or "The change was refused.")
    action_id = result.get("owner_action")
    receipt = plan["receipt"]
    if plan["kind"] == "resume":
        # What the account is once the pause is gone, read from the record.
        entry = routing._account_entry(routing.read_view(now),result.get("account") or pin.get("key")) or {}
        receipt = f"{receipt} {state_words(entry)}"
    return {"ok": True, "applied": True, "kind": plan["kind"], "action_id": action_id,
            "receipt": {"text": receipt, "action_id": action_id, "undoable": bool(action_id),
                        "saved_on": [_this_mac()], "not_reached": []}}


def run_cli(args) -> int:
    from khipu import owner_direction

    words, token = getattr(args, "preview", None), getattr(args, "apply", None)
    try:
        _from_app(args)
        if (words is None) == (token is None):
            raise Refusal("bad_args", "Give the words to preview (--preview) or the token to apply (--apply), one of the two.")
        out = preview(words) if words is not None else apply(token)
    except Refusal as err:
        routing._print_json(err.body())
        return 2
    except routing.RouteError as err:
        routing._print_json({"ok": False, "error": str(err), "code": getattr(err, "code", None) or "refused"})
        return 2
    except owner_direction.DirectionRefused as err:
        routing._print_json(err.refusal())
        return 2
    routing._print_json(out)
    return 0
