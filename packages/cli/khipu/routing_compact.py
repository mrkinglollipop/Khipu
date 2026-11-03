"""A bound on the routing log (``state/routing/events.jsonl``).

The log is append-only and was folded whole on every stale read: at 10,000
lines a fresh fold overran the prompt path's 50 ms budget and the
conversation got no routing. This module compacts it.

What a compaction is: dropped observations are archived and the live log
is replaced by its *checkpoint*, the lines the fold can still
tell apart from the lines left out, in their original order and bytes, with
every later line (the tail) after them. No event line is rewritten. One inert,
timestamped generation marker records the archive commit; readers already
ignore its event type. The fold over the compacted file is the full fold:

  * nothing an owner wrote is ever left out (rules, the note, the priority,
    holds, caps, clears, account lines and every undo or restore step), nor
    a line that cannot be read (doctor counts those), nor any line of an
    account change;
  * a ``usage_sample`` goes only when nothing pace reads can use it (see
    ``_sample_drops``): it is older than twice the longest span
    ``compute_pace`` reads (a rolling day), is not the newest reading of its
    witness and reset cycle before that span, is not the only one of its
    reset time still ahead, does not bridge reset estimates into one cycle,
    and is not the twin of a reading that stays. Every cycle keeps its
    newest reading per witness until it and its reset are ``ANCHOR_DAYS`` old
    (kept for good if the fold can't do without them). A line the fold ignores
    (no reset time) is no reading at all, and counts for nothing;
  * a ``source_seen`` goes unless it is among the last of its Mac and runner
    (only the last is read);
  * a conversation's or a check's ``evidence`` goes only when it is old, its
    end has passed, it is not renewed and it is not among the newest of its
    account, scope and state;
  * an ``observation`` (a source's own sighting) and its renewals go only when
    nothing below can read them (see ``_observation_drops``): it is over, a
    source no longer offers its ``source_id``, and it is not among the newest
    of its kind;
  * a ``reset_check`` goes when old and not among the newest of its account.

Before anything is replaced the compacted log and the full log are folded
at the present and at every moment the fold says its answer changes next
(``valid_until``), and at fixed distances out to a year, with the sources'
present sightings as well as without, and asked what those sightings would
append: they must agree, or nothing is written. The replacement writes the new
file beside the log, fsyncs it, writes the lines left out to ``archive/``
(compressed, byte for byte, each with its place: ``full_log`` puts them back) and
renames the new file over the log (atomic), so a crash at any point leaves a log
that reads, and at worst an uncommitted archive that reconstruction ignores.
Archives are pruned past both count and age, or oldest first to fit the absolute
byte budget. Reconstruction reports damaged or expired observation history.

The planning and the check run outside the routing lock on a copy of the
bytes read; under the lock the log must still begin with those bytes (a
writer only appends), and what was appended meanwhile is carried over
verbatim.
"""
from __future__ import annotations

import contextlib
import gzip
import hashlib
import warnings
import zlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from khipu import routing

ARCHIVE_DIR = "archive"
STATE_NAME = "compaction.json"
GENERATION_TYPE = "_routing_compaction_generation"

# Compaction starts on its own here. At a line a minute a compacted log holds
# about 1,700 lines plus the owner's, which never go: two days of samples, half a
# day of readings still counted, a fortnight of anchors.
LINES_TRIGGER, BYTES_TRIGGER = 2000, 1 << 20
# The bound doctor holds it to. Attempts are ``MIN_INTERVAL_S`` apart, so a healthy
# log peaks 360 lines over what compaction leaves: yellow is that, with room for
# about 900 owner lines, so it means something is wrong, not that the log is
# between compactions. Prompts don't read the log, so nothing here is a prompt's
# budget: a fold of 3,000 lines is about 50 ms. Red is where a compaction has been
# failing for days.
LINES_YELLOW, BYTES_YELLOW = 3000, 2 << 20
LINES_RED, BYTES_RED = 4000, 3 << 20
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
# Absolute on-disk observation archive budget, including recent archives.
ARCHIVE_MAX_BYTES = 64 << 20

# (sample days, samples kept per window, evidence days, evidence kept per
# state, reset-check days, observation hours, observations kept per kind). The
# first profile that reaches the target wins. No observation hours are under
# ``routing_stale_after``'s default and its margin (13 h): an open reading is
# still counted that long, whatever the profile.
PROFILES = (
    (3, 4, 45, 4, 30, 36, 3),
    (2, 3, 21, 3, 14, 24, 2),
    (2, 3, 7, 2, 7, 13, 1),
)
# An observation's end is this far past before it can go.
OBSERVATION_OVER_S = 3600
# The lines that say which account or lane a sighting belongs to: lines either
# side of one are not compared with each other (``_observation_drops``).
_ACCOUNT_CHANGES = frozenset({"account_note", "account_move", "account_rename", "account_migration",
                              "account_binding"})
RESET_CHECKS_KEPT = 3
# The longest span ``routing_pace.compute_pace`` reads back from the fold's
# moment: its horizon is the trailing window (a day at most) or the rolling
# day of a per-day cap (``horizon = 86400`` there). A reading inside twice
# that is never dropped.
PACE_READ_S = 86400
PACE_MARGIN = 2
# A cycle's anchor (``_sample_drops``) goes once it and its reset are this old:
# one per cycle and witness for good would be the log's one unbounded line.
ANCHOR_DAYS = 14
# ``routing_pace._cycles`` links reset estimates at most this far apart into
# one cycle; a bridge between two kept estimates stays under this less a margin.
CYCLE_LINK_S = 3600
CYCLE_BRIDGE_S = 2700
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
    """Doctor's row: ``level`` green, yellow (over the bound a healthy log stays
    under) or red (far over it), with the line count and bytes and the last
    compaction."""
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
        out["warning"] = (f"The routing record holds {lines:,} lines ({nbytes:,} bytes), over its bound of "
                          f"{LINES_YELLOW:,}, and compaction can't shrink it further: {total - gone:,} of "
                          "its lines are the owner's, readings and observations still counted, or the newest "
                          "of their kind, which the fold needs, and are kept.")
        owners = sum(1 for _line, event in _rows(routing.events_file().read_bytes())
                     if event is not None and (event.get("written_by") or {}).get("kind") == "owner")
        out["warning"] += (f" The owner's protected history accounts for {owners:,} lines; "
                           "nothing is broken by keeping that history.")
        out["kept_lines"] = total - gone
    elif level != "green":
        out["warning"] = (f"The routing record holds {lines:,} lines ({nbytes:,} bytes); it is compacted at "
                          f"{LINES_TRIGGER:,} lines or {BYTES_TRIGGER:,} bytes, nightly and after the plan figures "
                          f"refresh" + (f". It is past {LINES_RED:,} lines or {BYTES_RED:,} bytes, far more than "
                                        "compaction leaves, so it may be failing." if level == "red" else "."))
    return out


def _droppable() -> tuple[int, int]:
    """(lines the strongest profile could drop now, lines in the log)."""
    try:
        rows = _rows(routing.events_file().read_bytes())
    except OSError:
        return 0, 0
    now = routing._now()
    try:
        emitted = _emitted(_sightings(routing._inputs(), now))
    except Exception:  # noqa: BLE001 - doctor's row never fails on this
        emitted = None
    return len(plan(rows, now, PROFILES[-1], emitted=emitted, aged_anchors=True)), len(rows)


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


def _sample_drops(groups: dict[tuple, list[tuple[datetime, int, dict]]], now: datetime,
                  sample_days: float, sample_kept: int, aged_anchors: bool = False) -> set[int]:
    """The ``usage_sample`` lines nothing the fold does with samples can use.

    Everything the fold does with a sample is in ``routing.fold`` (through
    ``routing.pace_sample``): ``routing_pace.compute_pace``, ``next_change``
    and the newest reading. For one (account, window) at any moment from now
    on they read:

      * the readings inside the trailing window (a rolling day for a per-day
        cap) and the newest reading before it per witness (Mac and source)
        and reset cycle: every reading younger than ``PACE_MARGIN`` times the
        longest of those spans stays, and the newest reading before that
        point per witness and cycle, which is the anchor every later moment
        falls back on;
      * every reset time still ahead (``next_change`` schedules the fold's
        ``valid_until`` at the earliest one past each moment): one reading
        per distinct reset time ahead stays;
      * the cycles themselves: ``_cycles`` links reset estimates less than an
        hour apart into one cycle, so a reading that bridges two kept
        estimates of one cycle stays;
      * ``_filter``'s own choices: a reading it discards (a duplicate, one
        with too coarse a clock) stays when a kept reading is its twin, so
        dropping the kept one cannot let it back in;
      * the writer's cadence (``routing_pace.should_sample``): the newest
        line of each witness, whatever it holds.

    A line the fold ignores (no reset time) is read by none of this. Groups
    whose readings carry cycle ids keep a whole extra window back."""
    from khipu import routing_pace

    drop: set[int] = set()
    horizon = timedelta(seconds=PACE_MARGIN * PACE_READ_S)
    cut_days = timedelta(days=sample_days)
    anchor_cut = now - timedelta(days=ANCHOR_DAYS) if aged_anchors else None
    for (account, window), items in groups.items():
        keep: set[int] = set()
        cut = now - max(horizon, cut_days)
        parsed = {}
        for _at, i, event in items:
            got = routing.pace_sample(event)
            if got is not None and got[0] == (account, window):
                parsed[i] = dict(got[1], _i=i)
        params = routing_pace._sample_params(window, list(parsed.values())) if parsed else None
        filtered = routing_pace._filter(list(parsed.values()), params) if params else []
        if filtered:
            routing_pace._cycles(filtered, window)
            if any(s["_cycle_id"] for s in filtered):
                cut = min(cut, now - horizon - timedelta(seconds=2 * params["window_s"]))
        # Anything inside the span, and the newest few of the window.
        keep.update(i for at, i, _e in items if at >= cut)
        for _at, i, _e in sorted(items, key=lambda item: (item[0], item[1]), reverse=True)[:sample_kept]:
            keep.add(i)
        # The newest line of each witness, read or not (the writer's cadence).
        newest: dict[tuple, tuple] = {}
        for at, i, event in items:
            witness = (event.get("mac"), event.get("source"))
            if witness not in newest or (at, i) > newest[witness]:
                newest[witness] = (at, i)
        keep.update(i for _at, i in newest.values())
        if filtered:
            # The newest reading of each witness and cycle before the span,
            # for every cycle. Old cycles are not provably unread: on noisy
            # logs (several Macs, skewed clocks, jittered reset estimates)
            # dropping their anchors changed pace's verdict, so each keeps
            # one line per Mac and source. With ``aged_anchors`` one long past
            # goes, and ``agree`` says whether this log could spare it.
            anchors: dict[tuple, dict] = {}
            for s in filtered:
                if s["t"] < cut and not (anchor_cut and s["t"] < anchor_cut and s["reset"] < anchor_cut):
                    ident = (s["mac"], s["source"], s["_cycle_key"])
                    if ident not in anchors or s["t"] > anchors[ident]["t"]:
                        anchors[ident] = s
            keep.update(s["_i"] for s in anchors.values())
            # One reading per reset time still ahead.
            ahead: dict[datetime, dict] = {}
            for s in filtered:
                if s["reset"] > now and (s["reset"] not in ahead or s["t"] > ahead[s["reset"]]["t"]):
                    ahead[s["reset"]] = s
            keep.update(s["_i"] for s in ahead.values())
            # Bridges: ``_cycles`` links reset estimates less than an hour
            # apart into one cycle. Within a cycle, between the first and the
            # last reading that stays, a reading goes only when the next one
            # is still close enough to the last kept; separate cycles need
            # no bridge (dropping can split a cycle, never join two).
            ordered = sorted(filtered, key=lambda s: (s["reset"], s["t"]))
            comp = [0] * len(ordered)
            for pos in range(1, len(ordered)):
                gap = (ordered[pos]["reset"] - ordered[pos - 1]["reset"]).total_seconds()
                comp[pos] = comp[pos - 1] + (1 if gap > CYCLE_LINK_S else 0)
            last_forced: dict[int, int] = {}
            for pos, s in enumerate(ordered):
                if s["_i"] in keep:
                    last_forced[comp[pos]] = pos
            last = None
            for pos, s in enumerate(ordered):
                if pos and comp[pos] != comp[pos - 1]:
                    last = None
                if s["_i"] in keep:
                    last = s["reset"]
                    continue
                if last is None or pos > last_forced.get(comp[pos], -1):
                    continue
                if (ordered[pos + 1]["reset"] - last).total_seconds() > CYCLE_BRIDGE_S:
                    keep.add(s["_i"])
                    last = s["reset"]
            # Twins of the kept readings.
            kept_by_witness: dict[tuple, list[dict]] = {}
            for s in filtered:
                if s["_i"] in keep:
                    kept_by_witness.setdefault((s["mac"], s["source"]), []).append(s)
            filtered_ids = {s["_i"] for s in filtered}
            for i, r in parsed.items():
                if i in filtered_ids or i in keep:
                    continue
                for k in kept_by_witness.get((r["mac"], r["source"]), ()):
                    gap = abs((k["t"] - r["t"]).total_seconds())
                    if gap == 0 or (k["reset"] == r["reset"] and k["used"] == r["used"]
                                    and gap <= (k["t_err"] or 0) + (r["t_err"] or 0)):
                        keep.add(i)
                        break
        drop.update(i for _at, i, _e in items if i not in keep)
    return drop


def _sent_or_last(rows: list[tuple[bytes, dict | None]]) -> set[int]:
    """Lines no compaction may take, whatever their kind: the highest number
    of each Mac (the next line numbers from it, and the upload cursor compares
    with it), and with replication live any line not yet uploaded (the upload
    reads this file, ``routing_replication._sync``: a line gone is never sent)."""
    highest: dict[str, tuple[int, int]] = {}
    for i, (_line, event) in enumerate(rows):
        if event is not None and isinstance(event.get("seq"), int):
            origin = event.get("mac") or "legacy"
            if origin not in highest or event["seq"] >= highest[origin][0]:
                highest[origin] = (event["seq"], i)
    out = {i for _seq, i in highest.values()}
    if not routing._replication_live():
        return out
    from khipu import routing_replication

    state = routing_replication._state()
    if state.get("mac") != routing._local_mac():
        state = {}  # a new sender uploads everything again
    uploaded, deferred = state.get("uploaded") or {}, state.get("deferred") or {}
    for i, (_line, event) in enumerate(rows):
        if event is None:
            continue
        seq = event.get("seq")
        if not isinstance(seq, int) or seq > uploaded.get(event.get("mac") or "legacy", 0) \
                or event.get("id") in deferred:
            out.add(i)
    return out


def _over_at(row: dict, at: datetime, stale_after: int) -> datetime | None:
    """When ``row`` (as ``entry_model.observations`` gives it) stops counting
    for good: a limit at its time, anything else as ``_fold_account``'s
    ``stale`` has it. None when it never does, or when that is not sure."""
    state = row.get("state")
    if state == routing.LIMITED:
        end = routing._parse(row.get("until"))
        last = routing._parse(row.get("expires_at"))
        return max(end, last) if end is not None and last is not None else end
    if state not in (routing.OPEN, routing.DOWN):
        return None
    if "expires_at" in row:
        return routing._parse(row["expires_at"])
    return routing._after(at, stale_after) if stale_after > 0 else None


def _change_names(event: dict) -> list:
    """The accounts and lanes an account change (``_ACCOUNT_CHANGES``) names."""
    names = [event.get(k) for k in ("account", "target", "to")]
    for field in ("bindings", "aliases"):
        value = event.get(field)
        if isinstance(value, dict):
            names += list(value) + [v[0] if isinstance(v, (list, tuple)) and v else v for v in value.values()]
    names += [tuple(pair) for pair in event.get("lanes_cut") or [] if isinstance(pair, (list, tuple))]
    return [n for n in names if isinstance(n, (str, tuple))]


def _unmatched_run(history: list[tuple[datetime, int, dict]]) -> list[tuple[datetime, int, dict]]:
    """The tail of one lane's sightings ``fold``'s ``limited_since`` still
    walks: from the sighting its current run began with, on."""
    start, until_before = None, None
    for pos, (at, _write, row) in enumerate(history):
        until = routing._parse(row.get("until"))
        if row.get("state") != routing.LIMITED or until is None:
            start, until_before = None, None
        elif start is None or (until_before is not None and at > until_before):
            start, until_before = pos, until
        else:
            until_before = until
    return history[start:] if start is not None else []


def _observation_drops(rows: list[tuple[bytes, dict | None]], now: datetime, hours: float, kept: int,
                       emitted: set[str], guarded: set[int]) -> set[int]:
    """The ``observation`` lines and renewals nothing can read. Everything
    that reads an observation, and what keeps its answer:

      * ``routing_entries.observations``: a renewal counts only as the newest
        of its target, author and Mac (the rest of a kept line's renewals go,
        and all of a line that goes);
      * ``routing._fold_account``: of evidence past its end it asks only for
        the newest of a state, a scope, a window, a basis or a source, among
        lines newer than a clear (so a newer line of the same kind answers for
        an older one, whichever clear stands). Each kind (a slot of
        ``routing_entries.slot`` with its state, basis and declaration) keeps
        its newest ``kept`` and the last it wrote; a limit past its time meets
        one of those first in ``disagreement_after``'s walk back. Evidence
        that still counts it reads whole: a limit not yet at its time, or a
        reading not yet stale, stays;
      * ``routing.fold``: the keys and order of its rows, the first of a
        slot; ``limited_since`` of an unmatched lane, its current run
        (``_unmatched_run``); a priority level's end, the earliest reset after
        its ``as_of`` per account (a level the owner backdates later, past
        lines already gone, ends at the resets left); dismissals, their
        targets; ``valid_until``, only ends still ahead;
      * ``routing._unseen_sightings``: a sighting a source still offers is
        matched by ``source_id``, so a line (or renewal) holding one stays. A
        source's window only moves forward (a tail, the newest figures), so an
        id it has stopped offering does not come back;
      * ``routing_entries.source_events``: the last of each slot and Mac, and
        the first (its order breaks a tie), and for ``crossed`` a newer line of
        the same account and state;
      * an account change splits the kinds of the accounts it names: a line's
        account can differ either side, so neither side answers for the other.

    A line in a transaction or batch stays (a part missing voids the whole),
    as does any unreadable field. ``agree`` is the proof; this is only the
    proposal, so a mistake here costs a compaction, not the record."""
    entry_model = routing.entry_model
    raw = [dict(event, _line=i) for i, (_line, event) in enumerate(rows) if event is not None]
    ordered, _bad = routing._order_events(raw, 0)
    events, _open = routing._split_txns(ordered, routing._held_list())
    events = routing._complete_batches(routing._in_write_order(events))  # as ``fold`` reads them
    stale_after = routing.settings()["routing_stale_after"] or 0
    written = [at for at in (routing._parse(e.get("written_at")) for e in events) if at is not None]
    if not written:
        return set()
    span = timedelta(hours=hours)
    cut, tail, over = now - span, max(written) - span, now - timedelta(seconds=OBSERVATION_OVER_S)
    eras: dict[Any, int] = {}
    line_era: dict[int, tuple] = {}
    targets: set[str] = set()
    level_times: set[datetime] = set()
    renewals: dict[str, list[dict]] = {}
    for event in events:
        kind = event.get("type")
        if kind in _ACCOUNT_CHANGES:
            for name in _change_names(event):
                eras[name] = eras.get(name, 0) + 1
        elif kind == "observation_renew":
            if isinstance(event.get("target"), str):
                renewals.setdefault(event["target"], []).append(event)
            continue
        elif isinstance(event.get("target"), str):
            targets.add(event["target"])
        for entry in event.get("entries") if isinstance(event.get("entries"), list) else ():
            as_of = routing._parse(entry["level"].get("as_of")) if isinstance(entry, dict) \
                and isinstance(entry.get("level"), dict) else None
            if as_of is not None:
                level_times.add(as_of)
        if "_line" in event:
            line_era[event["_line"]] = (eras.get(event.get("account"), 0),
                                        eras.get((event.get("runner"), event.get("lane")), 0))
    if targets:
        targets |= {entry_model.source_id(t) for t in targets}

    keep: set[int] = set()
    candidates: set[int] = set()
    target_line: dict[str, int] = {}
    kinds: dict[str, list[tuple[datetime, int, int]]] = {}
    slots: dict[str, list[tuple[int, int]]] = {}
    lanes: dict[tuple, list[tuple[datetime, int, dict]]] = {}
    earliest: dict[tuple, tuple[datetime, int]] = {}
    for write, row in enumerate(entry_model.observations(events, {})):
        at = routing._parse(row.get("written_at"))
        line = row.get("_line")
        if at is None:
            if line is not None:
                keep.add(line)
            continue
        if row.get("runner") and (not row.get("account") or row.get("unmatched")):
            lanes.setdefault((row["runner"], row.get("lane") or ""), []).append((at, write, row))
        # The fold can read only confirmed resets to end a level.
        reset = routing._confirmed_level_reset(row)
        for as_of in level_times if reset is not None else ():
            if reset > as_of:
                ident = (as_of, row.get("account"), row.get("runner"), row.get("lane"), line_era.get(line))
                if ident not in earliest or reset < earliest[ident][0]:
                    earliest[ident] = (reset, line if line is not None else -1)
        if line is None or not (row.get("type") == "observation" or row.get("source_id")):
            continue
        candidates.add(line)
        if isinstance(row.get("id"), str):
            target_line[row["id"]] = line
        written_at = routing._parse(rows[line][1].get("written_at"))
        ends = _over_at(row, at, stale_after)
        if (ends is None or ends > over or at > cut or written_at is None or written_at > tail or line in guarded
                or row.get("txn") or row.get("batch") or row.get("id") in emitted or row.get("source_id") in emitted):
            keep.add(line)
        slot = entry_model.slot(row)
        slots.setdefault(json.dumps([slot, row.get("mac")], default=str), []).append((write, line))
        if row.get("id") in targets or row.get("source_id") in targets:
            keep.add(line)
            continue  # dismissed: read by its target's name alone, and answers for no other line
        kind = json.dumps([slot, row.get("mac"), row.get("type"), row.get("state"), row.get("basis"),
                           row.get("declaration"), line_era.get(line)], default=str, sort_keys=True)
        kinds.setdefault(kind, []).append((at, write, line))
    for items in kinds.values():
        items.sort(reverse=True)
        floor = items[min(kept, len(items)) - 1][0]
        keep.update(line for at, _write, line in items if at >= floor)
        keep.add(max(items, key=lambda item: item[1])[2])
    for items in slots.values():
        keep.add(min(items)[1])
        keep.add(max(items)[1])
    for history in lanes.values():
        history.sort(key=lambda item: (item[0], item[1]))
        keep.update(row["_line"] for _at, _write, row in _unmatched_run(history) if "_line" in row)
    keep.update(line for _reset, line in earliest.values() if line >= 0)

    # Renewals: the source's id on one keeps it, and the line it renews.
    def young(event: dict) -> bool:
        at = routing._parse(rows[event["_line"]][1].get("written_at")) if "_line" in event else None
        return at is None or at > tail or event["_line"] in guarded or bool(event.get("txn") or event.get("batch"))

    held: set[int] = set()
    for target, group in renewals.items():
        for event in group:
            if "_line" in event and event.get("source_id") in emitted:
                held.add(event["_line"])
                if target in target_line:
                    keep.add(target_line[target])
    drop = candidates - keep
    for target, group in renewals.items():
        line = target_line.get(target)
        if line is None:
            continue  # its target is not an observation this log can see
        if line in drop:
            drop.update(e["_line"] for e in group if "_line" in e and e["_line"] not in held and not young(e))
            continue
        newest: dict[str, datetime] = {}
        for event in group:
            witness = routing._parse(event.get("witness_at"))
            if witness is not None:
                author = json.dumps([event.get("written_by"), event.get("mac")], default=str, sort_keys=True)
                newest[author] = max(newest.get(author, witness), witness)
        for event in group:
            witness = routing._parse(event.get("witness_at"))
            author = json.dumps([event.get("written_by"), event.get("mac")], default=str, sort_keys=True)
            if witness is not None and witness < newest[author] and "_line" in event \
                    and event["_line"] not in held and not young(event):
                drop.add(event["_line"])
    return drop


def plan(rows: list[tuple[bytes, dict | None]], now: datetime, profile: tuple,
         emitted: set[str] | None = None, aged_anchors: bool = False) -> set[int]:
    """The indexes of ``rows`` (line, its event or None) the fold can do
    without, under ``profile``. Anything not sure is kept: ``emitted`` is the
    ids the sources offer now, and None, that they could not be read, keeps
    every observation; ``aged_anchors`` lets old cycles' anchors go."""
    sample_days, sample_kept, evidence_days, evidence_kept, reset_days, seen_hours, seen_kept = profile
    evidence_cut = now - timedelta(days=evidence_days)
    reset_cut = now - timedelta(days=reset_days)
    drop: set[int] = set()
    samples: dict[tuple, list[tuple[datetime, int, dict]]] = {}
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
                samples.setdefault((event["account"], event.get("window")), []).append((at, i, event))
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
    drop.update(_sample_drops(samples, now, sample_days, sample_kept, aged_anchors))
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
    guarded = _sent_or_last(rows)
    drop -= guarded
    if emitted is not None:
        # Their own tail guard, by the hours of the profile, is inside.
        try:
            drop |= _observation_drops(rows, now, seen_hours, seen_kept, emitted, guarded)
        except Exception:  # noqa: BLE001 - not sure: every observation stays
            pass
    return drop


def _generation(data: bytes) -> str | None:
    try:
        row = json.loads(data.partition(b"\n")[0])
    except ValueError:
        return None
    if not isinstance(row, dict) or set(row) != {"type", "generation", "written_at", "seq"} or row["type"] != GENERATION_TYPE:
        return None
    if type(row["seq"]) is not int or row["seq"] != 0:
        return None
    value = row["generation"]
    return value if isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value) else None


def _content_lines(data: bytes) -> list[bytes]:
    # The first line is commit metadata, never an owner line or observation.
    if _generation(data) is not None:
        data = data.partition(b"\n")[2]
    return [line for line in data.split(b"\n") if line.strip()]


def _rows(data: bytes) -> list[tuple[bytes, dict | None]]:
    return [(line, routing._line_event(line)) for line in _content_lines(data)]


# ---- the check ------------------------------------------------------------------


class _Log:
    """One log's lines parsed once, folded at any moment (the fold copies
    what it is given, so one parse serves every moment). ``sightings`` is the
    sources' (evidence, status) now, for the answers that read them."""

    def __init__(self, lines: list[bytes], inputs: tuple, sightings: tuple | None = None):
        self.raw = [e for e in (routing._line_event(line) for line in lines) if e is not None]
        self.inputs = inputs
        self.sightings = sightings if sightings and (sightings[0] or sightings[1] is not None) else None
        self.cache: dict[datetime, dict] = {}
        self.seen_cache: dict[datetime, dict] = {}

    def _events(self) -> list[dict]:
        ordered, _bad = routing._order_events(self.raw, 0)
        return routing._split_txns(ordered, routing._held_list())[0]

    def at(self, when: datetime) -> dict:
        if when not in self.cache:
            _fingerprint, cfg, known = self.inputs
            self.cache[when] = routing.fold(self._events(), now=when, cfg=cfg, known=known, extra=(), sources=None)
        return self.cache[when]

    def seen_at(self, when: datetime) -> dict | None:
        """``at``, as ``routing._fold_body`` folds it: with the sightings no
        line of this log holds. None when no source says anything."""
        if self.sightings is None:
            return None
        if when not in self.seen_cache:
            _fingerprint, cfg, known = self.inputs
            events = self._events()
            self.seen_cache[when] = routing.fold(events, now=when, cfg=cfg, known=known,
                                                 extra=routing._unseen_sightings(self.sightings[0], events),
                                                 sources=self.sightings[1])
        return self.seen_cache[when]

    def would_write(self) -> list[dict]:
        """What the sightings would append to this log, ``routing._refold_locked``'s
        batch: a new line's random id left out, the line a renewal names not."""
        if self.sightings is None or not self.sightings[0]:
            return []
        _fingerprint, cfg, known = self.inputs
        events = self._events()
        ordered = routing._in_write_order(events)
        batch, _ids = routing._source_batch(self.sightings[0], events, ordered, cfg, known, cfg.get("_mac"))
        return [{k: v for k, v in row.items() if k != "id"} for row in batch]


def _normal(body: dict) -> str:
    return json.dumps({k: v for k, v in body.items() if k not in _VOLATILE}, sort_keys=True, default=str)


def agree(full: "_Log", kept: "_Log", now: datetime) -> tuple[bool, int, str | None]:
    """(whether the two logs answer alike, how many moments were compared, the
    first moment they differ at). The moments are now, each ``valid_until``
    the full fold names after it (with the sources' sightings and without), and
    fixed distances out; each is compared without the sightings, and with them
    (``_Log.seen_at``), and last what the sightings would append."""
    moments, at = [now], now
    for _ in range(CHAIN_MOST - 1):
        nxt = routing._parse(full.at(at).get("valid_until"))
        if nxt is None or nxt <= at:
            break
        moments.append(nxt)
        at = nxt
    if full.sightings is not None:
        at = now
        for _ in range(CHAIN_MOST - 1):
            nxt = routing._parse(full.seen_at(at).get("valid_until"))
            if nxt is None or nxt <= at:
                break
            moments.append(nxt)
            at = nxt
    moments += [now + timedelta(seconds=s) for s in CHECK_OFFSETS_S]
    moments = sorted(set(moments), key=moments.index)
    for count, when in enumerate(moments, 1):
        if _normal(full.at(when)) != _normal(kept.at(when)):
            return False, count, routing._iso(when)
        if full.sightings is not None and _normal(full.seen_at(when)) != _normal(kept.seen_at(when)):
            return False, count, routing._iso(when)
    if full.would_write() != kept.would_write():
        return False, len(moments), "the sources' next lines"
    return True, len(moments), None


# ---- the compaction -------------------------------------------------------------


def _sightings(inputs: tuple, now: datetime) -> tuple[list[dict], dict | None] | None:
    """The sources' (evidence, status) now, read as a fold reads them; None
    when they could not be read."""
    try:
        extra, status, _cache = routing._sources(inputs[1], now, routing._load_view())
        return list(extra), status
    except Exception:  # noqa: BLE001 - not sure: ``plan`` keeps every observation
        return None


def _emitted(sightings: tuple | None) -> set[str] | None:
    """Every id a source offers now, as a line of the log could hold it."""
    if sightings is None:
        return None
    try:
        ids = {item["id"] for item in sightings[0] if item.get("id")}
        return ids | {routing.entry_model.source_id(i) for i in ids}
    except Exception:  # noqa: BLE001
        return None


def _choose(data: bytes, now: datetime) -> dict:
    """The best verified compaction of ``data``: {"kept": lines, "profile",
    "moments", "dropped", "by_type", "positions"}, or {"why": ...} when there
    is none worth making. A profile whose old anchors or observations can't be
    left out alike is tried again keeping them, so they never hold up the rest."""
    rows = _rows(data)
    full = [line for line, _e in rows]
    inputs = routing._inputs()
    sightings = _sightings(inputs, now)
    emitted = _emitted(sightings)
    whole = _Log(full, inputs, sightings)
    best: dict | None = None
    why = "nothing the fold can do without"
    for number, profile in enumerate(PROFILES, 1):
        tries = [(emitted, True), (emitted, False)] if emitted is not None else []
        for offered, aged in [*tries, (None, True), (None, False)]:
            drop = plan(rows, now, profile, emitted=offered, aged_anchors=aged)
            if len(drop) < MIN_GAIN * len(rows):
                why = "less than a tenth of the lines can go"
                continue
            kept = [line for i, line in enumerate(full) if i not in drop]
            same, moments, differs = agree(whole, _Log(kept, inputs, sightings), now)
            if not same:
                why = f"the compacted log would fold differently at {differs}"
                continue
            if best is None or len(kept) < best["after"]:
                seqs = [e.get("seq") for i, (_l, e) in enumerate(rows)
                        if i not in drop and e and isinstance(e.get("seq"), int)]
                best = {"kept": kept, "profile": number, "moments": moments, "dropped": len(drop),
                        "by_type": _by_type(rows, drop), "before": len(rows), "after": len(kept),
                        "through_seq": max(seqs) if seqs else None, "positions": sorted(drop),
                        "left_out": [full[i] for i in sorted(drop)]}
            break
        if best is not None and best["after"] <= LINES_TARGET \
                and sum(len(k) + 1 for k in best["kept"]) <= BYTES_TARGET:
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
    name = f"events-{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}.jsonl.gz"
    archive = _archive_bytes(chosen, now)
    with routing._lock():
        current = path.read_bytes()
        if not current.startswith(data):
            return {"result": "skipped", "why": "the record changed while it was checked"}
        body = head + current[len(data):]
        _replace(path, body, name, archive)
        _write_state({**_read_state(), "at": routing._iso(now), "attempted_at": routing._iso(now), "result": "compacted",
                      "lines_before": chosen["before"], "lines_after": chosen["after"],
                      "bytes_before": len(data), "bytes_after": len(head),
                      "through_seq": chosen["through_seq"], "archive": name, "profile": chosen["profile"],
                      "moments_checked": chosen["moments"], "dropped_by_type": chosen["by_type"]})
        pruned = prune_archives(now)
        with contextlib.suppress(Exception):
            routing._refold_locked(now)
    return {"result": "compacted", "lines_before": chosen["before"], "lines_after": chosen["after"],
            "bytes_after": len(head), "archive": name, "pruned": pruned, "dropped_by_type": chosen["by_type"]}


def _archive_bytes(chosen: dict, now: datetime) -> bytes:
    """The lines left out, as written, after a header with each one's place
    among the lines the compaction read."""
    original = list(chosen["kept"])
    for position, line in zip(chosen["positions"], chosen["left_out"]):
        original.insert(position, line)
    header = json.dumps({"compacted_at": routing._iso(now), "before": chosen["before"],
                         "positions": chosen["positions"], "kept_count": len(chosen["kept"]),
                         "generation": _line_hash(chosen["kept"]),
                         "original": _line_hash(original)}, sort_keys=True).encode("utf-8")
    return gzip.compress(b"".join(line + b"\n" for line in [header, *chosen["left_out"]]), 9, mtime=0)


def _replace(path: Path, body: bytes, name: str, archive_body: bytes) -> None:
    """Write ``body`` beside the log and fsync it, write the lines it leaves
    out to ``archive/`` as ``name`` and rename ``body`` over the log. A crash
    before the rename leaves the log whole and an uncommitted archive;
    after it, the generation marker authorizes that archive's inverse."""
    folder = path.parent
    temp = folder / f".events-compact-{os.getpid()}-{secrets.token_hex(3)}.tmp"
    archive = archive_dir()
    archive.mkdir(parents=True, exist_ok=True)
    os.chmod(archive, 0o700)
    generation = secrets.token_hex(16)
    previous = _generation(path.read_bytes()) if path.exists() else None
    header, *left_out = gzip.decompress(archive_body).split(b"\n")[:-1]
    meta = dict(json.loads(header), generation_id=generation, previous_generation_id=previous)
    archive_body = gzip.compress(b"\n".join([json.dumps(meta, sort_keys=True).encode(), *left_out, b""]), 9, mtime=0)
    # Sequence zero is below every upload cursor, including the legacy
    # cursor. Metadata must never acknowledge a real event on its behalf.
    marker = json.dumps({"type": GENERATION_TYPE, "generation": generation, "seq": 0,
                         "written_at": meta["compacted_at"]}, sort_keys=True).encode() + b"\n"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(marker + body)
            stream.flush()
            os.fsync(stream.fileno())
        _write_archive(archive / name, archive_body)
        _fsync_dir(archive)
        os.replace(temp, path)
        _fsync_dir(folder)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


def _write_archive(target: Path, data: bytes) -> None:
    temp = target.parent / f".archive-{os.getpid()}-{secrets.token_hex(6)}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
        _fsync_dir(target.parent)
    finally:
        temp.unlink(missing_ok=True)


def _clean_archive_temps() -> None:
    # The caller holds the routing lock, as every archive publisher does.
    # Any remaining temporary file belongs to an interrupted publication.
    for folder, pattern in ((archive_dir(), ".archive-*.tmp"), (_dir(), ".events-compact-*.tmp")):
        for path in folder.glob(pattern):
            with contextlib.suppress(OSError):
                path.unlink()


def _note(now: datetime, why: str) -> None:
    with contextlib.suppress(OSError):
        with routing._lock():
            _write_state(dict(_read_state(), attempted_at=routing._iso(now), result=f"skipped: {why}"))


# ---- archives -------------------------------------------------------------------


def _archives() -> list[tuple[datetime, Path]]:
    out = []
    try:
        names = sorted(archive_dir().iterdir())
    except OSError:
        return []
    for path in names:
        # A ``.jsonl`` is a whole log, as compaction used to keep them.
        if not (path.name.startswith("events-") and path.name.endswith((".jsonl", ".jsonl.gz"))):
            continue
        try:
            at = datetime.strptime(path.name[7:23], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        out.append((at, path))
    return sorted(out, reverse=True)


def _line_hash(lines: list[bytes]) -> str:
    return hashlib.sha256(b"".join(line + b"\n" for line in lines)).hexdigest()


def full_log() -> list[bytes]:
    """Reconstruct only generations committed in the log, retaining appended tails.

    The generation marker is committed atomically with the compacted log.
    Digests validate each inverse, and the header links to the previous
    generation. A pre-swap archive cannot authorize reconstruction.
    """
    data = routing.events_file().read_bytes()
    generation = _generation(data)
    lines = _content_lines(data)
    if _read_state().get("archive_retention_expired"):
        warnings.warn("Archive retention: older observations are unavailable; owner history remains in the log", RuntimeWarning)
    pending = []
    for _at, path in _archives():
        if not path.name.endswith(".gz"):
            continue
        try:
            decoded = gzip.decompress(path.read_bytes())
            if not decoded.endswith(b"\n"):
                raise ValueError("unterminated archive")
            header, *left_out = decoded.split(b"\n")[:-1]
            meta = json.loads(header)
            if "generation_id" not in meta:
                continue  # published before log replacement, or an uncommitted old format
            if not isinstance(meta["generation_id"], str) or not isinstance(meta.get("previous_generation_id"), (str, type(None))):
                raise ValueError("invalid generation link")
            count, positions = meta["kept_count"], meta["positions"]
            if (not isinstance(count, int) or count < 0 or not isinstance(positions, list)
                    or len(positions) != len(left_out)
                    or any(not isinstance(p, int) or p < 0 or p > count + i
                           for i, p in enumerate(positions))
                    or positions != sorted(set(positions))
                    or meta["before"] != count + len(left_out)
                    or not isinstance(meta["generation"], str) or not isinstance(meta["original"], str)):
                raise ValueError("invalid archive header")
        except (OSError, EOFError, ValueError, KeyError, TypeError, zlib.error):
            warnings.warn(f"Archive {path.name} is damaged or unverified; its history is unavailable", RuntimeWarning)
            continue
        pending.append((meta, left_out))
    # Match the committed chain, not filename order (compactions may share a second).
    while pending:
        matched = False
        for i, (meta, left_out) in enumerate(pending):
            count = meta["kept_count"]
            if meta["generation_id"] != generation:
                continue
            if _line_hash(lines[:count]) != meta["generation"]:
                warnings.warn("Committed archive prefix is damaged; its history is unavailable", RuntimeWarning)
                pending.pop(i)
                matched = True
                break
            restored = list(lines[:count])
            positions = meta["positions"]
            for position, line in zip(positions, left_out):
                restored.insert(position, line)
            if len(restored) != meta["before"] or _line_hash(restored) != meta["original"]:
                warnings.warn("Archive generation is damaged; its history is unavailable", RuntimeWarning)
                pending.pop(i)
                matched = True
                break
            lines = restored + lines[count:]
            generation = meta["previous_generation_id"]
            pending.pop(i)
            matched = True
            break
        if not matched:
            break
    if generation is not None:
        warnings.warn("A committed archive generation is unavailable; returning the remaining history", RuntimeWarning)
    return lines


def prune_archives(now: datetime) -> int:
    with routing._lock():
        return _prune_archives(now)


def _prune_archives(now: datetime) -> int:
    """Expire by age/count, then enforce the absolute byte budget oldest first.

    Persist the retention boundary before deletion so a crash still reports
    the unavailable history. Owner lines are never stored in these archives.
    """
    _clean_archive_temps()
    archives = _archives()
    sizes = {}
    for _at, path in archives:
        with contextlib.suppress(OSError):
            sizes[path] = path.stat().st_size
    expired = {path for rank, (at, path) in enumerate(archives)
               if rank >= ARCHIVE_KEEP_COUNT and now - at > timedelta(days=ARCHIVE_KEEP_DAYS)}
    total = sum(size for path, size in sizes.items() if path not in expired)
    for _at, path in reversed(archives):
        if total <= ARCHIVE_MAX_BYTES:
            break
        if path not in expired:
            expired.add(path)
            total -= sizes.get(path, 0)
    if expired:
        _write_state(dict(_read_state(), archive_retention_expired=True))
    removed = 0
    for path in expired:
        with contextlib.suppress(OSError):
            path.unlink()
            removed += 1
    return removed


# ---- when ----------------------------------------------------------------------


def due(now: datetime | None = None, *, nightly: bool = False) -> bool:
    """Whether the log is over the line compaction starts at (and, outside the
    nightly, no attempt was made in the last ``MIN_INTERVAL_S``)."""
    now = now or routing._now()
    try:
        small = routing.events_file().stat().st_size < SMALL_BYTES
    except OSError:
        return False
    if small:
        return False
    size = log_size()
    if size["lines"] <= LINES_TRIGGER and size["bytes"] <= BYTES_TRIGGER:
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
