"""Round 5 of the routing-exceptions review (jobs/2026-10-26/route-exceptions-sol-r4): every undo
without the provenance mark is refused; incomplete batches never reach a fold; a retried restore
never writes again over a newer owner choice. Each test failed on f3a28d7b. Hermetic: a temp data
dir and fake accounts."""
from __future__ import annotations

import errno
from datetime import timedelta

import pytest

from khipu import owner_act, owner_direction, routing

pytest_plugins = ("tests.test_routing_acceptance", "tests.test_owner_direction")

ME = {"kind": "conversation", "harness": "claude_code", "project": "project-a", "session": "s-one"}
OP = "01a17700-0000-7000-8000-0000000000aa"
REFUSAL = "this change was saved before Khipu recorded who wrote it; set it again instead"


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


def _legacy_undo(world, monkeypatch, action, args, strip=("provenance",)):
    real = owner_direction._before

    def old(want):
        out = real(want)
        return {k: v for k, v in out.items() if k not in strip} if isinstance(out, dict) else out
    monkeypatch.setattr(owner_direction, "_before", old)
    world.say(f"do {action}")
    ok, applied = owner_act.act(action, args)
    assert ok, applied
    world.say("undo that")
    return owner_act.act("direction.undo", {"id": applied["owner_action"]})


# ---- every undo without the provenance mark is refused -------------------------------------------


def test_a_legacy_confirm_undo_is_refused(direction_world, monkeypatch):
    routing.set_priority([{"account": "Codex", "role": "reviews"}], via="app", expect=routing.priority_version())
    base = routing.conversation_view()["priority_owner_revision"]
    routing.request_priority([{"account": "Codex", "role": "CONVERSATION WORDS"}], owner_request="Matt asked",
                             author=ME, expect=routing.priority_version(), base=base)
    ok, out = _legacy_undo(direction_world, monkeypatch, "route.priority.confirm",
                           {"expect": routing.priority_version()}, strip=("provenance", "pending", "written_by",
                                                                         "owner_request", "replaced", "via"))
    assert not ok and REFUSAL in str(out)


def test_a_legacy_priority_undo_with_no_predecessor_on_record_is_refused(direction_world, monkeypatch):
    base = routing.conversation_view()["priority_owner_revision"]
    routing.request_priority([{"account": "Codex", "role": "CONVERSATION WORDS"}], owner_request="Matt asked",
                             author=ME, expect=routing.priority_version(), base=base)
    ok, out = _legacy_undo(direction_world, monkeypatch, "route.priority.clear",
                           {"expect": routing.priority_version()},
                           strip=("provenance", "pending", "written_by", "owner_request", "replaced", "via"))
    assert not ok and REFUSAL in str(out)
    assert routing.read_view()["priority"] is None


def test_a_legacy_rule_add_undo_is_refused(direction_world, monkeypatch):
    ok, out = _legacy_undo(direction_world, monkeypatch, "route.rule.add", {"text": "A new owner rule"})
    assert not ok and REFUSAL in str(out)


# ---- incomplete batches never reach a fold ---------------------------------------------------------


def _short_written_restore(monkeypatch):
    routing.set_priority([{"account": "Codex", "role": "reviews"}], via="app", expect=routing.priority_version())
    base = routing.conversation_view()["priority_owner_revision"]
    routing.request_priority([{"account": "Codex", "role": "CONVERSATION WORDS"}], owner_request="Matt asked",
                             author=ME, expect=routing.priority_version(), base=base)
    stored = routing.read_view()["priority_stored"]
    routing.clear_priority(via="app", expect=routing.priority_version())
    real = routing._write_many

    def first_line_then_disk_full(events):
        if len(events) > 1:
            real(events[:1])
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(events)
    monkeypatch.setattr(routing, "_write_many", first_line_then_disk_full)
    with pytest.raises(OSError):
        routing.restore_priority(stored, via="app")
    monkeypatch.setattr(routing, "_write_many", real)


def test_the_fold_itself_ignores_an_incomplete_batch(direction_world, monkeypatch):
    _short_written_restore(monkeypatch)
    _fingerprint, cfg, known = routing._inputs()
    raw, _bad = routing._read_all_events()
    assert any(e.get("batch") for e in raw)
    view = routing.fold(raw, now=routing._now(), cfg=cfg, known=known)
    assert view["priority"] is None


def test_a_batch_whose_members_disagree_on_its_size_counts_as_incomplete(direction_world):
    rows = [{"type": "note_set", "text": "one", "batch": "b1", "batch_n": 2, "id": "a"},
            {"type": "note_set", "text": "two", "batch": "b1", "batch_n": 3, "id": "b"},
            {"type": "note_set", "text": "plain", "id": "c"}]
    kept, _open = routing._split_txns(rows)
    assert [e["text"] for e in kept] == ["plain"]


# ---- a retried restore never writes again over a newer owner choice ---------------------------------


def test_a_retried_rule_restore_never_brings_back_a_request_the_owner_since_undid(direction_world):
    routing.add_rule("Owner's rule words", via="app")
    shown = routing.conversation_view()
    routing.request_rule("CONVERSATION RULE WORDS", n=1, owner_request="asked", author=ME, via="mcp",
                         base=shown["rules"][0]["owner_revision"])
    request = routing.pending_rule_line(routing.read_view()["rules"][0]["entity"])
    routing.edit_rule(1, "Owner's newer words", via="app")
    token = routing._OP.set(OP)
    try:
        routing.restore_rule_request(1, request, via="app")  # complete: owner words back, request pending
    finally:
        routing._OP.reset(token)
    assert routing.read_view()["rules"][0]["pending"] is True
    routing.undo_rule_request(1, via="app")  # the owner's newer choice: the request is gone
    token = routing._OP.set(OP)
    try:
        routing.restore_rule_request(1, request, via="app")  # the retry regenerates one line
    finally:
        routing._OP.reset(token)
    rule = routing.read_view()["rules"][0]
    assert not rule.get("pending") and rule["text"] == "Owner's rule words"


def test_a_retried_cap_restore_never_removes_a_newer_cap(direction_world):
    now = routing._now()
    layer = {"kind": "share", "pct": 50.0, "window": "weekly", "purpose": "job",
             "until": routing._iso(now + timedelta(hours=1))}
    token = routing._OP.set(OP)
    try:
        routing.restore_cap_layers("Codex", window="weekly", kind="share", layers=[layer], via="app", now=now)
    finally:
        routing._OP.reset(token)
    routing.set_cap("Codex", window="weekly", kind="share", pct=70, revision="newer", via="app", now=now)
    later = now + timedelta(hours=2)
    token = routing._OP.set(OP)
    try:
        routing.restore_cap_layers("Codex", window="weekly", kind="share", layers=[layer], via="app", now=later)
    finally:
        routing._OP.reset(token)
    caps = next(a for a in routing.read_view(later)["accounts"] if a["label"] == "Codex").get("caps") or []
    assert [c["pct"] for c in caps] == [70.0]
