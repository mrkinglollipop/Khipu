"""``forget.list`` through ``khipu_owner_act``: a forgotten episode's hub
summary reaches only a conversation the owner is typing in. A background job
still sees what is listed (ids, strings, count), never the text."""
from __future__ import annotations

import json
from unittest import mock

from khipu import mcp_server, owner_direction

LISTING = {"ok": True, "strings": ["k:" + "cd" * 32], "ids": [{"id": 7, "summary": "SECRET FORGOTTEN SUMMARY"}],
           "count": 2, "facts_hidden": True}


def _call(name: str, arguments: dict) -> dict:
    out = mcp_server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": name, "arguments": arguments}})
    return out["result"]


def _listed() -> dict:
    with mock.patch("khipu.forget.unsent_forgets", return_value=json.loads(json.dumps(LISTING))):
        return _call("khipu_owner_act", {"action": "forget.list", "args": {}})


def test_forget_list_without_an_owner_turn_does_not_return_summaries(monkeypatch):
    monkeypatch.setattr(owner_direction, "check", mock.Mock(
        side_effect=owner_direction.DirectionRefused("no_recorded_turn", "no owner turn")))
    result = _listed()
    assert "SECRET FORGOTTEN SUMMARY" not in json.dumps(result)
    reply = json.loads(result["content"][0]["text"])
    assert reply["ids"] == [{"id": 7}] and reply["strings"] == LISTING["strings"] and reply["count"] == 2


def test_forget_list_in_the_owners_conversation_keeps_summaries(monkeypatch):
    monkeypatch.setattr(owner_direction, "check", mock.Mock(return_value={"auth": "direction"}))
    assert "SECRET FORGOTTEN SUMMARY" in json.dumps(_listed())


def _refused(monkeypatch):
    monkeypatch.setattr(owner_direction, "check", mock.Mock(
        side_effect=owner_direction.DirectionRefused("no_recorded_turn", "no owner turn")))


def test_forget_list_redacts_on_any_check_error_and_strips_extra_fields(monkeypatch):
    monkeypatch.setattr(owner_direction, "check", mock.Mock(side_effect=RuntimeError("boom")))
    listing = dict(LISTING, ids=[{"id": 7, "summary": "SECRET FORGOTTEN SUMMARY", "raw": "SECRET RAW"}])
    with mock.patch("khipu.forget.unsent_forgets", return_value=listing):
        result = _call("khipu_owner_act", {"action": "forget.list", "args": {}})
    assert "SECRET" not in json.dumps(result)
    assert json.loads(result["content"][0]["text"])["ids"] == [{"id": 7}]


def test_forget_list_drops_a_malformed_item_instead_of_failing(monkeypatch):
    _refused(monkeypatch)
    listing = dict(LISTING, ids=["SECRET FORGOTTEN SUMMARY", {"id": 7, "summary": "SECRET FORGOTTEN SUMMARY"}])
    with mock.patch("khipu.forget.unsent_forgets", return_value=listing):
        result = _call("khipu_owner_act", {"action": "forget.list", "args": {}})
    assert "SECRET" not in json.dumps(result)
    assert json.loads(result["content"][0]["text"])["ids"] == [{"id": 7}]


# ---- khipu_get: a forgotten episode the hub still holds is not returned --------------


def _gate(ids):
    from khipu import tombstones

    return tombstones.Gate({"source_keys": [], "payload_hashes": [], "legacy_identities": [],
                            "episode_ids": list(ids)})


ROW = {"id": 7, "summary": "SECRET FORGOTTEN SUMMARY", "raw": "SECRET RAW", "verbatim": "SECRET VERBATIM"}


def _get(ident="7", **extra) -> dict:
    with mock.patch("khipu.activity.episode_detail", return_value=dict(ROW)), \
            mock.patch("khipu.hub_snapshot.episode_detail_snapshot", return_value=dict(ROW)), \
            mock.patch("khipu.decisions.decision_states_for_episode", return_value={}):
        return mcp_server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                          "params": {"name": "khipu_get", "arguments": {"id": ident, **extra}}})


def test_khipu_get_does_not_return_an_episode_forgotten_on_this_mac(monkeypatch):
    monkeypatch.setattr("khipu.tombstones.read_gate", lambda: _gate([7]))
    for extra in ({}, {"kind": "episode"}):
        assert "SECRET" not in json.dumps(_get(**extra))


def test_khipu_get_fails_closed_when_the_registry_is_unreadable(monkeypatch):
    from khipu import tombstones

    def broken():
        raise tombstones.RegistryError("unreadable")

    monkeypatch.setattr("khipu.tombstones.read_gate", broken)
    assert "SECRET" not in json.dumps(_get(kind="episode"))


def test_khipu_get_still_returns_an_episode_nobody_forgot(monkeypatch):
    monkeypatch.setattr("khipu.tombstones.read_gate", lambda: _gate([8]))
    assert "SECRET FORGOTTEN SUMMARY" in json.dumps(_get(kind="episode"))


# ---- khipu episode forget-pending: the CLI read is gated the same way ----------------


def _cli_list(monkeypatch, capsys, *flags, app=False) -> str:
    from khipu import cli, routing

    monkeypatch.setattr(routing, "_caller", lambda: {"app": app, "chain": []})
    args = cli.build_parser().parse_args(["episode", "forget-pending", *flags])
    with mock.patch("khipu.forget.unsent_forgets", return_value=json.loads(json.dumps(LISTING))):
        assert cli.cmd_episode(args) == 0
    return capsys.readouterr().out


def test_cli_forget_pending_without_an_owner_turn_does_not_print_summaries(monkeypatch, capsys):
    _refused(monkeypatch)
    out = _cli_list(monkeypatch, capsys)
    assert "SECRET" not in out and json.loads(out)["ids"] == [{"id": 7}]
    # --from-app from anything but the app itself counts for nothing.
    assert "SECRET" not in _cli_list(monkeypatch, capsys, "--from-app")


def test_cli_forget_pending_keeps_summaries_for_the_app_and_the_owner(monkeypatch, capsys):
    _refused(monkeypatch)
    assert "SECRET FORGOTTEN SUMMARY" in _cli_list(monkeypatch, capsys, "--from-app", app=True)
    monkeypatch.setattr(owner_direction, "check", mock.Mock(return_value={"auth": "direction"}))
    assert "SECRET FORGOTTEN SUMMARY" in _cli_list(monkeypatch, capsys)


# ---- khipu get / khipu activity --show: the CLI reads are gated like khipu_get --------


def _cli_show(argv: list[str], capsys) -> str:
    from khipu import cli

    args = cli.build_parser().parse_args(argv)
    with mock.patch("khipu.activity.episode_detail", return_value=dict(ROW)):
        args.func(args)
    captured = capsys.readouterr()
    return captured.out + captured.err


def test_cli_get_and_activity_show_hide_a_forgotten_episode(monkeypatch, capsys):
    monkeypatch.setattr("khipu.tombstones.read_gate", lambda: _gate([7]))
    for argv in (["get", "7"], ["activity", "--show", "7"]):
        assert "SECRET" not in _cli_show(argv, capsys), argv


def test_cli_get_and_activity_show_fail_closed_on_an_unreadable_registry(monkeypatch, capsys):
    from khipu import tombstones

    def broken():
        raise tombstones.RegistryError("unreadable")

    monkeypatch.setattr("khipu.tombstones.read_gate", broken)
    for argv in (["get", "7"], ["activity", "--show", "7"]):
        assert "SECRET" not in _cli_show(argv, capsys), argv


def test_cli_get_and_activity_show_still_print_an_episode_nobody_forgot(monkeypatch, capsys):
    monkeypatch.setattr("khipu.tombstones.read_gate", lambda: _gate([8]))
    for argv in (["get", "7"], ["activity", "--show", "7"]):
        assert "SECRET FORGOTTEN SUMMARY" in _cli_show(argv, capsys), argv


# ---- khipu_search: hub hits for a forgotten episode are dropped -----------------------


HITS = {"query": "q", "mode": "hybrid", "results": [
    {"kind": "episode", "id": "7", "snippet": "SECRET FORGOTTEN SUMMARY"},
    {"kind": "episode", "id": 9, "snippet": "kept episode"},
    {"kind": "topic", "id": "khipu", "snippet": "kept topic"}]}


def _search() -> dict:
    with mock.patch("khipu.embed.hybrid_search", return_value=json.loads(json.dumps(HITS))), \
            mock.patch("khipu.query_log.log_query"):
        out = _call("khipu_search", {"query": "q"})
    return json.loads(out["content"][0]["text"])


def test_khipu_search_drops_a_forgotten_episode_hit(monkeypatch):
    monkeypatch.setattr("khipu.tombstones.read_gate", lambda: _gate([7]))
    reply = _search()
    assert "SECRET" not in json.dumps(reply)
    assert [r["id"] for r in reply["results"]] == [9, "khipu"]


def test_khipu_search_drops_hub_episode_hits_when_the_registry_is_unreadable(monkeypatch):
    from khipu import tombstones

    def broken():
        raise tombstones.RegistryError("unreadable")

    monkeypatch.setattr("khipu.tombstones.read_gate", broken)
    assert [r["id"] for r in _search()["results"]] == ["khipu"]


# ---- hybrid_search and the budgeted hub lane: every other search route --------------
# `khipu search`, khipu_reflect and the per-prompt recall push all read
# hybrid_search; khipu_status's prior work reads _hub_hits_budgeted.


def _hub_rows() -> list[dict]:
    return [{"kind": "episode", "id": "7", "label": "e7", "snippet": "SECRET FORGOTTEN SUMMARY",
             "score": 0.95, "rank_text": "widget batching endpoint SECRET", "project": "p"},
            {"kind": "episode", "id": "8", "label": "e8", "snippet": "kept episode",
             "score": 0.9, "rank_text": "widget batching endpoint kept", "project": "p"},
            {"kind": "topic", "id": "khipu", "label": "t", "snippet": "kept topic",
             "score": 0.85, "rank_text": "widget batching endpoint topic"}]


class _Conn:
    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_hub():
    from contextlib import ExitStack

    from khipu import embed

    stack = ExitStack()
    for target, kw in (
            ("khipu.embed._cosine_candidates", {"side_effect": lambda *a, **k: [dict(r) for r in _hub_rows()]}),
            ("khipu.cli._literal_candidates", {"side_effect": lambda *a, **k: [dict(r) for r in _hub_rows()]}),
            ("khipu.hub_snapshot.try_hub_connect", {"return_value": _Conn()}),
            ("khipu.db.connect", {"return_value": _Conn()}),
            ("khipu.topic_graph.enrich_search_results", {"side_effect": lambda cur, r: list(r)}),
            ("khipu.decisions.enrich_search_results", {"side_effect": lambda cur, r: list(r)}),
            ("khipu.library_search.enabled_libraries", {"return_value": []}),
            ("khipu.query_log.log_query", {})):
        stack.enter_context(mock.patch(target, **kw))
    stack.enter_context(mock.patch.object(embed, "_apply_search_filters", side_effect=lambda cur, r, **k: r))
    return stack


def _routes(capsys) -> dict[str, str]:
    from khipu import cli, embed, recall_prompt, reflect

    out = {}
    with _fake_hub():
        for mode in ("hybrid", "semantic", "literal"):
            out[f"hybrid_search:{mode}"] = json.dumps(
                embed.hybrid_search("widget batching endpoint", limit=8, mode=mode))
        args = cli.build_parser().parse_args(["search", "widget batching endpoint"])
        args.func(args)
        out["khipu search"] = capsys.readouterr().out
        out["khipu_reflect"] = json.dumps(reflect._search("widget batching endpoint", None, 8))
        out["prior work"] = json.dumps(recall_prompt._hub_hits_budgeted(
            "widget batching endpoint", ["widget", "batching", "endpoint"], project=None,
            budget_ms=2000, limit=8))
    return out


def test_every_search_route_drops_a_forgotten_episode(monkeypatch, capsys):
    monkeypatch.setattr("khipu.tombstones.read_gate", lambda: _gate([7]))
    routes = _routes(capsys)
    assert [r for r, text in routes.items() if "SECRET" in text] == []
    assert [r for r, text in routes.items() if "kept episode" not in text] == []


def test_every_search_route_drops_hub_episodes_when_the_registry_is_unreadable(monkeypatch, capsys):
    from khipu import tombstones

    def broken():
        raise tombstones.RegistryError("unreadable")

    monkeypatch.setattr("khipu.tombstones.read_gate", broken)
    assert [r for r, text in _routes(capsys).items() if "SECRET" in text or "kept episode" in text] == []


def test_every_search_route_still_returns_an_episode_nobody_forgot(monkeypatch, capsys):
    monkeypatch.setattr("khipu.tombstones.read_gate", lambda: _gate([9]))
    assert [r for r, text in _routes(capsys).items() if "SECRET FORGOTTEN SUMMARY" not in text] == []
