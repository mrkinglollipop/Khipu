"""Synthetic filesystem stalls cannot block routing readers or report refresh."""
from __future__ import annotations

import multiprocessing
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from khipu import routing_scan_report as scan


def bounded_call(fn, seconds=2.0):
    """The regression itself has a killable outer guard, even on old code."""
    ctx = multiprocessing.get_context("fork")
    receive, send = ctx.Pipe(duplex=False)
    def run():
        try:
            send.send((True, fn()))
        except BaseException as exc:
            send.send((False, repr(exc)))
    process = ctx.Process(target=run)
    process.start()
    send.close()
    try:
        assert receive.poll(seconds), "filesystem stall escaped the refresh deadline"
        ok, result = receive.recv()
        assert ok, result
        return result
    finally:
        process.kill() if process.is_alive() else None
        process.join(timeout=0.2)
        receive.close()


def setup_scan(tmp_path, monkeypatch):
    cache = tmp_path / "state" / "report.json"
    candidate = tmp_path / "AGENTS.md"
    candidate.write_text("Prefer Codex first\n", encoding="utf-8")
    monkeypatch.setattr(scan, "store_path", lambda: cache)
    monkeypatch.setattr(scan, "candidate_entries", lambda: [(candidate, "AGENTS.md")])
    monkeypatch.setattr(scan, "FILE_DEADLINE_SECONDS", 0.08, raising=False)
    monkeypatch.setattr(scan, "DISCOVERY_DEADLINE_SECONDS", 0.08, raising=False)
    monkeypatch.setattr(scan, "REFRESH_BUDGET_SECONDS", 0.3, raising=False)
    return candidate


def test_report_never_discovers_stats_or_refreshes_candidates(tmp_path, monkeypatch):
    setup_scan(tmp_path, monkeypatch)
    scan.refresh()
    called = []
    def forbidden():
        called.append(True)
        raise AssertionError("candidate discovery on routing read")
    monkeypatch.setattr(scan, "candidate_entries", forbidden)
    monkeypatch.setattr(scan, "refresh", forbidden)
    assert scan.report()["hits"]
    assert called == []


@pytest.mark.parametrize("operation", ["stat", "open", "read"])
def test_each_candidate_io_stall_times_out_once(tmp_path, monkeypatch, operation):
    candidate = setup_scan(tmp_path, monkeypatch)
    original_stat, original_open = Path.stat, Path.open
    def hanging_stat(path, *a, **kw):
        if path == candidate and operation == "stat":
            time.sleep(20)
        return original_stat(path, *a, **kw)
    class HangingRead:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, *args):
            time.sleep(20)
    def hanging_open(path, *a, **kw):
        if path == candidate:
            if operation == "open":
                time.sleep(20)
            if operation == "read":
                return HangingRead()
        return original_open(path, *a, **kw)
    monkeypatch.setattr(Path, "stat", hanging_stat)
    monkeypatch.setattr(Path, "open", hanging_open)
    report = bounded_call(scan.refresh)
    assert report["files_checked"] == 0
    assert report["skipped"] == [{"file": "AGENTS.md", "reason": "timeout", "message": "Couldn't read in time."}]


def test_discovery_stall_is_bounded_and_visible(tmp_path, monkeypatch):
    setup_scan(tmp_path, monkeypatch)
    monkeypatch.setattr(scan, "candidate_entries", lambda: time.sleep(20))
    report = bounded_call(scan.refresh)
    assert report["skipped"] == [{"file": "candidate discovery", "reason": "timeout", "message": "Couldn't read in time."}]


def test_refresh_total_budget_caps_many_stalled_files(tmp_path, monkeypatch):
    candidate = setup_scan(tmp_path, monkeypatch)
    monkeypatch.setattr(scan, "candidate_entries", lambda: [
        (candidate.with_name(f"{i}.md"), f"{i}.md") for i in range(100)])
    original_stat = Path.stat
    def stall_candidates(path, *a, **kw):
        if path.parent == candidate.parent and path.suffix == ".md":
            time.sleep(20)
        return original_stat(path, *a, **kw)
    monkeypatch.setattr(Path, "stat", stall_candidates)
    report = bounded_call(scan.refresh)
    assert any(item["reason"] == "refresh_budget" for item in report["skipped"])
    assert len(report["skipped"]) <= 6


@pytest.mark.parametrize("relative", ["Documents", "Desktop", "Downloads",
    "Library/Mobile Documents", "Library/CloudStorage", "Library/iCloud", "iCloud Drive"])
def test_protected_paths_are_skipped_without_any_metadata(tmp_path, monkeypatch, relative):
    setup_scan(tmp_path, monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    path = tmp_path / relative / "Repo" / "AGENTS.md"
    monkeypatch.setattr(scan, "candidate_entries", lambda: [(path, "Repo/AGENTS.md")])
    original_stat = Path.stat
    touched = tmp_path / "touched"
    def blocked_metadata(p, *a, **kw):
        if p == path or p in path.parents and p != tmp_path:
            touched.write_text("metadata touched")
            raise AssertionError("protected candidate metadata")
        return original_stat(p, *a, **kw)
    monkeypatch.setattr(Path, "stat", blocked_metadata)
    report = bounded_call(scan.refresh)
    assert not touched.exists()
    assert report["skipped"] == [{"file": "Repo/AGENTS.md", "reason": "protected_path"}]


def test_dataless_candidate_is_not_opened(tmp_path, monkeypatch):
    candidate = setup_scan(tmp_path, monkeypatch)
    original_stat = Path.stat
    def flagged(path, *a, **kw):
        if path == candidate:
            value = original_stat(path, *a, **kw)
            return SimpleNamespace(st_mode=value.st_mode, st_size=value.st_size,
                                   st_flags=0x40000000, st_mtime_ns=value.st_mtime_ns)
        return original_stat(path, *a, **kw)
    monkeypatch.setattr(Path, "stat", flagged)
    report = bounded_call(scan.refresh)
    assert report["files_checked"] == 0
    assert report["skipped"] == [{"file": "AGENTS.md", "reason": "dataless"}]


def test_size_growth_after_stat_still_uses_bounded_read(tmp_path, monkeypatch):
    candidate = setup_scan(tmp_path, monkeypatch)
    original_open = Path.open
    requested = tmp_path / "requested"
    class GrowingRead:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, size=-1):
            requested.write_text(str(size))
            return b"x" * (scan.MAX_FILE_BYTES + 1)
    monkeypatch.setattr(Path, "open", lambda path, *a, **kw:
        GrowingRead() if path == candidate else original_open(path, *a, **kw))
    report = bounded_call(scan.refresh)
    assert int(requested.read_text()) == scan.MAX_FILE_BYTES + 1
    assert report["too_large"] == ["AGENTS.md"]


def test_cached_report_fifo_cannot_block_reader(tmp_path, monkeypatch):
    cache = tmp_path / "scan-report.json"
    os.mkfifo(cache)
    monkeypatch.setattr(scan, "store_path", lambda: cache)
    monkeypatch.setattr(scan, "FILE_DEADLINE_SECONDS", 0.08, raising=False)
    assert bounded_call(scan.report)["status"] == "not_checked"


def test_discovery_preserves_dot_md_note(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(scan, "known_project_roots", lambda: [])
    note = tmp_path / ".codex" / "memories" / ".md"
    note.parent.mkdir(parents=True)
    note.write_text("Prefer Codex first\n", encoding="utf-8")
    assert note in scan.candidate_files()
