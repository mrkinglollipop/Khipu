"""Approved routing delivery A-F regressions; all state is hermetic."""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import agent_view
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
    routing.conversation_view()
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
    assert recall_prompt.mark_routing_full(SESSION, version=base["version"], epoch=base["epoch"],
                                          at=marker["full_at"])
    for i in range(8):
        routing.report(account=f"A{i}", state="open", author=ME)
    agent_view.prepare_routing_table(refresh=True)
    first = routing.prompt_push(marker, budget=1.0)
    assert first["kind"] == "changes"
    assert "more changes" in first["text"]
    _deliver(first)
    heard = recall_prompt.routing_marker(SESSION)
    assert heard.get("owed")
    agent_view.prepare_routing_table(refresh=True)
    second = routing.prompt_push(heard, budget=1.0)
    _deliver(second)
    two = first["text"] + "\n" + second["text"]
    assert all(f"A{i}:" in two for i in range(8))
    assert not recall_prompt.routing_marker(SESSION).get("owed")
    assert len(first["shown_entities"] + second["shown_entities"]) == 8
    assert len(set(first["shown_entities"] + second["shown_entities"])) == 8
    agent_view.prepare_routing_table(refresh=True)
    assert routing.prompt_push(recall_prompt.routing_marker(SESSION), budget=1.0)["kind"] == "none"
    assert all(len(out["text"].split("\n", 1)[1]) <= 400 for out in (first, second))


def test_c_same_version_debt_precedes_newer_priority(home):
    _set(home, routing_line_chars=55)
    key = lambda i: f"codex|/logins/synthetic-login-{i}"
    routing.report(account="Grok", state="open", author=ME)
    base = routing.read_view()
    routing._write_many([{"type": "evidence", "source": "report", "state": "open", "via": "cli",
                          "written_by": ME, "account": key(i)} for i in range(8)])
    marker = {"version": base["version"], "epoch": base["epoch"], "full_at": routing._iso(T0)}
    assert recall_prompt.mark_routing_full(SESSION, version=base["version"], epoch=base["epoch"],
                                          at=marker["full_at"])
    agent_view.prepare_routing_table(refresh=True)
    first = routing.prompt_push(marker, budget=1.0)
    _deliver(first)
    owed = recall_prompt.routing_marker(SESSION)["owed"]
    assert owed
    routing.conversation_view()
    routing.request_priority([{"account": "Grok", "role": "newer priority"}], owner_request="use Grok",
                             author=ME, expect=routing.priority_version())
    agent_view.prepare_routing_table(refresh=True)
    second = routing.prompt_push(recall_prompt.routing_marker(SESSION), budget=1.0)
    first_owed = next(f"A{i}" for i in range(8) if f"account:{key(i)}" in owed)
    assert second["text"].index(first_owed) < second["text"].index("Your priority")
    _deliver(second)
    assert not recall_prompt.routing_marker(SESSION).get("owed")


def test_c_identical_rendered_lines_only_acknowledge_the_selected_entity(home):
    changed = {"entities": ["account:one", "account:two"],
               "lines": ["same delivery line" * 3, "same delivery line" * 3], "directions": [], "since": 0,
               "owed_entities": ["account:one", "account:two"]}
    routing.changes_text(changed, routing.read_view(), home["clock"].now,
                         dict(routing.settings(), routing_prompt_chars=100, routing_line_chars=0))
    assert changed["shown_entities"] == ["account:one"]


def test_d_replacement_takes_effect_and_names_the_replaced_author(home):
    routing.conversation_view()
    routing.request_priority([{"account": "Grok"}], owner_request="use Grok", author=ME,
                             expect=routing.priority_version())
    routing.conversation_view()
    routing.request_priority([{"account": "Codex"}], owner_request="replace it", author=OTHER,
                             expect=routing.priority_version())
    view = routing.read_view()
    assert [e["label"] for e in view["priority"]["entries"]] == ["Codex"]
    replaced = [p for p in view["proposals"].values() if p["status"] == "replaced"]
    assert len(replaced) == 1 and replaced[0]["notify"] == ME


def test_e_stale_level_claim_is_marked_beside_fresh_usage_without_editing_words(home):
    _usage(home)
    role = "Grok is low: 2% left"
    routing.conversation_view()
    routing.request_priority([{"account": "Grok", "role": role}], owner_request="use Grok", author=ME,
                             expect=routing.priority_version())
    text = routing.summary(now=home["clock"].now)["text"]
    assert role in text and "stale level claim; Khipu read 96% left" in text
    view = routing.read_view()
    changed = routing.since(0, view)
    prompt, _count = routing.changes_text(changed, view, T0)
    assert role in prompt and "stale level claim; Khipu read 96% left" in prompt
    assert len(prompt.split("\n", 1)[1]) <= 400
    assert view["priority"]["entries"][0]["role"] == role


def test_e_compact_rule_keeps_the_stale_marker_in_a_prompt(home):
    _usage(home)
    words = ("Grok: 2% left. " + "Builder guidance follows. " * 30).strip()
    routing.request_rule(words, owner_request="add builder guidance", author=ME)
    view = routing.read_view()
    changed = routing.since(0, view)
    prompt, _count = routing.changes_text(changed, view, T0)
    assert "Standing rule 1 added for Grok" in prompt
    assert "stale level claim; Grok: Khipu read 96% left" in prompt
    assert len(prompt.split("\n", 1)[1]) <= 400
    assert view["rules"][0]["text"] == words


def test_f_route_output_makes_end_hold_discoverable(home):
    held = routing.add_hold("wait", reason="keep", scope="account", account="Grok", author=ME)
    text = routing.summary(now=home["clock"].now)["text"]
    assert f"khipu_route_report action=end_hold hold_id={held['id']}" in text


@pytest.mark.parametrize("colliding_login_names", [False, True])
def test_acceptance_real_replay_has_grok_priority_and_hold_contradiction(
        home, monkeypatch, colliding_login_names):
    # Synthetic events reproduce the dates and transitions, with no copied
    # sessions, projects, paths or words from the private routing log.
    if colliding_login_names:
        _set(home, usage_accounts=ACCOUNTS[:2] + [
            {"label": f"A{i}", "harness": "codex", "home": f"/logins/a{i}"} for i in range(8)
        ])
    source = Path(__file__).parent / "fixtures" / "routing_delivery_oct14_19.jsonl"
    target = routing.events_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    routing.add_rule("Every builder result gets a review.", via="app")
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    start = routing.start_push(budget=1.0)
    view = routing.read_view()
    routing.report(account="Codex", state="open", author=ME)
    marker = {"version": view["version"], "epoch": view["epoch"], "full_at": routing._iso(T0)}
    agent_view.prepare_routing_table(refresh=True)
    prompt = routing.prompt_push(marker, budget=1.0)
    start_text, prompt_text = (start or {}).get("text", ""), prompt.get("text") or ""
    for rendered in (start_text, prompt_text):
        assert "Grok" in rendered and "builder" in rendered and "contradicts active hold" in rendered and "end_hold" in rendered
    summary = routing.summary(view, now=T0)
    assert len(start_text) - len(start_text.splitlines()[0]) - 1 - summary["rules_note_chars"] <= 1500
    assert len(prompt_text) - len(prompt_text.splitlines()[0]) - 1 <= 400
    assert "Hold on Grok" in prompt_text and "contradicts priority" in prompt_text
    assert all(rule["text"] in start_text for rule in view["rules"] if not rule.get("pending"))
    assert routing.read_view()["holds"][0]["id"] == "h-synthetic-grok"


def test_d_rule_replacement_takes_effect_and_names_the_replaced_author(home):
    routing.request_rule("Grok builds.", author=ME)
    routing.conversation_view()
    routing.request_rule("Grok reviews.", n=1, author=OTHER)
    view = routing.read_view()
    assert view["rules"][0]["text"] == "Grok reviews."
    assert view["rules"][0]["pending_by"] == "a Codex conversation"
    replaced = [p for p in view["proposals"].values() if p["status"] == "replaced"]
    assert len(replaced) == 1 and replaced[0]["notify"] == ME


def test_f_owner_hold_names_only_the_owner_route_and_never_ends_from_prose(home):
    held = routing.add_hold("Wait for the owner.", reason="keep", scope="account", account="Grok",
                            owner=True, via="app")
    routing.conversation_view()
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


@pytest.mark.parametrize("path", ["start", "full_prompt", "changes"])
def test_review_omitted_pending_changes_are_counted_and_delivered_later(home, monkeypatch, path):
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    before = routing.read_view()
    for i in range(12):
        routing.request_rule(f"pending {i} " + "x" * 500, author=ME)
    routing.conversation_view()
    routing.request_priority([{"account": "Grok", "role": "builder " * 150}],
                             owner_request="use Grok", author=ME, expect=routing.priority_version())
    if path == "start":
        monkeypatch.setattr(routing, "kick_plan_refresh", lambda: None)
        at = recall_rule._RoutingAtStart(session_id=SESSION).begin()
        assert at.text()
        out = at.out
    else:
        if path == "changes":
            assert recall_prompt.mark_routing_full(SESSION, version=before["version"], epoch=before["epoch"],
                                                   at=routing._iso(T0))
        agent_view.prepare_routing_table(refresh=True)
        out = routing.prompt_push(recall_prompt.routing_marker(SESSION), budget=1.0)
    view = routing.read_view()
    pending = {*[routing._rule_component(r) for r in view["rules"]], "priority"}
    from khipu import routing_delivery

    payloads = {entity: [] for entity in pending}
    for _ in range(80):
        # A preview never completes a rule. Every reserved payload has to
        # occur in the actual offered context before its ack advances it.
        state = routing_delivery.snapshot(SESSION)
        for item in out["routing_changes"]["items"]:
            entity = item["entity"]
            if entity in pending:
                entry = state["entries"][entity]
                end = state["flight"]["progress"][entity]
                payload = entry["text"][entry["offset"]:end]
                assert payload in out["text"]
                payloads[entity].append(payload)
                assert (entity in out["shown_entities"]) == (end == len(entry["text"]))
        _deliver(dict(out, kind=out.get("kind") or "full", at=routing._iso(T0)))
        if not recall_prompt.routing_marker(SESSION).get("owed"):
            break
        agent_view.prepare_routing_table(refresh=True)
        out = routing.prompt_push(recall_prompt.routing_marker(SESSION), budget=1.0)
        assert out["kind"] == "changes"
    state = routing_delivery.snapshot(SESSION)
    assert not recall_prompt.routing_marker(SESSION).get("owed")
    for entity in pending:
        assert state["entries"][entity]["acked"]
        assert "".join(payloads[entity]) == state["entries"][entity]["text"]


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


@pytest.mark.parametrize("words", ["Grok is limited to read-only audits", "Grok is low priority for builds",
                                  "Keep Grok at 20% left as a floor", "If Grok has 20% left, stop builds"])
def test_review_policy_words_are_not_current_allowance_claims(home, words):
    _usage(home)
    routing.add_rule(words, via="app")
    assert "stale level claim" not in routing.summary()["text"]


def test_review_owner_rule_flags_share_the_prompt_budget(home):
    for account in ACCOUNTS:
        routing.add_hold("wait", reason="keep", scope="account", account=account["label"], author=ME)
    before = routing.read_view()
    words = " and ".join(a["label"] for a in ACCOUNTS) + " can build."
    for _ in range(4):
        routing.add_rule(words, via="app", holds={h["id"]: "keep" for h in before["holds"]})
    view = routing.read_view()
    changed = routing.since(before["version"], view)
    text, _ = routing.changes_text(changed, view, T0)
    owner_lines = ["Standing rule " + routing.rule_line(r) for r in view["rules"]]
    assert all(line in text for line in owner_lines)
    body = text.split("\n", 1)[1]
    for line in owner_lines:
        body = body.replace(line, "", 1)
    assert len(body) <= 400
    assert body.count("Rule flags: see `khipu_route`.") == 1


@pytest.mark.parametrize("kind", ["rule", "priority"])
def test_review_previews_scrub_before_truncating_private_names(home, kind):
    _set(home, usage_accounts=ACCOUNTS + [{"label": "Extra", "harness": "codex",
                                         "home": "/logins/matthewschwartz_private_login_folder"}])
    text = "matthewschwartz_private_login_folder " + "x" * 500
    if kind == "rule":
        pointer = routing.rule_pointer({"n": 1, "text": text}, routing.read_view())
    else:
        pointer = routing.priority_pointer({"entries": [{"label": "Grok", "account": "grok|", "role": text}]},
                                           routing.read_view())
    assert "matthew" not in pointer
