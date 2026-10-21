"""Numbering across disposable Macs, including history predating replication."""
import json

import pytest

from khipu import routing, routing_replication as replication
from tests.test_routing_s11b_replication import pair, sync, T0


def seed(mac, *, idless=False, local_order=False, duplicate=False):
    mac.activate()
    rows = []
    for n in (1, 2, 3):
        row = dict(type="rule_add", text=f"confirmed {n}", seq=n,
                   written_at=routing._iso(T0), written_by={"kind": "owner"}, via="app")
        row["local_order" if local_order else "n"] = 1 if duplicate else n
        if not idless:
            row.update(id=f"legacy-{n}", entity=f"rule-{routing.event_identity.uuid7()}")
        rows.append(row)
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path.read_bytes()


@pytest.mark.parametrize("idless,local_order", [(False, False), (False, True), (True, False)])
def test_confirmed_numbers_survive_replication_and_new_rule_is_four(pair, idless, local_order):
    a, b, hub = pair
    seed(a, idless=idless, local_order=local_order)
    assert [r["n"] for r in a.view()["rules"]] == [1, 2, 3]
    added = routing.add_rule("new four", via="app")
    original = routing.events_file().read_bytes()
    for mac in (a, b, a, b):
        assert sync(mac, hub)["ok"]
    for mac in (a, b):
        mac.activate()
        numbers = {r["text"]: r["n"] for r in mac.view()["rules"]}
        assert numbers["new four"] == 4
        if mac is a or not idless:
            assert numbers == {"confirmed 1": 1, "confirmed 2": 2, "confirmed 3": 3, "new four": 4}
        assert routing.rule_reference(4)[0] == added["entity"]
    a.activate()
    assert routing.events_file().read_bytes() == original
    assert len(hub.rows) == (1 if idless else 4)


def test_legacy_only_sync_reserves_numbers_for_other_mac(pair):
    a, b, hub = pair
    seed(a, idless=True)
    assert sync(a, hub)["ok"]
    assert not hub.rows
    b.activate()
    routing.add_rule("other Mac four", via="app")
    for mac in (b, a):
        assert sync(mac, hub)["ok"]
        assert next(r for r in mac.view()["rules"] if r["text"] == "other Mac four")["n"] == 4


def test_duplicate_number_refuses_edit_and_history_with_uuids(pair):
    a, _, hub = pair
    seed(a, duplicate=True)
    ids = [r["entity"] for r in a.view()["rules"]]
    original = routing.events_file().read_bytes()
    for operation in (lambda: routing.edit_rule(1, "must not write", via="app"),
                      lambda: routing.rule_reference(1),
                      lambda: a.write(a.event("rule_edit", 10, n=1, text="must not write"))):
        with pytest.raises(routing.RouteError, match="ambiguous") as error:
            operation()
        assert all(entity in str(error.value) for entity in ids)
    assert routing.events_file().read_bytes() == original
    routing.edit_rule(ids[0], "explicit uuid works", via="app")
    assert next(r for r in a.view()["rules"] if r["entity"] == ids[0])["text"] == "explicit uuid works"


def test_restore_busy_is_clean_and_leaves_every_file_intact(pair):
    a, _, hub = pair
    a.activate()
    routing.add_rule("unchanged", via="app")
    assert sync(a, hub)["ok"]
    paths = [routing.events_file(), replication._path("mac-id"),
             replication._path("remote.jsonl"), replication._path("replication.json")]
    before = [path.read_bytes() for path in paths]
    with replication._sync_lock():
        assert replication.restore() == {"ok": False, "error": "busy"}
    assert [path.read_bytes() for path in paths] == before


def test_mixed_legacy_history_and_other_event_kinds_do_not_consume_rule_numbers(pair):
    a, b, hub = pair
    seed(a)
    path = routing.events_file()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[-1].pop("id")
    rows[-1].pop("entity")
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    a.write(a.event("note_set", 4, text="synthetic owner note", owner=True))
    routing.add_rule("four", via="app")
    hub.fail_upload = True
    assert not sync(a, hub)["ok"]
    for mac in (a, b, a):
        assert sync(mac, hub)["ok"]
    assert {r["text"]: r["n"] for r in a.view()["rules"]} == {
        "confirmed 1": 1, "confirmed 2": 2, "confirmed 3": 3, "four": 4}
    b.activate()
    routing.add_rule("five", via="app")
    for mac in (b, a):
        assert sync(mac, hub)["ok"]
        assert next(r for r in mac.view()["rules"] if r["text"] == "five")["n"] == 5
    a.activate()
    replication.restore()
    for mac in (a, b):
        assert sync(mac, hub)["ok"]
        numbers = {r["text"]: r["n"] for r in mac.view()["rules"]}
        assert numbers["four"] == 4 and numbers["five"] == 5


def test_duplicate_legacy_and_numbered_target_refuses_even_after_sync(pair):
    a, b, hub = pair
    seed(a, idless=True)
    a.activate()
    path = routing.events_file()
    existing = path.read_text()
    seed(b)
    for mac in (a, b, a):
        assert sync(mac, hub)["ok"]
    a.activate()
    uuids = [r["entity"] for r in a.view()["rules"] if r["n"] == 1]
    assert len(uuids) == 2
    with pytest.raises(routing.RouteError, match="ambiguous") as error:
        routing.edit_rule(1, "must not write", via="app")
    assert all(uuid in str(error.value) for uuid in uuids)
    assert path.read_text() == existing


def test_restore_busy_cli_returns_failure_without_traceback(pair, capsys):
    from khipu.cli import main
    a, _, _ = pair
    a.activate()
    with replication._sync_lock():
        assert main(["route", "replication", "restore"]) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "busy"}


@pytest.mark.parametrize("fields", [{"n": 3}, {"local_order": 3}, {"local_order": 3, "number_pending": True}])
def test_existing_number_is_preserved_even_with_global_identity_and_hub_order(fields):
    row = dict(type="rule_add", mac="synthetic", entity="rule-synthetic", hub_order=41,
               rule_number=17, **fields)
    assert routing._rule_number(row) == 3


def test_numeric_writer_resolves_display_number_after_sync(pair):
    a, b, hub = pair
    seed(a)
    added = routing.add_rule("four", via="app")
    for mac in (a, b):
        assert sync(mac, hub)["ok"]
    b.write(b.event("rule_edit", 10, n=4, text="four edited"))
    assert next(r for r in b.view()["rules"] if r["entity"] == added["entity"])["text"] == "four edited"
    latest = routing._read_event_lines()[0][-1]
    assert latest["target"] == added["entity"]
