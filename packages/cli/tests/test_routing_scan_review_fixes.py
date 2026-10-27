"""Review regressions: bounded pointer lookup and honest cached scan evidence."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import notes, paths
from khipu import routing_scan_report as scan
from tests.test_routing_scan_deadlines import bounded_call

NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


@pytest.fixture
def local_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(scan, "known_project_roots", lambda: [])
    monkeypatch.setattr(notes, "cursor_memory_roots", lambda: [])
    monkeypatch.setattr(notes, "aegis_memory_roots", lambda: [])
    monkeypatch.setattr(scan, "store_path", lambda: tmp_path / "scan-report.json")
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(scan, "datetime", Clock)
    return tmp_path


@pytest.mark.parametrize("operation", ["stat", "read"])
def test_data_directory_pointer_resolution_is_inside_reader_deadline(tmp_path, monkeypatch, operation):
    for key in ("KHIPU_ISOLATED_DIR", "KHIPU_DATA_DIR", "ALZY_DATA_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(paths, "DEFAULT_DIR", tmp_path / "config")
    pointer = paths.pointer_file()
    pointer.parent.mkdir()
    pointer.write_text(json.dumps({"path": str(tmp_path / "data")}))
    original_stat, original_read = Path.stat, Path.read_text
    def stat(path, *args, **kwargs):
        if path == pointer and operation == "stat":
            time.sleep(20)
        return original_stat(path, *args, **kwargs)
    def read(path, *args, **kwargs):
        if path == pointer and operation == "read":
            time.sleep(20)
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(scan, "FILE_DEADLINE_SECONDS", 0.03)
    assert bounded_call(scan.report, seconds=0.5)["status"] == "not_checked"


def cache(scanned_at, **extra):
    return {"status": "checked", "scanned_at": scanned_at,
            "files_checked": 1, "hits_total": 1, "more_not_shown": 0,
            "hits": [{"file": "~/AGENTS.md", "line": 1,
                      "quote": "Prefer Codex first", "superseded": True}],
            "too_large": [], "skipped": [], **extra}


@pytest.mark.parametrize("stamp,expected", [
    (NOW.isoformat(), "checked"),
    ((NOW - timedelta(hours=24, microseconds=-1)).isoformat(), "checked"),
    ((NOW - timedelta(hours=24)).isoformat(), "stale"),
    ((NOW - timedelta(hours=24, microseconds=1)).isoformat(), "stale"),
    ((NOW + timedelta(microseconds=1)).isoformat(), "stale"),
    ("2000-01-01T00:00:00Z", "stale"),
    ("damaged", "stale"), ("2026-10-26T12:00:00", "stale"), (None, "stale"),
])
def test_cache_freshness_never_rechecks_candidates(local_scan, monkeypatch, stamp, expected):
    data = cache(stamp)
    scan.store_path().write_text(json.dumps(data))
    def forbidden(*args, **kwargs):
        raise AssertionError("cached reader inspected candidates")
    monkeypatch.setattr(scan, "candidate_entries", forbidden)
    monkeypatch.setattr(scan, "refresh", forbidden)
    result = scan.report()
    assert result["status"] == expected
    assert result["hits"] == data["hits"]
    assert result["scanned_at"] == stamp


@pytest.mark.parametrize("extra", [
    {"skipped": [{"file": "~/Documents/Repo/AGENTS.md", "reason": "protected_path"}]},
    {"skipped": [{"file": "~/AGENTS.md", "reason": "timeout"}]},
    {"too_large": ["~/AGENTS.md"]},
])
def test_incomplete_cache_preserves_evidence_and_expiry(local_scan, extra):
    data = cache(NOW.isoformat(), **extra)
    scan.store_path().write_text(json.dumps(data))
    result = scan.report()
    assert result["status"] == "incomplete"
    assert result["hits"] == data["hits"]
    assert result["too_large"] == data["too_large"]
    assert [(s["file"], s["reason"]) for s in result["skipped"]] == [
        (s["file"], s["reason"]) for s in data["skipped"]]
    data["scanned_at"] = (NOW - timedelta(days=2)).isoformat()
    scan.store_path().write_text(json.dumps(data))
    assert scan.report()["status"] == "stale"


@pytest.mark.parametrize("operation", ["metadata", "list"])
def test_unreadable_discovery_directory_is_visible(local_scan, monkeypatch, operation):
    directory = local_scan / ".codex" / "memories"
    directory.mkdir(parents=True)
    original_stat, original_iterdir = Path.stat, Path.iterdir
    def stat(path, *args, **kwargs):
        if path == directory and operation == "metadata":
            raise PermissionError("synthetic access refusal")
        return original_stat(path, *args, **kwargs)
    def iterdir(path):
        if path == directory and operation == "list":
            raise PermissionError("synthetic access refusal")
        return original_iterdir(path)
    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    result = bounded_call(lambda: scan.refresh(now=NOW))
    assert {"file": "~/.codex/memories", "reason": "unreadable"} in result["skipped"]
    assert result["status"] == "incomplete"


def test_missing_optional_discovery_directories_are_ordinary(local_scan):
    result = scan.refresh(now=NOW)
    assert result["status"] == "checked"
    assert result["skipped"] == []


def test_discovery_candidate_limit_is_visible(local_scan, monkeypatch):
    directory = local_scan / ".codex" / "memories"
    directory.mkdir(parents=True)
    for name in ("a.md", "b.md"):
        (directory / name).write_text("Prefer Codex first\n")
    monkeypatch.setattr(scan, "_MAX_CANDIDATES", 5)  # four optional globals, one note
    result = scan.refresh(now=NOW)
    assert {"file": "candidate discovery", "reason": "candidate_limit"} in result["skipped"]
    assert result["status"] == "incomplete"
    assert result["files_checked"] == 1


def test_protected_discovery_directory_uses_no_metadata(local_scan, monkeypatch):
    directory = local_scan / "Documents" / "notes"
    monkeypatch.setattr(notes, "codex_memories_root", lambda: directory)
    original_stat = Path.stat
    touched = local_scan / "metadata-touched"
    def stat(path, *args, **kwargs):
        if path == directory or local_scan / "Documents" in path.parents:
            touched.write_text("unexpected metadata")
            raise AssertionError("protected metadata")
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", stat)
    result = bounded_call(lambda: scan.refresh(now=NOW))
    assert not touched.exists()
    assert {"file": "~/Documents/notes", "reason": "protected_path"} in result["skipped"]
    assert result["status"] == "incomplete"
