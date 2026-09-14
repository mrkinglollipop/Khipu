import plistlib

from khipu import launchd_gen


def test_render_extra_env_only_set_keys():
    out = launchd_gen.render_extra_env(
        {"KHIPU_GRAPHIFY_NIGHTLY": "/x/graphify_nightly.py", "KHIPU_BUILD_INDEX": " "}
    )
    assert "<key>KHIPU_GRAPHIFY_NIGHTLY</key>" in out
    assert "<string>/x/graphify_nightly.py</string>" in out
    assert "KHIPU_BUILD_INDEX" not in out
    assert launchd_gen.render_extra_env({}) == ""


def test_rendered_plist_carries_override_env(monkeypatch):
    monkeypatch.setenv("KHIPU_CONSOLIDATE_NIGHTLY", "/Volumes/A&B/consolidate_nightly.py")
    monkeypatch.delenv("KHIPU_BUILD_INDEX", raising=False)
    data = plistlib.loads(launchd_gen.render_plist("nightly"))
    env = data["EnvironmentVariables"]
    assert env["KHIPU_CONSOLIDATE_NIGHTLY"] == "/Volumes/A&B/consolidate_nightly.py"
    assert "KHIPU_BUILD_INDEX" not in env
    assert env["KHIPU_ROOT"]


def test_rendered_plist_redirects_bytecode_cache_outside_bundle():
    """Every launchd job exports PYTHONPYCACHEPREFIX so the bundled Python's
    __pycache__ writes never land inside a signed .app (the 0.3.15 "Khipu is
    damaged" incident — see khipu.paths.pycache_dir)."""
    for job in ("nightly", "monthly", "graph_build", "notes_watch", "queue_drain"):
        data = plistlib.loads(launchd_gen.render_plist(job))
        env = data["EnvironmentVariables"]
        assert env["PYTHONPYCACHEPREFIX"], job
        assert "Caches/Khipu/pycache" in env["PYTHONPYCACHEPREFIX"], job


def test_queue_drain_plist_runs_sessions_drain_every_5_min():
    """K10: the Aegis capture queue had no drainer of its own."""
    data = plistlib.loads(launchd_gen.render_plist("queue_drain"))
    assert data["Label"] == "com.khipu.queue-drain"
    assert data["ProgramArguments"][-2:] == ["sessions", "drain"]
    assert data["StartInterval"] == 300
    assert data["RunAtLoad"] is False


def test_notes_watch_plist_watches_every_memory_dir_and_throttles(monkeypatch):
    """F1: the fourth LaunchAgent — WatchPaths on every memory dir the notes
    scanner knows, debounced so a burst of edits triggers one reconcile, not
    one launch per write."""
    from khipu import notes

    monkeypatch.setattr(notes, "memory_dirs", lambda: [
        __import__("pathlib").Path("/tmp/a/memory"), __import__("pathlib").Path("/tmp/b/memory"),
    ])
    data = plistlib.loads(launchd_gen.render_plist("notes_watch"))
    assert data["Label"] == "com.khipu.notes-watch"
    assert data["WatchPaths"] == ["/tmp/a/memory", "/tmp/b/memory"]
    assert data["ThrottleInterval"] == 30
    assert data["ProgramArguments"][-3:] == ["notes", "reconcile", "--changed-only"]
    assert data["RunAtLoad"] is False
