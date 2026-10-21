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
else. Reads are bounded: a file over ``MAX_FILE_BYTES`` is marked too
large to read instead of being opened whole, and hits are capped per
file and in total, with the rest counted as not shown.

The report lives on this Mac only; it must never ride a push, a child
page, an MCP conversation view, a replication record, doctor output, a
log or a gateway projection.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STORE_NAME = "scan-report.json"
_QUOTE_MAX = 140

MAX_FILE_BYTES = 1_000_000
MAX_HITS_PER_FILE = 50
MAX_HITS_TOTAL = 500
_MAX_PROJECT_ROOTS = 500

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
    out: list[Path] = []
    for (raw,) in rows:
        if not isinstance(raw, str) or not raw.strip():
            continue
        root = Path(raw)
        try:
            if root.is_dir():
                out.append(root)
        except OSError:
            continue
    return out


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


def candidate_entries() -> list[tuple[Path, str]]:
    """The approved scan set as ``(path, display)`` pairs, no wider."""
    from khipu import notes

    home = Path.home()
    roots = known_project_roots()
    out: list[tuple[Path, str]] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        if path not in seen:
            seen.add(path)
            out.append((path, display_path(path, home, roots)))

    for path in global_files(home):
        if path.is_file():
            add(path)
    for root in roots:
        for name in ("CLAUDE.md", "AGENTS.md"):
            path = root / name
            if path.is_file():
                add(path)
    for path, _harness, _slug in notes._iter_note_file_candidates():
        add(path)
    return out


def candidate_files() -> list[Path]:
    """The approved scan set (see ``candidate_entries``)."""
    return [path for path, _display in candidate_entries()]


def _signatures(entries: list[tuple[Path, str]]) -> dict[str, list[int]]:
    """``{path: [mtime_ns, size]}`` for every candidate that stats."""
    out: dict[str, list[int]] = {}
    for path, _display in entries:
        try:
            st = path.stat()
        except OSError:
            continue
        out[str(path)] = [st.st_mtime_ns, st.st_size]
    return out


def refresh(now: datetime | None = None) -> dict[str, Any]:
    """Scan every candidate file once and keep the report on this Mac.

    A file that cannot be read is skipped, never fatal; a file over the
    size cap is named too large instead of read. The scan reads only,
    and never edits a scanned file."""
    from khipu.routing_scanner import scan_text

    now = now or datetime.now(timezone.utc)
    entries = candidate_entries()
    hits: list[dict[str, Any]] = []
    too_large: list[str] = []
    hits_total = 0
    files_checked = 0
    for path, display in entries:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                too_large.append(display)
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        files_checked += 1
        claims = scan_text(str(path), text)
        hits_total += len(claims)
        kept = 0
        for claim in claims:
            if kept >= MAX_HITS_PER_FILE or len(hits) >= MAX_HITS_TOTAL:
                break
            hits.append({"file": display, "line": claim["line"],
                         "quote": _clip(claim["words"]), "superseded": True})
            kept += 1
    hits.sort(key=lambda h: (h["file"], h["line"]))
    report = {"status": "checked", "scanned_at": now.isoformat(),
              "files_checked": files_checked, "hits": hits,
              "hits_total": hits_total,
              "more_not_shown": hits_total - len(hits),
              "too_large": too_large,
              "file_sigs": _signatures(entries)}
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
    return {"status": "checked",
            "scanned_at": data.get("scanned_at") if isinstance(data.get("scanned_at"), str) else None,
            "files_checked": files_checked if isinstance(files_checked, int)
            and not isinstance(files_checked, bool) else 0,
            "hits": hits, "hits_total": hits_total, "more_not_shown": more,
            "too_large": [t for t in too_large if isinstance(t, str)]
            if isinstance(too_large, list) else []}


def _read_raw() -> dict[str, Any] | None:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("status") != "checked" \
            or not isinstance(data.get("hits"), list):
        return None
    return data


def report() -> dict[str, Any]:
    """The kept report, or the not-looked-at-yet state. A missing or
    damaged report reads as not checked, never as an error — and is
    never auto-rescanned here. A valid report whose scanned files have
    changed (mtime or size, a new candidate, or a removed one) is
    refreshed first: one bounded stat per candidate on this read path
    (``route --json`` / ``public_view``), never on a per-prompt path,
    and with no hub needed."""
    data = _read_raw()
    if data is None:
        return {"status": "not_checked", "scanned_at": None, "files_checked": 0,
                "hits": [], "hits_total": 0, "more_not_shown": 0, "too_large": []}
    public = _public(data)
    if public["status"] != "checked":
        return public
    try:
        if _signatures(candidate_entries()) != data.get("file_sigs"):
            return refresh()
    except Exception:  # noqa: BLE001 — a stale check must not break the read
        pass
    return public
