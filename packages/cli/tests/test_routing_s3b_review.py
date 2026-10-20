"""Synthetic review probes and regressions for routing slice 3b."""
import json
import time
from datetime import timedelta
import pytest
from khipu import routing, routing_events
from tests.test_routing_acceptance import Replay, T0


@pytest.fixture
def mac(tmp_path, monkeypatch):
    monkeypatch.setattr(routing, "kick_plan_refresh", lambda: False)
    return Replay(tmp_path / "one", monkeypatch)


def _raw(row):
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")


def test_P1_owner_note_after_upgrade_is_not_lost_behind_ahead_legacy_lines(mac):
    # Old-format log whose last lines came from a clock two days ahead.
    _raw(mac.event("note_set", 1, text="old note"))
    _raw({**mac.event("note_set", 2, text="ahead note"),
          "written_at": routing._iso(T0 + timedelta(days=2))})
    mac.advance(60)
    routing.set_note("owner's new note", via="app")
    assert mac.view()["note"]["text"] == "owner's new note"   # pre-3b code: passes


def test_P1_hlc_dominates_the_base_it_was_written_against(mac):
    # Another Mac's edit (clock 1 day ahead) is in the record; this Mac edits
    # the same rule against it. The newer, causally later write must win.
    added = routing.add_rule("start", via="app")
    ahead = int((T0 + timedelta(days=1)).timestamp() * 1000)
    _raw({**mac.event("rule_edit", 50, text="from mac b", entity=added["entity"], target=added["entity"]),
          "id": routing_events.uuid7(), "mac": "b" * 32, "seq": 1,
          "hlc": {"wall": ahead, "counter": 0, "mac": "b" * 32}})
    assert mac.view()["rules"][0]["text"] == "from mac b"
    mac.advance(60)
    out = routing.edit_rule(added["entity"], "from this mac", via="app")
    last = [e for e in routing.read_events()[0] if e.get("id") == out["revision"]][0]
    assert routing_events.hlc_key(last) > (ahead, 0)
    assert mac.view()["rules"][0]["text"] == "from this mac"


def test_P2_two_macs_provisional_rule_1_stay_two_rules_in_effective(mac):
    a, b = "a" * 32, "b" * 32
    rows = [dict(mac.event("rule_add", 1, n=1, number_pending=True, text="A words", entity="rule-" + routing_events.uuid7()),
                 mac=a, id=routing_events.uuid7(), hlc={"wall": 1, "counter": 0, "mac": a}),
            dict(mac.event("rule_add", 2, n=1, number_pending=True, text="B words", entity="rule-" + routing_events.uuid7()),
                 mac=b, id=routing_events.uuid7(), hlc={"wall": 2, "counter": 0, "mac": b})]
    view = mac.fold(rows)
    assert len(view["rules"]) == 2
    assert all(rule["n"] is None and rule["number_pending"] for rule in view["rules"])
    assert len([k for k in view["effective"] if k.startswith("rule:")]) == 2


def test_P2_legacy_rule_ids_from_two_macs_do_not_collide():
    a = {"type": "rule_add", "id": "o1111", "n": 1, "text": "Mac A rule"}
    b = {"type": "rule_add", "id": "o2222", "n": 1, "text": "Mac B rule"}
    assert routing_events.entity(a) != routing_events.entity(b)


def test_P2_a_retried_owner_write_with_the_same_event_id_does_not_double(mac):
    routing.add_rule("once", via="app", event_id="fixed-event")
    with pytest.raises(Exception):
        routing.add_rule("once", via="app", event_id="fixed-event")
    assert len(mac.view()["rules"]) == 1


def test_P2_a_retried_conversation_request_does_not_double(mac):
    author = {"kind": "conversation", "harness": "codex", "project": "p", "session": "s1"}
    routing.request_rule("same words", owner_request="please", author=author, op="synthetic-retry")
    try:
        routing.request_rule("same words", owner_request="please", author=author, op="synthetic-retry")
    except routing.RouteError:
        pass
    assert len(mac.view()["rules"]) == 1


def test_P3_identical_legacy_lines_are_not_collapsed_after_upgrade(mac):
    row = {"type": "report", "state": "limited", "account": "codex|", "written_by": {"kind": "owner"},
           "written_at": routing._iso(T0), "via": "cli"}
    _raw(row)
    _raw(row)
    before = len(routing._in_write_order(routing.read_events()[0]))
    mac.advance(5)
    routing.set_note("x", via="app")
    after = routing._in_write_order(routing.read_events()[0])
    assert len(after) == before + 1


@pytest.mark.parametrize("hub", [False])
def test_cost_fold_at_20000_events(mac, hub):
    rows = []
    for i in range(20000):
        kind = i % 4
        e = mac.event(["rule_add", "rule_edit", "note_set", "hold"][kind], i + 1, n=(i // 4) + 1,
                      text=f"words {i}", scope={"kind": "all"}, reason="other")
        rows.append(json.dumps(e))
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n")
    t = time.perf_counter()
    mac.view()
    legacy = time.perf_counter() - t
    mac.advance(30)
    routing.set_note("modern", via="app")
    t = time.perf_counter()
    routing._save_view({})
    mac.view()
    mixed = time.perf_counter() - t
    t = time.perf_counter()
    routing.set_note("modern 2", via="app")
    write = time.perf_counter() - t
    print(f"\nCOST legacy_fold={legacy:.2f}s mixed_fold={mixed:.2f}s one_write={write:.2f}s")


def test_hub_credentials_keep_local_numbers_until_replication_is_live(mac, monkeypatch):
    monkeypatch.setattr(routing, "_hub_configured", lambda: True)
    for n in range(1, 15):
        assert routing.add_rule(f"local rule {n}", via="app")["n"] == n
    routing.edit_rule(14, "edited by number", via="app")
    rows = routing.read_events()[0]
    assert all("hub_order" not in row for row in rows)
    assert [r["local_order"] for r in rows if r["type"] == "rule_add"] == list(range(1, 15))
    assert mac.view()["rules"][-1]["text"] == "edited by number"


def test_legacy_origin_is_stable_across_upgrade_and_separates_macs(mac):
    rows = [mac.event("rule_add", 1, n=1, text="A"), mac.event("rule_edit", 2, n=1, text="edited A")]
    for row in rows:
        _raw(row)
    before = mac.view()["rules"][0]
    routing.edit_rule(1, "new A", via="app")
    assert mac.view()["rules"][0]["entity"] == before["entity"]
    assert before["text"] == "edited A"
    a, b = dict(rows[0], legacy_mac="a" * 32), dict(rows[0], legacy_mac="b" * 32)
    assert routing_events.entity(a) != routing_events.entity(b)
    assert routing_events.entity(a) == routing_events.entity(dict(rows[1], legacy_mac="a" * 32))
    assert len(mac.fold([a, b])["rules"]) == 2


@pytest.mark.parametrize("explicit", [True, False])
def test_retried_mcp_rule_add_returns_the_original_rule(mac, monkeypatch, explicit):
    from khipu import mcp_server

    monkeypatch.setenv("KHIPU_HARNESS", "codex")
    monkeypatch.setenv("KHIPU_SESSION_ID", "synthetic-retry-session")
    args = {"text": "synthetic rule", "owner_request": "please add this"}
    if explicit:
        args["op"] = "synthetic-retry-op"
    msg = {"jsonrpc": "2.0", "id": "synthetic-request", "method": "tools/call",
           "params": {"name": "khipu_route_rule", "arguments": args}}
    first = mcp_server.handle_message(msg)["result"]
    assert not first["isError"], first
    mac.advance(30)
    repeated = mcp_server.handle_message(msg)["result"]
    assert not repeated["isError"], repeated
    assert json.loads(first["content"][0]["text"])["id"] == json.loads(repeated["content"][0]["text"])["id"]
    assert len(mac.view()["rules"]) == 1
    if explicit:
        msg["params"]["arguments"]["text"] = "different words"
        assert mcp_server.handle_message(msg)["result"]["isError"]
        assert len(mac.view()["rules"]) == 1


def test_append_decodes_each_existing_line_once(mac, monkeypatch):
    for i in range(100):
        _raw(mac.event("note_set", i + 1, text="synthetic"))
    calls = []
    original = routing._loads

    def loads(value):
        if '"type": "note_set"' in value:
            calls.append(value)
        return original(value)

    monkeypatch.setattr(routing, "_loads", loads)
    routing.set_note("new synthetic note", via="app")
    assert len(calls) == 100


def test_operation_receipts_use_the_private_routing_salt(mac):
    event = {"type": "note_set", "text": "x", "written_by": routing.OWNER, "via": "app"}
    routing.set_note("x", via="app")
    row = routing.read_events()[0][0]
    assert row["op_body"] == routing_events.body_hash(event, routing._id_salt(operations=True))
    assert row["op_body"] != routing_events.body_hash(event, b"different synthetic salt")
    assert not routing._legacy_ids_issued()


def test_hlc_skew_is_reported_without_excluding_the_remote_clock(mac):
    ahead = int((T0 + timedelta(days=1)).timestamp() * 1000)
    _raw(dict(mac.event("note_set", text="remote", mac="b", hlc={"wall": ahead, "counter": 7, "mac": "b"})))
    routing.set_note("local", via="app")
    assert routing_events.hlc_key(routing.read_events()[0][-1]) > (ahead, 7)
    assert any("causal clock ahead" in w for w in routing.doctor_block()["warnings"])


def test_raw_legacy_clock_outlier_still_constrains_the_next_hlc(mac):
    ahead = T0 + timedelta(days=2)
    _raw(mac.event("note_set", 1, text="before"))
    _raw(dict(mac.event("note_set", 2, text="fast clock"), written_at=routing._iso(ahead)))
    _raw(mac.event("note_set", 3, text="back to normal"))
    mac.advance(60)
    routing.set_note("after all observed events", via="app")
    latest = routing.read_events()[0][-1]
    assert routing_events.hlc_key(latest) > (int(ahead.timestamp() * 1000), 0)
    assert mac.view()["note"]["text"] == "after all observed events"
