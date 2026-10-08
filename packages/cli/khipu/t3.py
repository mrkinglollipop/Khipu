# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""What Khipu knows about T3 Code (pingdotgg/t3code), read-only.

T3 lets one thread switch provider and account mid-conversation. Three facts of
its behaviour reach Khipu, all pinned in docs/plans/2026-10-07-khipu-t3.md:

  - its settings file names every Claude instance and the config folder
    (``homePath``) each one runs with;
  - the first turn after a switch carries a hand-over of earlier turns glued in
    front of what the user typed;
  - it runs helper Claude sessions (thread titles) in throwaway folders.

Khipu never writes T3's settings or database. Every read here degrades quietly:
a missing file, bad JSON or a changed shape is "nothing found", never an error.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# T3 starts its title-generating Claude sessions in temp folders with this name.
HELPER_CWD_MARKER = "t3code-claude-title-"

# ContextHandoffDelivery.ts opens the hand-over with "Context handoff (<strategy>):"
# and ProviderTurnStartService.ts joins it to the typed text as
# `${context}\n\nUser message:\n${userText}`.
HANDOFF_PREFIX = "Context handoff ("
HANDOFF_USER_MARKER = "\n\nUser message:\n"

CLAUDE_DRIVER = "claudeAgent"


def settings_path(home: Path | None = None) -> Path:
    return (home if home is not None else Path.home()) / ".t3" / "userdata" / "settings.json"


def handoff_thread(text: str) -> str | None:
    """Read only the validated header, never quoted history."""
    if not isinstance(text, str):
        return None
    lines = text.replace("\r\n", "\n").lstrip().split("\n", 2)
    if len(lines) < 2 or not re.fullmatch(r"Context handoff \([^)]+\):", lines[0]):
        return None
    match = re.match(r"Provider context handoff\. Thread: (\S+?)\.(?:\s|$)", lines[1])
    return match.group(1) if match else None


def thread_for_session(sid: str) -> str | None:
    """A bounded read of T3's provider projection; failures mean no mapping.

    The connection timeout matches T3's WAL contract, but the busy handler
    and VM deadline are tighter so a locked or large DB cannot stall a hook.
    """
    if not sid:
        return None
    path = Path(os.environ.get("KHIPU_T3_DB") or Path.home() / ".t3/userdata/statev2.sqlite")
    con = None
    try:
        # With T3 closed there is no -wal file, and even a mode=ro open of a
        # WAL database would create -wal/-shm beside it; immutable reads the
        # file as is and leaves T3's folder untouched.
        mode = "?mode=ro" if Path(f"{path}-wal").exists() else "?mode=ro&immutable=1"
        con = sqlite3.connect(path.absolute().as_uri() + mode, uri=True, timeout=0.5)
        con.execute("PRAGMA busy_timeout = 2")
        deadline = time.monotonic() + 0.05
        con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
        row = con.execute(
            "SELECT thread_id FROM orchestration_v2_projection_provider_threads "
            "WHERE CASE WHEN json_valid(payload_json) THEN "
            "json_extract(payload_json, '$.nativeThreadRef.nativeId') END = ? "
            "ORDER BY updated_at DESC LIMIT 1", (sid,),
        ).fetchone()
        return row[0] if row and isinstance(row[0], str) and row[0] else None
    except (OSError, ValueError, sqlite3.Error):
        return None
    finally:
        if con is not None:
            con.close()


def _thread_db_path(home: Path | None = None) -> Path:
    return Path(os.environ.get("KHIPU_T3_DB") or (home if home is not None else Path.home()) / ".t3/userdata/statev2.sqlite")


def _readonly_uri(path: Path) -> str:
    """Active WALs need ``mode=ro``; a closed WAL needs immutable safety."""
    mode = "?mode=ro" if Path(f"{path}-wal").exists() else "?mode=ro&immutable=1"
    return path.absolute().as_uri() + mode


def _lookup_health(path: Path) -> tuple[bool, str | None]:
    """Can this build still read T3's provider-thread projection?

    This intentionally validates the exact three columns ``thread_for_session``
    reads.  It never guesses from a database mtime or a capture: those only say
    T3 was used, not that Khipu can link a native session to its T3 thread.
    """
    if not path.is_file():
        return False, "T3's thread database was not found"
    source_files = [path, Path(f"{path}-wal")]
    try:
        before = [(item, item.stat().st_size, item.stat().st_mtime_ns) for item in source_files if item.is_file()]
        with tempfile.TemporaryDirectory(prefix="khipu-t3-health-") as td:
            staged = Path(td) / path.name
            for item, _, _ in before:
                suffix = "-wal" if item == Path(f"{path}-wal") else ""
                shutil.copyfile(item, Path(f"{staged}{suffix}"))
            after = [(item, item.stat().st_size, item.stat().st_mtime_ns) for item in source_files if item.is_file()]
            if before != after:
                return False, "T3's thread database changed while it was being checked"
            con = None
            try:
                # SQLite may update shared-memory read marks even in mode=ro.
                # Open a verified stable copy so health never writes beside
                # T3's live database or WAL.
                con = sqlite3.connect(_readonly_uri(staged), uri=True, timeout=0.5)
                con.execute("PRAGMA busy_timeout = 2")
                deadline = time.monotonic() + 0.05
                con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
                # Prepare the same column/order expression as
                # ``thread_for_session`` before judging payload shape.
                con.execute(
                    "SELECT thread_id, payload_json, updated_at "
                    "FROM orchestration_v2_projection_provider_threads "
                    "ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
                count, compatible = con.execute(
                    "SELECT COUNT(*), COUNT(CASE WHEN json_valid(payload_json) "
                    "AND typeof(json_extract(payload_json, '$.nativeThreadRef.nativeId')) = 'text' "
                    "AND json_extract(payload_json, '$.nativeThreadRef.nativeId') <> '' THEN 1 END) "
                    "FROM orchestration_v2_projection_provider_threads"
                ).fetchone()
                if count and not compatible:
                    return False, "T3's thread records changed shape (no native session ids found)"
                return True, None
            finally:
                if con is not None:
                    con.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower() or "no such column" in str(exc).lower():
            return False, "T3's thread records changed shape (table not found)"
        return False, "T3's thread lookup is unavailable"
    except (OSError, ValueError, sqlite3.Error):
        return False, "T3's thread lookup is unavailable"


def _thread_linked_capture_evidence() -> tuple[str | None, str | None, int]:
    """Newest stored capture explicitly stamped ``via:t3`` with a thread id.

    Doctor has already refreshed the local hub replica when the hub is
    reachable.  Reading it keeps this check local and fail-open when a
    portable install has no replica or an older replica lacks ``raw``.
    """
    try:
        from khipu.hub_snapshot import snapshot_path

        path = snapshot_path()
        if not path.is_file():
            return None, None, 0
        con = sqlite3.connect(_readonly_uri(path), uri=True, timeout=0.1)
        try:
            deadline = time.monotonic() + 0.05
            con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
            cols = {str(row[1]) for row in con.execute("PRAGMA table_info(episodes)")}
            harness = "harness" if "harness" in cols else "NULL"
            row = con.execute(
                f"SELECT ts, {harness} FROM episodes "
                "WHERE json_valid(raw) "
                "AND json_extract(raw, '$.via') = 't3' "
                "AND COALESCE(json_extract(raw, '$.t3_thread_id'), '') <> '' "
                "ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            today = datetime.fromtimestamp(time.time(), timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")
            count = con.execute(
                "SELECT COUNT(*) FROM episodes "
                "WHERE ts >= ? AND json_valid(raw) "
                "AND json_extract(raw, '$.via') = 't3' "
                "AND COALESCE(json_extract(raw, '$.t3_thread_id'), '') <> ''",
                (today,),
            ).fetchone()
            return (
                str(row[0]) if row and row[0] else None,
                str(row[1]) if row and row[1] else None,
                int(count[0]) if count else 0,
            )
        finally:
            con.close()
    except (ImportError, OSError, ValueError, sqlite3.Error):
        return None, None, 0


def _age_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, time.time() - parsed.timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


def health(home: Path | None = None) -> dict[str, Any] | None:
    """Read-only T3 health for ``khipu doctor`` and the desktop card.

    A T3 card exists only when its settings file exists.  Settings contents are
    deliberately not parsed here: they can hold provider credentials and a
    malformed or newer settings shape must not hide the installed T3 surface.
    """
    if not settings_path(home).is_file():
        return None
    path = _thread_db_path(home)
    lookup_ok, lookup_error = _lookup_health(path)
    activity_mtime = None
    try:
        mtimes = [p.stat().st_mtime for p in (path, Path(f"{path}-wal")) if p.is_file()]
        activity_mtime = max(mtimes) if mtimes else None
    except OSError:
        activity_mtime = None
    activity_age = max(0.0, time.time() - activity_mtime) if activity_mtime is not None else None
    last_capture_at, last_capture_harness, captures_today = _thread_linked_capture_evidence()
    last_capture_age = _age_seconds(last_capture_at)
    recent_without_capture = bool(
        activity_age is not None
        and activity_age <= 86400
        and (last_capture_age is None or last_capture_age > 86400)
    )
    warnings: list[str] = []
    if not lookup_ok:
        warnings.append(lookup_error or "T3's thread lookup is unavailable")
    if recent_without_capture:
        warnings.append("T3 was used in the last day without a thread-linked capture")
    return {
        "detected": True,
        "lookup": {"ok": lookup_ok, "error": lookup_error},
        "used_recently": activity_age is not None and activity_age <= 86400,
        "last_thread_linked_capture_at": last_capture_at,
        "last_thread_linked_capture_age_s": last_capture_age,
        "last_thread_linked_capture_harness": last_capture_harness,
        "thread_linked_captures_today": captures_today,
        "warnings": warnings,
    }


def cache_thread(st: dict, sid: str) -> str | None:
    """Keep hits for the session; retry negative reads after five minutes.

    A provider row can land after its first Stop. Negative caching must not
    label that session as non-T3 forever, or retry sqlite on every Stop.
    """
    now = time.time()
    if st.get("t3_lookup_session") == sid:
        try:
            age = now - float(st.get("t3_lookup_at") or 0)
        except (TypeError, ValueError):
            age = 300
        if st.get("t3_thread_id") or age < 300:
            return st.get("t3_thread_id")
    thread = thread_for_session(sid)
    st.update(t3_lookup_session=sid, t3_lookup_at=now, t3_thread_id=thread)
    return thread


def is_helper_session(cwd: Any) -> bool:
    """A T3 helper session (thread-title generation): Khipu neither recalls
    into it nor captures it."""
    if not isinstance(cwd, str) or not cwd:
        return False
    path = Path(cwd)
    if not path.name.startswith(HELPER_CWD_MARKER):
        return False
    parents = {str(path.parent), str(path.parent.resolve())}
    temp_root = Path(tempfile.gettempdir())
    temp_roots = {str(temp_root), str(temp_root.resolve()), "/tmp", "/private/tmp"}
    return bool(parents & temp_roots) or any(re.fullmatch(
        r"/(?:private/)?var/folders/[^/]+/[^/]+/T", parent,
    ) for parent in parents)


def strip_handoff(text: str, *, prefer_last: bool = False) -> str:
    """The user's own words from a message that starts with T3's hand-over:
    everything after the first ``\\n\\nUser message:\\n`` following the last
    historical item (or the header when there are none). Anything else, and a
    hand-over with no such marker, comes back unchanged.

    The split is ambiguous only when the marker occurs more than once after
    the last item: either the item's own text or the typed text quotes it.
    The first marker never drops typed words (capture's choice); the last one
    never leaks history into a search query (``prefer_last``, recall's)."""
    if not isinstance(text, str):
        return text
    norm = text.replace("\r\n", "\n").lstrip()
    header, newline, rest = norm.partition("\n")
    provider, provider_newline, body = rest.partition("\n")
    if (not newline or not provider_newline
            or not re.fullmatch(r"Context handoff \([^)]+\):", header)
            or not provider.startswith("Provider context handoff. Thread: ")):
        return text
    historical = [m.start() for m in re.finditer(r"(?m)^\[Historical ", norm)]
    start = historical[-1] if historical else len(header) + len(newline) + len(provider)
    marker_at = (norm.rfind if prefer_last else norm.find)(HANDOFF_USER_MARKER, start)
    if marker_at >= 0:
        return norm[marker_at + len(HANDOFF_USER_MARKER):]
    return "" if norm.endswith(HANDOFF_USER_MARKER.rstrip("\n")) else text


def claude_instances(settings_file: Path) -> list[dict[str, Any]]:
    """Every Claude instance in T3's ``settings.json`` (``providerInstances``
    entries whose ``driver`` is ``claudeAgent``), as ``{id, name, home_path,
    enabled}``. ``home_path`` is ``""`` when T3 runs it from the default
    ``~/.claude``. Nothing else in that file is read into the result: it can
    hold tokens."""
    try:
        data = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    instances = data.get("providerInstances") if isinstance(data, dict) else None
    if not isinstance(instances, dict):
        return []
    out: list[dict[str, Any]] = []
    for instance_id, inst in instances.items():
        if not isinstance(inst, dict) or inst.get("driver") != CLAUDE_DRIVER:
            continue
        config = inst.get("config") if isinstance(inst.get("config"), dict) else {}
        home_path = config.get("homePath")
        display = inst.get("displayName")
        if isinstance(display, str) and display.strip():
            name = display.strip()
        else:
            name = "Claude" if instance_id == CLAUDE_DRIVER else str(instance_id)
        out.append({
            "id": str(instance_id),
            "name": name,
            "home_path": home_path.strip() if isinstance(home_path, str) else "",
            "enabled": inst.get("enabled") is not False,
        })
    return out
