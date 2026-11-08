"""Storage bounds: nothing Khipu keeps grows without a limit, and every limit
runs and shows in doctor. Hermetic: throwaway HOME, data and capture folders."""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path
from unittest import mock

import pytest

from khipu import jobs, logcap, session_capture, storage, storage_bounds

DAY = 86400


@pytest.fixture
def homes(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(("KHIPU_", "ALZY_")) and name != "KHIPU_KEYCHAIN":
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_OUTBOX", str(tmp_path / "outbox"))
    monkeypatch.setenv("KHIPU_TEST", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "data").mkdir()
    (tmp_path / "capture" / "state").mkdir(parents=True)
    return tmp_path


def _file(path: Path, age_days: float, text: str = "{}") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    then = time.time() - age_days * DAY
    os.utime(path, (then, then))
    return path


def _state(homes: Path) -> Path:
    return homes / "capture" / "state"


# ---- 1. logs are capped where they are written -------------------------------


def test_a_log_over_its_cap_is_rotated_by_its_writer_and_the_copy_is_kept(tmp_path):
    log = tmp_path / "stop-hook.log"
    log.write_bytes(b"x" * 3000)
    assert logcap.rotate_if_over(log, cap_bytes=1000) is True
    assert log.stat().st_size == 0
    assert (tmp_path / "stop-hook.log.1").read_bytes() == b"x" * 3000


def test_a_small_log_costs_one_stat_and_no_config_read(tmp_path):
    log = tmp_path / "a.log"
    log.write_bytes(b"x" * 100)
    with mock.patch.object(logcap, "_configured_cap", side_effect=AssertionError("read the config")), \
            mock.patch("shutil.copyfile", side_effect=AssertionError("copied")):
        assert logcap.rotate_if_over(log) is False


def test_the_config_is_read_once_per_process_and_a_cap_below_the_default_is_honoured(tmp_path):
    logcap.reset_cache()
    log = tmp_path / "a.log"
    log.write_bytes(b"x" * 200_000)
    reads = []
    with mock.patch("khipu.config.load_config", side_effect=lambda: reads.append(1) or {"log_max_mb": 0.1}):
        assert logcap.rotate_if_over(log) is True      # 200 KB is over the 0.1 MB the owner set
        log.write_bytes(b"y" * 200_000)
        assert logcap.rotate_if_over(log) is True
        log.write_bytes(b"z" * 10)
        assert logcap.rotate_if_over(log) is False
    assert len(reads) == 1
    logcap.reset_cache()


def test_a_log_under_the_configured_cap_is_left_alone_and_zero_means_no_cap(tmp_path):
    log = tmp_path / "a.log"
    log.write_bytes(b"x" * 200_000)
    for config, expected in (({"log_max_mb": 1}, False), ({"log_max_mb": 0}, False), ({}, False)):
        logcap.reset_cache()
        with mock.patch("khipu.config.load_config", return_value=config):
            assert logcap.rotate_if_over(log) is expected
    logcap.reset_cache()


def test_a_symlinked_log_is_never_rotated(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_bytes(b"x" * 3000)
    link = tmp_path / "a.log"
    link.symlink_to(target)
    assert logcap.rotate_if_over(link, cap_bytes=10) is False
    assert target.read_bytes() == b"x" * 3000


def test_the_capture_log_writer_rotates_without_the_sweep(homes, monkeypatch):
    monkeypatch.setattr(logcap, "FLOOR_BYTES", 100)
    monkeypatch.setattr(logcap, "_configured_cap", lambda: 100)
    log = session_capture.log_path()
    log.parent.mkdir(parents=True)
    log.write_bytes(b"y" * 500)
    session_capture._log("hello")
    assert log.read_text().count("\n") == 1 and "hello" in log.read_text()
    assert log.with_name("session-capture.log.1").read_bytes() == b"y" * 500


def test_the_stop_hook_caps_its_own_stderr_log_on_start():
    text = (Path(__file__).resolve().parents[1] / "bin" / "khipu-stop-hook").read_text()
    assert "KHIPU_HOOK_LOG" in text and "logcap.rotate_if_over" in text
    assert text.index("logcap.rotate_if_over") < text.index("hook_main(")


def test_the_nightly_runs_the_storage_sweep(homes, monkeypatch):
    seen = []
    monkeypatch.setattr(storage, "sweep_if_due", lambda: seen.append(1) or {
        "ok": True, "removed": ["a"], "rotated": ["b.log"], "freed_bytes": 5, "errors": []})
    out = jobs._storage_sweep()
    assert seen == [1] and out["ok"] and out["rotated"] == 1 and out["removed"] == 1


def test_the_nightly_reuses_a_sweep_the_replica_job_already_ran_today(homes):
    storage.sweep_if_due()
    out = jobs._storage_sweep()
    assert out["ok"] is True and out["reused_todays_sweep"] is True


def test_the_nightly_step_is_red_when_the_sweep_had_errors(homes, monkeypatch):
    monkeypatch.setattr(storage, "sweep_if_due", lambda: {"ok": False, "removed": [], "rotated": [],
                                                           "freed_bytes": 0, "errors": ["logs: boom"]})
    step = jobs._step_result(jobs._storage_sweep())
    assert step["ok"] is False and "boom" in step["error"]


# ---- 2. per-session debris ----------------------------------------------------


def test_old_idle_session_files_go_and_recent_ones_stay(homes):
    st = _state(homes)
    old = [_file(st / "claude_code--old1.json", 40), _file(st / "recall--old1.json", 40),
           _file(st / "recall--old1.lock", 40, ""), _file(st / "claude_code--old2.capture-now.json", 40),
           _file(st / "title-cache--abc123.json", 40)]
    young = [_file(st / "claude_code--new.json", 1), _file(st / "recall--new.lock", 1, ""),
             _file(st / "title-cache--def456.json", 1)]
    out = storage_bounds.prune_session_state(keep_days=30)
    assert not any(p.exists() for p in old)
    assert all(p.exists() for p in young)
    assert out["removed"] == 5 and out["ok"] is True


def test_a_held_lock_means_the_session_is_active_and_nothing_of_it_goes(homes):
    st = _state(homes)
    data, lock = _file(st / "recall--live.json", 90), _file(st / "recall--live.lock", 90, "")
    with lock.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        out = storage_bounds.prune_session_state(keep_days=30)
    assert data.exists() and lock.exists()
    assert out["busy"] == 1 and out["removed"] == 0
    assert storage_bounds.prune_session_state(keep_days=30)["removed"] == 2


def test_a_session_seen_recently_keeps_its_state_even_with_an_old_file_time(homes):
    st = _state(homes)
    seen = _file(st / "codex--live.json", 90, json.dumps({"seen_ts": time.time() - DAY}))
    assert storage_bounds.prune_session_state(keep_days=30)["removed"] == 0
    assert seen.exists()


def test_a_fresh_directory_lock_keeps_its_session_and_a_stale_one_is_removed(homes):
    st = _state(homes)
    _file(st / "cursor--a.json", 90)
    fresh = st / "cursor--a.lock"
    fresh.mkdir()
    out = storage_bounds.prune_session_state(keep_days=30)
    assert out["removed"] == 0 and fresh.exists() and (st / "cursor--a.json").exists()
    os.utime(fresh, (time.time() - 40 * DAY,) * 2)
    out = storage_bounds.prune_session_state(keep_days=30)
    assert not fresh.exists() and not (st / "cursor--a.json").exists()


def test_a_routing_ledger_and_its_lock_are_never_removed_here(homes):
    # routing_ledger_prune keeps lock files for good: two hooks could otherwise
    # hold different inodes as "the" lock. Not even after the ledger is gone.
    st = _state(homes)
    ledger, lock = _file(st / "recall--r.routing.json", 90), _file(st / "recall--r.routing.lock", 90, "")
    storage_bounds.prune_session_state(keep_days=30)
    assert ledger.exists() and lock.exists()
    ledger.unlink()
    storage_bounds.prune_session_state(keep_days=30)
    assert lock.exists()


def test_a_save_state_tmp_orphan_goes_with_the_same_age_and_lock_rules(homes):
    st = _state(homes)
    old = _file(st / "claude_code--o.tmp", 90, "{")
    young = _file(st / "claude_code--y.tmp", 1, "{")
    paired_json, paired_tmp = _file(st / "codex--p.json", 90), _file(st / "codex--p.tmp", 1, "{")
    out = storage_bounds.prune_session_state(keep_days=30)
    assert not old.exists() and young.exists()
    assert paired_json.exists() and paired_tmp.exists()  # a newer rewrite keeps the session
    assert out["removed"] == 1
    with (st / "recall--h.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        orphan = _file(st / "recall--h.tmp", 90, "{")
        _file(st / "recall--h.lock", 90, "")
        os.utime(st / "recall--h.lock", (time.time() - 90 * DAY,) * 2)
        storage_bounds.prune_session_state(keep_days=30)
        assert orphan.exists()  # the session is active


def test_the_dispatch_and_other_folders_keep_their_files(homes):
    st = _state(homes)
    other = _file(st / "claude_code--x.capture-now.tmp", 90, "{")
    storage_bounds.prune_session_state(keep_days=30)
    assert other.exists()


def test_other_files_symlinks_and_a_zero_window_are_left_alone(homes, tmp_path):
    st = _state(homes)
    others = [_file(st / n, 400) for n in ("t3-pin--x.json", "synth-usage.jsonl", "login-homes.swept",
                                           "title-pending--q.json", "stranger--s.json", ".recall--hidden.json")]
    victim = _file(tmp_path / "outside.json", 400)
    (st / "recall--link.json").symlink_to(victim)
    storage_bounds.prune_session_state(keep_days=30)
    assert all(p.exists() for p in others) and victim.exists() and (st / "recall--link.json").is_symlink()
    old = _file(st / "recall--zero.json", 400)
    assert storage_bounds.prune_session_state(keep_days=0)["disabled"] is True and old.exists()


def test_a_missing_state_folder_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "nowhere"))
    out = storage_bounds.prune_session_state(keep_days=30)
    assert out["ok"] is True and out["removed"] == 0


def test_the_prune_stops_at_its_time_budget_and_says_so(homes):
    st = _state(homes)
    for n in range(5):
        _file(st / f"recall--s{n}.json", 90)
    out = storage_bounds.prune_session_state(keep_days=30, budget_s=-1)
    assert out["time_budget_exhausted"] is True and "deferred" in out
    assert storage_bounds.prune_session_state(keep_days=30)["removed"] == 5


# ---- 4. archives and held work -------------------------------------------------


def test_the_empty_archive_and_outbox_quarantine_age_out_but_resolved_and_dead_stay(homes):
    from khipu import outbox

    empty, quarantine = session_capture.empty_dir(), outbox.quarantine_dir()
    old = [_file(empty / "job1.json", 120), _file(empty / "job1.error", 120),
           _file(quarantine / "q1.json", 120), _file(quarantine / "q1.error", 120)]
    kept = [_file(outbox.resolved_dir() / "r1.json", 400), _file(outbox.dead_dir() / "d1.json", 400),
            _file(empty / "notes.txt", 400)]
    # Age runs from the later of modify and change time, and a change time cannot
    # be backdated: look at the folders from 100 days on.
    out = storage_bounds.prune_archives(keep_days=90, now=time.time() + 100 * DAY)
    assert not any(p.exists() for p in old)
    assert all(p.exists() for p in kept)
    assert out["removed"] == 4 and out["ok"] is True


def test_an_old_job_renamed_into_the_archive_today_keeps_its_retention_from_today(homes, tmp_path):
    from khipu import outbox

    quarantine = outbox.quarantine_dir()
    quarantine.mkdir(parents=True)
    empty = session_capture.empty_dir()
    empty.mkdir(parents=True)
    for folder, name in ((quarantine, "q-old.json"), (empty, "e-old.json")):
        elsewhere = _file(tmp_path / "queue" / name, 120)  # modified four months ago
        os.replace(elsewhere, folder / name)               # archived just now: a new change time
    out = storage_bounds.prune_archives(keep_days=90)
    assert (quarantine / "q-old.json").exists() and (empty / "e-old.json").exists()
    assert out["removed"] == 0
    for folder, name in ((quarantine, "q-old.json"), (empty, "e-old.json")):
        assert max((folder / name).stat().st_mtime, (folder / name).stat().st_ctime) > time.time() - DAY


def test_held_work_is_counted_and_warned_about_past_the_threshold(homes):
    from khipu import outbox

    for n in range(3):
        _file(outbox.dead_dir() / f"d{n}.json", 1)
    counts = storage_bounds.held_counts()
    assert counts["outbox_dead"] == 3
    assert storage_bounds.warnings(counts, threshold=5) == []
    text = storage_bounds.warnings(counts, threshold=2)[0]
    assert "3 jobs are held in the outbox's dead folder" in text and "never removed" in text
    for n in range(3):
        assert (outbox.dead_dir() / f"d{n}.json").exists()


# ---- 8. the sweep runs them, and doctor sees sizes ------------------------------


def test_the_sweep_runs_the_bounds_and_records_them(homes):
    st = _state(homes)
    old = _file(st / "recall--gone.json", 90)
    kept = _file(st / "recall--kept.json", 1)
    empty_old = _file(session_capture.empty_dir() / "j.json", 200)
    out = storage.sweep()
    assert not old.exists() and kept.exists() and empty_old.exists()  # archives age from their change time
    assert out["bounds"]["session_state"]["removed"] == 1 and out["bounds"]["archives"]["removed"] == 0


def test_the_settings_are_listed_with_their_defaults():
    got = {k: v[1] for k, v in storage.SETTINGS.items()}
    assert got["session_state_keep_days"] == 30 and got["archive_keep_days"] == 90
    assert got["receipt_keep_days"] == 90


def test_usage_shows_outbox_queue_archive_state_and_jsonl_logs(homes):
    from khipu import outbox

    _file(outbox.dead_dir() / "d.json", 1, "x" * 200)
    _file(outbox.resolved_dir() / "r.json", 1, "x" * 300)
    _file(session_capture.queue_dir() / "q.json", 1, "x" * 400)
    _file(session_capture.empty_dir() / "e.json", 1, "x" * 500)
    _file(_state(homes) / "recall--s.json", 1, "x" * 600)
    _file(homes / "data" / "owner-actions.jsonl", 1, "x" * 700)
    _file(homes / "data" / "owner-actions.000001.jsonl", 1, "x" * 800)
    out = storage.usage()
    by = {p["name"]: p for p in out["places"]}
    assert by["outbox"]["bytes"] == 500 and by["outbox"]["parts"]["dead"] == 200
    assert by["capture queue"]["bytes"] == 400 and by["capture archive"]["bytes"] == 500
    assert by["session state"]["bytes"] == 600 and by["jsonl logs"]["bytes"] == 1500
    assert out["total_bytes"] == sum(p["bytes"] for p in out["places"])
    assert "outbox 500 B" in out["line"]


def test_doctor_gets_a_row_for_every_new_nightly_step():
    keys = set(jobs._NIGHTLY_STEP_CHECKS)
    assert {"storage_sweep_ok", "fact_changes_prune_ok",
            "receipt_prune_ok", "routing_ledger_prune_ok"} <= keys


def test_the_nightly_records_each_new_step(homes, monkeypatch):
    names = []
    monkeypatch.setattr(jobs, "_record_nightly_step", lambda steps, name, **kw: names.append(name))
    for fn in ("_drain_sessions", "_reconcile_notes_if_due", "_embed_backfill", "_summary_kind_backfill",
               "_prune_query_cache", "_prune_routing_ledgers", "_storage_sweep",
               "_commitment_close_rules", "_fact_changes_prune", "_mark_stale_commitments",
               "_provenance_nightly", "_ledger_nightly", "_receipt_prune", "_project_renames_sweep",
               "_recall_eval_step", "_refresh_snapshot", "_briefs_build_if_on", "_capture_previous_briefs_step"):
        monkeypatch.setattr(jobs, fn, lambda *a, **k: {"ok": True})
    monkeypatch.setattr(jobs, "_run_script", lambda *a, **k: 0)
    monkeypatch.setattr(jobs, "_write_job_state", lambda *a, **k: None)
    jobs._run_nightly_locked()
    for step in ("storage_sweep", "fact_changes_prune", "receipt_prune"):
        assert step in names
    assert names.index("fact_changes_prune") > names.index("provenance_backfill")


def test_fact_changes_prune_is_its_own_step_and_survives_a_close_rules_failure(homes, monkeypatch):
    from khipu import db, provenance

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(db, "connect", lambda *a, **k: Conn())
    monkeypatch.setattr(provenance, "prune_changes", lambda conn: {"ok": True, "pruned": 4})
    out = jobs._fact_changes_prune()
    assert out == {"ok": True, "pruned": 4}
    monkeypatch.setattr(provenance, "prune_changes", mock.Mock(side_effect=RuntimeError("boom")))
    bad = jobs._step_result(jobs._fact_changes_prune())
    assert bad["ok"] is False and "boom" in bad["error"]


@pytest.mark.parametrize("receipt, red", [
    ({"ok": False, "error": "synthetic cleanup failure"}, True),
    ({"ok": True, "pruned": 3}, False),
])
def test_a_failing_fact_changes_prune_is_reported_red_by_its_own_step(homes, monkeypatch, receipt, red):
    from khipu import db, provenance

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(db, "connect", lambda *a, **k: Conn())
    monkeypatch.setattr(provenance, "prune_changes", lambda conn: dict(receipt))
    assert jobs._step_result(jobs._fact_changes_prune())["ok"] is not red


def test_owner_writes_that_waited_too_long_or_were_refused_show_in_doctor_in_plain_words(homes):
    import json as _json

    from khipu import cli, owner_writes

    assert owner_writes.doctor_warnings() == []
    refused = owner_writes._refused_dir()
    refused.mkdir(parents=True)
    stale = owner_writes._TOO_OLD % owner_writes.REPLAY_HORIZON_DAYS
    for n in range(2):
        (refused / f"{n}.json").write_text(_json.dumps({"error": stale}))
    (refused / "x.json").write_text(_json.dumps({"error": "ValueError: no such item"}))
    got = owner_writes.doctor_warnings()
    assert got[0] == "2 owner writes waited over 60 days and were not sent; check them."
    assert got[1] == "1 owner write the hub refused is kept for you to check."
    assert cli._owner_writes_warnings() == got


def test_nothing_in_the_new_modules_names_a_private_folder():
    root = Path(__file__).resolve().parents[1] / "khipu"
    for name in ("storage_bounds.py", "logcap.py", "receipt_prune.py"):
        text = (root / name).read_text()
        assert "API Keys" not in text and "My Portfolio" not in text
