"""Lightweight identities for buffered routing answers; no engine import."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path


# Shared with the engine's loader; semantic fallback accepts the same format.
SCHEMA = 1

# What sources and probes report, as opposed to what anyone decided: a line of
# one of these kinds changes an answer only through what a fold makes of it.
# Compaction treats them as retention, too (``routing_replication``).
TELEMETRY_KINDS = frozenset({"usage_sample", "source_seen", "evidence", "observation", "observation_renew",
                             "reset_check"})
_TAIL_MOST = 256 * 1024
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _parse(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def digest(view: dict) -> str:
    body = {k: v for k, v in view.items() if k != "digest"}
    text = json.dumps(body, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _reason(value):
    """Which reason a pace figure gives, without its figures: "readings cover
    only 7.5 of the last 24 h" going to 7.6 is the clock, not a change."""
    return _NUMBER.sub("#", value).replace("readings", "reading") if isinstance(value, str) else value


def revision(view: dict) -> str:
    """Routing state and pace decisions, excluding advancing measurements.

    Effective entities cover owner, account and source changes. Pace verdicts
    can also change on the clock without moving the routing version.
    """
    # Deliberately exclude cap_proposals, machine_holds, tier_table,
    # priority_ended and proposal seven-day expiry from this whitelist.
    # Source edits are protected by file stats; clock-only changes outside
    # the whitelist do not invalidate a buffered answer.
    decisions = {}
    now = _parse(view.get("checked_at") or view.get("folded_at"))
    for account in view.get("accounts") or []:
        windows = []
        for pace in account.get("pace") or []:
            decision = {key: pace[key] for key in ("headline", "cycle_resets_at", "idle") if key in pace}
            if "why" in pace:
                decision["why"] = _reason(pace["why"])
            for key in ("plan", "share_cap", "day_cap", "pace_target", "check_pace_target"):
                value = pace.get(key)
                if isinstance(value, dict):
                    decision[key] = {k: _reason(value[k]) if k == "why" else value[k]
                                     for k in ("verdict", "why", "revision") if k in value}
                    if key in ("plan", "share_cap", "day_cap"):
                        eta = _parse(value.get("runs_out_at") or value.get("reaches_cap_at"))
                        decision[key]["crosses_within_hour"] = bool(
                            now is not None and eta is not None and now < eta
                            and eta - now <= timedelta(hours=1))
            if pace.get("ceiling_verdicts"):
                decision["ceiling_verdicts"] = pace["ceiling_verdicts"]
            if decision:
                windows.append(dict(decision, window=pace.get("window")))
        if windows:
            decisions[account["key"]] = windows
    return digest({**{k: view.get(k) for k in ("epoch", "version", "effective", "record")},
                   "pace_decisions": decisions,
                   "advice_cap_decisions": view.get("advice_cap_decisions")})


def _appended(path: str, start, end) -> list[dict] | None:
    """The whole lines added to the log between two of its stats. None when
    that cannot be told: another file, a shorter one, too much to read, a line
    half down or not one a writer writes."""
    if not (isinstance(start, list) and isinstance(end, list)) or start[2] != end[2] or end[1] <= start[1] \
            or end[1] - start[1] > _TAIL_MOST:
        return None
    with open(path, "rb") as fh:
        fh.seek(start[1])
        data = fh.read(end[1] - start[1])
    if not data.endswith(b"\n"):
        return None
    rows = []
    for line in data[:-1].split(b"\n"):
        row = json.loads(line) if line.strip() else None
        if not isinstance(row, dict):
            return None
        rows.append(row)
    return rows


def _kind_word(row: dict) -> str:
    kind = row.get("type")
    return kind if isinstance(kind, str) and re.fullmatch(r"[a-z_]{1,40}", kind) else "other"


def _same_reading(row: dict, view: dict) -> bool:
    """Whether a pace reading says what the view already holds for its window:
    the same usage in the same cycle, with nothing waiting to settle. It moves
    only the time of the newest reading, which no answer's advice rests on."""
    if row.get("type") != "usage_sample":
        return False
    entry = next((a for a in view.get("accounts") or [] if a.get("key") == row.get("account")), None)
    pace = next((p for p in (entry or {}).get("pace") or [] if p.get("window") == row.get("window")), None)
    if pace is None or pace.get("used_pct") is None or row.get("used_pct") != pace["used_pct"]:
        return False
    if str(pace.get("why") or "").startswith(("possible reset", "reset ")):
        return False
    held, read = _parse(pace.get("cycle_resets_at")), _parse(row.get("resets_at"))
    return held is not None and read is not None and abs((held - read).total_seconds()) <= 60


def _log_decision(path: str, answered, observed) -> str | None:
    """Which decision the log's growth since an answer's read holds, or None
    when it holds only reports. Anyone's decision always counts, as does growth
    that cannot be read."""
    rows = _appended(path, answered, observed)
    if rows is None:
        return "events:rewritten"
    for row in rows:
        by = row.get("written_by")
        if row.get("type") not in TELEMETRY_KINDS or (isinstance(by, dict) and by.get("kind") == "owner") \
                or row.get("via") in ("app", "direction"):
            return f"events:{_kind_word(row)}"
    return None


def _unread_change(path: str, view: dict, answered, observed) -> str | None:
    """A report counts as what the fold made of it. The saved view (its revision
    already matched) says so when it read that far; past that, only a reading it
    already holds can be excused."""
    folded = (view.get("fingerprint") or {}).get("events")
    if folded == observed:
        return None
    # A view older than the answer (a stale answer took the log's stat at its
    # read): what was there then is that answer's, delivered a prompt late.
    read_to = folded if isinstance(folded, list) and folded[2] == observed[2] and folded[1] > answered[1] else answered
    unread = _appended(path, read_to, observed)
    if unread is None:
        return "events:rewritten"
    for row in unread:
        if not _same_reading(row, view):
            return f"events:{_kind_word(row)}"
    return None


def _input_word(path: str) -> str:
    name = Path(path).name
    return {"config.json": "config", "remote.jsonl": "remote", "heartbeat.json": "heartbeat",
            "events.jsonl": "events", "view.json": "view"}.get(name, "seed")


def _read_view(path: str):
    """The saved view as one file. A save landing during the read (the rename
    is atomic) means reading the newer one; None when saves keep landing."""
    for _ in range(3):
        with open(path, encoding="utf-8") as fh:
            before = os.fstat(fh.fileno())
            view = json.load(fh)
        after = os.stat(path)
        if (before.st_mtime_ns, before.st_size, before.st_ino) == (after.st_mtime_ns, after.st_size, after.st_ino):
            return view
    return None


def _view_change(path: str, expected: str, changed: dict, answered: dict) -> str | None:
    """Damaged or constantly replaced views fail closed. What changed, in a word
    for the log, or None when the answer stands."""
    try:
        view = _read_view(path)
        if view is None:
            return "view:replaced"
        if not (isinstance(view, dict) and view.get("schema") == SCHEMA
                and view.get("digest") == digest(view)):
            return "view:unreadable"
        rest = dict(changed)
        log = str(Path(path).parent / "events.jsonl")
        if log in rest:
            # A decision in the log names itself, whatever the fold made of it.
            cause = _log_decision(log, answered.get(log), rest[log])
            if cause:
                return cause
        if revision(view) != expected:
            return "view:revision"
        if log in rest:
            cause = _unread_change(log, view, answered.get(log), rest.pop(log))
            if cause:
                return cause
        if rest:
            from khipu.routing_sources import RUNNER_FILES

            index = list(RUNNER_FILES).index("heartbeat")
            fingerprint = view.get("fingerprint") if isinstance(view.get("fingerprint"), dict) else {}
            fingerprints = dict((fingerprint.get("sources") or {}).get("runners") or [])
            recorded = {
                str(Path(runner["path"]).joinpath(*RUNNER_FILES["heartbeat"])):
                    fingerprints[runner["name"]][index]
                for runner in (view.get("sources") or {}).get("runners") or []
            }
            # What another Mac sent: a view folded after that write recorded
            # it, and its semantic revision already matched above, the same
            # standard a runner heartbeat meets. (replication.json is not an
            # input of an answer: its bookkeeping is no change of the record.)
            recorded[str(Path(path).parent / "remote.jsonl")] = (fingerprint.get("replication") or (None, None))[0]
            for heartbeat, observed in rest.items():
                # Only a fold that read this exact heartbeat can establish
                # equivalence; other changed inputs always invalidate it.
                if heartbeat not in recorded or recorded[heartbeat] != observed:
                    return _input_word(heartbeat)
                try:
                    current = os.stat(heartbeat)
                    stat = [current.st_mtime_ns, current.st_size, current.st_ino]
                except OSError:
                    stat = None
                if stat != observed:
                    return _input_word(heartbeat)
        return None
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
        return "view:unreadable"


def matches(path: str, expected: str, *, heartbeats: dict | None = None) -> bool:
    """Whether the saved view still carries this revision, with ``heartbeats``
    (files changed since) all read by the fold that saved it."""
    return _view_change(path, expected, heartbeats or {}, {}) is None


def stale_input(files) -> str | None:
    """What changed under a routing answer (``routing._generation``), in a word
    for the log, or None when the answer stands: every file it was read from
    has its stat, or, for a rewritten view, the same semantic routing revision.
    Other changed inputs need an equivalent view folded from their current
    stats; the log's own growth is judged by ``_log_change``. An answer can sit
    in the pipe while search runs; telemetry-only saves must not suppress it."""
    if not isinstance(files, list):
        return "generation"
    changed, answered = {}, {}
    views = []
    for item in files:
        if not (isinstance(item, list) and len(item) in (2, 3) and isinstance(item[0], str)):
            return "generation"
        if len(item) == 3 and not isinstance(item[2], str):
            return "generation"
        try:
            st = os.stat(item[0])
            now = [st.st_mtime_ns, st.st_size, st.st_ino]
        except OSError:
            now = None
        if len(item) == 3:
            views.append((item, now != item[1]))
        elif now != item[1]:
            changed[item[0]], answered[item[0]] = now, item[1]
    if changed and not views:
        return _input_word(next(iter(changed)))
    for item, rewritten in views:
        if rewritten or changed:
            cause = _view_change(item[0], item[2], changed, answered)
            if cause:
                return cause
    return None
