"""Slice 19b, case 17c's runner part: a job runner's own caps and budget
fields compared with Khipu's cap facts.

Hermetic: a fake runner folder, fake accounts and fake clocks. The comparison
reads a registered runner's ``config.json`` through routing_sources' existing
folder registration, reports each declaration that has no matching Khipu fact
(or disagrees with it), and never writes the runner's files.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from khipu import routing, routing_scanner, usage

NOW = datetime(2026, 10, 20, 16, tzinfo=timezone.utc)
MAIN = "claude|"
DEEPSEEK = "claude|/logins/deepseek"
GHOST_SECRET = "sk-live-0123456789abcdefghijklmnop"


@pytest.fixture
def mac(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setattr(routing, "_now", lambda: NOW)
    monkeypatch.setattr(usage, "_now", lambda: NOW)
    monkeypatch.setattr(usage, "_command", lambda h: None)
    (data / "config.json").write_text(json.dumps({"usage_accounts": [
        {"harness": "claude", "label": "Main"},
        {"harness": "claude", "label": "DeepSeek", "home": "/logins/deepseek"},
    ]}))
    return home, data


def _runner(mac, budget=None, *, lanes=None, register=True, name="runner a"):
    home, data = mac
    folder = home.parent / name.replace(" ", "-")
    (folder / "state").mkdir(parents=True, exist_ok=True)
    doc = {"lanes": lanes if lanes is not None else {
        "main": {"kind": "claude", "model": "opus", "budget_account": MAIN}}}
    if budget is not None:
        doc["budget"] = budget
    (folder / "config.json").write_text(json.dumps(doc))
    (folder / "state" / "global.json").write_text('{"limited": {}}')
    (folder / "events.log").write_text("")
    (folder / "state" / "heartbeat.json").write_text(
        json.dumps({"time": NOW.timestamp(), "interval": 20}))
    if register:
        cfg = json.loads((data / "config.json").read_text())
        cfg.setdefault("routing_sources", []).append(
            {"name": name, "kind": "lanes", "path": str(folder)})
        (data / "config.json").write_text(json.dumps(cfg))
    return folder


def _cap(label, kind, *, window=None, **values):
    routing.set_cap(label, window=window, kind=kind, revision=f"r-{kind}-{window or 'account'}",
                    via="app", **values)


def _flags():
    view = routing.read_view(now=NOW)
    return routing.runner_cap_flags(view)


def _source_data(mac):
    view = routing.read_view(now=NOW)
    return view["source_cache"]["runners"]["runner a"]["config"]["data"]


# ---- no fact, a fact, and a disagreement ----------------------------------


def test_a_runner_cap_with_no_khipu_fact_is_flagged(mac):
    _runner(mac, {"Main": [{"kind": "share", "window": "weekly", "pct": 91}]})
    flags = _flags()
    assert len(flags) == 1
    flag = flags[0]
    assert flag["runner"] == "runner a"
    assert (flag["account"], flag["kind"], flag["window"], flag["status"]) == (
        "Main", "share", "weekly", "no_fact")
    assert flag["declared"] == {"kind": "share", "window": "weekly", "pct": 91.0}
    assert flag["fact"] is None
    assert "runner a" in flag["text"] and "Main" in flag["text"]
    assert "91%" in flag["text"] and "no such cap" in flag["text"]
    assert len(flag["text"]) <= 200


def test_a_matching_runner_cap_passes(mac):
    _cap("Main", "share", window="weekly", pct=91)
    _runner(mac, {"Main": [{"kind": "share", "window": "weekly", "pct": 91}]})
    assert _flags() == []


def test_a_disagreeing_runner_cap_is_flagged_with_both_values(mac):
    _cap("Main", "share", window="weekly", pct=76)
    _runner(mac, {"Main": [{"kind": "share", "window": "weekly", "pct": 91}]})
    (flag,) = _flags()
    assert flag["status"] == "disagrees"
    assert flag["declared"]["pct"] == 91.0
    assert flag["fact"]["pct"] == 76.0
    assert flag["fact"]["revision"] == "r-share-weekly"
    assert "91%" in flag["text"] and "76%" in flag["text"]


def test_a_window_mismatch_is_no_fact_not_a_disagreement(mac):
    _cap("Main", "share", window="five_hour", pct=60)
    _runner(mac, {"Main": [{"kind": "share", "window": "weekly", "pct": 60}]})
    (flag,) = _flags()
    assert flag["status"] == "no_fact" and flag["window"] == "weekly"


# ---- every cap kind maps to its checked fact -------------------------------


def test_pace_target_spend_and_concurrency_match_their_facts(mac):
    _cap("Main", "pace_target", window="weekly", pct=65, slack=5)
    _cap("Main", "spend", usd=35, days=7)
    _cap("Main", "concurrency", n=1)
    _runner(mac, {"Main": [
        {"kind": "pace_target", "window": "weekly", "pct": 65, "slack": 5},
        {"kind": "spend", "usd": 35, "days": 7},
        {"kind": "concurrency", "n": 1},
    ]})
    assert _flags() == []


def test_a_pace_target_slack_mismatch_is_flagged(mac):
    _cap("Main", "pace_target", window="weekly", pct=65, slack=5)
    _runner(mac, {"Main": [{"kind": "pace_target", "window": "weekly", "pct": 65, "slack": 8}]})
    (flag,) = _flags()
    assert flag["status"] == "disagrees"
    assert flag["declared"]["slack"] == 8.0 and flag["fact"]["slack"] == 5.0


def test_account_scoped_caps_need_no_window_and_different_accounts_stay_apart(mac):
    _cap("Main", "concurrency", n=1)
    _runner(mac, {"DeepSeek": [{"kind": "concurrency", "n": 1}]})
    (flag,) = _flags()
    assert flag["account"] == "DeepSeek" and flag["status"] == "no_fact"
    assert flag["window"] is None


# ---- facts only, never proposals or guesses -------------------------------


def test_a_conversation_cap_proposal_is_not_a_khipu_fact(mac):
    routing.request_cap("Main", window="weekly", kind="share", pct=91, revision="conv1",
                        author={"kind": "conversation", "harness": "codex", "project": "p", "session": "s"})
    _runner(mac, {"Main": [{"kind": "share", "window": "weekly", "pct": 91}]})
    (flag,) = _flags()
    assert flag["status"] == "no_fact"


def test_an_unknown_account_is_flagged_and_never_guessed(mac):
    _runner(mac, {"Ghost": [{"kind": "share", "window": "weekly", "pct": 91}]})
    (flag,) = _flags()
    assert flag["status"] == "no_account" and flag["account"] is None
    assert "Ghost" not in json.dumps(flag)
    assert "an unnamed account" in flag["text"] and "isn't a Khipu account" in flag["text"]
    assert flag["account_words"] == "an unnamed account"


def test_a_secret_shaped_account_is_counted_and_never_shown(mac):
    _runner(mac, {GHOST_SECRET: [{"kind": "share", "window": "weekly", "pct": 91}]})
    assert _flags() == []
    assert _source_data(mac)["skipped"]["budget"] == 1
    assert GHOST_SECRET not in json.dumps(_flags())


def test_a_malformed_declaration_is_skipped_and_counted(mac):
    _runner(mac, {"Main": [
        {"kind": "share", "window": "weekly"},                      # no pct
        {"kind": "share", "window": "weekly", "pct": 91, "usd": 5},  # a field the kind does not use
        {"kind": "readonly", "window": "weekly", "pct": 50},         # not a Khipu cap kind
        {"kind": "share", "window": "weekly", "pct": 91},           # valid
    ]})
    (flag,) = _flags()
    assert flag["declared"]["pct"] == 91.0
    assert _source_data(mac)["skipped"]["budget"] == 3


# ---- registered folders only, read only -----------------------------------


def test_an_unregistered_runner_folder_is_never_read(mac):
    _runner(mac, {"Main": [{"kind": "share", "window": "weekly", "pct": 91}]}, register=False)
    assert _flags() == []


def test_comparison_never_writes_a_runner_file(mac):
    folder = _runner(mac, {"Main": [{"kind": "share", "window": "weekly", "pct": 91}]})
    before = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    assert _flags()
    after = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    assert after == before


def test_a_lane_capacity_is_not_a_quota_and_is_not_flagged(mac):
    # Decision 17: the runner's local lane cap is capacity on this Mac.
    _runner(mac, None, lanes={"main": {"kind": "claude", "model": "opus",
                                      "budget_account": MAIN, "cap": 8}})
    assert _flags() == []


# ---- the report is visible -------------------------------------------------


def test_public_view_and_doctor_carry_the_flags(mac):
    _runner(mac, {"Main": [{"kind": "share", "window": "weekly", "pct": 91}]})
    view = routing.read_view(now=NOW)
    public = routing.public_view(view, NOW)
    assert [f["status"] for f in public["runner_caps"]] == ["no_fact"]
    block = routing.doctor_block(now=NOW)
    assert [f["status"] for f in block["runner_caps"]] == ["no_fact"]
    assert any("no such cap" in w for w in block["warnings"])


def test_two_runners_are_reported_separately(mac):
    _cap("Main", "share", window="weekly", pct=91)
    _runner(mac, {"Main": [{"kind": "share", "window": "weekly", "pct": 91}]}, name="runner a")
    _runner(mac, {"Main": [{"kind": "share", "window": "weekly", "pct": 76}]}, name="runner b")
    (flag,) = _flags()
    assert flag["runner"] == "runner b" and flag["status"] == "disagrees"


def test_public_view_runner_caps_follow_the_account_filter(mac):
    _runner(mac, {"Main": [{"kind": "share", "window": "weekly", "pct": 91}],
                  "DeepSeek": [{"kind": "share", "window": "weekly", "pct": 80}]})
    view = routing.read_view(now=NOW)
    public = routing.public_view(view, NOW, account=MAIN)
    assert [e["key"] for e in public["accounts"]] == [MAIN]
    assert [(f["account"], f["status"]) for f in public["runner_caps"]] == [("Main", "no_fact")]
    public = routing.public_view(view, NOW, account=DEEPSEEK)
    assert [(f["account"], f["status"]) for f in public["runner_caps"]] == [("DeepSeek", "no_fact")]
    assert len(routing.public_view(view, NOW)["runner_caps"]) == 2


def test_a_login_key_never_appears_in_a_runner_cap_flag(mac):
    _runner(mac, {"DeepSeek": [{"kind": "share", "window": "weekly", "pct": 91}]})
    (flag,) = _flags()
    assert flag["account"] == "DeepSeek" and flag["account_words"] == "DeepSeek"
    assert DEEPSEEK not in json.dumps(flag) and "/logins/" not in json.dumps(flag)
    view = routing.read_view(now=NOW)
    assert DEEPSEEK not in json.dumps(routing.public_view(view, NOW)["runner_caps"])
    assert DEEPSEEK not in json.dumps(routing.doctor_block(now=NOW)["runner_caps"])


def test_a_login_key_shaped_label_is_never_echoed():
    accounts = [{"key": DEEPSEEK, "label": DEEPSEEK, "caps": []}]
    declared = [{"runner": "r", "account": DEEPSEEK, "kind": "share", "window": "weekly", "pct": 91.0}]
    (flag,) = routing_scanner.compare_runner_caps(declared, accounts)
    assert DEEPSEEK not in json.dumps(flag) and "/logins/" not in json.dumps(flag)


def test_an_unknown_letters_only_name_is_never_echoed():
    accounts = [{"key": MAIN, "label": "Main", "caps": []}]
    declared = [{"runner": "r", "account": "LettersOnlySecretName", "kind": "share",
                 "window": "weekly", "pct": 91.0}]
    (flag,) = routing_scanner.compare_runner_caps(declared, accounts)
    assert flag["status"] == "no_account" and flag["account"] is None
    assert "LettersOnlySecretName" not in json.dumps(flag)
    assert flag["account_words"] == "an unnamed account"
    assert "an unnamed account" in flag["text"]


# ---- the pure comparison and the parser used on their own ------------------


def test_compare_is_pure_and_takes_no_view(mac):
    accounts = [{"key": MAIN, "label": "Main", "caps": [
        {"kind": "share", "window": "weekly", "pct": 76.0, "revision": "r1"}]}]
    declared = [{"runner": "r", "account": "Main", "kind": "share", "window": "weekly", "pct": 91.0}]
    assert routing_scanner.compare_runner_caps(declared, accounts)[0]["status"] == "disagrees"
    assert accounts[0]["caps"][0]["pct"] == 76.0


def test_the_parser_keeps_only_typed_budget_fields(mac):
    _runner(mac, {"Main": [{"kind": "share", "window": "weekly", "pct": 91, "note": "never kept"}]})
    data = _source_data(mac)
    assert "never kept" not in json.dumps(data)
    assert data["skipped"]["budget"] == 1


def test_config_parse_keeps_the_budget_after_an_unreadable_reread(mac):
    folder = _runner(mac, {"Main": [{"kind": "share", "window": "weekly", "pct": 91}]})
    assert _flags()[0]["status"] == "no_fact"
    (folder / "config.json").write_text("{not json")
    # The last good read stays, with its budget facts rebuilt, never guessed.
    (flag,) = _flags()
    assert flag["status"] == "no_fact" and flag["declared"]["pct"] == 91.0
    assert routing.settings()["routing_sources"][0]["name"] == "runner a"
