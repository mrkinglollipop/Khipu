"""Owner-local report of routing copies found in files (slice 13 / 15d).

The scanner (``routing_scanner.scan_text``) already feeds recall's
superseded mark from the memory notes Khipu imports. This report keeps
what that scan found, per file, for the owner only: the file shown
relative to its root (``~`` for home, the repo name for a per-project
file), its line, a clipped quote, and that Khipu's record supersedes it.

The scan reads exactly the approved set (plan section 10, as amended):
the global ``~/.claude/CLAUDE.md``, ``~/.claude/AGENTS.md``,
``~/.codex/AGENTS.md`` and ``~/AGENTS.md`` when present; the
``CLAUDE.md`` / ``AGENTS.md`` at the root of each repo Khipu already
knows as a project, resolved from Khipu's own project/repo records
(the ``repo_root`` values recorded on its episodes, read from the
local hub snapshot — never by walking the filesystem); and the memory
notes Khipu imports (``notes._iter_note_file_candidates``). Nothing
else. Explicit/scheduled refreshes isolate discovery and each file in
deadline-limited workers, skip protected/cloud/dataless paths, cap reads
at ``MAX_FILE_BYTES + 1`` and cap hits per file and in total. Routing
readers use only the stored report and never test candidate freshness.

The report lives on this Mac only; it must never ride a push, a child
page, an MCP conversation view, a replication record, doctor output, a
log or a gateway projection.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STORE_NAME = "scan-report.json"
_QUOTE_MAX = 140

MAX_FILE_BYTES = 1_000_000
MAX_HITS_PER_FILE = 50
MAX_HITS_TOTAL = 500
_MAX_PROJECT_ROOTS = 500
FILE_DEADLINE_SECONDS = 0.25
DISCOVERY_DEADLINE_SECONDS = 2.0
REFRESH_BUDGET_SECONDS = 5.0
_MAX_CANDIDATES = 10_000
MAX_REPORT_BYTES = 4_000_000

NOT_CHECKED: dict[str, Any] = {"status": "not_checked", "scanned_at": None,
                               "files_checked": 0, "hits": [], "hits_total": 0,
                               "more_not_shown": 0, "too_large": []}


def store_path() -> Path:
    from khipu.paths import data_dir

    return data_dir().joinpath("state", "routing", STORE_NAME)


def global_files(home: Path | None = None) -> list[Path]:
    """The approved global files, in the order the plan names them."""
    home = Path.home() if home is None else home
    return [home / ".claude" / "CLAUDE.md", home / ".claude" / "AGENTS.md",
            home / ".codex" / "AGENTS.md", home / "AGENTS.md"]


def known_project_roots() -> list[Path]:
    """Repo roots Khipu already knows as projects, from its own records.

    Capture records each episode's ``repo_root``; the local hub snapshot
    carries those rows on this Mac, so this reads one bounded query
    against that replica and never walks the filesystem looking for
    repos, and never needs the hub itself. Any failure reads as no
    known roots: the global files and the imported notes still scan.
    """
    try:
        import sqlite3

        from khipu import hub_snapshot

        path = hub_snapshot.snapshot_path()
        if not path.is_file():
            return []
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            cols = {row[1] for row in con.execute("PRAGMA table_info(episodes)")}
            if "repo_root" not in cols:
                return []
            rows = con.execute(
                "SELECT DISTINCT repo_root FROM episodes "
                "WHERE repo_root IS NOT NULL AND repo_root != '' "
                "ORDER BY repo_root LIMIT ?", (_MAX_PROJECT_ROOTS,)).fetchall()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 — known roots are best-effort evidence
        return []
    # These are recorded identities, not claims that roots exist. All filesystem
    # validation happens in the bounded discovery/read workers, never here.
    return [Path(raw) for (raw,) in rows if isinstance(raw, str) and raw.strip()]


def display_path(path: Path, home: Path | None = None,
                 project_roots: list[Path] | tuple[Path, ...] = ()) -> str:
    """``path`` shown relative to its root: the repo name for a
    per-project file, ``~`` for home, never a stripped absolute path."""
    home = Path.home() if home is None else home
    try:
        path.relative_to(home)
        under_home = True
    except ValueError:
        under_home = False
    for root in project_roots:
        if root == home:
            # Home itself as a project root must not rename ~/… paths
            # after the home folder (which can be a username).
            continue
        if under_home:
            try:
                home.relative_to(root)
            except ValueError:
                pass
            else:
                # An ancestor of home as a project root must not rename
                # ~/… paths after the home folder (which can be a username).
                continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        return root.name if str(rel) == "." else f"{root.name}/{rel}"
    try:
        return "~/" + str(path.relative_to(home)) if path != home else "~"
    except ValueError:
        pass
    return f"{path.parent.name}/{path.name}" if path.parent.name else path.name


def _clip(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= _QUOTE_MAX else text[: _QUOTE_MAX - 1].rstrip() + "…"


def _discover() -> dict[str, Any]:
    """Enumerate only approved roots; reject protected aliases before traversal."""
    from khipu import notes
    from khipu.routing_scan_io import safety

    home = Path.home()
    roots = known_project_roots()
    out: list[list[str]] = []
    skipped: list[dict[str, str]] = []
    seen: set[Path] = set()

    def safe(path: Path) -> bool:
        reason = safety(path, home)
        if reason and reason != "unreadable":
            skipped.append({"file": display_path(path, home, roots), "reason": reason})
        return reason is None

    def add(path: Path) -> None:
        if len(out) < _MAX_CANDIDATES and path not in seen:
            seen.add(path)
            out.append([str(path), display_path(path, home, roots)])

    def children(root: Path) -> list[Path]:
        if not safe(root) or not root.is_dir():
            return []
        return sorted(root.iterdir())

    def notes_in(root: Path) -> None:
        for path in children(root):
            if path.name.endswith(".md") and path.name not in {"MEMORY.md", "_overflow_index.md"}:
                add(path)

    for path in global_files(home):
        add(path)
    for root in roots:
        for name in ("CLAUDE.md", "AGENTS.md"):
            add(root / name)
    # Same approved note inventory as notes._iter_note_file_candidates, with
    # safety checked before each directory traversal (including custom roots).
    for project in children(notes.claude_projects_root()):
        notes_in(project / "memory")
    notes_in(notes.codex_memories_root())
    if safe(home / ".cursor" / "projects"):
        for root in notes.cursor_memory_roots():
            notes_in(root)
    for root in notes.aegis_memory_roots():
        notes_in(root)
    return {"entries": out, "skipped": skipped}


def candidate_entries() -> list[tuple[Path, str]]:
    """Approved candidates; refresh invokes this only in a deadline worker."""
    data = _discover()
    return [(Path(path), display) for path, display in data["entries"]]


def candidate_files() -> list[Path]:
    return [path for path, _display in candidate_entries()]


def _discovery_result() -> dict[str, Any]:
    # Preserve the public candidate hook used by callers, while retaining skip
    # reasons from production discovery without process-global mutable state.
    if candidate_entries is _DEFAULT_CANDIDATE_ENTRIES:
        return _discover()
    return {"entries": [[str(path), display] for path, display in candidate_entries()], "skipped": []}


_DEFAULT_CANDIDATE_ENTRIES = candidate_entries


def refresh(now: datetime | None = None) -> dict[str, Any]:
    """Explicit/scheduled owner-local refresh, bounded for cloud/filesystem stalls.

    Protected/cloud paths are conservatively skipped even if previously read.
    The report records skips; readers retain this snapshot until the next pass.
    """
    from khipu.routing_scanner import scan_text
    from khipu.routing_scan_io import bounded, protected, read_file

    now = now or datetime.now(timezone.utc)
    end = time.monotonic() + REFRESH_BUDGET_SECONDS
    status, discovered = bounded(_discovery_result,
        min(DISCOVERY_DEADLINE_SECONDS, max(0, end - time.monotonic())))
    skipped = [] if status != "ok" else discovered["skipped"]
    entries = [] if status != "ok" else discovered["entries"]
    if status != "ok":
        skipped.append({"file": "candidate discovery", "reason": status})
    hits: list[dict[str, Any]] = []
    too_large: list[str] = []
    hits_total = files_checked = 0
    home = Path.home()
    seen: set[str] = set()
    for raw, display in entries:
        if raw in seen:
            continue
        seen.add(raw)
        remaining = end - time.monotonic()
        if remaining <= 0:
            skipped.append({"file": "remaining candidates", "reason": "refresh_budget"})
            break
        path = Path(raw)
        if protected(path, home):
            skipped.append({"file": display, "reason": "protected_path"})
            continue
        status, result = bounded(lambda: read_file(path, home, MAX_FILE_BYTES),
                                 min(FILE_DEADLINE_SECONDS, remaining))
        reason = status if status != "ok" else result.get("reason")
        if reason:
            if reason == "too_large":
                too_large.append(display)
            else:
                skipped.append({"file": display, "reason": reason})
            continue
        files_checked += 1
        claims = scan_text(raw, result["text"])
        hits_total += len(claims)
        for claim in claims[:MAX_HITS_PER_FILE]:
            if len(hits) >= MAX_HITS_TOTAL:
                break
            hits.append({"file": display, "line": claim["line"],
                         "quote": _clip(claim["words"]), "superseded": True})
    hits.sort(key=lambda h: (h["file"], h["line"]))
    report = {"status": "checked", "scanned_at": now.isoformat(),
              "files_checked": files_checked, "hits": hits, "hits_total": hits_total,
              "more_not_shown": hits_total - len(hits), "too_large": too_large,
              "skipped": skipped}
    _write_report(report)
    return _public(report)


def _write_report(report: dict[str, Any]) -> None:
    """Atomic, owner-only write, the ``routing_replication._atomic``
    shape: a unique temp file (0600 from ``mkstemp``), fsync, replace,
    directory fsync, in a 0700 folder."""
    path = store_path()
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        parent.chmod(0o700)
    except OSError:
        pass
    fd, temporary = tempfile.mkstemp(prefix=".scan-report-", suffix=".tmp",
                                     dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(report, indent=1) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        directory = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _public(data: dict[str, Any]) -> dict[str, Any]:
    """The report as readers may see it: no signature map, counts as
    stored, damaged fields read as the not-checked state."""
    hits = []
    for hit in data["hits"]:
        if not isinstance(hit, dict) or not isinstance(hit.get("file"), str) \
                or isinstance(hit.get("line"), bool) \
                or not isinstance(hit.get("line"), int) \
                or not isinstance(hit.get("quote"), str):
            return {"status": "not_checked", "scanned_at": None,
                    "files_checked": 0, "hits": [], "hits_total": 0,
                    "more_not_shown": 0, "too_large": []}
        hits.append({"file": hit["file"], "line": hit["line"],
                     "quote": hit["quote"],
                     "superseded": hit.get("superseded") is True})
    files_checked = data.get("files_checked")
    hits_total = data.get("hits_total")
    if not isinstance(hits_total, int) or isinstance(hits_total, bool):
        hits_total = len(hits)
    more = data.get("more_not_shown")
    if not isinstance(more, int) or isinstance(more, bool):
        more = max(0, hits_total - len(hits))
    too_large = data.get("too_large")
    skipped = data.get("skipped", [])
    return {"status": "checked",
            "scanned_at": data.get("scanned_at") if isinstance(data.get("scanned_at"), str) else None,
            "files_checked": files_checked if isinstance(files_checked, int)
            and not isinstance(files_checked, bool) else 0,
            "hits": hits, "hits_total": hits_total, "more_not_shown": more,
            "too_large": [t for t in too_large if isinstance(t, str)]
            if isinstance(too_large, list) else [],
            "skipped": [{"file": item["file"], "reason": item["reason"],
                         **({"message": "Couldn't read in time."}
                            if item["reason"] == "timeout" else {})}
                        for item in skipped if isinstance(item, dict)
                        and isinstance(item.get("file"), str)
                        and isinstance(item.get("reason"), str)]
            if isinstance(skipped, list) else []}


def _read_raw() -> dict[str, Any] | None:
    from khipu.routing_scan_io import bounded, read_file

    path, home = store_path(), Path.home()
    status, result = bounded(lambda: read_file(path, home, MAX_REPORT_BYTES), FILE_DEADLINE_SECONDS)
    if status != "ok" or "text" not in result:
        return None
    try:
        data = json.loads(result["text"])
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("status") != "checked" \
            or not isinstance(data.get("hits"), list):
        return None
    return data


def report() -> dict[str, Any]:
    """Read only the bounded owner-local cache; never discover/stat scan inputs.

    Freshness belongs to explicit refresh and scheduled notes reconciliation.
    Missing, unreadable, timed-out or damaged caches read as not checked.
    """
    data = _read_raw()
    return dict(NOT_CHECKED) if data is None else _public(data)
