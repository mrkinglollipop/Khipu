"""Approved routing delivery A-F regressions; all state is hermetic."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone

import pytest

from khipu import recall_prompt, routing


T0 = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)
ME = {"kind": "conversation", "harness": "claude_code", "project": "routing", "session": "one"}
OTHER = {"kind": "conversation", "harness": "codex", "project": "routing", "session": "two"}
SESSION = "routing-delivery-session"
ACCOUNTS = [{"label": "Grok", "harness": "grok"}, {"label": "Codex", "harness": "codex"}] + [
    {"label": f"A{i}", "harness": "codex", "home": f"/logins/a{i}"} for i in range(8)
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


def test_a_compact_rule_change_names_kind_account_and_opening(home):
    routing.request_rule("Grok builder may work only after the release check, then a second sentence that is long " * 8,
                         owner_request="add Grok builder guidance", author=ME)
    view = routing.read_view()
    changed = routing.since(0, view, now=home["clock"].now)
    text, _count = routing.changes_text(changed, view, home["clock"].now,
                                        dict(routing.settings(), routing_prompt_chars=400))
    assert "Standing rule 1 added" in text and "Grok" in text and "Grok builder may" in text


def test_b_priority_and_hold_name_their_contradiction_and_end_route(home):
    held = routing.add_hold("do not launch", reason="keep", scope="account", account="Grok", author=ME)
    routing.request_priority([{"account": "Grok", "role": "Grok builder"}],
                             owner_request="make Grok builder", author=ME, expect=routing.priority_version())
    text = routing.summary(now=home["clock"].now)["text"]
    assert "Grok" in text and "contradicts active hold" in text
    assert f"end_hold hold_id={held['id']}" in text


def test_c_omitted_changes_stay_behind_the_real_delivery_watermark(home):
    _set(home, routing_line_chars=55)
    routing.report(account="Grok", state="open", author=ME)
    base = routing.read_view()
    marker = {"version": base["version"], "epoch": base["epoch"],
              "full_at": routing._iso(T0)}
    for i in range(8):
        routing.report(account=f"A{i}", state="open", author=ME)
    first = routing.prompt_push(marker, budget=1.0)
    assert first["kind"] == "changes"
    assert "more changes" in first["text"]
    _deliver(first)
    heard = recall_prompt.routing_marker(SESSION)
    assert heard.get("owed")
    second = routing.prompt_push(heard, budget=1.0)
    _deliver(second)
    two = first["text"] + "\n" + second["text"]
    assert all(f"A{i}:" in two for i in range(8))
    assert not recall_prompt.routing_marker(SESSION).get("owed")
    assert routing.prompt_push(recall_prompt.routing_marker(SESSION), budget=1.0)["kind"] == "none"
    assert all(len(out["text"].split("\n", 1)[1]) <= 400 for out in (first, second))


def test_c_same_version_debt_precedes_newer_priority(home):
    _set(home, routing_line_chars=55)
    key = lambda i: f"codex|/logins/a{i}"
    routing.report(account="Grok", state="open", author=ME)
    base = routing.read_view()
    routing._write_many([{"type": "evidence", "source": "report", "state": "open", "via": "cli",
                          "written_by": ME, "account": key(i)} for i in range(8)])
    marker = {"version": base["version"], "epoch": base["epoch"], "full_at": routing._iso(T0)}
    first = routing.prompt_push(marker, budget=1.0)
    _deliver(first)
    owed = recall_prompt.routing_marker(SESSION)["owed"]
    assert owed
    routing.request_priority([{"account": "Grok", "role": "newer priority"}], owner_request="use Grok",
                             author=ME, expect=routing.priority_version())
    second = routing.prompt_push(recall_prompt.routing_marker(SESSION), budget=1.0)
    first_owed = next(f"A{i}" for i in range(8) if f"account:{key(i)}" in owed)
    assert second["text"].index(first_owed) < second["text"].index("Your priority")


def test_c_identical_rendered_lines_only_acknowledge_the_selected_entity(home):
    changed = {"entities": ["account:one", "account:two"],
               "lines": ["same delivery line" * 3, "same delivery line" * 3], "directions": [], "since": 0,
               "owed_entities": ["account:one", "account:two"]}
    routing.changes_text(changed, routing.read_view(), home["clock"].now,
                         dict(routing.settings(), routing_prompt_chars=100, routing_line_chars=0))
    assert changed["shown_entities"] == ["account:one"]


def test_d_collision_names_author_age_version_and_replacement_routes(home):
    routing.request_priority([{"account": "Grok"}], owner_request="use Grok", author=ME,
                             expect=routing.priority_version())
    with pytest.raises(routing.RouteError,
                       match=r"Claude Code conversation.*just now.*current version.*owner direction.*Settings.*terminal"):
        routing.request_priority([{"account": "Codex"}], owner_request="replace it", author=OTHER,
                                 expect=routing.priority_version())


def test_e_stale_level_claim_is_marked_beside_fresh_usage_without_editing_words(home):
    _usage(home)
    role = "Grok is low: 2% left"
    routing.request_priority([{"account": "Grok", "role": role}], owner_request="use Grok", author=ME,
                             expect=routing.priority_version())
    text = routing.summary(now=home["clock"].now)["text"]
    assert role in text and "stale level claim; Khipu read 96% left" in text


def test_f_route_output_makes_end_hold_discoverable(home):
    held = routing.add_hold("wait", reason="keep", scope="account", account="Grok", author=ME)
    text = routing.summary(now=home["clock"].now)["text"]
    assert f"khipu_route_report action=end_hold hold_id={held['id']}" in text


def test_acceptance_real_replay_has_grok_priority_and_hold_contradiction(home, monkeypatch):
    source = os.environ.get("ROUTING_DELIVERY_REPLAY_EVENTS")
    if not source or not os.path.isfile(source):
        pytest.skip("real replay fixture not supplied (set ROUTING_DELIVERY_REPLAY_EVENTS)")
    target = routing.events_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    start = routing.start_push(budget=1.0)
    view = routing.read_view()
    routing.report(account="Codex", state="open", author=ME)
    marker = {"version": view["version"], "epoch": view["epoch"], "full_at": routing._iso(T0)}
    prompt = routing.prompt_push(marker, budget=1.0)
    start_text, prompt_text = (start or {}).get("text", ""), prompt.get("text") or ""
    for rendered in (start_text, prompt_text):
        assert "Grok" in rendered and "builder" in rendered and "contradicts active hold" in rendered and "end_hold" in rendered
    summary = routing.summary(view, now=T0)
    assert len(start_text) - len(start_text.splitlines()[0]) - 1 - summary["rules_note_chars"] <= 1500
    assert len(prompt_text) - len(prompt_text.splitlines()[0]) - 1 <= 400
    assert "Hold on Grok" in prompt_text and "contradicts priority" in prompt_text
    assert all(rule["text"] in start_text for rule in view["rules"] if not rule.get("pending"))


def test_d_rule_collision_lists_the_existing_author_and_all_replacement_routes(home):
    routing.request_rule("Grok builds.", author=ME)
    with pytest.raises(routing.RouteError) as error:
        routing.request_rule("Grok reviews.", n=1, author=OTHER)
    text = str(error.value)
    assert all(word in text for word in ("Claude Code conversation", "just now", "current version",
                                         "owner direction", "Settings", "terminal"))


def test_f_owner_hold_names_only_the_owner_route_and_never_ends_from_prose(home):
    held = routing.add_hold("Wait for the owner.", reason="keep", scope="account", account="Grok",
                            owner=True, via="app")
    routing.request_priority([{"account": "Grok", "role": "Regular builder"}], owner_request="use Grok",
                             author=ME, expect=routing.priority_version())
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


@pytest.mark.parametrize("words", ["Grok is low", "Grok is nearly used up", "Grok is limited", "Grok: 2% left"])
def test_e_rule_claim_marker_keeps_each_original_rule_whole(home, words):
    _usage(home)
    routing.add_rule(words, via="app")
    rendered = routing.summary()["text"]
    assert words in rendered and "stale level claim; Grok: Khipu read 96% left" in rendered
    assert routing.read_view()["rules"][0]["text"] == words


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
