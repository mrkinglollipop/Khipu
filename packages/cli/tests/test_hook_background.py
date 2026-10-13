"""The Stop hook's post-enqueue work runs in a detached process (khipu.hook_background).

2026-10-13: inline drains held a turn's end for a median 10 s and were killed at
the harness's 20 s hook timeout. These tests pin the detach, the drainer slots,
the drain loop and the fail-open run, plus the real hook script end to end.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from khipu import hook_background as hb
from khipu import session_capture as sc

HOOK = Path(__file__).resolve().parents[1] / "bin" / "khipu-stop-hook"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    for name in ("KHIPU_DRAIN_SLOTS", "KHIPU_CAPTURE_NO_DRAIN", "KHIPU_HOOK_INLINE"):
        monkeypatch.delenv(name, raising=False)


# --- spawn -------------------------------------------------------------------

def test_spawn_detaches_a_fresh_interpreter(monkeypatch):
    seen = {}

    class FakeProc:
        pid = 4242

    def fake_popen(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return FakeProc()

    monkeypatch.setenv("KHIPU_HOOK_STDIN", '{"session_id": "s"}')
    monkeypatch.setenv("KHIPU_SPAWN_KEEP", "yes")
    monkeypatch.setattr(hb.subprocess, "Popen", fake_popen)
    pid = hb.spawn("/mem", "claude_code", "sid-1")
    assert pid == 4242
    assert seen["argv"] == [sys.executable, "-m", "khipu.hook_background", "/mem", "claude_code", "sid-1"]
    kw = seen["kwargs"]
    assert kw["stdin"] is subprocess.DEVNULL
    assert kw["stdout"] is subprocess.DEVNULL
    assert kw["start_new_session"] is True
    assert "KHIPU_HOOK_STDIN" not in kw["env"]
    assert kw["env"]["KHIPU_SPAWN_KEEP"] == "yes"


# --- drain_slots -------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (None, 4), ("2", 2), ("0", 1), ("-3", 1), ("many", 4), ("", 4),
])
def test_drain_slots(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("KHIPU_DRAIN_SLOTS", raising=False)
    else:
        monkeypatch.setenv("KHIPU_DRAIN_SLOTS", value)
    assert hb.drain_slots() == expected


# --- _slot -------------------------------------------------------------------

def test_slot_hands_out_at_most_the_configured_number(monkeypatch):
    monkeypatch.setenv("KHIPU_DRAIN_SLOTS", "2")
    with hb._slot() as a, hb._slot() as b:
        assert a is not None and b is not None
        with hb._slot() as c:
            assert c is None
    with hb._slot() as again:
        assert again is not None


# --- _drain ------------------------------------------------------------------

def _patch_capture(monkeypatch, queued, outs):
    calls = []
    results = list(outs)

    def fake_drain():
        calls.append(1)
        return results.pop(0)

    monkeypatch.setattr(sc, "queued_jobs", queued)
    monkeypatch.setattr(sc, "drain", fake_drain)
    return calls


def test_drain_skips_an_empty_queue(monkeypatch):
    calls = _patch_capture(monkeypatch, lambda: [], [])
    hb._drain("s", "claude_code")
    assert calls == []


def test_drain_honours_no_drain(monkeypatch):
    monkeypatch.setenv("KHIPU_CAPTURE_NO_DRAIN", "1")
    calls = _patch_capture(monkeypatch, lambda: ["job"], [])
    hb._drain("s", "claude_code")
    assert calls == []


def test_drain_leaves_work_to_running_drainers_when_no_slot_is_free(monkeypatch, capsys):
    monkeypatch.setenv("KHIPU_DRAIN_SLOTS", "2")
    calls = _patch_capture(monkeypatch, lambda: ["job"], [])
    with contextlib.ExitStack() as stack:
        assert stack.enter_context(hb._slot()) is not None
        assert stack.enter_context(hb._slot()) is not None
        hb._drain("s", "claude_code")
    assert calls == []
    assert "drain left to the" in capsys.readouterr().err


def test_drain_takes_another_pass_while_jobs_remain(monkeypatch):
    calls = _patch_capture(
        monkeypatch, lambda: ["job"],
        [{"captured": 1, "empty": 0}, {"captured": 0, "empty": 0}])
    hb._drain("s", "claude_code")
    assert len(calls) == 2


def test_drain_stops_after_one_pass_that_empties_the_queue(monkeypatch):
    state = {"jobs": ["job"]}

    def queued():
        return list(state["jobs"])

    calls = []

    def fake_drain():
        calls.append(1)
        state["jobs"] = []
        return {"captured": 1, "empty": 0}

    monkeypatch.setattr(sc, "queued_jobs", queued)
    monkeypatch.setattr(sc, "drain", fake_drain)
    hb._drain("s", "claude_code")
    assert len(calls) == 1


# --- run ---------------------------------------------------------------------

def test_run_never_raises_and_names_every_skipped_step(monkeypatch, capsys):
    import khipu.config
    import khipu.embed
    import khipu.mirror
    import khipu.notes
    import khipu.outbox

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(hb, "_drain", boom)
    monkeypatch.setattr(khipu.config, "path_setting", boom)
    monkeypatch.setattr(khipu.mirror, "sync_recent_episodes", boom)
    monkeypatch.setattr(khipu.outbox, "jobs", boom)
    monkeypatch.setattr(khipu.notes, "reconcile", boom)
    monkeypatch.setattr(khipu.embed, "embed_recent_missing", boom)

    hb.run("", "claude_code", "sid-9")

    err = capsys.readouterr().err
    for step in ("drain skipped", "mirror skipped", "outbox drain skipped",
                 "notes-reconcile skipped", "embed catch-up skipped"):
        assert step in err, (step, err)
    assert "session=sid-9" in err


# --- the real hook script ----------------------------------------------------

def _run_hook(tmp_path, **extra):
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "KHIPU_KEYCHAIN": "0",
        "KHIPU_PYTHON": sys.executable,
        "KHIPU_HARNESS": "claude_code",
        "KHIPU_CAPTURE_HOME": str(tmp_path / "capture"),
        "KHIPU_CAPTURE_NO_DRAIN": "1",
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "PYTHONPYCACHEPREFIX": str(tmp_path / "pyc"),
    }
    env.update(extra)
    payload = json.dumps({"hook_event_name": "Stop", "session_id": "hook-bg-test",
                          "cwd": str(tmp_path)})
    t0 = time.monotonic()
    done = subprocess.run(["sh", str(HOOK)], input=payload, text=True, env=env,
                          capture_output=True, timeout=30, cwd=tmp_path)
    return done, time.monotonic() - t0, home / "Library" / "Logs" / "khipu" / "stop-hook.log"


def test_real_hook_returns_after_spawning_the_background_process(tmp_path):
    done, elapsed, log = _run_hook(tmp_path)
    assert done.returncode == 0
    assert elapsed < 15
    deadline = time.monotonic() + 10
    text = ""
    while time.monotonic() < deadline:
        text = log.read_text() if log.exists() else ""
        if "background pid=" in text:
            break
        time.sleep(0.1)
    assert "background pid=" in text, text
    assert "hook_ms=" in text
    # Let the detached run finish before the tmp dir goes away.
    time.sleep(0.5)


def test_real_hook_inline_mode_does_not_spawn(tmp_path):
    done, _elapsed, log = _run_hook(tmp_path, KHIPU_HOOK_INLINE="1")
    assert done.returncode == 0
    text = log.read_text() if log.exists() else ""
    assert "background pid=" not in text, text
    assert "background spawn failed" not in text, text
