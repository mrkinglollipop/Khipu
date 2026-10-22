"""Regression contracts for audit F01–F17; every I/O boundary is synthetic."""
from __future__ import annotations

import copy
import json
from contextlib import nullcontext
from datetime import datetime, timezone
from unittest import mock

import psycopg.errors
import pytest

from khipu import mcp_server as ms, owner_writes


def call(name, args):
    response = ms.handle_message({"jsonrpc": "2.0", "id": "audit", "method": "tools/call",
                                  "params": {"name": name, "arguments": args}})
    result = response["result"]
    return result["isError"], json.loads(result["content"][0]["text"])


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setattr(ms, "_RELOAD_ON_CHANGE", False)
    monkeypatch.setattr(ms, "_via_https_gateway", lambda: False)


def malformed_cases():
    # Validate the advertised surface, including tools outside the original 18.
    for tool in ms.TOOLS:
        schema = tool["inputSchema"]
        base = {}
        for key in schema.get("required", []):
            prop = schema["properties"][key]
            typ = prop.get("type")
            base[key] = (prop["enum"][0] if prop.get("enum") else
                         {"account": "Codex"} if key == "args" else
                         [{"account": "Codex"}] if key == "entries" else
                         1 if typ == "integer" else "fixture")
        for key, prop in schema.get("properties", {}).items():
            types = prop.get("type", [])
            types = [types] if isinstance(types, str) else types
            bad = [] if "object" in types else {} if "array" in types else None
            if bad is None:
                bad = 2 if types == ["boolean"] else True if "integer" in types else []
            yield tool["name"], {**base, key: bad}, key
        for key in schema.get("required", []):
            yield tool["name"], {k: v for k, v in base.items() if k != key}, key
        explicit = {"khipu_route_advise": {"tier", "kind", "purpose"},
                    "khipu_status": {"prompt"}, "khipu_route_report": {"action"}}
        for key, prop in schema.get("properties", {}).items():
            if prop.get("type") == "string" and (key in schema.get("required", [])
                                                 or key in explicit.get(tool["name"], set())):
                yield tool["name"], {**base, key: "   "}, key


@pytest.mark.parametrize("name,args,field", list(malformed_cases()))
def test_schema_rejections_happen_before_handler_io(monkeypatch, name, args, field):
    # A boundary spy prevents malformed requests from reaching any actual tool.
    boundary = mock.Mock(return_value={"ok": True})
    monkeypatch.setitem(ms.TOOL_FUNCS, name, boundary)
    monkeypatch.setattr(ms, "_via_https_gateway", lambda: True)
    failed, payload = call(name, args)
    assert failed, (name, field, payload)
    assert field in payload.get("error", ""), payload
    boundary.assert_not_called()


@pytest.mark.parametrize("args", [[], "", False, 0, None, [["query", "fixture"]]])
def test_arguments_must_be_an_object_even_when_falsey(monkeypatch, args):
    boundary = mock.Mock(return_value={"ok": True})
    monkeypatch.setitem(ms.TOOL_FUNCS, "khipu_search", boundary)
    response = ms.handle_message({"jsonrpc": "2.0", "id": "audit", "method": "tools/call",
                                  "params": {"name": "khipu_search", "arguments": args}})
    assert response["error"]["code"] == -32602
    assert "arguments" in response["error"]["message"]
    boundary.assert_not_called()


@pytest.mark.parametrize("name,args", [
    ("khipu_capture", {"summary": "fixture", "topics": [], "people": [], "decisions": [], "preferences": []}),
    ("khipu_owed_update", {"action": "close", "id": "12"}),
    ("khipu_decisions_update", {"action": "supersede", "id": "12", "by": "13"}),
    ("khipu_decisions_update", {"action": "confirm", "link_id": "12"}),
    ("khipu_route", {"since": 12}),
    ("khipu_status", {}),
])
def test_documented_compatibility_and_optional_defaults(monkeypatch, name, args):
    boundary = mock.Mock(return_value={"ok": True})
    monkeypatch.setitem(ms.TOOL_FUNCS, name, boundary)
    monkeypatch.setattr(ms, "_via_https_gateway", lambda: True)
    assert call(name, args)[0] is False
    boundary.assert_called_once()


@pytest.mark.parametrize("value", [False, 0, [], {}, ""])
def test_f01_invalid_report_action_never_reaches_writer(value):
    with mock.patch("khipu.routing.conversation_write") as writer:
        with pytest.raises(ValueError):
            ms._tool_route_report({"action": value, "account": "Codex", "state": "open"})
    writer.assert_not_called()


@pytest.mark.parametrize("value", [{"madeup": 123}, 1, True, [], "", "  "])
def test_f02_replacement_text_rejected_before_owner_write(value):
    with mock.patch("khipu.owner_writes.run") as writer:
        with pytest.raises(ValueError):
            ms._tool_decisions_update({"action": "supersede", "id": 10, "new_text": value})
    writer.assert_not_called()


class Cursor:
    def __init__(self, failure=None):
        self.failure = failure
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append((sql, params))
        if sql.startswith(("SELECT ", "WITH ")) and self.failure:
            raise self.failure

    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Connection:
    def __init__(self, cur=None):
        self.cur = cur or Cursor()

    def cursor(self):
        return self.cur

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.mark.parametrize("failure", [psycopg.errors.QueryCanceled("fixture timeout"),
                                    psycopg.errors.InsufficientPrivilege("fixture denied")])
def test_f03_project_queries_propagate_unexpected_failures(failure):
    from khipu import identity, project_group
    for reader in (lambda cur: project_group._savepoint(cur, "fixture", "SELECT count(*)", ()),
                   lambda cur: identity.project_canonical_group(cur, "old"),
                   lambda cur: identity._resolve_project_alias(cur, "old")):
        cur = Cursor(failure)
        with pytest.raises(type(failure)):
            reader(cur)
        assert any(sql.startswith("ROLLBACK TO SAVEPOINT") for sql, _ in cur.sql)


def test_f03_missing_schema_compatibility_survives():
    from khipu import identity, project_group
    assert project_group._savepoint(Cursor(psycopg.errors.UndefinedTable()), "fixture", "SELECT 1", ()) == []
    assert identity.project_canonical_group(Cursor(psycopg.errors.UndefinedTable()), "old") == ("old", ["old"])


def direction(identity="rd-0123456789abcdef", project="fixture/alpha", at="2090-01-01T00:00:00+00:00"):
    return {"id": identity, "text": "Routing said here", "project": project, "at": at}


def commitment(identity=1, project="fixture/alpha"):
    return {"id": identity, "text": "follow up", "owner": "user", "owner_resolved": True,
            "kind": "followup", "project": project, "opened_at": "2090-01-01T00:00:00+00:00"}


def owed_fixture(monkeypatch, directions, rows):
    monkeypatch.setattr("khipu.db.connect", lambda **kw: Connection())
    monkeypatch.setattr("khipu.commitments.fetch_owed_rows", lambda *a, **kw: copy.deepcopy(rows))
    monkeypatch.setattr("khipu.routing_directions.owed_items", lambda: copy.deepcopy(directions))
    monkeypatch.setattr("khipu.owner_writes.annotate", lambda *a: {})
    monkeypatch.setattr("khipu.identity.project_canonical_group",
                        lambda cur, name: ("fixture/alpha", ["fixture/alpha", "fixture/old"]) if name == "fixture/old"
                        else (name, [name]))


def test_f04_direction_filter_uses_project_group_and_keeps_project(monkeypatch):
    owed_fixture(monkeypatch, [direction(project="fixture/old"), direction("rd-1123456789abcdef", "fixture/beta")], [])
    out = ms._tool_owed({"project": "fixture/old"})
    assert [r["id"] for r in out["results"]] == ["rd-0123456789abcdef"]
    assert out["results"][0]["project"] == "fixture/alpha"
    assert out["project"] == "fixture/alpha"


def test_f05_direction_and_commitment_share_bounded_keyset_pages(monkeypatch):
    owed_fixture(monkeypatch, [direction(), direction("rd-1123456789abcdef")], [commitment(), commitment(2)])
    ids, cursor = [], None
    for _ in range(5):
        out = ms._tool_owed({"limit": 1, **({"cursor": cursor} if cursor else {})})
        assert len(out["results"]) <= 1
        assert out["total"] == 4
        ids.extend(r["id"] for r in out["results"])
        cursor = out["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert len(ids) == len(set(ids)) == 4
    assert set(ids) == {1, 2, "rd-0123456789abcdef", "rd-1123456789abcdef"}


def test_f06_listed_direction_can_be_closed_without_commitment_write():
    with mock.patch("khipu.routing_directions.dismiss", return_value={"id": "rd-0123456789abcdef", "status": "dismissed"}) as dismiss, \
            mock.patch("khipu.owner_writes.run", wraps=owner_writes.run) as writer:
        failed, out = call("khipu_owed_update", {"action": "close", "id": "rd-0123456789abcdef"})
    assert not failed and out["ok"] is True, out
    dismiss.assert_called_once_with("rd-0123456789abcdef")
    writer.assert_called_once_with("owed_update", {"id": "rd-0123456789abcdef", "action": "close"}, queue_ok=False)


def test_f06_missing_direction_returns_tool_error():
    with mock.patch("khipu.routing_directions.dismiss", return_value=None):
        failed, out = call("khipu_owed_update", {"action": "close", "id": "rd-0123456789abcdef"})
    assert failed and out.get("error")


def test_f07_conflicts_since_reaches_sql():
    from khipu import decisions
    cur = Cursor()
    cutoff = datetime(2099, 1, 1, tzinfo=timezone.utc)
    with mock.patch("khipu.db.connect", return_value=Connection(cur)), \
            mock.patch.object(decisions, "_links_ready", return_value=True):
        ms._tool_decisions({"status": "conflicts", "since": "2099-01-01"})
    sql, params = cur.sql[-1]
    assert "l.created_at >=" in sql
    assert cutoff in params


@pytest.mark.parametrize("tool,args", [
    ("khipu_owed_update", {"action": "close", "id": 1}),
    ("khipu_owed_update", {"action": "close", "ids": [1, 2], "reason": "fixture done"}),
    ("khipu_decisions_update", {"action": "restore", "id": 123}),
])
def test_f08_terminal_refusals_are_tool_errors(tool, args):
    with mock.patch("khipu.owner_writes.run", return_value={"ok": False, "needs_owner": [1, 2]}), \
            mock.patch.object(ms, "_owed_owners_items", return_value=[]):
        failed, out = call(tool, args)
    assert failed and out["ok"] is False and out.get("error"), out


def test_f08_owner_gate_refusal_is_tool_error():
    with mock.patch.object(ms, "_owed_owners_items", return_value=[1, 2]), \
            mock.patch("khipu.commitments.bulk_owner_gate", return_value=None), \
            mock.patch("khipu.owner_writes.run") as writer:
        failed, out = call("khipu_owed_update", {"action": "close", "ids": [1, 2], "reason": "fixture"})
    assert failed and out["needs_owner"] == [1, 2]
    writer.assert_not_called()


def test_f08_queued_result_is_still_success():
    with mock.patch("khipu.owner_writes.run", return_value={"ok": True, "waiting_to_sync": True}):
        failed, out = call("khipu_decisions_update", {"action": "restore", "id": 123})
    assert not failed and out["waiting_to_sync"]


def test_f09_trivial_capture_skips_before_hook_queue_or_write():
    with mock.patch.object(ms, "_stdio_hook_owns_capture", return_value=True), \
            mock.patch("khipu.session_capture.request_capture_now") as queue, \
            mock.patch("khipu.capture.capture") as writer:
        out = ms._tool_capture({"summary": "fixture", "scope": "trivial", "session_id": "codex:fixture"})
    assert out["skipped"] is True and not out.get("queued")
    queue.assert_not_called()
    writer.assert_not_called()


@pytest.mark.parametrize("field,value", [("topics", 12), ("people", {}), ("decisions", True),
                                         ("preferences", "oops"), ("project", []), ("scope", False),
                                         ("topics", ["okay", 12])])
def test_f10_capture_metadata_rejected_before_queue(field, value):
    with mock.patch.object(ms, "_stdio_hook_owns_capture", return_value=True), \
            mock.patch("khipu.session_capture.request_capture_now") as queue:
        failed, out = call("khipu_capture", {"summary": "fixture", field: value})
    assert failed and field in out["error"]
    queue.assert_not_called()


@pytest.mark.parametrize("budget,expected", [(-1, 100), (0, 100), (10000, 5000), (None, 600)])
def test_f15_status_budget_clamps_provided_integers(budget, expected):
    args = {"prompt": "fixture prompt", **({"budget_ms": budget} if budget is not None else {})}
    with mock.patch("khipu.recall_prompt.prior_work_for_prompt",
                    return_value={"hits": [], "context": "", "reason": ""}) as recall:
        ms._attach_prior_work({}, args)
    assert recall.call_args.kwargs["budget_ms"] == expected


@pytest.mark.parametrize("semantic", [False, True])
@pytest.mark.parametrize("rows,confidence", [([], "none"), ([{"kind": "episode", "id": 1, "label": "orchid fixture",
                                                          "snippet": "orchid fixture"}], "strong")])
def test_f12_offline_search_exposes_honest_signals(monkeypatch, semantic, rows, confidence):
    from khipu import hub_snapshot as hs
    monkeypatch.setattr(hs, "open_snapshot", lambda: mock.Mock())
    monkeypatch.setattr(hs, "_snapshot_filters_dropped", lambda **kw: [])
    monkeypatch.setattr(hs, "search_snapshot", lambda *a, **kw: copy.deepcopy(rows))
    monkeypatch.setattr(hs, "semantic_search_snapshot", lambda *a, **kw: copy.deepcopy(rows))
    monkeypatch.setattr(hs, "merge_outbox_episodes", lambda results, **kw: results)
    monkeypatch.setattr(hs, "_graph_candidates_snapshot", lambda con, results: (results, []))
    monkeypatch.setattr(hs, "_filter_expanded_snapshot_rows", lambda con, results, **kw: results)
    monkeypatch.setattr(hs, "enrich_search_results_snapshot", lambda con, results: results)
    monkeypatch.setattr(hs, "_annotate_snapshot_validity", lambda con, results: results)
    monkeypatch.setattr(hs, "stale_fields", lambda: {"stale": True})
    monkeypatch.setattr("khipu.relevance.enabled", lambda: False)
    out = hs.search_stale_payload("orchid", 10, semantic=semantic)
    assert out["confidence"] == confidence and out["stale"]
    for row in out["results"]:
        assert row["lexical_hits"] == 1 and row["cosine"] is None
        assert isinstance(row["score"], (float, int))


@pytest.mark.parametrize("entry", ["Codex", {}, {"account": 12}, {"account": ""}, {"account": "Codex", "role": 12}])
def test_f13_priority_entries_are_schema_objects(entry):
    with mock.patch("khipu.routing.conversation_priority") as writer:
        failed, out = call("khipu_route_priority", {"entries": [entry], "base": "fixture", "owner_request": "fixture"})
    assert failed, out
    writer.assert_not_called()


@pytest.mark.parametrize("field", ["tier", "kind", "purpose"])
def test_f14_blank_advice_enum_rejected(field):
    with mock.patch("khipu.routing.advise") as advice:
        failed, out = call("khipu_route_advise", {field: ""})
    assert failed and field in out["error"]
    advice.assert_not_called()


def test_f16_malformed_ledger_note_never_reaches_removal():
    with mock.patch("khipu.ledger.remove_ref") as remove:
        failed, out = call("khipu_forget", {"id": "ledger:fixture#one", "note": {"reason": "fixture"}})
    assert failed and "note" in out["error"]
    remove.assert_not_called()
