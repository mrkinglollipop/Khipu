"""Round two regressions through MCP and the actual hook delivery writer."""
import json
import os

import pytest

from khipu import agent_view, discover, recall_prompt, recall_rule, routing, routing_delivery
from tests.test_routing_s7_fix1 import home, _call, _route, _configure


@pytest.fixture(autouse=True)
def isolated(home, monkeypatch):
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(home["tmp"] / "capture"))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    monkeypatch.delenv("ROUTING_NOW_OFF", raising=False)
    monkeypatch.setattr(agent_view, "_TABLE", {"sig": None, "at": 0.0, "table": None})


def test_explicit_cannot_ack_an_environment_identified_session(home, monkeypatch):
    routing.add_rule("owner debt", via="app")
    monkeypatch.setenv("CODEX_THREAD_ID", "owner")
    batch = _route()["routing_changes"]["batch"]
    monkeypatch.delenv("CODEX_THREAD_ID")
    body = _route(session_id="codex:owner", ack=batch)
    assert body["delivery"] == "untracked: pass session_id"
    monkeypatch.setenv("CODEX_THREAD_ID", "owner")
    assert _route()["routing_changes"]["batch"] == batch


@pytest.mark.parametrize("error", [False, True])
def test_owner_substring_cannot_hide_a_pending_secret(home, monkeypatch, error):
    monkeypatch.setenv("CODEX_THREAD_ID", "mask")
    routing.set_note("Keep", via="app")
    secret = "sk-proj-" + "A" * 8 + "Keep" + "B" * 18
    ok, body = _call("khipu_route_rule", {"text": "Use " + secret,
                                         "owner_request": "synthetic request"})
    assert not ok, body
    _, body = _call("khipu_route", {"since": "bad"} if error else {})
    assert secret not in json.dumps(body)
    assert not any(i["entity"].startswith("rule:") for i in body.get("routing_changes", {}).get("items", []))


@pytest.mark.parametrize("error", [False, True])
def test_secret_is_scanned_before_paging_and_never_acknowledged(home, monkeypatch, error):
    monkeypatch.setenv("CODEX_THREAD_ID", "pages")
    _configure(home, routing_prompt_chars=400)
    secret = "sk-proj-" + "Q" * 40
    routing.request_rule("x" * 329 + " " + secret + "y" * 650,
                         author={"kind": "conversation", "harness": "codex", "session": "pages"})
    args = {"since": "bad"} if error else {"since": "0"}
    _, body = _call("khipu_route", args)
    chunks = []
    for _ in range(12):
        changes = body.get("routing_changes")
        if not changes:
            break
        chunks.extend(i.get("text", "") for i in changes["items"])
        _, body = _call("khipu_route", dict(args, ack=changes["batch"]))
    assert secret not in "".join(chunks)
    entry = next(e for k, e in routing_delivery.snapshot("codex:pages", "mcp")["entries"].items()
                 if k.startswith("rule:"))
    assert entry["offset"] == 0 and not entry["acked"]


@pytest.mark.parametrize("inventory", ["broken"])
@pytest.mark.parametrize("error", [False, True])
def test_unknown_alias_is_withheld_in_all_fields_without_complete_table(home, monkeypatch, inventory, error):
    monkeypatch.setenv("CODEX_THREAD_ID", "alias")
    if inventory == "default-only":
        (home["tmp"] / ".codex").mkdir()
    elif inventory == "broken":
        monkeypatch.setattr(discover, "_source_names", lambda: (_ for _ in ()).throw(OSError("synthetic")))
    alias = "SecretAliasZZZ"
    _configure(home, routing_lane_map={"runnerx": {alias: "NoSuchAccount"}})
    routing.report(state="limited", runner="runnerx", lane=alias, until="1h",
                   author={"kind": "conversation", "harness": "codex", "session": "alias"})
    _, body = _call("khipu_route", {"since": "bad"} if error else {})
    assert alias not in json.dumps(body)
    assert not any(i["entity"].startswith("lane:") for i in body.get("routing_changes", {}).get("items", []))


@pytest.mark.parametrize("error", [False, True])
def test_owner_page_provenance_survives_final_mcp_scrub(home, monkeypatch, error):
    monkeypatch.setenv("CODEX_THREAD_ID", "owner-pages")
    _configure(home, routing_prompt_chars=400)
    words = ("codex|owner-words " * 65).strip()
    routing.add_rule(words, via="app")
    args = {"since": "bad"} if error else {"since": "0"}
    _, body = _call("khipu_route", args)
    chunks = []
    for _ in range(12):
        changes = body.get("routing_changes")
        if not changes:
            break
        rule_items = [i for i in changes["items"] if i["entity"].startswith("rule:")]
        chunks.extend(i["text"] for i in rule_items)
        assert all("[redacted]" not in i["text"] for i in rule_items)
        _, body = _call("khipu_route", dict(args, ack=changes["batch"]))
    assert "".join(chunks) == "1. " + words
    entry = next(e for k, e in routing_delivery.snapshot("codex:owner-pages", "mcp")["entries"].items()
                 if k.startswith("rule:"))
    assert entry["acked"]


def test_session_start_writer_uses_shared_identity(home, monkeypatch):
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    monkeypatch.setenv("CODEX_THREAD_ID", "native")
    monkeypatch.setenv("KHIPU_SESSION_ID", "t3:shared")
    routing.add_rule("shared conversation", via="app")
    start = recall_rule._RoutingAtStart(session_id="native")
    start.out = routing.start_push(budget=1.0)
    assert start.out["routing_changes"]
    start.delivered("native")
    _route()
    assert routing_delivery.snapshot("t3:shared", "main").get("acked_through") is not None
    assert len(list(routing_delivery._path("t3:shared").parent.glob("*.routing.json"))) == 1


@pytest.mark.parametrize("shared", [False, True])
def test_helper_ipc_reserves_and_hook_acks_mcp_identity(home, monkeypatch, shared):
    _configure(home, routing_prompt_budget_ms=1000, routing_reset_check=False)
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    routing.add_rule("one ledger across helper and hook", via="app")
    helper = recall_prompt.RoutingForkServer()
    helper.prepare()
    assert helper.wait_ready(10)
    try:
        # The helper was launched with no native identity. The caller has one.
        env = dict(os.environ, CODEX_THREAD_ID="native")
        if shared:
            env["KHIPU_SESSION_ID"] = "t3:shared"
        at = recall_prompt._RoutingAtPrompt().begin("native", env, helper)
        out = at.collect()
        assert out and out.get("routing_changes"), (at.status, out)
        _, info = at.add_to("", out)
        recall_prompt.finish_delivery({"routing": info}, delivered=True)
        monkeypatch.setenv("CODEX_THREAD_ID", "native")
        if shared:
            monkeypatch.setenv("KHIPU_SESSION_ID", "t3:shared")
        _route()
        key = "t3:shared" if shared else "codex:native"
        assert routing_delivery.snapshot(key, "main").get("acked_through") is not None
        files = list(routing_delivery._path(key).parent.glob("*.routing.json"))
        assert len(files) == 1
    finally:
        helper.stop()


def test_explicit_id_cannot_borrow_a_custom_shared_root(home, monkeypatch):
    routing.add_rule("shared debt", via="app")
    monkeypatch.setenv("KHIPU_SESSION_ID", "client:owner")
    batch = _route()["routing_changes"]["batch"]
    monkeypatch.delenv("KHIPU_SESSION_ID")
    error, body = _call("khipu_route", {"session_id": "client:owner", "ack": batch})
    assert error and body["code"] == "ack_not_yours", body
    monkeypatch.setenv("KHIPU_SESSION_ID", "client:owner")
    assert _route()["routing_changes"]["batch"] == batch


def test_complete_table_failure_with_existing_inventory_consumes_nothing(home, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "inventory")
    login = home["tmp"] / ".codex-privateAliasZZ"
    login.mkdir()
    routing.add_rule("owner debt", via="app")
    batch = _route()["routing_changes"]["batch"]
    monkeypatch.setattr(agent_view, "_rules", lambda *a, **kw: (_ for _ in ()).throw(OSError("unavailable")))
    error, body = _call("khipu_route", {"ack": batch})
    assert not error and body.get("available") is False, body
    assert routing_delivery.snapshot("codex:inventory", "mcp")["flight"]["out"]["routing_changes"]["batch"] == batch


def test_legacy_withheld_batch_cannot_ack_original_characters(home, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "legacy")
    routing.request_rule("Use codex|unknown-login", author={"kind": "conversation", "harness": "codex"})
    view = routing.read_view()
    # Reserve as the old writer did, before its final MCP projection.
    with monkeypatch.context() as old:
        old.setattr(routing, "_for_agents", lambda value, view=None: value)
        planned = routing_delivery.plan({"kind": "full", "text": "", "version": view["version"],
                                         "epoch": view["epoch"]}, view, home["clock"].now, routing.settings())
        changes, _ = routing_delivery.pull_offer(planned, session="codex:legacy")
    _route(ack=changes["batch"])
    entry = next(e for k, e in routing_delivery.snapshot("codex:legacy", "mcp")["entries"].items()
                 if k.startswith("rule:"))
    assert not entry["acked"] and entry["offset"] == 0
    assert changes["batch"] not in routing_delivery.snapshot("codex:legacy", "mcp").get("batches", {})


def test_stale_owner_exemption_cannot_authorize_pending_page(home, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "stale-owner")
    words = "codex|OldOwnerWords"
    routing.request_rule("Use " + words, author={"kind": "conversation", "harness": "codex"})
    # A helper's saved view can precede the fold performed by this pull.
    monkeypatch.setattr(agent_view, "_saved_view", lambda: {"note": {"text": words}})
    body = _route(since="0")
    assert words not in json.dumps(body)
    assert not any(i["entity"].startswith("rule:") for i in body.get("routing_changes", {}).get("items", []))


def test_new_fold_account_alias_is_in_the_pull_table(home, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "fresh-alias")
    alias = "SecretAliasZZZ"
    key = "codex|/logins/" + alias
    routing._write({"type": "evidence", "account": key, "state": "limited", "basis": "estimated",
                    "until": "2026-10-20T16:00:00Z", "source": "conversation",
                    "written_by": {"kind": "conversation", "harness": "codex"}})
    routing.request_rule("The " + alias + " lane is busy", author={"kind": "conversation", "harness": "codex"})
    monkeypatch.setattr(agent_view, "_saved_view", lambda: {})
    assert key in [a["key"] for a in routing.read_view()["accounts"]]
    body = _route(since="0")
    assert alias not in json.dumps(body)
