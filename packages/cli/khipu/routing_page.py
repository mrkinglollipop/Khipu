"""The routing page (routing slice 3): one short page, rendered from the folded
view and nothing else.

``page(view, now, conversation=None)`` is pure: no file is read, nothing is
folded, no clock is asked. It prints what the record says, account by account
in the owner's order, then the owner's rules. Scope:
jobs/2026-11-07/scope-routing-one-page.md section 2.

Nothing here decides anything. A status word is the record's own facts put in
one word (a hold, a limit, a cap verdict, under 10% left); advice and the
words of the rules are other slices.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

LIMIT = 4500  # characters, the whole page
NEARLY_OUT_PCT = 10.0
# Role and exception widths tried, widest first, before any rule line is dropped.
_WIDTHS = ((220, 140), (140, 100), (90, 60), (0, 0))
# A rule that opens with one of these names (``WHICH MODEL: Opus: ...``) goes
# under that heading; every other rule goes under RULES.
SECTIONS = ("WHICH MODEL", "NEVER", "CHECKS", "MACS")
_HEAD = re.compile(r"^\s*(WHICH MODEL|NEVER|CHECKS|MACS)\s*:\s*", re.IGNORECASE)
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
_WINDOW_WORDS = {"five_hour": "of its 5 h window", "monthly": "of the month", "daily": "of the day"}
_WINDOW_NAMES = {"five_hour": "5 h window", "weekly": "week", "monthly": "month", "daily": "day"}


def _pct(value) -> str:
    return f"{round(float(value), 1):g}"


def _one_line(text) -> str:
    return " ".join(str(text or "").split())


def _cut(text: str, width: int) -> str:
    """The text cut at a word to at most ``width`` characters, with an
    ellipsis when cut; nothing for a width of 0."""
    text = _one_line(text)
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    cut = text[: max(1, width - 1)]
    cut = cut[: cut.rindex(" ")] if " " in cut else cut
    return cut.rstrip(" ,;:.-(") + "…"


def _sentence(text: str) -> str:
    text = text.strip()
    return text if not text or text[-1] in ".!?”\"…" else text + "."


def _num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _term_day(raw, now: datetime, r) -> str:
    """An ``ends`` or ``plan_change.on`` as a short date: a bare date is its
    own day, a time is a clock reading."""
    text = str(raw or "")
    if _DATE_ONLY.fullmatch(text):
        return datetime.strptime(text, "%Y-%m-%d").strftime("%b %d").replace(" 0", " ")
    dt = r._parse(text)
    return r._clock(dt, now) if dt else text


def _ended(entry: dict, now: datetime) -> bool:
    from khipu import routing_entries

    ends = (entry.get("terms") or {}).get("ends")
    at = routing_entries.term_time(ends) if ends else None
    return at is not None and at <= now


def _windows(entry: dict, now: datetime, r) -> list[dict]:
    """The pace windows with a reading and a reset still to come, longest
    first: (name, left, used, resets, pace object)."""
    from khipu import routing_pace

    out = []
    for pace in entry.get("pace") or []:
        used, resets = pace.get("used_pct"), r._parse(pace.get("cycle_resets_at"))
        if not _num(used) or resets is None or resets <= now or pace.get("not_reported"):
            continue
        name = pace.get("window")
        length = pace.get("window_s") if _num(pace.get("window_s")) else routing_pace.window_length(name, resets)
        out.append({"name": name, "left": max(0.0, 100.0 - float(used)), "used": float(used), "resets": resets,
                    "length": length or 0, "pace": pace})
    return sorted(out, key=lambda w: -w["length"])


def _job_target(window: dict | None) -> dict | None:
    """The even-pace figures a job is held to, when the account has them."""
    if window is None:
        return None
    target = window["pace"].get("pace_target")
    return target if isinstance(target, dict) and _num(target.get("allowance_pct")) else None


def _held_back(entry: dict, window: dict | None) -> bool:
    """A job-purpose cap's verdict says to wait: ahead of even pace, or at
    the ceiling."""
    if window is None:
        return False
    pace = window["pace"]
    for cap in entry.get("caps") or []:
        if cap.get("purpose", "job") != "job" or cap.get("window") != window["name"]:
            continue
        if cap.get("kind") == "pace_target" and (pace.get("pace_target") or {}).get("verdict") == "ahead":
            return True
        if cap.get("kind") == "share" and (pace.get("share_cap") or {}).get("verdict") == "cap_reached":
            return True
    return False


def _cap_words(cap: dict, now: datetime, r, measured: dict | None) -> str:
    kind, purpose = cap.get("kind"), cap.get("purpose", "job")
    if kind == "share" and _num(cap.get("pct")):
        text = f"ceiling {_pct(cap['pct'])}%"
        if cap.get("window") not in (None, "weekly"):
            text += f" of the {_WINDOW_NAMES.get(cap['window'], cap['window'])}"
    elif kind == "pace_target":
        text = "even pace" + (f" +{_pct(cap['slack'])}" if _num(cap.get("slack")) and cap["slack"] else "")
        if _num(cap.get("pct")) and cap["pct"] != 100:
            text += f", at most {_pct(cap['pct'])}%"
    elif kind == "per_day" and _num(cap.get("pct")):
        text = f"{_pct(cap['pct'])}% a day"
    elif kind == "spend" and _num(cap.get("usd")) and _num(cap.get("days")):
        text = f"spend cap ${cap['usd']:g} in {cap['days']:g} d"
        if measured and _num(measured.get("usd")):
            text += f" (${measured['usd']:g} spent)"
    elif kind == "concurrency" and _num(cap.get("n")):
        text = "one at a time" if cap["n"] == 1 else f"{cap['n']:g} at a time"
        if measured and _num(measured.get("jobs")):
            text += f" ({measured['jobs']:g} running)"
    else:
        text = str(kind or "cap")
    if purpose == "check":
        text += " for checks"
    if cap.get("sensitive_only"):
        text += " (sensitive work only)"
    if cap.get("tier"):
        text += f" ({cap['tier']} tier)"
    until = r._parse(cap.get("until"))
    if until is not None:
        text += f", until {r._clock(until, now)}"
    return text


def _override_words(row: dict, now: datetime, r) -> str:
    ceiling = row.get("ceiling_pct")
    if _num(ceiling):
        window = _WINDOW_NAMES.get("five_hour" if row.get("window") == "five-hour" else row.get("window") or "weekly",
                                   "week")
        words = f"may run to {_pct(ceiling)}% of its {window}"
    else:
        words = "may run ahead of pace"
    until = r._parse(row.get("until"))
    if until is not None:
        return f"{words} until {r._clock(until, now)}"
    review = r._parse(row.get("review_at"))
    return f"{words}, no end" + (f" (review {r._clock(review, now)})" if review else "")


def _hold_words(hold: dict, width: int, now: datetime, r) -> str:
    until = r._parse(hold.get("until"))
    text = f"until {r._clock(until, now)}" if until else "until lifted"
    said = hold.get("text")
    if said and width > 0:
        said = _cut(said if hold.get("owner") else r.their_words(said), width)
        text += f": “{said}”"
    return text


def _status(entry: dict, provider: dict, windows: list[dict], holds: list[dict], last: bool, widths,
            now: datetime, r) -> str:
    state = entry.get("state")
    until = r._parse(entry.get("until"))
    window = windows[0] if windows else None
    caps = entry.get("caps") or []
    if state == r.PAUSED:
        return "PAUSED " + (f"until {r._clock(until, now)}" if until else "until lifted")
    if holds:
        return "NO JOBS " + _hold_words(holds[0], widths[1], now, r)
    if state == r.LIMITED:
        text = "LIMIT REACHED" + (f", back {'about ' if entry.get('basis') == 'estimated' else ''}"
                                  f"{r._clock(until, now)}" if until else "")
        return text
    if state == r.DOWN:
        return "DOWN: " + _one_line(entry.get("reason") or "out of service")
    if state not in (r.OPEN, r.PROBABLY_OPEN):
        return "UNKNOWN"
    if window is not None and window["left"] < NEARLY_OUT_PCT:
        return "NEARLY OUT"
    if provider.get("use") == "second_opinion":  # the provider fact advice reads too
        return "SECOND OPINIONS ONLY"
    if last and any(c.get("kind") == "spend" for c in caps):
        return "LAST RESORT"
    if _held_back(entry, window):
        return "HOLD BACK"
    return "PROBABLY OPEN" if state == r.PROBABLY_OPEN else "OK"


def _account_text(label: str, entry: dict, item: dict | None, *, number, holds: list[dict], last: bool,
                  providers: dict, override: dict | None, readings: dict, widths, now: datetime, r) -> str:
    windows = _windows(entry, now, r)
    window = windows[0] if windows else None
    provider = providers.get(entry.get("harness")) or providers.get(entry["key"].partition(":")[0]) or {}
    status = _status(entry, provider, windows, holds, last, widths, now, r)
    parts = []
    if window is not None:
        text = f"{_pct(window['left'])}% {_WINDOW_WORDS.get(window['name'], '')} left".replace("%  left", "% left")
        text += f", resets {r._clock(window['resets'], now)}"
        target = _job_target(window)
        if target is not None:
            text += f"; used {_pct(window['used'])}%, even pace {_pct(target['allowance_pct'])}%"
        parts.append(text)
        for other in windows[1:]:
            if other["left"] < NEARLY_OUT_PCT:
                parts.append(f"{_WINDOW_NAMES.get(other['name'], other['name'])} {_pct(other['left'])}% left, "
                             f"resets {r._clock(other['resets'], now)}")
    elif entry.get("state") in (r.OPEN, r.PROBABLY_OPEN, r.UNKNOWN):
        parts.append("No reading of its limit yet")
    if (entry.get("state") == r.LIMITED):
        for m in entry.get("models") or []:
            m_until = r._parse(m.get("until"))
            if m_until is not None:
                parts.append(f"{r.model_words(m, push=True)} limit reached, back {r._clock(m_until, now)}")
    caps = [_cap_words(c, now, r, (readings.get(entry["key"]) or {}).get(c.get("kind")))
            for c in entry.get("caps") or []]
    if caps:
        parts.append("Caps: " + "; ".join(caps))
    terms = entry.get("terms") or {}
    if _num(terms.get("ceiling_with_ok")):
        parts.append(f"With the owner's okay, up to {_pct(terms['ceiling_with_ok'])}%")
    if override is not None:
        parts.append("Override for this conversation: " + _override_words(override, now, r))
    allowed = provider.get("models_allowed")
    if allowed:
        parts.append(", ".join(str(m) for m in allowed) + " only")
    change = terms.get("plan_change")
    if isinstance(change, dict) and change.get("to"):
        parts.append(f"Plan changes to {change['to']}" +
                     (f" on {_term_day(change['on'], now, r)}" if change.get("on") else ", date not set"))
    if terms.get("ends"):
        parts.append(f"Ends {_term_day(terms['ends'], now, r)}")
    live = [x for x in (item or {}).get("exceptions") or [] if (r._parse(x.get("until")) or now) > now]
    if widths[1] > 0:
        for x in live:
            ends = r._parse(x.get("until"))
            parts.append("Exception" + (f" until {r._clock(ends, now)}" if ends else "") +
                         f": “{_cut(x.get('text'), widths[1])}”")
    role = _cut((item or {}).get("role"), widths[0])
    if role:
        parts.append(role)
    lead = f"{number}. " if number else "- "
    return f"{lead}{label}: {_sentence(status)} " + " ".join(_sentence(p) for p in parts)


def _rules(view: dict) -> list[tuple[str, str]]:
    """The owner's confirmed rules as (heading, text), in the page's order:
    the named headings first, every other rule last under RULES. A rule
    awaiting the owner's yes is not shown."""
    found: dict[str, list[str]] = {name: [] for name in (*SECTIONS, "RULES")}
    for rule in view.get("rules") or []:
        if rule.get("pending") or rule.get("confirmed") is False:
            continue
        text = _one_line(rule.get("text"))
        head = _HEAD.match(text)
        found[head.group(1).upper() if head else "RULES"].append(text[head.end():] if head else text)
    return [(name, text) for name, texts in found.items() for text in texts]


def _render(view: dict, now: datetime, conversation: dict, readings: dict, rules: list, keep: int, widths,
            account_width: int, r) -> str:
    providers = (view.get("definitions") or {}).get("providers") or {}
    held = (view.get("priority") or {}).get("entries") or []
    by_key = {a["key"]: a for a in view.get("accounts") or []}
    ordered, seen = [], set()
    for item in held:
        entry = by_key.get(item.get("account"))
        if entry is not None and entry["key"] not in seen:
            seen.add(entry["key"])
            ordered.append((entry, item))
    extra = [(a, None) for a in view.get("accounts") or []
             if a["key"] not in seen and (a.get("note") or [None])[0] != "untracked"]
    ordered = [(e, i) for e, i in ordered if not _ended(e, now)]
    extra = [(e, i) for e, i in extra if not _ended(e, now)]
    holds = [h for h in view.get("holds") or []]
    lines = [f"ROUTING {r._clock(now, now)}. The owner's record. Guidance, not a gate: route differently if you "
             "know better, and say why.", ""]
    lines.append("ACCOUNTS, in the owner's order. Take the first with room that fits the work.")
    overridden = []

    def line(entry, item, number, last):
        label = r._account_label(entry["label"], entry.get("key"), view=view)
        mine = [h for h in holds if (h.get("scope") or {}).get("kind") == "account"
                and (h.get("scope") or {}).get("account") == entry["key"]]
        override = conversation.get(entry["key"])
        if override is not None:
            overridden.append(label)
        text = _account_text(label, entry, item, number=number, holds=mine, last=last, providers=providers,
                             override=override, readings=readings, widths=widths, now=now, r=r)
        return _cut(text, account_width) if account_width else text

    for n, (entry, item) in enumerate(ordered, 1):
        lines.append(line(entry, item, n, n == len(ordered) and not extra))
    if extra:
        lines.append("Not in the owner's order:")
        lines += [line(entry, item, None, False) for entry, item in extra]
    for hold in holds:
        if (hold.get("scope") or {}).get("kind") != "account":
            lines.append(f"Hold on {hold.get('scope_label') or 'everything'}: " + _hold_words(hold, widths[1], now, r)
                         + ".")
    resets = [f"{r._account_label(e['label'], e.get('key'), view=view)} {e['terms']['resets_held']}"
              for e, _i in (*ordered, *extra) if (e.get("terms") or {}).get("resets_held")]
    if resets:
        lines.append("Resets held: " + ", ".join(resets) + ".")
    if overridden:
        lines.append(f"This conversation: {', '.join(overridden)} may run ahead of pace under an override "
                     "(on its line above).")
    shown, last_head = rules[:keep], None
    for name, text in shown:
        if name != last_head:
            lines += ["", name]
            last_head = name
        lines.append(text)
    if keep < len(rules):
        lines += ["", f"{len(rules) - keep} more rule lines: run `khipu route`."]
    return "\n".join(lines)


def page(view: dict, now: datetime, conversation: dict | None = None, readings: dict | None = None) -> str:
    """The routing page, whole, at most ``LIMIT`` characters.

    ``conversation``: the override rows that apply to the asking conversation,
    by account key (``conversation_overrides.matches`` gives them); absent,
    no override line prints. ``readings``: measured spend and running jobs by
    account key and cap kind, for a caller that already read them; absent, a
    cap prints as set. Both are inputs because this function reads nothing.

    To fit, the roles and exceptions are shortened in steps, then whole rule
    lines are dropped from the bottom with a count; accounts, status words
    and caps are never dropped."""
    from khipu import routing as r

    now = now.astimezone(timezone.utc)
    if not view.get("record"):
        return "ROUTING: no routing record on this Mac."
    conversation, readings = conversation or {}, readings or {}
    rules = _rules(view)
    text = ""
    # Roles and exceptions shorten first (all rules kept), then whole rule
    # lines go from the bottom, then roles and exceptions go, then the
    # account lines themselves tighten.
    tries = ([(len(rules), w) for w in _WIDTHS[:-1]]
             + [(k, _WIDTHS[-2]) for k in range(len(rules) - 1, -1, -1)] + [(0, _WIDTHS[-1])])
    for account_width in (0, 400, 280, 200, 140):
        for keep, widths in tries:
            text = r._for_agents(_render(view, now, conversation, readings, rules, keep, widths, account_width, r),
                                 view)
            if len(text) <= LIMIT:
                return text
    # Last resort: a page never leaves the limit.
    return text[: LIMIT - 1].rstrip() + "…"
