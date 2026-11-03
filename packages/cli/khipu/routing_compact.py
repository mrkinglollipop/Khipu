"""A bound on the routing log (``state/routing/events.jsonl``).

The log is append-only and was folded whole on every stale read: at 10,000
lines a fresh fold overran the prompt path's 50 ms budget and the
conversation got no routing. This module compacts it.

What a compaction is: the previous log is kept whole as an archive and the
live log is replaced by its *checkpoint*, the lines the fold can still
tell apart from the lines left out, in their original order and bytes, with
every later line (the tail) after them. No line is rewritten, so the fold
over the compacted file is the fold over the full log:

  * nothing an owner wrote is ever left out (rules, the note, the priority,
    holds, caps, clears, account lines and every undo or restore step), nor
    a line that cannot be read (doctor counts those), nor any line of an
    account change;
  * a ``usage_sample`` goes only when it is older than the longest span the
    pace calculation reads (``compute_pace`` reads one rolling day, a
    trailing window and the newest earlier reading per witness) with
    margin, and is not one of the newest few of its account and window;
  * a ``source_seen`` goes unless it is among the last of its Mac and runner
    (only the last is read);
  * a conversation's or a check's ``evidence`` goes only when it is old, its
    end has passed, it is not renewed and it is not among the newest of its
    account, scope and state; an ``observation`` is never left out (the
    sources' own sightings are matched to them by ``source_id``, and one
    missing would let the sighting count again);
  * a ``reset_check`` goes when old and not among the newest of its account.

Before anything is replaced the compacted log and the full log are folded
at the present and at every moment the fold says its answer changes next
(``valid_until``), and at fixed distances out to a year: they must agree, or
nothing is written. The replacement writes the new file beside the log,
fsyncs it, links the old log into ``archive/`` and renames the new file over
the log (atomic), so a crash at any point leaves a log that reads, and at
worst an extra archive. Archives are pruned only when past both their count
and their age.

The planning and the check run outside the routing lock on a copy of the
bytes read; under the lock the log must still begin with those bytes (a
writer only appends), and what was appended meanwhile is carried over
verbatim.
"""
from __future__ import annotations

import contextlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from khipu import routing

ARCHIVE_DIR = "archive"
STATE_NAME = "compaction.json"

# The bound. The fold costs about 14 ms per 1,000 lines on this Mac and the
# prompt path has 50 ms, so a log that stays under ~2,000 lines folds well
# inside it. Yellow is the line where compaction starts on its own; red is
# where a fresh fold is near the whole budget (4,000 lines ~ 55 ms).
LINES_YELLOW, BYTES_YELLOW = 2000, 1 << 20
LINES_RED, BYTES_RED = 4000, 2 << 20
# Under this a log is never over the bound (a line is ~250 bytes): not even counted.
SMALL_BYTES = 32 * 1024
# A compaction aims below this, so the next one is not at once due.
LINES_TARGET, BYTES_TARGET = 1500, 768 * 1024
# A compaction that would take less than this share of the lines is not
# worth an archive.
MIN_GAIN = 0.10
# Automatic attempts are at least this far apart (the nightly ignores it).
MIN_INTERVAL_S = 6 * 3600
ARCHIVE_KEEP_COUNT = 4
ARCHIVE_KEEP_DAYS = 30

# (sample days, samples kept per window, evidence days, evidence kept per
# state, reset-check days). The first profile that reaches the target wins.
PROFILES = (
    (3, 4, 45, 4, 30),
    (2, 3, 21, 3, 14),
    (2, 3, 7, 2, 7),
)
RESET_CHECKS_KEPT = 3
SOURCE_SEEN_KEPT = 3
# Moments, after now, the full and the compacted fold are compared at in
# addition to the fold's own ``valid_until`` chain.
CHECK_OFFSETS_S = (3600, 86400, 8 * 86400, 40 * 86400, 400 * 86400)
CHAIN_MOST = 6
_VOLATILE = ("causes", "folded_at", "checked_at", "source_cache")


def _dir() -> Path:
    return routing.routing_dir()


def _state_file() -> Path:
    return _dir() / STATE_NAME


def archive_dir() -> Path:
    return _dir() / ARCHIVE_DIR


# ---- size and health ------------------------------------------------------------


def log_size() -> dict:
    """The log's line count and bytes (0 and 0 when there is none)."""
    try:
        data = routing.events_file().read_bytes()
    except OSError:
        return {"lines": 0, "bytes": 0}
    return {"lines": _lines(data), "bytes": len(data)}


def _lines(data: bytes) -> int:
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def health() -> dict:
    """Doctor's row: ``level`` green, yellow (over the bound compaction starts
    at) or red (over the line where a fresh fold nears the prompt budget),
    with the line count and bytes and the last compaction."""
    size = log_size()
    lines, nbytes = size["lines"], size["bytes"]
    level = "green"
    if lines > LINES_RED or nbytes > BYTES_RED:
        level = "red"
    elif lines > LINES_YELLOW or nbytes > BYTES_YELLOW:
        level = "yellow"
    last = _read_state()
    out = {"level": level, "lines": lines, "bytes": nbytes,
           "bound": {"yellow": {"lines": LINES_YELLOW, "bytes": BYTES_YELLOW},
                     "red": {"lines": LINES_RED, "bytes": BYTES_RED}},
           "last_compaction": last.get("at"), "last_result": last.get("result"),
           "archives": len(_archives())}
    gone, total = _droppable() if level != "green" and last.get("attempted_at") else (1, 0)
    if level != "green" and last.get("attempted_at") and gone < MIN_GAIN * total:
        out["level"] = "yellow"
        out["warning"] = (f"The routing record holds {lines:,} lines ({nbytes:,} bytes), over its bound of "
                          f"{LINES_YELLOW:,}, and compaction can't shrink it further: {total - gone:,} of "
                          "its lines are the owner's, observations or others the fold needs, and are kept.")
        out["kept_lines"] = total - gone
    elif level != "green":
        out["warning"] = (f"The routing record holds {lines:,} lines ({nbytes:,} bytes); it is compacted at "
                          f"{LINES_YELLOW:,} lines or {BYTES_YELLOW:,} bytes, nightly and after the plan figures "
                          f"refresh" + (f". It is past {LINES_RED:,} lines or {BYTES_RED:,} bytes, where reading it "
                                        "before a prompt can run out of the time allowed." if level == "red" else "."))
    return out


def _droppable() -> tuple[int, int]:
    """(lines the strongest profile could drop now, lines in the log)."""
    try:
        rows = _rows(routing.events_file().read_bytes())
    except OSError:
        return 0, 0
    return len(plan(rows, routing._now(), PROFILES[-1])), len(rows)


def _read_state() -> dict:
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_state(state: dict) -> None:
    """Atomically replace compaction.json (counts and times; no log text)."""
    folder = _dir()
    folder.mkdir(parents=True, exist_ok=True)
    temp = folder / f".compaction-{os.getpid()}-{secrets.token_hex(3)}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(state, sort_keys=True))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, _state_file())
        _fsync_dir(folder)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


# ---- planning -------------------------------------------------------------------


def _when(event: dict, *names: str):
    for name in names:
        at = routing._parse(event.get(name))
        if at is not None:
            return at
    return None


def _owner(event: dict) -> bool:
    return (event.get("written_by") or {}).get("kind") == "owner" or event.get("via") in ("app", "direction")


def plan(rows: list[tuple[bytes, dict | None]], now: datetime, profile: tuple) -> set[int]:
    """The indexes of ``rows`` (line, its event or None) the fold can do
    without, under ``profile``. Anything not sure is kept."""
    sample_days, sample_kept, evidence_days, evidence_kept, reset_days = profile
    sample_cut = now - timedelta(days=sample_days)
    evidence_cut = now - timedelta(days=evidence_days)
    reset_cut = now - timedelta(days=reset_days)
    drop: set[int] = set()
    samples: dict[tuple, list[tuple[datetime, int]]] = {}
    seen: dict[tuple, list[int]] = {}
    evidence: dict[tuple, list[tuple[datetime, int]]] = {}
    checks: dict[str, list[tuple[datetime, int]]] = {}
    renewed: dict[str, datetime] = {}
    renewals: dict[str, list[int]] = {}
    for i, (_line, event) in enumerate(rows):
        if event is not None and event.get("type") == "observation_renew" and isinstance(event.get("target"), str):
            at = _when(event, "witness_at", "written_at")
            if at is not None:
                renewed[event["target"]] = max(renewed.get(event["target"], at), at)
            renewals.setdefault(event["target"], []).append(i)
    for i, (_line, event) in enumerate(rows):
        if event is None or _owner(event):
            continue
        kind = event.get("type")
        if kind == "usage_sample":
            at = _when(event, "witness_at", "written_at")
            if at is not None and isinstance(event.get("account"), str):
                samples.setdefault((event["account"], event.get("window")), []).append((at, i))
        elif kind == "source_seen":
            seen.setdefault((event.get("mac"), event.get("runner")), []).append(i)
        elif kind == "reset_check" and isinstance(event.get("account"), str):
            at = _when(event, "written_at")
            if at is not None:
                checks.setdefault(event["account"], []).append((at, i))
        elif kind == "evidence" and isinstance(event.get("account"), str) and not event.get("source_id"):
            at = _when(event, "witness_at", "written_at")
            end = _when(event, "until", "expires_at")
            if at is not None:
                at = max(at, renewed.get(event.get("id"), at))
                # Nothing about a limit or an expiry that is still ahead.
                if end is not None and end > evidence_cut:
                    continue
                evidence.setdefault((event["account"], event.get("scope") or "all", event.get("state")), []).append((at, i))
    for group in samples.values():
        group.sort(reverse=True)
        drop.update(i for at, i in group[sample_kept:] if at < sample_cut)
    for group in seen.values():
        drop.update(group[:-SOURCE_SEEN_KEPT])
    for group in checks.values():
        group.sort(reverse=True)
        drop.update(i for at, i in group[RESET_CHECKS_KEPT:] if at < reset_cut)
    gone: set[str] = set()
    for group in evidence.values():
        group.sort(reverse=True)
        for at, i in group[evidence_kept:]:
            if at < evidence_cut:
                drop.add(i)
                if rows[i][1].get("id"):
                    gone.add(rows[i][1]["id"])
    # A renewal of a line that is gone has nothing to renew.
    for target in gone:
        drop.update(renewals.get(target, ()))
    # A tail guard: the newest day of the log is never touched.
    newest = max((at for at in (_when(e, "written_at") for _l, e in rows if e) if at is not None), default=None)
    if newest is not None:
        for i in list(drop):
            at = _when(rows[i][1], "written_at")
            if at is not None and at > newest - timedelta(days=1):
                drop.discard(i)
    return drop


def _rows(data: bytes) -> list[tuple[bytes, dict | None]]:
    # A blank line holds no event and is not carried.
    return [(line, routing._line_event(line)) for line in data.split(b"\n") if line.strip()]


# ---- the check ------------------------------------------------------------------


class _Log:
    """One log's lines parsed once, folded at any moment (the fold copies
    what it is given, so one parse serves every moment)."""

    def __init__(self, lines: list[bytes], inputs: tuple):
        self.raw = [e for e in (routing._line_event(line) for line in lines) if e is not None]
        self.inputs = inputs
        self.cache: dict[datetime, dict] = {}

    def at(self, when: datetime) -> dict:
        if when not in self.cache:
            _fingerprint, cfg, known = self.inputs
            ordered, _bad = routing._order_events(self.raw, 0)
            events, _open = routing._split_txns(ordered, routing._held_list())
            self.cache[when] = routing.fold(events, now=when, cfg=cfg, known=known, extra=(), sources=None)
        return self.cache[when]


def _normal(body: dict) -> str:
    return json.dumps({k: v for k, v in body.items() if k not in _VOLATILE}, sort_keys=True, default=str)


def agree(full: "_Log", kept: "_Log", now: datetime) -> tuple[bool, int, str | None]:
    """(whether the two logs fold alike, how many moments were compared, the
    first moment they differ at). The moments are now, each ``valid_until``
    the full fold names after it, and fixed distances out."""
    moments, at = [now], now
    for _ in range(CHAIN_MOST - 1):
        nxt = routing._parse(full.at(at).get("valid_until"))
        if nxt is None or nxt <= at:
            break
        moments.append(nxt)
        at = nxt
    moments += [now + timedelta(seconds=s) for s in CHECK_OFFSETS_S]
    for count, when in enumerate(moments, 1):
        if _normal(full.at(when)) != _normal(kept.at(when)):
            return False, count, routing._iso(when)
    return True, len(moments), None


# ---- the compaction -------------------------------------------------------------


def _choose(data: bytes, now: datetime) -> dict:
    """The best verified compaction of ``data``: {"kept": lines, "profile",
    "moments", "dropped", "by_type"}, or {"why": ...} when there is none
    worth making."""
    rows = _rows(data)
    full = [line for line, _e in rows]
    inputs = routing._inputs()
    whole = _Log(full, inputs)
    best: dict | None = None
    why = "nothing the fold can do without"
    for number, profile in enumerate(PROFILES, 1):
        drop = plan(rows, now, profile)
        if len(drop) < MIN_GAIN * len(rows):
            why = "less than a tenth of the lines can go"
            continue
        kept = [line for i, line in enumerate(full) if i not in drop]
        same, moments, differs = agree(whole, _Log(kept, inputs), now)
        if not same:
            why = f"the compacted log would fold differently at {differs}"
            continue
        seqs = [e.get("seq") for i, (_l, e) in enumerate(rows)
                if i not in drop and e and isinstance(e.get("seq"), int)]
        best = {"kept": kept, "profile": number, "moments": moments, "dropped": len(drop),
                "by_type": _by_type(rows, drop), "before": len(rows), "after": len(kept),
                "through_seq": max(seqs) if seqs else None}
        if len(kept) <= LINES_TARGET and sum(len(k) + 1 for k in kept) <= BYTES_TARGET:
            break
    return best or {"why": why}


def _by_type(rows, drop: set[int]) -> dict:
    out: dict[str, int] = {}
    for i in drop:
        kind = (rows[i][1] or {}).get("type") or "?"
        out[kind] = out.get(kind, 0) + 1
    return out


def compact(now: datetime | None = None) -> dict:
    """Compact the log now when a compaction is worth making and folds alike.
    Never raises: ``{"result": "compacted", ...}`` or ``{"result":
    "skipped", "why": ...}``."""
    now = now or routing._now()
    try:
        return _compact(now)
    except routing.LockBusy:
        return {"result": "skipped", "why": "the routing record is busy"}
    except Exception as exc:  # noqa: BLE001 - a failed compaction leaves the log as it was
        return {"result": "skipped", "why": type(exc).__name__}


def _compact(now: datetime) -> dict:
    path = routing.events_file()
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {"result": "skipped", "why": "no routing record"}
    # Whole lines only: a writer's line may be half down at this read. The
    # piece rides in the tail below, verbatim, whole once it is finished.
    data = data[:data.rfind(b"\n") + 1]
    chosen = _choose(data, now)
    if "kept" not in chosen:
        _note(now, chosen["why"])
        return {"result": "skipped", "why": chosen["why"]}
    head = b"".join(line + b"\n" for line in chosen["kept"])  # built before the lock is taken
    name = f"events-{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}.jsonl"
    with routing._lock():
        current = path.read_bytes()
        if not current.startswith(data):
            return {"result": "skipped", "why": "the record changed while it was checked"}
        body = head + current[len(data):]
        _replace(path, body, name)
        _write_state({"at": routing._iso(now), "attempted_at": routing._iso(now), "result": "compacted",
                      "lines_before": chosen["before"], "lines_after": chosen["after"],
                      "bytes_before": len(data), "bytes_after": len(head),
                      "through_seq": chosen["through_seq"], "archive": name, "profile": chosen["profile"],
                      "moments_checked": chosen["moments"], "dropped_by_type": chosen["by_type"]})
        pruned = prune_archives(now)
        with contextlib.suppress(Exception):
            routing._refold_locked(now)
    return {"result": "compacted", "lines_before": chosen["before"], "lines_after": chosen["after"],
            "bytes_after": len(head), "archive": name, "pruned": pruned, "dropped_by_type": chosen["by_type"]}


def _replace(path: Path, body: bytes, name: str) -> None:
    """Write ``body`` beside the log and fsync it, link the log into
    ``archive/`` as ``name`` and rename ``body`` over the log. A crash before
    the rename leaves the log whole; after it, the compacted log."""
    folder = path.parent
    temp = folder / f".events-compact-{os.getpid()}-{secrets.token_hex(3)}.tmp"
    archive = archive_dir()
    archive.mkdir(parents=True, exist_ok=True)
    os.chmod(archive, 0o700)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(path, archive / name)
        except OSError:
            _copy(path, archive / name)
        _fsync_dir(archive)
        os.replace(temp, path)
        _fsync_dir(folder)
        with contextlib.suppress(OSError):
            os.chmod(archive / name, 0o600)  # the old log is now only the archive
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


def _copy(source: Path, target: Path) -> None:
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(source.read_bytes())
        stream.flush()
        os.fsync(stream.fileno())


def _note(now: datetime, why: str) -> None:
    with contextlib.suppress(OSError):
        _write_state(dict(_read_state(), attempted_at=routing._iso(now), result=f"skipped: {why}"))


# ---- archives -------------------------------------------------------------------


def _archives() -> list[tuple[datetime, Path]]:
    out = []
    try:
        names = sorted(archive_dir().iterdir())
    except OSError:
        return []
    for path in names:
        if not (path.name.startswith("events-") and path.name.endswith(".jsonl")):
            continue
        try:
            at = datetime.strptime(path.name[7:23], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        out.append((at, path))
    return sorted(out, reverse=True)


def prune_archives(now: datetime) -> int:
    """Remove archives past both ``ARCHIVE_KEEP_COUNT`` (the newest are kept)
    and ``ARCHIVE_KEEP_DAYS``. An archive inside either bound stays."""
    removed = 0
    for rank, (at, path) in enumerate(_archives()):
        if rank >= ARCHIVE_KEEP_COUNT and now - at > timedelta(days=ARCHIVE_KEEP_DAYS):
            with contextlib.suppress(OSError):
                path.unlink()
                removed += 1
    return removed


# ---- when ----------------------------------------------------------------------


def due(now: datetime | None = None, *, nightly: bool = False) -> bool:
    """Whether the log is over the yellow bound (and, outside the nightly,
    no attempt was made in the last ``MIN_INTERVAL_S``)."""
    now = now or routing._now()
    try:
        small = routing.events_file().stat().st_size < SMALL_BYTES
    except OSError:
        return False
    if small:
        return False
    size = log_size()
    if size["lines"] <= LINES_YELLOW and size["bytes"] <= BYTES_YELLOW:
        return False
    if nightly:
        return True
    tried = routing._parse(_read_state().get("attempted_at"))
    return tried is None or (now - tried).total_seconds() >= MIN_INTERVAL_S


def maybe_compact(now: datetime | None = None, *, nightly: bool = False) -> dict:
    """The automatic compaction: only over the bound. Never raises."""
    try:
        now = now or routing._now()
        if not due(now, nightly=nightly):
            prune_archives(now)
            return {"result": "not due"}
        return compact(now)
    except Exception as exc:  # noqa: BLE001
        return {"result": "skipped", "why": type(exc).__name__}
