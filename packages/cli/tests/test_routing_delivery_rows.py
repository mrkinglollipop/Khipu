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
    assert row["label"] == "Conversation 1 · unknown"
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


def test_t3_thread_title_reaches_the_label_via_the_read_path(replay, tmp_path, monkeypatch):  # noqa: F811
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
        {"label": OTHER_MAC_NAME, "age_seconds": None, "behind": False, "freshness": "unknown"}]


def test_other_mac_is_plain_another_mac_until_named(replay, monkeypatch):  # noqa: F811
    monkeypatch.setattr(routing_replication, "status", lambda now=None: {
        "sources": [{"mac": OTHER_MAC, "last_seen_at": routing._iso(replay.now - timedelta(hours=2))}]})
    public = routing.public_view(replay.view(), replay.now)
    assert public["delivery_macs"][0]["label"] == "another Mac"
    assert public["delivery_macs"][0]["age_seconds"] is None
    assert public["delivery_macs"][0]["behind"] is False
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
    out["epoch"] = view["epoch"]
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


@pytest.mark.parametrize("shaped", [
    "claude_code_0123abcd", "codex_0123abcd", "Explore-abcdef", "t3:thread-synthetic",
    "claude:synthetic-delivery-session", "agent:synthetic-component",
    "0123abcd-0123-abcd-0123-abcd0123abcd", "0123456789abcdef0123",
    "Investigate t3:private-thread-synthetic today",
    "Investigate claude_code_0123abcd today",
    "Investigate codex_0123abcd today",
    "Investigate agent:/Explore-abcdef today",
    "Investigate Explore-abcdef today",
    "Investigate 0123abcd-0123-abcd-0123-abcd0123abcd today",
    "Investigate deadbeefcafe1234 today",
    "Investigate synthetic-one today",
])
def test_identity_shaped_friendly_names_are_never_labels(replay, shaped):  # noqa: F811
    routing_delivery.offer(planned(), session="synthetic-one")
    set_friendly("synthetic-one", shaped)
    public = routing.public_view(replay.view(), replay.now)
    label = public["delivery_rows"][0]["label"]
    assert shaped not in label
    assert label.startswith("Conversation 1 · ")


def test_own_session_id_is_never_a_label(replay):  # noqa: F811
    routing_delivery.offer(planned(), session="synthetic-one")
    set_friendly("synthetic-one", "synthetic-one")
    public = routing.public_view(replay.view(), replay.now)
    assert public["delivery_rows"][0]["label"].startswith("Conversation 1 · ")


def test_conversation_authored_name_field_is_not_a_label(replay):  # noqa: F811
    from khipu import routing_delivery_rows

    routing_delivery.offer(planned(), session="synthetic-one")
    view = dict(replay.view())
    view["authored"] = {"name": "AuthoredSentinelName", "session": "synthetic-one"}
    data = routing_delivery_rows.delivery_data(view, replay.now, routing.settings())
    assert data["rows"][0]["label"].startswith("Conversation 1 · ")
    assert not any("AuthoredSentinelName" in s for s in strings(data["rows"]))


def test_recipient_needs_evidence_for_the_changes_revision_and_epoch(replay):  # noqa: F811
    from khipu import routing_delivery_rows

    routing.add_rule("Synthetic ordinary rule for recipients", via="app")
    view = replay.view()
    change = view["changes"][-1]
    out = planned()
    out["epoch"] = view["epoch"]
    out["version"] = change["version"]
    out["delivery_plan"]["items"] = [dict(entity=e, revision="r1", text="abc",
                                          label="Standing rule", whole=True, visible=True)
                                     for e in change["entities"]]
    out["delivery_plan"]["current_entities"] = list(change["entities"])
    answer = routing_delivery.offer(out, session="synthetic-one")
    assert routing_delivery.acknowledge("synthetic-one", answer["routing_changes"], now=replay.now)
    data = routing_delivery_rows.delivery_data(replay.view(), replay.now, routing.settings())
    assert data["recipients"][change["version"]]["have_count"] == 1
    # The same version under another epoch is not evidence for this epoch.
    other = dict(replay.view(), epoch="another-epoch")
    data = routing_delivery_rows.delivery_data(other, replay.now, routing.settings())
    assert data["recipients"][change["version"]]["have_count"] == 0
    # Editing the rule makes a new version whose revision was never delivered.
    rule = replay.view()["rules"][-1]
    routing.edit_rule(rule["n"], "Synthetic edited rule for recipients", via="app")
    view2 = replay.view()
    change2 = view2["changes"][-1]
    assert change2["version"] != change["version"]
    assert set(change["entities"]) & set(change2["entities"])
    data = routing_delivery_rows.delivery_data(view2, replay.now, routing.settings())
    assert data["recipients"][change2["version"]]["have_count"] == 0


def test_t3_title_is_resolved_at_route_read_not_on_delivery(replay, tmp_path, monkeypatch):  # noqa: F811
    from khipu import t3

    db = tmp_path / "statev2.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE orchestration_v2_projection_threads "
                "(thread_id TEXT, payload_json TEXT, updated_at TEXT)")
    con.execute("INSERT INTO orchestration_v2_projection_threads VALUES (?, ?, ?)",
                ("thread-synthetic", json.dumps({"title": FRIENDLY}), "2026-10-20"))
    con.commit()
    con.close()
    monkeypatch.setenv("KHIPU_T3_DB", str(db))
    calls = []
    real_title = t3.thread_title
    monkeypatch.setattr(t3, "thread_title", lambda tid: calls.append(tid) or real_title(tid))
    routing_delivery.offer(planned(), session="t3:thread-synthetic")
    assert calls == []
    public = routing.public_view(replay.view(), replay.now)
    assert public["delivery_rows"][0]["label"] == FRIENDLY
    assert calls == ["thread-synthetic"]
    public = routing.public_view(replay.view(), replay.now)
    assert public["delivery_rows"][0]["label"] == FRIENDLY
    assert calls == ["thread-synthetic"]


def test_title_pending_holds_only_a_digest_and_title_files_are_private(replay, tmp_path, monkeypatch):  # noqa: F811
    import os
    import stat

    from khipu import recall_prompt

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
    folder = recall_prompt._dedup_path("ledger").parent
    pending = list(folder.glob("title-pending--*.json"))
    assert len(pending) == 1
    assert "thread-synthetic" not in pending[0].read_text()
    assert "t3:" not in pending[0].read_text()
    assert stat.S_IMODE(pending[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    public = routing.public_view(replay.view(), replay.now)
    assert public["delivery_rows"][0]["label"] == FRIENDLY
    cache = list(folder.glob("title-cache--*.json"))
    assert len(cache) == 1
    assert stat.S_IMODE(cache[0].stat().st_mode) == 0o600
    assert "thread-synthetic" not in cache[0].read_text()
    assert not list(folder.glob("title-pending--*.json"))
    assert os.name == "posix"


def test_other_mac_freshness_is_unknown_without_a_catch_up_receipt(replay, monkeypatch):  # noqa: F811
    monkeypatch.setattr(routing_replication, "status", lambda now=None: {
        "sources": [{"mac": OTHER_MAC, "last_seen_at": routing._iso(replay.now)}]})
    public = routing.public_view(replay.view(), replay.now)
    assert public["delivery_macs"][0]["age_seconds"] is None
    assert public["delivery_macs"][0]["behind"] is False


def test_other_mac_freshness_uses_its_catch_up_receipt(replay, monkeypatch):  # noqa: F811
    monkeypatch.setattr(routing_replication, "status", lambda now=None: {
        "sources": [{"mac": OTHER_MAC, "last_seen_at": routing._iso(replay.now),
                     "caught_up_at": routing._iso(replay.now - timedelta(hours=2))}]})
    public = routing.public_view(replay.view(), replay.now)
    assert public["delivery_macs"][0]["age_seconds"] == 7200
    assert public["delivery_macs"][0]["behind"] is True


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
