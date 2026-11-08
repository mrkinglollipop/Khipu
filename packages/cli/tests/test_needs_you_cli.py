"""Owner-shell PTY acceptance; all state stays in the hermetic fixture."""
import json
import os
import pty
import sys

import pytest

from khipu import cli, needs_you, owner_direction, routing
from tests.test_t3_owner_confirm import world, _be, T3_SESSION
from tests.test_owner_needs_you import _setting_request, _priority_request
from tests.test_owner_priority_pending import _ask_in_t3


@pytest.mark.parametrize("answer", ["yes", "no"])
def test_owner_terminal_settles_the_displayed_setting(world, khipu_app, monkeypatch, capsys, answer):
    _setting_request(world)
    khipu_app.as_shell()
    for key in ("CLAUDE_CODE_SESSION_ID", "KHIPU_SESSION_ID", "CODEX_THREAD_ID"):
        monkeypatch.delenv(key, raising=False)
    main, sub = pty.openpty()
    try:
        os.write(main, b"yes\n")
        with os.fdopen(sub, "r") as terminal:
            monkeypatch.setattr(sys, "stdin", terminal)
            args = cli.build_parser().parse_args(["needs-you", "--" + answer, "setting:routing_replication_live"])
            assert cli.cmd_needs_you(args) == 0
        output = capsys.readouterr().out
        assert output.count("Type yes") == 1
        assert "copy routing" in output
        status = routing.settings_status()["routing_replication_live"]
        assert not status.get("pending")
        assert status["value"] is (answer == "yes")
    finally:
        os.close(main)


def test_agent_terminal_refuses_without_settling(world, khipu_app, monkeypatch, capsys):
    _setting_request(world)
    khipu_app.as_shell("/opt/codex")
    main, sub = pty.openpty()
    try:
        with os.fdopen(sub, "r") as terminal:
            monkeypatch.setattr(sys, "stdin", terminal)
            args = cli.build_parser().parse_args(["needs-you", "--yes", "setting:routing_replication_live"])
            assert cli.cmd_needs_you(args) == 2
        assert "conversation or the app" in capsys.readouterr().err
        assert routing.settings_status()["routing_replication_live"]["pending"]
    finally:
        os.close(main)


def test_stale_priority_request_is_decline_only_before_proof(world, khipu_app, capsys):
    _priority_request(world)
    pin = routing.pending_priority_pin(routing.read_view())
    _be(world, T3_SESSION)
    ok, held = _ask_in_t3(world, "route.priority.confirm", {"expect": pin})
    assert ok, held
    routing.undo_priority(via="app", expect=pin)
    _priority_request(world)
    item = next(i for i in needs_you.waiting(key="held:" + held["request"], record_read=False)["items"])
    assert item["decline_only"] and item["settle"]["confirm"] is None
    assert "priority changed since" in item["words"]
    with pytest.raises(owner_direction.DirectionRefused, match="priority changed since"):
        owner_direction.confirm(held["request"])
    assert next(r for r in owner_direction._read_requests(owner_direction.requests_path()) if r["id"] == held["request"])["state"] == "pending"

    view = routing.public_view(routing.read_view(), routing._now())
    assert view["decline_only_requests"][0]["id"] == held["request"]
    khipu_app.as_app()
    args = cli.build_parser().parse_args(["owner-direction", "decline", "--from-app", held["request"]])
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    assert next(r for r in owner_direction._read_requests(owner_direction.requests_path()) if r["id"] == held["request"])["state"] == "declined"


def test_saved_end_in_past_is_decline_only(world, monkeypatch):
    import time
    from datetime import datetime, timezone
    end = time.time() + 60
    until = datetime.fromtimestamp(end, timezone.utc).isoformat()
    ok, held = _ask_in_t3(world, "direction.request", {"action": "route.priority.set", "args": {
        "entries": ["Codex"], "expect": routing.priority_version(), "until": until}})
    assert ok, held
    monkeypatch.setattr(owner_direction, "_clock", lambda: end + 1)
    item = needs_you.waiting(key="held:" + held["request"], record_read=False)["items"][0]
    assert item["decline_only"] and item["settle"]["confirm"] is None
    assert "expired" in item["words"]
    with pytest.raises(owner_direction.DirectionRefused, match="expired"):
        owner_direction.confirm(held["request"])


def test_terminal_no_applies_an_already_held_undo(world, khipu_app, monkeypatch):
    _setting_request(world)
    ok, held = _ask_in_t3(world, "config.answer_request.undo", {"key": "routing_replication_live"})
    assert ok, held
    khipu_app.as_shell()
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    main, sub = pty.openpty()
    try:
        os.write(main, b"yes\n")
        with os.fdopen(sub, "r") as terminal:
            monkeypatch.setattr(sys, "stdin", terminal)
            args = cli.build_parser().parse_args(["needs-you", "--no", "setting:routing_replication_live"])
            assert cli.cmd_needs_you(args) == 0
        assert not routing.settings_status()["routing_replication_live"]["pending"]
        assert next(r for r in owner_direction._read_requests(owner_direction.requests_path()) if r["id"] == held["request"])["state"] == "applied"
    finally:
        os.close(main)


def test_cli_listing_is_inert_and_zero_is_not_an_item(world, capsys):
    _setting_request(world)
    before = owner_direction.requests_path().read_bytes() if owner_direction.requests_path().exists() else None
    for argv in (["needs-you"], ["needs-you", "--json"], ["needs-you", "--item", "1"]):
        args = cli.build_parser().parse_args(argv)
        assert cli.cmd_needs_you(args) == 0
    after = owner_direction.requests_path().read_bytes() if owner_direction.requests_path().exists() else None
    assert before == after
    args = cli.build_parser().parse_args(["needs-you", "--item", "0"])
    assert cli.cmd_needs_you(args) == 2


def test_expired_pending_priority_remains_declineable_after_it_leaves_the_live_order(world, monkeypatch):
    from datetime import timedelta
    now = routing._now()
    end = now + timedelta(minutes=1)
    _be(world, T3_SESSION)
    shown = routing.conversation_view()
    out = routing.request_priority(["Codex"], until=routing._iso(end), owner_request="Codex for a minute",
                                   author=routing.conversation_author(), via="mcp", now=now,
                                   expect=routing.priority_version(), base=shown["priority_owner_revision"])
    assert out["ok"], out
    later = end + timedelta(seconds=1)
    monkeypatch.setattr(routing, "_now", lambda: later)
    view = routing.read_view(later)
    assert view["priority"] is None
    item = needs_you.waiting(key="priority", now=later, record_read=False)["items"][0]
    assert item["decline_only"] and item["settle"]["confirm"] is None
    assert routing.public_view(view, later)["expired_priority"]["until"] == routing._iso(end)
    with pytest.raises(routing.RouteError, match="saved end has passed"):
        routing.confirm_priority(via="app", now=later, expect=view["priority_version"])
    assert routing.undo_priority(via="app", now=later, expect=view["priority_version"])["ok"]
