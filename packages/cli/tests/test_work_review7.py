"""The Work view and the ledger, review of ec862b4 (its probes ported first):

1. when the privacy table can't be built, only the view's own field names
   stay as object keys: a key merely shaped like one (a login folder's
   name used as a tracker id) is withheld with its value;
2. a summary file that grows while it is read is refused, never shown cut
   (work.py reads through ``safe_read.read_file`` only, which refuses a
   file whose size changed since it was opened).

The app's half is in ReviewRound7.test.tsx. Everything here is synthetic.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from khipu import agent_view, work

LOGIN = "review_private_login"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / LOGIN))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")


def _no_table(monkeypatch):
    def unavailable(*a, **k):
        raise RuntimeError("privacy list unavailable")

    monkeypatch.setattr(agent_view, "_table", unavailable)


# ---- 1. dynamic keys when the privacy table can't be built -------------------------------------


def test_failed_privacy_table_withholds_dynamic_tracker_id_keys(monkeypatch):
    payload = {"rows": {LOGIN: {"id": LOGIN, "title": "Synthetic row"}}}
    assert LOGIN not in json.dumps(agent_view.project(payload))
    _no_table(monkeypatch)
    result = agent_view.project(payload)
    assert result["incomplete"] is True
    assert LOGIN not in json.dumps(result), result
    assert result["rows"] == {agent_view.WITHHELD: agent_view.WITHHELD}


def test_failed_privacy_table_keeps_the_views_own_field_names(monkeypatch):
    _no_table(monkeypatch)
    out = agent_view.project({"rows": 3, "generated_at": "2026-10-16T12:00:00Z", "counts": {"waiting": 1}})
    assert out["rows"] == 3 and out["generated_at"] == "2026-10-16T12:00:00Z" and out["counts"] == {"waiting": 1}


def test_work_scan_without_the_table_drops_dynamic_keys(monkeypatch):
    _no_table(monkeypatch)
    scan = work._Scan()
    assert scan.degraded
    out = scan({"by_state": {LOGIN: 1}, "flagged": 0})
    assert LOGIN not in json.dumps(out) and out == {"by_state": {}, "flagged": 0}


# ---- 2. a file that grows while it is read -----------------------------------------------------


@pytest.mark.parametrize("capped", [True, False])
def test_work_source_refuses_file_that_grows_during_read(tmp_path, monkeypatch, capped):
    path = tmp_path / "summary.json"
    raw = json.dumps({"format": "khipu-work-summary", "version": 1, "written_at": "2026-10-16T12:00:00Z",
                      "project": {"id": "atlas", "name": "Atlas"}}).encode()
    path.write_bytes(raw)
    inode = path.stat().st_ino
    real_read = os.read
    grew = []

    def grow(fd, n):
        data = real_read(fd, n)
        if os.fstat(fd).st_ino == inode and not grew:
            grew.append(True)
            with path.open("ab") as f:
                f.write(b" " * 50)
        return data

    monkeypatch.setattr(os, "read", grow)
    cfg = work.settings({})
    cfg["work_file_max_bytes"] = len(raw) if capped else 0
    got = work.read_source({"project": "atlas", "file": str(path), "on": True}, cfg,
                           datetime(2026, 10, 16, 12, tzinfo=timezone.utc))
    assert grew and path.stat().st_size > len(raw)
    assert got["state"] in {"refused", "too_big", "unreadable"}, got


def test_work_source_reads_an_unchanged_file(tmp_path):
    path = tmp_path / "summary.json"
    raw = json.dumps({"format": "khipu-work-summary", "version": 1, "written_at": "2026-10-16T12:00:00Z",
                      "project": {"id": "atlas", "name": "Atlas"}}).encode()
    path.write_bytes(raw)
    cfg = work.settings({})
    cfg["work_file_max_bytes"] = len(raw)
    got = work.read_source({"project": "atlas", "file": str(path), "on": True}, cfg,
                           datetime(2026, 10, 16, 12, tzinfo=timezone.utc))
    assert got["state"] == "ok", got
