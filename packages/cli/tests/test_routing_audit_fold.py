"""Offline regressions for the October 23 routing fold audit."""
import json
import socket
from datetime import datetime, timedelta, timezone

import pytest

from khipu import routing

T0 = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


@pytest.fixture
def world(tmp_path, monkeypatch, khipu_app):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": [
        {"label": "Claude", "harness": "claude"},
        {"label": "Codex", "harness": "codex"}]}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_SESSION_ID", "synthetic-audit-author")
    monkeypatch.setenv("KHIPU_HARNESS", "codex")
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    clock = [T0]
    monkeypatch.setattr(routing, "_now", lambda: clock[0])
    monkeypatch.setattr(routing, "kick_plan_refresh", lambda: False)
    def deny(*args, **kwargs):
        raise AssertionError("network forbidden in synthetic routing audit")
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    return clock


def _priority_proposal():
    routing.set_priority([{"account": "Claude"}], via="app", expect=routing.priority_version())
    base = routing.conversation_view()["priority_owner_revision"]
    routing.request_priority([{"account": "Codex"}], owner_request="synthetic request", base=base)
    return routing.read_view()


@pytest.mark.parametrize("minutes", [21, 61])
def test_cached_pace_expires_with_reading_and_reset(world, minutes):
    rows = [
        {"id": "sample", "type": "usage_sample", "written_at": routing._iso(T0),
         "account": "codex|", "window": "five_hour", "witness_at": routing._iso(T0),
         "resets_at": routing._iso(T0 + timedelta(hours=1)), "used_pct": 90,
         "mac": "synthetic", "source": "probe"},
        {"id": "cap", "type": "cap_set", "written_at": routing._iso(T0),
         "account": "codex|", "window": "five_hour", "kind": "pace_target", "pct": 100,
         "slack": 0, "revision": "cap-r1", "written_by": {"kind": "owner"}},
    ]
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    def target(view):
        account = next(a for a in view["accounts"] if a["key"] == "codex|")
        return account["pace"][0]["pace_target"]
    first = routing.read_view()
    assert target(first)["verdict"] == "ahead"
    world[0] = T0 + timedelta(minutes=minutes)
    cached = routing.read_view()
    direct = routing.fold(routing.read_events()[0], now=world[0], cfg=routing.settings(), known=routing.accounts())
    assert target(cached) == target(direct)
    assert target(cached)["verdict"] == "unknown"


def test_rule_confirm_cannot_confirm_unseen_replacement(world):
    routing.add_rule("OWNER WORDS", via="app")
    base = routing.conversation_view()["rules"][0]["owner_revision"]
    routing.request_rule("PROPOSAL SHOWN TO OWNER", n=1, base=base)
    shown = routing.read_view()["rules"][0]
    world[0] += timedelta(seconds=1)
    routing.request_rule("UNSEEN REPLACEMENT", n=1, base=base)
    before = routing.events_file().read_bytes()
    with pytest.raises(routing.RouteError, match="changed|revision|read"):
        routing.confirm_rule(shown["entity"], via="app")
    with pytest.raises(routing.RouteError, match="changed|revision|read"):
        routing.confirm_rule(shown["entity"], via="app", expect=shown["revision"])
    assert routing.events_file().read_bytes() == before
    after = routing.read_view()["rules"][0]
    assert after["text"] == "UNSEEN REPLACEMENT" and after["pending"]


def test_priority_expiry_changes_cas_without_changing_owner_revision(world):
    shown = _priority_proposal()
    world[0] += timedelta(days=8)
    expired = routing.read_view()
    assert expired["priority"]["entries"][0]["label"] == "Claude"
    assert expired["priority_owner_revision"] == shown["priority_owner_revision"]
    assert expired["priority_version"] != shown["priority_version"]
    before = routing.events_file().read_bytes()
    with pytest.raises(routing.PriorityMoved):
        routing.clear_priority(via="app", expect=shown["priority_version"])
    assert routing.events_file().read_bytes() == before
    assert routing.read_view()["priority"]["entries"][0]["label"] == "Claude"


def test_confirmation_crossing_expiry_cannot_advance_owner_base(world, monkeypatch):
    shown = _priority_proposal()
    world[0] += timedelta(days=7, seconds=-1)
    write = routing._owner_write
    def cross_expiry(event, via):
        world[0] += timedelta(seconds=2)
        return write(event, via)
    monkeypatch.setattr(routing, "_owner_write", cross_expiry)
    before = routing.events_file().read_bytes()
    with pytest.raises(routing.RouteError):
        routing.confirm_priority(via="app", expect=shown["priority_version"])
    assert routing.events_file().read_bytes() == before
    monkeypatch.setenv("KHIPU_SESSION_ID", "synthetic-fresh-reader")
    view = routing.conversation_view()
    assert view["priority"]["entries"][0]["label"] == "Claude"
    assert view["priority"]["owner_revision"] == view["priority_owner_revision"] == shown["priority_owner_revision"]
    routing.request_priority([{"account": "Codex"}], owner_request="new request",
                             base=view["priority"]["owner_revision"])


@pytest.mark.parametrize("field", ["used_pct", "pct", "slack", "usd", "days"])
def test_huge_stored_integer_is_skipped_and_diagnosed(world, field):
    routing.add_rule("OWNER WORDS", via="app")
    path = routing.events_file()
    good = path.read_bytes()
    corrupt = {"type": "evidence", "id": "corrupt-" + field,
               "written_at": routing._iso(T0), field: 10 ** 1000}
    path.write_bytes(good + json.dumps(corrupt).encode() + b"\n")
    events, bad = routing.read_events()
    assert bad == 1
    assert not any(e.get("id") == corrupt["id"] for e in events)
    assert routing.read_view()["rules"][0]["text"] == "OWNER WORDS"
    assert path.read_bytes().startswith(good)


@pytest.mark.parametrize("shape", ["entities-list", "revision-int", "versions-list"])
def test_malformed_receipt_does_not_break_conversation_read(world, shape):
    routing.add_rule("OWNER WORDS", via="app")
    routing.conversation_view()
    entity = routing.read_view()["rules"][0]["entity"]
    malformed = {"entities": []} if shape == "entities-list" else (
        {"entities": {entity: 4}} if shape == "revision-int" else {"priority_versions": []})
    receipt = routing._read_receipt_path()
    receipt.write_text(json.dumps(malformed))
    view = routing.conversation_view()
    assert view["rules"][0]["text"] == "OWNER WORDS"
    repaired = json.loads(receipt.read_text())
    assert repaired["entities"][entity] == [view["rules"][0]["owner_revision"]]
    assert isinstance(repaired["priority_versions"], dict)


def test_rule_confirmation_with_displayed_revision_still_applies(world):
    routing.request_rule("PROPOSAL SHOWN TO OWNER")
    rule = routing.read_view()["rules"][0]
    assert routing.confirm_rule(rule["entity"], via="app", expect=rule["revision"])["ok"]
    assert routing.read_view()["rules"][0]["confirmed"]


def test_new_priority_proposal_expiry_changes_cas_when_it_removes_priority(world):
    base = routing.conversation_view()["priority_owner_revision"]
    routing.request_priority([{"account": "Codex"}], owner_request="synthetic request", base=base)
    before = routing.read_view()
    world[0] += timedelta(days=8)
    after = routing.read_view()
    assert after["priority"] is None
    assert after["priority_version"] != before["priority_version"]
    assert after["priority_owner_revision"] == before["priority_owner_revision"]


def test_legacy_confirmation_after_expiry_does_not_advance_owner_base(world):
    shown = _priority_proposal()
    world[0] += timedelta(days=8)
    # An old log may already contain the faulty confirmation. Re-folding it
    # must treat that ineffective line as no owner change.
    legacy = {"id": "legacy-expired-confirm", "type": "priority_confirm",
              "written_at": routing._iso(world[0]), "written_by": {"kind": "owner"}, "via": "app"}
    with routing.events_file().open("a") as log:
        log.write(json.dumps(legacy) + "\n")
    after = routing.read_view()
    assert after["priority_owner_revision"] == shown["priority_owner_revision"]
    assert after["priority"]["owner_revision"] == shown["priority_owner_revision"]


def test_malformed_receipt_cannot_grant_unread_revision(world):
    routing.add_rule("OWNER WORDS", via="app")
    rule = routing.read_view()["rules"][0]
    receipt = routing._read_receipt_path()
    receipt.parent.mkdir(parents=True, exist_ok=True)
    # A string is iterable, but it is never an acknowledged revision list.
    receipt.write_text(json.dumps({"entities": {rule["entity"]: rule["owner_revision"]}}))
    with pytest.raises(routing.RouteError, match="not shown"):
        routing.request_rule("UNREAD", n=rule["entity"], base=rule["owner_revision"])
