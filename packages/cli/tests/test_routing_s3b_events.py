"""Identity/order regressions. Every writer uses a disposable local record."""
import itertools
import json
import re
from pathlib import Path

import pytest

from khipu import owner_direction, routing, routing_events
from tests.test_routing_acceptance import Replay


@pytest.fixture
def mac(tmp_path, monkeypatch):
    monkeypatch.setattr(routing, "kick_plan_refresh", lambda: False)
    return Replay(tmp_path / "one", monkeypatch)


def test_old_format_delivery_log_keeps_its_effective_view_and_bytes(mac):
    fixtures = Path(__file__).parent / "fixtures"
    original = (fixtures / "routing_delivery_oct14_19.jsonl").read_bytes()
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(original)
    expected = json.loads((fixtures / "legacy_effective_s3b.json").read_text())
    assert mac.view()["effective"] == expected
    assert path.read_bytes() == original
    routing.add_rule("new words", via="app")
    assert path.read_bytes().startswith(original)


def test_old_rule_numbers_keep_their_meaning_in_mixed_history(mac):
    old = [mac.event("rule_add", 1, n=11, text="old words"),
           mac.event("rule_edit", 2, n=11, text="edited words")]
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = "".join(json.dumps(row) + "\n" for row in old).encode()
    path.write_bytes(original)
    before = mac.view()
    entity = before["rules"][0]["entity"]
    assert before["effective"][f"rule:{entity}"]["text"] == "edited words"
    result = routing.edit_rule(11, "new edit", via="app")
    last = routing.read_events()[0][-1]
    assert last["target"] == last["entity"] == entity == result["entity"]
    assert last["base"] == before["rules"][0]["revision"]
    assert mac.view()["rules"][0]["text"] == "new edit"
    assert path.read_bytes().startswith(original)
    routing.retire_rule(entity, via="app")
    routing.restore_rule(entity, via="app")
    assert mac.view()["rules"][0]["entity"] == entity


def test_two_installations_have_distinct_ids_and_independent_sequences(mac, tmp_path, monkeypatch):
    first = routing.add_rule("one", via="app")
    rows1 = routing.read_events()[0]
    other = Replay(tmp_path / "two", monkeypatch)
    second = routing.add_rule("two", via="app")
    rows2 = routing.read_events()[0]
    assert rows1[0]["mac"] != rows2[0]["mac"]
    assert first["id"] != second["id"] and first["entity"] != second["entity"]
    assert rows1[0]["seq"] == rows2[0]["seq"] == 1
    assert first["n"] == second["n"] == 1  # no hub configured on either install
    other.advance(1)
    routing.edit_rule(second["entity"], "two edited", via="app")
    assert routing.read_events()[0][-1]["seq"] == 2


def test_local_numbering_and_owner_undo_use_the_entity(mac):
    first = routing.add_rule("one", via="app")
    assert first["n"] == 1
    want = owner_direction.intent("route.rule.edit", "1")
    before = owner_direction._before(want)
    line = owner_direction._ahead_line(want, {"auth": "app"}, before)
    assert line["target"] == first["entity"]
    routing.edit_rule(1, "two", via="app")
    owner_direction._undo_rule_words(line, "app")
    assert mac.view()["rules"][0]["text"] == "one"
    assert owner_direction._added_rule({"after": first}) == first["entity"]


def test_offline_rule_uses_uuid_and_never_publishes_a_number(mac, monkeypatch):
    monkeypatch.setattr(routing, "_hub_configured", lambda: True)
    monkeypatch.setattr(routing, "_replication_live", lambda: True)
    added = routing.add_rule("offline words", via="app")
    rule = mac.view()["rules"][0]
    assert added["n"] is None and rule["n"] is None and rule["number_pending"]
    assert routing.read_events()[0][0].get("n") is None
    assert "new rule (number pending)" in routing.rule_line(rule)
    assert routing.conversation_view()["rules"][0]["entity"] == added["entity"]
    with pytest.raises(routing.RouteError, match=added["entity"]):
        routing.edit_rule(1, "wrong target", via="app")
    routing.edit_rule(added["entity"], "uuid edit", via="app")
    assert mac.view()["rules"][0]["text"] == "uuid edit"


def test_assigned_number_remains_usable_while_another_rule_is_pending(mac, monkeypatch):
    old = routing.add_rule("assigned", via="app")
    monkeypatch.setattr(routing, "_hub_configured", lambda: True)
    monkeypatch.setattr(routing, "_replication_live", lambda: True)
    new = routing.add_rule("pending", via="app")
    assert new["n"] is None
    routing.edit_rule(1, "assigned edit", via="app")
    assert routing.rule_reference(1)[0] == old["entity"]
    with pytest.raises(routing.RouteError, match=new["entity"]):
        routing.rule_reference(2)


def test_hlc_orders_concurrent_edits_in_every_arrival_order(mac):
    entity = "rule-" + routing_events.uuid7()
    common = dict(entity=entity, n=1)
    add = mac.event("rule_add", 1, text="start", mac="a", hlc={"wall": 1000, "counter": 0, "mac": "a"}, **common)
    a = mac.event("rule_edit", 2, text="a", target=entity, base="e1", mac="a",
                  hlc={"wall": 1000, "counter": 1, "mac": "a"}, **common)
    b = mac.event("rule_edit", 3, text="b", target=entity, base="e1", mac="b",
                  hlc={"wall": 1000, "counter": 1, "mac": "b"}, **common)
    views = [mac.fold(list(rows)) for rows in itertools.permutations([add, a, b])]
    assert all(v["rules"][0]["text"] == "b" for v in views)
    assert all(v["effective"] == views[0]["effective"] for v in views)
    assert mac.fold([b, a, add, b])["rules"] == views[0]["rules"]


def test_hlc_survives_same_clock_and_restart_and_records_base(mac):
    added = routing.add_rule("one", via="app")
    first = routing.read_events()[0][-1]
    routing.edit_rule(added["entity"], "two", via="app")
    second = routing.read_events()[0][-1]
    assert routing_events.hlc_key(second) > routing_events.hlc_key(first)
    assert second["base"] == first["id"]
    assert second["mac"] == first["mac"]
    assert second["hlc"]["counter"] == first["hlc"]["counter"] + 1
    assert re.fullmatch(r"[0-9a-f]{32}", (routing.routing_dir() / "mac-id").read_text().strip())


def test_proposal_base_is_the_owner_revision(mac):
    owner = routing.add_rule("owner words", via="app")
    author = {"kind": "conversation", "harness": "codex", "session": "one"}
    routing.request_rule("first proposal", n=1, author=author)
    routing.request_rule("second proposal", n=1, author=author)
    rows = routing.read_events()[0]
    assert rows[1]["base"] == rows[2]["base"] == owner["id"]
    assert rows[1]["proposal"] != rows[2]["proposal"]
    assert all(re.fullmatch(r"proposal-[0-9a-f-]{36}", r["proposal"]) for r in rows[1:])


def test_saved_pre_identity_view_refolds_without_changing_effective_state(mac):
    routing.add_rule("one", via="app")
    before = mac.view()
    old = dict(before)
    old.pop("event_identity")
    old["rules"] = [{k: v for k, v in r.items() if k not in ("entity", "revision")} for r in old["rules"]]
    routing._save_view(old)
    after = mac.view()
    assert after["event_identity"] == 2
    assert after["effective"] == before["effective"]
    assert after["version"] == before["version"]
    assert after["rules"][0]["entity"] == before["rules"][0]["entity"]


def test_sanitizer_omits_a_provisional_rules_null_number(mac, monkeypatch):
    from scripts.routing_fixture_sanitize import load_replay, sanitize

    monkeypatch.setattr(routing, "_hub_configured", lambda: True)
    monkeypatch.setattr(routing, "_replication_live", lambda: True)
    added = routing.add_rule("SYNTHETIC-OFFLINE-RULE", via="app")
    routing.edit_rule(added["entity"], "SYNTHETIC-OFFLINE-EDIT", via="app")
    raw = [json.loads(line) for line in routing.events_file().read_text().splitlines()]
    assert raw[0]["n"] is None and raw[0]["number_pending"] is True
    clean = sanitize(raw)
    assert load_replay(clean) == clean
    first, edit = clean["events"]
    assert "n" not in first and "n" not in edit
    assert first["number_pending"] is True
    assert first["entity"] == edit["entity"] == edit["target"]
    assert edit["base"] == first["id"]
    assert "SYNTHETIC" not in json.dumps(clean)


def test_new_identity_fields_are_sanitized_with_relationships_intact(mac):
    from scripts.routing_fixture_sanitize import FixtureError, load_replay, sanitize

    added = routing.add_rule("SYNTHETIC-PRIVATE", via="app")
    routing.edit_rule(added["entity"], "SYNTHETIC-EDIT", via="app")
    raw = [json.loads(line) for line in routing.events_file().read_text().splitlines()]
    clean = sanitize(raw)
    rows = clean["events"]
    assert rows[1]["entity"] == rows[1]["target"] == rows[0]["entity"]
    assert rows[1]["base"] == rows[0]["id"]
    assert rows[0]["mac"] == rows[0]["hlc"]["mac"]
    assert "SYNTHETIC" not in json.dumps(clean)
    for key in ("mac", "entity", "base", "op", "op_body", "proposal"):
        damaged = json.loads(json.dumps(clean))
        damaged["events"][0][key] = "PRIVATE-SENTINEL"
        with pytest.raises(FixtureError):
            load_replay(damaged)


def test_op_retry_after_other_writes_and_within_a_batch(mac):
    row = {"type": "rule_add", "text": "one", "n": 1, "op": "client-op"}
    original, _ = mac.write(row)
    mac.advance(1)
    routing.add_rule("intervening", via="app")
    repeated, _ = mac.write(row)
    assert repeated == original
    assert len(routing.read_events()[0]) == 2
    repeated, _ = mac.write(row, row)
    assert repeated == original + original
    with pytest.raises(routing.RouteError, match="different body"):
        mac.write(dict(row, text="changed"))
    assert len(routing.read_events()[0]) == 2


def test_op_conflict_rolls_back_the_whole_batch(mac):
    row = {"type": "rule_add", "text": "one", "n": 1, "op": "client-op"}
    mac.write(row)
    before = routing.events_file().read_bytes()
    with pytest.raises(routing.RouteError, match="different body"):
        mac.write(dict(row, op="new-op"), dict(row, text="changed"))
    assert routing.events_file().read_bytes() == before


def test_op_receipt_is_independent_of_author_audit_projection(mac):
    mac.write({"type": "note_set", "text": "owner", "written_by": {"kind": "owner"},
               "via": "app", "auth": "app"})
    row = {"type": "rule_add", "n": 1, "text": "one", "op": "retry-op", "via": "app",
           "written_by": {"kind": "owner"}}
    first, _ = mac.write(row)
    assert routing.read_events()[0][-1]["via"] == "app-unchecked"
    again, _ = mac.write(row)
    assert again == first


def test_modern_hold_tombstone_is_keyed_by_entity(mac):
    entity = "hold-" + routing_events.uuid7()
    end = mac.event("hold_end", 1, target=entity, entity=entity, mac="b",
                    hlc={"wall": 1, "counter": 0, "mac": "b"})
    hold = mac.event("hold", 2, entity=entity, mac="a", scope={"kind": "all"}, text="wait",
                     hlc={"wall": 2, "counter": 0, "mac": "a"})
    assert mac.fold([end, hold])["holds"] == mac.fold([hold, end])["holds"] == []


@pytest.mark.parametrize("hlc", [{"wall": True, "counter": 0, "mac": "a"},
                                  {"wall": 1, "counter": -1, "mac": "a"}, [],
                                  {"wall": 1, "counter": 0, "mac": ""}])
def test_malformed_hlc_costs_one_line(mac, hlc):
    row = mac.event("rule_add", n=1, text="one", hlc=hlc)
    assert routing._line_event(json.dumps(row).encode()) is None
