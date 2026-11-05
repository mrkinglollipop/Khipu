"""What real use on the second Mac still showed (round 3): warnings and health rows
that were wrong about a Mac where a program is simply not set up."""
from __future__ import annotations

import pytest

from khipu import routing_advisory


def _view(state, reason):
    return {"accounts": [{"key": "grok:main", "label": "Grok", "harness": "grok", "state": state, "reason": reason,
                          "configured": True}],
            "priority": {"event_id": "e1", "entries": [{"account": "grok:main", "label": "Grok"}]},
            "rules": [], "directions": []}


@pytest.mark.parametrize("reason", ["not signed in", "not installed"])
def test_a_program_not_set_up_on_this_mac_is_not_a_contradiction(reason):
    assert routing_advisory._contradictions(_view("down", reason)) == []


@pytest.mark.parametrize("reason", ["out of service", "key rejected", "no plan on its key", None])
def test_a_real_outage_still_warns(reason):
    (flag,) = routing_advisory._contradictions(_view("down", reason))
    assert flag["text"] == "The priority names Grok, which is down."


def test_a_paused_account_in_the_priority_still_warns():
    (flag,) = routing_advisory._contradictions(_view("paused", None))
    assert "paused" in flag["text"]


pytest_plugins = ("tests.test_routing_acceptance",)


def _advice_for_down_codex(replay, reason):
    view = replay.view()
    entry = next(a for a in view["accounts"] if a["harness"] == "codex")
    entry.update(state="down", reason=reason)
    from khipu import routing
    out = routing.advise("Codex", view=view)
    return next(a for a in out["accounts"] if a["key"] == entry["key"]) if "accounts" in out else out


def test_advice_says_a_missing_sign_in_is_about_this_mac(replay):
    one = _advice_for_down_codex(replay, "not signed in")
    assert one["verdict"] == "closed" and "not signed in on this Mac" in one["reason"], one
    assert "can't be read here" in one["reason"]


def test_advice_for_a_real_outage_still_says_down(replay):
    one = _advice_for_down_codex(replay, "out of service")
    assert one["verdict"] == "closed" and one["reason"] == "down", one


def test_the_sign_in_item_says_it_is_this_mac(monkeypatch):
    from datetime import datetime, timezone

    from khipu import needs_you, usage
    account = {"label": "Grok", "harness": "grok"}
    monkeypatch.setattr(usage, "_load_cache", lambda: {"k": {"ok": False, "error": "not_signed_in"}})
    monkeypatch.setattr(usage, "all_accounts", lambda: [account])
    monkeypatch.setattr(usage, "_key", lambda a: "k")
    items, unread = needs_you._plans(None, datetime.now(timezone.utc))
    (item,) = items
    assert unread == [] and item["kind"] == "signin"
    assert "not signed in on this Mac" in item["words"] and "here" in item["words"]


# ---- capture liveness on a Mac where a harness or the gateway is not used ------------------------


def _liveness(**rows):
    harnesses = {h: {"harness": h, "ok": True, "seen": seen, "reasons": []} for h, seen in rows.items()}
    return {"ok": True, "red": [], "harnesses": harnesses}


def test_an_aegis_never_seen_here_is_not_red_for_having_no_gateway_token():
    from khipu import cli
    liveness = _liveness(aegis=False, claude_code=True)
    cli._fold_gateway_liveness(liveness, {"ok": True, "applicable": False},
                               {"ok": False, "applicable": True, "error": "no gateway token"})
    assert liveness["ok"] is True and liveness["red"] == []
    assert liveness["harnesses"]["aegis"]["ok"] is True and liveness["harnesses"]["aegis"]["gateway"]["error"]


def test_an_aegis_that_ran_here_without_a_token_stays_red():
    from khipu import cli
    liveness = _liveness(aegis=True)
    cli._fold_gateway_liveness(liveness, {"ok": True, "applicable": False},
                               {"ok": False, "applicable": True, "error": "no gateway token"})
    assert liveness["ok"] is False and liveness["red"] == ["aegis"]


def test_a_gateway_that_fails_for_a_mac_with_a_token_stays_red():
    from khipu import cli
    liveness = _liveness(aegis=False)
    cli._fold_gateway_liveness(liveness, {"ok": False, "applicable": True, "error": "ConnectionError"},
                               {"ok": True, "applicable": False})
    assert liveness["ok"] is False and liveness["red"] == ["gateway"]


def test_a_gateway_url_with_no_token_is_not_checked_from_this_mac(monkeypatch):
    from khipu import integrations
    monkeypatch.setattr("khipu.config.gateway_url", lambda: "https://gateway.invalid")
    monkeypatch.setattr(integrations, "_gateway_token", lambda: "")
    out = integrations.gateway_liveness_check()
    assert out["ok"] is True and out["applicable"] is False and "no gateway token" in out["note"]


def test_a_dead_job_with_no_harness_in_its_name_does_not_make_an_unused_aegis_red(tmp_path, monkeypatch):
    from khipu import session_capture as sc
    monkeypatch.setattr(sc, "_read_beat", lambda h: {})
    monkeypatch.setattr(sc, "_queue_by_harness", lambda: {"aegis": {
        "depth": 3, "dead_claims": 3, "dead_claims_stale": 3, "waiting_depth": 3, "waiting_age_s": 99999,
        "oldest_age_s": 99999, "oldest_job": "job.json", "held_errors": []}})
    row = sc.liveness("aegis")
    assert row["ok"] is True and row["seen"] is False
    assert sc.job_harness(tmp_path / "job.json", {}) == "aegis"  # the default that could misfile a job
