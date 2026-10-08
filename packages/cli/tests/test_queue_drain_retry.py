"""Execute the rendered POSIX launcher; never load a real LaunchAgent."""
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from khipu import launchd_gen


_STARTUP = (
    "Python path configuration:\n"
    "Fatal Python error: init_fs_encoding: failed to get the Python codec\n"
    '  File "<frozen importlib._bootstrap_external>", line 1662, in _fill_cache\n'
    "InterruptedError: [Errno 4] Interrupted system call: '/example/cli'\n"
)


def _run_fake(tmp_path, mode):
    # Spaces and shell metacharacters must remain literal argv values.
    python = tmp_path / "fake python & interpreter"
    python.write_text("""#!/bin/sh
count_file="$DRAIN_TEST_DIR/count"
count=0
if [ -f "$count_file" ]; then count=$(cat "$count_file"); fi
count=$((count + 1))
printf '%s' "$count" >"$count_file"
if [ "$1" != '-c' ] || [ "$3" != sessions ] || [ "$4" != drain ]; then exit 99; fi
if [ "$DRAIN_TEST_MODE" = recover ] && [ "$count" -ge 2 ]; then
    printf 'drained\\n'
    exit 0
fi
if [ "$DRAIN_TEST_MODE" = runtime ]; then
    printf '%s\\n' '[khipu-queue-drain] entered Khipu' >&2
elif [ "$DRAIN_TEST_MODE" = other ]; then
    printf 'ModuleNotFoundError: missing dependency\\n' >&2
    exit 23
elif [ "$DRAIN_TEST_MODE" = bare_eintr ]; then
    printf 'InterruptedError: [Errno 4] Interrupted system call\\n' >&2
    exit 24
fi
cat "$DRAIN_TEST_DIR/startup" >&2
printf 'failed attempt %s\\n' "$count"
exit 17
""")
    python.chmod(0o700)
    (tmp_path / "startup").write_text(_STARTUP)
    sleep = tmp_path / "sleep"
    sleep.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >>"$DRAIN_TEST_DIR/backoffs"\n')
    sleep.chmod(0o700)
    env = dict(os.environ, DRAIN_TEST_DIR=str(tmp_path), DRAIN_TEST_MODE=mode,
               TMPDIR=str(tmp_path), PATH=f"{tmp_path}:/usr/bin:/bin")
    result = subprocess.run(launchd_gen._queue_drain_arguments(str(python)),
                            env=env, capture_output=True, text=True, timeout=10)
    count = int((tmp_path / "count").read_text())
    backoffs = tmp_path / "backoffs"
    delays = backoffs.read_text().splitlines() if backoffs.exists() else []
    assert list(tmp_path.glob("khipu-drain.*")) == []
    return result, count, delays


def test_generator_emits_inline_wrapper_and_keeps_bytecode_outside_bundle(monkeypatch):
    monkeypatch.setattr(launchd_gen, "_bundled_python", lambda: Path("/example/python"))
    data = plistlib.loads(launchd_gen.render_plist("queue_drain"))
    args = data["ProgramArguments"]
    assert args == launchd_gen._queue_drain_arguments("/example/python")
    assert args[-2:] == ["sessions", "drain"]
    assert data["StartInterval"] == 300
    assert "Caches/Khipu/pycache" in data["EnvironmentVariables"]["PYTHONPYCACHEPREFIX"]


@pytest.mark.parametrize("mode,exit_code,attempts,delays", [
    ("recover", 0, 2, ["1"]),
    ("exhaust", 17, 3, ["1", "2"]),
    ("runtime", 17, 1, []),
    ("other", 23, 1, []),
    ("bare_eintr", 24, 1, []),
])
def test_wrapper_retries_only_pre_entry_startup_eintr(tmp_path, mode, exit_code, attempts, delays):
    result, count, actual_delays = _run_fake(tmp_path, mode)
    assert result.returncode == exit_code, result.stderr
    assert count == attempts
    assert actual_delays == delays
    assert result.stderr.count(launchd_gen._DRAIN_ATTEMPT) == attempts
    if mode == "recover":
        assert result.stdout == "failed attempt 1\ndrained\n"
        assert result.stderr.count(launchd_gen._STARTUP_EINTR) == 1
    elif mode == "exhaust":
        assert result.stderr.count(launchd_gen._STARTUP_EINTR) == 3


@pytest.mark.parametrize("fail_in_init", [False, True])
def test_real_python_shim_marks_after_the_package_import(tmp_path, fail_in_init):
    package = tmp_path / "khipu"
    package.mkdir()
    failure = "raise InterruptedError(4, 'Interrupted system call')\n"
    (package / "__init__.py").write_text(failure if fail_in_init else "")
    (package / "__main__.py").write_text(
        "import sys\nassert sys.argv[1:] == ['sessions', 'drain']\n" + failure)
    env = dict(os.environ, PYTHONPATH=str(tmp_path), TMPDIR=str(tmp_path),
               PYTHONPYCACHEPREFIX=str(tmp_path / "pyc"))
    result = subprocess.run(launchd_gen._queue_drain_arguments(sys.executable),
                            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 1
    assert "InterruptedError: [Errno 4]" in result.stderr
    if fail_in_init:
        # Importing the package is still startup, so no boundary marker: a
        # real EINTR there (raised inside importlib, so its frames show) is
        # retried by the shell rules the fake-interpreter tests cover.
        assert launchd_gen._DRAIN_STARTED not in result.stderr
    else:
        assert result.stderr.count(launchd_gen._DRAIN_ATTEMPT) == 1
        assert result.stderr.index(launchd_gen._DRAIN_STARTED) < result.stderr.index("Traceback")


@pytest.mark.parametrize("python,external", [
    ("/example/current-python", False),
    ("/example/maintainer-python", True),
    ("/Applications/Khipu.app/Contents/Resources/python", False),
])
def test_wrapper_ownership_uses_child_interpreter(tmp_path, monkeypatch, python, external):
    dest = tmp_path / "queue.plist"
    dest.write_bytes(plistlib.dumps({"ProgramArguments": launchd_gen._queue_drain_arguments(python)}))
    monkeypatch.setattr(launchd_gen, "_plist_path", lambda label: dest)
    monkeypatch.setattr(launchd_gen, "_bundled_python", lambda: Path("/example/current-python"))
    assert launchd_gen.plist_external("queue_drain") is external


def _health_log(tmp_path, monkeypatch):
    dest = tmp_path / "queue.plist"
    log = tmp_path / "actual.err.log"
    dest.write_bytes(plistlib.dumps({"StandardErrorPath": str(log)}))
    monkeypatch.setattr(launchd_gen, "_plist_path", lambda label: dest)
    monkeypatch.setattr(launchd_gen, "_log_paths", lambda stem: pytest.fail("ignored installed log path"))
    return log


def test_counter_counts_legacy_and_retried_startups_but_not_runtime(tmp_path, monkeypatch):
    log = _health_log(tmp_path, monkeypatch)
    log.write_text(_STARTUP * 2 + launchd_gen._DRAIN_ATTEMPT + "1\n" +
                   launchd_gen._DRAIN_STARTED + "\n" + _STARTUP +
                   launchd_gen._DRAIN_ATTEMPT + "1\n" + _STARTUP)
    health = launchd_gen.queue_drain_startup_failures()
    assert health["count"] == 3
    assert health["log_path"] == str(log)
    assert health["truncated"] is False


def test_counter_bounds_tail_and_tolerates_missing_or_unreadable_logs(tmp_path, monkeypatch):
    log = _health_log(tmp_path, monkeypatch)
    assert launchd_gen.queue_drain_startup_failures()["missing"] is True
    log.write_bytes(_STARTUP.encode() + b"noise\n" * launchd_gen._STARTUP_LOG_BYTES + _STARTUP.encode())
    health = launchd_gen.queue_drain_startup_failures()
    assert health["count"] == 1 and health["truncated"] is True
    log.unlink()
    log.mkdir()
    health = launchd_gen.queue_drain_startup_failures()
    assert health["count"] is None and health["error"]


def test_refresh_reports_startup_failures_even_for_external_queue_job(tmp_path, monkeypatch):
    log = _health_log(tmp_path, monkeypatch)
    log.write_text(_STARTUP)
    monkeypatch.setattr(launchd_gen, "installed_jobs", lambda: ["queue_drain"])
    monkeypatch.setattr(launchd_gen, "plist_external", lambda job: True)
    monkeypatch.setattr(launchd_gen, "install_job", lambda *a, **kw: pytest.fail("external job modified"))
    out = launchd_gen.refresh_scheduled_jobs(["queue_drain"])
    assert out["external"] == ["queue_drain"]
    assert out["startup_failures"]["queue_drain"]["count"] == 1
    assert launchd_gen.refresh_scheduled_jobs(["nightly"])["startup_failures"] == {}


def test_terminated_run_keeps_its_error_output(tmp_path):
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\nprintf 'drain progress line\\n' >&2\nexec sleep 30\n")
    python.chmod(0o700)
    env = dict(os.environ, TMPDIR=str(tmp_path))
    proc = subprocess.Popen(launchd_gen._queue_drain_arguments(str(python)), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    deadline = __import__("time").monotonic() + 10
    while not list(tmp_path.glob("khipu-drain.*")) or not any(
            p.stat().st_size for p in tmp_path.glob("khipu-drain.*")):
        assert __import__("time").monotonic() < deadline
        __import__("time").sleep(0.05)
    os.killpg(proc.pid, 15)  # launchd stops the whole job group
    _, err = proc.communicate(timeout=10)
    assert proc.returncode == 143
    assert "drain progress line" in err
    assert list(tmp_path.glob("khipu-drain.*")) == []
