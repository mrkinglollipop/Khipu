"""Delivery health uses only isolated hook, pull, child and cursor evidence."""
from datetime import timedelta

import pytest

from khipu import recall_prompt, routing, routing_delivery as delivery, routing_health as health
from tests import test_routing_acceptance as acceptance

T0 = acceptance.T0
replay = acceptance.replay


def planned(texts=("abc",), budget=400, revision="r1"):
    items = [dict(entity=f"rule:{i}", revision=revision, text=text, label=f"Standing rule {i}",
                  whole=True, visible=True) for i, text in enumerate(texts)]
    return dict(epoch="e1", version=1, text="\n".join(texts), delivery_plan=dict(
        items=items, current_entities=[i["entity"] for i in items], at=routing._iso(T0),
        page_limit=budget, limit=budget, lease=60, heading="Routing", text="\n".join(texts)))


def test_missing_evidence_is_unknown_and_does_not_write(replay):
    before = set(replay.root.rglob("*"))
    result = health.doctor_block({"unmatched": []}, replay.now, routing.settings())
    assert result["status"] == "unknown"
    assert "live delivery identities" in result["missing"]
    assert "today's hook outcomes" in result["missing"]
    assert set(replay.root.rglob("*")) == before


def test_budget_bound_counts_characters_ahead_and_surrogate_pairs(replay):
    out = planned(("a" * 400, "😀" * 201))
    delivery.offer(out, session="s1")
    entries = delivery.snapshot("s1")["entries"]
    import math

    assert [e["owed_chars"] for e in entries.values()] == [400, 802]
    assert all(e["bound"] == math.ceil(e["owed_chars"] / e["budget"]) for e in entries.values())
    for _ in range(min(e["bound"] for e in entries.values()) + 1):
        delivery.offer(out, session="s1")
    assert health.component_health(delivery.snapshot("s1"), T0)["status"] == "green"
    delivery.offer(out, session="s1")
    assert "budget-stall" in health.component_health(delivery.snapshot("s1"), T0)["reasons"]


def test_refit_is_not_an_extra_delivery_point(replay):
    out = delivery.offer(planned(("a" * 600,)), session="s1")
    before = delivery.snapshot("s1")["delivery_points"]
    delivery.refit("s1", out["routing_changes"], 300)
    assert delivery.snapshot("s1")["delivery_points"] == before


def test_new_revision_has_its_own_retry_and_budget_evidence(replay):
    out = planned()
    for i in range(3):
        out["delivery_plan"]["at"] = routing._iso(T0 + timedelta(seconds=i * 61))
        delivery.offer(out, session="s1")
    assert "lease-expired-twice" in health.component_health(delivery.snapshot("s1"), T0 + timedelta(seconds=122))["reasons"]
    out = planned(revision="r2")
    out["delivery_plan"]["at"] = routing._iso(T0 + timedelta(seconds=123))
    delivery.offer(out, session="s1")
    assert health.component_health(delivery.snapshot("s1"), T0 + timedelta(seconds=123))["status"] == "green"


def test_child_ack_and_mcp_ack_do_not_clear_hooked_parent(replay):
    out = planned()
    for _ in range(4):
        delivery.offer(out, session="s1")
    child = delivery.child_offer(out, session="s1", component="agent:c", now=T0)
    assert delivery.acknowledge("s1", child["routing_changes"], now=T0)
    changes, _ = delivery.pull_offer(out, session="s1", counting_component="main", now=T0)
    assert delivery.pull_acknowledge("s1", changes["batch"], now=T0) == "acked"
    result = health.doctor_block({}, T0, routing.settings())
    parent = next(row for row in result["identities"] if row["component"] == "main")
    assert parent["status"] == "red"
    child = next(row for row in result["identities"] if row["component"] == "agent:c")
    assert child["status"] == "green"
    # Current pull-only capability changes which parent component counts.
    _, summary = delivery.pull_offer(out, session="s1", counting_component="mcp", now=T0)
    assert summary["health"] == "green"
    result = health.doctor_block({}, T0, routing.settings())
    assert not any(row["component"] == "main" for row in result["identities"])
    assert all(row["status"] == "green" for row in result["identities"])
    # A return to hooked capability counts main again.
    delivery.pull_offer(out, session="s1", counting_component="main", now=T0)
    assert any(row["status"] == "red" for row in health.doctor_block({}, T0, routing.settings())["identities"])


def test_inactive_identities_are_not_stalled_live_work(replay):
    for _ in range(4):
        delivery.offer(planned(), session="s1")
    now = T0 + timedelta(hours=13)
    result = health.doctor_block({}, now, routing.settings())
    assert not result["identities"] and "budget-stall" not in result["reasons"]
    assert result["status"] == "unknown"


def test_enabled_copies_without_evidence_are_unknown(replay):
    replay.configure(routing_replication_live=True, routing_cloud_copy=True)
    result = health.doctor_block({}, T0, routing.settings())
    assert result["replication"]["status"] == result["gateway"]["status"] == "unknown"
    assert "cross-Mac caught-up cursor" in result["missing"]
    assert "gateway delivered copy revision" in result["missing"]


def test_prompt_rate_uses_today_and_strict_one_percent(replay):
    path = recall_prompt._log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    def line(at, status):
        return routing._iso(at) + " [khipu-prompt-recall] session=s1 reason=no_hits ms=1 hits=[] routing=" + status + "\n"
    path.write_text(line(T0 - timedelta(days=1), "skipped:budget") * 500
                    + line(T0, "skipped:budget") + line(T0, "none") * 99)
    assert health.prompt_health(T0)["status"] == "green"
    path.write_text(path.read_text() + line(T0, "skipped:busy"))
    assert health.prompt_health(T0)["status"] == "red"


@pytest.mark.parametrize("state", [{}, {"entries": {}}, {"entries": {"x": {"text": "a", "acked": True, "offset": 1}}}])
def test_legacy_missing_proof_never_green(replay, state):
    assert health.component_health(state, T0)["status"] == "unknown"


def test_child_pruning_keeps_receipt_proof_and_does_not_hide_damage(replay):
    out = planned()
    answer = delivery.child_offer(out, session="s1", component="agent:ok", now=T0)
    assert delivery.acknowledge("s1", answer["routing_changes"], now=T0)
    state = delivery.snapshot("s1", "agent:ok")
    assert state["done_shown"] == state["done"]
    assert health.component_health(state, T0)["status"] == "green"
    delivery.child_offer(out, session="s1", component="agent:damaged", now=T0)
    with delivery._transaction("s1") as data:
        entry = next(iter(data["agent:damaged"]["entries"].values()))
        entry.update(acked=True, offset=len(entry["text"]), shown_offset=0)
        delivery._prune_agent_components(data, T0)
    state = delivery.snapshot("s1", "agent:damaged")
    assert state["entries"]
    assert health.component_health(state, T0)["reasons"] == ["ack-unshown"]


def test_anonymous_start_receipt_records_hooked_attachment(replay):
    answer = delivery.offer(planned())
    assert delivery.acknowledge("s1", answer["routing_changes"],
                                reservation=answer["delivery_reservation"], now=T0)
    result = health.doctor_block({}, T0, routing.settings())
    assert result["identities"][0]["component"] == "main"
    assert result["identities"][0]["status"] == "green"


def test_ack_validation_counts_one_tool_result(replay):
    replay.patch.setenv("KHIPU_HARNESS", "aegis")
    routing.report(account="Codex", state="open", author=acceptance.AUTHOR)
    first = replay.pull(session_id="client:once")
    session = delivery.canonical_identity(explicit="client:once")["session"]
    state = delivery.snapshot(session, "mcp")
    points = state["delivery_points"]
    replay.pull(session_id="client:once", ack=first["routing_changes"]["batch"])
    assert delivery.snapshot(session, "mcp")["delivery_points"] == points + 1


def test_healthy_pagination_does_not_exceed_the_budget_bound(replay):
    routing.add_rule("Synthetic owner instruction " * 400, via="app")
    for _ in range(80):
        _, _, info = replay.offer()
        replay.ack(info)
        state = delivery.snapshot("s1")
        assert "budget-stall" not in health.component_health(state, replay.now)["reasons"]
        if all(e.get("acked") for e in state["entries"].values()):
            break
    else:
        pytest.fail("bounded rule pages did not finish")


@pytest.mark.parametrize("field,value", [("offset", "bad"), ("shown_offset", []), ("expired", "bad")])
def test_malformed_component_evidence_is_unknown(replay, field, value):
    delivery.offer(planned(), session="s1")
    state = delivery.snapshot("s1")
    next(iter(state["entries"].values()))[field] = value
    assert health.component_health(state, T0)["status"] == "unknown"


def test_impossible_payload_budget_is_unknown(replay):
    delivery.offer(planned(("long synthetic text" * 40,), budget=40), session="s1")
    assert health.component_health(delivery.snapshot("s1"), T0)["status"] == "unknown"


def test_pull_summary_uses_configured_slack(replay):
    out = planned()
    out["delivery_plan"]["slack"] = 5
    for _ in range(4):
        _, summary = delivery.pull_offer(out, session="s1", now=T0)
    assert summary["health"] == "green"
    for _ in range(3):
        _, summary = delivery.pull_offer(out, session="s1", now=T0)
    assert summary["health"] == "red"
