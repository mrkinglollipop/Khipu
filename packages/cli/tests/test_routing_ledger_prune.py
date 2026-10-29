import fcntl
import json
import os
import subprocess
import sys
import types
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


def test_old_ledger_is_removed_and_its_lock_stays(folder):
    p = _ledger(folder, "a", 30, 30)
    out = run(folder)
    assert not p.exists() and p.with_suffix(".lock").exists()
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


def test_two_writers_cannot_overlap_after_a_prune(folder):
    p = _ledger(folder, "a", 30, 30)
    lock = p.with_suffix(".lock")
    with lock.open("a") as early:
        # A hook opened the lock before the prune and takes it after.
        assert run(folder)["removed"] == 1
        fcntl.flock(early, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with lock.open("a") as late:
            with pytest.raises(BlockingIOError):
                fcntl.flock(late, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_oversized_ledger_with_recent_evidence_is_kept(folder):
    from khipu import routing_delivery

    p = folder / "recall--big.routing.json"
    p.write_text(json.dumps({"main": {"touched": _iso(-25000), "pad": "x" * (routing_delivery._SESSION_FILE_BUDGET + 10)}}))
    when = (NOW - timedelta(days=30)).timestamp()
    os.utime(p, (when, when))
    out = run(folder)
    assert p.exists() and out["removed"] == 0 and out["skipped"] == 1


def test_unparseable_ledger_is_kept(folder):
    p = folder / "recall--bad.routing.json"
    p.write_text("{not json")
    os.utime(p, (0, 0))
    out = run(folder)
    assert p.exists() and out["removed"] == 0 and out["skipped"] == 1


def test_fifo_and_symlinks_are_never_opened_or_removed(folder):
    os.mkfifo(folder / "recall--fifo.routing.json")
    target = folder / "elsewhere.json"
    target.write_text(json.dumps({"main": {"touched": _iso(30)}}))
    os.utime(target, (0, 0))
    (folder / "recall--link.routing.json").symlink_to(target)
    (folder / "recall--fifolink.routing.json").symlink_to(folder / "recall--fifo.routing.json")
    code = ("import json, sys; from pathlib import Path; from khipu import routing_ledger_prune as r; "
            f"print(json.dumps(r.prune(keep_days=7, now={NOW.timestamp()}, folder=Path(sys.argv[1]))))")
    done = subprocess.run([sys.executable, "-c", code, str(folder)], capture_output=True, text=True,
                          timeout=30, env=os.environ)
    out = json.loads(done.stdout)
    assert out["skipped"] == 3 and out["removed"] == 0, out
    assert target.exists()
    assert all((folder / n).is_symlink() for n in ("recall--link.routing.json", "recall--fifolink.routing.json"))


def test_deadline_is_checked_while_listing_the_folder(folder, monkeypatch):
    pulled = []

    class Entry:
        def __init__(self, i):
            self.name = f"junk-{i}"
            self.path = str(folder / self.name)

        def is_dir(self, **_kw):
            return False

        def is_symlink(self):
            return False

    class Listing:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def __iter__(self):
            for i in range(5000):
                pulled.append(i)
                yield Entry(i)

        def close(self):
            pass

    ticks = iter(range(10**6))
    monkeypatch.setattr(rlp, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(os, "scandir", lambda *_a, **_k: Listing())
    out = run(folder, budget_s=10)
    assert out["time_budget_exhausted"] is True
    assert len(pulled) < 50
