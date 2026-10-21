"""Advisory checks for rule 12, contradictions and rule hygiene.

These marks are guidance. They never deny a dispatch, never edit the owner's
words, and never become a gate. Brief text is classified on this Mac only.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

TASK_CLASSES = (
    "scope", "mock", "ruling", "hard-security", "failed-fix", "build",
    "fix-round", "tests", "docs", "synthesis", "mechanical",
)
TIER_NAMES = frozenset({"opus", "sonnet", "haiku"})
# The fix-round / Opus line is the case's exact sentence. Other mismatches
# use the same shape so a consumer can read the tier names without a parser.
FIX_ROUND_OPUS = "Rule 12: fix rounds go to Sonnet; this dispatch is Opus. Say why if intended."
_PENDING_AFTER = 24 * 3600
_TASK_LINE = re.compile(r"(?im)^\s*task:\s*([a-z0-9-]+)\s*$")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# A date, weekday or reset sets a deadline only after one of these words; a
# date anywhere else (a provenance note such as "(Matt 2026-10-12)") and the
# bare word "until" followed by anything else are not deadlines.
_PREP = r"(?:until|through|by|expires(?:\s+on)?)"
_DATE_DEADLINE = re.compile(rf"\b{_PREP}\s+(?:on\s+)?(\d{{4}}-\d{{2}}-\d{{2}})\b", re.IGNORECASE)
_WEEKDAY_DEADLINE = re.compile(
    rf"\b{_PREP}\s+(?:on\s+)?({'|'.join(_WEEKDAYS)})\b", re.IGNORECASE)
_WEEKDAY_ON_END = re.compile(
    rf"\bon\s+(?:{'|'.join(_WEEKDAYS)})\b(?=\s*(?:[.!?;]|$))", re.IGNORECASE)
_THIS_WEEK = re.compile(r"\bthis week\b", re.IGNORECASE)
_STOP = frozenset(
    "a an the use for to of and or on in its this week until may handles handle "
    "with from by at be is are that it their your into".split()
)
# Longer phrases first so "fix round" is not lost to a shorter class word.
_GUESSES = (
    ("fix-round", ("fix-round", "fix round")),
    ("hard-security", ("hard-security", "hard security")),
    ("failed-fix", ("failed-fix", "failed fix")),
    ("mechanical", ("mechanical",)),
    ("synthesis", ("synthesis",)),
    ("ruling", ("ruling",)),
    ("scope", ("scope",)),
    ("mock", ("mock",)),
    ("docs", ("docs", "documentation")),
    ("tests", ("tests",)),
    ("build", ("build",)),
)
_PHRASE = {
    "fix-round": "fix rounds", "scope": "scope", "mock": "mocks", "ruling": "rulings",
    "hard-security": "hard security", "failed-fix": "failed fixes", "build": "builds",
    "tests": "tests", "docs": "docs", "synthesis": "synthesis", "mechanical": "mechanical work",
}
_SETTINGS_MOST = 65536


def valid_tiers(tiers) -> bool:
    """A confirmed table names every task class and only opus, sonnet or haiku."""
    if not isinstance(tiers, dict) or set(tiers) != set(TASK_CLASSES):
        return False
    return all(isinstance(value, str) and value in TIER_NAMES for value in tiers.values())


def fold_facts(events: list[dict]) -> tuple[dict | None, list[dict]]:
    """The adopted tier table and the live structured directions.

    Only an owner-confirmed table with every class replaces the previous
    one. A conversation write, an unconfirmed write, or an unknown tier
    leaves the adopted table where it was.
    """
    table = None
    directions: dict[str, dict] = {}
    for event in events:
        if event.get("type") == "tier_table_clear" and _owner_written(event):
            table = None
        elif event.get("type") == "tier_table_set" and _authority(event):
            table = {"tiers": dict(event["tiers"]), "confirmed": True}
        elif event.get("type") == "direction_set" and _direction(event):
            directions[event["id"]] = {
                "id": event["id"], "account": event["account"],
                "topic": event["topic"], "value": event["value"],
            }
    return table, list(directions.values())


def _owner_written(event: dict) -> bool:
    author = event.get("written_by") if isinstance(event.get("written_by"), dict) else {}
    return author.get("kind") == "owner"


def _authority(event: dict) -> bool:
    return (event.get("confirmed") is True and _owner_written(event)
            and valid_tiers(event.get("tiers")))


def _direction(event: dict) -> bool:
    author = event.get("written_by") if isinstance(event.get("written_by"), dict) else {}
    return (author.get("kind") == "owner" and isinstance(event.get("id"), str) and event["id"]
            and isinstance(event.get("account"), str) and event["account"]
            and isinstance(event.get("topic"), str) and event["topic"]
            and isinstance(event.get("value"), str))


def attach(view: dict, now: datetime) -> dict:
    """Add contradiction and hygiene flags for ``now``. Reads no event log.

    Flags are derived on each read so a cached fold still expires a deadline
    and a pending edit when the clock passes them. Nothing here is written.
    """
    if not isinstance(view, dict):
        return view
    view["contradictions"] = _contradictions(view)
    view["rule_hygiene"] = _hygiene(view, now)
    return view


def _contradictions(view: dict) -> list[dict]:
    by_key, by_label = _accounts(view)
    held = _held_keys(view)
    flags = []
    priority = view.get("priority") if isinstance(view.get("priority"), dict) else None
    if priority:
        event_id = priority.get("event_id")
        for entry in priority.get("entries") or []:
            record = _record_for_key(entry.get("account"), entry.get("label"), by_key)
            state = _priority_state(record, entry, held)
            if state is None or not record or not record.get("id") or not event_id:
                continue
            flags.append(_flag(
                "account_state", record["id"], [event_id],
                f"The priority names {record.get('label') or record['id']}, which is {state}.",
                state=state, source="priority"))
    for rule in view.get("rules") or []:
        if not isinstance(rule, dict) or not isinstance(rule.get("text"), str):
            continue
        origin = rule.get("origin_id") or rule.get("entity")
        for record in _named(rule["text"], by_key, by_label):
            state = _rule_state(record, held)
            if state is None or not origin:
                continue
            flags.append(_flag(
                "account_state", record["id"], [origin],
                f"A standing rule names {record.get('label') or record['id']}, which is {state}.",
                state=state, source="rule"))
    flags += _opposite(view.get("directions") or [], by_key)
    return flags


def _opposite(directions: list, by_key: dict) -> list[dict]:
    groups: dict[tuple, list] = {}
    for row in directions:
        if not isinstance(row, dict):
            continue
        # The fold forwards a stable id to the login key. The flag publishes
        # the stable id the direction was written with.
        account = _published_account(row.get("account"), by_key)
        groups.setdefault((account, row.get("topic")), []).append(row)
    flags = []
    for (account, topic), rows in groups.items():
        values = {row.get("value") for row in rows}
        if len(values) < 2 or not account:
            continue
        flags.append({
            "kind": "opposite_directions", "account": account,
            "entities": [row["id"] for row in rows if row.get("id")],
            "text": f"Structured directions for {account} on {topic} disagree.",
            "advisory": True,
        })
    return flags


def _published_account(account, by_key: dict) -> str | None:
    if not isinstance(account, str) or not account:
        return None
    record = by_key.get(account)
    if record and record.get("id"):
        return record["id"]
    return account


def _hygiene(view: dict, now: datetime) -> list[dict]:
    rules = [rule for rule in (view.get("rules") or []) if isinstance(rule, dict)]
    by_key, by_label = _accounts(view)
    resets = _named_resets(rules)
    flags = []
    for rule in rules:
        text = rule.get("text") if isinstance(rule.get("text"), str) else ""
        named = _named(text, by_key, by_label)
        record = named[0] if named else None
        scoped = resets.get(record.get("key")) if record else None
        if text and record and record.get("id") and _deadline_worded(text, scoped):
            flags.append(_flag(
                "move_to_role", record["id"], [rule.get("entity")],
                "Move this into the account's priority role with an until."))
        written = _parse(rule.get("at"))
        if text and record and _expired(text, written, now, scoped):
            flags.append(_flag(
                "expired", record.get("id"), [rule.get("entity")],
                "This standing rule's date or named reset has passed."))
    flags += _overlaps(rules, by_key, by_label)
    flags += _restatements(view, rules, by_key)
    flags += _pending(rules, now)
    return [flag for flag in flags if flag.get("entities") and all(flag["entities"])]


def _overlaps(rules: list[dict], by_key: dict, by_label: dict) -> list[dict]:
    grouped: dict[str, list] = {}
    tokens: dict[str, set] = {}
    labels = [record["label"] for record in by_key.values() if record.get("label")]
    for rule in rules:
        text = rule.get("text") if isinstance(rule.get("text"), str) else ""
        tokens[rule.get("entity")] = _tokens(text, labels)
        for record in _named(text, by_key, by_label):
            if record.get("id"):
                grouped.setdefault(record["id"], []).append(rule)
    flags = []
    for account, group in grouped.items():
        for index, left in enumerate(group):
            for right in group[index + 1:]:
                a, b = tokens.get(left.get("entity")) or set(), tokens.get(right.get("entity")) or set()
                if not a or not b or len(a & b) / len(a | b) < 0.5:
                    continue
                flags.append(_flag(
                    "overlap", account, [left.get("entity"), right.get("entity")],
                    "Two standing rules name the same account and topic."))
    return flags


def _restatements(view: dict, rules: list[dict], by_key: dict) -> list[dict]:
    priority = view.get("priority") if isinstance(view.get("priority"), dict) else None
    if not priority or not priority.get("event_id"):
        return []
    flags = []
    for entry in priority.get("entries") or []:
        role = entry.get("role")
        if not isinstance(role, str) or not role.strip():
            continue
        record = _record_for_key(entry.get("account"), entry.get("label"), by_key)
        if not record or not record.get("id"):
            continue
        for rule in rules:
            if isinstance(rule.get("text"), str) and rule["text"].strip() == role.strip():
                flags.append(_flag(
                    "role_restates_rule", record["id"],
                    [rule.get("entity"), priority["event_id"]],
                    "This priority role restates a standing rule."))
    return flags


def _pending(rules: list[dict], now: datetime) -> list[dict]:
    flags = []
    for rule in rules:
        if not rule.get("pending"):
            continue
        at = _parse(rule.get("at"))
        if at is None or (now - at).total_seconds() <= _PENDING_AFTER:
            continue
        number = rule.get("n")
        flags.append({
            "kind": "long_pending", "entities": [rule.get("entity")],
            "text": "This edit has been waiting on the owner for more than 24 hours.",
            "advisory": True,
            "owner_direction": {
                "confirm": f"Confirm rule {number}",
                "drop": f"Undo the pending edit of rule {number}",
            },
        })
    return flags


def _reset_deadline(text: str, phrase: str) -> bool:
    """A named reset sets a deadline after until/through/by/expires."""
    return bool(re.search(
        rf"\b{_PREP}\b[^.;]*?\b{re.escape(phrase)}\b", text, re.IGNORECASE))


def _deadline_worded(text: str, scoped: list | None) -> bool:
    """Time wording that belongs in a role: a deadline date, weekday or
    named reset, "this week", or a weekday the rule is worded for ("on
    Friday" at the end of its clause)."""
    if _DATE_DEADLINE.search(text) or _WEEKDAY_DEADLINE.search(text) or _THIS_WEEK.search(text):
        return True
    if _WEEKDAY_ON_END.search(text):
        return True
    return any(_reset_deadline(text, phrase) for phrase, _at in scoped or [])


def _expired(text: str, written: datetime | None, now: datetime, scoped: list | None) -> bool:
    for match in _DATE_DEADLINE.finditer(text):
        if now >= _day_end(match.group(1)):
            return True
    match = _WEEKDAY_DEADLINE.search(text)
    if match is not None and written is not None:
        index = _WEEKDAYS.index(match.group(1).casefold())
        ahead = (index - written.weekday()) % 7
        day = written.date() + timedelta(days=ahead)
        end = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1)
        if now >= end:
            return True
    return any(at is not None and now >= at and _reset_deadline(text, phrase)
               for phrase, at in scoped or [])


def _day_end(iso_day: str) -> datetime:
    year, month, day = (int(part) for part in iso_day.split("-"))
    return datetime(year, month, day, tzinfo=timezone.utc) + timedelta(days=1)


def _tokens(text: str, labels: list[str]) -> set[str]:
    banned = set(_STOP)
    for label in labels:
        banned.update(re.findall(r"[a-z0-9]+", label.casefold()))
    return {word for word in re.findall(r"[a-z0-9]+", text.casefold())
            if word not in banned and len(word) > 2}


def _reset_phrases(name: str) -> set[str]:
    """The phrases a rule may name one window's reset by, from its name."""
    spaced = name.replace("_", " ")
    words = {"five_hour": "five-hour", "weekly": "week", "daily": "day"}.get(name)
    if words is None and name.startswith("weekly_"):
        words = f"{name.removeprefix('weekly_').replace('_', ' ')} week"
    if words is None:
        mins = re.fullmatch(r"(\d+)_min", name)
        words = f"{mins.group(1)} min" if mins else spaced
    candidates = {spaced, spaced.replace(" ", "-"), words, name}
    return {f"{candidate} reset" for candidate in candidates if candidate}


def _named_resets(rules: list[dict]) -> dict:
    """Each account's windows as (reset phrase, time), plus ("reset", the
    earliest) for a rule that names no window. Only that account's phrases
    match its rules."""
    if not any(re.search(r"\breset\b", rule.get("text") or "", re.IGNORECASE) for rule in rules):
        return {}
    from khipu import usage

    try:
        rows = usage._load_cache()
    except (OSError, ValueError, TypeError):
        return {}
    found: dict[str, list] = {}
    for key, row in (rows or {}).items():
        if not isinstance(row, dict):
            continue
        scoped = []
        for window in row.get("windows") or []:
            if not isinstance(window, dict):
                continue
            at = _parse(window.get("resets_at"))
            if at is None:
                continue
            scoped += [(phrase, at) for phrase in _reset_phrases(str(window.get("name") or ""))]
        if scoped:
            scoped.append(("reset", min(at for _phrase, at in scoped)))
            found[key] = scoped
    return found


def _accounts(view: dict) -> tuple[dict, dict]:
    by_key, by_label = {}, {}
    for account in view.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        record = _record(account.get("key"), account.get("label"), account.get("harness"),
                         account.get("state"), account.get("configured", True))
        if record["key"] is not None:
            by_key[record["key"]] = record
        if record.get("label"):
            by_label.setdefault(record["label"].casefold(), record)
    priority = view.get("priority") if isinstance(view.get("priority"), dict) else {}
    for entry in (priority or {}).get("entries") or []:
        key = entry.get("account")
        if key in by_key:
            continue
        record = _record(key, entry.get("label"), None, "open", entry.get("in_settings", True))
        if key is not None:
            by_key[key] = record
        if record.get("label"):
            by_label.setdefault(record["label"].casefold(), record)
    return by_key, by_label


def _record(key, label, harness, state, configured) -> dict:
    return {"key": key, "id": _public_id(key, label, harness), "label": label,
            "state": state, "configured": configured}


def _public_id(key, label, harness) -> str | None:
    from khipu import routing_identity as identity

    if isinstance(key, str) and identity.STABLE.fullmatch(key):
        return key
    harness = harness or (key.partition("|")[0] if isinstance(key, str) else "")
    if not harness or not isinstance(label, str) or not label:
        return None
    try:
        return identity.stable_id(harness, label)
    except (ValueError, TypeError):
        return None


def _record_for_key(key, label, by_key: dict) -> dict | None:
    if key in by_key:
        return by_key[key]
    if not isinstance(label, str):
        return None
    record = _record(key, label, None, "open", True)
    return record if record.get("id") else None


def _held_keys(view: dict) -> set:
    found = set()
    for hold in view.get("holds") or []:
        scope = hold.get("scope") if isinstance(hold, dict) else None
        if isinstance(scope, dict) and scope.get("kind") == "account" and scope.get("account"):
            found.add(scope["account"])
    return found


def _priority_state(record: dict | None, entry: dict, held: set):
    if entry.get("in_settings") is False:
        return "untracked"
    return _rule_state(record, held) or ("down" if record and record.get("state") == "down" else None)


def _rule_state(record: dict | None, held: set):
    if not record:
        return None
    if record.get("key") in held:
        return "held"
    if record.get("state") == "paused":
        return "paused"
    return None


def _named(text: str, by_key: dict, by_label: dict) -> list[dict]:
    found = []
    seen = set()
    for label, record in by_label.items():
        if re.search(rf"(?<![\w-]){re.escape(label)}(?![\w-])", text, re.IGNORECASE):
            if record.get("key") not in seen:
                found.append(record)
                seen.add(record.get("key"))
    for record in by_key.values():
        ident = record.get("id")
        if ident and record.get("key") not in seen and re.search(
                rf"(?<![\w-]){re.escape(ident)}(?![\w-])", text, re.IGNORECASE):
            found.append(record)
            seen.add(record.get("key"))
    return found


def _flag(kind: str, account, entities: list, text: str, *, state=None, source=None) -> dict:
    flag = {"kind": kind, "account": account, "entities": entities, "text": text, "advisory": True}
    if state is not None:
        flag["state"] = state
    if source is not None:
        flag["source"] = source
    return flag


def _parse(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def model_tier(model: str | None) -> str | None:
    """The tier a model id belongs to, from its name parts. Unknown stays unknown."""
    if not isinstance(model, str) or not model.strip():
        return None
    parts = set(re.findall(r"[a-z]+", model.casefold()))
    for name in ("opus", "sonnet", "haiku"):
        if name in parts:
            return name
    return None


def task_class(brief: str | None) -> tuple[str | None, bool]:
    """(class, guessed). A ``task:`` line wins. Otherwise one keyword guess."""
    match = _TASK_LINE.search(brief or "")
    if match and match.group(1) in TASK_CLASSES:
        return match.group(1), False
    low = (brief or "").casefold()
    for name, phrases in _GUESSES:
        if any(re.search(rf"(?<![a-z0-9-]){re.escape(phrase)}(?![a-z0-9-])", low) for phrase in phrases):
            return name, True
    return None, False


def format_line(task: str, expected: str, actual: str, guessed: bool) -> str:
    if not guessed and task == "fix-round" and expected == "sonnet" and actual == "opus":
        return FIX_ROUND_OPUS
    guess = " (a guess)" if guessed else ""
    phrase = _PHRASE.get(task, task)
    return (f"Rule 12: {phrase} go to {expected.title()}{guess}; "
            f"this dispatch is {actual.title()}. Say why if intended.")


def dispatch_line(model: str | None, brief: str | None) -> str | None:
    """The one rule 12 line for this dispatch, or None when it matches or no
    confirmed table governs. Never a permission decision."""
    from khipu import routing

    table = (routing.read_view().get("tier_table") or {})
    tiers = table.get("tiers") if table.get("confirmed") is True else None
    if not valid_tiers(tiers):
        return None
    task, guessed = task_class(brief)
    actual = model_tier(model)
    expected = tiers.get(task) if task else None
    if task is None or actual is None or expected is None or actual == expected:
        return None
    return format_line(task, expected, actual, guessed)


def settings_model(home: str) -> str | None:
    """The ``model`` string in a Claude settings file, or None.

    Only that field is kept. The file is size-capped. A missing or unreadable
    file is no model, and its error text is not kept.
    """
    path = Path(home) / "settings.json"
    try:
        if not path.is_file() or path.stat().st_size > _SETTINGS_MOST:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    model = data.get("model") if isinstance(data, dict) else None
    return model if isinstance(model, str) else None
