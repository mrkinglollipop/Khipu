"""Compact ``view`` on ``khipu_route`` and ``khipu route --view``.

Hermetic: own data dir, fake clock, no hub. Covers summary size, section
views, default byte-identity, and delivery/ack parity with the full answer.
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone

import pytest

from khipu import cli
from khipu import mcp_server as ms
from khipu import routing
from khipu.mcp_server import handle_message

T0 = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
ACCOUNTS = [{"label": "Claude", "harness": "claude"},
            {"label": "Codex", "harness": "codex"},
            {"label": "Grok", "harness": "grok"}]
_HARNESS_ENV = ("KHIPU_HARNESS", "KHIPU_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID",
                "CLAUDE_CODE_ENTRYPOINT", "CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_HOME", "GROK_HOOK_NAME",
                "GROK_HOOK_EVENT", ms.GATEWAY_ACTIVE_ENV)
_REQUEST_IDS = itertools.count(1)


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def home(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    for name in _HARNESS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ms, "_GATEWAY_ACTIVE", False)
    project = tmp_path / "project-z"
    project.mkdir()
    monkeypatch.chdir(project)
    clock = Clock(T0)
    monkeypatch.setattr(routing, "_now", clock)
    return {"data": data, "clock": clock, "tmp": tmp_path}


def _call(name: str, arguments: dict | None = None) -> tuple[bool, dict]:
    out = handle_message({"jsonrpc": "2.0", "id": next(_REQUEST_IDS), "method": "tools/call",
                          "params": {"name": name, "arguments": arguments or {}}})
    result = out["result"]
    return result["isError"], json.loads(result["content"][0]["text"])


def _route(**arguments) -> dict:
    is_error, body = _call("khipu_route", arguments)
    assert not is_error, body
    return body


def _fat_answer() -> dict:
    """A large synthetic route answer (today-sized and then some)."""
    long = "x" * 4000
    accounts = []
    for i in range(12):
        accounts.append({
            "label": f"Account-{i}", "state": "limited", "until": "2026-10-14T20:00:00Z",
            "line": long, "models": [{"model": f"m{j}", "evidence": long} for j in range(8)],
            "witness": {"evidence": long, "id": f"w{i}"},
            "pace": [
                {"window": "five_hour", "used_pct": 80.0 + i, "cycle_resets_at": "2026-10-14T20:00:00Z",
                 "plan": {"verdict": "on_pace", "needed_pct_per_h": 1.0, "runs_out_at": None},
                 "share_cap": {"verdict": "near", "pct": 90}, "why": long},
                {"window": "weekly", "used_pct": 40.0, "cycle_resets_at": "2026-10-20T00:00:00Z",
                 "plan": {"verdict": "underpacing", "needed_pct_per_h": 0.5},
                 "day_cap": {"verdict": "ok"}, "buckets": list(range(100))},
            ],
            "advice": {"verdict": "go", "reason": long},
            "cap_statuses": [{"kind": "spend", "verdict": "unknown", "reported": long}],
        })
    rules = [{"entity": f"rule:{i}", "n": i, "text": f"Standing rule {i}: " + long} for i in range(20)]
    holds = [{"id": f"h{i}", "scope": {"kind": "account", "account": f"acct-{i}"},
              "until": "2026-10-15T00:00:00Z", "text": "hold words " + long} for i in range(15)]
    return {
        "schema": 1, "epoch": "e1", "version": 9, "generated_at": "2026-10-14T15:00:00Z",
        "accounts": accounts,
        "priority": {"entries": [{"label": f"Account-{i}", "id": f"id-{i}", "role": f"role-{i}",
                                  "until": None} for i in range(8)],
                     "version": "p3", "pending": False, "confirmed": True,
                     "line": long, "owner_request": long},
        "rules": rules,
        "holds": holds,
        "note": {"text": long, "at": "2026-10-14T12:00:00Z", "via": "app"},
        "proposals": [{"id": f"p{i}", "kind": "rule", "status": "pending", "base": long} for i in range(10)],
        "providers": [{"name": f"p{i}", "models_allowed": [long] * 5} for i in range(6)],
        "sources": {"runners": [{"name": f"r{i}", "files": {long: True}} for i in range(6)]},
        "unmatched": [{"lane": f"l{i}", "line": long} for i in range(6)],
        "about": routing.ABOUT,
        "plan_facts": {f"k{i}": {"words": long, "used_pct": 50} for i in range(12)},
        "tier_table": {"rows": [long] * 20},
        "contradictions": [long] * 10,
        "rule_hygiene": [long] * 10,
    }


def test_summary_of_a_large_record_stays_under_16kb():
    fat = _fat_answer()
    assert len(json.dumps(fat, separators=(",", ":"))) > 16000
    compact = routing.project_route_view(fat, "summary")
    blob = json.dumps(compact, separators=(",", ":"))
    assert len(blob.encode("utf-8")) < 16000, len(blob)
    assert set(compact) == {"accounts", "priority", "rules", "holds", "omitted"}
    assert "providers" in compact["omitted"] and "note" in compact["omitted"]
    first = compact["accounts"][0]
    assert set(first) == {"label", "state", "until", "pace", "advice"}
    assert first["pace"][0] == {"window": "five_hour", "used_pct": 80.0,
                                "cycle_resets_at": "2026-10-14T20:00:00Z",
                                "plan": {"verdict": "on_pace"}}
    assert compact["rules"][0]["text"] == fat["rules"][0]["text"][:120]
    assert compact["holds"][0]["text"] == fat["holds"][0]["text"][:120]
    assert compact["priority"]["entries"][0] == {
        "account": "Account-0", "role": "role-0", "until": None}
    assert compact["priority"]["version"] == "p3"
    assert compact["priority"]["pending"] is False and compact["priority"]["confirmed"] is True


def test_section_view_returns_that_section_in_full():
    fat = _fat_answer()
    for name in ("accounts", "priority", "rules", "holds", "note", "proposals"):
        assert routing.project_route_view(fat, name) == {name: fat[name]}


def test_default_khipu_route_matches_conversation_view_bytes(home):
    routing.add_rule("Every result that is acted on gets a re-check from another model family.", via="app")
    routing.set_note("Builds go to Codex this week.", via="app")
    routing.report(state="limited", account="Codex", until="1h",
                   author={"kind": "conversation", "harness": "codex", "session": "s"})
    routing.add_hold("disk low", reason="disk-low", scope="all",
                     author={"kind": "conversation", "harness": "codex", "session": "s"})
    # Strip delivery fields: they are issued per call; the record body must
    # match conversation_view byte-for-byte when view is omitted.
    full = _route()
    body = {k: v for k, v in full.items() if k not in ("routing_changes", "delivery")}
    expected = routing.conversation_view()
    assert json.dumps(body, sort_keys=True, separators=(",", ":")) == json.dumps(
        expected, sort_keys=True, separators=(",", ":"))
    assert "omitted" not in full


def test_khipu_route_summary_and_section_through_mcp(home, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "view-sess")
    routing.add_rule("Never a -fast tier. " + ("rule-body-" * 30), via="app")
    routing.set_note("Owner note for the week.", via="app")
    routing.set_priority([{"account": "Codex", "role": "builds first"}], via="app",
                         expect=routing.priority_version())
    routing.report(state="limited", account="Codex", until="1h",
                   author={"kind": "conversation", "harness": "codex", "session": "view-sess"})
    routing.add_hold("keep Grok free", reason="keep", scope="account", account="Grok",
                     author={"kind": "conversation", "harness": "codex", "session": "view-sess"})
    full = _route()
    summary = _route(view="summary")
    assert set(summary) >= {"accounts", "priority", "rules", "holds", "omitted", "delivery"}
    assert "routing_changes" in summary
    assert summary["delivery"] == full["delivery"]
    assert summary["routing_changes"] == full["routing_changes"]
    assert "providers" in summary["omitted"]
    assert all(len(r["text"]) <= 120 for r in summary["rules"])
    section = _route(view="note")
    assert section["note"] == full["note"]
    assert section["delivery"] == full["delivery"]
    assert section["routing_changes"] == full["routing_changes"]
    accounts = _route(view="accounts")
    assert accounts["accounts"] == full["accounts"]


def test_view_preserves_ack_reservation(home, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "ack-view")
    routing.add_rule("Synthetic standing rule for delivery.", via="app")
    first = _route(view="summary")
    batch = first["routing_changes"]["batch"]
    assert batch
    still = _route(view="summary")
    assert still["routing_changes"] == first["routing_changes"]
    assert still["delivery"] == first["delivery"]
    _route(view="summary", ack=batch)
    after = _route(view="summary")
    # The batch was consumed; a new offer may replace it, or the field may be
    # absent when nothing is left to deliver.
    changes = after.get("routing_changes")
    assert changes is None or changes.get("batch") != batch or changes.get("items") == []
    monkeypatch.setenv("CODEX_THREAD_ID", "ack-view")
    live = _route(view="accounts")
    live_batch = (live.get("routing_changes") or {}).get("batch") or batch
    monkeypatch.setenv("CODEX_THREAD_ID", "other")
    error, body = _call("khipu_route", {"view": "summary", "ack": live_batch})
    assert error and body.get("code") == "ack_not_yours"


def test_tool_schema_and_route_help_advertise_view():
    import argparse

    tool = next(item for item in ms.TOOLS if item["name"] == "khipu_route")
    assert tool["inputSchema"]["properties"]["view"]["enum"] == list(routing.ROUTE_VIEW_CHOICES)
    assert "view" in tool["description"]
    parser = cli.build_parser()
    route_parser = next(action.choices["route"] for action in parser._actions
                        if isinstance(action, argparse._SubParsersAction))
    view_action = next(a for a in route_parser._actions if a.dest == "view")
    assert list(view_action.choices) == list(routing.ROUTE_VIEW_CHOICES)
    assert "summary" in (view_action.help or "")
    with pytest.raises(SystemExit):
        cli.main(["route", "--view", "not-a-view"])


def test_cli_route_view_summary_and_default(home, capsys):
    routing.add_rule("Never a -fast tier. " + ("y" * 200), via="app")
    routing.set_note("CLI note.", via="app")
    assert cli.main(["route", "--json"]) == 0
    full = json.loads(capsys.readouterr().out)
    assert cli.main(["route", "--json", "--view", "summary"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert "omitted" in summary and len(json.dumps(summary).encode()) < len(json.dumps(full).encode())
    assert all(len(r.get("text") or "") <= 120 for r in summary["rules"])
    assert cli.main(["route", "--json", "--view", "rules"]) == 0
    rules_only = json.loads(capsys.readouterr().out)
    assert rules_only == {"rules": full["rules"]}
    assert cli.main(["route", "--json"]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again == full


def test_invalid_view_is_refused(home):
    error, body = _call("khipu_route", {"view": "telemetry"})
    assert error
    assert body.get("code") == "bad_args" or "view" in body.get("error", "").lower()


def test_since_plus_view_is_refused_in_mcp_and_cli(home, capsys):
    """P2: view on a since answer silently projected wrong shape; refuse instead."""
    routing.add_rule("Standing rule so the record has a version.", via="app")
    error, body = _call("khipu_route", {"since": "0", "view": "summary"})
    assert error
    err = (body.get("error") or "").lower()
    assert "since" in err and "view" in err
    assert "omitted" not in body and "entities" not in body
    error, body = _call("khipu_route", {"since": 0, "view": "rules"})
    assert error and "since" in (body.get("error") or "").lower()
    assert cli.main(["route", "--since", "0", "--view", "summary"]) == 2
    refused = json.loads(capsys.readouterr().out)
    assert refused.get("ok") is False and "since" in refused.get("error", "").lower()
    assert cli.main(["route", "--since", "0", "--json", "--view", "rules"]) == 2
    refused = json.loads(capsys.readouterr().out)
    assert refused.get("ok") is False and "view" in refused.get("error", "").lower()


def test_summary_of_a_very_large_record_stays_under_16kb():
    """P2: list counts and ids must be capped, not only text clip length."""
    long = "x" * 4000
    long_id = "rule:" + ("i" * 200)
    accounts = []
    for i in range(6):
        accounts.append({
            "label": f"Account-{i}-" + long[:80], "state": "limited",
            "until": "2026-10-14T20:00:00." + ("9" * 80) + "Z",
            "pace": [
                {"window": f"w{j}", "used_pct": float(j), "cycle_resets_at": "2026-10-14T20:00:00Z",
                 "plan": {"verdict": "on_pace", "detail": long}}
                for j in range(3)
            ],
        })
    rules = [{"entity": f"{long_id}{i}", "n": i, "text": f"Standing rule {i}: " + long}
             for i in range(200)]
    holds = [{"id": f"hold-{i}-" + ("h" * 200), "scope": {"kind": "account", "account": f"acct-{i}"},
              "until": "2026-10-15T00:00:00." + ("8" * 80) + "Z",
              "text": "hold words " + long} for i in range(100)]
    fat = {
        "schema": 1, "epoch": "e1", "version": 9, "generated_at": "2026-10-14T15:00:00Z",
        "accounts": accounts,
        "priority": {"entries": [{"label": f"Account-{i}", "id": f"id-{i}-" + ("p" * 200),
                                  "role": f"role-{i}-" + long[:200], "until": "until-" + ("u" * 200)}
                                 for i in range(40)],
                     "version": "p3", "pending": False, "confirmed": True},
        "rules": rules, "holds": holds,
        "note": {"text": long}, "proposals": [], "providers": [], "about": routing.ABOUT,
    }
    assert len(json.dumps(fat, separators=(",", ":"))) > 16000
    compact = routing.project_route_view(fat, "summary")
    blob = json.dumps(compact, separators=(",", ":")).encode("utf-8")
    assert len(blob) < 16000, len(blob)
    assert any(isinstance(r, dict) and r.get("more") for r in compact["rules"])
    assert any(isinstance(h, dict) and h.get("more") for h in compact["holds"])
    for rule in compact["rules"]:
        if "id" in rule and isinstance(rule["id"], str):
            assert len(rule["id"]) <= 120
    for hold in compact["holds"]:
        if "id" in hold and isinstance(hold["id"], str):
            assert len(hold["id"]) <= 120
        if "until" in hold and isinstance(hold["until"], str):
            assert len(hold["until"]) <= 120


def test_cli_view_refused_with_summary_or_brief(home, capsys):
    """P3: --view must not be quietly ignored on --summary/--brief paths."""
    assert cli.main(["route", "--summary", "--view", "rules"]) == 2
    refused = json.loads(capsys.readouterr().out)
    assert refused.get("ok") is False and "view" in refused.get("error", "").lower()
    assert cli.main(["route", "--brief", "--view", "summary"]) == 2
    refused = json.loads(capsys.readouterr().out)
    assert refused.get("ok") is False and "view" in refused.get("error", "").lower()
