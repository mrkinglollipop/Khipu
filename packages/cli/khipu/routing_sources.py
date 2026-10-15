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
  read-only (``os.O_RDONLY``) and no lock of the runner's is taken. No text
  of a runner file is stored or shown: no line, no reason, no argument.
  Only parsed, typed fields are kept (times, the line kind, numbers, flags)
  and names (lane, pipeline, model, kind) that pass ``safe_name``; every
  evidence line is worded by Khipu from those fields, and the existing
  redaction runs over it as a second layer. A lane's ``launch`` arguments
  are never kept: only the facts the rule checks need are derived from them
  as they are read (whether a model ending in -fast is named, whether the
  model is one of the arguments, and the launch script's name). A folder,
  or a file that resolves, inside a credential location is refused and
  never opened.
- Khipu's usage cache (``state/usage.json``): a window at 100% or more is a
  limit until its ``resets_at`` (basis cli) for its scope (a model, or the
  whole account); figures with headroom (every window under 100%) are open
  (basis checked), and a window at 100% with no reset time ahead gives no
  evidence for its scope; ``not_signed_in`` and ``not_installed`` are down.
  Only rows of configured accounts are read; a row for any other key is
  skipped whole.
- The routing table (setting ``routing_table``), read only and never as
  account state: the time its NOW block's ``updated:`` line gives and a
  hash of the block; the line's text is never kept.

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
# The bounded tail of events.log read on each fold, and how many lines of
# each kind are kept from it: the defaults of the settings
# ``routing_runner_tail_bytes`` and ``routing_runner_limited_lines`` (ADDED
# and LAUNCH lines keep their own caps, which only shorten doctor's checks).
TAIL_BYTES = 512 * 1024
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

_STAMPED = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s")
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


# What ``redact_secrets`` leaves that a runner's line or argument can carry:
# an xAI key, and the value after a secret flag (``--api-key VALUE``,
# ``--password=VALUE``), which no pattern there knows is a secret. The word
# after a secret flag is masked whatever it looks like: a token may start
# with a dash, and masking a following flag by mistake costs nothing.
_XAI_KEY = re.compile(r"\bxai-[A-Za-z0-9_-]{16,}")
_FLAG_WORDS = r"(?:key|token|secret|password|passwd|pwd|credential|auth)"
_SECRET_FLAG = re.compile(
    r"(?i)(?<![\w-])(--?[a-z0-9_-]*" + _FLAG_WORDS + r"[a-z0-9_-]*)(\s*=\s*|\s+)(\S+)")
_SECRET_FLAG_ONLY = re.compile(r"(?i)^--?[a-z0-9_-]*" + _FLAG_WORDS + r"[a-z0-9_-]*$")
# Bumped whenever what the source cache keeps narrows: a source cache kept
# by a view saved under an older version is dropped, never carried forward,
# so text that older rules let through cannot outlive the fix. 3: no free
# text at all, only parsed fields and names that pass ``safe_name``.
REDACTION = 3


def _redacted(text, limit: int = LINE_MAX) -> str:
    """One line of a runner's words, secrets masked whatever the redaction
    switch says, clipped."""
    from khipu.redact import MASK, redact_secrets

    one = " ".join(str(text or "").split())
    one = redact_secrets(one, force=True)[0]
    one = _XAI_KEY.sub(MASK, one)
    one = _SECRET_FLAG.sub(lambda m: m.group(1) + m.group(2) + MASK, one)
    return one if len(one) <= limit else one[: max(1, limit - 1)].rstrip() + "…"


def _name(value, limit: int = 64) -> str | None:
    """An owner's runner name from Khipu's own settings, as plain short
    text (never a runner file's words: those go through ``safe_name``)."""
    if not isinstance(value, str) or not value.strip():
        return None
    return _redacted(value.strip(), limit)


# A name from a runner file, the usage cache or the table is kept only in
# this shape, only when redaction leaves it alone, and only when it does not
# look like a key; anything else is this fixed placeholder. An allowlist:
# masking free text has lost to new secret shapes round after round.
WITHHELD = "(name withheld)"
_NAME_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_KEY_PREFIX = re.compile(
    r"(?:^|[._-])(?:sk-|sk_|rk_|pk_live|xai-|AIza|gh[pousr]_|github_pat_|gl(?:pat|ptt|dt|oas|rt)-|AKIA|ASIA|xox|eyJ"
    r"|hf_|npm_|pypi-|shp(?:at|ss|ca)_|dop_v1_|SG\.)")
_KEY_RUN = re.compile(r"[A-Za-z0-9]{16,}")
_UUID = re.compile(r"[0-9A-Za-z]{8}-[0-9A-Za-z]{4}-[0-9A-Za-z]{4}-[0-9A-Za-z]{4}-[0-9A-Za-z]{12}")


def _key_like(text: str) -> bool:
    """A known key prefix, a UUID, or a run of 16 or more letters and digits
    that holds both. A key made of letters only cannot be told from a name;
    that residue is why a name is only ever shown, never trusted."""
    if _KEY_PREFIX.search(text) or _UUID.search(text):
        return True
    return any(re.search(r"[A-Za-z]", run) and re.search(r"[0-9]", run) for run in _KEY_RUN.findall(text))


def safe_name(value) -> str | None:
    """A lane, pipeline, model, kind or window name a source file gives: the
    name when it matches the strict shape, survives redaction unchanged and
    does not look like a key; ``WITHHELD`` otherwise. Anything that is not
    text is dropped (None), never turned into text."""
    if not isinstance(value, str) or not value.strip():
        return None
    name = value.strip()
    if not _NAME_SHAPE.match(name) or _key_like(name) or _redacted(name, 128) != name:
        return WITHHELD
    return name


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
    keeps its first line. A tail of 0 is the whole file."""
    if private_path(path):
        raise PermissionError("inside a private folder")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode):
            raise IsADirectoryError("a folder, not a file")
        if not stat.S_ISREG(st.st_mode):
            raise OSError("not a regular file")
        before = False  # whether the byte before the tail is read too
        if tail is not None:
            tail = tail or st.st_size  # 0: the whole file
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


_OWN_ERRORS = re.compile(r"not a regular file|larger than \d+ MB")


def _short_error(exc: BaseException) -> str:
    """A fixed phrase for why a file could not be read: never the
    exception's own text."""
    if isinstance(exc, FileNotFoundError):
        return "missing"
    if isinstance(exc, PermissionError):
        return "not allowed to read it"
    if isinstance(exc, (OutOfRange, OverflowError)):
        return "a number out of range"
    if isinstance(exc, (ValueError, UnicodeDecodeError, RecursionError)):
        return "not readable JSON"
    if isinstance(exc, IsADirectoryError):
        return "a folder, not a file"
    message = exc.args[0] if len(exc.args) == 1 and isinstance(exc.args[0], str) else ""
    return message if _OWN_ERRORS.fullmatch(message) else "can't be read"


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
    return {"runners": runners, "usage": _stat(usage.cache_file()), "table": _stat(Path(table)) if table else None,
            "redaction": REDACTION}


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
        lane_name = safe_name(lane)
        until = item.get("until") if isinstance(item, dict) else None
        if lane_name is None or _epoch(until) is None:
            continue
        # The lane and its end only: the runner's reason is free text.
        out[lane_name] = {"until": _number(until)}
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


_SCRIPT_EXT = (".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".rb", ".pl")
_LAUNCH_COMMAND = "the launch command"


def _script_name(launch: list[str]) -> str:
    """The launch script's file name, the only text a launch list gives the
    record: the first argument that is a script file, else the program run,
    and only when it is a plain short file name that redaction leaves alone
    (``safe_name``) and that no secret flag precedes; otherwise a fixed
    phrase."""
    after_flag = False
    candidates = []
    for arg in launch:
        if not after_flag and arg.endswith(_SCRIPT_EXT):
            candidates.append(arg)
        after_flag = bool(_SECRET_FLAG_ONLY.match(arg))
    if launch:
        candidates.append(launch[0])
    for arg in candidates:
        name = Path(arg).name
        if safe_name(name) == name:
            return name
    return _LAUNCH_COMMAND


def launch_facts(model, launch) -> dict:
    """What doctor's rule checks need from a lane's launch arguments, derived
    as they are read; the arguments themselves are never kept."""
    args = [a.strip() for a in launch[:64] if isinstance(a, str)] if isinstance(launch, list) else []
    model = model.strip() if isinstance(model, str) else ""
    return {"fast": any(t.endswith("-fast") for t in [model, *args]),
            "model_in_launch": bool(model) and model in args,
            "script": _script_name(args)}


def _config_facts(lanes) -> dict:
    """A config read kept by an older view, rebuilt from the allowed fields
    only: names that pass ``safe_name``, the launch facts (any launch list it
    held turned into the facts and dropped), and nothing else."""
    out = {}
    for lane, spec in (lanes.items() if isinstance(lanes, dict) else ()):
        lane_name = safe_name(lane)
        if lane_name is None or not isinstance(spec, dict):
            continue
        if "launch" in spec or "fast" not in spec:
            facts = launch_facts(spec.get("model"), spec.get("launch"))
        else:
            script = spec.get("script")
            facts = {"fast": spec.get("fast") is True, "model_in_launch": spec.get("model_in_launch") is True,
                     "script": script if isinstance(script, str) and safe_name(script) == script
                     else _LAUNCH_COMMAND}
        out[lane_name] = {"kind": safe_name(spec.get("kind")), "model": safe_name(spec.get("model")),
                          "confirm_lane": safe_name(spec.get("confirm_lane")), **facts}
    return out


def _parse_config(data: bytes) -> dict:
    """Only the named keys: each lane's kind, model and confirm_lane, the
    facts its launch arguments give (``launch_facts``: never the arguments),
    and defaults.check_lane and defaults.fix_lanes. Nothing else in
    config.json is kept."""
    doc = json.loads(data.decode("utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("lanes"), dict):
        raise ValueError("no lanes")
    lanes = {}
    for lane, spec in list(doc["lanes"].items())[:64]:
        lane_name = safe_name(lane)
        if lane_name is None or not isinstance(spec, dict):
            continue
        lanes[lane_name] = {
            "kind": safe_name(spec.get("kind")), "model": safe_name(spec.get("model")),
            "confirm_lane": safe_name(spec.get("confirm_lane")),
            **launch_facts(spec.get("model"), spec.get("launch")),
        }
    defaults = doc.get("defaults") if isinstance(doc.get("defaults"), dict) else {}
    fix = defaults.get("fix_lanes")
    return {"lanes": lanes, "check_lane": safe_name(defaults.get("check_lane")),
            "fix_lanes": [n for n in (safe_name(x) for x in fix[:32]) if n] if isinstance(fix, list) else []}


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


def _limits(glob) -> dict[str, datetime]:
    """Each lane's limit end in a ``global.json`` read (``_parse_global``)."""
    out: dict[str, datetime] = {}
    for lane, item in ((glob or {}).get("limited") or {}).items() if isinstance(glob, dict) else ():
        until = _epoch(item.get("until")) if isinstance(item, dict) else None
        if until is not None:
            out[lane] = until
    return out


def _parse_events(data: bytes, now: datetime, limits: dict[str, datetime] | None = None,
                  keep: dict[str, int] | None = None) -> dict:
    """The kept lines of one tail of events.log. ``matched`` names, for each
    ``global.json`` limit (``limits``), the newest LIMITED line anywhere in
    the tail for it (same lane, same end), found before the line caps
    (``keep``, 0: no cap) drop older lines, so a limit whose line is still in
    the tail is always dated by that line, never by ``first``."""
    # ``read_only`` hands over whole lines only (a cut-off first line is
    # already left out), so every line here is kept.
    limits = limits or {}
    matched: dict[str, datetime] = {}
    lines = data.decode("utf-8", "replace").split("\n")
    out: dict[str, list] = {"LIMITED": [], "ADDED": [], "LAUNCH": []}
    holds = 0
    first: datetime | None = None
    for line in lines:
        # ``first`` is the oldest line of every kind the tail holds, not
        # only the kinds kept below.
        stamped = _STAMPED.match(line.strip())
        at = _parse(stamped.group(1)) if stamped else None
        if at is not None:
            first = at if first is None else min(first, at)
        m = _LINE.match(line.strip())
        if not m or at is None:
            continue
        # The line's own words never leave this loop: only its time, its
        # kind, and names that pass ``safe_name``.
        kind, pipeline, rest = m.group(3), safe_name(m.group(2)), m.group(4)
        fields = dict(_FIELD.findall(rest))
        if kind == "HOLD":
            holds += 1
        elif kind == "LIMITED":
            lane, until = safe_name(fields.get("lane")), _parse(fields.get("until"))
            if lane and until:
                if lane in limits and _same_until(limits[lane], until):
                    matched[lane] = at if lane not in matched else max(matched[lane], at)
                out["LIMITED"].append({"at": _iso(at), "pipeline": pipeline, "lane": lane, "until": _iso(until),
                                       "basis": runner_basis(at, until)})
        elif kind == "ADDED":
            # A line that names no check lane or no fix lanes leaves the
            # runner's defaults (config.json) to pick them: kept as None.
            check = safe_name(fields.get("check_lane")) if "check_lane" in fields else None
            fix = [n for n in (safe_name(x) for x in fields["fix_lanes"].split(",")) if n] \
                if "fix_lanes" in fields else None
            out["ADDED"].append({"at": _iso(at), "pipeline": pipeline,
                                 "private": bool(re.search(r"(?:^|\s)privacy(?:\s|$)", rest)),
                                 "lanes": [n for n in [check, *(fix or [])] if n],
                                 "check_lane": check, "fix_lanes": fix})
        elif kind == "LAUNCH":
            lane = safe_name(fields.get("lane"))
            if lane and now - at <= LAUNCH_KEEP:
                out["LAUNCH"].append({"at": _iso(at), "pipeline": pipeline, "lane": lane})
    for kind, cap in dict(KEEP_LINES, **(keep or {})).items():
        if cap:
            out[kind] = out[kind][-cap:]
    return {"limited_lines": out["LIMITED"], "added": out["ADDED"], "launches": out["LAUNCH"],
            "holds": holds, "first": _iso(first),
            "matched": {lane: {"until": _iso(limits[lane]), "at": _iso(at)} for lane, at in matched.items()}}


_PARSERS = {"global": _parse_global, "heartbeat": _parse_heartbeat, "config": _parse_config}


def read_runner(entry: dict, now: datetime, prev: dict | None, silent_intervals: int,
                tail_bytes: int = TAIL_BYTES, limited_lines: int = KEEP_LINES["LIMITED"]) -> tuple[dict, dict]:
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
                # global.json is read first (RUNNER_FILES order): its limits
                # are matched against every LIMITED line of the tail.
                data, mtime = read_only(path, tail=tail_bytes)
                parsed = _parse_events(data, now, _limits((cache.get("global") or {}).get("data")),
                                       {"LIMITED": limited_lines})
            else:
                data, mtime = read_only(path)
                parsed = _PARSERS[key](data)
            # ``seen_at``: when Khipu first read the file as it reads now. A
            # re-read that finds the same content keeps it, so a check judged
            # on the file (``rule_checks``) is dated by when Khipu first saw
            # what it judges, not by the latest refresh.
            same = last.get("data") is not None and last.get("data") == parsed and last.get("seen_at")
            cache[key] = {"data": parsed, "good_at": _iso(now), "seen_at": last["seen_at"] if same else _iso(now),
                          "mtime": mtime, "since": None}
            files[key] = {"error": None, "good_at": _iso(now), "since": None}
        except (OSError, ValueError, UnicodeDecodeError, RecursionError, OverflowError, TypeError) as exc:
            since = last.get("since") or _iso(now)
            if last.get("data") is not None:
                cache[key] = dict(last, since=since)
                if key == "config" and isinstance(last["data"], dict):
                    cache[key]["data"] = dict(last["data"], lanes=_config_facts(last["data"].get("lanes")))
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


def _in_tail(events: dict, lane: str, until: datetime) -> list[datetime]:
    """The times of LIMITED lines in the tail for this limit (same lane,
    same end): the lines kept, and the newest line matched before the line
    caps (``matched``), so a line the caps dropped still dates its limit."""
    out = []
    for line in events.get("limited_lines") or []:
        if isinstance(line, dict) and line.get("lane") == lane and _parse(line.get("at")):
            end = _parse(line.get("until"))
            if end is not None and _same_until(end, until):
                out.append(_parse(line["at"]))
    matched = events.get("matched") if isinstance(events.get("matched"), dict) else {}
    hit = matched.get(lane) if isinstance(matched.get(lane), dict) else {}
    end, at = _parse(hit.get("until")), _parse(hit.get("at"))
    if end is not None and at is not None and _same_until(end, until):
        out.append(at)
    return out


def _sightings(cache: dict, prev) -> dict:
    """For each ``global.json`` limit, the time it is dated by, kept in the
    view between reads (``source_cache``) so it outlives the tail:

    - ``line`` true: the time of the newest LIMITED line Khipu has seen for
      it (same lane, same end) anywhere in the tail, including a line the
      line cap dropped (``_in_tail``), kept after that line leaves the tail;
    - ``line`` false: a limit with no line anywhere in the tail is dated at
      the oldest line the tail held when Khipu first found it (its line was
      written before the tail began, so this is the latest it can have been
      written), and keeps that first time.

    An owner's clear is weighed against this time like any other evidence:
    a clear written after it outranks the limit, and a limit seen after the
    clear stands even once its line has left the tail."""
    prev = prev if isinstance(prev, dict) else {}
    events = (cache.get("events") or {}).get("data") or {}
    glob = cache.get("global") or {}
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
        seen = _in_tail(events, lane, until)
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


def _until_words(at: datetime, until: datetime) -> str:
    return until.strftime("%H:%MZ") if until.date() == at.date() else until.strftime("%Y-%m-%d %H:%MZ")


def _runner_words(runner: str, lane, at: datetime, until: datetime, *, pipeline=None, file: bool = False) -> str:
    """An evidence line worded by Khipu from parsed fields, never a runner's
    own text: "project A runner: lane muse limited until 16:00Z"."""
    where = " (global.json)" if file else (f" (pipeline {pipeline})" if pipeline else "")
    return _redacted(f"{runner} runner: lane {lane or WITHHELD} limited until {_until_words(at, until)}{where}")


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
                    "written_by": by, "via": "runner", "at": at, "reason": None,
                    "evidence": _runner_words(name, line.get("lane"), at, until, pipeline=line.get("pipeline")),
                    "message_time": basis == "cli", "silent": status["silent"]})
    glob = cache.get("global") or {}
    sightings = cache.get("sightings") if isinstance(cache.get("sightings"), dict) else {}
    bound = _parse(events.get("first")) or _epoch(glob.get("mtime"))
    for lane, item in ((glob.get("data") or {}).get("limited") or {}).items():
        until = _epoch(item.get("until"))
        sight = sightings.get(lane) if isinstance(sightings.get(lane), dict) else {}
        sight_until = _epoch(sight.get("until"))
        at = _parse(sight.get("at")) if sight_until and until and _same_until(sight_until, until) else None
        if at is None and until is not None:
            at = max(_in_tail(events, lane, until), default=None) or bound
        if until is None or at is None or until <= at:
            continue  # an old limit the runner keeps on file; its time has passed
        if any(line["lane"] == lane and abs((_parse(line["until"]) - until).total_seconds()) <= 1 for line in lines):
            continue  # its LIMITED line is in the tail: that line is the evidence
        exact = float(item["until"]).is_integer() and until.second == 0
        out.append({"id": "r" + _sid(name, "global", lane, item.get("until")), "account": None, "runner": name,
                    "lane": lane, "state": "limited", "until": until, "basis": "cli" if exact else "estimated",
                    "scope": "all", "window": None, "used_pct": None, "source": "runner", "written_by": by,
                    "via": "runner", "at": at, "reason": None, "message_time": exact,
                    "evidence": _runner_words(name, lane, at, until, file=True),
                    "silent": status["silent"]})
    return out


# ---- the usage cache ----------------------------------------------------------------


def configured_keys(config: dict) -> set[str]:
    """The usage cache keys of the configured accounts: each valid
    ``usage_accounts`` entry, or with none set every harness's default
    login (what ``usage.configured_accounts`` reads, without its PATH
    lookup, so every caller gets the same set)."""
    from khipu import usage

    raw = config.get("usage_accounts")
    if not isinstance(raw, list) or not raw:
        return {f"{harness}|" for harness in usage.HARNESSES}
    out = set()
    for entry in raw:
        account = usage._account_from_entry(entry)
        if not account.get("invalid"):
            out.add(usage._key(account))
    return out


def usage_evidence(configured: set[str] | None = None) -> tuple[list[dict], dict]:
    """Evidence from Khipu's own usage cache (``khipu usage``), and a short
    status of it. A window at 100% or more closes its scope until its reset
    (basis cli); figures with headroom (every window under 100%) reopen it
    (basis checked); a window at 100% with no reset time ahead gives no
    evidence for its scope, never open; a probe that found the CLI not
    signed in or not installed is down. Figures Muse
    or Codex keep from their last turn are marked ``observed``. Only rows
    whose key is a configured account (``configured``) are read; any other
    row is skipped whole, so no key of its own becomes an account."""
    from khipu import usage

    rows = usage._load_cache()
    out: list[dict] = []
    newest: datetime | None = None
    by = {"kind": "probe"}
    accounts_read = 0
    for key, row in sorted(rows.items()):
        if key.partition("|")[0] not in usage.HARNESSES or (configured is not None and key not in configured):
            continue
        accounts_read += 1
        attempt = _parse(row.get("last_attempt_at") or row.get("probed_at"))
        error = None if row.get("ok") else (row.get("last_error") or row.get("error"))
        error = next((e for e in (usage.E_SIGNIN, usage.E_NOT_INSTALLED) if e == error), None)
        if error is not None and attempt is not None:
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
            groups.setdefault(safe_name(w.get("model")) or "all", []).append(w)
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
            base = {"account": key, "runner": None, "lane": None, "scope": scope, "source": "probe",
                    "written_by": by, "via": "probe", "at": at, "reason": None, "message_time": False,
                    "observed": observed}
            if full:
                reset, w = max(full, key=lambda item: item[0])
                name = safe_name(w.get("name")) or ""
                window = "weekly" if name.startswith("weekly") else name or None
                out.append(dict(base, id="p" + _sid(key, scope, "limited", _iso(at), _iso(reset)), state="limited",
                                until=reset, basis="cli", window=window, used_pct=_number(w.get("used_pct")),
                                evidence=_redacted(f"usage probe: {name or 'window'} at {_number(w.get('used_pct'))}%")))
            else:
                out.append(dict(base, id="p" + _sid(key, scope, "open", _iso(at)), state="open", until=None,
                                basis="checked", window=None, used_pct=top,
                                evidence=f"usage probe: highest window at {top}%"))
    return out, {"accounts": accounts_read, "newest": _iso(newest)}


# ---- the routing table (a pointer, never account state) ------------------------------


def read_table(path: str | None) -> dict | None:
    """The table's NOW block as a pointer: a hash of the block, the time
    its ``updated:`` line gives, and the file's modification time. No text
    of the table is kept, the ``updated:`` line's included."""
    if not path:
        return None
    out: dict[str, Any] = {"path": path, "hash": None, "stamped_at": None, "changed_at": None, "error": None}
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
    if prev_cache.get("redaction") != REDACTION:
        prev_cache = {}
    lane_map = cfg.get("routing_lane_map") or {}
    runners: list[dict] = []
    cache: dict[str, Any] = {"runners": {}, "redaction": REDACTION}
    evidence: list[dict] = []
    for entry in entries:
        status, kept = read_runner(entry, now, (prev_cache.get("runners") or {}).get(entry["name"]),
                                   cfg.get("routing_runner_silent_intervals") or 0,
                                   cfg.get("routing_runner_tail_bytes", TAIL_BYTES),
                                   cfg.get("routing_runner_limited_lines", KEEP_LINES["LIMITED"]))
        mapped = lane_map.get(entry["name"]) or {}
        status["unmatched_lanes"] = [lane for lane in status["lanes"] if lane not in mapped]
        runners.append(status)
        cache["runners"][entry["name"]] = kept
        evidence += runner_evidence(status, kept)
    usage_items, usage_status = usage_evidence(configured_keys(config))
    evidence += usage_items
    status = {"runners": runners, "refused": refused, "invalid": invalid, "usage": usage_status,
              "table": read_table(cfg.get("routing_table"))}
    return evidence, status, cache


# ---- doctor's rule checks (read from config.json and ADDED lines only) ------------------


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
    passing), with the runner it was judged on and which of its files
    (``settings`` or ``events``) so the app can name both."""
    watch = watched(rules)
    if not watch:
        return []
    out: list[dict] = []
    for name, kept in sorted(((cache or {}).get("runners") or {}).items()):
        config = (kept.get("config") or {}).get("data") or {}
        # When Khipu first saw what a check is judged on (the app says "seen
        # at 11:22 AM"): the first read of the settings file as it reads now
        # (an older cache without that time falls back to its last good
        # read), or an ADDED line's own time.
        config_kept = kept.get("config") or {}
        config_at = config_kept.get("seen_at") or config_kept.get("good_at")
        events_at = (kept.get("events") or {}).get("good_at")
        on_settings = {"at": config_at, "runner": name, "file": "settings"}
        lanes = _config_facts(config.get("lanes"))
        for lane, spec in sorted(lanes.items()):
            kind = (spec.get("kind") or "").lower()
            script = spec.get("script") or "the launch command"
            if "fast_tier" in watch:
                if spec.get("fast"):
                    out.append({"rule": watch["fast_tier"], "status": "broken", **on_settings,
                                "text": f"A standing rule may be broken: {name}'s settings name a model ending in "
                                        f"-fast for its {lane} lane."})
                elif not spec.get("model_in_launch"):
                    out.append({"rule": watch["fast_tier"], "status": "cannot_check", **on_settings,
                                "text": f"{name}'s {lane} lane: can't be checked: the model is set inside "
                                        f"{script}"})
            if "paid_xai" in watch and kind.startswith("grok"):
                out.append({"rule": watch["paid_xai"], "status": "cannot_check", **on_settings,
                            "text": f"{name}'s {lane} lane: whether it uses the paid xAI API can't be checked: "
                                    f"the launch environment is set inside {script}"})
            if "grok_confirm" in watch and kind == "grokcheck":
                confirm = spec.get("confirm_lane")
                other = lanes.get(confirm) if confirm else None
                other_kind = ((other or {}).get("kind") or "").lower()
                if other is None or other_kind.startswith("grok"):  # none named, or a lane the settings lack
                    out.append({"rule": watch["grok_confirm"], "status": "broken", **on_settings,
                                "text": f"A standing rule may be broken: {name}'s Grok check lane has no "
                                        "confirming lane from another model family."})
                elif not other_kind:
                    out.append({"rule": watch["grok_confirm"], "status": "cannot_check", **on_settings,
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
                                "at": added.get("at") or events_at, "runner": name, "file": "events",
                                "text": f"A standing rule may be broken: a Grok lane is set to work on "
                                        f"{added.get('pipeline')}, which is marked private."})
    return out


# Which lanes each check reads, and whether it could judge one (the same
# tests ``rule_checks`` applies, so a lane counted as checked here is one it
# could have reported as broken).
def _check_lanes(check: str, lanes: dict) -> tuple[int, int]:
    """(lanes this check is about, of those the ones it could judge)."""
    about = judged = 0
    for spec in lanes.values():
        kind = (spec.get("kind") or "").lower()
        if check == "fast_tier":
            about += 1
            judged += bool(spec.get("fast") or spec.get("model_in_launch"))
        elif check == "paid_xai" and kind.startswith("grok"):
            about += 1  # the launch environment is never read
        elif check == "grok_confirm" and kind == "grokcheck":
            about += 1
            other = lanes.get(spec.get("confirm_lane")) if spec.get("confirm_lane") else None
            judged += other is None or bool((other or {}).get("kind"))
        elif check == "grok_private" and kind.startswith("grok"):
            about += 1
            judged += 1
    return about, judged


# The worst outcome first: a rule's result is its worst check's, so it is
# "ok" only when every check it asks for ran and passed.
_RESULT_ORDER = ("broken", "stale", "unreadable", "no_runner", "cannot_check", "no_lane", "ok")


def _source(name: str, kept: dict, now: datetime | None, stale_after: float) -> dict:
    """One runner file's freshness: when it was last read well, whether the
    latest read failed (``since`` set by ``read_runner``), and whether that
    good read is older than ``stale_after`` seconds."""
    good = _parse(kept.get("good_at"))
    stale = bool(now and stale_after and (good is None or (now - good).total_seconds() > stale_after))
    return {"runner": name, "good_at": kept.get("good_at"), "failed": bool(kept.get("since")),
            "since": kept.get("since"), "stale": stale}


def _one_check(check: str, n, runners: list, checks: list[dict], now, stale_after: float) -> dict:
    about = judged = read = 0
    sources: list[dict] = []
    for name, kept in runners:
        config_kept = kept.get("config") or {}
        config = config_kept.get("data")
        if not isinstance(config, dict):
            continue
        read += 1
        a, j = _check_lanes(check, _config_facts(config.get("lanes")))
        if not a:
            continue
        # The file the check judges: the ADDED lines for Grok on private
        # work, the settings for the others.
        kept_file = (kept.get("events") or {}) if check == "grok_private" else config_kept
        if check == "grok_private" and not isinstance(kept_file.get("data"), dict):
            j = 0  # its ADDED lines were never read
        src = _source(name, kept_file, now, stale_after)
        sources.append(src)
        about += a
        if not (src["failed"] or src["stale"]):  # a kept read from a failed file is not a check that ran
            judged += j
    mine = [c for c in checks if c.get("rule") == n and c.get("runner") not in
            {s["runner"] for s in sources if s["failed"] or s["stale"]}]
    broken = [c for c in checks if c.get("rule") == n and c.get("status") == "broken"]
    if broken:
        result = "broken"
    elif not runners:
        result = "no_runner"
    elif not read:
        result = "unreadable"
    elif any(s["failed"] or s["stale"] for s in sources):
        result = "stale"
    elif not about:
        result = "no_lane"
    elif judged:
        result = "ok"
    else:
        result = "cannot_check"
    # Coverage is as old as its oldest source, never dated by the newest.
    times = [s["good_at"] for s in sources if s["good_at"]]
    return {"check": check, "result": result, "at": min(times) if result == "ok" and times else None,
            "lanes": judged, "unchecked": about - judged, "sources": sources,
            "text": broken[0]["text"] if broken else None, "more": max(len(broken) - 1, 0),
            "why": next((c["text"] for c in mine if c.get("status") == "cannot_check"), None)}


def rule_results(cache: dict, rules: list[dict], checks: list[dict] | None = None, *,
                 now: datetime | None = None, stale_after: float = 0) -> list[dict]:
    """One result per rule Khipu has a check for, for the app's rule rows,
    with every check the rule asks for in ``checks`` (each with its own
    ``sources``: each runner file's last good read, failure and staleness).
    The rule's ``result`` is its worst check's:

    - ``broken``: a check found the rule may be broken (``text`` says how);
    - ``stale``: a file a check judges failed to read or was last read longer
      ago than ``stale_after``, so that check has not run on it;
    - ``unreadable``: folders added but no settings file was ever read;
    - ``no_runner``: no job runner folder added, so nothing to read;
    - ``cannot_check``: every lane a check is about is set where Khipu can't see;
    - ``no_lane``: no job runner has a lane the check is about;
    - ``ok``: every check ran on fresh reads and found nothing; ``at`` is the
      oldest of those reads; ``unchecked`` lanes it couldn't judge.

    "Nothing wrong found" is only ever ``ok``."""
    watch = watched(rules)
    if not watch:
        return []
    checks = rule_checks(cache, rules) if checks is None else checks
    runners = sorted(((cache or {}).get("runners") or {}).items())
    by_rule: dict[int, list[dict]] = {}
    for check, n in watch.items():
        by_rule.setdefault(n, []).append(_one_check(check, n, runners, checks, now, stale_after))
    out = []
    for n, found in by_rule.items():
        found.sort(key=lambda r: _RESULT_ORDER.index(r["result"]))
        worst = found[0]
        at = min(r["at"] for r in found) if all(r["result"] == "ok" for r in found) else None
        out.append({k: v for k, v in worst.items() if k != "sources"} | {"rule": n, "at": at, "checks": found})
    return sorted(out, key=lambda r: r["rule"])


def launches(cache: dict) -> list[dict]:
    """Every LAUNCH line kept, with its runner."""
    out = []
    for name, kept in sorted(((cache or {}).get("runners") or {}).items()):
        events = (kept.get("events") or {}).get("data") or {}
        out += [{"runner": name, "lane": item["lane"], "at": item["at"]} for item in events.get("launches") or []]
    return out
