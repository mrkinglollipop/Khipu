"""Owner-local delivery rows (survey rows 8-11) ride only on ``public_view``.

Only disposable homes and synthetic names; every outward projection is
scanned recursively, including strings nested in lists and dictionary keys.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest

from khipu import cli, routing, routing_delivery, routing_replication
from tests.test_routing_acceptance import replay  # noqa: F401
from tests.test_routing_s16_health import planned

FRIENDLY = "Fix the QuasarSentinel build"
MAC_NAME = "StudioMacSentinel"
OTHER_MAC_NAME = "AirMacSentinel"
SESSION = "claude:synthetic-delivery-session"
PRIVATE_PATH = "/private/synthetic-delivery-folder/transcript.jsonl"
OTHER_MAC = "b" * 32


def strings(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield str(key)
            yield from strings(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            yield from strings(inner)
    elif isinstance(value, str):
        yield value


def set_friendly(session, name):
    with routing_delivery._transaction(session) as data:
        data.setdefault("attachment", {})["friendly_name"] = name


def local_mac(replay):
    with routing._lock():
        return routing._mac_locked()


def test_public_view_carries_live_rows_with_last_prompt_and_this_mac(replay):  # noqa: F811
    out = planned()
    answer = routing_delivery.offer(out, session="synthetic-one")
    assert routing_delivery.acknowledge("synthetic-one", answer["routing_changes"], now=replay.now)
    public = routing.public_view(replay.view(), replay.now)
    rows = public["delivery_rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["label"] == "Conversation 1"
    assert row["harness"] == "unknown"
    assert row["mac"] == "This Mac"
    assert row["last_prompt_at"] == routing._iso(replay.now)
    assert row["status"] == "green"
    assert row["owed"] == 0
    assert not any(SESSION in s or PRIVATE_PATH in s for s in strings(rows))


def test_stored_friendly_name_is_the_row_label(replay):  # noqa: F811
    routing_delivery.offer(planned(), session="synthetic-one")
    set_friendly("synthetic-one", FRIENDLY)
    public = routing.public_view(replay.view(), replay.now)
    assert public["delivery_rows"][0]["label"] == FRIENDLY


def test_t3_thread_title_is_stored_at_delivery_time(replay, tmp_path, monkeypatch):  # noqa: F811
    db = tmp_path / "statev2.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE orchestration_v2_projection_threads "
                "(thread_id TEXT, payload_json TEXT, updated_at TEXT)")
    con.execute("INSERT INTO orchestration_v2_projection_threads VALUES (?, ?, ?)",
                ("thread-synthetic", json.dumps({"title": FRIENDLY}), "2026-10-20"))
    con.commit()
    con.close()
    monkeypatch.setenv("KHIPU_T3_DB", str(db))
    routing_delivery.offer(planned(), session="t3:thread-synthetic")
    public = routing.public_view(replay.view(), replay.now)
    assert public["delivery_rows"][0]["label"] == FRIENDLY


def test_mac_names_setting_names_this_and_other_macs(replay, monkeypatch):  # noqa: F811
    routing_delivery.offer(planned(), session="synthetic-one")
    mac = local_mac(replay)
    routing.set_setting("routing_mac_names", json.dumps({mac: MAC_NAME, OTHER_MAC: OTHER_MAC_NAME}))
    monkeypatch.setattr(routing_replication, "status", lambda now=None: {
        "sources": [{"mac": OTHER_MAC, "last_seen_at": routing._iso(replay.now - timedelta(minutes=5))},
                    {"mac": mac, "last_seen_at": routing._iso(replay.now)}]})
    public = routing.public_view(replay.view(), replay.now)
    assert public["delivery_rows"][0]["mac"] == MAC_NAME
    assert public["delivery_macs"] == [
        {"label": OTHER_MAC_NAME, "age_seconds": 300, "behind": False}]


def test_other_mac_is_plain_another_mac_until_named(replay, monkeypatch):  # noqa: F811
    monkeypatch.setattr(routing_replication, "status", lambda now=None: {
        "sources": [{"mac": OTHER_MAC, "last_seen_at": routing._iso(replay.now - timedelta(hours=2))}]})
    public = routing.public_view(replay.view(), replay.now)
    assert public["delivery_macs"][0]["label"] == "another Mac"
    assert public["delivery_macs"][0]["behind"] is True
    assert not any(OTHER_MAC in s for s in strings(public["delivery_macs"]))


def test_mac_names_setting_rejects_bad_shapes(replay):  # noqa: F811
    with pytest.raises(ValueError):
        routing.set_setting("routing_mac_names", json.dumps({"not-a-mac": "Name"}))
    with pytest.raises(ValueError):
        routing.set_setting("routing_mac_names", json.dumps({OTHER_MAC: "two\nlines"}))


def test_per_change_recipients_count_and_name_who_has_it(replay):  # noqa: F811
    routing.add_rule("Synthetic ordinary rule for recipients", via="app")
    view = replay.view()
    change = view["changes"][-1]
    out = planned()
    out["delivery_plan"]["items"] = [dict(entity=e, revision="r1", text="abc",
                                          label="Standing rule", whole=True, visible=True)
                                     for e in change["entities"]]
    out["delivery_plan"]["current_entities"] = list(change["entities"])
    answer = routing_delivery.offer(out, session="synthetic-one")
    assert routing_delivery.acknowledge("synthetic-one", answer["routing_changes"], now=replay.now)
    routing_delivery.offer(out, session="synthetic-two")
    set_friendly("synthetic-one", FRIENDLY)
    public = routing.public_view(replay.view(), replay.now)
    recipients = next(c["recipients"] for c in public["changes"] if c["version"] == change["version"])
    assert recipients["have_count"] == 1
    assert recipients["live_count"] == 2
    assert recipients["have"] == [FRIENDLY]
    assert len(recipients["waiting"]) == 1
    assert recipients["waiting"][0].startswith("Conversation ")


def test_empty_and_damaged_ledgers_read_as_empty_rows(replay, tmp_path):  # noqa: F811
    public = routing.public_view(replay.view(), replay.now)
    assert public["delivery_rows"] == []
    assert public["delivery_macs"] == []
    from khipu import recall_prompt
    folder = recall_prompt._dedup_path("ledger").parent
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "recall--damaged.routing.json").write_text("{not json")
    public = routing.public_view(replay.view(), replay.now)
    assert public["delivery_rows"] == []


@pytest.fixture
def planted(replay, monkeypatch):  # noqa: F811
    replay.configure(routing_cloud_copy=True, routing_cloud_copy_rules=True,
                     routing_cloud_copy_hold_words=True, routing_child_chars=400)
    for i in range(3):
        routing.add_rule(f"Synthetic ordinary rule {i} " + "x" * 900, via="app")
    routing_delivery.offer(planned(), session="synthetic-one")
    set_friendly("synthetic-one", FRIENDLY)
    mac = local_mac(replay)
    routing.set_setting("routing_mac_names", json.dumps({mac: MAC_NAME, OTHER_MAC: OTHER_MAC_NAME}))
    monkeypatch.setattr(routing_replication, "status", lambda now=None: {
        "sources": [{"mac": OTHER_MAC, "last_seen_at": routing._iso(replay.now)}]})
    return replay


def test_owner_local_delivery_rows_exist_only_on_public_view(planted, capsys):
    public = routing.public_view(planted.view(), planted.now)
    assert public["delivery_rows"][0]["label"] == FRIENDLY
    assert public["delivery_rows"][0]["mac"] == MAC_NAME
    assert planted.view().get("delivery_rows") is None
    assert cli.main(["route", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["delivery_rows"] == public["delivery_rows"]


@pytest.mark.parametrize("surface", ["push", "child", "mcp", "replication", "gateway", "doctor"])
def test_owner_local_delivery_names_never_reach_other_surfaces(planted, monkeypatch, surface):
    view = planted.view()
    public = routing.public_view(view, planted.now)
    assert public["delivery_rows"][0]["label"] == FRIENDLY
    enriched = dict(view, delivery_rows=public["delivery_rows"],
                    delivery_macs=public["delivery_macs"])
    monkeypatch.setattr(routing, "read_view", lambda *a, **kw: enriched)
    monkeypatch.setattr(routing, "_start_view", lambda *a, **kw: enriched)
    if surface == "push":
        output = routing.start_push(budget=2, session_id="synthetic-recipient")
        assert output and output["text"]
    elif surface == "child":
        start = routing.start_push(budget=2, moment="delegation",
                                   child_component="agent:synthetic", child_session="synthetic-recipient")
        assert routing_delivery.acknowledge("synthetic-recipient", start["routing_changes"], now=planted.now)
        pages = []
        for _ in range(20):
            page = routing._child_page_push_inline("synthetic-recipient", "synthetic", planted.now)
            if not page:
                break
            pages.append(page)
            assert routing_delivery.acknowledge("synthetic-recipient", page["routing_changes"], now=planted.now)
        assert start and start["text"] and pages
        output = [start, pages, routing_delivery.snapshot("synthetic-recipient", "agent:synthetic")]
    elif surface == "mcp":
        error, output = planted.call(session_id="synthetic-recipient")
        assert not error and output["accounts"]
    elif surface == "replication":
        events = routing._read_all_events()[0]
        events = [dict(e, delivery_rows=public["delivery_rows"]) for e in events]
        output = routing.replication_payload(events, bindings={})
        assert output
    elif surface == "doctor":
        output = routing.doctor_block(planted.now)
        assert output["routing_delivery"]
    else:
        output = routing.gateway_projection(public, now=planted.now)
        assert output and output["accounts"] and output["rules"]
    forbidden = ("delivery_rows", "delivery_macs", FRIENDLY, "QuasarSentinel",
                 MAC_NAME, OTHER_MAC_NAME, OTHER_MAC, SESSION, PRIVATE_PATH)
    for leaf in strings(output):
        assert not any(secret in leaf for secret in forbidden), (surface, leaf)
