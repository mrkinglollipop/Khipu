"""Round 9 of the routing-exceptions review (jobs/2026-10-26/route-exceptions-astra-r8): a cap
restore's retry sees a newer cap written under a moved login's old key. Failed on b8cb1fe0.
Hermetic: a temp data dir and fake accounts."""
from __future__ import annotations

import errno

import pytest

from khipu import accounts, routing

pytest_plugins = ("tests.test_routing_acceptance", "tests.test_owner_direction")


@pytest.fixture
def direction_world(tmp_path, monkeypatch, khipu_app):
    # test_owner_direction's world built from its parts (another module's
    # ``world`` wins by name in a whole-directory run).
    import json
    import sys

    from khipu import owner_turns
    from tests import test_owner_direction as od

    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": od.ACCOUNTS}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    for name in od._SESSION_VARS:
        monkeypatch.delenv(name, raising=False)
    khipu_app.as_shell("/opt/harness/claude")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    owner_turns.write_settings({"enabled": True})
    w = od.World(tmp_path, monkeypatch)
    w.be(od.OWNER)
    return w


NAME = "Claude second"
OLD = "claude|/logins/claude-second"


def _caps():
    return [c["pct"] for c in next(a for a in routing.read_view()["accounts"] if a["label"] == NAME)["caps"]]


def test_a_cap_retry_sees_a_newer_cap_written_under_a_moved_logins_old_key(direction_world, monkeypatch, tmp_path):
    routing.set_cap(NAME, window="weekly", kind="share", pct=40, revision="base", via="app")
    layers = [dict(c) for c in next(a for a in routing.read_view()["accounts"] if a["label"] == NAME)["cap_layers"]]
    routing.set_cap(NAME, window="weekly", kind="share", pct=70, revision="changed", via="app")
    dest = tmp_path / "moved-login"
    dest.mkdir()
    accounts.move(OLD, str(dest), expect=accounts.status()["version"], via="app")

    def restore():
        return routing.restore_cap_layers(NAME, window="weekly", kind="share", layers=layers, via="app")
    real = routing._write_many

    def first_line_then_disk_full(events):
        if len(events) > 1:
            real(events[:1])
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(events)
    with monkeypatch.context() as m, routing.operation("01a17700-0000-7000-8000-0000000000ee"):
        m.setattr(routing, "_write_many", first_line_then_disk_full)
        with pytest.raises(OSError):
            restore()
    # A writer that still names the old login key: the move forwards it to
    # the same account, so this is a newer owner choice for the same slot.
    routing.set_cap(OLD, window="weekly", kind="share", pct=90, revision="new-owner", via="app")
    assert _caps() == [90.0]
    with routing.operation("01a17700-0000-7000-8000-0000000000ee"):
        with pytest.raises(routing.RouteError, match="changed after the interrupted undo"):
            restore()
    assert _caps() == [90.0]
