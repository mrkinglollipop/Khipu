"""Routing truth, phase 1b: the sources the routing record folds in.

Scope: docs/plans/2026-10-14-routing-truth.md (Sources, Phase 1b). Khipu
reports; nothing here picks, ranks, refuses, launches or reorders work.

- Job runners (setting ``routing_sources``: ``{name, kind: "lanes", path}``)
  are read and never written. Each folder is read for ``state/global.json``
  ``limited``; the ``ADDED``, ``LIMITED``, ``LAUNCH`` and ``HOLD`` lines of a
  bounded tail of ``events.log``; ``state/heartbeat.json``; and, for doctor's
  rule checks only, these keys of ``config.json`` and nothing else: each
  lane's ``kind``, ``model``, ``launch`` and ``confirm_lane``, and
  ``defaults.check_lane`` and ``defaults.fix_lanes``. Every file is opened
  read-only (``os.O_RDONLY``), no lock of the runner's is taken, and every
  line kept passes through ``redact_secrets`` first. A folder, or a file
  that resolves, inside a credential location is refused and never opened.
- Khipu's usage cache (``state/usage.json``): a window at 100% or more is a
  limit until its ``resets_at`` (basis cli) for its scope (a model, or the
  whole account); figures with headroom (every window under 100%) are open
  (basis checked), and a window at 100% with no reset time ahead gives no
  evidence for its scope; ``not_signed_in`` and ``not_installed`` are down.
- The routing table (setting ``routing_table``), read only and never as
  account state: its NOW block's ``updated:`` line and a hash of the block.

Everything here reads files and returns plain data; the fold
(``routing.fold``) stays pure.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# A runner's files, relative to its folder (the lanes runner's own layout).
RUNNER_FILES = {"global": ("state", "global.json"), "heartbeat": ("state", "heartbeat.json"),
                "events": ("events.log",), "config": ("config.json",)}
TAIL_BYTES = 512 * 1024          # the bounded tail of events.log read on each fold
JSON_MAX = 4 * 1024 * 1024       # a state file larger than this is not read
TABLE_MAX = 2 * 1024 * 1024
# The lanes runner rests a lane 30 minutes when the message gives no reset
# time (LIMIT_SECONDS in lanes.py). Its LIMITED line is stamped a moment after
# that time was taken, both cut to whole seconds, so the guess lands 29:55 to
# 30:00 after the line (a real line: 03:09:03 with until 03:39:02).
GUESS_SECONDS = 30 * 60
GUESS_SLACK = 5
LINE_MAX = 200
KEEP_LINES = {"LIMITED": 200, "ADDED": 300, "LAUNCH": 300}
LAUNCH_KEEP = timedelta(days=2)
# A heartbeat interval longer than this is not a heartbeat (the runners write
# 20 and 30 seconds); the file is reported as out of range.
INTERVAL_MAX = 24 * 3600

_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+(\S+)\s+(ADDED|LIMITED|LAUNCH|HOLD)\b[ \t]*(.*)$")
_FIELD = re.compile(r"(?:^|\s)([a-z_]+)=(\S+)")
_NOW_BLOCK = re.compile(r"## NOW\s*\n```\n(.*?)\n```", re.S)
_UPDATED = re.compile(r"^updated:.*$", re.M)
_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?Z")


# ---- small helpers -------------------------------------------------------------


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


class OutOfRange(ValueError):
    """A number a file holds that no time or interval can be."""


def _number(value) -> float | None:
    """A finite number a file holds, as a float, or None. JSON reads an
    integer of any length, which a float can't always hold."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        out = float(value)
    except OverflowError:
        return None
    return out if math.isfinite(out) else None


def _epoch(value) -> datetime | None:
    """A runner's epoch seconds (a float in global.json and heartbeat.json)."""
    number = _number(value)
    if number is None:
        return None
    try:
        return datetime.fromtimestamp(number, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _redacted(text, limit: int = LINE_MAX) -> str:
    """One line of a runner's words, secrets masked whatever the redaction
    switch says, clipped."""
    from khipu.redact import redact_secrets

    one = " ".join(str(text or "").split())
    one = redact_secrets(one, force=True)[0]
    return one if len(one) <= limit else one[: max(1, limit - 1)].rstrip() + "…"


def _name(value, limit: int = 64) -> str | None:
    """A lane, model or pipeline name a runner wrote, as plain short text."""
    if not isinstance(value, str) or not value.strip():
        return None
    return _redacted(value.strip(), limit)


def _sid(*parts) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8", "replace")).hexdigest()[:12]


def private_path(path: str | os.PathLike) -> bool:
    """A path inside a credential location, by its own names or by where it
    resolves (a link into one counts)."""
    from khipu.routing import credential_location

    if credential_location(path):
        return True
    try:
        return credential_location(os.path.realpath(os.fspath(Path(path).expanduser())))
    except (OSError, ValueError, RuntimeError):
        return True


def read_only(path: Path, *, tail: int | None = None, limit: int = JSON_MAX) -> tuple[bytes, float]:
    """The bytes of one runner file (the last ``tail`` bytes, or at most
    ``limit``) and its modification time. Opened ``O_RDONLY`` (plus
    ``O_NONBLOCK``, so a FIFO never hangs a read) and never anything else:
    no write handle, no lock, no temp file. A path that resolves inside a
    credential location raises PermissionError before anything is opened.

    A tail always starts on a whole line: the byte before it is read too,
    and when that byte is not a newline the cut-off first line is left out.
    A tail that starts at the top of the file, or right after a newline,
    keeps its first line."""
    if private_path(path):
        raise PermissionError("inside a private folder")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError("not a regular file")
        before = False  # whether the byte before the tail is read too
        if tail is not None:
            begin = max(0, st.st_size - tail)
            before = begin > 0
            os.lseek(fd, begin - 1 if before else 0, os.SEEK_SET)
            want = tail + 1 if before else tail
        else:
            if st.st_size > limit:
                raise OSError(f"larger than {limit // (1024 * 1024)} MB")
            want = limit
        chunks: list[bytes] = []
        got = 0
        while got < want:
            chunk = os.read(fd, min(65536, want - got))
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
        data = b"".join(chunks)
        if before:
            # The byte before the tail: a newline means the tail starts a
            # line; anything else means its first line is cut off.
            data = data[1:] if data[:1] == b"\n" else data.partition(b"\n")[2]
        return data, st.st_mtime
    finally:
        os.close(fd)


def _short_error(exc: BaseException) -> str:
    if isinstance(exc, FileNotFoundError):
        return "missing"
    if isinstance(exc, PermissionError):
        return "not allowed to read it"
    if isinstance(exc, (OutOfRange, OverflowError)):
        return "a number out of range"
    if isinstance(exc, (ValueError, UnicodeDecodeError, RecursionError)):
        return "not readable JSON"
    return _redacted(str(exc) or type(exc).__name__, 80)


# ---- which runner folders ---------------------------------------------------------


def registered(config: dict) -> tuple[list[dict], list[str], int]:
    """(the runner folders to read, the names of those refused because they
    are inside a private folder, how many entries are not valid). Each entry
    of ``routing_sources`` is checked on its own, so one bad entry (a hand
    edit) costs that entry, never the others."""
    from khipu import routing

    raw = config.get("routing_sources")
    if raw is None:
        return [], [], 0
    if not isinstance(raw, list):
        return [], [], 1
    entries: list[dict] = []
    refused: list[str] = []
    invalid = 0
    seen: set[str] = set()
    for item in raw:
        name = item.get("name") if isinstance(item, dict) else None
        path = item.get("path") if isinstance(item, dict) else None
        if isinstance(path, str) and path.strip() and private_path(path.strip()):
            refused.append(_name(name) or "a job runner")
            continue
        try:
            entry = routing._coerce("sources", [item], "routing_sources")[0]
        except (ValueError, TypeError):
            invalid += 1
            continue
        if entry["name"].casefold() in seen:
            invalid += 1
            continue
        seen.add(entry["name"].casefold())
        entries.append(entry)
    return entries, refused, invalid


def fingerprint(config: dict, cfg: dict) -> dict:
    """The modification times the view is folded from, besides the log and
    config.json: each runner file, the usage cache and the table. Stat only:
    nothing is opened."""
    from khipu import usage
    from khipu.routing import _stat

    entries, _refused, _invalid = registered(config)
    runners = []
    for entry in entries:
        folder = Path(entry["path"])
        runners.append([entry["name"], [_stat(folder.joinpath(*parts)) for parts in RUNNER_FILES.values()]])
    table = cfg.get("routing_table")
    return {"runners": runners, "usage": _stat(usage.cache_file()), "table": _stat(Path(table)) if table else None}


def any_configured(config: dict, cfg: dict) -> bool:
    """Whether any source exists to fold: a runner (or a refused one), the
    table, or a usage cache."""
    from khipu import usage

    raw = config.get("routing_sources")
    return bool((isinstance(raw, list) and raw) or cfg.get("routing_table") or usage.cache_file().exists())


# ---- one runner ------------------------------------------------------------------------


def _parse_global(data: bytes) -> dict:
    doc = json.loads(data.decode("utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("not an object")
    limited = doc.get("limited") or {}
    if not isinstance(limited, dict):
        raise ValueError("limited is not an object")
    out = {}
    for lane, item in limited.items():
        lane_name = _name(lane)
        until = item.get("until") if isinstance(item, dict) else None
        if lane_name is None or _epoch(until) is None:
            continue
        out[lane_name] = {"until": _number(until), "reason": _redacted(item.get("reason"), 120) or None}
    return {"limited": out}


def _parse_heartbeat(data: bytes) -> dict:
    doc = json.loads(data.decode("utf-8"))
    if not isinstance(doc, dict) or doc.get("time") is None:
        raise ValueError("no time")
    if _epoch(doc["time"]) is None:
        raise OutOfRange("time out of range")
    if doc.get("interval") is None:
        raise ValueError("no interval")
    interval = _number(doc["interval"])
    if interval is None or not 0 < interval <= INTERVAL_MAX:
        raise OutOfRange("interval out of range")
    return {"time": _number(doc["time"]), "interval": interval}


def _parse_config(data: bytes) -> dict:
    """Only the named keys: each lane's kind, model, launch and
    confirm_lane, and defaults.check_lane and defaults.fix_lanes. Nothing
    else in config.json is kept."""
    doc = json.loads(data.decode("utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("lanes"), dict):
        raise ValueError("no lanes")
    lanes = {}
    for lane, spec in list(doc["lanes"].items())[:64]:
        lane_name = _name(lane)
        if lane_name is None or not isinstance(spec, dict):
            continue
        launch = spec.get("launch")
        lanes[lane_name] = {
            "kind": _name(spec.get("kind")), "model": _name(spec.get("model")),
            "launch": [_redacted(a, 300) for a in launch[:32] if isinstance(a, str)]
            if isinstance(launch, list) else [],
            "confirm_lane": _name(spec.get("confirm_lane")),
        }
    defaults = doc.get("defaults") if isinstance(doc.get("defaults"), dict) else {}
    fix = defaults.get("fix_lanes")
    return {"lanes": lanes, "check_lane": _name(defaults.get("check_lane")),
            "fix_lanes": [n for n in (_name(x) for x in fix[:32]) if n] if isinstance(fix, list) else []}


def runner_basis(at: datetime, until: datetime) -> str:
    """Until runners write the basis themselves: cli when the time's seconds
    are zero and it is not the 30-minute rest after the line (the shape
    ``limit_until`` gives a parsed message plus two minutes); everything else
    is estimated."""
    gap = (until - at).total_seconds()
    if until.second == 0 and until.microsecond == 0 and not (
            GUESS_SECONDS - GUESS_SLACK <= gap <= GUESS_SECONDS + GUESS_SLACK):
        return "cli"
    return "estimated"


def _parse_events(data: bytes, now: datetime) -> dict:
    # ``read_only`` hands over whole lines only (a cut-off first line is
    # already left out), so every line here is kept.
    lines = data.decode("utf-8", "replace").split("\n")
    out: dict[str, list] = {"LIMITED": [], "ADDED": [], "LAUNCH": []}
    holds = 0
    first: datetime | None = None
    for line in lines:
        m = _LINE.match(line.strip())
        if not m:
            continue
        at = _parse(m.group(1))
        if at is None:
            continue
        first = at if first is None else min(first, at)
        kind, pipeline, rest = m.group(3), _name(m.group(2)), m.group(4)
        fields = dict(_FIELD.findall(rest))
        if kind == "HOLD":
            holds += 1
        elif kind == "LIMITED":
            lane, until = _name(fields.get("lane")), _parse(fields.get("until"))
            if lane and until:
                out["LIMITED"].append({"at": _iso(at), "pipeline": pipeline, "lane": lane, "until": _iso(until),
                                       "basis": runner_basis(at, until),
                                       "text": _redacted(f"{m.group(2)} LIMITED {rest}")})
        elif kind == "ADDED":
            # A line that names no check lane or no fix lanes leaves the
            # runner's defaults (config.json) to pick them: kept as None.
            check = _name(fields.get("check_lane")) if "check_lane" in fields else None
            fix = [n for n in (_name(x) for x in fields["fix_lanes"].split(",")) if n] \
                if "fix_lanes" in fields else None
            out["ADDED"].append({"at": _iso(at), "pipeline": pipeline,
                                 "private": bool(re.search(r"(?:^|\s)privacy(?:\s|$)", rest)),
                                 "lanes": [n for n in [check, *(fix or [])] if n],
                                 "check_lane": check, "fix_lanes": fix})
        elif kind == "LAUNCH":
            lane = _name(fields.get("lane"))
            if lane and now - at <= LAUNCH_KEEP:
                out["LAUNCH"].append({"at": _iso(at), "pipeline": pipeline, "lane": lane})
    for kind, keep in KEEP_LINES.items():
        out[kind] = out[kind][-keep:]
    return {"limited_lines": out["LIMITED"], "added": out["ADDED"], "launches": out["LAUNCH"],
            "holds": holds, "first": _iso(first)}


_PARSERS = {"global": _parse_global, "heartbeat": _parse_heartbeat, "config": _parse_config}


def read_runner(entry: dict, now: datetime, prev: dict | None, silent_intervals: int) -> tuple[dict, dict]:
    """(status, cache) for one runner. A file that can't be read or parsed
    keeps its last good read (from ``prev``, the cache the last saved view
    kept) and is reported with the time it was last read well and since when
    it has failed; nothing is guessed around it."""
    folder = Path(entry["path"])
    prev = prev if isinstance(prev, dict) else {}
    files: dict[str, dict] = {}
    cache: dict[str, dict] = {}
    for key, parts in RUNNER_FILES.items():
        path = folder.joinpath(*parts)
        last = prev.get(key) if isinstance(prev.get(key), dict) else {}
        try:
            if key == "events":
                data, mtime = read_only(path, tail=TAIL_BYTES)
                parsed = _parse_events(data, now)
            else:
                data, mtime = read_only(path)
                parsed = _PARSERS[key](data)
            cache[key] = {"data": parsed, "good_at": _iso(now), "mtime": mtime, "since": None}
            files[key] = {"error": None, "good_at": _iso(now), "since": None}
        except (OSError, ValueError, UnicodeDecodeError, RecursionError, OverflowError, TypeError) as exc:
            since = last.get("since") or _iso(now)
            if last.get("data") is not None:
                cache[key] = dict(last, since=since)
            else:
                cache[key] = {"data": None, "good_at": None, "mtime": None, "since": since}
            files[key] = {"error": _short_error(exc), "good_at": cache[key].get("good_at"), "since": since}
    cache["sightings"] = _sightings(cache, prev.get("sightings"))
    from khipu.routing import _after

    beat = cache["heartbeat"].get("data")
    beat = beat if isinstance(beat, dict) else {}
    beat_at = _epoch(beat.get("time"))
    interval = _number(beat.get("interval"))
    deadline = None
    if beat_at is not None and interval and silent_intervals:
        deadline = _after(beat_at, interval * silent_intervals)
    silent = beat_at is None or (deadline is not None and now >= deadline)
    errors = [f for f in files.values() if f["error"]]
    status = {
        "name": entry["name"], "path": entry["path"], "files": files,
        "heartbeat_at": _iso(beat_at), "interval": interval,
        "silent": silent, "silent_deadline": None if silent else _iso(deadline),
        "unreadable": bool(errors), "unreadable_since": min((f["since"] for f in errors), default=None),
        "lanes": sorted((cache["config"].get("data") or {}).get("lanes", {})),
    }
    return status, cache


def _same_until(a: datetime, b: datetime) -> bool:
    return abs((a - b).total_seconds()) <= 1


def _sightings(cache: dict, prev) -> dict:
    """For each ``global.json`` limit, the time it is dated by, kept in the
    view between reads (``source_cache``) so it outlives the tail:

    - ``line`` true: the time of the newest LIMITED line Khipu has seen for
      it (same lane, same end), kept after that line leaves the tail;
    - ``line`` false: a limit whose line Khipu never saw is dated at the
      oldest line the tail held when Khipu first found it (the latest that
      line can have been written), and keeps that first time.

    An owner's clear is weighed against this time like any other evidence:
    a clear written after it outranks the limit, and a limit seen after the
    clear stands even once its line has left the tail."""
    prev = prev if isinstance(prev, dict) else {}
    events = (cache.get("events") or {}).get("data") or {}
    glob = cache.get("global") or {}
    lines = []
    for line in events.get("limited_lines") or []:
        if isinstance(line, dict) and _parse(line.get("at")) and _parse(line.get("until")):
            lines.append((line.get("lane"), _parse(line["at"]), _parse(line["until"])))
    bound = _parse(events.get("first")) or _epoch(glob.get("mtime"))
    out: dict[str, dict] = {}
    for lane, item in ((glob.get("data") or {}).get("limited") or {}).items():
        until = _epoch(item.get("until")) if isinstance(item, dict) else None
        if until is None:
            continue
        old = prev.get(lane) if isinstance(prev.get(lane), dict) else {}
        old_at, old_until = _parse(old.get("at")), _epoch(old.get("until"))
        if old_at is None or old_until is None or not _same_until(old_until, until):
            old_at = None  # a different limit on the lane, or none kept
        old_line = old_at is not None and old.get("line") is True
        seen = [at for name, at, end in lines if name == lane and _same_until(end, until)]
        if seen:
            at, line = max(seen + ([old_at] if old_line else [])), True
        elif old_at is not None:
            at, line = old_at, old_line
        elif bound is not None:
            at, line = bound, False
        else:
            continue
        out[lane] = {"until": item["until"], "at": _iso(at), "line": line}
    return out


def runner_evidence(status: dict, cache: dict) -> list[dict]:
    """The evidence one runner gives, in the fold's shape: one limited entry
    per LIMITED line in the tail, and one per ``global.json`` limit still in
    force whose LIMITED line is no longer in the tail, dated by its sighting
    (``_sightings``): the time of its LIMITED line when Khipu saw it, or the
    latest that line can have been written when Khipu never did. Every entry
    is marked ``silent`` when the runner's heartbeat has stopped."""
    name = status["name"]
    by = {"kind": "runner", "name": name}
    events = (cache.get("events") or {}).get("data") or {}
    out: list[dict] = []
    lines = [line for line in events.get("limited_lines") or []
             if _parse(line.get("at")) is not None and _parse(line.get("until")) is not None]
    for line in lines:
        at, until = _parse(line["at"]), _parse(line["until"])
        basis = line.get("basis") if line.get("basis") in ("cli", "estimated") else "estimated"
        out.append({"id": "r" + _sid(name, line["at"], line.get("lane"), line["until"], line.get("pipeline")),
                    "account": None, "runner": name, "lane": line["lane"], "state": "limited", "until": until,
                    "basis": basis, "scope": "all", "window": None, "used_pct": None, "source": "runner",
                    "written_by": by, "via": "runner", "at": at, "evidence": line.get("text"), "reason": None,
                    "message_time": basis == "cli", "silent": status["silent"]})
    glob = cache.get("global") or {}
    sightings = cache.get("sightings") if isinstance(cache.get("sightings"), dict) else {}
    bound = _parse(events.get("first")) or _epoch(glob.get("mtime"))
    for lane, item in ((glob.get("data") or {}).get("limited") or {}).items():
        until = _epoch(item.get("until"))
        sight = sightings.get(lane) if isinstance(sightings.get(lane), dict) else {}
        sight_until = _epoch(sight.get("until"))
        at = _parse(sight.get("at")) if sight_until and until and _same_until(sight_until, until) else None
        at = at or bound
        if until is None or at is None or until <= at:
            continue  # an old limit the runner keeps on file; its time has passed
        if any(line["lane"] == lane and abs((_parse(line["until"]) - until).total_seconds()) <= 1 for line in lines):
            continue  # its LIMITED line is in the tail: that line is the evidence
        exact = float(item["until"]).is_integer() and until.second == 0
        out.append({"id": "r" + _sid(name, "global", lane, item.get("until")), "account": None, "runner": name,
                    "lane": lane, "state": "limited", "until": until, "basis": "cli" if exact else "estimated",
                    "scope": "all", "window": None, "used_pct": None, "source": "runner", "written_by": by,
                    "via": "runner", "at": at, "reason": None, "message_time": exact,
                    "evidence": _redacted(f"global.json: {lane} limited ({item.get('reason') or 'no reason'})"),
                    "silent": status["silent"]})
    return out


# ---- the usage cache ----------------------------------------------------------------


def usage_evidence() -> tuple[list[dict], dict]:
    """Evidence from Khipu's own usage cache (``khipu usage``), and a short
    status of it. A window at 100% or more closes its scope until its reset
    (basis cli); figures with headroom (every window under 100%) reopen it
    (basis checked); a window at 100% with no reset time ahead gives no
    evidence for its scope, never open; a probe that found the CLI not
    signed in or not installed is down. Figures Muse
    or Codex keep from their last turn are marked ``observed``."""
    from khipu import usage

    rows = usage._load_cache()
    out: list[dict] = []
    newest: datetime | None = None
    by = {"kind": "probe"}
    for key, row in sorted(rows.items()):
        if key.partition("|")[0] not in usage.HARNESSES:
            continue
        attempt = _parse(row.get("last_attempt_at") or row.get("probed_at"))
        error = None if row.get("ok") else (row.get("last_error") or row.get("error"))
        if error in (usage.E_SIGNIN, usage.E_NOT_INSTALLED) and attempt is not None:
            out.append({"id": "p" + _sid(key, "down", _iso(attempt)), "account": key, "runner": None, "lane": None,
                        "state": "down", "until": None, "basis": "checked", "scope": "all", "window": None,
                        "used_pct": None, "source": "probe", "written_by": by, "via": "probe", "at": attempt,
                        "evidence": f"usage probe: {error}", "reason": error, "message_time": False})
            newest = attempt if newest is None else max(newest, attempt)
            continue
        if not (row.get("ok") or row.get("last_good_at")):
            continue
        at = _parse(row.get("observed_at") or row.get("probed_at"))
        windows = [w for w in row.get("windows") if isinstance(w, dict)] if isinstance(row.get("windows"), list) else []
        if at is None or not windows:
            continue
        newest = at if newest is None else max(newest, at)
        observed = row.get("source") in ("last_observed", "rollout")
        groups: dict[str, list[dict]] = {}
        for w in windows:
            model = w.get("model")
            groups.setdefault(model if isinstance(model, str) and model.strip() else "all", []).append(w)
        for scope, group in sorted(groups.items()):
            full: list[tuple[datetime, dict]] = []
            top = None
            for w in group:
                used = _number(w.get("used_pct"))
                if used is None:
                    continue
                top = used if top is None else max(top, used)
                reset = _parse(w.get("resets_at"))
                if used >= 100 and reset is not None and reset > at:
                    full.append((reset, w))
            # A window at its limit is never headroom: with no reset time
            # still ahead of the probe, the scope gives no evidence at all.
            if top is None or (top >= 100 and not full):
                continue
            base = {"account": key, "runner": None, "lane": None, "scope": _redacted(scope, 64), "source": "probe",
                    "written_by": by, "via": "probe", "at": at, "reason": None, "message_time": False,
                    "observed": observed}
            if full:
                reset, w = max(full, key=lambda item: item[0])
                name = str(w.get("name") or "")
                window = "weekly" if name.startswith("weekly") else name or None
                out.append(dict(base, id="p" + _sid(key, scope, "limited", _iso(at), _iso(reset)), state="limited",
                                until=reset, basis="cli", window=window, used_pct=_number(w.get("used_pct")),
                                evidence=_redacted(f"usage probe: {name or 'window'} at {_number(w.get('used_pct'))}%")))
            else:
                out.append(dict(base, id="p" + _sid(key, scope, "open", _iso(at)), state="open", until=None,
                                basis="checked", window=None, used_pct=top,
                                evidence=f"usage probe: highest window at {top}%"))
    return out, {"accounts": len(rows), "newest": _iso(newest)}


# ---- the routing table (a pointer, never account state) ------------------------------


def read_table(path: str | None) -> dict | None:
    """The table's NOW block as a pointer: a hash of the block, its
    ``updated:`` line (clipped and redacted) and the time it gives, and the
    file's modification time. Its prose is never kept."""
    if not path:
        return None
    out: dict[str, Any] = {"path": path, "hash": None, "updated": None, "stamped_at": None, "changed_at": None,
                           "error": None}
    try:
        data, mtime = read_only(Path(path), limit=TABLE_MAX)
    except (OSError, ValueError) as exc:
        out["error"] = _short_error(exc)
        return out
    m = _NOW_BLOCK.search(data.decode("utf-8", "replace"))
    if not m:
        out["error"] = "no NOW block"
        return out
    block = m.group(1)
    out["hash"] = hashlib.sha256(block.encode("utf-8", "replace")).hexdigest()[:16]
    out["changed_at"] = _iso(_epoch(mtime))
    line = _UPDATED.search(block)
    if line:
        out["updated"] = _redacted(line.group(0), 120)
        stamp = _STAMP.search(line.group(0))
        if stamp:
            out["stamped_at"] = _iso(_parse(f"{stamp.group(1)}T{stamp.group(2)}:{stamp.group(3)}:"
                                            f"{stamp.group(4) or '00'}Z"))
    return out


# ---- all sources --------------------------------------------------------------------


def collect(config: dict, cfg: dict, now: datetime, prev_cache: dict | None) -> tuple[list[dict], dict, dict]:
    """(evidence for the fold, the sources' status, the cache to keep in the
    view). Reads files only: starts no process, probes nothing, writes
    nothing, takes no runner's lock."""
    entries, refused, invalid = registered(config)
    prev_cache = prev_cache if isinstance(prev_cache, dict) else {}
    lane_map = cfg.get("routing_lane_map") or {}
    runners: list[dict] = []
    cache: dict[str, Any] = {"runners": {}}
    evidence: list[dict] = []
    for entry in entries:
        status, kept = read_runner(entry, now, (prev_cache.get("runners") or {}).get(entry["name"]),
                                   cfg.get("routing_runner_silent_intervals") or 0)
        mapped = lane_map.get(entry["name"]) or {}
        status["unmatched_lanes"] = [lane for lane in status["lanes"] if lane not in mapped]
        runners.append(status)
        cache["runners"][entry["name"]] = kept
        evidence += runner_evidence(status, kept)
    usage_items, usage_status = usage_evidence()
    evidence += usage_items
    status = {"runners": runners, "refused": refused, "invalid": invalid, "usage": usage_status,
              "table": read_table(cfg.get("routing_table"))}
    return evidence, status, cache


# ---- doctor's rule checks (read from config.json and ADDED lines only) ------------------


def _script(launch: list[str]) -> str:
    for arg in launch:
        if "/" in arg or arg.endswith((".sh", ".py")):
            return Path(arg).name or arg
    return launch[0] if launch else "the launch command"


def watched(rules: list[dict]) -> dict[str, int]:
    """Which checks the active standing rules ask for: the check's name to
    the rule's number. The public product ships with no rules, so nothing is
    checked there."""
    out: dict[str, int] = {}
    for rule in rules:
        text = str(rule.get("text") or "").lower()
        n = rule.get("n")
        if "-fast" in text:
            out.setdefault("fast_tier", n)
        if "xai" in text:
            out.setdefault("paid_xai", n)
        if "grok" in text and ("privacy" in text or "safety" in text):
            out.setdefault("grok_private", n)
        if "grok" in text and ("re-check" in text or "recheck" in text or "model family" in text):
            out.setdefault("grok_confirm", n)
    return out


def rule_checks(cache: dict, rules: list[dict]) -> list[dict]:
    """Each watched rule against each runner's settings and ADDED lines:
    ``broken`` (a doctor warning) or ``cannot_check`` (never reported as
    passing)."""
    watch = watched(rules)
    if not watch:
        return []
    out: list[dict] = []
    for name, kept in sorted(((cache or {}).get("runners") or {}).items()):
        config = (kept.get("config") or {}).get("data") or {}
        lanes = config.get("lanes") or {}
        for lane, spec in sorted(lanes.items()):
            launch = spec.get("launch") or []
            kind = (spec.get("kind") or "").lower()
            model = spec.get("model")
            if "fast_tier" in watch:
                if any(str(t).endswith("-fast") for t in [model or "", *launch]):
                    out.append({"rule": watch["fast_tier"], "status": "broken",
                                "text": f"A standing rule may be broken: {name}'s settings name a model ending in "
                                        f"-fast for its {lane} lane."})
                elif not model or model not in launch:
                    out.append({"rule": watch["fast_tier"], "status": "cannot_check",
                                "text": f"{name}'s {lane} lane: can't be checked: the model is set inside "
                                        f"{_script(launch)}"})
            if "paid_xai" in watch and kind.startswith("grok"):
                out.append({"rule": watch["paid_xai"], "status": "cannot_check",
                            "text": f"{name}'s {lane} lane: whether it uses the paid xAI API can't be checked: "
                                    f"the launch environment is set inside {_script(launch)}"})
            if "grok_confirm" in watch and kind == "grokcheck":
                confirm = spec.get("confirm_lane")
                other = lanes.get(confirm) if confirm else None
                other_kind = ((other or {}).get("kind") or "").lower()
                if other is None or other_kind.startswith("grok"):  # none named, or a lane the settings lack
                    out.append({"rule": watch["grok_confirm"], "status": "broken",
                                "text": f"A standing rule may be broken: {name}'s Grok check lane has no "
                                        "confirming lane from another model family."})
                elif not other_kind:
                    out.append({"rule": watch["grok_confirm"], "status": "cannot_check",
                                "text": f"{name}'s Grok check lane: whether its confirming lane {confirm} is from "
                                        "another model family can't be checked: its kind isn't set"})
        if "grok_private" in watch:
            grok = {lane for lane, spec in lanes.items() if (spec.get("kind") or "").lower().startswith("grok")}
            events = (kept.get("events") or {}).get("data") or {}
            for added in events.get("added") or []:
                named = set(added.get("lanes") or [])
                # Lanes the line leaves to the runner's defaults.
                if "check_lane" in added and added["check_lane"] is None and config.get("check_lane"):
                    named.add(config["check_lane"])
                if "fix_lanes" in added and added["fix_lanes"] is None:
                    named |= set(config.get("fix_lanes") or [])
                if added.get("private") and grok & named:
                    out.append({"rule": watch["grok_private"], "status": "broken",
                                "text": f"A standing rule may be broken: a Grok lane is set to work on "
                                        f"{added.get('pipeline')}, which is marked private."})
    return out


def launches(cache: dict) -> list[dict]:
    """Every LAUNCH line kept, with its runner."""
    out = []
    for name, kept in sorted(((cache or {}).get("runners") or {}).items()):
        events = (kept.get("events") or {}).get("data") or {}
        out += [{"runner": name, "lane": item["lane"], "at": item["at"]} for item in events.get("launches") or []]
    return out
