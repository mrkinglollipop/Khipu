"""One Mac's login is really the other account (Matt, 2026-11-05: the M5 Max has
both Claude accounts, the M1 only the second). Rebinding it there moves the M1's
readings to the second account on both Macs, and nothing about the main one moves.
Two disposable Macs and an in-memory hub."""
from __future__ import annotations

from datetime import timedelta

import pytest

from khipu import accounts, cli, owner_act, routing, routing_advice, routing_replication as rr
from tests import test_routing_s11b_replication as _s11b
from tests.test_routing_acceptance import AUTHOR, T0

pair = _s11b.pair
sync = _s11b.sync
MAIN_RESET = T0 + timedelta(days=4)
SECOND_RESET = T0 + timedelta(days=2)


def history(account, used, reset, *, offset=0):
    start = T0 - timedelta(hours=30)
    return [{"type": "usage_sample", "account": account, "window": "weekly", "t_err": 60, "source": "probe",
             "witness_at": routing._iso(start + timedelta(minutes=30 * i + offset)),
             "used_pct": used - 0.25 * (59 - i), "resets_at": routing._iso(reset),
             "written_by": {"kind": "probe"}, "via": "probe"} for i in range(60)]


@pytest.fixture
def macs(pair, tmp_path):
    """The M5 reads both logins, the M1 one, still bound to the main account."""
    m5, m1, hub = pair
    second = tmp_path / "claude-second"
    second.mkdir()
    m5.activate()
    m5.configure(usage_accounts=[{"label": "Claude", "harness": "claude", "label_source": "owner"},
                                 {"label": "Claude second", "harness": "claude", "home": str(second),
                                  "label_source": "owner"},
                                 {"label": "Codex", "harness": "codex", "label_source": "owner"}])
    key = f"claude|{second}"
    m5.write(*history("claude|", 41.0, MAIN_RESET), *history(key, 64.0, SECOND_RESET))
    # What the M5's own reads report, which is how another Mac comes to list them.
    routing.report(account="Claude", state="open", author=AUTHOR)
    routing.report(account="Claude second", state="open", author=AUTHOR)
    routing.set_cap("Claude", window="weekly", kind="share", pct=91, via="app")
    routing.set_cap("Claude", window="weekly", kind="pace_target", pct=100, slack=5, via="app")
    routing.set_priority([{"account": "Claude", "role": "first"}, {"account": "Codex", "role": "then"}],
                         via="app", expect=routing.priority_version())
    routing.add_rule("Claude main takes the big builds", via="app")
    assert sync(m5, hub)["ok"]
    assert sync(m1, hub)["ok"]
    m1.activate()
    m1.write(*history("claude|", 64.0, SECOND_RESET, offset=10))
    return m5, m1, hub, key


def rebind(mac):
    """The two owner actions, on the M1."""
    mac.activate()
    out = accounts.stop("claude|", expect=accounts.status()["version"], keep_priority=True, via="app")
    assert out["priority"] == "kept"
    accounts.track([{"harness": "claude", "label": "Claude second"}], expect=accounts.status()["version"],
                   read=False, via="app")


def settle(m5, m1, hub):
    for mac in (m1, m5, m1, m5):
        assert sync(mac, hub)["ok"]


def entry(view, key):
    return next(a for a in view["accounts"] if a["key"] == key)


def weekly(item):
    return next(p for p in item["pace"] if p["window"] == "weekly")


def main_as_read(view, key="claude|"):
    item = entry(view, key)
    pace = weekly(item)
    return {"label": item["label"], "state": item["state"], "used": pace["used_pct"], "reset": pace["cycle_resets_at"],
            "why": pace["why"], "plan": pace["plan"]["verdict"], "rate": round(pace["rate_pct_per_h"], 6),
            "share": (pace["share_cap"] or {}).get("verdict"), "target": (pace["pace_target"] or {}).get("verdict"),
            "caps": sorted((c["kind"], c.get("window"), c.get("pct")) for c in item["caps"]),
            "priority": [(e["account"], e["label"], e.get("role")) for e in view["priority"]["entries"]],
            "rules": [r["text"] for r in view["rules"]], "holds": view["holds"]}


def test_the_m1s_readings_count_for_the_second_account_on_both_macs(macs):
    m5, m1, hub, key = macs
    rebind(m1)
    settle(m5, m1, hub)
    m1.activate()
    here = m1.view()
    labels = {a["key"]: a["label"] for a in here["accounts"]}
    assert labels["claude|"] == "Claude second" and "claude:second" not in labels and labels["claude:main"] == "Claude"
    assert weekly(entry(here, "claude|"))["used_pct"] == 64.0
    assert weekly(entry(here, "claude|"))["cycle_resets_at"] == routing._iso(SECOND_RESET)
    sent = {r["payload"]["account"] for r in hub.rows.values()
            if r["payload"]["type"] == "usage_sample" and r["payload"]["mac"] == routing._local_mac()}
    assert sent == {"claude:second"}, "the M1's readings went out under the wrong account"
    m5.activate()
    there = m5.view()
    pace = weekly(entry(there, key))
    assert pace["why"] is None and pace["used_pct"] == 64.0 and pace["cycle_resets_at"] == routing._iso(SECOND_RESET)
    # Two witnesses to it now, not the one before.
    witnesses = {r["mac"] for r in rr.remote_events() if r["type"] == "usage_sample" and r["account"] == "claude:second"}
    assert len(witnesses) == 1 and routing._local_mac() not in witnesses


def test_the_main_account_stays_exactly_as_the_m5_reads_it(macs):
    m5, m1, hub, key = macs
    m5.activate()
    before = main_as_read(m5.view())
    version = m5.view()["version"]
    rebind(m1)
    settle(m5, m1, hub)
    m5.activate()
    view = m5.view()
    assert main_as_read(view) == before
    assert before["used"] == 41.0 and before["caps"] and before["priority"][0][1] == "Claude"
    assert view["version"] == version or not [c for c in view["changes"] if c["version"] > version
                                              and any(e in ("account:claude|", "priority") for e in c["entities"])]
    assert routing_advice.account_advice(view, entry(view, "claude|"), m5.now)["verdict"] == "go"
    assert routing_advice.account_advice(view, entry(view, key), m5.now)["verdict"] == "go"


def test_nothing_the_m1_sends_can_rename_or_rebind_the_m5(macs):
    m5, m1, hub, _key = macs
    rebind(m1)
    settle(m5, m1, hub)
    kinds = [r["payload"] for r in hub.rows.values() if r["payload"].get("mac") == _mac(m1)]
    assert not [p for p in kinds if p["type"] in ("account_rename", "account_move", "priority_set", "priority_clear",
                                                  "priority_drop", "cap_set", "cap_remove", "rule_add", "rule_retire")]
    # Bindings cross only without a login key, which ``state`` ignores.
    assert all("|" not in p["account"] for p in kinds if p["type"] == "account_binding")
    m5.activate()
    ids = routing.account_identity.state(routing._in_write_order(routing.read_events()[0]), routing.accounts())
    assert ids[0]["claude|"] == "claude:main" and "claude:main" not in ids[1]


def _mac(mac):
    mac.activate()
    return routing._local_mac()


def test_the_m1_keeps_the_main_accounts_facts_and_does_not_advise_it(macs):
    m5, m1, hub, _key = macs
    rebind(m1)
    settle(m5, m1, hub)
    m1.activate()
    view = m1.view()
    main = entry(view, "claude:main")
    assert weekly(main)["used_pct"] == 41.0 and weekly(main)["why"] is None
    assert {("share", 91), ("pace_target", 100)} <= {(c["kind"], c["pct"]) for c in main["caps"]}
    assert ("claude:main", "Claude", "first") in [(e["account"], e["label"], e.get("role")) for e in view["priority"]["entries"]]
    advice = routing_advice.account_advice(view, main, m1.now)
    assert advice["verdict"] == "closed" and "not logged in on this Mac" in advice["reason"]
    assert routing_advice.account_advice(view, entry(view, "claude|"), m1.now)["verdict"] == "go"


def test_a_provider_with_no_login_here_is_still_advised_as_it_was():
    view = {"accounts": [{"key": "kimi:main"}, {"key": "claude|"}, {"key": "claude:main"},
                         {"key": "codex:main"}, {"key": "codex|", "configured": False}]}
    other = routing_advice._login_here_is_another_account
    assert other(view, view["accounts"][2])
    assert not other(view, view["accounts"][0]) and not other(view, view["accounts"][1])
    assert not other(view, view["accounts"][3]), "a stopped login is no login here"


def test_the_rebind_can_be_turned_back(macs):
    m5, m1, hub, _key = macs
    rebind(m1)
    m1.activate()
    accounts.stop("claude|", expect=accounts.status()["version"], keep_priority=True, via="app")
    accounts.track([{"harness": "claude", "label": "Claude"}], expect=accounts.status()["version"], read=False, via="app")
    ids = routing.account_identity.state(routing._in_write_order(routing.read_events()[0]), routing.accounts())[0]
    assert ids["claude|"] == "claude:main"
    settle(m5, m1, hub)
    for mac in (m5, m1):
        mac.activate()
        view = mac.view()
        assert entry(view, "claude|").get("note", [None])[0] != "untracked"
        assert "no longer tracked" not in routing.change_line("account:claude|", view, mac.now)


def test_the_owner_action_carries_keep_priority_to_the_command_line():
    values = owner_act._values("accounts.stop", {"account": "claude|", "keep_priority": True, "expect": "v1"})
    head, positionals = owner_act.argv("accounts.stop", values)
    parsed = cli.build_parser().parse_args(owner_act.full_argv(head, positionals, ["--owner-direction"]))
    assert parsed.keep_priority is True and parsed.account == "claude|" and parsed.expect == "v1"
    plain = owner_act.argv("accounts.stop", owner_act._values("accounts.stop", {"account": "claude|", "expect": "v1"}))
    assert "--keep-priority" not in plain[0]


def test_a_stop_without_it_would_take_the_main_account_out_of_the_shared_priority(macs):
    """Why the action needs it, and why this path is not the plain stop."""
    m5, m1, hub, _key = macs
    m1.activate()
    assert sync(m1, hub)["ok"]
    accounts.stop("claude|", expect=accounts.status()["version"], via="app")
    written = [e for e in routing._read_event_lines()[0] if e["type"] in ("priority_set", "priority_clear", "priority_drop")]
    assert written[-1]["written_by"]["kind"] == "owner" and "claude|" not in [e["account"] for e in written[-1].get("entries", [])]


@pytest.mark.parametrize("reverse", [False, True])
def test_stop_and_retrack_only_changes_the_originating_macs_notes(pair, reverse):
    a, b, hub = pair
    if reverse:
        a, b = b, a
    for mac in (a, b):
        mac.activate()
        routing.report(account="Claude", state="open", author=AUTHOR)
    a.activate()
    accounts.stop("claude|", expect=accounts.status()["version"], keep_priority=True, via="app")
    assert "no longer tracked" in routing.change_line("account:claude|", a.view(), a.now)
    for mac in (a, b):
        assert sync(mac, hub)["ok"]
    b.activate()
    view = b.view()
    assert entry(view, "claude|").get("note") is None
    assert "no longer tracked" not in routing.change_line("account:claude|", view, b.now)
    a.activate()
    accounts.track([{"harness": "claude", "label": "Claude second"}],
                   expect=accounts.status()["version"], read=False, via="app")
    settle(b, a, hub)
    a.activate()
    assert entry(a.view(), "claude|")["note"][0] == "tracked"
    b.activate()
    assert entry(b.view(), "claude|").get("note") is None
    assert "no longer tracked" not in routing.change_line("account:claude|", b.view(), b.now)


@pytest.mark.parametrize("state", ["untracked", "tracked", "renamed"])
def test_stale_peer_note_does_not_change_local_advice_priority_or_caps(macs, state):
    m5, _m1, _hub, _key = macs
    m5.activate()
    before = m5.view()
    # Exact old wire shape, already retained by an earlier version. Also
    # exercise the legacy same-key shape: neither describes this Mac's list.
    for key in ("claude:main", "claude|"):
        rows = routing.read_events()[0] + [{"type": "account_note", "account": key,
            "state": state, "written_at": routing._iso(T0 + timedelta(seconds=1)),
            "written_by": {"kind": "owner"}, "mac": "peer", "remote": True}]
        view = routing.fold(rows, now=m5.now, cfg=routing.settings(m5.config), known=routing.accounts(),
                            mac=routing._local_mac())
        main = entry(view, "claude|")
        assert main.get("note") is None and main["configured"] is True
        assert main_as_read(view) == main_as_read(before)
        assert all(e["in_settings"] for e in view["priority"]["entries"])
        assert routing_advice.account_advice(view, main, m5.now)["verdict"] == "go"
        assert "no longer tracked" not in routing.change_line("account:claude|", view, m5.now)


def test_peer_probe_words_use_the_account_label(macs):
    m5, m1, hub, _key = macs
    m5.activate()
    m5.write({"type": "evidence", "account": "claude|", "state": "open", "source": "probe",
              "written_by": {"kind": "probe"}, "via": "probe"})
    rebind(m1)
    settle(m5, m1, hub)
    m1.activate()
    main = entry(m1.view(), "claude:main")
    assert main["harness"] == "claude"
    words = routing.account_line(main, m1.now)
    assert "read from Claude" in words
    assert "claude:main" not in words
    second = dict(main, label="Claude second")
    assert "read from Claude second" in routing.account_line(second, m1.now)


def test_shared_account_definition_removal_still_reaches_the_other_mac(pair):
    a, b, hub = pair
    a.activate()
    value = {"id": "kimi:main", "label": "Kimi", "provider": "kimi"}
    routing.set_definition("account", value, via="app")
    routing.set_priority([{"account": "Kimi", "role": "first"}], via="app",
                         expect=routing.priority_version())
    for mac in (a, b):
        assert sync(mac, hub)["ok"]
    b.activate()
    assert b.view()["priority"]["entries"][0]["in_settings"] is True
    a.activate()
    # This is the existing event emitted when undo removes a definition
    # that did not exist before the owner's addition.
    routing._owner_write({"type": "definition_remove", "definition": "account", "value": value}, "app")
    for mac in (a, b):
        assert sync(mac, hub)["ok"]
    b.activate()
    view = b.view()
    assert view["priority"]["entries"][0]["in_settings"] is False
    assert "no longer tracked" in routing.priority_line(view["priority"], b.now, view=view)
