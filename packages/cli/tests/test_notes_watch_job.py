"""The notes-watch LaunchAgent keeps a runnable interpreter and stays startable.

2026-11-10: on the M5 the watcher's own watch-list refresh re-rendered its
plist from inside launchd, where ``shutil.which("python3")`` is Apple's
/usr/bin/python3 (3.9), and every later run failed. On the M1 the app was
replaced while the job (Program = the bundled Python) was loaded; launchd
recorded "Missing executable" (exit 78) and never started it again, while the
app's launch refresh skipped it because its plist was current and loaded.
"""
from __future__ import annotations

import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from khipu import jobs, launchd_gen
from khipu.doctor_summary import failing

LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
NOTES_LABEL = jobs.PLIST_NOTES_WATCH
CLI_DIR = Path(__file__).resolve().parents[1]

# `launchctl print` of the M1's notes-watch at 2026-11-10 17:50Z (trimmed).
M1_STUCK = """gui/501/com.khipu.notes-watch = {
	active count = 0
	path = /Users/u/Library/LaunchAgents/com.khipu.notes-watch.plist
	type = LaunchAgent
	state = spawn scheduled

	program = /Applications/Khipu.app/Contents/Resources/khipu/python/bin/python3.11
	runs = 27
	last exit code = 78: EX_CONFIG

	event channels = {
		"com.apple.launchd.helper" = {
			active = 0
		}
	}
	resource coalition = {
		state = active
	}
	run interval = 1800 seconds
	job state = spawn failed
	properties = penalty box | inferred program
}
"""
# The M5's at the same time: it starts, and every run exits 1.
M5_FAILING = """gui/501/com.khipu.notes-watch = {
	state = not running
	program = /usr/bin/python3
	runs = 5
	last exit code = 1
	spawn type = daemon (3)
	job state = exited
	properties = inferred program
}
"""
HEALTHY = """gui/501/com.khipu.notes-watch = {
	state = not running
	runs = 40
	last exit code = 0
	job state = exited
	properties = inferred program
}
"""


def _executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class _Home(type(Path())):
    """A home path that also carries the labels the fake launchctl loaded."""


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A Mac home under tmp: LaunchAgents, one memory folder, no launchd."""
    h = _Home(str(tmp_path / "home"))
    (h / ".claude" / "projects" / "-repo-a" / "memory").mkdir(parents=True)
    (h / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    for key in launchd_gen.PASSTHROUGH_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(jobs, "_launchagents_dir", lambda: h / "Library" / "LaunchAgents")
    monkeypatch.setattr(launchd_gen, "_launchagents_dir", lambda: h / "Library" / "LaunchAgents")
    monkeypatch.setattr(jobs, "LOG_DIR", h / "Library" / "Logs" / "Khipu")
    monkeypatch.setattr(jobs, "LOG_DIR_LEGACY", None)
    loads: list[str] = []
    monkeypatch.setattr(launchd_gen, "_launchctl_load", lambda label, path: loads.append(label) or {"ok": True})
    monkeypatch.setattr(launchd_gen, "read_versions", lambda: {})
    monkeypatch.setattr(launchd_gen, "write_versions", lambda v: None)
    monkeypatch.setattr(launchd_gen, "launchd_state", lambda label: {"loaded": True, **jobs.parse_launchctl_print(HEALTHY)})
    monkeypatch.setattr(launchd_gen, "_plist_loaded", lambda label: True)
    h.loads = loads
    return h


def _plist(home: Path) -> dict:
    return plistlib.loads((home / "Library" / "LaunchAgents" / f"{NOTES_LABEL}.plist").read_bytes())


def _interpreter(data: dict) -> str:
    args = data["ProgramArguments"]
    return args[4] if args[:2] == ["/bin/sh", "-c"] else args[0]


# ---- 1. the interpreter never depends on the caller's PATH -------------------------------------


def test_launchd_path_never_yields_apples_python(monkeypatch):
    """Under launchd's PATH the first python3 is /usr/bin/python3; it is never chosen."""
    monkeypatch.setenv("PATH", LAUNCHD_PATH)
    monkeypatch.setattr(launchd_gen, "_UNBUNDLED_PYTHONS", ())
    got = launchd_gen._unbundled_python()
    assert got != "/usr/bin/python3"
    assert got == sys.executable


def test_the_fixed_homebrew_path_wins_over_any_path(monkeypatch, tmp_path):
    brew = _executable(tmp_path / "opt" / "python3.11")
    elsewhere = _executable(tmp_path / "bin" / "python3.11")
    monkeypatch.setattr(launchd_gen, "_UNBUNDLED_PYTHONS", (str(brew),))
    for path in (LAUNCHD_PATH, f"{elsewhere.parent}:{LAUNCHD_PATH}"):
        monkeypatch.setenv("PATH", path)
        assert launchd_gen._unbundled_python() == str(brew)


def _fake_launchctl(bin_dir: Path, record: Path) -> None:
    # Never the real launchctl: print says "not loaded", the rest succeed.
    _executable(bin_dir / "launchctl", f"""#!/bin/sh
echo "$@" >> "{record}"
if [ "$1" = print ]; then echo "Could not find service" >&2; exit 113; fi
exit 0
""")


def _run_install_like_a_job(home: Path, tmp_path: Path, *extra: str) -> subprocess.CompletedProcess:
    """`khipu jobs install notes_watch` the way the watcher starts it: a child
    of the job's own interpreter (sys.executable) with launchd's environment."""
    fake_bin = tmp_path / "fakebin"
    _fake_launchctl(fake_bin, tmp_path / "launchctl.calls")
    env = {
        "HOME": str(home),
        # launchd's PATH; the fake launchctl first so nothing real is loaded.
        "PATH": f"{fake_bin}:{LAUNCHD_PATH}",
        "PYTHONPATH": os.environ.get("PYTHONPATH", str(CLI_DIR)),
        "PYTHONDONTWRITEBYTECODE": "1",
        "KHIPU_KEYCHAIN": "0",
        "XPC_SERVICE_NAME": NOTES_LABEL,
    }
    return subprocess.run(
        [sys.executable, "-m", "khipu", "jobs", "install", "notes_watch", *extra],
        env=env, cwd=str(home), capture_output=True, text=True, timeout=120,
    )


def test_a_refresh_under_launchds_environment_keeps_the_jobs_interpreter(home, tmp_path):
    """The live failure, end to end: the job is installed from a terminal, then
    its own refresh runs inside launchd's environment. The rewritten plist
    must name the same interpreter, never /usr/bin/python3."""
    first = launchd_gen.install_job("notes_watch")
    assert first["ok"], first
    installed = _interpreter(_plist(home))
    assert installed != "/usr/bin/python3"
    (home / ".claude" / "projects" / "-new" / "memory").mkdir(parents=True)

    r = _run_install_like_a_job(home, tmp_path, "--keep-program")
    assert r.returncode == 0, r.stdout + r.stderr
    data = _plist(home)
    assert _interpreter(data) == installed
    assert data["ProgramArguments"][:2] == ["/bin/sh", "-c"]
    assert str(home / ".claude" / "projects" / "-new" / "memory") in data["WatchPaths"]
    calls = (tmp_path / "launchctl.calls").read_text()
    assert "bootstrap" in calls  # reloaded, through the fake


def test_a_plain_install_under_launchds_environment_renders_what_a_terminal_does(home, tmp_path):
    """Even without --keep-program, launchd's PATH no longer changes the choice."""
    assert launchd_gen.install_job("notes_watch")["ok"]
    from_terminal = (home / "Library" / "LaunchAgents" / f"{NOTES_LABEL}.plist").read_bytes()
    r = _run_install_like_a_job(home, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _interpreter(_plist(home)) != "/usr/bin/python3"
    assert (home / "Library" / "LaunchAgents" / f"{NOTES_LABEL}.plist").read_bytes() == from_terminal


def test_the_watch_list_refresh_asks_to_keep_the_program(monkeypatch):
    seen = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            seen["argv"] = argv

    monkeypatch.setattr(launchd_gen.subprocess, "Popen", FakePopen)
    assert launchd_gen.refresh_watch_list()["ok"]
    assert seen["argv"][1:] == ["-m", "khipu", "jobs", "install", "notes_watch", "--keep-program"]


def test_keep_program_keeps_an_installed_interpreter_the_render_would_not_choose(home):
    assert launchd_gen.install_job("notes_watch", keep_program=False)["ok"]
    dest = home / "Library" / "LaunchAgents" / f"{NOTES_LABEL}.plist"
    data = plistlib.loads(dest.read_bytes())
    data["ProgramArguments"][4] = "/opt/elsewhere/python3.11"
    dest.write_bytes(plistlib.dumps(data))
    (home / ".claude" / "projects" / "-new" / "memory").mkdir(parents=True)

    assert launchd_gen.install_job("notes_watch", keep_program=True)["ok"]
    after = plistlib.loads(dest.read_bytes())
    assert after["ProgramArguments"][4] == "/opt/elsewhere/python3.11"
    assert str(home / ".claude" / "projects" / "-new" / "memory") in after["WatchPaths"]


def test_keep_program_keeps_an_older_direct_start_exactly(home):
    dest = home / "Library" / "LaunchAgents" / f"{NOTES_LABEL}.plist"
    legacy = ["/Applications/Khipu.app/Contents/Resources/khipu/python/bin/python3.11",
              "-m", "khipu", "notes", "reconcile", "--changed-only"]
    dest.write_bytes(plistlib.dumps({"Label": NOTES_LABEL, "ProgramArguments": legacy, "WatchPaths": []}))
    assert launchd_gen.install_job("notes_watch", keep_program=True)["ok"]
    after = plistlib.loads(dest.read_bytes())
    assert after["ProgramArguments"] == legacy
    assert after["WatchPaths"] == [str(home / ".claude" / "projects" / "-repo-a" / "memory")]


def test_keep_program_without_an_installed_plist_refuses(home):
    out = launchd_gen.install_job("notes_watch", keep_program=True)
    assert out["ok"] is False and out["error"] == "nothing_installed_to_keep"
    assert home.loads == []


# ---- 2. started like queue-drain: through /bin/sh -------------------------------------------------


def test_notes_watch_starts_through_sh_and_execs_the_interpreter(home):
    data = plistlib.loads(launchd_gen.render_plist("notes_watch"))
    args = data["ProgramArguments"]
    assert args[:4] == ["/bin/sh", "-c", 'exec "$@"', "khipu-notes-watch"]
    assert args[5:] == ["-m", "khipu", "notes", "reconcile", "--changed-only"]
    assert args[4] == str(launchd_gen._bundled_python())
    assert data["WatchPaths"] == [str(home / ".claude" / "projects" / "-repo-a" / "memory")]
    assert data["StartInterval"] == 1800 and data["ThrottleInterval"] == 30
    # the exec start really runs the interpreter with the arguments
    r = subprocess.run(["/bin/sh", "-c", args[2], args[3], sys.executable, "-c", "import sys; print(sys.argv[1:])", "x"],
                       capture_output=True, text=True, timeout=30)
    assert r.stdout.strip() == "['x']"


def test_the_sh_start_is_ours_and_current_after_install(home):
    assert launchd_gen.install_job("notes_watch")["ok"]
    assert launchd_gen.installed_interpreter("notes_watch") == str(launchd_gen._bundled_python())
    assert launchd_gen.plist_external("notes_watch") is False
    assert launchd_gen.plist_current("notes_watch") is True


def test_a_plist_naming_apples_python_is_ours_to_repair(home):
    dest = home / "Library" / "LaunchAgents" / f"{NOTES_LABEL}.plist"
    dest.write_bytes(plistlib.dumps({"Label": NOTES_LABEL, "ProgramArguments": [
        "/usr/bin/python3", "-m", "khipu", "notes", "reconcile", "--changed-only"]}))
    assert launchd_gen.plist_external("notes_watch") is False
    assert launchd_gen.plist_current("notes_watch") is False
    out = launchd_gen.refresh_scheduled_jobs(["notes_watch"])
    assert out["refreshed"] == ["notes_watch"]
    assert _interpreter(_plist(home)) == str(launchd_gen._bundled_python())


# ---- 3. a refresh reloads a job launchd cannot start ----------------------------------------------


def test_launchctl_print_is_read_for_spawn_failure_and_last_exit():
    stuck = jobs.parse_launchctl_print(M1_STUCK)
    assert stuck["spawn_failed"] is True and stuck["last_exit"] == 78 and stuck["runs"] == 27
    assert "penalty box" in stuck["properties"]
    failing_runs = jobs.parse_launchctl_print(M5_FAILING)
    assert failing_runs["spawn_failed"] is False and failing_runs["last_exit"] == 1
    fresh = jobs.parse_launchctl_print("job state = exited\nlast exit code = (never exited)\n")
    assert fresh["last_exit"] is None and fresh["spawn_failed"] is False


def test_refresh_reloads_a_current_loaded_job_that_cannot_spawn(home, monkeypatch):
    assert launchd_gen.install_job("notes_watch")["ok"]
    home.loads.clear()
    before = (home / "Library" / "LaunchAgents" / f"{NOTES_LABEL}.plist").read_bytes()
    out = launchd_gen.refresh_scheduled_jobs(["notes_watch"])
    assert out["current"] == ["notes_watch"] and home.loads == []

    monkeypatch.setattr(launchd_gen, "launchd_state", lambda label: {"loaded": True, **jobs.parse_launchctl_print(M1_STUCK)})
    out = launchd_gen.refresh_scheduled_jobs(["notes_watch"])
    assert out["refreshed"] == ["notes_watch"] and out["reloaded_unspawnable"] == ["notes_watch"]
    assert home.loads == [NOTES_LABEL]
    assert (home / "Library" / "LaunchAgents" / f"{NOTES_LABEL}.plist").read_bytes() == before


def test_ensure_reloads_a_current_loaded_job_that_cannot_spawn(home, monkeypatch):
    assert launchd_gen.install_job("notes_watch")["ok"]
    home.loads.clear()
    monkeypatch.setattr(launchd_gen, "launchd_state", lambda label: {"loaded": True, **jobs.parse_launchctl_print(M1_STUCK)})
    out = launchd_gen.ensure_scheduled_jobs(["notes_watch"])
    assert out["refreshed"] == ["notes_watch"]
    assert home.loads == [NOTES_LABEL]


# ---- 4. the doctor row: loaded is not enough --------------------------------------------------


def _health_with(monkeypatch, tmp_path, printed: str | None, err_line: str = "") -> dict:
    err = tmp_path / "notes-watch.err.log"
    err.write_text(f"older line\n{err_line}\n" if err_line else "")
    monkeypatch.setattr(jobs, "_job_entry", lambda name: {"plist_loaded": printed is not None,
                                                          "err_log_path": str(err)})
    monkeypatch.setattr(jobs, "launchd_state",
                        lambda label: {"loaded": True, **jobs.parse_launchctl_print(printed)} if printed else {"loaded": False})
    monkeypatch.setattr(launchd_gen, "watch_list_drift", lambda: {"installed": ["/m/a"], "unwatched": [], "stale": []})
    return jobs.notes_watch_health()


def test_doctor_is_red_when_launchd_cannot_start_the_watcher(monkeypatch, tmp_path):
    row = _health_with(monkeypatch, tmp_path, M1_STUCK)
    assert row["ok"] is False and row["state"] == "cannot_start"
    assert "spawn failed" in row["error"] and "78" in row["error"]
    assert row["fix"] == "run `khipu jobs install notes_watch`"
    got = failing({"nightly_steps": {"notes_reconcile_ok": row}, "nightly_steps_ok": False})
    assert any("notes_watch (launchd cannot start it)" in what for _a, what, _f in got), got


def test_doctor_is_red_when_the_last_run_failed_and_quotes_the_error(monkeypatch, tmp_path):
    row = _health_with(monkeypatch, tmp_path, M5_FAILING, "error: No module named 'psycopg'")
    assert row["ok"] is False and row["state"] == "last_run_failed"
    assert "exit 1" in row["error"] and "No module named 'psycopg'" in row["error"]
    got = failing({"nightly_steps": {"notes_reconcile_ok": row}, "nightly_steps_ok": False})
    assert any("notes_watch (last run failed)" in what for _a, what, _f in got), got


def test_doctor_is_green_when_loaded_startable_and_the_last_run_worked(monkeypatch, tmp_path):
    assert _health_with(monkeypatch, tmp_path, HEALTHY)["ok"] is True
    never_ran = "job state = exited\nlast exit code = (never exited)\n"
    assert _health_with(monkeypatch, tmp_path, never_ran)["ok"] is True


def test_doctor_still_says_not_installed_when_it_is_not_loaded(monkeypatch, tmp_path):
    row = _health_with(monkeypatch, tmp_path, None)
    assert row["ok"] is False and row["state"] == "not_installed" and "not installed" in row["error"]


RUNNING_AFTER_78 = """gui/501/com.khipu.notes-watch = {
\tstate = running
\tprogram = /bin/sh
\truns = 28
\tpid = 4242
\tlast exit code = 78: EX_CONFIG
\tjob state = running
\tproperties = inferred program
}
"""


def test_keep_program_does_not_keep_apples_python(home):
    """The plist the 2026-11-10 bug wrote is replaced, not kept, by the watcher's own refresh."""
    dest = home / "Library" / "LaunchAgents" / f"{NOTES_LABEL}.plist"
    broken = ["/usr/bin/python3", "-m", "khipu", "notes", "reconcile", "--changed-only"]
    dest.write_bytes(plistlib.dumps({"Label": NOTES_LABEL, "ProgramArguments": broken, "WatchPaths": []}))
    assert launchd_gen.install_job("notes_watch", keep_program=True)["ok"]
    after = plistlib.loads(dest.read_bytes())
    assert _interpreter(after) != "/usr/bin/python3"
    assert after["ProgramArguments"][:2] == ["/bin/sh", "-c"]


def test_a_running_job_with_an_old_exit_78_is_not_unspawnable():
    got = jobs.parse_launchctl_print(RUNNING_AFTER_78)
    assert got["job_state"] == "running" and got["last_exit"] == 78
    assert got["spawn_failed"] is False
    assert jobs.parse_launchctl_print(M1_STUCK)["spawn_failed"] is True


def test_refresh_leaves_a_running_job_with_an_old_78_alone(home, monkeypatch):
    assert launchd_gen.install_job("notes_watch")["ok"]
    home.loads.clear()
    monkeypatch.setattr(launchd_gen, "launchd_state",
                        lambda label: {"loaded": True, **jobs.parse_launchctl_print(RUNNING_AFTER_78)})
    out = launchd_gen.refresh_scheduled_jobs(["notes_watch"])
    assert home.loads == []
    assert out["current"] == ["notes_watch"]


def test_ensure_leaves_a_running_job_with_an_old_78_alone(home, monkeypatch):
    assert launchd_gen.install_job("notes_watch")["ok"]
    home.loads.clear()
    monkeypatch.setattr(launchd_gen, "launchd_state",
                        lambda label: {"loaded": True, **jobs.parse_launchctl_print(RUNNING_AFTER_78)})
    launchd_gen.ensure_scheduled_jobs(["notes_watch"])
    assert home.loads == []


def test_doctor_does_not_say_cannot_start_while_it_is_running(monkeypatch, tmp_path):
    row = _health_with(monkeypatch, tmp_path, RUNNING_AFTER_78)
    assert row.get("state") != "cannot_start"


def test_doctor_is_red_when_the_launchd_state_read_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "_job_entry", lambda name: {"plist_loaded": True, "err_log_path": str(tmp_path / "e.log")})
    monkeypatch.setattr(jobs, "launchd_state",
                        lambda label: {"loaded": False, "error": "TimeoutExpired: launchctl print timed out"})
    monkeypatch.setattr(launchd_gen, "watch_list_drift", lambda: {"installed": [], "unwatched": [], "stale": []})
    row = jobs.notes_watch_health()
    assert row["ok"] is False
    assert row["state"] == "check_failed"
