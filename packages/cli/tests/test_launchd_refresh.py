import tempfile
from pathlib import Path
from unittest import mock

from khipu import launchd_gen


def _patched(tmpdir: str):
    """Context manager stack: plist paths under tmpdir, launchctl/versions no-ops."""
    tmp = Path(tmpdir)

    def fake_plist_path(label: str) -> Path:
        return tmp / f"{label}.plist"

    return [
        mock.patch.object(launchd_gen, "_plist_path", side_effect=fake_plist_path),
        mock.patch.object(launchd_gen, "_launchagents_dir", return_value=tmp),
        mock.patch.object(
            launchd_gen,
            "_launchctl_load",
            return_value={"ok": True},
        ),
        mock.patch.object(launchd_gen, "read_versions", return_value={}),
        mock.patch.object(launchd_gen, "write_versions", return_value=None),
    ]


def test_refresh_reports_all_missing_when_nothing_installed():
    with tempfile.TemporaryDirectory() as tmpdir:
        patches = _patched(tmpdir)
        for p in patches:
            p.start()
        try:
            out = launchd_gen.refresh_scheduled_jobs()
            assert out["ok"] is True
            assert out["refreshed"] == []
            assert sorted(out["missing"]) == sorted(["nightly", "monthly", "graph_build"])
        finally:
            for p in patches:
                p.stop()


def test_refresh_rewrites_a_stale_plist_then_is_current():
    with tempfile.TemporaryDirectory() as tmpdir:
        patches = _patched(tmpdir)
        for p in patches:
            p.start()
        try:
            dest = Path(tmpdir) / f"{launchd_gen._LABELS['nightly']}.plist"
            dest.write_bytes(b"stale")

            out = launchd_gen.refresh_scheduled_jobs()
            assert "nightly" in out["refreshed"]
            assert dest.read_bytes() == launchd_gen.render_plist("nightly")
            assert launchd_gen.plist_current("nightly") is True

            out2 = launchd_gen.refresh_scheduled_jobs()
            assert "nightly" in out2["current"]
            assert out2["refreshed"] == []
        finally:
            for p in patches:
                p.stop()


def test_plist_current_is_none_when_file_absent():
    with tempfile.TemporaryDirectory() as tmpdir:
        patches = _patched(tmpdir)
        for p in patches:
            p.start()
        try:
            assert launchd_gen.plist_current("monthly") is None
        finally:
            for p in patches:
                p.stop()


def _plist_bytes(program: str, env: dict | None = None) -> bytes:
    import plistlib

    data = {"Label": "x", "ProgramArguments": [program, "-m", "khipu", "nightly"]}
    if env:
        data["EnvironmentVariables"] = env
    return plistlib.dumps(data)


def test_external_maintainer_plist_is_never_rewritten():
    with tempfile.TemporaryDirectory() as tmpdir:
        patches = _patched(tmpdir)
        for p in patches:
            p.start()
        try:
            dest = launchd_gen._plist_path(launchd_gen._LABELS["nightly"])
            dest.write_bytes(_plist_bytes("/usr/local/bin/python3.11"))
            assert launchd_gen.plist_external("nightly") is True
            assert launchd_gen.plist_current("nightly") is None
            out = launchd_gen.refresh_scheduled_jobs()
            assert out["external"] == ["nightly"]
            assert out["refreshed"] == []
            assert dest.read_bytes() == _plist_bytes("/usr/local/bin/python3.11")
        finally:
            for p in patches:
                p.stop()


def test_refresh_keeps_passthrough_env_baked_in_the_installed_plist(monkeypatch):
    import plistlib

    monkeypatch.delenv("KHIPU_CONSOLIDATE_NIGHTLY", raising=False)
    with tempfile.TemporaryDirectory() as tmpdir:
        patches = _patched(tmpdir)
        for p in patches:
            p.start()
        try:
            dest = launchd_gen._plist_path(launchd_gen._LABELS["nightly"])
            dest.write_bytes(_plist_bytes(
                "/Applications/Khipu.app/Contents/Resources/khipu/python/bin/python3.11",
                {"KHIPU_CONSOLIDATE_NIGHTLY": "/x/consolidate_nightly.py"},
            ))
            assert launchd_gen.plist_external("nightly") is False
            out = launchd_gen.refresh_scheduled_jobs(["nightly"])
            assert out["refreshed"] == ["nightly"]
            data = plistlib.loads(dest.read_bytes())
            assert data["EnvironmentVariables"]["KHIPU_CONSOLIDATE_NIGHTLY"] == "/x/consolidate_nightly.py"
            assert launchd_gen.plist_current("nightly") is True
        finally:
            for p in patches:
                p.stop()


def test_ensure_installs_missing_refreshes_stale_and_leaves_external_alone(monkeypatch):
    monkeypatch.delenv("KHIPU_CONSOLIDATE_NIGHTLY", raising=False)
    with tempfile.TemporaryDirectory() as tmpdir:
        patches = _patched(tmpdir)
        for p in patches:
            p.start()
        try:
            # nightly: maintainer-managed; monthly: stale app-rendered; graph_build: missing.
            launchd_gen._plist_path(launchd_gen._LABELS["nightly"]).write_bytes(
                _plist_bytes("/usr/local/bin/python3.11"))
            launchd_gen._plist_path(launchd_gen._LABELS["monthly"]).write_bytes(
                _plist_bytes("/Applications/Khipu.app/Contents/Resources/khipu/python/bin/python3.11"))
            out = launchd_gen.ensure_scheduled_jobs()
            assert out["ok"] is True
            assert out["external"] == ["nightly"]
            assert out["refreshed"] == ["monthly"]
            assert out["installed"] == ["graph_build"]
            assert launchd_gen._plist_path(launchd_gen._LABELS["nightly"]).read_bytes() == _plist_bytes("/usr/local/bin/python3.11")
            again = launchd_gen.ensure_scheduled_jobs()
            assert again["installed"] == [] and again["refreshed"] == []
            assert sorted(again["current"]) == ["graph_build", "monthly"]
        finally:
            for p in patches:
                p.stop()
