import fcntl
import json
import os
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from khipu import jobs, routing_ledger_prune as rlp

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def _iso(delta_days):
    return (NOW - timedelta(days=delta_days)).isoformat().replace("+00:00", "Z")


def _ledger(folder, name, stamp_days, mtime_days, lock=True):
    path = folder / f"recall--{name}.routing.json"
    path.write_text(json.dumps({"main": {"last_delivery_point": _iso(stamp_days), "touched": _iso(stamp_days)}}))
    when = (NOW - timedelta(days=mtime_days)).timestamp()
    os.utime(path, (when, when))
    if lock:
        path.with_suffix(".lock").write_text("")
    return path


@pytest.fixture
def folder(tmp_path):
    return tmp_path


def run(folder, **kw):
    return rlp.prune(keep_days=kw.pop("keep_days", 7), now=NOW.timestamp(), folder=folder, **kw)


def test_old_ledger_and_its_free_lock_are_removed(folder):
    p = _ledger(folder, "a", 30, 30)
    out = run(folder)
    assert not p.exists() and not p.with_suffix(".lock").exists()
    assert (out["scanned"], out["removed"], out["kept"], out["busy"], out["errors"]) == (1, 1, 0, 0, 0)
    assert out["ok"] is True


def test_recent_ledger_is_kept(folder):
    p = _ledger(folder, "a", 1, 1)
    out = run(folder)
    assert p.exists() and p.with_suffix(".lock").exists()
    assert out["kept"] == 1 and out["removed"] == 0


def test_old_timestamp_but_recent_mtime_is_kept(folder):
    p = _ledger(folder, "a", 30, 1)
    assert run(folder)["kept"] == 1 and p.exists()


def test_recent_timestamp_but_old_mtime_is_kept(folder):
    p = _ledger(folder, "a", 1, 30)
    assert run(folder)["kept"] == 1 and p.exists()


def test_held_lock_is_skipped(folder):
    p = _ledger(folder, "a", 30, 30)
    with p.with_suffix(".lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        out = run(folder)
    assert p.exists() and p.with_suffix(".lock").exists()
    assert out["busy"] == 1 and out["removed"] == 0 and out["ok"] is True


def test_unrelated_files_are_untouched(folder):
    _ledger(folder, "a", 30, 30)
    others = [folder / n for n in ("t3-pin--x.json", "recall--x.json", "recall--x.lock", "gateway.routing.copy", "other.lock")]
    for o in others:
        o.write_text("{}")
        os.utime(o, (0, 0))
    run(folder)
    assert all(o.exists() for o in others)


def test_zero_disables(folder):
    p = _ledger(folder, "a", 300, 300)
    out = run(folder, keep_days=0)
    assert p.exists() and out["scanned"] == 0 and out["ok"] is True


def test_reads_setting_default_seven(folder, monkeypatch):
    p = _ledger(folder, "a", 30, 30)
    with mock.patch("khipu.recall_prompt._dedup_path", return_value=folder / "recall--ledger.json"):
        out = rlp.prune(now=NOW.timestamp())
    assert out["keep_days"] == 7 and not p.exists()


def test_nightly_step_reports_counts(folder, monkeypatch):
    _ledger(folder, "a", 30, 30)
    _ledger(folder, "b", 1, 1)
    monkeypatch.setattr(jobs, "_nightly_log", lambda *_a, **_k: None)
    with mock.patch("khipu.recall_prompt._dedup_path", return_value=folder / "recall--ledger.json"):
        res = jobs._step_result(jobs._prune_routing_ledgers())
    assert res["ok"] is True
    c = res["counts"]
    assert (c["scanned"], c["removed"], c["kept"], c["busy"], c["errors"]) == (2, 1, 1, 0, 0)
