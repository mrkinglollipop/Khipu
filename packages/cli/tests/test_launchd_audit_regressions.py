"""Synthetic launchctl regressions for promotion audit findings 4 and 5."""
import plistlib
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from khipu import jobs, launchd_gen as ld


@pytest.fixture
def installed(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(ld, "_launchagents_dir", lambda: agents)
    monkeypatch.setattr(ld, "_plist_path", lambda label: agents / (label + ".plist"))
    monkeypatch.setattr(ld, "read_versions", lambda: {})
    writes = []
    monkeypatch.setattr(ld, "write_versions", lambda data: writes.append(data))
    return writes


def fake_launchctl(monkeypatch, dest, *, previous=None, fail_new=False, fail_old=False):
    state = {"loaded": previous is not None, "calls": [], "fail_new": fail_new}

    def run(argv, **kwargs):
        assert argv[0] == "launchctl"
        action = argv[1]
        state["calls"].append(action)
        if action == "print":
            return CompletedProcess(argv, 0 if state["loaded"] else 113, "", "Could not find service")
        if action == "bootout":
            state["loaded"] = False
            return CompletedProcess(argv, 0, "", "")
        assert action == "bootstrap"
        old = previous is not None and dest.read_bytes() == previous
        failed = fail_old if old else state["fail_new"]
        state["loaded"] = not failed
        return CompletedProcess(argv, 5 if failed else 0, "", "synthetic bootstrap failed" if failed else "")

    monkeypatch.setattr(ld.subprocess, "run", run)
    return state


@pytest.mark.parametrize("restore_load_fails", [False, True])
def test_failed_replacement_restores_previous_plist_and_refresh_retries(installed, monkeypatch, restore_load_fails):
    dest = ld._plist_path(ld._LABELS["recall_daemon"])
    previous = plistlib.dumps({"Label": ld._LABELS["recall_daemon"],
                               "ProgramArguments": ["/old/Khipu.app/Contents/python"]})
    dest.write_bytes(previous)
    state = fake_launchctl(monkeypatch, dest, previous=previous, fail_new=True, fail_old=restore_load_fails)

    out = ld.install_job("recall_daemon")
    assert out["ok"] is False
    assert dest.read_bytes() == previous
    assert state["loaded"] is (not restore_load_fails)
    assert state["calls"] == ["bootout", "bootstrap", "bootout", "bootstrap"]
    assert out["recovery"]["ok"] is (not restore_load_fails)
    assert "synthetic bootstrap failed" in out["error"]
    assert installed == []  # Failed replacement never publishes installation success.

    state["fail_new"] = False
    refreshed = ld.refresh_scheduled_jobs(["recall_daemon"])
    assert refreshed["ok"] is True
    assert refreshed["refreshed"] == ["recall_daemon"]
    assert state["loaded"] is True
    assert dest.read_bytes() == ld.render_plist("recall_daemon")


@pytest.mark.parametrize("refresh", [ld.refresh_scheduled_jobs, ld.ensure_scheduled_jobs])
@pytest.mark.parametrize("fail_load", [False, True])
def test_matching_unloaded_plist_must_be_reloaded(installed, monkeypatch, refresh, fail_load):
    dest = ld._plist_path(ld._LABELS["recall_daemon"])
    dest.write_bytes(ld.render_plist("recall_daemon"))
    state = fake_launchctl(monkeypatch, dest, fail_new=fail_load)
    out = refresh(["recall_daemon"])
    assert out["ok"] is (not fail_load)
    assert out["current"] == []
    assert out["refreshed"] == ([] if fail_load else ["recall_daemon"])
    assert "print" in state["calls"]
    assert "bootstrap" in state["calls"]
    assert state["loaded"] is (not fail_load)


def test_loaded_matching_plist_stays_current_without_replacement(installed, monkeypatch):
    dest = ld._plist_path(ld._LABELS["recall_daemon"])
    previous = ld.render_plist("recall_daemon")
    dest.write_bytes(previous)
    state = fake_launchctl(monkeypatch, dest, previous=previous)
    out = ld.refresh_scheduled_jobs(["recall_daemon"])
    assert out["ok"] and out["current"] == ["recall_daemon"]
    assert state["calls"] == ["print"]


@pytest.mark.parametrize("job", ["nightly", "monthly"])
@pytest.mark.parametrize("path_kind", ["tilde", "relative"])
def test_validated_driver_is_absolute_and_runs_from_plist_working_directory(installed, monkeypatch, tmp_path, job, path_kind):
    driver = Path.home() / (job + "_driver.py")
    driver.write_text("raise SystemExit(0)\n")
    monkeypatch.chdir(Path.home())
    raw = "~/" + driver.name if path_kind == "tilde" else driver.name
    env_key = ld._REQUIRED_DRIVER_ENV[job]
    monkeypatch.setattr(ld, "_launchctl_load", lambda *args: {"ok": True})
    out = ld.install_job(job, environ={env_key: raw})
    assert out["ok"]
    data = plistlib.loads(Path(out["path"]).read_bytes())
    rendered = data["EnvironmentVariables"][env_key]
    assert rendered == str(driver.absolute())
    monkeypatch.setenv(env_key, rendered)
    monkeypatch.chdir(data["WorkingDirectory"])
    runtime = jobs._env_script_path(env_key)
    writes = []
    monkeypatch.setattr(jobs, "_write_job_state", lambda *args: writes.append(args))
    monkeypatch.setattr(jobs, "_log_paths", lambda stem: (tmp_path / "out.log", tmp_path / "err.log"))
    assert jobs._run_script(runtime, log_stem="test", state_name=job) == 0
    assert writes == [(job, 0)]
