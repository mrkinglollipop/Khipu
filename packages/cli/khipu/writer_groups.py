"""Durable records of running legacy writer process groups.

Scope: docs/plans/2026-10-11-capture-identity.md ("Durable forget: one memory
lock"). A capture holds the memory lock shared while the legacy file writer's
process group runs. If the capture itself is killed with SIGKILL, its locks
go with it while the group (a grandchild that appends the line) lives on.
So every group is recorded here before it may run, and a forget, holding the
lock exclusive, stops every recorded group that is still alive before it
looks at the legacy logs. A record whose group is gone, or whose number now
belongs to an unrelated process (its leader started at another time), is
stale and is cleared. A record that cannot be read is kept and keeps the
forget open until it can be read or the group its file name gives is gone.

A record is written atomically and names the group id and the start time of
its leader. The group's leader is the process the capture started; a group id
cannot be reused while any member of the group is alive.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

DIR_NAME = ".legacy_writers"
_TERM_GRACE = 5.0
_KILL_GRACE = 5.0


def records_dir() -> Path:
    from khipu.paths import ensure_data_dir

    return ensure_data_dir() / DIR_NAME


def _record_path(pgid: int) -> Path:
    return records_dir() / f"{int(pgid)}.json"


def start_time(pid: int) -> str | None:
    """The process's start time as ps prints it, or None when there is no
    such process."""
    try:
        res = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))],
                             capture_output=True, text=True, timeout=10,
                             env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    value = " ".join(res.stdout.split())
    return value if res.returncode == 0 and value else None


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Darwin answers EPERM while the group holds only a zombie that has
        # not been reaped yet: something is still there.
        return True
    return True


def record(pgid: int, started: str) -> Path:
    """Write the group's record atomically and durably. Raises OSError; the
    caller must not let the group run without it."""
    directory = records_dir()
    directory.mkdir(parents=True, exist_ok=True)
    dest = _record_path(pgid)
    fd, name = tempfile.mkstemp(prefix=".tmp.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"pgid": int(pgid), "start": started}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, dest)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    dir_fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return dest


def clear(pgid: int) -> None:
    """Remove the group's record, only once no process of it is left."""
    if not group_alive(pgid):
        _record_path(pgid).unlink(missing_ok=True)


def _ours(pgid: int, started: str) -> bool:
    """A live group is the recorded one unless a process now holds the group
    id as its own pid with another start time (the id was reused after the
    recorded group ended). A group whose leader is gone keeps its id while
    any member lives, so it is still the recorded one."""
    now = start_time(pgid)
    return now is None or now == started


def _named_group(path: Path) -> int | None:
    """The group id a record's file name gives (``record`` names it
    ``<pgid>.json``), or None when the name is not one."""
    try:
        pgid = int(path.stem)
    except ValueError:
        return None
    return pgid if pgid > 1 and path.name == f"{pgid}.json" else None


def _parse_record(path: Path) -> tuple[int, str]:
    """The (group id, leader start time) of a record, validated against the
    exact shape ``record`` writes: an object with only ``pgid`` (an integer
    above 1 that matches the file name) and ``start`` (a non-empty string).
    Anything else raises ValueError. Values are never coerced: a null or
    numeric start turned into a string would read as a reused group id and
    clear the record while its writer lives."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"pgid", "start"}:
        raise ValueError("not a writer record")
    pgid, started = data["pgid"], data["start"]
    if type(pgid) is not int or pgid <= 1 or path.name != f"{pgid}.json":
        raise ValueError("invalid group id")
    if not isinstance(started, str) or not started.strip():
        raise ValueError("invalid start time")
    return pgid, started


def _wait_gone(pgid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while group_alive(pgid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def _stop(pgid: int) -> bool:
    for sig, grace in ((signal.SIGTERM, _TERM_GRACE), (signal.SIGKILL, _KILL_GRACE)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        if _wait_gone(pgid, grace):
            return True
    return False


def sweep() -> dict[str, Any]:
    """Stop every recorded writer group that is still alive and clear every
    record whose group is gone. ``ok`` is False while any recorded group
    could not be confirmed gone, or the records could not be read.

    The caller holds the memory lock exclusive, so no new group starts
    while this runs."""
    locations: list[dict[str, str]] = []
    try:
        directory = records_dir()
        # scandir, never glob: glob hides a folder it may not list, which
        # would read as no records while a recorded writer still runs.
        try:
            with os.scandir(directory) as listing:
                names = [entry.name for entry in listing]
        except FileNotFoundError:
            names = []
        paths = sorted(directory / name for name in names if name.endswith(".json"))
    except OSError as exc:
        why = f"legacy writer records could not be read ({type(exc).__name__}), so the forget stays open"
        return {"ok": False, "stopped": 0, "locations": [
            {"place": "legacy writer", "path": None, "state": "not checked", "why": why}]}
    stopped = 0
    ok = True
    for path in paths:
        try:
            pgid, started = _parse_record(path)
        except (OSError, ValueError):
            # A record damaged after it was written still names a group that
            # may be running, so it fails closed: kept, and the forget stays
            # open, until a sweep reads it or the group its name gives is gone.
            # Its group is never stopped: without the start time it cannot be
            # told apart from an unrelated process that reused the number.
            named = _named_group(path)
            if named is not None and not group_alive(named):
                path.unlink(missing_ok=True)
                locations.append({"place": "legacy writer", "path": str(path), "state": "clean"})
                continue
            ok = False
            until = (f"a sweep can read it or no process of group {named} runs"
                     if named is not None else "the owner removes it (its name gives no group)")
            locations.append({"place": "legacy writer", "path": str(path), "state": "not checked",
                              "why": (f"legacy writer record {path.name} could not be read, so "
                                      "its writer may still write a copy; the forget stays open "
                                      f"until {until}")})
            continue
        if not group_alive(pgid) or not _ours(pgid, started):
            path.unlink(missing_ok=True)
            locations.append({"place": "legacy writer", "path": str(path), "state": "clean"})
            continue
        if _stop(pgid):
            stopped += 1
            path.unlink(missing_ok=True)
            locations.append({"place": "legacy writer", "path": str(path), "state": "removed"})
            continue
        ok = False
        locations.append({"place": "legacy writer", "path": str(path), "state": "copy remains",
                          "why": (f"legacy writer process group {pgid} is still running and "
                                  "could not be stopped, so it may still write a copy")})
    return {"ok": ok, "stopped": stopped, "locations": locations}
