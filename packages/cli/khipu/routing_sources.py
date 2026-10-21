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
# Lanes read from one config.json. Their facts are a few short fields each
# and the file is already bounded by JSON_MAX; a lane past this is counted
# (``skipped``) so a rule check says it was not fully checked.
LANES_MAX = 1024
# Budget declarations a runner's config may carry (slice 19b): accounts and
# caps per account. Bounded like the lanes, and a declaration past the bound
# is counted, never silently dropped.
BUDGET_ACCOUNTS_MAX = 64
BUDGET_DECLARATIONS_MAX = 32
LAUNCH_KEEP = timedelta(days=2)
# A heartbeat interval longer than this is not a heartbeat (the runners write
# 20 and 30 seconds); the file is reported as out of range.
INTERVAL_MAX = 24 * 3600
# Runner telemetry (slice 17t): how much of one heartbeat's records is kept,
# and how many spend events one runner's accumulation keeps. Spend past the
# keep is dropped and marked incomplete, so a total that needs it reads
# unknown, never a smaller invented figure.
TELEMETRY_ACTIVE_MAX = 256
TELEMETRY_SPEND_MAX = 4096
TELEMETRY_COVERAGE_MAX = 512
TELEMETRY_SPEND_KEEP = 4096
TELEMETRY_COVERAGE_KEEP = 1024
TELEMETRY_USD_MAX = 1_000_000_000.0
_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_UUID_FULL = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

_STAMPED = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s")
_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+(\S+)\s+(ADDED|LIMITED|LAUNCH|HOLD|ERROR|FAILED|FAIL)\b[ \t]*(.*)$")
_CURSOR_LIMIT = re.compile(r"\busage limit\b|\brate limit\b|\bquota\b", re.I)
_FIELD = re.compile(r"(?:^|\s)([a-z_]+)=(\S+)")
_LANE_FIELD = re.compile(r"(?:^|\s)lane=([^\s:]+)")
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
# 6: telemetry spend persisted as aggregates, event ids only as hashes.
REDACTION = 6


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
    from khipu import session_capture

    # Capture state is replaced atomically; the directory changes on new
    # attachments, updates and Forget, without reading a session's contents.
    capture = _stat(session_capture.state_dir()) if entries else None
    return {"captures": capture, "runners": runners, "usage": _stat(usage.cache_file()), "table": _stat(Path(table)) if table else None,
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
    out = {"time": _number(doc["time"]), "interval": interval}
    lanes = doc.get("lanes")
    if isinstance(lanes, dict) or any(k in doc for k in ("active", "spend", "spend_coverage")):
        from khipu import routing

        known = routing.accounts()
        if isinstance(lanes, dict):
            out["lanes"] = {lane: routing.login_binding(spec, known)
                            for lane, spec in list(lanes.items())[:LANES_MAX]
                            if safe_name(lane) == lane and isinstance(spec, dict)}
        out["telemetry"] = _parse_telemetry(doc, known)
    else:
        # Every heartbeat is a telemetry occasion: one with none of the
        # telemetry keys still replaces (expires) the active snapshot.
        out["telemetry"] = _parse_telemetry(doc, [])
    return out


def _declared_account(value, known: list[dict]):
    """A telemetry record's account declaration, kept only in a form the
    lane resolver can name: a known account key, a ``provider|`` prefix, a
    stable ``provider:label`` id, or a name that passes ``safe_name``.
    Anything else (a login path that is no known key, free text) becomes
    ``WITHHELD``, which resolves to no account and stays unattributed."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value in {a["key"] for a in known}:
        return value
    if value.endswith("|") and safe_name(value[:-1]) not in (None, WITHHELD):
        return value
    if value.count(":") == 1 and all(safe_name(p) not in (None, WITHHELD) for p in value.split(":")):
        return value
    return safe_name(value) or WITHHELD


def _telemetry_provider(value):
    name = safe_name(value)
    return name if name not in (None, WITHHELD) else None


def _event_id(value) -> str | None:
    """A spend event id kept only in a safe shape: a uuid, or a short
    name-shaped id that does not look like a key (the same withholding
    ``safe_name`` applies to other fields, uuids allowed because the
    contract mints them). Anything else is dropped, never repaired."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if _UUID_FULL.match(text):
        return text
    if not _EVENT_ID.match(text) or _KEY_PREFIX.search(text) or _UUID.search(text):
        return None
    if any(re.search(r"[A-Za-z]", run) and re.search(r"[0-9]", run) for run in _KEY_RUN.findall(text)):
        return None
    return text


def _event_token(event: str) -> str:
    """The persisted form of an event id: a hash, so a raw id (which may
    be key-shaped in a hostile heartbeat) never reaches view.json, while
    equality (dedup, correction targets) still works."""
    return hashlib.sha256(event.encode("utf-8")).hexdigest()[:24]


def _parse_telemetry(doc: dict, known: list[dict]) -> dict:
    """The heartbeat's telemetry records, typed and bounded. Only parsed
    fields survive: names through ``safe_name``, account declarations
    through ``_declared_account``, money through ``_number`` and bounded
    by ``TELEMETRY_USD_MAX``, event ids through ``_event_id`` and kept
    only as hashes. A record that fails its shape is dropped and counted,
    never repaired; a dropped spend record is remembered (its account and
    time, when parseable) so the runner's stream reads incomplete.
    Record times are bounded to the heartbeat's own time (arrival), so a
    future-dated record cannot wedge a lane or extend coverage."""
    beat_at = _epoch(doc.get("time"))
    out: dict[str, Any] = {"active": [], "spend": [], "coverage": [], "dropped": 0, "truncated": False,
                           "snapshot": True, "spend_dropped": 0, "spend_dropped_records": []}

    def bounded(at: datetime | None) -> datetime | None:
        return min(at, beat_at) if at is not None and beat_at is not None else at

    # Account and provider declarations repeat across a heartbeat's
    # records; each distinct value is resolved once per heartbeat.
    account_cache: dict[str, Any] = {}
    provider_cache: dict[str, Any] = {}

    def account_of(value):
        if isinstance(value, str) and value in account_cache:
            return account_cache[value]
        resolved = _declared_account(value, known)
        if isinstance(value, str):
            account_cache[value] = resolved
        return resolved

    def provider_of(value):
        if isinstance(value, str) and value in provider_cache:
            return provider_cache[value]
        resolved = _telemetry_provider(value)
        if isinstance(value, str):
            provider_cache[value] = resolved
        return resolved

    def drop_spend(item) -> None:
        out["dropped"] += 1
        out["spend_dropped"] += 1
        if isinstance(item, dict):
            at = bounded(_epoch(item.get("at")))
            out["spend_dropped_records"].append(
                {"account": account_of(item.get("account")),
                 "provider": provider_of(item.get("provider")),
                 "at": at.timestamp() if at is not None else None})

    active = doc.get("active")
    if isinstance(active, list):
        out["dropped"] += max(0, len(active) - TELEMETRY_ACTIVE_MAX)
        for item in active[:TELEMETRY_ACTIVE_MAX]:
            lane = safe_name(item.get("lane")) if isinstance(item, dict) else None
            jobs = item.get("jobs") if isinstance(item, dict) else None
            at = bounded(_epoch(item.get("at"))) if isinstance(item, dict) else None
            if (lane is None or at is None or isinstance(jobs, bool) or not isinstance(jobs, int)
                    or not 0 <= jobs <= 100000 or item.get("unit", "job") != "job"):
                out["dropped"] += 1
                continue
            out["active"].append({"lane": lane, "account": account_of(item.get("account")),
                                  "provider": provider_of(item.get("provider")),
                                  "jobs": jobs, "unit": "job", "at": at.timestamp()})
    spend = doc.get("spend")
    if isinstance(spend, list):
        records: list[dict] = []
        for item in spend:
            event = _event_id(item.get("event")) if isinstance(item, dict) else None
            usd = _number(item.get("usd")) if isinstance(item, dict) else None
            at = bounded(_epoch(item.get("at"))) if isinstance(item, dict) else None
            raw_corrects = item.get("corrects") if isinstance(item, dict) else None
            corrects = _event_id(raw_corrects) if raw_corrects is not None else None
            if (event is None or usd is None or abs(usd) > TELEMETRY_USD_MAX or at is None
                    or (raw_corrects is not None and corrects is None)
                    or (usd < 0 and corrects is None)):
                drop_spend(item)
                continue
            records.append({
                "event": _event_token(event),
                "provider": provider_of(item.get("provider")),
                "account": account_of(item.get("account")),
                "usd": usd, "at": at.timestamp(),
                "corrects": _event_token(corrects) if corrects is not None else None})
        if len(records) > TELEMETRY_SPEND_MAX:
            records.sort(key=lambda r: (r["at"], r["event"]))
            for record in records[:-TELEMETRY_SPEND_MAX]:
                drop_spend({"account": record["account"], "provider": record["provider"], "at": record["at"]})
            out["truncated"] = True
            records = records[-TELEMETRY_SPEND_MAX:]
        out["spend"] = records
    coverage = doc.get("spend_coverage")
    if isinstance(coverage, list):
        out["dropped"] += max(0, len(coverage) - TELEMETRY_COVERAGE_MAX)
        for item in coverage[:TELEMETRY_COVERAGE_MAX]:
            start = _epoch(item.get("from")) if isinstance(item, dict) else None
            end = bounded(_epoch(item.get("to"))) if isinstance(item, dict) else None
            if start is None or end is None or start > end:
                out["dropped"] += 1
                continue
            out["coverage"].append({"account": account_of(item.get("account")),
                                    "provider": provider_of(item.get("provider")),
                                    "from": start.timestamp(), "to": end.timestamp()})
    return out


def _telemetry_event_count(tele: dict) -> int:
    """Accumulated spend events: aggregated plain events plus corrections."""
    tele = tele if isinstance(tele, dict) else {}
    return (sum(b.get("n") or 0 for b in tele.get("buckets") or [] if isinstance(b, dict))
            + len([c for c in tele.get("corrections") or [] if isinstance(c, dict)]))


def merge_telemetry(prev, parsed, now: datetime | None = None) -> dict:
    """One runner's accumulated telemetry, in the form persisted in the
    view: plain spend aggregated into one bucket per (account declaration,
    provider, second) holding the summed usd, the event count and the
    hashed event ids (so dedup and correction targets survive a reload
    without persisting every event); corrections kept individually, since
    each counts only against its own target; the current ``active``
    snapshot, which each heartbeat replaces whole (an empty or missing
    snapshot expires the old jobs, never keeps them); and every
    coverage interval vouched so far. Deduplication is per runner, on the
    hashed event id: the first seen wins, so a heartbeat that republishes
    its recent spend never double-counts, and two runners minting the same
    id never merge. Spend dropped for any reason (malformed, truncated,
    past the keep) marks the accumulation incomplete.

    ``since`` is when Khipu started accumulating this runner (the first
    merge with no kept accumulation, dated by the fold's clock): it is
    kept across merges and reset whenever the kept accumulation is gone
    (a cache discarded at ``collect`` or a lost view.json both reach this
    merge with no ``prev``), so a period that starts before it can never
    read as a known, understated total."""
    prev = prev if isinstance(prev, dict) else {}
    parsed = parsed if isinstance(parsed, dict) else {}
    since = _number(prev.get("since"))
    if _epoch(since) is None:
        since = now.timestamp() if now is not None else None
    buckets: dict[tuple, dict] = {}
    for item in prev.get("buckets") or []:
        if isinstance(item, dict) and _epoch(item.get("at")) is not None:
            key = (item.get("a"), item.get("p"), _number(item.get("at")))
            bucket = buckets.setdefault(key, {"a": item.get("a"), "p": item.get("p"), "at": _number(item.get("at")),
                                              "usd": 0.0, "n": 0, "ids": set()})
            bucket["usd"] += _number(item.get("usd")) or 0.0
            bucket["n"] += item.get("n") if isinstance(item.get("n"), int) else 0
            bucket["ids"] |= {t for t in str(item.get("ids") or "").split(",") if t}
    corrections: dict[str, dict] = {}
    for item in prev.get("corrections") or []:
        if isinstance(item, dict) and isinstance(item.get("event"), str) and _epoch(item.get("at")) is not None:
            corrections[item["event"]] = item
    dropped = [d for d in prev.get("dropped_spend") or [] if isinstance(d, dict)]
    active: dict[str, dict] = {}
    if isinstance(prev.get("active"), dict):
        active = {k: v for k, v in prev["active"].items() if isinstance(v, dict) and isinstance(k, str)}
    coverage = [c for c in prev.get("coverage") or [] if isinstance(c, dict)]
    incomplete = bool(prev.get("incomplete"))
    if parsed:
        incomplete = incomplete or bool(parsed.get("truncated")) or (parsed.get("spend_dropped") or 0) > 0
        dropped += [d for d in parsed.get("spend_dropped_records") or [] if isinstance(d, dict)]
        seen = set(corrections) | {t for b in buckets.values() for t in b["ids"]}
        for record in parsed.get("spend") or []:
            if not isinstance(record, dict) or not isinstance(record.get("event"), str):
                continue
            token, usd, at = record["event"], _number(record.get("usd")), _number(record.get("at"))
            if token in seen or usd is None or at is None:
                continue
            seen.add(token)
            if record.get("corrects"):
                corrections[token] = {"event": token, "corrects": record["corrects"],
                                      "account": record.get("account"), "provider": record.get("provider"),
                                      "usd": usd, "at": at}
            else:
                key = (record.get("account"), record.get("provider"), at)
                bucket = buckets.setdefault(key, {"a": key[0], "p": key[1], "at": at,
                                                  "usd": 0.0, "n": 0, "ids": set()})
                bucket["usd"] += usd
                bucket["n"] += 1
                bucket["ids"].add(token)
        if sum(b["n"] for b in buckets.values()) > TELEMETRY_SPEND_KEEP:
            for key in sorted(buckets, key=lambda k: k[2] or 0):
                if sum(b["n"] for b in buckets.values()) <= TELEMETRY_SPEND_KEEP:
                    break
                buckets.pop(key, None)
            incomplete = True
        if len(corrections) > TELEMETRY_SPEND_KEEP:
            for token in sorted(corrections, key=lambda t: corrections[t].get("at") or 0)[
                    :len(corrections) - TELEMETRY_SPEND_KEEP]:
                corrections.pop(token, None)
            incomplete = True
        if len(dropped) > 128:
            dropped = sorted(dropped, key=lambda d: _number(d.get("at")) or 0)[-128:]
        if parsed.get("snapshot") or "active" in parsed:
            active = {}
            for record in parsed.get("active") or []:
                if isinstance(record, dict) and isinstance(record.get("lane"), str):
                    active[record["lane"]] = record
        seen_marks = {(c.get("account"), c.get("provider"), c.get("from"), c.get("to")) for c in coverage}
        for record in parsed.get("coverage") or []:
            if isinstance(record, dict):
                mark = (record.get("account"), record.get("provider"), record.get("from"), record.get("to"))
                if mark not in seen_marks:
                    seen_marks.add(mark)
                    coverage.append(record)
        if len(coverage) > TELEMETRY_COVERAGE_KEEP:
            coverage = sorted(coverage, key=lambda c: _number(c.get("to")) or 0)[-TELEMETRY_COVERAGE_KEEP:]
            incomplete = True
    out_buckets = [{"a": b["a"], "p": b["p"], "at": b["at"], "usd": b["usd"], "n": b["n"],
                    "ids": ",".join(sorted(b["ids"]))}
                   for b in sorted(buckets.values(), key=lambda b: b["at"] or 0)]
    return {"buckets": out_buckets,
            "corrections": sorted(corrections.values(), key=lambda c: c.get("at") or 0),
            "dropped_spend": dropped, "active": active, "coverage": coverage, "incomplete": incomplete,
            "since": since}


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
    # Every argument: they are only scanned here, never kept.
    args = [a.strip() for a in launch if isinstance(a, str)] if isinstance(launch, list) else []
    model = model.strip() if isinstance(model, str) else ""
    return {"fast": any(t.endswith("-fast") for t in [model, *args]),
            "model_in_launch": bool(model) and model in args,
            "script": _script_name(args)}


def model_family(model) -> str:
    """The model's family, independent of the transport and paying account."""
    name = str(model or "").casefold()
    for prefixes, family in ((("grok-",), "xAI"), (("claude-", "opus", "sonnet", "haiku"), "Anthropic"),
                             (("gpt-", "codex", "o1", "o3", "o4"), "OpenAI"), (("glm-",), "Zhipu"),
                             (("deepseek-",), "DeepSeek"), (("gemini-",), "Google"), (("muse-",), "Meta")):
        if name.startswith(prefixes):
            return family
    return "unknown"


def lane_identity(spec: dict) -> dict:
    """Keep safe declarations unresolved so new owner facts can resolve them."""
    from khipu import routing

    out = {k: safe_name(spec.get(k)) for k in ("kind", "provider", "model") if spec.get(k) is not None}
    if spec.get("identity_invalid") or any(v is None for v in out.values()):
        out["identity_invalid"] = True
    known = None
    for field in ("account", "budget_account"):
        if field in spec:
            known = routing.accounts() if known is None else known
            value = spec[field]
            key = routing._lane_target(value, known) if isinstance(value, str) else None
            if key in {a["key"] for a in known} and isinstance(value, str) and ("/" in value or "\\" in value):
                out[field] = key
            elif isinstance(value, str) and value.endswith("|") and safe_name(value[:-1]) not in (None, WITHHELD):
                out[field] = value
            elif isinstance(value, str) and value.count(":") == 1 and all(
                    safe_name(part) not in (None, WITHHELD) for part in value.split(":")):
                out[field] = value
            elif key in {a["key"] for a in known} and safe_name(value) in (None, WITHHELD):
                out[field] = key
            else:
                out[field] = safe_name(value) or WITHHELD
    known = routing.accounts() if known is None else known
    local = routing.login_binding(spec, known)
    if local:
        out.update(local)
    elif "login_account" in spec:
        key = spec["login_account"]
        out["login_account"] = key if key in {a["key"] for a in known} else None
        if out["login_account"] is None:
            out["identity_invalid"] = True
    return out


def _budget_declaration(item) -> dict | None:
    """One typed budget cap a runner's config declares, or None. The values
    pass the same check an owner cap does (``routing_pace.checked_cap``),
    and a field the kind does not use refuses the declaration, never drops
    it silently. A window belongs only to the window-scoped kinds."""
    from khipu import routing_pace

    if not isinstance(item, dict):
        return None
    kind = item.get("kind")
    if kind not in routing_pace.CAP_KINDS:
        return None
    if set(item) - {"kind", "window", "pct", "slack", "usd", "days", "n"}:
        return None
    window = item.get("window")
    if kind in routing_pace.WINDOW_CAP_KINDS:
        if window not in routing_pace.WINDOWS:
            return None
    elif window is not None:
        return None
    fields = {k: item.get(k) for k in ("pct", "slack", "usd", "days", "n") if item.get(k) is not None}
    cap = routing_pace.checked_cap({"kind": kind, "revision": "declared", **fields})
    if cap is None:
        return None
    out = {k: cap[k] for k in ("kind", "pct", "slack", "usd", "days", "n") if k in cap}
    if kind in routing_pace.WINDOW_CAP_KINDS:
        out["window"] = window
    return out


def _parse_budget(value, known: list[dict]) -> tuple[dict, int]:
    """A runner's ``budget`` object: account key -> a list of cap
    declarations. Only a known account key, a ``provider|`` prefix, a stable
    id or a shape-checked name is kept (``_declared_account``); a secret-
    shaped or unusable account is counted, never repaired. ``(budget,
    skipped)``."""
    if value is None:
        return {}, 0
    if not isinstance(value, dict):
        return {}, 1
    out: dict[str, list[dict]] = {}
    skipped = 0
    items = list(value.items())
    skipped += max(0, len(items) - BUDGET_ACCOUNTS_MAX)
    for raw_account, declarations in items[:BUDGET_ACCOUNTS_MAX]:
        account = _declared_account(raw_account, known)
        if account is None or account == WITHHELD or not isinstance(declarations, list):
            skipped += 1
            continue
        skipped += max(0, len(declarations) - BUDGET_DECLARATIONS_MAX)
        kept = []
        for item in declarations[:BUDGET_DECLARATIONS_MAX]:
            declaration = _budget_declaration(item)
            if declaration is None:
                skipped += 1
                continue
            kept.append(declaration)
        if kept:
            out.setdefault(account, []).extend(kept)
    return out, skipped


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
                          "confirm_lane": safe_name(spec.get("confirm_lane")), **facts, **lane_identity(spec)}
    return out


def _config_budget(budget) -> dict:
    """A budget kept by an older view, rebuilt through the same typed check
    as a fresh read. A declaration that no longer checks is dropped."""
    rebuilt: dict[str, list[dict]] = {}
    for account, declarations in (budget.items() if isinstance(budget, dict) else ()):
        if not isinstance(account, str) or not account or not isinstance(declarations, list):
            continue
        kept = [declaration for declaration in
                (_budget_declaration(item) for item in declarations if isinstance(item, dict))
                if declaration is not None]
        if kept:
            rebuilt[account] = kept
    return rebuilt


def _parse_config(data: bytes) -> dict:
    """Only the named keys: each lane's kind, model and confirm_lane, the
    facts its launch arguments give (``launch_facts``: never the arguments),
    defaults.check_lane and defaults.fix_lanes, and the typed ``budget``
    declarations (slice 19b: account -> caps). Nothing else in config.json
    is kept."""
    doc = json.loads(data.decode("utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("lanes"), dict):
        raise ValueError("no lanes")
    lanes = {}
    skipped: dict[str, int] = {}
    items = list(doc["lanes"].items())
    if len(items) > LANES_MAX:
        skipped["lanes"] = len(items) - LANES_MAX
    for lane, spec in items[:LANES_MAX]:
        lane_name = safe_name(lane)
        # A lane Khipu can't name, or whose withheld name another already
        # took, is left out of every check: counted, never silently dropped.
        if lane_name is None or not isinstance(spec, dict) or lane_name in lanes:
            skipped["lanes"] = skipped.get("lanes", 0) + 1
            continue
        if safe_name(spec.get("kind")) == WITHHELD:  # can't tell whether it is a Grok lane
            skipped["kinds"] = skipped.get("kinds", 0) + 1
        lanes[lane_name] = {
            "kind": safe_name(spec.get("kind")), "model": safe_name(spec.get("model")),
            "confirm_lane": safe_name(spec.get("confirm_lane")),
            **launch_facts(spec.get("model"), spec.get("launch")),
            **lane_identity(spec),
        }
    defaults = doc.get("defaults") if isinstance(doc.get("defaults"), dict) else {}
    fix = defaults.get("fix_lanes")
    fix_lanes = [n for n in (safe_name(x) for x in fix) if n] if isinstance(fix, list) else []
    if isinstance(fix, list) and len(fix_lanes) < len(fix):
        skipped["fix_lanes"] = len(fix) - len(fix_lanes)
    from khipu import routing

    budget, budget_skipped = _parse_budget(doc.get("budget"), routing.accounts())
    if budget_skipped:
        skipped["budget"] = budget_skipped
    out = {"lanes": lanes, "check_lane": safe_name(defaults.get("check_lane")), "fix_lanes": fix_lanes}
    if budget:
        out["budget"] = budget
    return dict(out, skipped=skipped) if skipped else out


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
        lane_field = _LANE_FIELD.search(rest)
        fields["lane"] = lane_field.group(1) if lane_field else None
        if kind == "HOLD":
            holds += 1
        elif kind == "LIMITED" or (kind in ("ERROR", "FAILED", "FAIL")
                                   and (fields.get("lane") or "").startswith("cursor-")
                                   and _CURSOR_LIMIT.search(rest)):
            lane, until = safe_name(fields.get("lane")), _parse(fields.get("until"))
            # LIMITED lines carry the runner's until. Cursor error lines
            # without one use only the report's default window, as other lanes do.
            if kind != "LIMITED" and lane and until is None:
                until = at + timedelta(seconds=GUESS_SECONDS)
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
                out["LAUNCH"].append({"at": _iso(at), "pipeline": pipeline, "lane": lane,
                    **({"attachment_ref": _sid(fields["harness"], fields["session"])}
                       if fields.get("harness") in {"claude_code", "codex", "aegis"} and fields.get("session") else {})})
    for kind, cap in dict(KEEP_LINES, **(keep or {})).items():
        if cap:
            out[kind] = out[kind][-cap:]
    return {"limited_lines": out["LIMITED"], "added": out["ADDED"], "launches": out["LAUNCH"],
            "holds": holds, "first": _iso(first),
            "matched": {lane: {"until": _iso(limits[lane]), "at": _iso(at)} for lane, at in matched.items()}}


# The privacy check reads the whole events log, not its tail: once from the
# start, a chunk per refresh, then only what was added. The cursor names the
# file (device and inode), how far it has read, and a hash of its start (a
# file rewritten in place); the private ADDED lines found are kept with it.
SCAN_CHUNK = 2 * 1024 * 1024
SCAN_HEAD = 4096
PRIVATE_MAX = 5000


def _read_range(path: Path, start: int, length: int) -> bytes:
    """``length`` bytes of one runner file from ``start``, opened as
    ``read_only`` opens it."""
    if private_path(path):
        raise PermissionError("inside a private folder")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        os.lseek(fd, start, os.SEEK_SET)
        chunks, got = [], 0
        while got < length:
            chunk = os.read(fd, min(65536, length - got))
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


# A last line still being written is left for the next read; one that stays
# unfinished and unchanged this long is counted as unreadable.
UNFINISHED_AFTER = 600


def _valid_cursor(cursor, st) -> bool:
    ints = ("offset", "size", "mtime_ns", "lines", "dropped", "head_len", "tail_len")
    return (isinstance(cursor, dict) and cursor.get("dev") == st.st_dev and cursor.get("ino") == st.st_ino
            and all(type(cursor.get(k)) is int for k in ints)
            and 0 <= cursor["offset"] <= cursor["size"] <= st.st_size
            and isinstance(cursor.get("head"), str) and isinstance(cursor.get("tail"), str)
            and isinstance(cursor.get("private"), list))


def _hash_range(path: Path, start: int, length: int) -> str:
    return hashlib.sha256(_read_range(path, start, length)).hexdigest() if length else ""


def _fingerprints(path: Path, offset: int) -> dict:
    """The file's first 4 KB and the 4 KB just before ``offset``, hashed."""
    head_len, tail_len = min(offset, SCAN_HEAD), min(offset, SCAN_HEAD)
    return {"head_len": head_len, "head": _hash_range(path, 0, head_len),
            "tail_len": tail_len, "tail": _hash_range(path, offset - tail_len, tail_len)}


def scan_events(path: Path, cursor, now: datetime) -> dict:
    """One step of the privacy check's read of the whole events log: from the
    cursor to at most ``SCAN_CHUNK`` bytes on, whole lines only.

    The log is taken as append-only only on evidence: the same file (device,
    inode), its first 4 KB and the 4 KB before the offset unchanged, and
    either the same size and mtime as at the last read or a larger size.
    Anything else (``rotated``, ``truncated``, ``rewritten``, or a missing or
    ``damaged`` cursor) starts again from the first byte and forgets what it
    held. ``done`` only when every byte up to the end of the file was read; a
    final line without its newline is left unread and ``unfinished_since``
    dates it, so ``unfinished`` is set once it has stayed so for
    ``UNFINISHED_AFTER`` seconds."""
    st = os.stat(path)
    reset = None
    if not isinstance(cursor, dict):
        reset = "new" if cursor is None else "damaged"
    elif (cursor.get("dev"), cursor.get("ino")) != (st.st_dev, st.st_ino):
        reset = "rotated"
    elif type(cursor.get("size")) is int and st.st_size < cursor["size"]:
        reset = "truncated"
    elif not _valid_cursor(cursor, st):
        reset = "damaged"
    else:
        same = st.st_size == cursor["size"] and st.st_mtime_ns == cursor["mtime_ns"]
        prints = _fingerprints(path, cursor["offset"])
        if not (same or st.st_size > cursor["size"]) or any(prints[k] != cursor[k] for k in prints):
            reset = "rewritten"
    if reset:
        cursor = {"dev": st.st_dev, "ino": st.st_ino, "offset": 0, "size": 0, "mtime_ns": 0, "private": [],
                  "lines": 0, "dropped": 0, "head_len": 0, "head": "", "tail_len": 0, "tail": ""}
    start = cursor["offset"]
    want = min(SCAN_CHUNK, st.st_size - start)
    data = _read_range(path, start, want)
    cut = data.rfind(b"\n")
    dropped = cursor["dropped"]
    if cut != -1:
        whole = data[:cut + 1]
    elif len(data) == SCAN_CHUNK:  # one line longer than a chunk: passed over, counted
        whole, cut, dropped = b"", len(data) - 1, dropped + 1
    else:
        whole = b""  # a last line still being written: read it next time
    found = [a for a in _parse_events(whole, now, {}, {"ADDED": 0})["added"] if a["private"]] if whole else []
    private = cursor["private"] + found
    if len(private) > PRIVATE_MAX:
        dropped += len(private) - PRIVATE_MAX
        private = private[-PRIVATE_MAX:]
    offset = start + cut + 1 if cut != -1 else start
    # Bytes after the offset at the end of the file: a final line with no
    # newline yet. Dated from when it was first seen as it is now.
    at_end = start + want >= st.st_size
    pending = at_end and offset < st.st_size
    unchanged = not reset and st.st_size == cursor["size"] and st.st_mtime_ns == cursor["mtime_ns"]
    since = cursor.get("unfinished_since") if pending and unchanged and cursor.get("unfinished_since") else (
        _iso(now) if pending else None)
    began = _parse(since)
    return {"dev": st.st_dev, "ino": st.st_ino, "offset": offset, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
            **_fingerprints(path, offset), "private": private, "lines": cursor["lines"] + whole.count(b"\n"),
            "dropped": dropped, "done": at_end and not pending, "reset": reset, "unfinished_since": since,
            "unfinished": bool(began and (now - began).total_seconds() > UNFINISHED_AFTER)}


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
            # The heartbeat's telemetry is kept once, accumulated, in
            # ``cache["telemetry"]``: the heartbeat read itself keeps only
            # its time, interval and lanes, so no spend record (or event
            # id) is persisted twice in view.json.
            stored, telemetry_parsed = parsed, None
            if key == "heartbeat" and isinstance(parsed, dict):
                telemetry_parsed = parsed.get("telemetry")
                stored = {k: v for k, v in parsed.items() if k != "telemetry"}
            # ``seen_at``: when Khipu first read the file as it reads now. A
            # re-read that finds the same content keeps it, so a check judged
            # on the file (``rule_checks``) is dated by when Khipu first saw
            # what it judges, not by the latest refresh.
            same = last.get("data") is not None and last.get("data") == stored and last.get("seen_at")
            cache[key] = {"data": stored, "good_at": _iso(now), "seen_at": last["seen_at"] if same else _iso(now),
                          "mtime": mtime, "since": None}
            if key == "heartbeat":
                cache["telemetry"] = merge_telemetry(prev.get("telemetry"), telemetry_parsed, now)
            if key == "events":
                cache[key]["scan"] = scan_events(path, last.get("scan"), now)
            files[key] = {"error": None, "good_at": _iso(now), "since": None}
        except (OSError, ValueError, UnicodeDecodeError, RecursionError, OverflowError, TypeError) as exc:
            since = last.get("since") or _iso(now)
            if last.get("data") is not None:
                cache[key] = dict(last, since=since)
                if key == "config" and isinstance(last["data"], dict):
                    rebuilt = dict(last["data"], lanes=_config_facts(last["data"].get("lanes")))
                    budget = _config_budget(last["data"].get("budget"))
                    if budget:
                        rebuilt["budget"] = budget
                    else:
                        rebuilt.pop("budget", None)
                    cache[key]["data"] = rebuilt
            else:
                cache[key] = {"data": None, "good_at": None, "mtime": None, "since": since}
            files[key] = {"error": _short_error(exc), "good_at": cache[key].get("good_at"), "since": since}
    if "telemetry" not in cache:
        cache["telemetry"] = merge_telemetry(prev.get("telemetry"), None, now)
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
        "telemetry": {"spend_events": _telemetry_event_count(cache["telemetry"]),
                      "active_lanes": len(cache["telemetry"]["active"]),
                      "coverage": len(cache["telemetry"]["coverage"]), "incomplete": cache["telemetry"]["incomplete"]},
    }
    return status, cache


def runner_budget_declarations(cache: dict) -> list[dict]:
    """Each typed budget declaration the registered runners' configs hold,
    flattened as ``{runner, account, kind, window, ...}`` for the comparison
    with Khipu's cap facts (slice 19b). Read-only: it consumes the parsed
    cache and opens nothing."""
    out: list[dict] = []
    runners = cache.get("runners") if isinstance(cache, dict) else None
    for name, kept in sorted((runners or {}).items()):
        data = ((kept or {}).get("config") or {}).get("data") if isinstance(kept, dict) else None
        budget = data.get("budget") if isinstance(data, dict) else None
        for account, declarations in (budget.items() if isinstance(budget, dict) else ()):
            if not isinstance(account, str) or not isinstance(declarations, list):
                continue
            for declaration in declarations:
                if isinstance(declaration, dict):
                    out.append({"runner": name, "account": account, **declaration})
    return out


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
    specs = ((cache.get("config") or {}).get("data") or {}).get("lanes") or {}
    for item in out:
        spec = specs.get(item["lane"]) or {}
        item.update(declaration=lane_identity(spec), model=spec.get("model"), family=model_family(spec.get("model")),
                    transport="cursor" if spec.get("kind") == "cursor" or spec.get("provider") == "cursor" else None)
    return out


# ---- runner telemetry totals (slice 17t) ---------------------------------------------
#
# Spend is money data: a total is known only on complete coverage, and a
# missing or partial stream reads unknown, never zero. Accounts never pool:
# every record is attributed to exactly one account through the lane
# resolver, or to no account (unattributed, never guessed).


def _telemetry_key(runner: str, lane, record: dict, lane_map: dict, known: list[dict],
                   providers: dict | None, specs: dict) -> str | None:
    from khipu import routing

    spec = dict(specs.get(lane) or {}) if lane else {}
    if record.get("account") is not None:
        spec["account"] = record["account"]
    if record.get("provider"):
        spec["provider"] = record["provider"]
    return routing.resolve_lane(runner, lane or "", lane_map, known, spec, providers)["key"]


def telemetry_streams(cache: dict, *, known: list[dict], lane_map: dict, providers: dict | None,
                      silent: dict[str, bool]) -> list[dict]:
    """Accumulated telemetry grouped into one stream per (runner, account):
    that runner's spend, coverage and current active snapshots for the
    account. ``account`` is None for records no lane resolution can name."""
    streams: dict[tuple[str, str | None], dict] = {}
    for runner, kept in sorted(((cache or {}).get("runners") or {}).items()):
        kept = kept if isinstance(kept, dict) else {}
        tele = kept.get("telemetry") if isinstance(kept.get("telemetry"), dict) else {}
        specs = ((kept.get("config") or {}).get("data") or {}).get("lanes") or {}

        def stream(key):
            return streams.setdefault((runner, key), {
                "runner": runner, "account": key, "silent": bool(silent.get(runner)),
                "incomplete": bool(tele.get("incomplete")), "buckets": [], "corrections": [], "dropped": [],
                "coverage": [], "active": [], "launches": [],
                "accumulating_since": _epoch(tele.get("since"))})

        for item in tele.get("buckets") or []:
            if isinstance(item, dict) and _epoch(item.get("at")) is not None:
                record = {"account": item.get("a"), "provider": item.get("p")}
                stream(_telemetry_key(runner, None, record, lane_map, known, providers, specs))["buckets"].append(
                    {"at": _epoch(item.get("at")), "usd": _number(item.get("usd")) or 0.0,
                     "n": item.get("n") if isinstance(item.get("n"), int) else 0,
                     "ids": [t for t in str(item.get("ids") or "").split(",") if t]})
        for item in tele.get("corrections") or []:
            if isinstance(item, dict) and _epoch(item.get("at")) is not None:
                stream(_telemetry_key(runner, None, item, lane_map, known, providers, specs))["corrections"].append(
                    {"event": item.get("event"), "corrects": item.get("corrects"),
                     "usd": _number(item.get("usd")) or 0.0, "at": _epoch(item.get("at"))})
        for item in tele.get("dropped_spend") or []:
            if isinstance(item, dict):
                stream(_telemetry_key(runner, None, item, lane_map, known, providers, specs))["dropped"].append(
                    {"at": _epoch(item.get("at"))})
        for record in tele.get("coverage") or []:
            if isinstance(record, dict):
                start, end = _epoch(record.get("from")), _epoch(record.get("to"))
                if start is not None and end is not None:
                    stream(_telemetry_key(runner, None, record, lane_map, known, providers, specs))[
                        "coverage"].append((start, end))
        for lane, record in (tele.get("active") or {}).items():
            if isinstance(record, dict):
                stream(_telemetry_key(runner, lane, record, lane_map, known, providers, specs))["active"].append(
                    {"lane": lane, "jobs": record.get("jobs"), "at": _epoch(record.get("at"))})
        events = (kept.get("events") or {}).get("data") or {}
        for item in events.get("launches") or []:
            if isinstance(item, dict) and item.get("lane"):
                at = _parse(item.get("at"))
                if at is not None:
                    stream(_telemetry_key(runner, item["lane"], {}, lane_map, known, providers, specs))[
                        "launches"].append(at)
    return sorted(streams.values(), key=lambda s: (s["runner"], s["account"] or ""))


def _coverage_covers(intervals: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    """Whether the union of coverage intervals holds one unbroken interval
    covering [start, end]."""
    merged: list[list[datetime]] = []
    for begin, finish in sorted(intervals):
        if merged and begin <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], finish)
        else:
            merged.append([begin, finish])
    return any(begin <= start and finish >= end for begin, finish in merged)


def _participation(stream: dict, start: datetime, end: datetime) -> tuple[datetime, datetime] | None:
    """One stream's participation interval inside [start, end], or None
    when it took no part. Participation starts at the runner's first
    evidence in the period (its first spend, correction, dropped record,
    active snapshot, LAUNCH, or the start of a coverage interval that
    overlaps the period), not at its first spend event alone, and ends at
    its last such evidence."""
    core = [b["at"] for b in stream["buckets"] if start <= b["at"] <= end]
    core += [c["at"] for c in stream["corrections"] if start <= c["at"] <= end]
    core += [d["at"] for d in stream["dropped"] if d["at"] is not None and start <= d["at"] <= end]
    core += [a["at"] for a in stream["active"] if a["at"] is not None and start <= a["at"] <= end]
    core += [at for at in stream["launches"] if start <= at <= end]
    if not core:
        return None
    times = list(core)
    for begin, finish in stream["coverage"]:
        if finish >= start and begin <= end:
            times += [max(begin, start), min(finish, end)]
    return min(times), max(times)


def _stream_spend(stream: dict, start: datetime, end: datetime) -> tuple[float, int]:
    """One stream's (usd, events) inside the period. Dedup already ran
    per runner at accumulation. A correction counts only when its target
    exists in this same stream (same runner, same resolved account) with
    its own time inside the same period; an orphan, cross-account or
    cross-period correction counts nothing."""
    total, count = 0.0, 0
    targets: dict[str, datetime] = {}
    for bucket in stream["buckets"]:
        for token in bucket["ids"]:
            targets[token] = bucket["at"]
        if start <= bucket["at"] <= end:
            total += bucket["usd"]
            count += bucket["n"]
    for correction in stream["corrections"]:
        targets.setdefault(correction["event"], correction["at"])
    for correction in stream["corrections"]:
        target_at = targets.get(correction["corrects"])
        if start <= correction["at"] <= end and target_at is not None and start <= target_at <= end:
            total += correction["usd"]
            count += 1
    return total, count


def _dropped_correction_taint(streams: list[dict], start: datetime, end: datetime) -> set[tuple[str, str | None]]:
    """The (runner, account) streams a correction in the period taints.
    A correction counts only against a target in its own stream with its
    own time in the same period; one dropped for lack of such a target
    (no target in its stream, a cross-period target, or a target in
    another of the same runner's streams, as when the correction names
    no account) marks its own stream incomplete for the period, and the
    target's stream too when the target lives in one, so no total reads
    known with the correction silently missing from it."""
    targets: list[tuple[dict, dict[str, datetime]]] = []
    for stream in streams:
        found: dict[str, datetime] = {}
        for bucket in stream["buckets"]:
            for token in bucket["ids"]:
                found[token] = bucket["at"]
        for correction in stream["corrections"]:
            found.setdefault(correction["event"], correction["at"])
        targets.append((stream, found))
    tainted: set[tuple[str, str | None]] = set()
    for stream, found in targets:
        for correction in stream["corrections"]:
            if not start <= correction["at"] <= end:
                continue
            target_at = found.get(correction["corrects"])
            if target_at is not None and start <= target_at <= end:
                continue  # the correction counted
            tainted.add((stream["runner"], stream["account"]))
            for other, other_found in targets:
                if other is not stream and other["runner"] == stream["runner"] \
                        and correction["corrects"] in other_found:
                    tainted.add((other["runner"], other["account"]))
    return tainted


def spend_total(streams: list[dict], account: str, start: datetime, end: datetime) -> dict:
    """One account's spend over [start, end] from its streams. Known only
    when every stream that took part covers its own participation interval;
    a silent or incomplete stream (any spend record dropped, for any
    reason), or no stream at all, reads unknown with no figure."""
    base = {"account": account, "from": _iso(start), "to": _iso(end)}
    participating = []
    for stream in streams:
        if stream["account"] != account:
            continue
        part = _participation(stream, start, end)
        if part is not None:
            participating.append((stream, part))
    if not participating:
        return {**base, "status": "unknown", "usd": None, "reason": "no spend reported for this period"}
    tainted = _dropped_correction_taint(streams, start, end)
    for stream, (begin, finish) in participating:
        if stream["silent"]:
            return {**base, "status": "unknown", "usd": None,
                    "reason": f"{stream['runner']} stopped reporting; its spend is unknown"}
        if stream["incomplete"]:
            return {**base, "status": "unknown", "usd": None,
                    "reason": f"some of {stream['runner']}'s spend records were not kept"}
        since = stream.get("accumulating_since")
        if since is not None and start < since:
            return {**base, "status": "unknown", "usd": None,
                    "reason": f"{stream['runner']}'s telemetry is only accumulated from {_iso(since)}; "
                              "this period starts before that"}
        if (stream["runner"], stream["account"]) in tainted:
            return {**base, "status": "unknown", "usd": None,
                    "reason": f"a correction from {stream['runner']} could not be applied for this account"}
        if not _coverage_covers(stream["coverage"], begin, finish):
            return {**base, "status": "unknown", "usd": None,
                    "reason": f"{stream['runner']} has a gap in its spend coverage for this account"}
    total, count = 0.0, 0
    for stream, _part in sorted(participating, key=lambda p: p[0]["runner"]):
        usd, events = _stream_spend(stream, start, end)
        total += usd
        count += events
    return {**base, "status": "known", "usd": round(total, 2), "events": count, "reason": None}


def spend_report(streams: list[dict], account: str, start: datetime, end: datetime) -> dict | None:
    """Owner-display evidence for an unknown total; never a verdict input.

    Reuse the accumulated, attributed and deduplicated records, including
    applicable corrections. Gaps are the uncovered parts of each runner's
    participation interval, not an estimate of missing spend. With no
    reported events there is no figure, even if a runner was active.
    """
    if spend_total(streams, account, start, end)["status"] == "known":
        return None
    total, count = 0.0, 0
    gaps = []
    for stream in sorted(streams, key=lambda s: s["runner"]):
        if stream["account"] != account:
            continue
        part = _participation(stream, start, end)
        if part is None:
            continue
        usd, events = _stream_spend(stream, start, end)
        total += usd
        count += events
        cursor, finish = part
        for begin, stop in sorted(stream["coverage"]):
            begin, stop = max(begin, part[0]), min(stop, finish)
            if stop < cursor or begin > finish:
                continue
            if begin > cursor:
                gaps.append({"runner": stream["runner"], "from": _iso(cursor), "to": _iso(begin),
                             "seconds": (begin - cursor).total_seconds()})
            cursor = max(cursor, stop)
        if cursor < finish:
            gaps.append({"runner": stream["runner"], "from": _iso(cursor), "to": _iso(finish),
                         "seconds": (finish - cursor).total_seconds()})
    return {"usd": round(total, 2), "events": count, "gaps": gaps} if count else None


def active_totals(streams: list[dict]) -> dict:
    """Current active jobs per account: each (runner, lane) snapshot counts
    once, runners sum, and a silent runner that held a snapshot for the
    account makes the account's count unknown rather than partial."""
    out: dict[str, dict] = {}
    for key in sorted({s["account"] for s in streams if s["account"] is not None}):
        mine = [s for s in streams if s["account"] == key and s["active"]]
        if not mine:
            continue
        if any(s["silent"] for s in mine):
            out[key] = {"jobs": None, "status": "unknown"}
        else:
            out[key] = {"jobs": sum(a["jobs"] for s in mine for a in s["active"] if isinstance(a["jobs"], int)),
                        "status": "known"}
    return out


def unattributed_summary(streams: list[dict], start: datetime | None = None, end: datetime | None = None) -> dict:
    """Spend and active records no account could be resolved for, kept
    apart under their own label and never added to any account. The same
    honesty as an account total: only the given period counts, and a
    participating stream that is silent, incomplete or has a coverage gap
    makes the figure unknown (``spend_usd`` None), never zero; with no
    active snapshot at all, ``active_jobs`` is None, never zero."""
    start = start or datetime.min.replace(tzinfo=timezone.utc)
    end = end or datetime.max.replace(tzinfo=timezone.utc)
    mine = [s for s in streams if s["account"] is None]
    total, count, unknown = 0.0, 0, False
    participating = False
    tainted = _dropped_correction_taint(streams, start, end)
    for stream in sorted(mine, key=lambda s: s["runner"]):
        part = _participation(stream, start, end)
        if part is None:
            continue
        participating = True
        since = stream.get("accumulating_since")
        if stream["silent"] or stream["incomplete"] or not _coverage_covers(stream["coverage"], part[0], part[1]) \
                or (since is not None and start < since) \
                or (stream["runner"], stream["account"]) in tainted:
            unknown = True
        usd, events = _stream_spend(stream, start, end)
        total += usd
        count += events
    active = [s for s in mine if s["active"]]
    jobs = None if not active or any(s["silent"] for s in active) else sum(
        a["jobs"] for s in active for a in s["active"] if isinstance(a["jobs"], int))
    return {"spend_events": count, "spend_usd": round(total, 2) if participating and not unknown and count else None,
            "spend_status": "known" if participating and not unknown else "unknown",
            "active_jobs": jobs}


# ---- the usage cache ----------------------------------------------------------------


def configured_keys(config: dict) -> set[str]:
    """The usage cache keys of the configured accounts: each valid
    ``usage_accounts`` entry, with none set every harness's default
    login, or nothing after the owner chose "Read no accounts" (what
    ``usage.configured_accounts`` reads, without its PATH lookup, so every
    caller gets the same set)."""
    from khipu import usage

    raw = config.get("usage_accounts")
    if not isinstance(raw, list) or not raw:
        return set() if usage.reads_none(config) else {f"{harness}|" for harness in usage.HARNESSES}
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
    from khipu.routing_identity import STABLE

    for key, row in sorted(rows.items()):
        if (key.partition("|")[0] not in usage.HARNESSES and not STABLE.fullmatch(key)) \
                or (configured is not None and key not in configured):
            continue
        accounts_read += 1
        attempt = _parse(row.get("last_attempt_at") or row.get("probed_at"))
        error = None if row.get("ok") else (row.get("last_error") or row.get("error"))
        error = next((e for e in (usage.E_SIGNIN, usage.E_NOT_INSTALLED, usage.E_REDIRECT) if e == error), None)
        if error is not None and attempt is not None:
            out.append({"id": "p" + _sid(key, "down", _iso(attempt)), "account": key, "runner": None, "lane": None,
                        "state": "down", "until": None, "basis": "checked", "scope": "all", "window": None,
                        "used_pct": None, "source": "probe", "written_by": by, "via": "probe", "at": attempt,
                        "evidence": f"usage probe: {error}", "reason": error, "message_time": False})
            newest = attempt if newest is None else max(newest, attempt)
            continue
        if not (row.get("ok") or row.get("last_good_at")):
            continue
        at = _parse(row.get("observed_at") or (row.get("last_good_at") if not row.get("ok") else None)
                    or row.get("probed_at"))
        windows = [w for w in row.get("windows") if isinstance(w, dict)] if isinstance(row.get("windows"), list) else []
        if at is None or not windows:
            continue
        newest = at if newest is None else max(newest, at)
        observed = row.get("source") in ("last_observed", "rollout")
        groups: dict[str, list[dict]] = {}
        for w in windows:
            groups.setdefault(safe_name(w.get("model")) or "all", []).append(w)
        for scope, group in sorted(groups.items()):
            full: list[tuple[datetime, datetime, dict]] = []
            top = None
            for w in group:
                used = _number(w.get("used_pct"))
                if used is None:
                    continue
                top = used if top is None else max(top, used)
                reset = _parse(w.get("resets_at"))
                window_at = _parse(w.get("observed_at")) or at
                if used >= 100 and reset is not None and reset > window_at:
                    full.append((window_at, reset, w))
            # A window at its limit is never headroom: with no reset time
            # still ahead of the probe, the scope gives no evidence at all.
            if top is None or (top >= 100 and not full):
                continue
            base = {"account": key, "runner": None, "lane": None, "scope": scope, "source": "probe",
                    "written_by": by, "via": "probe", "at": at, "reason": None, "message_time": False,
                    "observed": observed, "turn": row.get("source") == "turn"}
            resets = [_parse(w.get("resets_at")) for w in group]
            resets = [reset for reset in resets if reset is not None and reset > at]
            if resets:
                base["reset_at"] = _iso(min(resets))
            if full:
                # The newest full window wins, then the latest reset: a fresh refusal must
                # not hide behind an older full window carried beside it (which an owner
                # clear may have overruled). Windows without their own time all share ``at``.
                window_at, reset, w = max(full, key=lambda item: item[:2])
                session_logs = key.partition("|")[0] == "muse" and w.get("source") == "session_logs"
                if session_logs:
                    base.update(observed=True, turn=False, session_logs=True)
                base["at"] = window_at
                newest = max(newest, window_at)
                name = safe_name(w.get("name")) or ""
                window = "weekly" if name.startswith("weekly") else name or None
                out.append(dict(base, id="p" + _sid(key, scope, "limited", _iso(window_at), _iso(reset)), state="limited",
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


def job_identities(cache: dict, known: list[dict], now: datetime) -> dict:
    """Join a runner's LAUNCH reference to the job's own captured login."""
    events = (cache.get("events") or {}).get("data") or {}
    launches = [row for row in events.get("launches", []) if row.get("attachment_ref")]
    if not launches:
        return {}
    from khipu import routing, session_capture as sc

    forgotten = sc.login_homes_forgotten_at()
    attachments = {}
    refs = {row["attachment_ref"] for row in launches}
    for name, state in sc.iter_states():
        harness, sep, sid = name.removesuffix(".json").partition("--")
        ref = _sid(harness, sid)
        if not sep or ref not in refs or state.get("origin") != "job":
            continue
        seen = _epoch(state.get("seen_ts"))
        if seen is None or (now - seen).total_seconds() > sc.LOGIN_HOME_KEEP_S or (
                forgotten is not None and seen.timestamp() <= forgotten):
            continue
        # The captured harness is the transport; the local login binding
        # identifies the payer (for example GLM through Claude Code).
        binding = routing.login_binding({"login_home": state.get(sc.LOGIN_HOME_FIELD)}, known)
        attachments[ref] = dict(binding, source="job_attachment")
    out = {}
    for row in sorted(launches, key=lambda x: x["at"]):
        if row["attachment_ref"] in attachments:
            out[row["lane"]] = attachments[row["attachment_ref"]]
    return out


def lane_witnesses(cache: dict, known: list[dict], now: datetime, status: dict) -> dict:
    heartbeat = (cache.get("heartbeat") or {}).get("data") or {}
    identities = job_identities(cache, known, now)
    # The current heartbeat outranks a historical captured launch.
    if not status.get("silent") and not (cache.get("heartbeat") or {}).get("since"):
        identities.update({lane: dict(binding, source="heartbeat") for lane, binding in heartbeat.get("lanes", {}).items()
                           if binding})
    return identities


def collect(config: dict, cfg: dict, now: datetime, prev_cache: dict | None) -> tuple[list[dict], dict, dict]:
    """(evidence for the fold, the sources' status, the cache to keep in the
    view). Reads files only: starts no process, probes nothing, writes
    nothing, takes no runner's lock."""
    from khipu import routing

    entries, refused, invalid = registered(config)
    known = routing.accounts()
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
        specs = ((kept.get("config") or {}).get("data") or {}).get("lanes") or {}
        identities = lane_witnesses(kept, known, now, status)
        matches = {lane: routing.resolve_lane(entry["name"], lane, lane_map, known, specs.get(lane),
                                             cfg.get("_providers"), identities.get(lane))
                   for lane in status["lanes"]}
        labels = {a["key"]: a["label"] for a in known}
        status["unmatched_lanes"] = [lane for lane, match in matches.items() if not match["key"]]
        status["lane_matches"] = {lane: {"account": labels.get(match["key"]),
                                         "matched_by": match["matched_by"],
                                         "identity_state": match["identity_state"], "reason": match["reason"],
                                         "field_to_add": match["field_to_add"],
                                         "candidates": [labels[k] for k in match["candidates"]]}
                                   for lane, match in matches.items()}
        runners.append(status)
        cache["runners"][entry["name"]] = kept
        for item in runner_evidence(status, kept):
            match = matches.get(item["lane"])
            if match is not None and not match["key"]:
                item["unmatched"] = True
            evidence.append(item)
    # http-quota accounts live in provider facts, not in usage_accounts:
    # their cache rows are keyed by stable account id, which the known
    # definition accounts already carry (no extra read of the log here).
    from khipu.routing_identity import STABLE

    providers = cfg.get("_providers") or {}
    configured = configured_keys(config) | {
        a.get("definition_id") or a["key"] for a in known
        if (providers.get(a.get("harness") or "") or {}).get("probe") == "http-quota"
        and STABLE.fullmatch(a.get("definition_id") or a["key"] or "")}
    usage_items, usage_status = usage_evidence(configured)
    evidence += usage_items
    streams = telemetry_streams(cache, known=known, lane_map=lane_map, providers=cfg.get("_providers"),
                                silent={s["name"]: bool(s["silent"]) for s in runners})
    status = {"runners": runners, "refused": refused, "invalid": invalid, "usage": usage_status,
              "table": read_table(cfg.get("routing_table")),
              "telemetry": {"accounts": {key: {"active_jobs": value["jobs"], "active_status": value["status"]}
                                         for key, value in active_totals(streams).items()},
                            "unattributed": unattributed_summary(streams, now - timedelta(days=7), now)}}
    return evidence, status, cache


# ---- doctor's rule checks (read from config.json and ADDED lines only) ------------------


def _asks_for(text) -> list[str]:
    """The checks one rule's own words ask for."""
    text = str(text or "").lower()
    out = []
    if "-fast" in text:
        out.append("fast_tier")
    if "xai" in text:
        out.append("paid_xai")
    if "grok" in text and ("privacy" in text or "safety" in text):
        out.append("grok_private")
    if "grok" in text and ("re-check" in text or "recheck" in text or "model family" in text):
        out.append("grok_confirm")
    return out


def watched(rules: list[dict]) -> dict[str, int]:
    """Which checks the active standing rules ask for: the check's name to
    the first rule's number that asks for it. The public product ships with
    no rules, so nothing is checked there."""
    out: dict[str, int] = {}
    for rule in rules:
        for check in _asks_for(rule.get("text")):
            out.setdefault(check, rule.get("n"))
    return out


def rule_checks(cache: dict, rules: list[dict]) -> list[dict]:
    """Each watched rule against each runner's settings and ADDED lines:
    ``broken`` (a doctor warning) or ``cannot_check`` (never reported as
    passing), with the check that found it (``check``), the runner it was
    judged on and which of its files (``settings`` or ``events``) so the app
    can name both."""
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
                    out.append({"rule": watch["fast_tier"], "check": "fast_tier", "status": "broken", **on_settings,
                                "text": f"A standing rule may be broken: {name}'s settings name a model ending in "
                                        f"-fast for its {lane} lane."})
                elif not spec.get("model_in_launch"):
                    out.append({"rule": watch["fast_tier"], "check": "fast_tier", "status": "cannot_check", **on_settings,
                                "text": f"{name}'s {lane} lane: can't be checked: the model is set inside "
                                        f"{script}"})
            if "paid_xai" in watch and kind.startswith("grok"):
                out.append({"rule": watch["paid_xai"], "check": "paid_xai", "status": "cannot_check", **on_settings,
                            "text": f"{name}'s {lane} lane: whether it uses the paid xAI API can't be checked: "
                                    f"the launch environment is set inside {script}"})
            if "grok_confirm" in watch and (kind == "grokcheck" or
                                            (spec.get("confirm_lane") and model_family(spec.get("model")) == "xAI")):
                confirm = spec.get("confirm_lane")
                other = lanes.get(confirm) if confirm else None
                other_kind = ((other or {}).get("kind") or "").lower()
                if other is None or other_kind.startswith("grok") or model_family(other.get("model")) == "xAI":
                    out.append({"rule": watch["grok_confirm"], "check": "grok_confirm", "status": "broken", **on_settings,
                                "text": f"A standing rule may be broken: {name}'s Grok check lane has no "
                                        "confirming lane from another model family."})
                elif not other_kind:
                    out.append({"rule": watch["grok_confirm"], "check": "grok_confirm", "status": "cannot_check", **on_settings,
                                "text": f"{name}'s Grok check lane: whether its confirming lane {confirm} is from "
                                        "another model family can't be checked: its kind isn't set"})
        if "grok_private" in watch:
            grok = {lane for lane, spec in lanes.items() if (spec.get("kind") or "").lower().startswith("grok")
                    or model_family(spec.get("model")) == "xAI"}
            events_kept = kept.get("events") or {}
            scan = events_kept.get("scan") if isinstance(events_kept.get("scan"), dict) else None
            events = events_kept.get("data") or {}
            for added in (scan["private"] if scan else events.get("added") or []):
                named = set(added.get("lanes") or [])
                # Lanes the line leaves to the runner's defaults.
                if "check_lane" in added and added["check_lane"] is None and config.get("check_lane"):
                    named.add(config["check_lane"])
                if "fix_lanes" in added and added["fix_lanes"] is None:
                    named |= set(config.get("fix_lanes") or [])
                if added.get("private") and grok & named:
                    out.append({"rule": watch["grok_private"], "check": "grok_private", "status": "broken",
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
        elif check == "grok_confirm" and (kind == "grokcheck" or
                                         (spec.get("confirm_lane") and model_family(spec.get("model")) == "xAI")):
            about += 1
            other = lanes.get(spec.get("confirm_lane")) if spec.get("confirm_lane") else None
            judged += other is None or bool((other or {}).get("kind"))
        elif check == "grok_private" and (kind.startswith("grok") or model_family(spec.get("model")) == "xAI"):
            about += 1
            judged += 1
    return about, judged


# Worst first. A cell's outcome on one runner, then a check's or a rule's
# result: a rule's result is its worst check's, so it is "ok" only when every
# check it asks for ran on every runner and passed.
_CELL_ORDER = ("broken", "not_read", "failed", "stale", "no_lane", "cannot_check", "ok")
_RESULT_ORDER = ("broken", "failed", "stale", "not_read", "checking", "incomplete", "no_runner", "cannot_check", "no_lane", "ok")
_NOT_NOW = ("failed", "stale", "not_read", "checking", "incomplete")


def _skipped(check: str, kept: dict) -> dict:
    """What a cap or a name Khipu can't use left out of the files this check
    judges on one runner: lanes and default fix lanes from the settings, and
    for the privacy check private ADDED lines past ``PRIVATE_MAX`` or longer
    than a chunk (``added``) and a last line left unfinished too long
    (``unfinished_line``)."""
    config = (kept.get("config") or {}).get("data")
    config = config if isinstance(config, dict) else {}
    wanted = {"lanes"} | ({"fix_lanes"} if check == "grok_private" else set()) | \
        ({"kinds"} if check != "fast_tier" else set())
    out = {k: v for k, v in (config.get("skipped") or {}).items() if v and k in wanted}
    if check == "grok_private":
        scan = (kept.get("events") or {}).get("scan")
        if isinstance(scan, dict) and scan.get("dropped"):
            out["added"] = scan["dropped"]
        if isinstance(scan, dict) and scan.get("unfinished"):
            out["unfinished_line"] = 1  # a last line left unfinished too long: never read
    return out


def _read(kept: dict, now: datetime | None, stale_after: float) -> dict:
    """One runner file's read as a check depends on it: ``read`` (it was
    ever read well and its data kept), ``good_at`` (that read), ``failed``
    and ``since`` (the latest read failed, set by ``read_runner``), and
    ``stale`` (that good read is older than ``stale_after`` seconds, 0:
    never)."""
    read = isinstance(kept.get("data"), dict)
    good = _parse(kept.get("good_at")) if read else None
    return {"read": read, "good_at": kept.get("good_at") if read else None, "failed": bool(kept.get("since")),
            "since": kept.get("since"),
            "stale": bool(good and now and stale_after and (now - good).total_seconds() > stale_after)}


def _cell(check: str, runner: str, kept: dict, findings: list[dict], now, stale_after: float) -> dict:
    """One check on one runner, from that runner's own reads and findings
    only. Every check reads its lanes from the settings; the privacy check
    also reads the ADDED lines (``events``)."""
    config_kept = kept.get("config") or {}
    reads = {"settings": _read(config_kept, now, stale_after)}
    if check == "grok_private":
        reads["events"] = _read(kept.get("events") or {}, now, stale_after)
    mine = [c for c in findings if c.get("check") == check and c.get("runner") == runner]
    broken = [c for c in mine if c.get("status") == "broken"]
    config = config_kept.get("data") if reads["settings"]["read"] else {}
    skipped = _skipped(check, kept)
    scan = (kept.get("events") or {}).get("scan") if check == "grok_private" else None
    scan = scan if isinstance(scan, dict) else None
    about, judged = _check_lanes(check, _config_facts(config.get("lanes")))
    if broken:
        outcome = "broken"
    elif not all(r["read"] for r in reads.values()):
        outcome = "not_read"
    elif any(r["failed"] for r in reads.values()):
        outcome = "failed"
    elif any(r["stale"] for r in reads.values()):
        outcome = "stale"
    elif check == "grok_private" and not (scan or {}).get("done") and not (scan or {}).get("unfinished"):
        outcome = "checking"  # the whole-log read has not reached the end yet
    elif skipped:
        outcome = "incomplete"  # never a pass on part of the input
    elif not about:
        outcome = "no_lane"
    elif not judged:
        outcome = "cannot_check"
    else:
        outcome = "ok"
    return {"runner": runner, "outcome": outcome, "lanes": judged, "unchecked": about - judged, "reads": reads,
            **({"skipped": skipped} if skipped else {}),
            **({"scan": {"read": scan.get("offset"), "size": scan.get("size"), "lines": scan.get("lines"),
                         "done": bool(scan.get("done")), "reset": scan.get("reset")}} if scan else {}),
            "text": broken[0]["text"] if broken else None, "more": max(len(broken) - 1, 0),
            "why": next((c["text"] for c in mine if c.get("status") == "cannot_check"), None)}


def _check_result(check: str, cells: list[dict]) -> dict:
    """A check's result from its own cells only: ``ok`` only when every cell
    is ok, no_lane or cannot_check and at least one is ok; its time is the
    oldest good read any of its cells depends on."""
    outcomes = {c["outcome"] for c in cells}
    if not cells:
        result = "no_runner"
    elif "broken" in outcomes:
        result = "broken"
    elif outcomes & set(_NOT_NOW):
        result = next(r for r in _NOT_NOW if r in outcomes)
    elif "ok" in outcomes:
        result = "ok"
    elif "cannot_check" in outcomes:
        result = "cannot_check"
    else:
        result = "no_lane"
    broken = [c for c in cells if c["outcome"] == "broken"]
    counted = [c for c in cells if c["outcome"] in ("ok", "cannot_check")]
    times = [r["good_at"] for c in cells for r in c["reads"].values() if r["good_at"]]
    return {"check": check, "result": result, "at": min(times) if result == "ok" and times else None,
            "lanes": sum(c["lanes"] for c in counted), "unchecked": sum(c["unchecked"] for c in counted),
            "text": broken[0]["text"] if broken else None, "more": max(sum(1 + c["more"] for c in broken) - 1, 0),
            "why": next((c["why"] for c in counted if c["why"]), None), "cells": cells}


def rule_results(cache: dict, rules: list[dict], checks: list[dict] | None = None, *,
                 now: datetime | None = None, stale_after: float = 0, runners: list[str] | None = None) -> list[dict]:
    """One result per active rule Khipu has a check for, built from a matrix
    of rule x check x job runner. A rule's checks are the ones its own words
    ask for; every registered runner (``runners``, with every runner the
    cache holds) is a source of every check, read well or never. Each check
    in ``checks`` carries its ``cells``: per runner, the ``outcome`` and the
    freshness of each read it depends on (``_read``). The rule's ``result``
    is its worst check's:

    - ``broken``: a check found the rule may be broken on a runner (``text``
      says how; broken findings match the check and runner, never the rule);
    - ``failed`` / ``stale`` / ``not_read``: a read a check depends on failed,
      is older than ``stale_after``, or never succeeded, so that check has not
      run there (the cells say which runner and which file);
    - ``no_runner``: no job runner folder added, so nothing to read;
    - ``cannot_check``: every lane a check is about is set where Khipu can't see;
    - ``no_lane``: no job runner has a lane the check is about;
    - ``ok``: every check ran on every runner and found nothing; ``at`` is the
      oldest read it relied on; ``unchecked`` lanes it couldn't judge.

    "Nothing wrong found" is only ever ``ok``."""
    wanted = [(rule, _asks_for(rule.get("text"))) for rule in rules]
    wanted = [(rule, asks) for rule, asks in wanted if asks]
    if not wanted:
        return []
    findings = rule_checks(cache, rules) if checks is None else checks
    kept = (cache or {}).get("runners") or {}
    names = sorted(set(kept) | {n for n in runners or [] if n})
    by_check: dict[str, dict] = {}
    out = []
    for rule, asks in wanted:
        for check in asks:
            if check not in by_check:
                by_check[check] = _check_result(check, [_cell(check, name, kept.get(name) or {}, findings, now,
                                                              stale_after) for name in names])
        found = sorted((dict(by_check[c]) for c in dict.fromkeys(asks)), key=lambda r: _RESULT_ORDER.index(r["result"]))
        worst = found[0]
        at = min(r["at"] for r in found) if all(r["result"] == "ok" for r in found) else None
        out.append({k: v for k, v in worst.items() if k != "cells"} | {"rule": rule.get("n"), "at": at,
                                                                       "checks": found})
    return sorted(out, key=lambda r: r["rule"] if isinstance(r["rule"], int) else 0)


def launches(cache: dict) -> list[dict]:
    """Every LAUNCH line kept, with its runner."""
    out = []
    for name, kept in sorted(((cache or {}).get("runners") or {}).items()):
        events = (kept.get("events") or {}).get("data") or {}
        specs = ((kept.get("config") or {}).get("data") or {}).get("lanes") or {}
        out += [{"runner": name, "lane": item["lane"], "at": item["at"],
                 "declaration": lane_identity(specs.get(item["lane"]) or {})}
                for item in events.get("launches") or []]
    return out


def provider_checks(cache: dict, providers: dict, known: list[dict], lane_map: dict | None = None) -> list[dict]:
    """Provider model facts checked against each runner's declared settings."""
    from khipu import routing
    from khipu.routing_providers import advisory

    out = []
    for runner, kept in (cache.get("runners") or {}).items():
        specs = ((kept.get("config") or {}).get("data") or {}).get("lanes") or {}
        for lane, spec in specs.items():
            match = routing.resolve_lane(runner, lane, lane_map or {}, known, spec, providers)
            account = next((a for a in known if a["key"] == match["key"]), None)
            provider = providers.get(account["harness"]) if account else providers.get(
                routing._provider_id(spec.get("provider") or spec.get("kind")))
            if provider:
                out += [{"check": "provider_models", "runner": runner, "lane": lane, "status": "broken",
                         "text": f"{runner} lane {lane}: {text}"}
                        for text in advisory(provider, spec.get("model"), include_use=False)]
    return out
