"""Runner limits retain the configured secondary account's identity."""
import json

import pytest

from khipu import config, routing
from tests.test_routing_cursor import NOW, runner

pytest_plugins = ("tests.test_routing_cursor",)

HOME = "/synthetic/second"
LIMIT = "2026-10-20T15:59:00Z job LIMITED lane=secondary-haiku until=2026-10-20T18:00:00Z\n"


@pytest.mark.parametrize("label", ["Claude second", "Second", HOME])
@pytest.mark.parametrize("how", ["map_label", "map_key", "login"])
def test_secondary_limit_paths_keep_one_account(mac, label, how):
    cfg = json.loads(config.config_file().read_text())
    cfg["usage_accounts"] = [
        {"harness": "claude", "label": "Claude"},
        {"harness": "claude", "label": label, "home": HOME},
    ]
    if how.startswith("map"):
        target = label if how == "map_label" else "claude|" + HOME
        cfg["routing_lane_map"] = {"runner": {"secondary-haiku": target}}
    config.config_file().write_text(json.dumps(cfg))
    spec = {"kind": "claude", "model": "haiku"}
    if how == "login":
        spec["login_home"] = HOME
    runner(mac, {"secondary-haiku": spec}, LIMIT)

    first = routing.read_view()
    observations = [e for e in routing.read_events()[0] if e["type"] == "observation"]
    assert len(observations) == 1
    observed_id = observations[0]["account"]
    assert HOME not in json.dumps(observations)
    assert {a["label"] for a in first["accounts"]} == {"Claude", label}
    rows = {a["label"]: a for a in first["accounts"]}
    assert rows[label]["state"] == "limited"
    assert rows["Claude"]["state"] != "limited"
    if label == "Claude second":
        bindings = routing.account_identity.state(routing.read_events()[0], [])[0]
        assert bindings[rows[label]["key"]] == observed_id
    assert routing.account_id(rows[label]["key"]) == observed_id

    # Discard process caches and force a fresh disk fold of the observation.
    routing._IDENTITY_CACHE.clear()
    routing._MIGRATION_REVISION = None
    with routing.events_file().open("a") as stream:
        stream.write("\n")
    second = routing.read_view()
    assert {a["label"] for a in second["accounts"]} == {"Claude", label}
    rows = {a["label"]: a for a in second["accounts"]}
    assert rows[label]["state"] == "limited"
    assert rows["Claude"]["state"] != "limited"
    assert routing.account_id(rows[label]["key"]) == observed_id
    assert len([e for e in routing.read_events()[0] if e["type"] == "observation"]) == 1
    if label != HOME:
        assert HOME not in json.dumps(routing._conversation_view(now=NOW), default=str)


@pytest.mark.parametrize("target", ["Missing", "claude:missing", "acct-0123456789ab",
                                    "claude|/missing", "claude|", "claude", "/missing"])
def test_unknown_lane_map_target_never_creates_account(mac, target):
    cfg = json.loads(config.config_file().read_text())
    cfg["usage_accounts"] = [{"harness": "grok", "label": "Grok"}]
    cfg["routing_lane_map"] = {"runner": {"secondary-haiku": target}}
    config.config_file().write_text(json.dumps(cfg))
    assert routing._lane_target(target, routing.accounts()) is None
    runner(mac, {"secondary-haiku": {"kind": "claude", "model": "haiku"}}, LIMIT)
    view = routing.read_view()
    assert [a["label"] for a in view["accounts"]] == ["Grok"]
    match = view["sources"]["runners"][0]["lane_matches"]["secondary-haiku"]
    assert match["account"] is None
    assert any(e["lane"] == "secondary-haiku" and e["state"] == "limited" for e in view["unmatched"])
    assert all(e.get("account") is None for e in routing.read_events()[0] if e["type"] == "observation")
