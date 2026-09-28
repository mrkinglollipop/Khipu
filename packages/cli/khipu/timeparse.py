# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""Natural-language time interpretation (Phase 3, session A; "BUILD — time
interpretation", docs/plans/2026-09-27-memory-reasoning-scope.md).

A bounded grammar, English, case-insensitive, standard library only (finding
B6, docs/research/hindsight-plan-review-2026-09-28.md: no ``dateparser``, no
model). ``interpret`` never guesses: a query naming zero productions, or more
than one, returns ``None`` rather than picking one. Applied by a caller only
when the ``time_interpretation`` switch is on AND no explicit since/until was
already given — see ``khipu.embed.hybrid_search`` and
``khipu.recall_prompt._snapshot_search_hits``.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

# A bounded, multiplicative preference — never an exclusion (finding 8,
# docs/research/hindsight-plan-review-2026-09-28.md: Hindsight's own
# score-space boost collapsed recall; this stays in rank/score space applied
# uniformly, and nothing is ever dropped for falling outside the window).
TIME_BOOST = 1.25

MONTH_NAMES: dict[str, int] = {
    name.lower(): i
    for i, name in enumerate(
        [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ],
        start=1,
    )
}
# Monday=0 .. Sunday=6 (weeks start Monday, per the brief).
WEEKDAY_NAMES: dict[str, int] = {
    name.lower(): i
    for i, name in enumerate(
        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    )
}

_ISO_DATE = r"\d{4}-\d{2}-\d{2}"
_MONTH_ALT = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
_WEEKDAY_ALT = "|".join(sorted(WEEKDAY_NAMES, key=len, reverse=True))


def _day_bounds(d: date, zone: Any) -> tuple[datetime, datetime]:
    start = datetime(d.year, d.month, d.day, tzinfo=zone)
    return start, start + timedelta(days=1) - timedelta(microseconds=1)


def _week_bounds(d: date, zone: Any) -> tuple[datetime, datetime]:
    monday = d - timedelta(days=d.weekday())
    start = datetime(monday.year, monday.month, monday.day, tzinfo=zone)
    return start, start + timedelta(days=7) - timedelta(microseconds=1)


def _shift_month(year: int, month: int, n: int) -> tuple[int, int]:
    """(year, month) shifted back by ``n`` calendar months."""
    idx = (year * 12 + (month - 1)) - n
    return idx // 12, idx % 12 + 1


def _month_bounds(year: int, month: int, zone: Any) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=zone)
    ny, nm = _shift_month(year, month, -1)
    nxt = datetime(ny, nm, 1, tzinfo=zone)
    return start, nxt - timedelta(microseconds=1)


def _most_recent_weekday(today: date, target: int) -> date:
    return today - timedelta(days=(today.weekday() - target) % 7)


def _most_recent_month_start(today: date, target_month: int) -> tuple[int, int]:
    year = today.year
    if target_month > today.month:
        year -= 1
    return year, target_month


# ---- resolvers: (match, now_local_date, zone) -> (since_local, until_local) -


def _r_today(m: re.Match, now: date, zone: Any):
    return _day_bounds(now, zone)


def _r_yesterday(m: re.Match, now: date, zone: Any):
    return _day_bounds(now - timedelta(days=1), zone)


def _r_this_week(m: re.Match, now: date, zone: Any):
    return _week_bounds(now, zone)


def _r_last_week(m: re.Match, now: date, zone: Any):
    return _week_bounds(now - timedelta(days=7), zone)


def _r_this_month(m: re.Match, now: date, zone: Any):
    return _month_bounds(now.year, now.month, zone)


def _r_last_month(m: re.Match, now: date, zone: Any):
    y, mo = _shift_month(now.year, now.month, 1)
    return _month_bounds(y, mo, zone)


def _r_n_ago(m: re.Match, now: date, zone: Any):
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("day"):
        return _day_bounds(now - timedelta(days=n), zone)
    if unit.startswith("week"):
        return _week_bounds(now - timedelta(days=7 * n), zone)
    y, mo = _shift_month(now.year, now.month, n)
    return _month_bounds(y, mo, zone)


def _r_in_last(m: re.Match, now: date, zone: Any):
    n = int(m.group(1))
    unit = m.group(2).lower()
    until_start, until_end = _day_bounds(now, zone)
    if unit.startswith("day"):
        since_start, _ = _day_bounds(now - timedelta(days=n), zone)
    elif unit.startswith("week"):
        since_start, _ = _day_bounds(now - timedelta(days=7 * n), zone)
    else:
        y, mo = _shift_month(now.year, now.month, n)
        since_start, _ = _day_bounds(date(y, mo, min(now.day, 28)), zone)
    return since_start, until_end


def _r_since(m: re.Match, now: date, zone: Any):
    iso = m.group("since_iso")
    wd = m.group("since_wd")
    mo_name = m.group("since_mo")
    if iso:
        target = date.fromisoformat(iso)
        since, _ = _day_bounds(target, zone)
        return since, None
    if wd:
        target = _most_recent_weekday(now, WEEKDAY_NAMES[wd.lower()])
        since, _ = _day_bounds(target, zone)
        return since, None
    y, mnum = _most_recent_month_start(now, MONTH_NAMES[mo_name.lower()])
    since, _ = _month_bounds(y, mnum, zone)
    return since, None


def _r_before(m: re.Match, now: date, zone: Any):
    target = date.fromisoformat(m.group(1))
    start, _ = _day_bounds(target, zone)
    return None, start - timedelta(microseconds=1)


def _r_after(m: re.Match, now: date, zone: Any):
    target = date.fromisoformat(m.group(1))
    _, end = _day_bounds(target, zone)
    return end + timedelta(microseconds=1), None


def _r_on(m: re.Match, now: date, zone: Any):
    target = date.fromisoformat(m.group(1))
    return _day_bounds(target, zone)


def _r_in_month(m: re.Match, now: date, zone: Any):
    mo_name = m.group(1)
    year = int(m.group(2)) if m.group(2) else now.year
    return _month_bounds(year, MONTH_NAMES[mo_name.lower()], zone)


# Priority order matters only for overlap resolution (a match from an
# earlier pattern wins a shared span); the trigger keywords are disjoint in
# practice so this is mostly a documentation aid, not a load-bearing choice.
_PRODUCTIONS: list[tuple[re.Pattern, Callable]] = [
    (re.compile(r"\btoday\b", re.I), _r_today),
    (re.compile(r"\byesterday\b", re.I), _r_yesterday),
    (re.compile(r"\bthis week\b", re.I), _r_this_week),
    (re.compile(r"\blast week\b", re.I), _r_last_week),
    (re.compile(r"\bthis month\b", re.I), _r_this_month),
    (re.compile(r"\blast month\b", re.I), _r_last_month),
    (re.compile(r"\b(\d+)\s+(days?|weeks?|months?)\s+ago\b", re.I), _r_n_ago),
    (
        re.compile(r"\bin the (?:last|past)\s+(\d+)\s+(days|weeks|months)\b", re.I),
        _r_in_last,
    ),
    (
        re.compile(
            r"\bsince\s+(?:(?P<since_iso>" + _ISO_DATE + r")"
            r"|(?P<since_wd>" + _WEEKDAY_ALT + r")"
            r"|(?P<since_mo>" + _MONTH_ALT + r"))\b",
            re.I,
        ),
        _r_since,
    ),
    (re.compile(r"\bbefore\s+(" + _ISO_DATE + r")\b", re.I), _r_before),
    (re.compile(r"\bafter\s+(" + _ISO_DATE + r")\b", re.I), _r_after),
    (re.compile(r"\bon\s+(" + _ISO_DATE + r")\b", re.I), _r_on),
    # "as of" joins "in" here (not in the brief's literal list) because the
    # mandatory scenario suite's own fixed query text is "... as of September
    # 2026", and khipu.validity._HISTORY_CUES already treats "as of" as a
    # canonical historical marker — this production keeps that word aligned
    # with the one grammar production it would otherwise fall outside of.
    (
        re.compile(
            r"\b(?:in|as of)\s+(" + _MONTH_ALT + r")(?:\s+(\d{4}))?\b", re.I
        ),
        _r_in_month,
    ),
]


def _resolve_zone(tz: str | None) -> tuple[Any, str, bool]:
    """(zone, name actually used, True if ``tz`` was given but unknown)."""
    if not tz:
        return timezone.utc, "UTC", False
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tz), tz, False
    except Exception:  # noqa: BLE001 — an unknown/bad zone name falls back, never raises
        return timezone.utc, "UTC", True


def interpret(query: str, now: datetime, tz: str | None = None) -> dict[str, Any] | None:
    """``None``, or ``{since, until, phrase, tz, as_of}`` (ISO strings, UTC;
    ``since``/``until`` individually nullable — e.g. "since Monday" has no
    ``until``). ``as_of`` is ``since`` (or ``until`` when there is no
    ``since``) as a single representative instant, for a caller that wants
    one point rather than a window. ``tz`` in the result is the zone actually
    used ("UTC" whenever ``tz`` was omitted or unrecognized); an unrecognized
    ``tz`` also adds ``tz_note`` naming what was asked for, so the fallback is
    visible rather than silent.

    Two matched productions (even two instances of the same one) — or a
    phrase this grammar simply does not cover, like "recently" — both read as
    "cannot place it unambiguously" and return ``None``. Never raises.
    """
    text = query or ""
    if not text.strip():
        return None
    zone, tz_name, fallback = _resolve_zone(tz)
    now_aware = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    now_local_date = now_aware.astimezone(zone).date()

    kept: list[tuple[int, int, re.Match, Callable]] = []
    for pattern, resolver in _PRODUCTIONS:
        for m in pattern.finditer(text):
            if any(not (m.end() <= s or m.start() >= e) for s, e, _, _ in kept):
                continue
            kept.append((m.start(), m.end(), m, resolver))
    if len(kept) != 1:
        return None

    start, end, match, resolver = kept[0]
    try:
        since_local, until_local = resolver(match, now_local_date, zone)
    except (ValueError, KeyError):
        # A malformed captured date (e.g. Feb 30) is "cannot be placed", not a crash.
        return None

    since_utc = since_local.astimezone(timezone.utc) if since_local else None
    until_utc = until_local.astimezone(timezone.utc) if until_local else None
    as_of = since_utc or until_utc
    result: dict[str, Any] = {
        "since": since_utc.isoformat() if since_utc else None,
        "until": until_utc.isoformat() if until_utc else None,
        "phrase": text[start:end],
        "tz": tz_name,
        "as_of": as_of.isoformat() if as_of else None,
    }
    if fallback:
        result["tz_note"] = f"unknown timezone {tz!r}; using UTC"
    return result


def _parse_row_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if hasattr(value, "timestamp") and hasattr(value, "tzinfo"):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def apply_time_boost(
    rows: list[dict[str, Any]], interpretation: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Multiply ``score`` by ``TIME_BOOST`` for every row whose ``ts`` falls
    inside the interpreted window, and re-sort. A preference, not a filter:
    a row with no usable ``ts``, or outside the window, is kept exactly as
    given — nothing is ever excluded here."""
    if not rows or not interpretation:
        return rows
    since = interpretation.get("since")
    until = interpretation.get("until")
    if not since and not until:
        return rows
    since_dt = _parse_row_ts(since) if since else None
    until_dt = _parse_row_ts(until) if until else None
    changed = False
    for row in rows:
        ts = _parse_row_ts(row.get("ts"))
        if ts is None:
            continue
        if since_dt is not None and ts < since_dt:
            continue
        if until_dt is not None and ts > until_dt:
            continue
        try:
            score = float(row.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        row["score"] = round(score * TIME_BOOST, 6)
        changed = True
    if not changed:
        return rows
    return sorted(rows, key=lambda r: -(r.get("score") or 0.0))
