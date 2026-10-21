"""Owner-local report of routing copies found in files (slice 13 / 15d).

The scanner (``routing_scanner.scan_text``) already feeds recall's
superseded mark from the memory notes Khipu imports. This report keeps
what that scan found, per file, for the owner only: the file shown from
home, its line, a clipped quote, and that Khipu's record supersedes it.
It reads only the files the scanner already reaches — memory notes from
``notes._iter_note_file_candidates`` and the global and per-project
CLAUDE.md / AGENTS.md files slice 13 names — and never widens that set.
The report lives on this Mac only; it must never ride a push, a child
page, an MCP conversation view, a replication record, doctor output, a
log or a gateway projection.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STORE_NAME = "scan-report.json"
_QUOTE_MAX = 140

NOT_CHECKED: dict[str, Any] = {"status": "not_checked", "scanned_at": None,
                               "files_checked": 0, "hits": []}


def store_path() -> Path:
    from khipu.paths import data_dir

    return data_dir().joinpath("state", "routing", STORE_NAME)


def display_path(path: Path, home: Path | None = None) -> str:
    """``path`` shown relative to its root, with home shown as ``~``."""
    home = Path.home() if home is None else home
    try:
        return "~/" + str(path.relative_to(home)) if path != home else "~"
    except ValueError:
        pass
    try:
        return str(path.relative_to(path.anchor))
    except ValueError:
        return str(path)


def _clip(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= _QUOTE_MAX else text[: _QUOTE_MAX - 1].rstrip() + "…"


def candidate_files() -> list[Path]:
    """The files the scanner already reads, no wider (see module docstring)."""
    from khipu import notes

    out: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        if path not in seen:
            seen.add(path)
            out.append(path)

    home = Path.home()
    for path in (home / ".claude" / "CLAUDE.md", home / ".claude" / "AGENTS.md",
                 home / ".codex" / "AGENTS.md", home / ".codex" / "CLAUDE.md"):
        if path.is_file():
            add(path)
    for proj_dir in notes._claude_project_dirs(notes.claude_projects_root()):
        resolved = notes.resolve_claude_project_path(proj_dir.name)
        if resolved is None:
            continue
        for name in ("CLAUDE.md", "AGENTS.md"):
            path = resolved / name
            if path.is_file():
                add(path)
    for path, _harness, _slug in notes._iter_note_file_candidates():
        add(path)
    return out


def refresh(now: datetime | None = None) -> dict[str, Any]:
    """Scan every candidate file once and keep the report on this Mac.

    A file that cannot be read is skipped, never fatal; the scan reads
    only, and never edits a scanned file."""
    from khipu.routing_scanner import scan_text

    now = now or datetime.now(timezone.utc)
    hits: list[dict[str, Any]] = []
    files_checked = 0
    for path in candidate_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        files_checked += 1
        for claim in scan_text(str(path), text):
            hits.append({"file": display_path(path), "line": claim["line"],
                         "quote": _clip(claim["words"]), "superseded": True})
    hits.sort(key=lambda h: (h["file"], h["line"]))
    report = {"status": "checked", "scanned_at": now.isoformat(),
              "files_checked": files_checked, "hits": hits}
    _write_report(report)
    return report


def _write_report(report: dict[str, Any]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def report() -> dict[str, Any]:
    """The kept report, or the not-looked-at-yet state. A missing or
    damaged report reads as not checked, never as an error."""
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(NOT_CHECKED)
    if not isinstance(data, dict) or data.get("status") != "checked" \
            or not isinstance(data.get("hits"), list):
        return dict(NOT_CHECKED)
    hits = []
    for hit in data["hits"]:
        if not isinstance(hit, dict) or not isinstance(hit.get("file"), str) \
                or not isinstance(hit.get("line"), int) \
                or not isinstance(hit.get("quote"), str):
            return dict(NOT_CHECKED)
        hits.append({"file": hit["file"], "line": hit["line"],
                     "quote": hit["quote"],
                     "superseded": hit.get("superseded") is True})
    files_checked = data.get("files_checked")
    return {"status": "checked",
            "scanned_at": data.get("scanned_at") if isinstance(data.get("scanned_at"), str) else None,
            "files_checked": files_checked if isinstance(files_checked, int) else 0,
            "hits": hits}
