"""Nightly and doctor must tell the truth (reliability audit 2026-11-02).

Each test names one lead: write it so the old code fails, then keep it green
after the fix. Hermetic: throwaway HOME, no real database or service.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from khipu import hub_snapshot as hs
from khipu import jobs, promote as pm
from khipu import recall_prompt as rp
from khipu import routing_ledger_prune as rlp
from khipu import storage


# ---- 1. snapshot_refresh: lock contention is a deferral, not a red night ----


def test_refresh_busy_is_deferred_not_a_hard_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KHIPU_TEST", "1")
    busy = {"ok": False, "error": "hub snapshot refresh already in progress"}

    with mock.patch("khipu.hub_snapshot.refresh", return_value=busy), \
            mock.patch("khipu.hub_snapshot.snapshot_health", return_value={
                "exists": True, "age_seconds": 60, "refreshed_at": datetime.now(timezone.utc).isoformat(),
            }), \
            mock.patch.object(jobs, "_wait_refresh_idle", return_value=True):
        res = jobs._refresh_snapshot()
    step = jobs._step_result(res)
    assert step["ok"] is True
    assert "deferred" in step
    assert "in progress" in step["deferred"].lower() or "refresh" in step["deferred"].lower()


def test_refresh_fails_only_when_still_stale_after_holder_finishes(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KHIPU_TEST", "1")
    busy = {"ok": False, "error": "hub snapshot refresh already in progress"}
    calls = {"n": 0}

    def refresh():
        calls["n"] += 1
        # After the holder finishes we get a real miss while the replica is
        # still stale — that is tonight's failure, not a deferral.
        if calls["n"] > jobs._REFRESH_BUSY_RETRIES + 1:
            return {"ok": False, "error": "hub unreachable"}
        return busy

    with mock.patch("khipu.hub_snapshot.refresh", side_effect=refresh), \
            mock.patch("khipu.hub_snapshot.snapshot_health", return_value={
                "exists": True, "age_seconds": 48 * 3600, "refreshed_at": (
                    datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
            }), \
            mock.patch.object(jobs, "_wait_refresh_idle", return_value=True):
        res = jobs._refresh_snapshot()
    assert res["ok"] is False
    assert "stale" in str(res.get("error") or "").lower()


def test_memory_lock_wait_is_bounded_while_refresh_lock_is_held(tmp_path, monkeypatch):
    """While the refresh lock is held, the publication must not wait forever
    on the memory lock (a forget would keep every other dump 'in progress')."""
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KHIPU_TEST", "1")
    from khipu import memory_lock

    seen: list[float | None] = []
    real_shared = memory_lock.shared

    def tracking_shared(wait=None):
        seen.append(wait)
        if wait is None:
            raise memory_lock.MemoryLockError("unbounded wait refused by test")
        raise memory_lock.MemoryLockError("memory lock busy until the deadline")

    monkeypatch.setattr(memory_lock, "shared", tracking_shared)
    dest = tmp_path / "hub_snapshot.sqlite"
    dest.write_bytes(b"")
    with pytest.raises(Exception):
        with hs._replica_write_gate(dest, refresh_lock_held=True, scrub=False):
            pass
    assert seen and all(w is not None for w in seen), seen


# ---- 2. sweep_stale_temps without the refresh lock ----


def test_sweep_reclaims_graph_build_temps_without_the_refresh_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KHIPU_TEST", "1")
    # Hold the refresh lock the whole time: the old sweep returns ok and
    # deletes nothing.
    lock = hs._acquire_refresh_lock()
    assert lock is not None
    try:
        graph_tmp = Path(tempfile.gettempdir())
        # Prefer a private dir so we never touch the real machine's leftovers.
        private = tmp_path / "tmp"
        private.mkdir()
        stale = private / "graph_build_deadbeef.sqlite"
        stale.write_bytes(b"x" * 100)
        then = time.time() - hs.STALE_TEMP_AGE_S - 60
        os.utime(stale, (then, then))
        young = private / "graph_build_alive.sqlite"
        young.write_bytes(b"y")
        other = private / "not_graph_build.sqlite"
        other.write_bytes(b"z")
        os.utime(other, (then, then))

        with mock.patch.object(hs, "_graph_temp_dirs", return_value=[private]), \
                mock.patch.object(hs, "snapshot_path", return_value=tmp_path / "hub_snapshot.sqlite"):
            out = hs.sweep_stale_temps()
        assert out["ok"] is True
        assert stale.name in out["removed"]
        assert not stale.exists()
        assert young.exists() and other.exists()
        assert "left" in out
    finally:
        hs._release_refresh_lock(lock)


def test_sweep_never_claims_success_for_doing_nothing_when_busy_with_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KHIPU_TEST", "1")
    old = tmp_path / ".hub_snapshot.old.sqlite"
    old.write_bytes(b"pages")
    then = time.time() - hs.STALE_TEMP_AGE_S - 1
    os.utime(old, (then, then))
    lock = hs._acquire_refresh_lock()
    assert lock is not None
    try:
        with mock.patch.object(hs, "snapshot_path", return_value=tmp_path / "hub_snapshot.sqlite"), \
                mock.patch.object(hs, "_graph_temp_dirs", return_value=[]):
            out = hs.sweep_stale_temps()
        # Age-based reclaim must still remove the leftover even while a dump
        # holds the refresh lock (the leftover is not that dump's open file).
        assert out["ok"] is True
        assert old.name in out.get("removed", [])
        assert not old.exists()
    finally:
        hs._release_refresh_lock(lock)


# ---- 3. disk_space covers every volume Khipu writes ----


def test_disk_space_stats_the_inactive_live_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KHIPU_TEST", "1")
    (tmp_path / "data").mkdir()
    slot = tmp_path / ".config" / "khipu" / "live-b"
    slot.mkdir(parents=True)
    # Put the inactive slot on a "different device" by stubbing st_dev.
    seen_paths: list[str] = []

    real_stat = Path.stat

    def fake_stat(self, *a, **k):
        st = real_stat(self, *a, **k)
        if "live-b" in str(self) or "live-a" in str(self):
            return os.stat_result((st.st_mode, st.st_ino, 99999, st.st_nlink, st.st_uid, st.st_gid,
                                   st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
        return st

    usages = {}

    def fake_usage(path):
        seen_paths.append(str(path))
        import shutil as _sh
        # Red on the slot's device only.
        if "live" in str(path) or str(path).endswith(str(slot)) or Path(path).name in ("live-a", "live-b"):
            return _sh._ntuple_diskusage(int(400e9), int(398e9), int(2e9))
        return _sh._ntuple_diskusage(int(400e9), int(100e9), int(300e9))

    monkeypatch.setattr(Path, "stat", fake_stat)
    monkeypatch.setattr(storage.shutil, "disk_usage", fake_usage)
    monkeypatch.setattr(pm, "config_dir", lambda: tmp_path / ".config" / "khipu")
    monkeypatch.setattr(pm, "active_slot", lambda: tmp_path / ".config" / "khipu" / "live-a")
    (tmp_path / ".config" / "khipu" / "live-a").mkdir(parents=True, exist_ok=True)

    out = storage.disk_space()
    labels = [v["label"] for v in out["volumes"]]
    assert any("inactive" in lab.lower() or "live" in lab.lower() for lab in labels), labels
    assert out["level"] == "red"
    assert out["ok"] is False


def test_disk_space_stats_a_configured_worktree_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KHIPU_TEST", "1")
    (tmp_path / "data").mkdir()
    wt = tmp_path / "worktrees"
    wt.mkdir()
    monkeypatch.setattr("khipu.config.load_config", lambda: {"worktree_root": str(wt)})

    real_stat = Path.stat

    def fake_stat(self, *a, **k):
        st = real_stat(self, *a, **k)
        if "worktrees" in str(self):
            return os.stat_result((st.st_mode, st.st_ino, 88888, st.st_nlink, st.st_uid, st.st_gid,
                                   st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
        return st

    def fake_usage(path):
        import shutil as _sh
        if "worktrees" in str(path):
            return _sh._ntuple_diskusage(int(100e9), int(98e9), int(2e9))
        return _sh._ntuple_diskusage(int(400e9), int(100e9), int(300e9))

    monkeypatch.setattr(Path, "stat", fake_stat)
    monkeypatch.setattr(storage.shutil, "disk_usage", fake_usage)
    out = storage.disk_space()
    assert any("worktree" in v["label"].lower() for v in out["volumes"]), out["volumes"]
    assert out["level"] == "red"


# ---- 4. Doctor rows for every nightly step; prune/briefs tell the truth ----


def test_doctor_reads_summary_kind_routing_prune_and_project_renames(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    recent = datetime.now(timezone.utc).isoformat()
    (tmp_path / "nightly-last.json").write_text(json.dumps({"steps": [
        {"name": "summary_kind_backfill", "ok": False, "error": "boom", "ts": recent},
        {"name": "routing_ledger_prune", "ok": False, "error": "budget", "ts": recent},
        {"name": "project_renames", "ok": False, "error": "gh down", "ts": recent},
    ]}))
    with mock.patch.object(jobs, "_is_index_sync_host", return_value=True), \
            mock.patch.object(jobs, "ensure_data_dir", return_value=tmp_path), \
            mock.patch("khipu.features.enabled", return_value=False):
        health = jobs.nightly_step_health()
    for key in ("summary_kind_backfill_ok", "routing_ledger_prune_ok", "project_renames_ok"):
        assert key in health, sorted(health)
        assert health[key]["ok"] is False


def test_routing_ledger_prune_budget_exhaustion_is_visible(tmp_path):
    # One huge ledger the prune skips forever, and a budget that expires
    # before any removal: must not read as a silent green success.
    big = tmp_path / "recall--big.routing.json"
    from khipu import routing_delivery
    big.write_text(json.dumps({"main": {"touched": "2000-01-01T00:00:00Z",
                                        "pad": "x" * (routing_delivery._SESSION_FILE_BUDGET + 10)}}))
    os.utime(big, (0, 0))
    out = rlp.prune(keep_days=7, now=time.time(), folder=tmp_path, budget_s=0)
    assert out.get("time_budget_exhausted") or out.get("skipped")
    # Visible to the nightly step / doctor: not a bare ok with removed=0.
    assert out.get("deferred") or out.get("time_budget_exhausted") or not (
        out.get("ok") and out.get("removed") == 0 and not out.get("skipped")
    )


def test_briefs_lock_held_is_deferred_not_built_zero_green(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(jobs, "ensure_data_dir", lambda: tmp_path)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return self

        def commit(self):
            pass

    with mock.patch("khipu.features.enabled", return_value=True), \
            mock.patch("khipu.db.connect", return_value=_Conn()), \
            mock.patch("khipu.briefs.acquire_build_lock", return_value=False), \
            mock.patch.object(jobs, "_nightly_log"):
        out = jobs._briefs_build_if_on()
    assert out["ok"] is True
    assert isinstance(out.get("deferred"), str) and "lock" in out["deferred"].lower()
    assert out.get("reason") != "lock held" or "deferred" in out


def test_briefs_deferred_two_nights_running_goes_red(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(jobs, "ensure_data_dir", lambda: tmp_path)
    yesterday = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()
    previous = {"name": "briefs_build", "ok": True, "deferred": "lock held", "ts": yesterday,
                "counts": {"built": 0}, "error": None}

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return self

        def commit(self):
            pass

    with mock.patch("khipu.features.enabled", return_value=True), \
            mock.patch("khipu.db.connect", return_value=_Conn()), \
            mock.patch("khipu.briefs.acquire_build_lock", return_value=False), \
            mock.patch.object(jobs, "_nightly_log"):
        out = jobs._briefs_build_if_on(previous=previous)
    assert out["ok"] is False
    assert "deferred" in str(out.get("deferred") or out.get("error") or "").lower() or "lock" in str(
        out.get("error") or "").lower()


# ---- 5. Promote: judge the candidate against live; thin sample is yellow ----


def test_search_problem_timeout_uses_live_slack():
    # Live was also past the budget: shared load, not the candidate.
    run = {"exit": 0, "valid_json": True, "reason": "timeout>1.2s", "ms": 1250.0, "hits": []}
    assert pm._search_problem(run, (1300.0,)) is None
    # Live succeeded well under budget: the candidate's timeout is a refusal.
    assert pm._search_problem(run, (300.0,)) == "timeout>1.2s"


def test_promote_refuses_candidate_timeout_where_live_succeeded(home, monkeypatch):
    candidate = _one_query(home)

    def fake_run(argv, *, env=None, input=None, cwd=None, timeout=120):
        import subprocess
        if argv[1:2] == ["-c"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"push": True}) + "\n", "")
        cand = "/candidate/packages/" in argv[0]
        on = env.get("KHIPU_FEATURE_ROUTING_PUSH") == "1"
        if cand:
            # Past the budget wall so slowness/timeout is unambiguous.
            reason, ms, hits = "timeout>1.2s", 1400.0, ["episode:3"]
        else:
            reason, ms, hits = "ok", 300.0, ["episode:3"]
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = (f"{stamp} [khipu-prompt-recall] session=s1 reason={reason} ms={ms} "
                f"hits={hits} legs=['lexical'] degraded=None")
        if on:
            line += " routing=full"
        log = Path(env["KHIPU_CAPTURE_HOME"]) / "logs" / "prompt-recall.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(line + "\n")
        return subprocess.CompletedProcess(argv, 0, '{"hookSpecificOutput": {}}\n', "")

    monkeypatch.setattr(pm, "_run", fake_run)
    out = pm.gate_recall_replay(candidate)
    assert not out["ok"]
    assert "timeout" in out["detail"]


def test_promote_refuses_candidate_slower_than_live_by_more_than_slack(home, monkeypatch):
    from tests.test_promote import _one_query as oq, _timed_hook
    candidate = oq(home)
    monkeypatch.setattr(pm, "_run", _timed_hook(1000.0, 1300.0, 1300.0))
    out = pm.gate_recall_replay(candidate)
    assert not out["ok"]
    assert "1300 ms > 1200 ms" in out["detail"]


def test_promote_refuses_candidate_crash(home, monkeypatch):
    candidate = _one_query(home)

    def fake_run(argv, *, env=None, input=None, cwd=None, timeout=120):
        import subprocess
        if argv[1:2] == ["-c"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"push": True}) + "\n", "")
        cand = "/candidate/packages/" in argv[0]
        if cand:
            return subprocess.CompletedProcess(argv, 1, "", "boom")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = (f"{stamp} [khipu-prompt-recall] session=s1 reason=ok ms=300.0 "
                f"hits=['episode:3'] legs=['lexical'] degraded=None")
        log = Path(env["KHIPU_CAPTURE_HOME"]) / "logs" / "prompt-recall.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(line + "\n")
        return subprocess.CompletedProcess(argv, 0, '{"hookSpecificOutput": {}}\n', "")

    monkeypatch.setattr(pm, "_run", fake_run)
    out = pm.gate_recall_replay(candidate)
    assert not out["ok"]
    assert "exit" in out["detail"]


def test_promote_refuses_withhold_where_live_routes_all(home, monkeypatch):
    candidate = _many_queries(home, 2)
    WITHHOLD = "skipped:error-InventoryUnavailable"

    def fake_run(argv, *, env=None, input=None, cwd=None, timeout=120):
        import subprocess
        if argv[1:2] == ["-c"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"push": True}) + "\n", "")
        on = env.get("KHIPU_FEATURE_ROUTING_PUSH") == "1"
        cand = "/candidate/packages/" in argv[0]
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = (f"{stamp} [khipu-prompt-recall] session=s1 reason=ok ms=300.0 "
                f"hits=['episode:3'] legs=['lexical'] degraded=None")
        if on:
            status = WITHHOLD if cand else "full"
            line += f" routing={status}"
        log = Path(env["KHIPU_CAPTURE_HOME"]) / "logs" / "prompt-recall.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(line + "\n")
        return subprocess.CompletedProcess(argv, 0, '{"hookSpecificOutput": {}}\n', "")

    monkeypatch.setattr(pm, "_run", fake_run)
    out = pm.gate_recall_replay(candidate)
    assert not out["ok"], out
    assert "withheld" in out["detail"].lower() or WITHHOLD in out["detail"]


def test_promote_passes_shared_timeout_and_shared_withhold(home, monkeypatch):
    """Load alone: live timed out / withheld the same way — not a refusal."""
    candidate = _one_query(home)

    def fake_run(argv, *, env=None, input=None, cwd=None, timeout=120):
        import subprocess
        if argv[1:2] == ["-c"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"push": True}) + "\n", "")
        on = env.get("KHIPU_FEATURE_ROUTING_PUSH") == "1"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = (f"{stamp} [khipu-prompt-recall] session=s1 reason=timeout>1.2s ms=1250.0 "
                f"hits=[] legs=['lexical'] degraded=None")
        if on:
            line += f" routing={pm.ROUTING_WITHHOLD}"
        log = Path(env["KHIPU_CAPTURE_HOME"]) / "logs" / "prompt-recall.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(line + "\n")
        return subprocess.CompletedProcess(argv, 0, '{"hookSpecificOutput": {}}\n', "")

    monkeypatch.setattr(pm, "_run", fake_run)
    out = pm.gate_recall_replay(candidate)
    assert out["ok"], out


def test_thin_outcomes_sample_is_yellow_not_hard_red(tmp_path, monkeypatch):
    log = tmp_path / "prompt-recall.log"
    now = datetime(2026, 9, 28, tzinfo=timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"{ts} [khipu-prompt-recall] session=s reason=ok ms=500.0 hits=[] legs=[] degraded=None"
        for _ in range(5)
    ]
    log.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(rp, "_log_path", lambda: log)
    out = rp.prompt_recall_outcomes(now=now)
    assert out["insufficient"] is True
    # Yellow: not a hard red that blocks a promote by load alone.
    assert out.get("level") == "amber" or (out["ok"] is True and out.get("quiet") is not True)
    assert out["ok"] is True or out.get("level") == "amber"


def test_outcomes_does_not_hard_red_on_live_timeouts_alone_under_shared_load(tmp_path, monkeypatch):
    """The live hook's own timeout>1.2s lines under load must not alone
    refuse a promote: they are yellow, not a hard red gate."""
    log = tmp_path / "prompt-recall.log"
    now = datetime(2026, 9, 28, tzinfo=timezone.utc)
    lines = []
    for i in range(25):
        ts = (now - timedelta(minutes=2 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # All timeouts: the live Mac under load.
        lines.append(
            f"{ts} [khipu-prompt-recall] session=s reason=timeout>1.2s ms=1200.0 "
            f"hits=[] legs=[] degraded=None"
        )
    log.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(rp, "_log_path", lambda: log)
    out = rp.prompt_recall_outcomes(now=now)
    # Either yellow, or still red only when failure_rate is structural —
    # timeouts-only under the budget wall are not a hard promote block.
    assert out.get("level") == "amber" or out["ok"] is True or out.get("timeout_only")


# ---- helpers pulled from test_promote without importing its fixtures twice ----


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KHIPU_TEST", "1")
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    (tmp_path / "data").mkdir()
    return tmp_path


def _one_query(home):
    from tests.test_promote import _one_query as real
    return real(home)


def _many_queries(home, n):
    from tests.test_promote import _many_queries as real
    return real(home, n)
