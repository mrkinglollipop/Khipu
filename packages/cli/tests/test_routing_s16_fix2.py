"""S16 re-check: revision age and private delivery-row metadata."""
import hashlib
import json

from khipu import routing, routing_gateway_copy
from tests import routing_gateway_fixture as gateway_fixtures
from tests import test_routing_acceptance as acceptance
from tests.test_routing_s16_fix1 import AUTHOR, _gateway_off, _gateway_on, _outcomes


replay = acceptance.replay
gateway_hub = gateway_fixtures.gateway_hub
hub = gateway_fixtures.hub
pg_cluster = gateway_fixtures.pg_cluster


def test_gateway_behind_age_starts_at_current_revision(replay, gateway_hub):
    routing.report(account="Codex", state="open", author=AUTHOR)
    _, _, info = replay.offer()
    replay.ack(info)
    replay.configure(routing_cloud_copy=True)
    _gateway_on(replay)
    body = replay.pull(session_id="client:health")
    if body.get("routing_changes"):
        replay.pull(session_id="client:health", ack=body["routing_changes"]["batch"])
    _gateway_off(replay)
    # Local delivery continues while the gateway client is idle for an hour.
    for _ in range(6):
        replay.advance(600)
        _, _, info = replay.offer()
        replay.ack(info)
        assert routing_gateway_copy.publish(force=True)["ok"]  # Mac heartbeats while the client is idle
    routing.report(account="Codex", state="limited", until="1h", author=AUTHOR)
    routing.read_view(replay.now)
    replay.advance(60)
    _, _, info = replay.offer()
    replay.ack(info)
    _outcomes(replay)
    health = routing.doctor_block(now=replay.now)["routing_delivery"]
    assert health["gateway"]["status"] == "green"
    assert health["gateway"]["age_seconds"] == 60
    assert health["gateway"]["copies"][0]["revision"] == body["version"]
    assert health["gateway"]["copies"][0]["epoch"] == body["epoch"]
    assert health["gateway"]["published_revision_at"] == body["folded_at"]
    assert "gateway-stale" not in health["reasons"]
    replay.advance(540)  # At the bound is still fresh.
    assert routing.doctor_block(now=replay.now)["routing_delivery"]["gateway"]["status"] == "green"
    replay.advance(1)
    health = routing.doctor_block(now=replay.now)["routing_delivery"]
    assert health["gateway"]["status"] == "red"
    assert health["gateway"]["age_seconds"] == 601
    assert "gateway-stale" in health["reasons"]
    _gateway_on(replay)
    replay.pull(session_id="client:health")
    _gateway_off(replay)  # the publisher fetches the durable hub receipt
    recovered = routing.doctor_block(now=replay.now)["routing_delivery"]["gateway"]
    assert recovered["status"] == "green" and recovered["age_seconds"] == 0
    view = routing.read_view(replay.now)
    assert recovered["published_epoch"] == view["epoch"]
    assert recovered["published_revision"] == view["version"]
    assert recovered["published_revision_at"] == view["folded_at"]


def test_delivery_rows_keep_safe_harness_and_last_delivery_point(replay, gateway_hub):
    session = "SYNTHETIC-PRIVATE-SESSION-/private/project"
    replay.patch.setenv("KHIPU_HARNESS", "codex")
    routing.report(account="Codex", state="open", author=AUTHOR)
    _, _, info = replay.offer(session=session)
    replay.ack(info)
    routing.start_push(moment="delegation", child_component="agent:child", child_session=session)
    rows = routing.doctor_block(now=replay.now)["routing_delivery"]["identities"]
    assert len(rows) == 2
    expected = hashlib.sha256(session.encode()).hexdigest()
    for row in rows:
        assert row["identity"] == expected
        assert row["harness"] == "codex"
        assert row["last_delivery_point"] == routing._iso(replay.now)
    assert session not in json.dumps(rows)
    assert "/private/project" not in json.dumps(rows)

    replay.patch.setenv("KHIPU_HARNESS", "muse")
    replay.pull(session_id="synthetic-pull")
    rows = routing.doctor_block(now=replay.now)["routing_delivery"]["identities"]
    assert any(row["harness"] == "muse" and row["component"] == "mcp" for row in rows)
    replay.configure(routing_cloud_copy=True)
    _gateway_on(replay)
    replay.pull(session_id="synthetic-gateway")
    assert routing_gateway_copy.publish(force=True)["ok"]
    rows = routing.doctor_block(now=replay.now)["routing_delivery"]["identities"]
    assert any(row["harness"] == "gateway" and row["component"] == "mcp" for row in rows)
    _gateway_off(replay)
    replay.patch.setenv("KHIPU_HARNESS", "/private/SYNTHETIC-SESSION")
    replay.pull(session_id="synthetic-unknown")
    rows = routing.doctor_block(now=replay.now)["routing_delivery"]["identities"]
    unknown = next(row for row in rows if row["identity"] == hashlib.sha256(b"pull\0synthetic-unknown").hexdigest())
    assert unknown["harness"] == "unknown"
    assert "/private/" not in json.dumps(rows)
