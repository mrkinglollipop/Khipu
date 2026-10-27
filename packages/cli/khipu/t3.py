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

import hashlib
import json
import os
import re
import shlex
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


# How many of the newest provider-thread rows the hook lookup examines.
# A session's row is written when T3 starts that session, so at hook time it
# is among the newest rows; examining a bounded newest window (by rowid,
# which SQLite can walk backwards without sorting or touching older rows)
# keeps a multi-gigabyte projection from turning every hook into a full
# table scan. Older rows fall back to the native identity, as a miss did
# before. When T3 (or a future schema) indexes the native id expression,
# that index is used instead and the whole table is in reach again.
#
# Fallback window (no ``rowid``): on a WITHOUT ROWID table (or any schema
# where the rowid query errors naming rowid) the lookup retries with the
# newest ``_LOOKUP_WINDOW`` rows by ``updated_at`` (a plain LIMITed scan of
# the first ``_LOOKUP_WINDOW`` stored rows when ``updated_at`` is also
# unavailable). The window is the same size, so a row older than the newest
# 1024 by that ordering can still miss; the retry exists so a schema
# without rowid does not silently miss every row inside the window.
_LOOKUP_WINDOW = 1024


def _distinct_threads(rows) -> str | None:
    """One thread id from candidate rows, or None when they disagree.

    A native id reused across two distinct threads must not silently pick
    one thread's ledger; several rows for the same thread (a provider
    switch) still resolve to that thread."""
    threads: list[str] = []
    for row in rows:
        thread = row[0] if row else None
        if isinstance(thread, str) and thread and thread not in threads:
            threads.append(thread)
            if len(threads) > 1:
                return None
    return threads[0] if threads else None


def _fallback_window_rows(con, sid: str):
    """Bounded lookup for tables with no usable ``rowid``.

    Tries the newest ``_LOOKUP_WINDOW`` rows by ``updated_at`` first, then
    a plain LIMITed scan of the first ``_LOOKUP_WINDOW`` stored rows when
    that ordering is unavailable. Raises ``sqlite3.Error`` when neither
    bounded query can run, so the caller reports a failure, not a miss.
    """
    guarded = ("WHERE CASE WHEN json_valid(payload_json) THEN "
               "json_extract(payload_json, '$.nativeThreadRef.nativeId') END = ?")
    try:
        return con.execute(
            "SELECT thread_id FROM ("
            "SELECT thread_id, payload_json FROM orchestration_v2_projection_provider_threads "
            f"ORDER BY updated_at DESC LIMIT ?) {guarded}",
            (_LOOKUP_WINDOW, sid),
        ).fetchall()
    except sqlite3.Error:
        return con.execute(
            "SELECT thread_id FROM ("
            "SELECT thread_id, payload_json FROM orchestration_v2_projection_provider_threads "
            f"LIMIT ?) {guarded}",
            (_LOOKUP_WINDOW, sid),
        ).fetchall()


def thread_for_session_result(sid: str) -> tuple[str | None, bool]:
    """``(thread, definitive)`` for a native session id.

    ``definitive`` is True only when a query actually completed: a row was
    found (``thread`` set, or None when the completed query matched rows
    naming more than one distinct thread), or a successful query matched
    zero rows. A missing database, a lock or timeout, an interrupted or
    otherwise failing query is ``(None, False)`` — a failure, not a miss —
    so callers must not treat it as a settled negative answer.
    """
    if not sid:
        return None, True
    path = Path(os.environ.get("KHIPU_T3_DB") or Path.home() / ".t3/userdata/statev2.sqlite")
    if not path.is_file():
        return None, False
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
        indexed = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'orchestration_v2_projection_provider_threads' "
            "AND sql LIKE '%nativeId%' LIMIT 1"
        ).fetchone()
        if indexed:
            # The CASE/json_valid guard would defeat the expression index,
            # so match it exactly; a malformed row errors and falls through
            # to the guarded window query below.
            try:
                rows = con.execute(
                    "SELECT DISTINCT thread_id FROM orchestration_v2_projection_provider_threads "
                    "WHERE json_extract(payload_json, '$.nativeThreadRef.nativeId') = ? "
                    "LIMIT 2", (sid,),
                ).fetchall()
                return _distinct_threads(rows), True
            except sqlite3.Error:
                pass
        try:
            rows = con.execute(
                "SELECT thread_id FROM ("
                "SELECT thread_id, payload_json FROM orchestration_v2_projection_provider_threads "
                "ORDER BY rowid DESC LIMIT ?) "
                "WHERE CASE WHEN json_valid(payload_json) THEN "
                "json_extract(payload_json, '$.nativeThreadRef.nativeId') END = ?",
                (_LOOKUP_WINDOW, sid),
            ).fetchall()
        except sqlite3.Error as exc:
            if "rowid" not in str(exc).lower():
                raise
            rows = _fallback_window_rows(con, sid)
        return _distinct_threads(rows), True
    except (OSError, ValueError, sqlite3.Error):
        return None, False
    finally:
        if con is not None:
            con.close()


def thread_for_session(sid: str) -> str | None:
    """A bounded read of T3's provider projection; failures mean no mapping.

    The connection timeout matches T3's WAL contract, but the busy handler
    and VM deadline are tighter so a locked or large DB cannot stall a hook.
    The query never evaluates the JSON payload outside a bounded newest
    window (or an index on the native id, when one exists), and it returns
    None when the native id names more than one distinct thread. Callers
    that must tell a settled miss from a failed lookup (and so must not
    settle anything on a failure) use ``thread_for_session_result``.
    """
    thread, _definitive = thread_for_session_result(sid)
    return thread


def thread_title(thread_id: str) -> str | None:
    """A T3 thread's own title, read read-only from its thread projection.

    Fail-open like every T3 read here: a missing database, a changed shape
    or a locked file is "no title known", never an error. Only a short
    one-line title leaves this function; no payload text is returned."""
    if not isinstance(thread_id, str) or not thread_id:
        return None
    path = _thread_db_path()
    if not path.is_file():
        return None
    con = None
    try:
        con = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.5)
        con.execute("PRAGMA busy_timeout = 50")
        deadline = time.monotonic() + 0.5
        con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in ("orchestration_v2_projection_threads",
                      "orchestration_v2_projection_provider_threads"):
            if table not in tables:
                continue
            cols = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
            if "thread_id" not in cols:
                continue
            if "title" in cols:
                row = con.execute(
                    f"SELECT title FROM {table} WHERE thread_id = ? LIMIT 1",
                    (thread_id,)).fetchone()
                title = _clean_title(row[0] if row else None)
                if title:
                    return title
            if "payload_json" in cols:
                row = con.execute(
                    f"SELECT payload_json FROM {table} WHERE thread_id = ? LIMIT 1",
                    (thread_id,)).fetchone()
                if row and isinstance(row[0], str):
                    try:
                        payload = json.loads(row[0])
                    except ValueError:
                        payload = None
                    if isinstance(payload, dict):
                        for key in ("title", "name", "customTitle", "displayTitle", "threadTitle"):
                            title = _clean_title(payload.get(key))
                            if title:
                                return title
        return None
    except (OSError, ValueError, sqlite3.Error):
        return None
    finally:
        if con is not None:
            con.close()


def thread_title_for_digest(digest: str) -> str | None:
    """The title of the T3 thread whose ``t3:<thread_id>`` identity hashes
    to ``digest`` (a sha256 hex digest), or None.

    Callers that must not persist a raw thread identity queue only this
    digest; the matching thread id is recovered here, in memory, by
    hashing each projected thread id until one matches, and is never
    written back by this function."""
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    path = _thread_db_path()
    if not path.is_file():
        return None
    con = None
    match = None
    try:
        con = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.5)
        con.execute("PRAGMA busy_timeout = 50")
        deadline = time.monotonic() + 0.5
        con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in ("orchestration_v2_projection_threads",
                      "orchestration_v2_projection_provider_threads"):
            if table not in tables or match is not None:
                continue
            cols = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
            if "thread_id" not in cols:
                continue
            rows = con.execute(
                f"SELECT DISTINCT thread_id FROM {table} LIMIT 10000").fetchall()
            for (thread_id,) in rows:
                if isinstance(thread_id, str) and thread_id and \
                        hashlib.sha256(f"t3:{thread_id}".encode("utf-8")).hexdigest() == digest:
                    match = thread_id
                    break
    except (OSError, ValueError, sqlite3.Error):
        return None
    finally:
        if con is not None:
            con.close()
    return thread_title(match) if match is not None else None


def _clean_title(value) -> str | None:
    if not isinstance(value, str):
        return None
    title = " ".join(value.split()).strip()
    if not title or len(title) > 120 or "/" in title or "\\" in title:
        return None
    return title


def _thread_db_path(home: Path | None = None) -> Path:
    return Path(os.environ.get("KHIPU_T3_DB") or (home if home is not None else Path.home()) / ".t3/userdata/statev2.sqlite")


def _readonly_uri(path: Path) -> str:
    """Active WALs need ``mode=ro``; a closed WAL needs immutable safety."""
    mode = "?mode=ro" if Path(f"{path}-wal").exists() else "?mode=ro&immutable=1"
    return path.absolute().as_uri() + mode


def _lookup_health(path: Path) -> tuple[bool, str | None]:
    """Can this build still read T3's provider-thread projection?

    This intentionally validates the exact three columns ``thread_for_session``
    reads. It only verifies the provider-thread lookup; it does not establish
    whether T3 has recent conversation activity.
    """
    if not path.is_file():
        return False, "T3's thread database was not found"
    con = None
    try:
        # The same read-only open thread_for_session uses in every hook, so
        # this checks the lookup Khipu really runs. No copy: T3's database is
        # over a gigabyte. A doctor read gets a longer deadline than a hook.
        con = sqlite3.connect(_readonly_uri(path), uri=True, timeout=0.5)
        con.execute("PRAGMA busy_timeout = 50")
        deadline = time.monotonic() + 0.5
        con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
        rows = con.execute(
            "SELECT thread_id, CASE WHEN json_valid(payload_json) THEN "
            "json_extract(payload_json, '$.nativeThreadRef.nativeId') END "
            "FROM orchestration_v2_projection_provider_threads "
            "ORDER BY updated_at DESC LIMIT 20"
        ).fetchall()
        if rows and not any(isinstance(native, str) and native for _, native in rows):
            return False, "T3's thread records changed shape (no native session ids found)"
        return True, None
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower() or "no such column" in str(exc).lower():
            return False, "T3's thread records changed shape (table not found)"
        return False, "T3's thread lookup is unavailable"
    except (OSError, ValueError, sqlite3.Error):
        return False, "T3's thread lookup is unavailable"
    finally:
        if con is not None:
            con.close()


def _recent_conversation_use(path: Path) -> tuple[bool | None, str | None]:
    """Whether user or assistant conversation messages are newer than one day.

    ``None`` is deliberately distinct from an empty table: unreadable or
    changed message records cannot safely prove that T3 was unused.
    """
    if not path.is_file():
        return None, "T3 recent conversation use is unavailable"
    con = None
    try:
        con = sqlite3.connect(_readonly_uri(path), uri=True, timeout=0.5)
        con.execute("PRAGMA busy_timeout = 50")
        deadline = time.monotonic() + 0.5
        con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
        row = con.execute(
            "SELECT created_at FROM orchestration_v2_projection_messages "
            "WHERE role IN ('user', 'assistant') ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return False, None
        age = _age_seconds(str(row[0]) if row[0] is not None else None)
        if age is None:
            return None, "T3 recent conversation use is unavailable"
        return age < 86400, None
    except (OSError, ValueError, sqlite3.Error):
        return None, "T3 recent conversation use is unavailable"
    finally:
        if con is not None:
            con.close()


def _thread_linked_capture_evidence() -> tuple[str | None, str | None, int, bool]:
    """Newest stored capture explicitly stamped ``via:t3`` with a thread id.

    Doctor has already refreshed the local hub replica when the hub is
    reachable.  Reading it keeps this check local and fail-open when a
    portable install has no replica or an older replica lacks ``raw``.
    """
    try:
        from khipu.hub_snapshot import snapshot_path

        path = snapshot_path()
        if not path.is_file():
            return None, None, 0, True
        con = sqlite3.connect(_readonly_uri(path), uri=True, timeout=0.1)
        try:
            deadline = time.monotonic() + 0.5
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
                True,
            )
        finally:
            con.close()
    except (ImportError, OSError, ValueError, sqlite3.Error):
        # Unknown is not "none": a slow or unreadable replica must not raise
        # the "used without a linked capture" warning.
        return None, None, 0, False


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


def _provider_label(inst: dict[str, Any]) -> str:
    name, driver = str(inst["name"]), str(inst["driver"])
    return name if not driver or name.strip().lower() == driver.strip().lower() else f"{name} ({driver})"


def _provider_messages(inst: dict[str, Any], status: dict[str, Any]) -> tuple[str | None, list[str]]:
    """The doctor warning (a gap the owner can fix) and the notes (facts about
    the harness the owner cannot change) for one provider instance."""
    label = _provider_label(inst)
    functions = status.get("functions") or {}
    notes: list[str] = []
    if inst["driver"] == ANTIGRAVITY_DRIVER:
        notes.append(f"T3 runs {label} without Khipu's start block (its runtime ignores injected context); "
                     "routing reaches it through `khipu_route` and the brief")
        runtime = status.get("runtime") or {}
        if runtime.get("recheck"):
            notes.append(f"T3's {label} runtime ({runtime.get('version') or 'unknown version'}) is not one "
                         "Khipu has checked; verify it from the Antigravity card")
    if status["hooks"] == MISSING:
        if inst["driver"] == ANTIGRAVITY_DRIVER:
            return (f"T3 provider {label} has no Khipu hooks, so its sessions are not captured and it has no "
                    "Khipu memory tools; add Khipu to a project from the Antigravity card", notes)
        return (f"T3 provider {label} has no Khipu hooks, so sessions on it get no Khipu routing start block", notes)
    gaps = [FUNCTION_LABELS[f] for f in _CORE_FUNCTIONS if functions.get(f) == MISSING]
    if gaps:
        return f"T3 provider {label} is missing Khipu {' and '.join(gaps)}", notes
    return None, notes


def health(home: Path | None = None) -> dict[str, Any] | None:
    """Read-only T3 health for ``khipu doctor`` and the desktop card.

    A T3 card exists only when its settings file exists.  Provider instances
    are read through ``provider_instances``' closed field set (the file can
    hold provider credentials, and a malformed or newer settings shape must
    not hide the installed T3 surface).
    """
    if not settings_path(home).is_file():
        return None
    path = _thread_db_path(home)
    lookup_ok, lookup_error = _lookup_health(path)
    used_recently, recent_use_error = _recent_conversation_use(path)
    last_capture_at, last_capture_harness, captures_today, evidence_ok = _thread_linked_capture_evidence()
    last_capture_age = _age_seconds(last_capture_at)
    recent_without_capture = bool(
        evidence_ok
        and used_recently is True
        and (last_capture_age is None or last_capture_age > 86400)
    )
    warnings: list[str] = []
    if not lookup_ok:
        warnings.append(lookup_error or "T3's thread lookup is unavailable")
    if recent_without_capture:
        warnings.append("T3 was used in the last day without a thread-linked capture")
    notes: list[str] = []
    providers: list[dict[str, Any]] = []
    for inst in all_provider_instances(settings_path(home)):
        if not inst["enabled"]:
            continue
        status = provider_status(inst, home if home is not None else Path.home())
        entry = {"id": inst["id"], "driver": inst["driver"], "name": inst["name"], "hooks": status["hooks"]}
        entry.update({key: status[key] for key in ("functions", "projects", "runtime") if key in status})
        providers.append(entry)
        warning, note = _provider_messages(inst, status)
        if warning:
            warnings.append(warning)
        notes += note
    return {
        "detected": True,
        "providers": providers,
        "lookup": {"ok": lookup_ok, "error": lookup_error},
        "used_recently": used_recently,
        "recent_use_error": recent_use_error,
        "last_thread_linked_capture_at": last_capture_at,
        "last_thread_linked_capture_age_s": last_capture_age,
        "last_thread_linked_capture_harness": last_capture_harness,
        "thread_linked_captures_today": captures_today,
        "warnings": warnings,
        "notes": notes,
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


CODEX_DRIVER = "codex"
# The name an instance gets when T3 gives none and its id is the driver's own.
_DRIVER_NAMES = {CLAUDE_DRIVER: "Claude", CODEX_DRIVER: "Codex"}


def provider_instances(settings_file: Path | None, drivers, *, text: str | None = None) -> list[dict[str, Any]]:
    """Every instance in T3's ``settings.json`` (``providerInstances``)
    whose ``driver`` is one of ``drivers``, as ``{id, driver, name,
    home_path, enabled}``. Only ``driver``, ``displayName``,
    ``config.homePath`` and ``enabled`` are read into the result: the file
    can hold tokens. ``home_path`` is ``""`` when T3 runs the instance from
    its CLI's default login folder. ``text``: the file's contents, already
    read by a caller that opens it its own way (discovery, no symlinks)."""
    wanted = set(drivers)
    try:
        if settings_file is not None:
            text = settings_file.read_text(encoding="utf-8")
        data = json.loads(text) if text is not None else None
    except (OSError, ValueError):
        return []
    instances = data.get("providerInstances") if isinstance(data, dict) else None
    if not isinstance(instances, dict):
        return []
    out: list[dict[str, Any]] = []
    for instance_id, inst in instances.items():
        if not isinstance(inst, dict) or inst.get("driver") not in wanted:
            continue
        driver = inst["driver"]
        config = inst.get("config") if isinstance(inst.get("config"), dict) else {}
        home_path = config.get("homePath")
        display = inst.get("displayName")
        if isinstance(display, str) and display.strip():
            name = display.strip()
        else:
            name = _DRIVER_NAMES.get(driver, str(instance_id)) if instance_id == driver else str(instance_id)
        out.append({
            "id": str(instance_id),
            "driver": driver,
            "name": name,
            "home_path": home_path.strip() if isinstance(home_path, str) else "",
            "enabled": inst.get("enabled") is not False,
        })
    return out


def all_provider_instances(settings_file: Path | None = None, *, text: str | None = None) -> list[dict[str, Any]]:
    """Every enabled-or-not provider instance, whatever its driver.

    Only the driver names are read to build the allow-list, then
    ``provider_instances`` extracts its closed field set; no other settings
    byte leaves this module."""
    try:
        if text is None:
            target = settings_file if settings_file is not None else settings_path()
            text = target.read_text(encoding="utf-8")
        data = json.loads(text) if text is not None else None
    except (OSError, ValueError):
        return []
    instances = data.get("providerInstances") if isinstance(data, dict) else None
    if not isinstance(instances, dict):
        return []
    drivers = {inst.get("driver") for inst in instances.values()
               if isinstance(inst, dict) and isinstance(inst.get("driver"), str) and inst.get("driver")}
    if not drivers:
        return []
    return provider_instances(settings_file, drivers, text=text)


def _provider_folder(instance: dict[str, Any], home: Path, default_name: str) -> Path:
    raw = (instance.get("home_path") or "").strip()
    if not raw:
        return home / default_name
    if raw == "~":
        return home
    if raw.startswith("~/"):
        return home / raw[2:]
    path = Path(raw)
    return path if path.is_absolute() else home / path


CURSOR_DRIVER = "cursor"
ANTIGRAVITY_DRIVER = "antigravity"

# What Khipu can give a provider's sessions, in the order the app lists them.
FUNCTIONS = ("capture", "start_block", "prompt_recall", "delegation", "mcp")
FUNCTION_LABELS = {
    "capture": "capture",
    "start_block": "routing start block",
    "prompt_recall": "memory on each prompt",
    "delegation": "routing at delegation",
    "mcp": "memory tools",
}
# A provider with per-function states is flagged in doctor only for these:
# the other two have no installer in every Khipu build, so a gap there is
# shown in the app's rows, not raised as a warning.
_CORE_FUNCTIONS = ("capture", "start_block", "mcp")
INSTALLED, MISSING, UNSUPPORTED = "installed", "missing", "unsupported"


def _read_json_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _hook_commands(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        command = value.get("command")
        if isinstance(command, str):
            out.append(command)
        for inner in value.values():
            out += _hook_commands(inner)
    elif isinstance(value, list):
        for inner in value:
            out += _hook_commands(inner)
    return out


def _cursor_functions(folder: Path) -> dict[str, str]:
    """T3 runs Cursor in process with every settings layer the Cursor CLI
    loads, so the user's ``~/.cursor`` hooks and MCP file apply. Read: only
    which Khipu launchers each hook event runs."""
    hooks = _read_json_object(folder / "hooks.json").get("hooks")
    events: dict[str, set[str]] = {}
    if isinstance(hooks, dict):
        for event, handlers in hooks.items():
            for command in _hook_commands(handlers):
                name = _khipu_hook_name(command)
                if name:
                    events.setdefault(str(event), set()).add(name)

    def runs(name: str, *names: str) -> str:
        return INSTALLED if any(name in events.get(event, ()) for event in names) else MISSING

    server = (_read_json_object(folder / "mcp.json").get("mcpServers") or {}).get("khipu")
    command = server.get("command") if isinstance(server, dict) else None
    try:
        mcp = isinstance(command, str) and Path(shlex.split(command)[0]).name == "khipu-mcp"
    except (ValueError, IndexError):
        mcp = False
    return {
        "capture": runs("khipu-stop-hook", "stop"),
        "start_block": runs("khipu-recall-hook", "sessionStart"),
        "prompt_recall": runs("khipu-prompt-recall", "beforeSubmitPrompt"),
        "delegation": runs("khipu-delegation-hook", "postToolUse", "preToolUse"),
        "mcp": INSTALLED if mcp else MISSING,
    }


def _rollup(functions: dict[str, str]) -> str:
    """``installed`` when every function the harness can carry is installed,
    ``missing`` when none is, else ``partial``. ``unsupported`` is the
    harness's limit, never a gap."""
    wanted = [state for state in functions.values() if state != UNSUPPORTED]
    if wanted and all(state == INSTALLED for state in wanted):
        return INSTALLED
    return "partial" if any(state == INSTALLED for state in wanted) else MISSING


def provider_status(instance: dict[str, Any], home: Path | None = None) -> dict[str, Any]:
    """What Khipu gives one T3 provider instance: ``{"hooks": rollup}`` for
    every driver, plus ``functions`` (``installed`` / ``missing`` /
    ``unsupported`` per function, see ``FUNCTIONS``) for the Cursor and
    Antigravity drivers, where the answer differs by function. Claude Code
    and Codex stay a single rollup: either Khipu's hooks are in their config
    home or they are not. Only provider ids, drivers and ``homePath`` come
    from T3's settings; the files read are the provider's own hook and MCP
    files (command names only)."""
    home = home if home is not None else Path.home()
    driver = instance.get("driver")
    if driver == CURSOR_DRIVER:
        functions = _cursor_functions(_provider_folder(instance, home, ".cursor"))
        return {"hooks": _rollup(functions), "functions": functions}
    if driver == ANTIGRAVITY_DRIVER:
        from khipu import t3_antigravity

        found = t3_antigravity.functions_for(str(instance.get("id") or ""), home)
        return {"hooks": _rollup(found["functions"]), **found}
    if driver == CLAUDE_DRIVER:
        target = _provider_folder(instance, home, ".claude") / "settings.json"
    elif driver == CODEX_DRIVER:
        target = _provider_folder(instance, home, ".codex") / "hooks.json"
    else:
        return {"hooks": MISSING}
    data = _read_json_object(target)
    commands = _hook_commands(data.get("hooks"))
    return {"hooks": INSTALLED if any(_is_khipu_hook_command(command) for command in commands) else MISSING}


def provider_hooks(instance: dict[str, Any], home: Path | None = None) -> str:
    """The single word for a provider: ``installed`` when its own config home
    carries Khipu, ``partial`` when only some of the functions its harness can
    carry are in place, else ``missing``. Drivers with no Khipu pack (anything
    but Claude Code, Codex, Cursor and Antigravity) are always ``missing``: a
    T3 child on them gets no routing start block."""
    return provider_status(instance, home)["hooks"]


def child_gets_start_block(instance: dict[str, Any], home: Path | None = None) -> bool:
    """Whether a T3 child on this provider gets Khipu's routing start block.
    Per-function drivers answer from their ``start_block`` state (always no on
    Antigravity: T3's runtime ignores injected context); the others from the
    single rollup."""
    status = provider_status(instance, home)
    functions = status.get("functions")
    if functions:
        return functions.get("start_block") == INSTALLED
    return status["hooks"] == INSTALLED


# The actual Khipu hook launchers (integrations.LAUNCHER_NAMES minus the
# MCP server, which is not a hook). A command counts only when its
# executable's name is exactly one of these; a shell snippet that merely
# mentions a Khipu-looking word (``echo not-a-khipu-hook``) is not a hook.
_KHIPU_HOOK_NAMES = frozenset({
    "khipu-stop-hook", "khipu-recall-hook", "khipu-prompt-recall",
    "khipu-aegis-capture", "khipu-delegation-hook",
})
_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "python3", "python"})


def _khipu_hook_name(command: str) -> str | None:
    """The Khipu hook launcher a command runs (``khipu-stop-hook`` ...), or
    None when the command is not a direct exec of one."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    index = 0
    # Skip an `env` prefix and variable assignments (`env K=V <hook>`,
    # `K=V <hook>`) so the executable itself is what gets matched.
    if tokens and Path(tokens[0]).name == "env":
        index = 1
    while index < len(tokens) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[index]):
        index += 1
    if index < len(tokens) and Path(tokens[index]).name in _INTERPRETERS:
        index += 1
        while index < len(tokens) and tokens[index].startswith("-") and tokens[index] != "-c":
            index += 1
        if index < len(tokens) and tokens[index] == "-c":
            return None  # a shell string, not a direct hook exec
    if index < len(tokens) and Path(tokens[index]).name in _KHIPU_HOOK_NAMES:
        return Path(tokens[index]).name
    # A quoted or unquoted absolute path with spaces, standing alone as the
    # whole command, tokenizes into pieces above; accept it when the path
    # itself ends in a Khipu hook name. A mention inside a larger command
    # (`echo /opt/.../khipu-stop-hook`) is not a hook exec and stays None.
    trimmed = command.strip().strip("\"'").strip()
    if trimmed.startswith("/"):
        for name in _KHIPU_HOOK_NAMES:
            if trimmed == name or trimmed.endswith("/" + name):
                return name
    return None


def _is_khipu_hook_command(command: str) -> bool:
    return _khipu_hook_name(command) is not None


def claude_instances(settings_file: Path) -> list[dict[str, Any]]:
    """Every Claude instance in T3's ``settings.json`` (``providerInstances``
    entries whose ``driver`` is ``claudeAgent``), as ``{id, name, home_path,
    enabled}``. ``home_path`` is ``""`` when T3 runs it from the default
    ``~/.claude``. Nothing else in that file is read into the result: it can
    hold tokens."""
    return [{k: inst[k] for k in ("id", "name", "home_path", "enabled")}
            for inst in provider_instances(settings_file, (CLAUDE_DRIVER,))]
