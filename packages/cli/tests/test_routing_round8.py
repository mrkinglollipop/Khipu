"""Round 8 of the routing-exceptions review (jobs/2026-10-26/route-exceptions-astra-r7): an
interrupted restore's retry target is the stored write's own identity. Each test failed on
3effb8eb. Hermetic: a temp data dir and fake accounts."""
from __future__ import annotations

import errno
from datetime import timedelta

import pytest

from khipu import routing

pytest_plugins = ("tests.test_routing_acceptance", "tests.test_owner_direction")

ME = {"kind": "conversation", "harness": "claude_code", "project": "project-a", "session": "s-one"}
OP = "01a17700-0000-7000-8000-0000000000dd"


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


def _interrupted(monkeypatch, call):
    real = routing._write_many

    def first_line_then_disk_full(events):
        if len(events) > 1:
            real(events[:1])
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(events)
    token = routing._OP.set(OP)
    try:
        monkeypatch.setattr(routing, "_write_many", first_line_then_disk_full)
        with pytest.raises(OSError):
            call()
    finally:
        monkeypatch.setattr(routing, "_write_many", real)
        routing._OP.reset(token)


def _retry(call):
    token = routing._OP.set(OP)
    try:
        return call()
    finally:
        routing._OP.reset(token)


def test_a_cap_retry_is_refused_when_a_newer_cap_reuses_the_last_ones_revision(direction_world, monkeypatch):
    now = routing._now()
    routing.set_cap("Codex", window="weekly", kind="share", pct=40, revision="base", via="app", now=now)
    layers = [dict(c) for c in next(a for a in routing.read_view(now)["accounts"] if a["label"] == "Codex")
              ["cap_layers"]]
    routing.set_cap("Codex", window="weekly", kind="share", pct=70, revision="same", via="app", now=now)
    _interrupted(monkeypatch, lambda: routing.restore_cap_layers("Codex", window="weekly", kind="share",
                                                                  layers=layers, via="app", now=now))
    routing.set_cap("Codex", window="weekly", kind="share", pct=90, revision="same", via="app", now=now)
    with pytest.raises(routing.RouteError, match="changed after the interrupted undo"):
        _retry(lambda: routing.restore_cap_layers("Codex", window="weekly", kind="share", layers=layers,
                                                  via="app", now=now))
    caps = next(a for a in routing.read_view(now)["accounts"] if a["label"] == "Codex").get("caps") or []
    assert [c["pct"] for c in caps] == [90.0]


def test_a_priority_retry_survives_a_proposal_expiring_on_its_own(direction_world, monkeypatch):
    routing.set_priority([{"account": "Codex", "role": "owner"}], via="app", expect=routing.priority_version())
    base = routing.conversation_view()["priority_owner_revision"]
    routing.request_priority([{"account": "Codex", "role": "request"}], owner_request="asked", author=ME,
                             expect=routing.priority_version(), base=base)
    stored = routing.read_view()["priority_stored"]
    now = routing._now()
    _interrupted(monkeypatch, lambda: routing.restore_priority(stored, via="app", now=now))
    later = now + timedelta(days=8)  # the live proposal expires; nobody wrote
    _retry(lambda: routing.restore_priority(stored, via="app", now=later))  # not refused
    held = routing.read_view()["priority"]  # the request it wrote, as of its own write
    assert held["pending"] is True and [e["role"] for e in held["entries"]] == ["request"]
