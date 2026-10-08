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
import sqlite3
import tempfile
import time
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
        con = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.5)
        con.execute("PRAGMA busy_timeout = 2")
        deadline = time.monotonic() + 0.005
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
