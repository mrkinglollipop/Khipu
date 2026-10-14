"""Routing truth, phases 1a and 1b: one record of which accounts are open.

Phase 1b folds in the sources (``khipu.routing_sources``): job runners'
files, read and never written; Khipu's usage cache; and the routing table's
NOW block as a pointer only. Each line of the log carries its sequence
number, settled by the writer under the lock (``_stamp_locked``).

Scope: docs/plans/2026-10-14-routing-truth.md (the Record, Folding the
sources, Doctor warnings, Phase 1a and Decisions). Khipu reports; it never
chooses, refuses, launches or reorders work. Nothing here returns a pick or a
ranking, and folding never starts a process, calls a model or takes another
program's lock.

The record lives beside the usage cache:

  state/routing/events.jsonl  the full history, append-only, written under
                              state/routing/routing.lock (Khipu's own lock)
  state/routing/view.json     the folded view, replaced atomically

An account is a login, keyed as the usage cache keys it (``usage._key``:
``<harness>|<absolute login home>``), never a lane. Every entry carries who
wrote it, set by Khipu and never taken from the writer's say-so, and when, by
Khipu's clock. Each line of the log is read on its own, from its bytes, into
the fields Khipu writes (``_line_event``): one line that is not UTF-8, not
JSON or not of a shape Khipu writes is counted and left out, and no text in
any input (a lone surrogate) or object (deep or odd) can stop a read or a
write.

Every read refolds when an input file changed or the view's ``valid_until``
has passed, so a limit's end, a hold's end or an entry going stale becomes a
version with no file changing. The version moves only when the effective view
changes (each account's state, until, scope, basis and disagreement; the
rules; the note; the holds): who wrote an entry and when are shown but are
not part of it, so the same report from many conversations does not move it.
The version is kept in view.json with a random ``epoch``; if that file is
lost, replaced or changed by anything but Khipu (it carries a digest of
itself) the count starts again under a new epoch, and ``since``
answers a version newer than the current one, or from another epoch, with
everything (``complete: false``).

"Owner only" (rules, the note, pauses, lifts and clears) is a convention, not
a lock: Khipu holds no credential. The CLI asks for a typed confirmation on a
terminal and refuses without one, the app marks its own writes, and doctor
warns when one of them was written by anything other than the app.
"""
from __future__ import annotations

import bisect
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = 1
STATE_DIR = ("state", "routing")
EVENTS_NAME = "events.jsonl"
VIEW_NAME = "view.json"
LOCK_NAME = "routing.lock"
CHANGES_KEPT = 100

LIMITED, OPEN, DOWN = "limited", "open", "down"
PAUSED, PROBABLY_OPEN, UNKNOWN = "paused", "probably_open", "unknown"
BASES = ("cli", "estimated", "checked", "owner")
STRONG_BASES = ("cli", "checked", "owner")
SOURCES = ("probe", "check", "runner", "report", "owner")
WINDOW_SECONDS = {"five_hour": 5 * 3600, "weekly": 7 * 86400}
HOLD_REASONS = {"machine_busy": "machine busy", "disk_low": "disk low", "keep": "keep for one purpose",
                "outage": "outage", "other": "other"}
DOWN_REASONS = {"not_signed_in": "not signed in", "not_installed": "not installed",
                "out_of_service": "out of service"}
EVIDENCE_MAX = 200
NAME_MAX = 64
# The harness names Khipu writes as a conversation's author; any other value
# (KHIPU_HARNESS is the caller's own environment) is stored as "unknown".
HARNESS_WORDS = {"claude_code": "Claude Code", "codex": "Codex", "aegis": "Aegis", "grok": "Grok Build",
                 "grok_build": "Grok Build", "cursor": "Cursor", "muse": "Muse"}
SHELL, UNKNOWN_HARNESS = "shell", "unknown"
# A model name as a CLI gives it (opus, gpt-6.1-sol, claude-opus-4.1): never
# a conversation's sentence, which would reach the push as a model's name.
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
# How far ahead of this Mac's clock a time already in the record may be and
# still be taken over the clock (another writer's clock read a moment ago).
# A time further ahead is not trusted: the clock is used, and doctor warns.
CLOCK_TOLERANCE = 300
# Display order of account lines: what closes work first.
_STATE_ORDER = {LIMITED: 0, PAUSED: 1, DOWN: 2, UNKNOWN: 3, PROBABLY_OPEN: 4, OPEN: 5}


class RouteError(ValueError):
    """A write or a setting Khipu refuses; the text is shown as written."""


# ---- settings ----------------------------------------------------------------
#
# Every size, time and switch the scope names is a setting the owner can raise
# or remove on the spot (decision 13). Stored in config.json; 0 turns a limit
# off where the words say so. Several are read only from a later phase; they
# are registered now so the app has one list to show.

SETTINGS: dict[str, tuple[str, Any, str]] = {
    # The record (phase 1a)
    "routing_stale_after": ("duration", 12 * 3600, "Treat a report as out of date after (0: never)"),
    "routing_report_default_limit": ("duration", 30 * 60,
                                     "How long a reported limit lasts when the report names no time"),
    "routing_conversation_hold": ("duration", 24 * 3600, "How long a conversation's hold lasts"),
    "routing_conversation_hold_max": ("duration", 7 * 86400,
                                      "Longest hold a conversation may set (0: no maximum)"),
    "routing_hold_chars": ("count", 300, "Longest words in a hold (0: no limit)"),
    "routing_owner_hold_reminder": ("duration", 90 * 86400, "Remind you of your own holds after (0: never)"),
    "routing_disagreement_warn_after": ("duration", 30 * 60, "Warn when sources disagree for longer than (0: never)"),
    "routing_summary_chars": ("count", 1500,
                              "Longest summary at the start of a conversation, rules and note aside (0: no limit)"),
    "routing_line_chars": ("count", 200, "Longest line, rules and note aside (0: no limit)"),
    "routing_rules_seed": ("path", None, "Local file the standing rules are imported from, once"),
    "routing_lane_map": ("lane_map", {}, "Which account each job lane uses"),
    # The sources (phase 1b)
    "routing_sources": ("sources", [], "Job runner folders"),
    "routing_table": ("path", None, "Routing table file"),
    "routing_runner_silent_intervals": ("count", 5, "A job runner has stopped reporting after this many intervals"),
    # Telling conversations (phases 3a to 3c and 6)
    "routing_push_start": ("switch", True, "Tell conversations at the start and after compaction"),
    "routing_push_prompt": ("switch", True, "Tell conversations before each prompt"),
    "routing_push_delegation": ("switch", True, "Tell conversations before handing work to another agent"),
    "routing_prompt_chars": ("count", 400, "Longest update before a prompt, rules and note aside (0: no limit)"),
    "routing_prompt_budget_ms": ("count", 50, "Time allowed before a prompt, in milliseconds"),
    "routing_resend_after_versions": ("count", 20, "Send everything again after this many changes (0: never)"),
    "routing_resend_after": ("duration", 6 * 3600, "Send everything again after (0: never)"),
    "routing_not_refreshed_after": ("duration", 5 * 60, "The record counts as not refreshed after (0: never)"),
    "routing_poll_interval": ("duration", 10, "How often the daemon looks at the source files"),
    "routing_slow_read_pct": ("number", 1.0, "Warn when reading before a prompt is too slow on more than this % of prompts"),
    # Checking for resets (phase 5)
    "routing_reset_check": ("switch", True, "Check an account at its reset time"),
    "routing_reset_check_wait": ("duration", 120, "Wait after the reset time"),
    "routing_reset_check_retries": ("durations", [600, 1800], "Try again after"),
    "routing_busy_load": ("number", 75.0, "Wait while the Mac is busy: load over (0: never wait)"),
    "routing_busy_wait": ("duration", 300, "How long to wait while the Mac is busy"),
    "routing_busy_tries": ("count", 3, "How many times to wait while the Mac is busy"),
    # Copy for cloud agents (phase 7)
    "routing_cloud_copy": ("switch", False, "Copy for cloud agents"),
    "routing_cloud_copy_hold_words": ("switch", False, "Include hold words in the copy"),
    "routing_cloud_copy_rules": ("switch", False, "Include the rules and the note in the copy"),
    "routing_cloud_copy_stale_after": ("duration", 600, "The copy counts as out of date after"),
    "routing_cloud_copy_debounce": ("duration", 30, "Wait for a burst of changes to end before copying"),
}

_UNITS = {"w": 604800, "wk": 604800, "week": 604800, "weeks": 604800, "d": 86400, "day": 86400, "days": 86400,
          "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600, "m": 60, "min": 60, "mins": 60,
          "minute": 60, "minutes": 60, "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1}
_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)")
_TRUE = {"1", "true", "on", "yes"}
_FALSE = {"0", "false", "off", "no"}
_CREDENTIAL_PARTS = {"api keys", "my portfolio", "keychains"}
_CREDENTIAL_SUFFIXES = (".keychain", ".keychain-db", ".p12", ".pfx")


def parse_duration(raw) -> float:
    """Seconds from a number or text such as ``12h``, ``30m``, ``1h30m``, ``7d``."""
    if isinstance(raw, bool):
        raise ValueError("a length of time, not true or false")
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        text = str(raw or "").strip().lower()
        if text.startswith("in "):
            text = text[3:].strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            value = float(text)
        elif text and re.fullmatch(r"(?:\s*\d+(?:\.\d+)?\s*[a-z]+)+\s*", text):
            value = 0.0
            for amount, unit in _DURATION.findall(text):
                if unit not in _UNITS:
                    raise ValueError(f"{raw!r} is not a length of time (use s, m, h, d or w)")
                value += float(amount) * _UNITS[unit]
        else:
            raise ValueError(f"{raw!r} is not a length of time (for example 30m, 12h or 7d)")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{raw!r} is not a length of time of 0 or more")
    return value


def credential_location(path: str | os.PathLike) -> bool:
    """True for a path inside a credential location: an ``API Keys`` or
    ``My Portfolio`` folder, a keychain folder or export, or any
    ``auth.json``. Decided from the path's names alone: nothing is opened."""
    try:
        p = Path(path).expanduser()
    except RuntimeError:
        p = Path(str(path))
    parts = [part.casefold() for part in p.parts]
    if any(part in _CREDENTIAL_PARTS for part in parts):
        return True
    if p.name.casefold() == "auth.json":
        return True
    return any(part.endswith(_CREDENTIAL_SUFFIXES) for part in parts)


def _short_text(value, what: str, limit: int = NAME_MAX) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be text")
    value = value.strip()
    if len(value) > limit or not value.isprintable():
        raise ValueError(f"{what} must be one line of at most {limit} characters")
    return value


def _loads(text):
    """``json.loads``, with JSON nested too deeply to read raised as
    ValueError like any other unreadable JSON. Python 3.11, the interpreter
    the app ships, raises RecursionError there (3.14 reads it), and one such
    line Khipu never wrote must not stop every read and write."""
    try:
        return json.loads(text)
    except RecursionError:
        raise ValueError("JSON nested too deeply to read") from None


_SURROGATE = re.compile(f"[{chr(0xD800)}-{chr(0xDFFF)}]")


def _no_surrogates(text: str) -> str:
    """``text`` with each lone surrogate replaced by U+FFFD. A JSON escape
    such as ``\\ud800``, or a name from argv or the environment holding bytes
    that are not UTF-8, gives one; no UTF-8 file can hold it, so one left in
    would stop every write of the log and every save of the view."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return _SURROGATE.sub("\ufffd", text)
    return text


def _scrub(value):
    """``value`` (dicts, lists and text Khipu built) with every lone
    surrogate replaced, in keys too."""
    if isinstance(value, str):
        return _no_surrogates(value)
    if isinstance(value, dict):
        return {_no_surrogates(k) if isinstance(k, str) else k: _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


def _coerce(kind: str, value, key: str):
    """``value`` as the setting's stored form, or ValueError naming ``key``."""
    if kind == "duration":
        seconds = parse_duration(value)
        return int(seconds) if seconds == int(seconds) else seconds
    if kind == "count":
        if isinstance(value, bool):
            raise ValueError(f"{key} must be a whole number of 0 or more")
        try:
            number = int(str(value).strip()) if not isinstance(value, int) else value
        except ValueError:
            raise ValueError(f"{key} must be a whole number of 0 or more, got {value!r}") from None
        if number < 0:
            raise ValueError(f"{key} must be a whole number of 0 or more, got {number}")
        return number
    if kind == "number":
        if isinstance(value, bool):
            raise ValueError(f"{key} must be a number of 0 or more")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number of 0 or more, got {value!r}") from None
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"{key} must be a number of 0 or more, got {value!r}")
        return number
    if kind == "switch":
        if isinstance(value, bool):
            return value
        word = str(value).strip().lower()
        if word in _TRUE:
            return True
        if word in _FALSE:
            return False
        raise ValueError(f"{key} must be on or off, got {value!r}")
    if kind == "durations":
        items = value
        if isinstance(value, str):
            text = value.strip()
            items = _loads(text) if text.startswith("[") else [p for p in text.split(",") if p.strip()]
        if not isinstance(items, list) or not items or len(items) > 10:
            raise ValueError(f"{key} must be one to ten lengths of time, for example 10m,30m")
        return [_coerce("duration", item, key) for item in items]
    if kind == "path":
        # Absolute only: a relative name would be read against whichever
        # folder each reader (the app, doctor, a hook) runs in, and the
        # private-folder check would see only the typed names.
        path = Path(_short_text(value, key, 1024)).expanduser()
        if not path.is_absolute():
            raise ValueError(f"{key} needs the file's full path (starting with / or ~)")
        if credential_location(path):
            raise ValueError(f"{key} can't name a file inside a private folder (API Keys, My Portfolio, "
                             "a keychain or an auth.json)")
        return str(path)
    if kind == "lane_map":
        data = _loads(value) if isinstance(value, str) else value
        if not isinstance(data, dict) or len(data) > 64:
            raise ValueError(f'{key} must be JSON like {{"runner": {{"lane": "account"}}}}')
        out: dict[str, dict[str, str]] = {}
        for runner, lanes in data.items():
            runner = _short_text(runner, f"{key}: a runner name")
            if not isinstance(lanes, dict) or len(lanes) > 256:
                raise ValueError(f"{key}: the lanes of {runner} must be an object of lane: account")
            out[runner] = {_short_text(lane, f"{key}: a lane name"): _short_text(acct, f"{key}: an account", 1100)
                           for lane, acct in lanes.items()}
        return out
    if kind == "sources":
        data = _loads(value) if isinstance(value, str) else value
        if not isinstance(data, list) or len(data) > 32:
            raise ValueError(f'{key} must be a JSON list of {{"name", "kind": "lanes", "path"}}')
        out_list: list[dict] = []
        names: set[str] = set()
        for i, entry in enumerate(data):
            if not isinstance(entry, dict) or set(entry) - {"name", "kind", "path"}:
                raise ValueError(f"{key} entry {i + 1} must have only name, kind and path")
            name = _short_text(entry.get("name"), f"{key} entry {i + 1}: name")
            if entry.get("kind") != "lanes":
                raise ValueError(f'{key} entry {i + 1}: kind must be "lanes"')
            path = Path(_short_text(entry.get("path"), f"{key} entry {i + 1}: path", 1024)).expanduser()
            if not path.is_absolute():
                raise ValueError(f"{key} entry {i + 1}: give the folder's full path")
            if credential_location(path):
                raise ValueError(f"{key} entry {i + 1}: a job runner folder can't be inside a private folder "
                                 "(API Keys, My Portfolio, a keychain or an auth.json)")
            if name.casefold() in names:
                raise ValueError(f"{key}: two entries are named {name!r}")
            names.add(name.casefold())
            out_list.append({"name": name, "kind": "lanes", "path": str(path)})
        return out_list
    raise ValueError(f"unknown kind for {key}")


def settings(config: dict | None = None) -> dict[str, Any]:
    """Every routing setting's value: the stored one when valid, else the
    default. A hand-edited bad value reads as the default (shown by
    ``settings_status``), never as an error."""
    if config is None:
        from khipu.config import load_config

        config = load_config()
    out: dict[str, Any] = {}
    for key, (kind, default, _words) in SETTINGS.items():
        stored = config.get(key)
        value = default
        if stored is not None:
            try:
                value = _coerce(kind, stored, key)
            except (ValueError, TypeError):
                value = default
        out[key] = value
    return out


def settings_status() -> dict[str, dict]:
    """What ``khipu config`` shows: each setting's value, default, words and
    whether it comes from the file."""
    from khipu.config import load_config

    config = load_config()
    values = settings(config)
    out = {}
    for key, (kind, default, words) in SETTINGS.items():
        source = "default"
        if config.get(key) is not None:
            try:
                _coerce(kind, config[key], key)
                source = "file"
            except (ValueError, TypeError):
                source = "invalid"
        out[key] = {"value": values[key], "default": default, "kind": kind, "source": source, "words": words}
    return out


def set_setting(key: str, raw) -> Path:
    """Store a routing setting (``None`` removes it: the default again)."""
    from khipu.config import _load_config_for_mutation, save_config

    if key not in SETTINGS:
        raise KeyError(f"unknown routing setting {key!r}")
    kind = SETTINGS[key][0]
    data = _load_config_for_mutation()
    if raw is None:
        data.pop(key, None)
    else:
        try:
            data[key] = _coerce(kind, raw, key)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{key} must be JSON: {exc.msg}") from None
    return save_config(data)


# ---- time and text -------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _after(dt: datetime, seconds: float) -> datetime:
    """``dt`` plus ``seconds``, held at the last time Python can hold
    instead of overflowing (a line dated 9999, a setting of a million days):
    a fold never fails on arithmetic."""
    try:
        return dt + timedelta(seconds=seconds)
    except OverflowError:
        return datetime.max.replace(tzinfo=timezone.utc, microsecond=0)


def _not_behind(now: datetime, other: datetime | None) -> datetime:
    """``other`` when it is ahead of ``now`` by at most ``CLOCK_TOLERANCE``,
    else ``now``. The record's times run forward, but a time further ahead of
    this Mac's clock than that (a clock that read ahead once, a line appended
    by hand) is not taken over the clock: it would pin every later write and
    fold to it, and limits and holds ending before it would show as ended."""
    if other is not None and now < other and (other - now).total_seconds() <= CLOCK_TOLERANCE:
        return other
    return now


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse(text) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _clock(dt: datetime, now: datetime) -> str:
    """'18:52Z' today, 'Thu 09:00Z' within the week, else 'Oct 21 09:00Z'."""
    d, n = dt.astimezone(timezone.utc), now.astimezone(timezone.utc)
    if d.date() == n.date():
        return d.strftime("%H:%MZ")
    if abs((d.date() - n.date()).days) < 7:
        return d.strftime("%a %H:%MZ")
    return d.strftime("%b %d %H:%MZ")


def _span(seconds: float) -> str:
    minutes = int(abs(seconds) // 60)
    if minutes < 1:
        return "less than a minute"
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days} d {hours} h" if hours else f"{days} d"
    if hours:
        return f"{hours} h {mins} min" if mins else f"{hours} h"
    return f"{mins} min"


def _ago(dt: datetime, now: datetime) -> str:
    seconds = (now - dt).total_seconds()
    return "just now" if seconds < 60 else f"{_span(seconds)} ago"


def _remaining(dt: datetime, now: datetime) -> str:
    seconds = (dt - now).total_seconds()
    return f"in {_span(seconds)}" if seconds > 0 else f"{_span(seconds)} ago"


def _one_line(text) -> str:
    return " ".join(str(text or "").split())


def _clip(text: str, limit: int) -> str:
    if limit and len(text) > limit:
        return text[: max(1, limit - 1)].rstrip() + "…"
    return text


def _clean(text, limit: int) -> str:
    """One line, secrets masked, clipped: for any words a conversation gives."""
    from khipu.redact import redact_secrets

    return _clip(redact_secrets(_one_line(text))[0], limit)


def _typed_time(raw, now: datetime, flag: str, *, ago: bool = False) -> datetime:
    """A time typed for ``flag``: a length (after ``now``, or before it with
    ``ago``) or an ISO time; an ISO time with no zone is this Mac's local
    time. A bare number is refused: "8" could mean 8 seconds, 8 hours or
    8 pm. A time outside the years Python holds (1 to 9999), with a zone or
    without one, is refused as a RouteError, never raised as a traceback."""
    text = str(raw or "").strip()
    if ago:
        text = re.sub(r"\s+ago$", "", text, flags=re.I)
    lengths = "10m or 2h (how long ago)" if ago else "30m, 2h or 3d"
    if re.fullmatch(r"(?:in\s+)?\d+(?:\.\d+)?", text, re.I):
        raise RouteError(f"{flag} needs a unit: {lengths} (got {raw!r}), or a time such as 2026-10-14T18:52Z")
    ahead = f"{flag} is further ahead than Khipu can hold (got {raw!r}); give a time before the year 9999"
    back = f"{flag} is further back than Khipu can hold (got {raw!r}); give a time after the year 1"
    try:
        seconds = parse_duration(text)
    except ValueError:
        seconds = None
    if seconds is not None:
        try:
            return now - timedelta(seconds=seconds) if ago else now + timedelta(seconds=seconds)
        except OverflowError:
            raise RouteError(back if ago else ahead) from None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise RouteError(f"{flag} takes a time (2026-10-14T18:52Z) or a length ({lengths}), got {raw!r}") from None
    try:
        if dt.tzinfo is None:
            dt = dt.astimezone()  # this Mac's local time
        return dt.astimezone(timezone.utc).replace(microsecond=0)
    except (OverflowError, ValueError, OSError):
        # Near the year limits a time with no zone raises ValueError (its
        # local offset can't be worked out) and one with a zone OverflowError.
        raise RouteError(ahead if dt.year > 5000 else back) from None


def parse_until(raw: str, now: datetime) -> datetime:
    """A time the owner or a conversation typed for ``--until``: a length
    (``30m``, ``in 2h``) or an ISO time (see ``_typed_time``)."""
    return _typed_time(raw, now, "--until")


def parse_seen(raw: str, now: datetime) -> datetime:
    """When a conversation saw a limit message (``--seen``): an ISO time or
    how long ago (``10m``, ``10m ago``). Never later than now: a time ahead
    of this Mac's clock by more than ``CLOCK_TOLERANCE`` is refused, and one
    within it is taken as now."""
    seen = _typed_time(raw, now, "--seen", ago=True)
    if (seen - now).total_seconds() > CLOCK_TOLERANCE:
        raise RouteError(f"--seen is when the limit message was shown, so not later than now (got {raw!r})")
    return min(seen, now)


_RESET_ISO = re.compile(r"\bresets?\s+at\s+(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
                        r"(?:Z|[+-]\d{2}:?\d{2})?)", re.I)
_RESET_CLOCK = re.compile(r"\bresets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?\s*"
                          r"\(([A-Za-z][A-Za-z0-9_+\-/]*)\)", re.I)
_SIGNIN = re.compile(r"not (signed|logged) in|(log|sign) ?in (is )?required|please (log|sign) ?in", re.I)


def parse_limit_message(text: str, now: datetime, seen: datetime | None = None) -> tuple[datetime | None, bool]:
    """The reset time in a limit message Khipu reads itself, in the two forms
    the scope names, and whether that time is the message's own.
    "resets at <ISO time>" (UTC when it names no zone) always is. "resets
    8pm (America/New_York)" names no day: it is the next 8 pm there after
    ``seen``, when the message was shown, and the message's own time only
    then. Without ``seen`` the day is taken from ``now``, which is a guess:
    a message shown at 7:50 pm and reported at 8:10 pm would land a day
    late. (None, False) when the message has neither form."""
    if not text:
        return None, False
    m = _RESET_ISO.search(text)
    if m:
        when = _parse(m.group(1).replace(" ", "T"))
        return when, when is not None
    m = _RESET_CLOCK.search(text)
    if not m:
        return None, False
    hour, minute, half, zone = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower(), m.group(4)
    if not (1 <= hour <= 12 and 0 <= minute <= 59):
        return None, False
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(zone)
    except (KeyError, ValueError, OSError):
        return None, False
    hour24 = hour % 12 + (12 if half == "p" else 0)
    try:
        local_then = (seen or now).astimezone(tz)
        day = local_then.date()
        for _ in range(2):
            cand = datetime(day.year, day.month, day.day, hour24, minute, tzinfo=tz)
            if cand > local_then:
                return cand.astimezone(timezone.utc), seen is not None
            day = day + timedelta(days=1)
    except (OverflowError, ValueError):  # a day past the years Python holds
        pass
    return None, False


# ---- accounts and authors -----------------------------------------------------------


def accounts() -> list[dict]:
    """The accounts plan usage reads (``usage_accounts``, or every installed
    CLI with its default login), keyed as the usage cache keys them."""
    from khipu import usage

    out = []
    for account in usage.configured_accounts():
        if account.get("invalid"):
            continue
        out.append({"key": usage._key(account), "label": account["label"], "harness": account["harness"],
                    "home": account.get("home")})
    return out


def _fold_accounts(config: dict) -> list[dict]:
    """The accounts a fold shows before any entry names them: the configured
    ``usage_accounts`` only (config.json is one of the fold's inputs). Never
    the installed-CLI lookup: it depends on the caller's PATH, so two callers
    would fold different account sets and move the version with nothing
    changed. The fold adds every account an entry names."""
    from khipu import usage

    raw = config.get("usage_accounts")
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        account = usage._account_from_entry(entry)
        if account.get("invalid"):
            continue
        out.append({"key": usage._key(account), "label": account["label"], "harness": account["harness"],
                    "home": account.get("home")})
    return out


def _label_for_key(key: str) -> str:
    from khipu import usage

    harness, _, home = key.partition("|")
    base = usage.LABELS.get(harness, harness or "?")
    return f"{base} ({_clean(Path(home).name, NAME_MAX)})" if home else base


def resolve_account(name: str, known: list[dict] | None = None, *, conversation: bool = False) -> str:
    """An account's key from its label, its key (``harness|login folder``) or
    a bare harness name; RouteError when Khipu doesn't know it. A login
    folder inside a private folder, or whose name looks like a secret, is
    refused for everyone; a conversation may name a login folder only when
    it is one of the accounts Khipu knows, so no text of its own becomes an
    account key or a pushed label."""
    from khipu import usage
    from khipu.redact import redact_secrets

    known = accounts() if known is None else known
    raw = (name or "").strip()
    if not raw:
        raise RouteError("name an account")
    if "|" in raw:
        harness, _, home = raw.partition("|")
        harness, home = harness.strip().lower(), home.strip()
        if harness not in usage.HARNESSES:
            raise RouteError("that names no CLI Khipu reads (claude, codex, grok, muse)")
        if home and (credential_location(home) or redact_secrets(home, force=True)[1]):
            raise RouteError("that login folder is inside a private folder or its name looks like a secret; "
                             "Khipu won't record it")
        key = usage._key({"harness": harness, "home": home or None})
        if conversation and home and key not in {a["key"] for a in known}:
            labels = ", ".join(a["label"] for a in known) or "none"
            raise RouteError(f"Khipu doesn't know that login folder; a conversation can report only on the "
                             f"accounts Khipu knows: {labels}")
        return key
    for account in known:
        if account["label"].casefold() == raw.casefold():
            return account["key"]
    low = raw.lower()
    if low in usage.HARNESSES:
        for account in known:
            if account["harness"] == low and not account["home"]:
                return account["key"]
        return f"{low}|"
    labels = ", ".join(a["label"] for a in known) or "none"
    raise RouteError(f"Khipu doesn't know the account {raw!r}; known: {labels} (or harness|login folder)")


def _resolve_quiet(name: str, known: list[dict]) -> str | None:
    try:
        return resolve_account(name, known)
    except RouteError:
        return None


def _project_name(cwd: str | None) -> str | None:
    from khipu.identity import WORKTREE_MARKERS, _is_scratch_cwd

    path = str(cwd or "")
    if not path or _is_scratch_cwd(path):
        return None
    for marker in WORKTREE_MARKERS:
        if marker in path:
            path = path.split(marker)[0]
            break
    return _clean(Path(path).name, NAME_MAX) or None


def conversation_author(environ: dict | None = None, cwd: str | None = None, name: str | None = None) -> dict:
    """Who a conversation's write is from, as Khipu sees it: its harness,
    project and session id from its own environment and folder. A name the
    conversation gives itself is kept beside these, never in their place."""
    env = os.environ if environ is None else environ
    harness = (env.get("KHIPU_HARNESS") or "").strip().lower()
    if not harness:
        if env.get("GROK_HOOK_NAME") or env.get("GROK_HOOK_EVENT"):
            harness = "aegis"
        elif env.get("CLAUDECODE") or env.get("CLAUDE_CODE_SESSION_ID") or env.get("CLAUDE_CODE_ENTRYPOINT"):
            harness = "claude_code"
        elif env.get("CODEX_THREAD_ID") or env.get("CODEX_SANDBOX") or env.get("CODEX_HOME"):
            harness = "codex"
        else:
            harness = SHELL
    if harness not in HARNESS_WORDS and harness != SHELL:
        harness = UNKNOWN_HARNESS
    session = (env.get("KHIPU_SESSION_ID") or env.get("CLAUDE_CODE_SESSION_ID") or env.get("CODEX_THREAD_ID")
               or "").strip()
    if cwd is None:
        try:
            cwd = os.getcwd()
        except OSError:
            cwd = None
    out = {"kind": "conversation", "harness": harness, "project": _project_name(cwd),
           "session": _clean(session, 64) or None}
    if name and _one_line(name):
        out["name"] = _clean(name, NAME_MAX)
    return out


OWNER = {"kind": "owner"}


def author_words(by: dict | None, *, push: bool = True) -> str:
    """'a Claude Code conversation in khipu', 'you', 'Khipu usage probe'...
    With ``push`` only what Khipu set is used: never the name a conversation
    gives itself, and no session id."""
    by = by or {}
    kind = by.get("kind")
    if kind == "owner":
        return "you"
    if kind == "probe":
        return "Khipu usage probe"
    if kind == "check":
        return "Khipu reset check"
    if kind == "runner":
        return f"{by.get('name') or 'a job runner'} lanes (read by Khipu)"
    if kind == "seed":
        return "Khipu rules seed"
    if kind != "conversation":
        return "an unknown writer"
    harness = by.get("harness") or SHELL
    if harness == SHELL:
        words = "a shell command"
    elif isinstance(harness, str) and harness in HARNESS_WORDS:
        words = f"a {HARNESS_WORDS[harness]} conversation"
    else:  # never the stored text: it came from the writer's environment
        words = "a conversation from an unknown harness"
    if by.get("project"):
        words += f" in {by['project']}"
    if not push:
        if by.get("session"):
            words += f" (session {str(by['session'])[:8]})"
        if by.get("name"):
            words += f", which calls itself “{by['name']}”"
    return words


# ---- the log ---------------------------------------------------------------------


def routing_dir() -> Path:
    from khipu.paths import data_dir

    return data_dir().joinpath(*STATE_DIR)


def events_file() -> Path:
    return routing_dir() / EVENTS_NAME


def view_file() -> Path:
    return routing_dir() / VIEW_NAME


_held = threading.local()


@contextlib.contextmanager
def _lock():
    """Khipu's own routing lock (flock, released if the holder dies);
    re-entrant within a thread. Never another program's lock."""
    if getattr(_held, "depth", 0):
        _held.depth += 1
        try:
            yield
        finally:
            _held.depth -= 1
        return
    folder = routing_dir()
    folder.mkdir(parents=True, exist_ok=True)
    fd = os.open(folder / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _held.depth = 1
        try:
            yield
        finally:
            _held.depth = 0
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(4)


def _append_locked(*events: dict) -> None:
    """Append whole lines in one write, then fsync. A torn last line (a full
    disk or a power loss part way through an earlier write) is ended first,
    so it stays one unreadable line, counted in ``bad_lines``, and never
    swallows the new one. Lone surrogates (text from argv, the environment
    or a seed file) are replaced first: UTF-8 can't hold them."""
    data = "".join(json.dumps(_scrub(e), sort_keys=True, ensure_ascii=False) + "\n"
                   for e in events).encode("utf-8")
    fd = os.open(events_file(), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            data = b"\n" + data
        left = memoryview(data)
        while left:
            left = left[os.write(fd, left):]
        os.fsync(fd)
    finally:
        os.close(fd)


# The fields Khipu writes, by the type each has. A line is read into these
# alone: any other field is dropped, and a line with one of these of another
# type (a hand-edited or foreign line: a list for an account, a number for a
# key) would stop every fold, so it is counted as unreadable and left out.
_TEXT_FIELDS = ("id", "account", "runner", "lane", "reason", "target", "state", "basis", "source", "window",
                "evidence", "until", "written_at", "via", "text", "file", "date", "sha256", "clock_at")
_TIME_FIELDS = ("written_at", "until", "clock_at")
_WHOLE_FIELDS = ("n", "count", "seq")
_WHOLE_MOST = 2 ** 53
_SWITCH_FIELDS = ("message_time", "owner", "seeded")
_AUTHOR_FIELDS = ("kind", "harness", "project", "session", "name", "file")
_HOLD_KINDS = ("account", "machine", "all")


def _log_time(text: str) -> str | None:
    """A time from a line of the log in the form Khipu writes it (UTC, whole
    seconds), held within the years Python can hold; None when the text is
    no time at all."""
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        dt = dt.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        dt = (datetime.max if dt.year > 5000 else datetime.min).replace(tzinfo=timezone.utc)
    return _iso(dt)


def _checked(data: dict) -> dict | None:
    """The event Khipu folds from one parsed line: only the fields Khipu
    writes, each of the type Khipu writes it with, text with any lone
    surrogate replaced and times in Khipu's form; None when the line is not
    one Khipu could have written (it is counted as unreadable). A hold's
    scope keeps only its kind and account, so nothing else in a line, deep
    or odd, reaches the view."""
    out: dict[str, Any] = {"type": _no_surrogates(data["type"])}
    for field in _TEXT_FIELDS:
        value = data.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            return None
        out[field] = _no_surrogates(value)
    for field in _TIME_FIELDS:
        if field in out:
            out[field] = _log_time(out[field])
            if out[field] is None:
                return None
    if "written_at" not in out:
        return None  # every line Khipu writes carries its time
    for field in _WHOLE_FIELDS:
        value = data.get(field)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or abs(value) > _WHOLE_MOST:
            return None
        out[field] = value
    for field in _SWITCH_FIELDS:
        value = data.get(field)
        if value is None:
            continue
        if not isinstance(value, bool):
            return None
        out[field] = value
    used = data.get("used_pct")
    if used is not None:
        if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used):
            return None
        out["used_pct"] = used
    by = data.get("written_by")
    if by is not None:
        if not isinstance(by, dict) or not all(v is None or isinstance(v, str) for v in by.values()):
            return None
        out["written_by"] = {k: _no_surrogates(by[k]) for k in _AUTHOR_FIELDS if isinstance(by.get(k), str)}
    scope = data.get("scope")
    if scope is not None:
        if out["type"] != "hold":
            if not isinstance(scope, str):
                return None
            out["scope"] = _no_surrogates(scope)
        else:
            kind = scope.get("kind") if isinstance(scope, dict) else None
            if kind not in _HOLD_KINDS:
                return None
            if kind == "account":
                account = scope.get("account")
                if not isinstance(account, str) or not account:
                    return None
                out["scope"] = {"kind": kind, "account": _no_surrogates(account)}
            else:
                out["scope"] = {"kind": kind}
    return out


def _line_event(raw: bytes) -> dict | None:
    """One line of the log, decoded on its own from its bytes: the checked
    event (``_checked``), or None when the line is not UTF-8 (a write cut
    off inside a character), not JSON, nested too deeply to read, has no
    type or is not one Khipu could have written."""
    try:
        data = _loads(raw.decode("utf-8"))
    except ValueError:  # UnicodeDecodeError is a ValueError
        return None
    if not isinstance(data, dict) or not isinstance(data.get("type"), str):
        return None
    return _checked(data)


def read_events() -> tuple[list[dict], int]:
    """Every event in write order with its place in that order (``_seq``),
    and how many lines could not be read (kept in the file, left out of the
    fold). The file is read as bytes and each line decoded on its own, so one
    line that is not UTF-8, not JSON or not of a shape Khipu writes
    (``_line_event``) costs that line alone, never the read.

    Write order is settled when a line is written: the writer holds the lock,
    sees the last line and records the next sequence number (``seq``) with
    its line, and the lines that carry one are put in that order. Lines with
    no ``seq`` (written by phase 1a, or appended by hand) keep their place in
    the file."""
    try:
        data = events_file().read_bytes()
    except OSError:
        return [], 0
    events: list[dict] = []
    bad = 0
    # Split on the newline byte: UTF-8 never uses it inside a character, and
    # a JSON string may hold U+2028, which splitlines() would cut at.
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        event = _line_event(line)
        if event is None:
            bad += 1
            continue
        events.append(event)
    slots = [i for i, e in enumerate(events) if isinstance(e.get("seq"), int)]
    if slots:
        ordered = sorted((events[i] for i in slots), key=lambda e: e["seq"])  # stable: file order breaks a tie
        for i, event in zip(slots, ordered):
            events[i] = event
    for i, event in enumerate(events):
        event["_seq"] = i
    return events, bad


def _stamp_locked(clock: datetime, count: int = 1) -> list[dict]:
    """The time and sequence number of the next ``count`` lines, settled now,
    while the writer holds the lock and sees the record as it stands.

    The sequence number follows the highest one written (or the number of
    readable lines before the first). The time is the writer's clock, or the
    newest line's time when that is a little ahead of it (``_not_behind``).
    When the clock reads more than ``CLOCK_TOLERANCE`` before every line of a
    record of two lines or more (a Mac whose clock reads 2001), the record is
    older than the clock says: the line takes the newest line's time and
    keeps the clock's reading beside it (``clock_at``), so a run of such
    lines never ages the history written before it, however long the run,
    and doctor names those lines. A clock behind only some of the record (a
    line written ahead, then a right clock) keeps its own time, and the fold
    orders the lines as in phase 1a."""
    events, _bad = read_events()
    seqs = [e["seq"] for e in events if isinstance(e.get("seq"), int)]
    first = max(seqs) + 1 if seqs else len(events)
    times = [t for t in (_parse(e.get("written_at")) for e in events) if t is not None]
    last = _parse(events[-1].get("written_at")) if events else None
    clock_at = None
    stamp = _not_behind(clock, last)
    if len(times) >= 2 and last is not None and (min(times) - clock).total_seconds() > CLOCK_TOLERANCE:
        stamp, clock_at = last, clock
    out = []
    for i in range(count):
        line = {"written_at": _iso(stamp), "seq": first + i}
        if clock_at is not None:
            line["clock_at"] = _iso(clock_at)
        out.append(line)
    return out


def _stat(path: Path | None) -> list | None:
    if path is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size, st.st_ino]


def _inputs() -> tuple[dict, dict, list[dict]]:
    """The fold's inputs: (fingerprint, settings, accounts). Each file is
    stat'ed before it is read, so a file saved in between makes the next
    read refold; a fold from older settings is never kept under a newer
    fingerprint."""
    from khipu.config import config_file, load_config

    from khipu import routing_sources

    events_stat, config_stat = _stat(events_file()), _stat(config_file())
    config = load_config()
    cfg = settings(config)
    # routing_sources is read entry by entry (one bad entry costs only itself).
    cfg["_raw_sources"] = {"routing_sources": config.get("routing_sources")}
    seed = cfg.get("routing_rules_seed")
    fingerprint = {"events": events_stat, "config": config_stat, "seed": _stat(Path(seed)) if seed else None,
                   "sources": routing_sources.fingerprint(cfg["_raw_sources"], cfg)}
    return fingerprint, cfg, _fold_accounts(config)


def _sources(cfg: dict, now: datetime, prev: dict | None) -> tuple[list[dict], dict | None, dict]:
    """(evidence, status, cache) from the sources (phase 1b): job runners'
    files, the usage cache and the table pointer. Reads only. ``prev`` is the
    last saved view, whose cache holds each runner file's last good read."""
    from khipu import routing_sources

    if not routing_sources.any_configured(cfg["_raw_sources"], cfg):
        return [], None, {}
    return routing_sources.collect(cfg["_raw_sources"], cfg, now, (prev or {}).get("source_cache"))


def _evidence_account(event: dict, lane_map: dict, known: list[dict]) -> str | None:
    """The account an evidence entry is about: its own, or its runner's lane
    through ``routing_lane_map``; None when the lane isn't in the map (never
    guessed)."""
    key = event.get("account")
    if key or not event.get("runner"):
        return key or None
    target = (lane_map.get(event["runner"]) or {}).get(event.get("lane") or "")
    return _resolve_quiet(target, known) if target else None


# ---- the fold ----------------------------------------------------------------------


def _k(item: dict) -> tuple:
    return (item["at"], item["seq"])


def _evidence(event: dict, cfg: dict) -> dict | None:
    at = _parse(event.get("written_at"))
    state = event.get("state")
    if at is None or state not in (LIMITED, OPEN, DOWN):
        return None
    until = _parse(event.get("until"))
    if state == LIMITED and until is None:
        until = _after(at, cfg["routing_report_default_limit"] or 1800)
    return {"id": str(event.get("id") or f"line{event['_seq']}"), "account": event.get("account"),
            "runner": event.get("runner"), "lane": event.get("lane"), "state": state,
            "until": until if state == LIMITED else None,
            "basis": event.get("basis") if event.get("basis") in BASES else "estimated",
            "scope": event.get("scope") or "all", "window": event.get("window"), "used_pct": event.get("used_pct"),
            "source": event.get("source") if event.get("source") in SOURCES else "report",
            "written_by": event.get("written_by") or {}, "via": event.get("via"), "at": at, "seq": event["_seq"],
            "evidence": event.get("evidence"), "reason": event.get("reason"),
            "message_time": bool(event.get("message_time"))}


def _owner_event(event: dict) -> dict:
    return {"id": event.get("id"), "at": _parse(event.get("written_at")), "seq": event["_seq"],
            "until": _parse(event.get("until")), "written_by": event.get("written_by") or OWNER,
            "via": event.get("via"), "target": event.get("target"), "account": event.get("account")}


def _fold_account(account: dict, items: list[dict], owner: dict, now: datetime, cfg: dict) -> dict:
    key = account["key"]
    stale_after = cfg["routing_stale_after"]

    def stale(e: dict) -> bool:
        # Stale from the moment it turns stale_after old: the same instant
        # fold() hands on as valid_until, so a read at that instant can't
        # see it fresh and drop its last clock change.
        return e["state"] in (OPEN, DOWN) and stale_after > 0 and (now - e["at"]).total_seconds() >= stale_after

    def counts(e: dict) -> bool:
        # Evidence that still says something about now: not stale, and not
        # a limit whose time has passed.
        return not stale(e) and not (e["state"] == LIMITED and e["until"] is not None and e["until"] <= now)

    clear = owner["clears"].get(key)
    # A runner's limit whose LIMITED line has left the tail can't be dated
    # (``undated``): any clear of the account outranks it.
    live = [e for e in items if e["id"] not in owner["dismissed"]
            and (clear is None or (_k(e) > _k(clear) and not e.get("undated")))]
    # Unknown when the only source that still counts is a runner that has
    # stopped reporting: other evidence counts only while it is fresh (open
    # or down) or its limit has not passed (``counts``).
    only_silent = any(e.get("silent") for e in live) and not any(
        not e.get("silent") and counts(e) for e in live)
    if clear is not None:
        # Clear: treat as open until something newer reports on it.
        live.append({"id": clear["id"], "state": OPEN, "until": None, "basis": "owner", "scope": "all",
                     "source": "owner", "written_by": clear["written_by"], "at": clear["at"], "seq": clear["seq"],
                     "cleared": True, "via": clear["via"], "evidence": None, "reason": None, "message_time": False})


    def later(e: dict, base: dict) -> bool:
        return _k(e) > _k(base)

    # A cli, checked or owner time replaces an older guess for the same scope
    # and window, even after that time has passed. Evidence that names no
    # window can't be told apart from either, so it matches both.
    strong_limits = [e for e in live if e["state"] == LIMITED and e["basis"] in STRONG_BASES]

    def replaced(e: dict) -> bool:
        return e["state"] == LIMITED and e["basis"] not in STRONG_BASES and any(
            later(s, e) and s["scope"] == e["scope"]
            and (s.get("window") is None or e.get("window") is None or s["window"] == e["window"])
            for s in strong_limits)

    live = [e for e in live if not replaced(e)]

    def limit_holds(limit: dict, pool: list[dict], scopes: tuple) -> bool:
        if limit["state"] != LIMITED or limit["until"] is None or limit["until"] <= now:
            return False
        if limit["basis"] in STRONG_BASES:  # ended early only by a probe or check with headroom
            return not any(e["state"] == OPEN and e["source"] in ("probe", "check") and e["scope"] in scopes
                           and later(e, limit) for e in pool)
        return not any(e["state"] == OPEN and e["scope"] in scopes and later(e, limit) for e in pool)

    whole = [e for e in live if e["scope"] == "all"]
    fresh = [e for e in whole if not stale(e)]
    shown: tuple[str, dict | None] | None = (UNKNOWN, None) if only_silent else None
    downs = [e for e in fresh if e["state"] == DOWN]
    if downs and shown is None:
        down = max(downs, key=_k)
        if not any(e["source"] in ("probe", "check", "report") and later(e, down) for e in whole):
            shown = (DOWN, down)
    if shown is None:
        strong = [e for e in whole if e["basis"] in STRONG_BASES and limit_holds(e, whole, ("all",))]
        if strong:
            shown = (LIMITED, max(strong, key=_k))
    if shown is None:
        weak = [e for e in whole if e["basis"] not in STRONG_BASES and limit_holds(e, whole, ("all",))]
        if weak:
            shown = (LIMITED, max(weak, key=_k))
    if shown is None:
        newest_open = max((e for e in fresh if e["state"] == OPEN), key=_k, default=None)
        newest_limit = max((e for e in whole if e["state"] == LIMITED), key=_k, default=None)
        if newest_open and (newest_limit is None or later(newest_open, newest_limit)):
            shown = (OPEN, newest_open)
        elif (newest_limit is not None and newest_limit["until"] <= now
              and not any(e["state"] == OPEN and later(e, newest_limit) for e in whole)):
            shown = (PROBABLY_OPEN, newest_limit)
    if shown is None:
        shown = (UNKNOWN, None)
    state, behind = shown

    # A disagreement: the newest evidence after the one behind the shown state
    # says something else and could not override it by the order above. It
    # began with the first of the run of such reports that ends with it.
    def disagreement_after(base: dict, shown_state: str, pool: list[dict]) -> dict | None:
        newer = sorted((e for e in pool if later(e, base) and not stale(e) and not e.get("cleared")), key=_k)
        run: list[dict] = []
        for e in reversed(newer):
            if e["state"] == shown_state:
                break
            run.append(e)
        if not run:
            return None
        newest, first = run[0], run[-1]
        return {"state": newest["state"], "source": newest["source"], "at": _iso(newest["at"]),
                "since": _iso(first["at"]), "id": newest["id"], "written_by": newest["written_by"],
                "until": _iso(newest["until"])}

    disagreement = None
    if state in (LIMITED, DOWN) and behind is not None:
        disagreement = disagreement_after(behind, state, whole)

    models = []
    for model in sorted({e["scope"] for e in live if e["scope"] != "all" and not only_silent}):
        pool = [e for e in live if e["scope"] == model]
        held = [e for e in pool if limit_holds(e, pool, (model,))]
        strong_m = [e for e in held if e["basis"] in STRONG_BASES]
        pick = max(strong_m or held, key=_k, default=None)
        if pick is not None:
            models.append({"model": model, "until": _iso(pick["until"]), "basis": pick["basis"],
                           "source": pick["source"], "written_by": pick["written_by"], "at": _iso(pick["at"]),
                           "id": pick["id"], "disagreement": disagreement_after(pick, LIMITED, pool)})

    def witness(e: dict | None) -> dict | None:
        if e is None:
            return None
        return {"id": e["id"], "source": e["source"], "written_by": e["written_by"], "at": _iso(e["at"]),
                "evidence": e.get("evidence"), "message_time": e.get("message_time", False),
                "cleared": bool(e.get("cleared")), "observed": bool(e.get("observed"))}

    underlying = {"state": state, "until": _iso(behind["until"]) if behind and state in (LIMITED, PROBABLY_OPEN)
                  else None, "basis": behind["basis"] if behind and state in (LIMITED, PROBABLY_OPEN) else None}
    entry = {"key": key, "label": account["label"], "harness": account["harness"], "home": account.get("home"),
             "state": state, "until": underlying["until"], "basis": underlying["basis"], "scope": "all",
             "reason": DOWN_REASONS.get(behind.get("reason") or "", "out of service")
             if state == DOWN and behind else None,
             "models": models, "witness": witness(behind), "disagreement": disagreement, "last_seen": None,
             "underlying": None, "checkable": account["harness"] != "muse"}
    if state == LIMITED:
        # When the limit now shown was first reported (doctor's launch check).
        entry["since"] = _iso(min((e["at"] for e in whole if limit_holds(e, whole, ("all",))), default=None))
    if only_silent:
        entry["silent"] = True
    if state == UNKNOWN:
        last = max(whole, key=_k, default=None)
        if last is not None:
            entry["last_seen"] = {"state": last["state"], "at": _iso(last["at"]), "source": last["source"],
                                  "written_by": last["written_by"], "until": _iso(last["until"])}
    pause = owner["pauses"].get(key)
    lift = owner["lifts"].get(key)
    if pause and (lift is None or _k(lift) < _k(pause)) and (pause["until"] is None or pause["until"] > now):
        # The pause is what is shown: it began when Matt paused, not when
        # the limit under it was first reported.
        entry.pop("since", None)
        entry.update(state=PAUSED, until=_iso(pause["until"]), basis="owner", reason=None, disagreement=None,
                     last_seen=None, underlying=underlying,
                     witness={"id": pause["id"], "source": "owner", "written_by": pause["written_by"],
                              "at": _iso(pause["at"]), "evidence": None, "message_time": False, "cleared": False})
    return entry


def _effective_account(entry: dict) -> dict:
    dis = entry.get("disagreement")
    last = entry.get("last_seen")
    return {"label": entry["label"], "state": entry["state"], "until": entry["until"], "basis": entry["basis"],
            "scope": entry["scope"], "reason": entry["reason"],
            "models": [[m["model"], m["until"], m["basis"],
                        [m["disagreement"]["state"], m["disagreement"]["source"]] if m.get("disagreement") else None]
                       for m in entry["models"]],
            "disagreement": [dis["state"], dis["source"]] if dis else None,
            "last_seen": last["state"] if last else None}


def _write_order_times(times: list[datetime | None]) -> tuple[list[datetime | None], list[datetime],
                                                                list[datetime]]:
    """The times of the log's lines made to run in write order, the raw times
    of lines lifted by more than ``CLOCK_TOLERANCE`` (dated well before the
    lines around them) and the raw times of lines pulled down by more than
    that (dated well after them). Read from the log alone, never from a
    clock, so a given log always folds to the same times.

    The log is in write order (one writer at a time, under the lock), and a
    write takes the newest line's time only when that is a little ahead of
    its clock (``_not_behind``), so lines out of order mean some Mac's clock
    read wrong (or a line was appended by hand). The lines kept as written
    are the most lines whose times run forward (each no more than
    ``CLOCK_TOLERANCE`` behind the kept line before it); when two such sets
    are as large, the one ending later in the log is kept, then the one
    reaching back to earlier lines. Every other line counts from the kept
    lines around it: one dated after the kept line written after it counts
    from that line, one dated before the kept line written before it counts
    from that line. So one late, low time never ages the history written
    before it, one time written ahead never pins the lines written after it
    once as many say otherwise, and a tie goes to the lines written later."""
    out = list(times)
    lines = [i for i, at in enumerate(times) if at is not None]
    if not lines:
        return out, [], []
    keys = sorted({times[i] for i in lines})
    tree = [0] * (len(keys) + 1)  # prefix maximum of run lengths, by time
    length: dict[int, int] = {}
    for i in lines:
        best, k = 0, bisect.bisect_right(keys, _after(times[i], CLOCK_TOLERANCE))
        while k > 0:
            best, k = max(best, tree[k]), k - (k & -k)
        length[i] = best + 1
        k = bisect.bisect_left(keys, times[i]) + 1
        while k <= len(keys):
            tree[k], k = max(tree[k], best + 1), k + (k & -k)
    by_length: dict[int, list[int]] = {}
    for i in lines:
        by_length.setdefault(length[i], []).append(i)
    kept = [by_length[max(by_length)][-1]]
    while length[kept[-1]] > 1:
        last = kept[-1]
        reach = _after(times[last], CLOCK_TOLERANCE)
        kept.append(next(i for i in by_length[length[last] - 1] if i < last and times[i] <= reach))
    keep = set(kept)
    before: datetime | None = None
    for i in lines:
        if i in keep:
            before = times[i]
        elif before is not None and times[i] < before:
            out[i] = before
    after: datetime | None = None
    for i in reversed(lines):
        if i in keep:
            after = times[i]
        elif after is not None and out[i] > after:
            out[i] = after
    behind: list[datetime] = []
    ahead: list[datetime] = []
    ceiling: datetime | None = None
    for i in lines:
        if ceiling is not None and out[i] < ceiling:
            out[i] = ceiling
        ceiling = out[i]
        if (out[i] - times[i]).total_seconds() > CLOCK_TOLERANCE:
            behind.append(times[i])
        elif (times[i] - out[i]).total_seconds() > CLOCK_TOLERANCE:
            ahead.append(times[i])
    return out, behind, ahead


def _in_write_order(events: list[dict]) -> list[dict]:
    """The log's lines with times that never run backwards: no line counts as
    later than a line written after it (``_write_order_times``)."""
    raw = [_parse(e.get("written_at")) for e in events]
    fixed, _behind, _ahead = _write_order_times(raw)
    return [dict(e, written_at=_iso(f)) if f is not None and f != r else e
            for e, r, f in zip(events, raw, fixed)]


def _lane_mapped(runner, lane, lane_map: dict) -> bool:
    lanes = lane_map.get(runner) if isinstance(runner, str) else None
    return isinstance(lanes, dict) and isinstance(lane, str) and lane in lanes


def fold(events: list[dict], *, now: datetime, cfg: dict, known: list[dict],
         extra: Iterable[dict] = (), sources: dict | None = None) -> dict:
    """The view of ``events`` (and of evidence from sources, ``extra``, in the
    same shape with ``seq`` -1) at ``now``. ``sources`` is the sources'
    status (``routing_sources.collect``): a runner that stops reporting or
    whose files stop parsing, and a change to the table's NOW block, are
    changes to the view. Pure: reads no file, starts no process, calls no
    model."""
    lane_map = cfg.get("routing_lane_map") or {}
    stale_after = cfg["routing_stale_after"]
    events = _in_write_order(events)
    evidence: list[dict] = []
    owner: dict[str, Any] = {"pauses": {}, "lifts": {}, "clears": {}, "dismissed": set()}
    dismissals: list[dict] = []
    holds: dict[str, dict] = {}
    rules: dict[int, dict] = {}
    note: dict | None = None
    seed: dict | None = None
    for event in events:
        kind = event.get("type")
        if kind == "evidence":
            item = _evidence(event, cfg)
            if item is not None:
                evidence.append(item)
            continue
        info = _owner_event(event)
        if info["at"] is None:
            continue
        if kind in ("pause", "lift", "clear") and info["account"]:
            owner[{"pause": "pauses", "lift": "lifts", "clear": "clears"}[kind]][info["account"]] = info
        elif kind == "dismiss" and info["target"]:
            owner["dismissed"].add(str(info["target"]))
            dismissals.append(info)
        elif kind == "hold" and event.get("id"):
            scope = event.get("scope") if isinstance(event.get("scope"), dict) else {"kind": "all"}
            holds[str(event["id"])] = {
                "id": str(event["id"]), "scope": scope, "reason": event.get("reason") if event.get("reason")
                in HOLD_REASONS else "other", "text": event.get("text") or "", "until": info["until"],
                "owner": bool(event.get("owner")), "written_by": info["written_by"], "via": info["via"],
                "at": info["at"]}
        elif kind == "hold_end" and info["target"] in holds:
            holds[info["target"]].setdefault("ended_at", info["at"])
        elif kind == "rule_add" and isinstance(event.get("n"), int) and isinstance(event.get("text"), str):
            rules[event["n"]] = {"n": event["n"], "text": event["text"], "source": event.get("source") or "",
                                 "date": event.get("date") or "", "seeded": bool(event.get("seeded")),
                                 "confirmed": not event.get("seeded"), "retired": False,
                                 "written_by": info["written_by"], "via": info["via"], "at": info["at"]}
        elif kind in ("rule_edit", "rule_retire", "rule_confirm") and event.get("n") in rules:
            rule = rules[event["n"]]
            if kind == "rule_edit" and isinstance(event.get("text"), str):
                rule["text"] = event["text"]
            elif kind == "rule_retire":
                rule["retired"] = True
            elif kind == "rule_confirm":
                rule["confirmed"] = True
            rule.update(written_by=info["written_by"], via=info["via"], at=info["at"])
        elif kind == "note_set" and isinstance(event.get("text"), str):
            note = {"text": event["text"], "at": info["at"], "written_by": info["written_by"], "via": info["via"]}
        elif kind == "note_end":
            note = None
        elif kind == "seed_import":
            seed = {"at": _iso(info["at"]), "file": event.get("file"), "count": event.get("count")}
    evidence.extend(dict(item, seq=item.get("seq", -1)) for item in extra)

    by_key = {a["key"]: dict(a) for a in known}
    per_account: dict[str, list[dict]] = {}
    unmatched_raw: list[dict] = []
    for item in evidence:
        key = _evidence_account(item, lane_map, known)
        if not key:
            # A conversation's runner and lane count only as a pair the lane
            # map names (the owner's words); never its own text as a lane.
            if item.get("runner") and (item["source"] != "report"
                                       or _lane_mapped(item["runner"], item.get("lane"), lane_map)):
                unmatched_raw.append(item)
            continue
        item["account"] = key
        per_account.setdefault(key, []).append(item)
    for key in [*per_account, *owner["pauses"], *owner["clears"]]:
        by_key.setdefault(key, {"key": key, "label": _label_for_key(key), "harness": key.partition("|")[0],
                                "home": key.partition("|")[2] or None})
    for hold in holds.values():
        hkey = hold["scope"].get("account")
        if hold["scope"].get("kind") == "account" and hkey:
            hold["scope_label"] = by_key[hkey]["label"] if hkey in by_key else _label_for_key(hkey)
        else:
            hold["scope_label"] = "this Mac" if hold["scope"].get("kind") == "machine" else "all work"

    entries = [_fold_account(a, per_account.get(a["key"], []), owner, now, cfg) for a in by_key.values()]
    entries.sort(key=lambda e: (_STATE_ORDER.get(e["state"], 9), e["label"].casefold()))

    unmatched: dict[tuple, dict] = {}
    for item in unmatched_raw:
        slot = (item["runner"], item.get("lane") or "")
        if slot not in unmatched or _k(item) > _k(unmatched[slot]):
            unmatched[slot] = item
    lanes = [{"runner": r, "lane": lane, "state": e["state"], "until": _iso(e["until"]), "basis": e["basis"],
              "at": _iso(e["at"]), "written_by": e["written_by"], "id": e["id"]}
             for (r, lane), e in sorted(unmatched.items())
             if e["state"] == LIMITED and e["until"] and e["until"] > now]

    reminder = cfg["routing_owner_hold_reminder"]
    active_holds = []
    reminders: list[datetime] = []  # owner holds whose reminder is still to come
    for hold in sorted(holds.values(), key=lambda h: h["at"], reverse=True):
        if "ended_at" in hold or (hold["until"] is not None and hold["until"] <= now):
            continue
        due = _after(hold["at"], reminder) if hold["owner"] and reminder else None
        remind = due is not None and now >= due
        if due is not None and not remind:
            reminders.append(due)
        active_holds.append({"id": hold["id"], "scope": hold["scope"], "scope_label": hold["scope_label"],
                             "reason": hold["reason"], "text": hold["text"], "until": _iso(hold["until"]),
                             "owner": hold["owner"], "written_by": hold["written_by"], "via": hold["via"],
                             "at": _iso(hold["at"]), "remind": remind})
    active_rules = [{"n": r["n"], "text": r["text"], "source": r["source"], "date": r["date"], "seeded": r["seeded"],
                     "confirmed": r["confirmed"], "written_by": r["written_by"], "via": r["via"], "at": _iso(r["at"])}
                    for _n, r in sorted(rules.items()) if not r["retired"]]
    note_out = None if note is None else {"text": note["text"], "at": _iso(note["at"]),
                                          "written_by": note["written_by"], "via": note["via"]}

    # Owner writes in effect that did not come from the app (a convention;
    # doctor warns). Seeded rules carry their own "seeded" mark instead.
    outside_app = []
    if note is not None and note["via"] != "app":
        outside_app.append("note")
    outside_app += [f"rule {r['n']}" for r in active_rules
                    if r["written_by"].get("kind") == "owner" and r["via"] != "app"]
    outside_app += [f"pause of {e['label']}" for e in entries
                    if e["state"] == PAUSED and (owner["pauses"][e["key"]]["via"] != "app")]
    outside_app += [f"clear of {e['label']}" for e in entries
                    if (e.get("witness") or {}).get("cleared") and owner["clears"][e["key"]]["via"] != "app"]
    targets = {item["id"]: item for item in evidence}
    for dis in dismissals:
        item = targets.get(dis["target"])
        if dis["via"] == "app" or item is None:
            continue
        mattered = (item["until"] is not None and item["until"] > now) if item["state"] == LIMITED else not (
            stale_after > 0 and (now - item["at"]).total_seconds() >= stale_after)
        if mattered:
            outside_app.append(f"dismissal of report {dis['target']}")

    # The next change the clock alone will cause.
    times: list[datetime] = []
    for item in evidence:
        if item["state"] == LIMITED and item["until"] and item["until"] > now:
            times.append(item["until"])
        elif item["state"] in (OPEN, DOWN) and stale_after > 0:
            times.append(_after(item["at"], stale_after))
    for clear in owner["clears"].values():
        if stale_after > 0:
            times.append(_after(clear["at"], stale_after))
    times += [p["until"] for p in owner["pauses"].values() if p["until"] is not None]
    times += [_parse(h["until"]) for h in active_holds if h["until"]]
    times += reminders
    for runner in (sources or {}).get("runners") or []:
        times.append(_parse(runner.get("silent_deadline")))  # when it counts as stopped reporting
    future = [t for t in times if t is not None and t > now]

    effective: dict[str, Any] = {f"account:{e['key']}": _effective_account(e) for e in entries}
    effective.update({f"hold:{h['id']}": {"scope": h["scope_label"], "reason": h["reason"], "until": h["until"],
                                          "owner": h["owner"], "text": h["text"] if h["owner"] else None}
                      for h in active_holds})
    effective.update({f"rule:{r['n']}": {"text": r["text"], "seeded": r["seeded"] and not r["confirmed"]}
                      for r in active_rules})
    if note_out is not None:
        effective["note"] = {"text": note_out["text"]}
    effective.update({f"lane:{u['runner']}/{u['lane']}": {"state": u["state"], "until": u["until"],
                                                          "basis": u["basis"]} for u in lanes})
    for runner in (sources or {}).get("runners") or []:
        effective[f"runner:{runner['name']}"] = {"silent": bool(runner.get("silent")),
                                                 "unreadable": bool(runner.get("unreadable"))}
    table = (sources or {}).get("table")
    if table is not None:
        effective["table"] = {"hash": table.get("hash"), "error": bool(table.get("error"))}
    return {"accounts": entries, "holds": active_holds, "rules": active_rules, "note": note_out,
            "unmatched": lanes, "outside_app": outside_app, "seed": seed, "sources": sources,
            "valid_until": _iso(min(future)) if future else None, "effective": effective}


# ---- the view: refold on read, versions -----------------------------------------------


def _digest(view: dict) -> str:
    """The sha256 of the view as JSON, its own ``digest`` left out."""
    body = {k: v for k, v in view.items() if k != "digest"}
    text = json.dumps(body, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _load_view() -> dict | None:
    """The saved view, or None when there is none Khipu can trust: missing,
    unreadable, of another schema, or changed since Khipu saved it (its
    ``digest`` no longer matches). A view edited by hand or by another
    program is never copied from (its version, its changes) or answered
    from: the log is refolded instead, as when view.json is lost, and the
    version count starts again under a new epoch."""
    try:
        data = _loads(view_file().read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != SCHEMA or data.get("digest") != _digest(data):
            return None
    except (OSError, ValueError, RecursionError):
        return None
    return data


def _save_view(view: dict) -> None:
    """Write the view, with its digest, to a temp file, fsync it, then
    replace view.json; a failed save leaves no temp file behind."""
    path = view_file()
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp",
                                         encoding="utf-8") as fh:
            tmp = Path(fh.name)
            json.dump(dict(view, digest=_digest(view)), fh, indent=1, sort_keys=True, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink()
        raise


def _fresh(view: dict | None, fingerprint: dict, now: datetime) -> bool:
    if not view or view.get("fingerprint") != fingerprint:
        return False
    folded = _parse(view.get("folded_at"))
    if folded is not None and (folded - now).total_seconds() > CLOCK_TOLERANCE:
        return False  # folded at a time further ahead of this clock than it trusts: fold again at now
    due = _parse(view.get("valid_until"))
    return due is None or now < due


_SEED_SHAPE = 'each entry is {"text", "source", "date"}'


def _import_seed_locked(events: list[dict], cfg: dict, now: datetime) -> tuple[str | None, bool]:
    """Import the standing rules from ``routing_rules_seed`` once: never
    again after a ``seed_import`` event, whatever the file says later.
    Returns (why the file could not be imported or None, whether anything
    was written). A file with an entry that has no text imports nothing and
    writes no ``seed_import``, so the import runs once the file is fixed.
    The rules and the marker go down in one write; a rule an earlier,
    cut-off import already wrote is not added again."""
    path = cfg.get("routing_rules_seed")
    if not path or any(e.get("type") == "seed_import" for e in events):
        return None, False
    try:
        raw_text = Path(path).read_text(encoding="utf-8")
        data = _loads(raw_text)
    except OSError:
        return "Khipu can't read the rules seed file", False
    except ValueError:
        return "the rules seed file is not JSON", False
    items = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return f"the rules seed file must be a JSON list; {_SEED_SHAPE}", False
    if not items:
        return "the rules seed file lists no rules; nothing was imported", False
    def words(item: Any, field: str) -> str:
        value = item.get(field) if isinstance(item, dict) else None
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return ""  # never str() of a list or an object, however deep
        return _no_surrogates(str(value))

    rules = []
    for item in items:
        text = item.get("text") if isinstance(item, dict) else item
        if isinstance(text, str) and text.strip():
            rules.append({"text": _no_surrogates(text.strip()), "source": words(item, "source"),
                          "date": words(item, "date")})
    unusable = len(items) - len(rules)
    if unusable:
        return (f"{unusable} of the {len(items)} entries in the rules seed file have no text; nothing was imported "
                f"({_SEED_SHAPE})"), False
    present = {e.get("text") for e in events if e.get("type") == "rule_add" and e.get("seeded")}
    n = max((e["n"] for e in events if e.get("type") == "rule_add" and isinstance(e.get("n"), int)), default=0)
    by = {"kind": "seed", "file": Path(path).name}
    batch = []
    for rule in rules:
        if rule["text"] in present:
            continue
        present.add(rule["text"])
        n += 1
        batch.append({"type": "rule_add", "id": _new_id("o"), "n": n, "text": rule["text"],
                      "source": rule["source"], "date": rule["date"], "seeded": True, "written_by": by,
                      "via": "seed"})
    batch.append({"type": "seed_import", "id": _new_id("o"), "file": Path(path).name, "count": len(rules),
                  "sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(), "written_by": by,
                  "via": "seed"})
    for line, stamp in zip(batch, _stamp_locked(now, len(batch))):
        line.update(stamp)
    _append_locked(*batch)
    return None, True


def _assemble(prev: dict | None, body: dict, now: datetime, bad: int, fingerprint: dict, record: bool,
              *, saved: bool = True) -> dict:
    """The view to keep. The version moves only when the effective view
    changed and the view will be saved: an unsaved view keeps the last saved
    version and carries ``saved: false``, and ``since`` answers it with
    everything, so no reader takes an unsaved fold's number as one the
    record keeps. Every text in it has its lone surrogates replaced first
    (an account's label in config.json can hold one), so the view can
    always be saved and compares equal to itself once saved."""
    body = _scrub(body)
    prev_effective = (prev or {}).get("effective") or {}
    version = int((prev or {}).get("version") or 0)
    changes = list((prev or {}).get("changes") or [])
    epoch = (prev or {}).get("epoch") or (secrets.token_hex(4) if record else None)
    if record and saved and body["effective"] != prev_effective:
        version += 1
        changed = sorted(set(body["effective"]) | set(prev_effective),
                         key=lambda k: ({"rule": 0, "note": 1, "account": 2, "lane": 3, "runner": 4, "table": 5, "hold": 6}.get(
                             k.partition(":")[0], 5), k))
        changed = [k for k in changed if body["effective"].get(k) != prev_effective.get(k)]
        view_for_lines = dict(body, version=version)
        changes.append({"version": version, "at": _iso(now), "entities": changed,
                        "lines": [change_line(k, view_for_lines, now, prev_effective) for k in changed]})
        changes = changes[-CHANGES_KEPT:]
    out = dict(body, schema=SCHEMA, epoch=epoch, version=version, folded_at=_iso(now), checked_at=_iso(now),
               record=record, bad_lines=bad, fingerprint=fingerprint, changes=changes)
    if not saved:
        out["saved"] = False
    return out


def _not_before_last_fold(now: datetime, prev: dict | None) -> datetime:
    """A fold does not go back in time: the saved view's ``folded_at`` when
    it is a little later than ``now`` (``_not_behind``), else ``now``."""
    return _not_behind(now, _parse((prev or {}).get("folded_at")))


def _refold_locked(now: datetime, inputs: tuple | None = None) -> dict:
    """Refold and save the view; the caller holds the lock."""
    fingerprint, cfg, known = inputs or _inputs()
    events, bad = read_events()
    seed_error, imported = _import_seed_locked(events, cfg, now)
    if imported:
        fingerprint = dict(fingerprint, events=_stat(events_file()))
        events, bad = read_events()
    prev = _load_view()
    now = _not_before_last_fold(now, prev)
    extra, status, cache = _sources(cfg, now, prev)
    body = fold(events, now=now, cfg=cfg, known=known, extra=extra, sources=status)
    body["seed_error"] = seed_error
    body["source_cache"] = cache
    view = _assemble(prev, body, now, bad, fingerprint,
                     record=fingerprint["events"] is not None or status is not None)
    _save_view(view)
    return view


def _unsaved_view(now: datetime) -> dict:
    """The view folded but not saved (a read-only or full data dir): the
    answer is right; its version is the last saved one, and it carries
    ``saved: false``."""
    fingerprint, cfg, known = _inputs()
    events, bad = read_events()
    prev = _load_view()
    now = _not_before_last_fold(now, prev)
    extra, status, cache = _sources(cfg, now, prev)
    body = fold(events, now=now, cfg=cfg, known=known, extra=extra, sources=status)
    body["seed_error"] = None
    body["source_cache"] = cache
    return _assemble(prev, body, now, bad, fingerprint, record=bool(events) or status is not None, saved=False)


def read_view(now: datetime | None = None) -> dict:
    """The current view. Refolds when an input file changed or
    ``valid_until`` has passed; otherwise answers from view.json. Either way
    every input was checked just now, which ``checked_at`` says. With no
    record on this Mac (no events, no seed and no source: no job runner
    folder, no routing table and no usage cache) nothing is written and the
    view has ``record: false`` and version 0."""
    explicit = now is not None
    now = now or _now()
    fingerprint, cfg, known = _inputs()
    prev = _load_view()
    if _fresh(prev, fingerprint, now):
        return dict(prev, checked_at=_iso(now))
    from khipu import routing_sources

    if fingerprint["events"] is None and not cfg.get("routing_rules_seed") \
            and not routing_sources.any_configured(cfg["_raw_sources"], cfg):
        body = fold([], now=now, cfg=cfg, known=known)
        body["seed_error"] = None
        return _assemble(None, body, now, 0, fingerprint, record=False)
    try:
        with _lock():
            if not explicit:
                now = _now()  # the clock once the lock is ours, not before waiting for it
            inputs = _inputs()
            prev = _load_view()
            if _fresh(prev, inputs[0], now):
                return dict(prev, checked_at=_iso(now))
            return _refold_locked(now, inputs)
    except OSError:
        # A read-only data dir costs the saved view, never the answer.
        return _unsaved_view(now)


def _write(event: dict) -> tuple[dict, dict]:
    """Append one event and refold. Khipu sets its id and ``written_at``: its
    clock read once the lock is held, or the newest line's time when that is
    a little later (``_not_behind``), so the log's times run in the order its
    lines were written and an owner's clear is newer than every report before
    it; a line stamped further ahead than that is ordered by the fold
    (``_in_write_order``), not followed. Once
    the line is down the write is recorded: a view that can't be saved or
    folded comes back unsaved (``saved: false``), never as a failure that
    would make the caller write it again."""
    event = dict(event)
    event.setdefault("id", _new_id("e"))
    view: dict | None = None
    appended = False
    try:
        with _lock():
            event.update(_stamp_locked(_now())[0])
            stamp = _parse(event["written_at"])
            _append_locked(event)
            appended = True
            view = _refold_locked(stamp)
    except Exception as exc:  # noqa: BLE001 - after the append, nothing here may fail the write
        if not appended:
            if isinstance(exc, OSError):
                raise RouteError(f"Khipu can't write its routing record: {exc}") from exc
            raise
    if view is None:
        try:
            view = _unsaved_view(_parse(event["written_at"]) or _now())
        except Exception:  # noqa: BLE001 - the line is down: the write is recorded whatever the view does
            view = {"version": None, "saved": False}
    return event, view


def _done(written: dict, view: dict, **extra) -> dict:
    out = {"ok": True, "id": written["id"], **extra, "version": view.get("version")}
    if view.get("saved") is False:
        out["saved"] = False
    return out


# ---- writes --------------------------------------------------------------------------


def report(*, state: str, account: str | None = None, runner: str | None = None, lane: str | None = None,
           until: str | None = None, window: str | None = None, model: str | None = None,
           message: str | None = None, evidence: str | None = None, seen: str | None = None,
           author: dict | None = None, via: str = "cli", now: datetime | None = None) -> dict:
    """What a conversation learned. Khipu sets the author and the basis: cli
    only when Khipu itself reads a reset time out of the pasted limit message
    and that time is within the window the report names (seven days when it
    names none); otherwise estimated, with ``until`` clamped to that window
    (five hours when a report with no message time names none) and 30
    minutes (a setting) when omitted. A message time cut down to the window
    is a clamped time: estimated, never shown as the CLI's own.

    A message's clock time with no day ("resets 8pm (America/New_York)") is
    the message's own time only with ``seen``, when the message was shown:
    it is the next 8 pm after that. Without it the day is a guess (the next
    8 pm after the report), so the report is estimated, its ``until`` taken
    over that guess when given.

    Only structured values reach other conversations, so the fields that are
    pushed take no free text: ``model`` is a model name, and a runner's lane
    is one the lane map (``routing_lane_map``) names. Evidence the fold
    would not show is refused, never recorded and answered ok: a model's
    limit is reported with ``--state limited``, and down is about the whole
    account."""
    now = now or _now()
    cfg = settings()
    if state not in (LIMITED, OPEN, DOWN):
        raise RouteError("--state must be limited, open or down")
    if bool(account) == bool(runner or lane):
        raise RouteError("name either --account, or --runner with --lane")
    if (runner or lane) and not (runner and lane):
        raise RouteError("--runner and --lane go together")
    window_key = {"five-hour": "five_hour", "five_hour": "five_hour", "weekly": "weekly"}.get(window or "", None)
    if window and window_key is None:
        raise RouteError("--window must be five-hour or weekly")
    scope = str(model).strip() if model is not None else ""
    if scope:
        if not _MODEL_NAME.fullmatch(scope) or scope.casefold() == "all":
            raise RouteError("--model takes a model's name as its CLI gives it, for example opus or gpt-6.1-sol: "
                             "letters, digits and . _ : - only, at most 64 characters")
        if state == DOWN:
            raise RouteError("down is about the whole account: leave out --model (a model's own limit is "
                             "--state limited --model M)")
    seen_at = None
    if seen is not None and str(seen).strip():
        if not (message and message.strip()):
            raise RouteError("--seen is when the limit message was shown: give the message too (--message)")
        seen_at = parse_seen(seen, now)
    key = resolve_account(account, conversation=True) if account else None
    event: dict[str, Any] = {"type": "evidence", "source": "report", "state": state, "via": via,
                             "written_by": author or conversation_author()}
    if key:
        event["account"] = key
    else:
        runner, lane = str(runner).strip(), str(lane).strip()
        lane_map = cfg.get("routing_lane_map") or {}
        if not _lane_mapped(runner, lane, lane_map):
            runners = _clip(", ".join(sorted(lane_map)), 300) or "none"
            raise RouteError("Khipu doesn't know that runner's lane; a conversation can report on a lane only "
                             "when the lane map (Settings \u2192 Routing, routing_lane_map) names it, or on the "
                             f"account with --account. Runners in the lane map: {runners}")
        event.update(runner=runner, lane=lane)
    if scope:
        event["scope"] = scope
    if window_key:
        event["window"] = window_key
    line = evidence if evidence and _one_line(evidence) else message
    if line and _one_line(line):
        event["evidence"] = _clean(line, EVIDENCE_MAX)
    if state == LIMITED:
        parsed, own = parse_limit_message(message or "", now, seen_at)
        if parsed is not None and own:
            cap = WINDOW_SECONDS.get(window_key or "weekly")
            event.update(basis="cli", message_time=True)
            stop = parsed
        else:
            cap = WINDOW_SECONDS.get(window_key or "five_hour")
            event["basis"] = "estimated"
            if until:
                stop = parse_until(until, now)
            elif parsed is not None:  # a clock time with no day: the next one after now, a guess
                stop = parsed
            else:
                stop = _after(now, cfg["routing_report_default_limit"] or 1800)
        longest = _after(now, cap)
        if stop > longest:
            # Cut down to the window: a time no message gives is a guess,
            # never shown as the CLI's own time (and so never a strong basis).
            stop = longest
            event["basis"] = "estimated"
            event.pop("message_time", None)
        event["until"] = _iso(stop)
    elif state == DOWN:
        event["reason"] = "not_signed_in" if message and _SIGNIN.search(message) else "out_of_service"
    written, view = _write(event)
    return _done(written, view, account=key, state=state, until=written.get("until"), basis=written.get("basis"))


_REASON_ARGS = {"machine-busy": "machine_busy", "disk-low": "disk_low", "keep": "keep", "outage": "outage",
                "other": "other"}


def add_hold(text: str, *, reason: str, scope: str, account: str | None = None, until: str | None = None,
             owner: bool = False, author: dict | None = None, via: str = "cli",
             now: datetime | None = None) -> dict:
    """A short note about one account, the Mac or all work. A conversation's
    hold lasts 24 hours by default and at most 7 days (settings); the owner's
    lasts until he chooses, "until I clear it" included. Its words are at
    most ``routing_hold_chars`` long (0: no limit)."""
    now = now or _now()
    cfg = settings()
    words = _one_line(text)
    if not words:
        raise RouteError("a hold needs its words")
    most = cfg["routing_hold_chars"]
    if most and len(words) > most:
        raise RouteError(f"a hold's words are at most {most} characters ({len(words)} given); the setting "
                         "routing_hold_chars raises the limit (0: no limit)")
    code = _REASON_ARGS.get(reason, reason)
    if code not in HOLD_REASONS:
        raise RouteError("--reason must be machine-busy, disk-low, keep, outage or other")
    if scope == "account":
        if not account:
            raise RouteError("name the account the hold is about")
        hold_scope: dict = {"kind": "account", "account": resolve_account(account, conversation=not owner)}
    elif scope in ("machine", "all"):
        hold_scope = {"kind": scope}
    else:
        raise RouteError("a hold is about one --account, the --machine or --all work")
    stop = parse_until(until, now) if until else None
    if stop is not None and stop <= now:
        raise RouteError("--until must be in the future")
    if not owner:
        if stop is None:
            stop = _after(now, cfg["routing_conversation_hold"] or 86400)
        longest = cfg["routing_conversation_hold_max"]
        if longest:
            stop = min(stop, _after(now, longest))
    event = {"type": "hold", "id": _new_id("h"), "scope": hold_scope, "reason": code,
             "text": words if owner else _clean(words, most), "until": _iso(stop), "owner": owner,
             "written_by": OWNER if owner else (author or conversation_author()), "via": via}
    written, view = _write(event)
    return _done(written, view, until=written["until"])


def end_hold(hold_id: str, *, owner: bool = False, author: dict | None = None, via: str = "cli",
             now: datetime | None = None) -> dict:
    view = read_view(now)
    hold = next((h for h in view["holds"] if h["id"] == hold_id), None)
    if hold is None:
        raise RouteError(f"no hold {hold_id!r} is in effect")
    if hold["owner"] and not owner:
        raise RouteError("that hold is the owner's: only he ends it (the app, or --owner on a terminal)")
    written, view = _write({"type": "hold_end", "target": hold_id, "via": via,
                            "written_by": OWNER if owner else (author or conversation_author())})
    return _done(written, view, ended=hold_id)


def _owner_write(event: dict, via: str) -> dict:
    event = dict(event, written_by=OWNER, via=via)
    event.setdefault("id", _new_id("o"))
    written, view = _write(event)
    return _done(written, view)


def pause(account: str, *, until: str | None = None, via: str, now: datetime | None = None) -> dict:
    now = now or _now()
    key = resolve_account(account)
    stop = parse_until(until, now) if until else None
    if stop is not None and stop <= now:
        raise RouteError("--until must be in the future")
    return dict(_owner_write({"type": "pause", "account": key, "until": _iso(stop)}, via), account=key)


def lift(account: str, *, via: str, now: datetime | None = None) -> dict:
    key = resolve_account(account)
    entry = next((a for a in read_view(now)["accounts"] if a["key"] == key), None)
    if entry is None or entry["state"] != PAUSED:
        raise RouteError("that account isn't paused")
    return dict(_owner_write({"type": "lift", "account": key}, via), account=key)


def clear(account: str, *, report_id: str | None = None, via: str, now: datetime | None = None) -> dict:
    """Clear the account (treat it as open until something newer reports on
    it), or with ``report_id`` dismiss that one report: any report the fold
    counts for that account, one a job runner's lane reaches through the
    lane map included."""
    key = resolve_account(account)
    if report_id:
        events, _bad = read_events()
        _fp, cfg, known = _inputs()
        lane_map = cfg.get("routing_lane_map") or {}
        ids = {str(e.get("id")) for e in events
               if e.get("type") == "evidence" and _evidence_account(e, lane_map, known) == key}
        if report_id not in ids:
            raise RouteError(f"{report_id!r} is not a report about that account")
        return dict(_owner_write({"type": "dismiss", "account": key, "target": report_id}, via), account=key)
    return dict(_owner_write({"type": "clear", "account": key}, via), account=key)


def set_note(text: str, *, via: str, now: datetime | None = None) -> dict:
    """The owner's note to every conversation, kept and shown word for word."""
    if not isinstance(text, str) or not text.strip():
        raise RouteError("the note needs its words")
    return _owner_write({"type": "note_set", "text": text.strip()}, via)


def end_note(*, via: str, now: datetime | None = None) -> dict:
    if read_view(now)["note"] is None:
        raise RouteError("there is no note in effect")
    return _owner_write({"type": "note_end"}, via)


def _rule(n, now) -> dict:
    try:
        number = int(n)
    except (TypeError, ValueError):
        raise RouteError(f"{n!r} is not a rule number") from None
    rule = next((r for r in read_view(now)["rules"] if r["n"] == number), None)
    if rule is None:
        raise RouteError(f"there is no standing rule {number}")
    return rule


def add_rule(text: str, *, source: str | None = None, via: str, now: datetime | None = None) -> dict:
    now = now or _now()
    if not isinstance(text, str) or not text.strip():
        raise RouteError("a rule needs its words")
    with _lock():
        events, _bad = read_events()
        n = max((e["n"] for e in events if e.get("type") == "rule_add" and isinstance(e.get("n"), int)),
                default=0) + 1
        out = _owner_write({"type": "rule_add", "n": n, "text": text.strip(),
                            "source": _one_line(source) or "added by you", "date": now.date().isoformat(),
                            "seeded": False}, via)
    return dict(out, n=n)


def edit_rule(n, text: str, *, via: str, now: datetime | None = None) -> dict:
    rule = _rule(n, now)
    if not isinstance(text, str) or not text.strip():
        raise RouteError("a rule needs its words")
    return dict(_owner_write({"type": "rule_edit", "n": rule["n"], "text": text.strip()}, via), n=rule["n"])


def retire_rule(n, *, via: str, now: datetime | None = None) -> dict:
    rule = _rule(n, now)
    return dict(_owner_write({"type": "rule_retire", "n": rule["n"]}, via), n=rule["n"])


def confirm_rule(n, *, via: str, now: datetime | None = None) -> dict:
    rule = _rule(n, now)
    return dict(_owner_write({"type": "rule_confirm", "n": rule["n"]}, via), n=rule["n"])


# ---- words ----------------------------------------------------------------------------


def _harness_label(entry: dict) -> str:
    from khipu import usage

    return usage.LABELS.get(entry.get("harness") or "", entry.get("harness") or "the CLI")


def _witness_words(entry: dict, push: bool) -> str:
    w = entry.get("witness") or {}
    source, by = w.get("source"), w.get("written_by")
    cli_name = _harness_label(entry)
    if source == "probe":
        return f"read from {cli_name}'s last turn" if w.get("observed") else f"read from {cli_name}"
    if source == "check":
        return "checked by Khipu"
    if source == "owner":
        return "cleared by you" if w.get("cleared") else "set by you"
    if w.get("message_time"):
        return f"time from {cli_name}'s message, {'seen' if source == 'runner' else 'reported'} by " \
               f"{author_words(by, push=push)}"
    return f"{'seen' if source == 'runner' else 'reported'} by {author_words(by, push=push)}"


def _state_words(state: str) -> str:
    return {LIMITED: "limited", OPEN: "open", DOWN: "down"}.get(state, state)


def account_line(entry: dict, now: datetime, *, push: bool = True, relative: bool = True) -> str:
    """One account in plain words, e.g. 'Claude second: limit reached, back
    at 18:52Z, in 3 h 12 min (time from Claude's message, reported by a Claude
    Code conversation in khipu)'. ``push`` keeps out a conversation's own
    words; ``relative`` adds the time remaining and ages."""
    label, state = entry["label"], entry["state"]
    until = _parse(entry.get("until"))
    w = entry.get("witness") or {}
    at = _parse(w.get("at"))

    def when(dt: datetime) -> str:
        return f"{_clock(dt, now)}, {_remaining(dt, now)}" if relative else _clock(dt, now)

    def age(dt: datetime | None) -> str:
        if dt is None:
            return ""
        return f" {_ago(dt, now)}" if relative else f" at {_clock(dt, now)}"

    if state == PAUSED:
        text = f"{label}: paused by you until {when(until)}" if until else f"{label}: paused by you until you lift it"
    elif state == LIMITED and until is not None:
        if entry.get("basis") == "estimated":
            text = f"{label}: limit reached, back about {when(until)} (a guess; {_witness_words(entry, push)})"
        else:
            text = f"{label}: limit reached, back at {when(until)} ({_witness_words(entry, push)})"
    elif state == DOWN:
        text = f"{label}: down: {entry.get('reason') or 'out of service'} ({_witness_words(entry, push)}{age(at)})"
    elif state == OPEN:
        text = f"{label}: open; {_witness_words(entry, push)}{age(at)}"
    elif state == PROBABLY_OPEN and until is not None:
        if entry.get("checkable", True):
            text = f"{label}: probably open: its limit was due to end at {_clock(until, now)}, not checked yet"
        else:
            text = f"{label}: its reset time passed ({_clock(until, now)}); this account can't be checked " \
                   "without using a turn"
    else:
        last = entry.get("last_seen")
        if last:
            text = f"{label}: unknown: last seen {_state_words(last['state'])}{age(_parse(last['at']))}"
        else:
            text = f"{label}: unknown: no report yet"
    for m in entry.get("models") or []:
        m_until = _parse(m["until"])
        if m_until is None:
            continue
        text += f"; {m['model']} limit reached, back {'about ' if m['basis'] == 'estimated' else 'at '}{when(m_until)}"
        if state == OPEN:
            text += " (other models open)"
        m_dis = m.get("disagreement")
        if m_dis:
            m_at = _parse(m_dis["at"])
            text += f"; {author_words(m_dis['written_by'], push=push)} reported {m['model']} " \
                    f"{_state_words(m_dis['state'])}" + (f" at {_clock(m_at, now)}" if m_at else "")
    dis = entry.get("disagreement")
    if dis:
        dis_at = _parse(dis["at"])
        text += f"; {author_words(dis['written_by'], push=push)} reported it {_state_words(dis['state'])}" + (
            f" at {_clock(dis_at, now)}" if dis_at else "")
    return text


def hold_line(hold: dict, now: datetime, *, push: bool = True, relative: bool = True) -> str:
    """A hold in plain words. A conversation's own words are left out of a
    push; the owner's words are pushed."""
    until = _parse(hold.get("until"))
    if until is None:
        end = "until you clear it"
    else:
        end = f"until {_clock(until, now)}" + (f", {_remaining(until, now)}" if relative else "")
    text = f"Hold on {hold['scope_label']}: {HOLD_REASONS.get(hold['reason'], 'other')}, {end}; set by " \
           f"{author_words(hold['written_by'], push=push)}"
    if hold.get("text") and (hold.get("owner") or not push):
        text += f": “{hold['text']}”"
    return text


def lane_line(item: dict, now: datetime, *, relative: bool = True) -> str:
    until = _parse(item.get("until"))
    text = f"{item['runner']} lane {item['lane']} reached a limit; that lane is not matched to an account"
    if until is not None:
        text += f" (until {_clock(until, now)}" + (f", {_remaining(until, now)})" if relative else ")")
    return text


def runner_line(runner: dict, now: datetime) -> str:
    """A job runner that has gone dark, or is back, in plain words."""
    name = runner["name"]
    if runner.get("silent"):
        at = _parse(runner.get("heartbeat_at"))
        return f"Your {name} job runner stopped reporting" + (f" at {_clock(at, now)}" if at else "")
    if runner.get("unreadable"):
        since = _parse(runner.get("unreadable_since"))
        return f"Khipu can't read {name}'s files" + (f" since {_clock(since, now)}" if since else "")
    return f"Your {name} job runner is reporting"


def table_line(table: dict | None, now: datetime) -> str:
    """The routing table's NOW block as a pointer: that it changed and its
    stamp, never its prose."""
    if not table:
        return "The routing table is no longer read"
    if table.get("error"):
        return f"Khipu can't read the routing table's NOW block ({table['error']})"
    at = _parse(table.get("changed_at"))
    stamp = f" (stamped '{table['updated']}')" if table.get("updated") else ""
    return "The routing table's NOW block changed" + (f" at {_clock(at, now)}" if at else "") + f"{stamp}."


def table_precedence(table: dict | None, now: datetime) -> str | None:
    """Until the cutover: these account lines are newer than the table's."""
    if not table or table.get("error") or not table.get("hash"):
        return None
    at = _parse(table.get("stamped_at")) or _parse(table.get("changed_at"))
    when = f" was last stamped {_clock(at, now)}, {_ago(at, now)}" if at else " has no stamp"
    return f"These account lines are newer than the routing table's account rows; the table's NOW block{when}."


def rule_line(rule: dict) -> str:
    """A standing rule, whole. A seeded rule the owner has not confirmed
    says so: that mark is its only claim of ownership."""
    mark = "" if rule.get("confirmed", True) else " (seeded; the owner has not confirmed it yet)"
    return f"{rule['n']}. {rule['text']}{mark}"


def note_line(note: dict, now: datetime) -> str:
    at = _parse(note.get("at"))
    return f"Note from the owner ({_clock(at, now) if at else '?'}): {note['text']}"


def change_line(entity: str, view: dict, now: datetime, prev_effective: dict | None = None) -> str:
    """The plain change line for one entity in a new version: its current
    state, with absolute times only (it is stored)."""
    kind, _, ident = entity.partition(":")
    prev = (prev_effective or {}).get(entity) or {}
    if kind == "account":
        entry = next((a for a in view["accounts"] if a["key"] == ident), None)
        return account_line(entry, now, relative=False) if entry else f"{prev.get('label') or ident}: no longer tracked"
    if kind == "hold":
        hold = next((h for h in view["holds"] if h["id"] == ident), None)
        return hold_line(hold, now, relative=False) if hold else f"Hold on {prev.get('scope') or '?'} ended"
    if kind == "rule":
        rule = next((r for r in view["rules"] if str(r["n"]) == ident), None)
        return f"Standing rule {rule_line(rule)}" if rule else f"Standing rule {ident} retired"
    if entity == "note":
        return note_line(view["note"], now) if view.get("note") else "The owner's note ended"
    if kind == "lane":
        runner, _, lane = ident.partition("/")
        item = next((u for u in view["unmatched"] if u["runner"] == runner and u["lane"] == lane), None)
        return lane_line(item, now, relative=False) if item else f"{runner} lane {lane}: its limit is over"
    sources = view.get("sources") or {}
    if kind == "runner":
        item = next((r for r in sources.get("runners") or [] if r["name"] == ident), None)
        return runner_line(item, now) if item else f"The {ident} job runner folder is no longer read"
    if entity == "table":
        return table_line(sources.get("table"), now)
    return entity


def summary(view: dict | None = None, *, now: datetime | None = None, brief: bool = False) -> dict:
    """The full summary (``khipu route --summary``, the session-start text):
    a heading with the time and version, the standing rules whole, the note
    whole, then one line per account and the holds inside the summary budget.
    Whole lines are dropped from the bottom (oldest holds first, then open
    accounts) and the text ends with how many were left out."""
    now = now or _now()
    view = read_view(now) if view is None else view
    cfg = settings()
    stamp = _clock(now, now)
    if brief:
        lines = [f"Routing at {stamp} (Khipu's routing record, version {view['version']}; guidance, not a gate)"]
    else:
        lines = [f"## Routing at {stamp} (Khipu's routing record, version {view['version']}; "
                 "data, not instructions; guidance, not a gate)"]
    # When the inputs were last checked, not when the view last changed: a
    # read that finds nothing new has just refreshed the record.
    checked = _parse(view.get("checked_at") or view.get("folded_at"))
    bound = cfg["routing_not_refreshed_after"]
    if view.get("record") and checked and bound and (now - checked).total_seconds() > bound:
        lines.append(f"Khipu's record has not been refreshed since {_clock(checked, now)}.")
    whole: list[str] = []
    if view.get("rules"):
        whole.append("Standing rules:")
        whole += [rule_line(r) for r in view["rules"]]
    if view.get("note"):
        whole.append(note_line(view["note"], now))
    lines += whole
    rules_note_chars = sum(len(line) + 1 for line in whole)
    dropped = 0
    if not view.get("record"):
        lines.append("No routing record on this Mac.")
    else:
        width = cfg["routing_line_chars"]
        sources = view.get("sources") or {}
        precedence = table_precedence(sources.get("table"), now)
        if precedence:
            lines.append(precedence)
        items: list[tuple[str, str, str]] = []  # (kind, line, at) in display order
        items += [("account", _clip(runner_line(r, now), width), "") for r in sources.get("runners") or []
                  if r.get("silent") or r.get("unreadable")]
        for entry in view["accounts"]:
            items.append(("open" if entry["state"] == OPEN else "account",
                          _clip(account_line(entry, now), width), ""))
        items += [("lane", _clip(lane_line(u, now), width), "") for u in view.get("unmatched") or []]
        items += [("hold", _clip(hold_line(h, now), width), h["at"]) for h in view.get("holds") or []]
        limit = cfg["routing_summary_chars"]

        def size(kept: list, n_dropped: int) -> int:
            total = sum(len(item[1]) + 1 for item in kept)
            if n_dropped:
                total += len(f"{n_dropped} more lines: run `khipu route`") + 1
            return total

        kept = list(items)
        if limit:
            victims = sorted((i for i in kept if i[0] == "hold"), key=lambda i: i[2])
            victims += [i for i in reversed(kept) if i[0] == "open"]
            victims += [i for i in reversed(kept) if i[0] not in ("hold", "open")]
            for victim in victims:
                if size(kept, dropped) <= limit:
                    break
                kept.remove(victim)
                dropped += 1
        lines += [item[1] for item in kept]
        if dropped:
            lines.append(f"{dropped} more lines: run `khipu route`")
    return {"text": "\n".join(lines), "dropped": dropped, "rules_note_chars": rules_note_chars,
            "version": view["version"], "epoch": view.get("epoch")}


def since(version: int, view: dict | None = None, *, now: datetime | None = None,
          epoch: str | None = None) -> dict:
    """The current state of everything that changed after ``version``. The
    answer is complete only when this record's count can explain
    ``version``: not newer than the current version (a count that started
    again after view.json was lost or replaced), not older than the kept
    history, of the same ``epoch`` when the reader gives one, and from a
    saved view. Otherwise it is everything, with ``complete: false``."""
    now = now or _now()
    view = read_view(now) if view is None else view
    changes = view.get("changes") or []
    oldest = changes[0]["version"] if changes else view["version"] + 1
    complete = (oldest - 1 <= version <= view["version"] and view.get("saved", True) is not False
                and (epoch is None or epoch == view.get("epoch")))
    if complete:
        entities: list[str] = []
        for change in changes:
            if change["version"] > version:
                entities += [e for e in change["entities"] if e not in entities]
    else:
        entities = list(view.get("effective") or {})
    lines = [_current_line(e, view, now) for e in entities]
    return {"since": version, "epoch": view.get("epoch"), "version": view["version"], "complete": complete,
            "entities": entities, "lines": lines}


def _current_line(entity: str, view: dict, now: datetime) -> str:
    kind, _, ident = entity.partition(":")
    if kind == "account":
        entry = next((a for a in view["accounts"] if a["key"] == ident), None)
        if entry:
            return account_line(entry, now)
    if kind == "hold":
        hold = next((h for h in view["holds"] if h["id"] == ident), None)
        if hold:
            return hold_line(hold, now)
    if kind == "lane":
        runner, _, lane = ident.partition("/")
        item = next((u for u in view["unmatched"] if u["runner"] == runner and u["lane"] == lane), None)
        if item:
            return lane_line(item, now)
    if entity not in (view.get("effective") or {}):
        # Gone from the view: the line stored when it went says what it was.
        for change in reversed(view.get("changes") or []):
            if entity in change["entities"]:
                return change["lines"][change["entities"].index(entity)]
    return change_line(entity, view, now)


# ---- doctor ---------------------------------------------------------------------------


def doctor_block(now: datetime | None = None) -> dict:
    """What ``khipu doctor`` carries: the version, each account's line and
    the warnings from the scope's doctor list that phase 1a can see. Never
    gates doctor's ``ok`` and never probes anything."""
    now = now or _now()
    view = read_view(now)
    cfg = settings()
    warnings: list[str] = []
    for entry in view["accounts"]:
        if entry["state"] == LIMITED and entry["basis"] == "estimated":
            warnings.append(f"{entry['label']} is marked limited only on a guess.")
    for item in view.get("unmatched") or []:
        warnings.append(f"A limit came from {item['runner']}'s {item['lane']} lane, which isn't matched to an account.")
    after = cfg["routing_disagreement_warn_after"]
    for entry in view["accounts"]:
        sides = [(entry["label"], entry.get("disagreement"))]
        sides += [(f"{entry['label']} ({m['model']})", m.get("disagreement")) for m in entry.get("models") or []]
        for name, dis in sides:
            began = _parse(dis["since"]) if dis else None
            if began and after and (now - began).total_seconds() > after:
                warnings.append(f"Sources have disagreed about {name} for more than {_span(after)}.")
    text = summary(view, now=now) if view.get("record") else None
    if text and text["dropped"]:
        warnings.append(f"The summary had to leave out {text['dropped']} lines.")
    limit = cfg["routing_summary_chars"]
    if text and limit and text["rules_note_chars"] > limit:
        warnings.append("Your rules and note are longer than the summary size.")
    if view.get("outside_app"):
        warnings.append("A rule, a pause, a clear or your note was written by something other than the app.")
    if view.get("seed_error"):
        warnings.append(f"The standing rules were not imported: {view['seed_error']}.")
    bad = view.get("bad_lines") or 0
    if bad:
        warnings.append(f"{bad} line{'s' if bad != 1 else ''} of the routing record can't be read; "
                        f"Khipu leaves {'them' if bad != 1 else 'it'} out.")
    events, _bad = read_events()
    newest = max((at for at in (_parse(e.get("written_at")) for e in events) if at is not None), default=None)
    if newest is not None and (newest - now).total_seconds() > CLOCK_TOLERANCE:
        warnings.append(f"The routing record holds times ahead of this Mac's clock (as late as "
                        f"{_iso(newest)}); Khipu goes by the clock. Check the Mac's date and time.")
    _fixed, behind, ahead = _write_order_times([_parse(e.get("written_at")) for e in events])
    # Lines that took the record's time because their Mac's clock read before
    # the whole record (``_stamp_locked``) are named by that reading.
    for event in events:
        written, clock = _parse(event.get("written_at")), _parse(event.get("clock_at"))
        if written and clock and (written - clock).total_seconds() > CLOCK_TOLERANCE:
            behind.append(clock)
    for found, side, edge, which in ((behind, "before", "early", min), (ahead, "after", "late", max)):
        if found:
            them = "them" if len(found) != 1 else "it"
            warnings.append(f"The routing record holds {len(found)} line{'s' if len(found) != 1 else ''} dated "
                            f"well {side} the lines written around {them} (as {edge} as {_iso(which(found))}); "
                            f"Khipu counts each from the line {side} it. Check the date and time on the Mac "
                            f"that wrote {them}.")
    sources = view.get("sources") or {}
    warnings += [runner_line(r, now) + "." for r in sources.get("runners") or []
                 if r.get("silent") or r.get("unreadable")]
    if sources.get("refused"):
        warnings.append("A job runner folder was refused because it is inside a private folder.")
    if sources.get("invalid"):
        warnings.append(f"{sources['invalid']} job runner folder setting{'s are' if sources['invalid'] != 1 else ' is'} "
                        "not valid and left out (each needs a name, kind \"lanes\" and the folder's full path).")
    from khipu import routing_sources

    cache = view.get("source_cache") or {}
    checks = routing_sources.rule_checks(cache, view.get("rules") or [])
    warnings += [c["text"] for c in checks if c["status"] == "broken"]
    warnings += _launch_warnings(view, routing_sources.launches(cache), cfg, now)
    return {"record": bool(view.get("record")), "version": view["version"], "folded_at": view.get("folded_at"),
            "valid_until": view.get("valid_until"),
            "accounts": [{"account": e["label"], "state": e["state"], "until": e["until"], "basis": e["basis"],
                          "line": account_line(e, now)} for e in view["accounts"]],
            "holds": len(view.get("holds") or []), "rules": len(view.get("rules") or []),
            "rules_to_confirm": sum(1 for r in view.get("rules") or [] if r["seeded"] and not r["confirmed"]),
            "note": view.get("note") is not None, "outside_app": view.get("outside_app") or [],
            "seed_error": view.get("seed_error"), "bad_lines": view.get("bad_lines", 0),
            "sources": _sources_brief(sources), "rule_checks": checks, "warnings": warnings}


def _sources_brief(sources: dict) -> dict:
    """The sources' status as doctor and ``khipu route sources`` show it."""
    return {"runners": [{k: r.get(k) for k in ("name", "path", "files", "heartbeat_at", "interval", "silent",
                                                 "unreadable", "unreadable_since", "lanes", "unmatched_lanes")}
                        for r in sources.get("runners") or []],
            "refused": sources.get("refused") or [], "invalid": sources.get("invalid") or 0,
            "usage": sources.get("usage"), "table": sources.get("table")}


def _launch_warnings(view: dict, launched: list[dict], cfg: dict, now: datetime) -> list[str]:
    """"A job was started on <account> while you had it paused", or "...after
    it had reached its limit": a LAUNCH line, through the lane map, after the
    pause or the limit now shown began (and before the limit's end)."""
    lane_map = cfg.get("routing_lane_map") or {}
    known = [{"key": a["key"], "label": a["label"], "harness": a["harness"], "home": a.get("home")}
             for a in view["accounts"]]
    by_key = {a["key"]: a for a in view["accounts"]}
    out: list[str] = []
    said: set[str] = set()
    for item in launched:
        key = _evidence_account({"runner": item["runner"], "lane": item["lane"]}, lane_map, known)
        entry = by_key.get(key or "")
        at = _parse(item["at"])
        if entry is None or at is None or key in said:
            continue
        witnessed = (entry.get("witness") or {}).get("at")
        began = _parse(witnessed if entry["state"] == PAUSED else entry.get("since") or witnessed)
        until = _parse(entry.get("until"))
        if began is None or at < began:
            continue
        if entry["state"] == PAUSED:
            out.append(f"A job was started on {entry['label']} while you had it paused.")
            said.add(key)
        elif entry["state"] == LIMITED and (until is None or at < until):
            out.append(f"A job was started on {entry['label']} after it had reached its limit.")
            said.add(key)
    return out


# ---- the CLI ---------------------------------------------------------------------------


def _owner_via(args) -> str | None:
    """'app' for the app's own fixed-argv calls, 'cli' after a typed
    confirmation on a terminal, else None (refused)."""
    if getattr(args, "from_app", False):
        return "app"
    try:
        if not sys.stdin.isatty():
            return None
        answer = input("This is an owner's change to the routing record. Type yes to confirm: ")
    except (EOFError, OSError):
        return None
    return "cli" if answer.strip().lower() == "yes" else None


_REFUSED = ("This is the owner's command: it needs a typed confirmation on a terminal, or the app "
            "(Settings \u2192 Routing).")


def _print_json(data: dict) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _print_view(view: dict, now: datetime, account: str | None) -> None:
    print(f"Routing at {_clock(now, now)} (version {view['version']}; Khipu reports, routing stays with you)")
    if not view.get("record"):
        print("No routing record on this Mac.")
    if view.get("rules"):
        print("Standing rules:")
        for rule in view["rules"]:
            mark = "" if rule["confirmed"] else "  [seeded; confirm in Settings \u2192 Routing]"
            print(f"  {rule['n']}. {rule['text']}{mark}")
    if view.get("note"):
        print(note_line(view["note"], now))
    entries = [e for e in view["accounts"] if account is None or e["key"] == account]
    if entries:
        print("Accounts:")
    for entry in entries:
        print(f"  {account_line(entry, now, push=False)}")
        w = entry.get("witness") or {}
        if w.get("evidence"):
            print(f"      evidence: {w['evidence']}")
        if entry.get("underlying"):
            u = entry["underlying"]
            print(f"      underneath the pause: {u['state']}" + (f" until {u['until']}" if u.get("until") else ""))
        dis = entry.get("disagreement")
        if dis:
            print(f"      report {dis['id']} disagrees; `khipu route clear` can dismiss one side")
        for m in entry.get("models") or []:
            if m.get("disagreement"):
                print(f"      report {m['disagreement']['id']} disagrees about {m['model']}; "
                      "`khipu route clear` can dismiss one side")
    holds = [h for h in view.get("holds") or []
             if account is None or (h["scope"].get("kind") == "account" and h["scope"].get("account") == account)]
    if holds:
        print("Holds:")
        for hold in holds:
            print(f"  [{hold['id']}] {hold_line(hold, now, push=False)}")
    dark = [r for r in (view.get("sources") or {}).get("runners") or [] if r.get("silent") or r.get("unreadable")]
    if dark and account is None:
        print("Job runners:")
        for runner in dark:
            print(f"  {runner_line(runner, now)}")
    if view.get("unmatched") and account is None:
        print("Lanes not matched to an account:")
        for item in view["unmatched"]:
            print(f"  {lane_line(item, now)}")
    if view.get("seed_error"):
        print(f"Rules seed: {view['seed_error']}")


def _print_sources(out: dict, now: datetime) -> None:
    """``khipu route sources``: each job runner folder with its last good
    read and any error, the usage cache and the routing table."""
    if not (out["runners"] or out["refused"] or out["invalid"] or out["table"]):
        print("No job runner folders are registered (routing_sources).")
    for runner in out["runners"]:
        print(f"{runner['name']}: {runner['path']}")
        beat = _parse(runner.get("heartbeat_at"))
        if runner.get("silent"):
            print("  stopped reporting" + (f" at {_clock(beat, now)}" if beat else " (no heartbeat read)"))
        elif beat:
            print(f"  reporting; last heartbeat {_ago(beat, now)}")
        for name, f in sorted((runner.get("files") or {}).items()):
            good = _parse(f.get("good_at"))
            last = f"last good read {_clock(good, now)}" if good else "never read"
            print(f"  {name}: " + (f"can't read it ({f['error']}) since {_clock(_parse(f['since']), now)}; {last}"
                                   if f.get("error") else last))
        if runner.get("unmatched_lanes"):
            print("  lanes not matched to an account: " + ", ".join(runner["unmatched_lanes"]))
    for name in out["refused"]:
        print(f"{name}: refused, because it is inside a private folder")
    if out["invalid"]:
        print(f"{out['invalid']} job runner folder setting(s) not valid")
    usage = out.get("usage") or {}
    newest = _parse(usage.get("newest"))
    print(f"Usage cache: {usage.get('accounts') or 0} account(s)" + (f", newest figures {_ago(newest, now)}"
                                                                      if newest else ""))
    table = out.get("table")
    if table:
        print(f"Routing table: {table['path']}")
        print(f"  {table_line(table, now)}")


def _parse_since(raw) -> tuple[int, str | None]:
    """``--since V`` or ``--since EPOCH:V`` (both as ``khipu route --json``
    gives them)."""
    epoch, _, number = str(raw).strip().rpartition(":")
    try:
        return int(number), (epoch or None)
    except ValueError:
        raise RouteError("--since takes a version number, or epoch:version") from None


def run_cli(args) -> int:
    """``khipu route``: read the record, or write one change to it. An
    owner's change is timed after its typed confirmation, not before."""
    cmd = getattr(args, "route_cmd", None)
    now = _now()
    try:
        if cmd is None:
            view = read_view(now)
            account = resolve_account(args.view_account) if getattr(args, "view_account", None) else None
            if args.since is not None:
                version, epoch = _parse_since(args.since)
                out = since(version, view, now=now, epoch=epoch)
                if args.json:
                    _print_json(out)
                else:
                    if not out["complete"]:
                        print(f"Khipu can't tell what changed since version {version}; this is everything.")
                    print("\n".join(out["lines"]) if out["lines"] else "Nothing changed.")
                return 0
            if args.summary or args.brief:
                text = summary(view, now=now, brief=bool(args.brief))
                if args.json:
                    _print_json(text)
                else:
                    print(text["text"])
                return 0
            if args.json:
                out = dict(view, generated_at=_iso(now))
                out["accounts"] = [dict(e, line=account_line(e, now, push=False)) for e in view["accounts"]
                                   if account is None or e["key"] == account]
                out.pop("fingerprint", None)
                out.pop("digest", None)
                out.pop("source_cache", None)
                _print_json(out)
            else:
                _print_view(view, now, account)
            return 0
        if cmd == "sources":
            out = _sources_brief(read_view(now).get("sources") or {})
            if args.json:
                _print_json(out)
            else:
                _print_sources(out, now)
            return 0
        if cmd == "report":
            out = report(state=args.state, account=args.account, runner=args.runner, lane=args.lane,
                         until=args.until, window=args.window, model=args.model, message=args.message,
                         evidence=args.evidence, seen=getattr(args, "seen", None),
                         author=conversation_author(name=args.name), now=now)
            _print_json(out)
            return 0
        if cmd == "hold":
            words = list(args.words)
            owner_flag = bool(args.owner or args.from_app)
            via = "cli"
            if owner_flag:
                via = _owner_via(args)
                if via is None:
                    _print_json({"ok": False, "error": _REFUSED})
                    return 2
                now = _now()
            if len(words) == 2 and words[0] == "end":
                _print_json(end_hold(words[1], owner=owner_flag, via=via, now=now))
                return 0
            if not args.reason:
                raise RouteError("--reason is needed: machine-busy, disk-low, keep, outage or other")
            scope = "account" if args.account else "machine" if args.machine else "all" if args.all else ""
            _print_json(add_hold(" ".join(words), reason=args.reason, scope=scope, account=args.account,
                                 until=args.until, owner=owner_flag, via=via, now=now))
            return 0
        via = _owner_via(args)
        if via is None:
            _print_json({"ok": False, "error": _REFUSED})
            return 2
        now = _now()
        if cmd == "pause":
            out = pause(args.account, until=args.until, via=via, now=now)
        elif cmd == "lift":
            out = lift(args.account, via=via, now=now)
        elif cmd == "clear":
            out = clear(args.account, report_id=args.report, via=via, now=now)
        elif cmd == "note":
            if args.action == "set":
                out = set_note(args.text or "", via=via, now=now)
            else:
                out = end_note(via=via, now=now)
        elif cmd == "rule":
            params = list(args.params)
            if args.action == "add":
                out = add_rule(" ".join(params), source=args.source, via=via, now=now)
            elif args.action == "edit":
                if len(params) < 2:
                    raise RouteError('rule edit takes the rule number and its new words: rule edit 3 "<words>"')
                out = edit_rule(params[0], " ".join(params[1:]), via=via, now=now)
            elif len(params) != 1:
                raise RouteError(f"rule {args.action} takes one rule number")
            elif args.action == "retire":
                out = retire_rule(params[0], via=via, now=now)
            else:
                out = confirm_rule(params[0], via=via, now=now)
        else:
            raise RouteError(f"unknown route command {cmd!r}")
        _print_json(out)
        return 0
    except RouteError as err:
        _print_json({"ok": False, "error": str(err)})
        return 2
