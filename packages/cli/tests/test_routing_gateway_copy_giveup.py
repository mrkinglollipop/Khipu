"""The cleanup give-up is visible and the owner's retry goes past the cap."""
from datetime import timedelta

from khipu import db, routing, routing_gateway_copy as gc, routing_health
from tests import routing_gateway_fixture as gateway_fixtures
from tests import test_routing_s12_gateway as gateway_tests

gateway_hub = gateway_fixtures.gateway_hub
hub = gateway_fixtures.hub
pg_cluster = gateway_fixtures.pg_cluster
gw = gateway_tests.gw


def give_up(gw, monkeypatch):
    gateway_tests.configure(gw, routing_cloud_copy=False)
    routing._id_salt()
    with routing._lock():
        routing._mac_locked()
    monkeypatch.setattr(routing, "_hub_configured", lambda: True)
    calls = []

    def down(**kwargs):
        calls.append(True)
        raise OSError("configured hub offline")

    monkeypatch.setattr(db, "connect", down)
    for _ in range(20):
        gc.publish()
        routing._now.now += timedelta(seconds=400)
    assert gc.status()["failures"] == 6
    return calls


def health():
    now = routing._now()
    return routing_health.doctor_block(routing.read_view(now), now, routing.settings())


def test_cap_reached_is_reported_by_status_health_and_doctor(gw, monkeypatch):
    give_up(gw, monkeypatch)
    assert gc.status()["cleanup_gave_up"] is True
    block = health()
    assert block["gateway"]["status"] == "red"
    assert "gateway-cleanup-stopped" in block["reasons"]
    assert "stopped after 6 failed tries" in block["gateway"]["reason_words"]
    assert any("stopped after 6 failed tries" in w for w in routing.doctor_block()["warnings"])


def test_automatic_tick_does_not_retry_after_the_cap(gw, monkeypatch):
    calls = give_up(gw, monkeypatch)
    before = len(calls)
    for _ in range(3):
        assert gc.publish()["ok"] is False
        routing._now.now += timedelta(seconds=400)
    assert len(calls) == before


def test_forced_retry_success_clears_the_count_and_health(gw, monkeypatch):
    give_up(gw, monkeypatch)
    deleted = []

    class Up:
        def delete(self, publisher):
            deleted.append(publisher)

    assert gc.publish(force=True, hub=Up()) == {"ok": True, "disabled": True, "deleted": True}
    assert deleted
    state = gc.status()
    assert not state.get("failures") and not state.get("cleanup_gave_up")
    block = health()
    assert block["gateway"]["status"] != "red"
    assert "gateway-cleanup-stopped" not in block["reasons"]


def test_forced_retry_failure_records_one_more_and_stays_red(gw, monkeypatch):
    calls = give_up(gw, monkeypatch)
    before = len(calls)
    assert gc.publish(force=True) == {"ok": False, "error": "copy_publish_failed"}
    assert len(calls) == before + 1
    assert gc.status()["failures"] == 7
    assert gc.status()["cleanup_gave_up"] is True
    assert health()["gateway"]["status"] == "red"
