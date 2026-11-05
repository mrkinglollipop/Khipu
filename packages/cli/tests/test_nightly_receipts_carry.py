"""A nightly in progress must not erase last night's receipts (2026-11-05).

The nightly restarts nightly-last.json at its first step and runs for over an
hour, so every step it has not reached yet read "ran recently, left no
receipt" and `khipu promote` refused. Hermetic: throwaway data dir.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from khipu import jobs

NOW = datetime.now(timezone.utc)


def _ts(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(jobs, "ensure_data_dir", lambda: tmp_path)
    monkeypatch.setattr(jobs, "_CARRIED_STEPS", [])
    return tmp_path


def _write(data_dir, doc):
    (data_dir / "nightly-last.json").write_text(json.dumps(doc), encoding="utf-8")


def _health():
    with mock.patch.object(jobs, "_is_index_sync_host", return_value=True), \
            mock.patch("khipu.features.enabled", return_value=False), \
            mock.patch.object(jobs, "_job_entry", return_value={
                "last_exit": 0, "last_exit_ts": _ts(0.5), "last_run_iso": _ts(0.5),
                "plist_loaded": True, "plist_mtime": NOW.timestamp() - 3600}):
        return jobs.nightly_step_health()


def _night(names, hours_ago, ok=True):
    return [{"name": n, "ok": ok, "counts": {"big": "x" * 50}, "error": None if ok else "boom",
             "ts": _ts(hours_ago)} for n in names]


def test_partial_run_keeps_last_nights_receipts_for_unreached_steps(data_dir):
    _write(data_dir, {"steps": _night(["sessions_drain", "ledger", "snapshot_refresh"], 24)})
    jobs._capture_previous_receipts()
    steps: list = []
    jobs._record_nightly_step(steps, "sessions_drain", ok=True)

    doc = json.loads((data_dir / "nightly-last.json").read_text())
    assert [e["name"] for e in doc["steps"]] == ["sessions_drain"]
    assert sorted(e["name"] for e in doc["previous_steps"]) == ["ledger", "snapshot_refresh"]
    assert all("counts" not in e for e in doc["previous_steps"])  # receipts only, not payloads

    health = _health()
    assert health["ledger_ok"]["ok"] is True and health["ledger_ok"]["carried"] is True
    assert health["snapshot_refresh_ok"]["ok"] is True


def test_a_step_that_failed_last_night_stays_red_while_tonight_runs(data_dir):
    _write(data_dir, {"steps": _night(["ledger"], 24, ok=False)})
    jobs._capture_previous_receipts()
    jobs._record_nightly_step([], "sessions_drain", ok=True)
    assert _health()["ledger_ok"]["ok"] is False


def test_a_carried_receipt_still_goes_stale(data_dir):
    _write(data_dir, {"steps": _night(["ledger"], jobs.NIGHTLY_STEP_STALE_HOURS + 2)})
    jobs._capture_previous_receipts()
    jobs._record_nightly_step([], "sessions_drain", ok=True)
    out = _health()["ledger_ok"]
    assert out["ok"] is False and out.get("stale") is True


def test_tonights_own_receipt_replaces_the_carried_one(data_dir):
    _write(data_dir, {"steps": _night(["ledger"], 24)})
    jobs._capture_previous_receipts()
    steps: list = []
    jobs._record_nightly_step(steps, "ledger", ok=False, error="tonight")
    doc = json.loads((data_dir / "nightly-last.json").read_text())
    assert doc["previous_steps"] == []
    out = _health()["ledger_ok"]
    assert out["ok"] is False and "carried" not in out


def test_a_run_that_stopped_partway_keeps_the_older_receipts_it_carried(data_dir):
    _write(data_dir, {"steps": _night(["sessions_drain", "ledger"], 24)})
    jobs._capture_previous_receipts()
    steps: list = []
    jobs._record_nightly_step(steps, "sessions_drain", ok=True)  # then the process died
    jobs._capture_previous_receipts()  # the next night starts
    jobs._record_nightly_step([], "sessions_drain", ok=True)
    doc = json.loads((data_dir / "nightly-last.json").read_text())
    assert [e["name"] for e in doc["previous_steps"]] == ["ledger"]


def test_no_prior_receipt_is_still_unmeasured_not_green(data_dir):
    jobs._capture_previous_receipts()
    jobs._record_nightly_step([], "sessions_drain", ok=True)
    out = _health()["ledger_ok"]
    assert out["ok"] is False and out.get("unmeasured") is True
