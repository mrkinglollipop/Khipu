"""Approved routing delivery A-F regressions; all state is hermetic."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from khipu import recall_prompt, recall_rule, routing


T0 = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)
ME = {"kind": "conversation", "harness": "claude_code", "project": "routing", "session": "one"}
OTHER = {"kind": "conversation", "harness": "codex", "project": "routing", "session": "two"}
SESSION = "routing-delivery-session"
ACCOUNTS = [{"label": "Grok", "harness": "grok"}, {"label": "Codex", "harness": "codex"}] + [
    {"label": f"A{i}", "harness": "codex", "home": f"/logins/synthetic-login-{i}"} for i in range(8)
]


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def home(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS, "routing_prompt_chars": 400}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    clock = Clock()
    monkeypatch.setattr(routing, "_now", clock)
    return {"data": data, "clock": clock}


def _usage(home, left=96):
    resets = routing._iso(T0 + timedelta(days=6))
    rows = {"grok|": {"ok": True, "probed_at": routing._iso(T0), "observed_at": routing._iso(T0),
                       "windows": [{"name": "weekly", "used_pct": 100 - left, "resets_at": resets,
                                    "window_mins": 10080}]}}
    state = home["data"] / "state"
    state.mkdir(exist_ok=True)
    (state / "usage.json").write_text(json.dumps({"version": 2, "rows": rows}))


def _set(home, **values):
    path = home["data"] / "config.json"
    config = json.loads(path.read_text())
    config.update(values)
    path.write_text(json.dumps(config))


def _deliver(out):
    at = recall_prompt._RoutingAtPrompt()
    at.session = SESSION
    _ctx, info = at.add_to("", out)
    assert info and info.get("pending")
    assert recall_prompt._routing_delivered(info, True)


def test_d_replacement_takes_effect_and_names_the_replaced_author(home, monkeypatch):
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-one")
    shown = routing.conversation_view()
    routing.request_priority([{"account": "Grok"}], owner_request="use Grok", author=ME,
                             expect=routing.priority_version(), base=shown["priority_owner_revision"])
    shown = routing.conversation_view()
    routing.request_priority([{"account": "Codex"}], owner_request="replace it", author=OTHER,
                             expect=routing.priority_version(), base=shown["priority_owner_revision"])
    view = routing.read_view()
    assert [e["label"] for e in view["priority"]["entries"]] == ["Codex"]
    replaced = [p for p in view["proposals"].values() if p["status"] == "replaced"]
    assert len(replaced) == 1 and replaced[0]["notify"] == ME


def test_d_rule_replacement_takes_effect_and_names_the_replaced_author(home, monkeypatch):
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-one")
    routing.request_rule("Grok builds.", author=ME)
    shown = routing.conversation_view()
    routing.request_rule("Grok reviews.", n=1, author=OTHER, base=next(r["owner_revision"] for r in shown["rules"] if r["n"] == 1))
    view = routing.read_view()
    assert view["rules"][0]["text"] == "Grok reviews."
    assert view["rules"][0]["pending_by"] == "a Codex conversation"
    replaced = [p for p in view["proposals"].values() if p["status"] == "replaced"]
    assert len(replaced) == 1 and replaced[0]["notify"] == ME


def test_f_owner_hold_names_only_the_owner_route_and_never_ends_from_prose(home, monkeypatch):
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-one")
    held = routing.add_hold("Wait for the owner.", reason="keep", scope="account", account="Grok",
                            owner=True, via="app")
    shown = routing.conversation_view()
    routing.request_priority([{"account": "Grok", "role": "Regular builder"}], owner_request="use Grok",
                             author=ME, expect=routing.priority_version(), base=shown["priority_owner_revision"])
    view = routing.read_view()
    line = routing.hold_line(view["holds"][0], T0, view=view)
    assert "only the owner" in line and "Settings" in line and "--owner" in line
    assert "khipu_route_report action=end_hold" not in line
    assert view["holds"][0]["id"] == held["id"]
    assert view["holds"][0]["text"] == "Wait for the owner."


def test_c_late_duplicate_cannot_recreate_delivered_debt(home):
    assert recall_prompt.mark_routing_seen(SESSION, version=2, epoch="e", owed=["one", "two"], shown=["one"])
    assert recall_prompt.routing_marker(SESSION)["owed"] == ["two"]
    assert recall_prompt.mark_routing_seen(SESSION, version=2, epoch="e", owed=["two"], shown=["two"])
    assert not recall_prompt.routing_marker(SESSION).get("owed")
    assert recall_prompt.mark_routing_seen(SESSION, version=2, epoch="e", owed=["one", "two"], shown=["one"])
    assert not recall_prompt.routing_marker(SESSION).get("owed")
    assert recall_prompt.mark_routing_seen(SESSION, version=3, epoch="e", owed=["new"], shown=[])
    assert recall_prompt.mark_routing_seen(SESSION, version=2, epoch="e", owed=["old"], shown=["new"])
    assert recall_prompt.routing_marker(SESSION)["owed"] == ["new"]


def test_e_claims_from_other_accounts_and_instructions_are_not_stale_facts(home):
    fact = {"grok|": {"readable": True, "left_pct": 96}}
    for words in ("Grok is not limited", "Never run Grok to its limit", "Grok has 96% left; Codex is low"):
        assert not routing._level_claim_marker(words, "grok|", fact, label="Grok", labels=["Grok", "Codex"])


def test_c_a_budgeted_full_summary_keeps_existing_undelivered_debt(home):
    assert recall_prompt.mark_routing_seen(SESSION, version=2, epoch="e", owed=["one"], shown=[])
    assert recall_prompt.mark_routing_full(SESSION, version=3, epoch="e", at=routing._iso(T0))
    assert recall_prompt.routing_marker(SESSION)["owed"] == ["one"]


def test_f_conversation_route_lists_the_actual_end_hold_action(home):
    held = routing.add_hold("wait", reason="keep", scope="account", account="Grok", author=ME)
    out = routing.conversation_view(now=T0)
    assert f"khipu_route_report action=end_hold hold_id={held['id']}" in out["holds"][0]["line"]


def test_review_overlapping_account_labels_agree_on_hold_conflicts(home):
    _set(home, usage_accounts=[{"label": "Codex", "harness": "codex"},
                               {"label": "Codex second", "harness": "codex", "home": "/logins/second-login"}])
    held = routing.add_hold("wait", reason="keep", scope="account", account="Codex", author=ME)
    routing.add_rule("Codex second is open for small jobs.", via="app")
    view = routing.read_view()
    assert "contradicts" not in routing.rule_line(view["rules"][0], view=view)
    assert "contradicts" not in routing.hold_line(view["holds"][0], T0, view=view)
    routing.add_rule("Codex is open for small jobs.", via="app", holds={held["id"]: "keep"})
    view = routing.read_view()
    assert "contradicts active hold" in routing.rule_line(view["rules"][1], view=view)
    assert "contradicts rule 2" in routing.hold_line(view["holds"][0], T0, view=view)


