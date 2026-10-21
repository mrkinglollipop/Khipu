"""Default-login definitions and removal receipts, using disposable data only."""
import copy
from datetime import timedelta

import pytest

from khipu import accounts, agent_view, config, routing, routing_delivery, routing_sources, usage
from tests.test_routing_acceptance import AUTHOR, replay  # noqa: F401


def define(provider, label, probe="none", **fields):
    routing.set_definition("provider", {"id": provider, "label": label,
                           "family": label, "probe": probe, **fields}, via="app")
    routing.set_definition("account", {"id": provider + ":main", "label": label,
                           "provider": provider}, via="app")


@pytest.mark.parametrize("provider,label", [("glm", "GLM"), ("muse", "Muse"), ("codex", "Codex")])
def test_definition_reuses_migrated_default_login_and_folds_old_reports(replay, provider, label):
    replay.configure(usage_accounts=[{"harness": provider, "label": label}])
    key = provider + "|"
    original = provider + ":original"
    replay.write({"type": "account_migration", "bindings": {key: original},
                  "aliases": {}, "written_by": {"kind": "migration"}})
    define(provider, label, probe=provider)
    replay.write({"type": "evidence", "source": "report", "account": provider + ":main", "state": "limited",
                  "until": routing._iso(replay.now.replace(hour=20)), "scope": "all",
                  "written_by": AUTHOR})
    before = copy.deepcopy(routing.read_events()[0])
    assert [a["key"] for a in routing.accounts()] == [key]
    assert routing._resolve_reported(label) == key
    assert routing._resolve_reported(provider + ":main") == key
    assert routing.resolve_account(label) == original
    assert routing.resolve_lane("fake", "tier", {}, routing.accounts(),
                                {"budget_account": provider + ":main"})["key"] == key
    view = replay.view()
    assert [(a["key"], a["state"]) for a in view["accounts"]] == [(key, "limited")]
    routing.report(account=label, state="open", author=AUTHOR)
    assert routing.read_events()[0][:len(before)] == before
    assert replay.view()["accounts"][0]["state"] == "open"
    assert routing.read_events()[0][-1]["account"] == key


def test_untracked_change_is_acknowledged_once(replay):
    routing.report(account="Codex", state="open", author=AUTHOR)
    first, _, info = replay.offer()
    replay.ack(info)
    accounts.stop("codex|", expect=config.accounts_version(replay.config))
    replay.advance(1)
    replay.view()
    removed, context, info = replay.offer()
    assert "no longer tracked" in context
    replay.ack(info)
    for _ in range(3):
        replay.advance(1)
        again, context, info = replay.offer()
        assert "no longer tracked" not in context
        assert not any(i["entity"] == "account:codex|"
                       for i in (again.get("routing_changes") or {}).get("items", []))


def test_replayed_removal_offer_does_not_reprint_acknowledged_change(replay):
    entity = "account:glm:main"
    view = replay.view()
    view["effective"].pop(entity, None)
    view["changes"] = [{"entities": [entity], "lines": ["GLM: no longer tracked"]}]
    out = {"kind": "changes", "text": "Routing changes\nGLM: no longer tracked",
           "owed_entities": [entity], "shown_entities": [entity], "epoch": "fake",
           "version": 2}
    planned = routing_delivery.plan(out, view, replay.now, routing.settings())
    first = routing_delivery.offer(planned, session="fake-session")
    assert "no longer tracked" in first["text"]
    retry = routing_delivery.offer(planned, session="fake-session")
    assert retry["routing_changes"] == first["routing_changes"]
    assert routing_delivery.acknowledge("fake-session", first["routing_changes"], now=replay.now)
    # A marker or cached offer can still carry the old omission after its
    # receipt committed. The receipt, not that stale debt, is authoritative.
    for _ in range(3):
        again = routing_delivery.offer(planned, session="fake-session")
        assert "no longer tracked" not in (again.get("text") or "")
        assert not again.get("routing_changes")


def test_removed_message_projection_preserves_local_receipt_key(replay):
    key = "codex|/fake/private-login"
    entity = "account:" + key
    view = replay.view()
    view["changes"] = [{"entities": [entity], "lines": [key + ": no longer tracked"]}]
    planned = routing_delivery.plan(
        {"kind": "changes", "text": "", "owed_entities": [], "epoch": "fake", "version": 2},
        view, replay.now, routing.settings(),
        project=lambda value: agent_view.scrub_strict(value, extra_keys=[key]))
    assert entity in planned["delivery_plan"]["removed"]
    assert "/fake/private-login" not in planned["delivery_plan"]["removed"][entity]


def test_deepseek_names_missing_usage_setup_without_confirming_a_host(replay):
    define("deepseek", "DeepSeek")
    entry = next(a for a in replay.view()["accounts"] if a["harness"] == "deepseek")
    assert entry["usage_setup"] == "no usage reader set up; confirm its quota host in Settings → Accounts"
    assert entry["usage_setup"] in routing.account_line(entry, replay.now)
    assert "no report yet" not in routing.summary()["text"].split("DeepSeek:", 1)[1].split("\n", 1)[0]
    assert usage.http_quota_accounts() == []
    assert "host" not in replay.view()["definitions"]["providers"]["deepseek"]


def test_deepseek_http_quota_without_host_names_the_missing_fact(replay):
    define("deepseek", "DeepSeek", probe="http-quota")
    entry = next(a for a in replay.view()["accounts"] if a["harness"] == "deepseek")
    assert entry["usage_setup"] == "no usage reader set up; confirm its quota host in Settings → Accounts"


def test_configured_http_quota_does_not_claim_setup_is_missing(replay):
    define("deepseek", "DeepSeek", probe="http-quota", host="quota.invalid")
    entry = next(a for a in replay.view()["accounts"] if a["harness"] == "deepseek")
    assert not entry.get("usage_setup")


def test_deepseek_runner_declaration_and_reports_reach_the_definition_account(replay):
    define("deepseek", "DeepSeek")
    assert routing.resolve_lane("fake-runner", "small", {}, routing.accounts(),
                                {"kind": "deepseek", "budget_account": "deepseek:main"})["key"] == "deepseek:main"
    routing.report(account="DeepSeek", state="limited", until="2h", author=AUTHOR)
    entry = next(a for a in replay.view()["accounts"] if a["harness"] == "deepseek")
    assert entry["state"] == "limited"
    assert "no usage reader" not in routing.account_line(entry, replay.now)


def test_renamed_single_login_keeps_one_identity_in_pure_fold(replay):
    replay.configure(usage_accounts=[{"harness": "glm", "label": "My GLM"}])
    replay.write({"type": "account_migration", "bindings": {"glm|": "glm:my-glm"},
                  "aliases": {}, "written_by": {"kind": "migration"}})
    define("glm", "GLM", probe="glm")
    known = routing.accounts()
    assert [a["key"] for a in known] == ["glm|"]
    events = routing.read_events()[0]
    body = routing.fold(events, now=replay.now, cfg=routing.settings(replay.config),
                        known=[{"key": "glm|", "harness": "glm", "label": "My GLM", "home": None}])
    assert [a["key"] for a in body["accounts"]] == ["glm|"]


def test_explicit_other_login_is_not_merged_by_label(replay):
    replay.configure(usage_accounts=[{"harness": "codex", "label": "Codex"}],
                     routing_account_bindings={"codex:main": {"harness": "codex", "home": "/fake/other-login"}})
    replay.write({"type": "account_migration", "bindings": {"codex|": "codex:original"},
                  "aliases": {}, "written_by": {"kind": "migration"}})
    define("codex", "Codex", probe="codex")
    assert {a["key"] for a in routing.accounts()} == {"codex|", "codex|/fake/other-login"}


@pytest.mark.parametrize("owner_first", [True, False])
def test_label_fallback_cannot_take_another_definitions_default_login(replay, owner_first):
    replay.configure(usage_accounts=[{"harness": "codex", "label": "Codex"}])
    replay.write({"type": "account_migration", "bindings": {"codex|": "codex:orig"},
                  "aliases": {}, "written_by": {"kind": "migration"}})
    definitions = [("codex:orig", "Codex Pro"), ("codex:other", "Codex")]
    for ident, label in definitions if owner_first else reversed(definitions):
        routing.set_definition("account", {"id": ident, "label": label, "provider": "codex"}, via="app")
    assert {(a["key"], a["label"]) for a in routing.accounts()} == {
        ("codex|", "Codex Pro"), ("codex:other", "Codex")}
    assert routing._resolve_reported("codex:orig") == "codex|"
    assert routing._resolve_reported("codex:other") == "codex:other"
    routing.report(account="codex:orig", state="limited", until="2h", author=AUTHOR)
    routing.report(account="codex:other", state="open", author=AUTHOR)
    assert {(a["key"], a["label"], a["state"]) for a in replay.view()["accounts"]} == {
        ("codex|", "Codex Pro", "limited"), ("codex:other", "Codex", "open")}


def test_deepseek_runner_spend_reaches_its_account(replay):
    from tests.test_routing_s17t_telemetry import NOW, runner, spend

    replay.now = NOW
    define("deepseek", "DeepSeek")
    runner((replay.root, replay.data, None), "fake-deepseek",
           telemetry={"spend": [spend("fake-event", 1.25, NOW - timedelta(hours=1),
                                      account="deepseek:main", provider="deepseek")]},
           lanes={"small": {"provider": "deepseek", "budget_account": "deepseek:main"}})
    view = replay.view()
    streams = routing._telemetry_streams(view)
    report = routing_sources.spend_report(streams, "deepseek:main", NOW - timedelta(days=7), NOW)
    assert report["usd"] == 1.25
