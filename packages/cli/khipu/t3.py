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
import re
import tempfile
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


def strip_handoff(text: str) -> str:
    """The user's own words from a message that starts with T3's hand-over:
    everything after the first ``\\n\\nUser message:\\n`` following the last
    historical item (or the header when there are none). Anything else, and a
    hand-over with no such marker, comes back unchanged."""
    if not isinstance(text, str):
        return text
    header, newline, rest = text.partition("\n")
    provider, provider_newline, body = rest.partition("\n")
    if (not newline or not provider_newline
            or not re.fullmatch(r"Context handoff \([^)]+\):", header)
            or not provider.startswith("Provider context handoff. Thread: ")):
        return text
    historical = [m.start() for m in re.finditer(r"(?m)^\[Historical ", text)]
    start = historical[-1] if historical else len(header) + len(newline) + len(provider)
    marker_at = text.find(HANDOFF_USER_MARKER, start)
    if marker_at >= 0:
        return text[marker_at + len(HANDOFF_USER_MARKER):]
    return "" if text.endswith(HANDOFF_USER_MARKER.rstrip("\n")) else text


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
