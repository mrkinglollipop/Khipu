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
