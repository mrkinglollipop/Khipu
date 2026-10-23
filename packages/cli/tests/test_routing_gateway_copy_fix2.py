"""Disabled routing copies stay idle; known revocations retain bounded retries."""
from datetime import timedelta

from khipu import db, routing, routing_gateway_copy as gc
from tests import routing_gateway_fixture as gateway_fixtures
from tests import test_routing_s12_gateway as gateway_tests

gateway_hub = gateway_fixtures.gateway_hub
hub = gateway_fixtures.hub
pg_cluster = gateway_fixtures.pg_cluster
gw = gateway_tests.gw


def local_files():
    return {str(p.relative_to(routing.routing_dir())): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in routing.routing_dir().rglob("*") if p.is_file()}


def never_published(gw):
    gateway_tests.configure(gw, routing_cloud_copy=False)
    routing._id_salt()
    with routing._lock():
        routing._mac_locked()


def test_never_on_unconfigured_poll_ticks_do_not_connect_or_write(gw, monkeypatch):
    never_published(gw)
    monkeypatch.setattr(routing, "_hub_configured", lambda: False)
    calls = []
    def down(**kwargs):
        calls.append(True)
        raise OSError("no configured hub")
    monkeypatch.setattr(db, "connect", down)
    before = local_files()
    for _ in range(4):
        assert gc.publish() == {"ok": True, "disabled": True}
        routing._now.now += timedelta(seconds=400)
    assert calls == []
    assert local_files() == before


def test_never_on_configured_cleanup_retries_with_backoff_then_deletes(gw, gateway_hub, monkeypatch):
    never_published(gw)
    monkeypatch.setattr(routing, "_hub_configured", lambda: True)
    calls = []
    good = db.connect
    def down(**kwargs):
        calls.append(True)
        raise OSError("configured hub offline")
    monkeypatch.setattr(db, "connect", down)
    assert gc.publish() == {"ok": False, "error": "copy_publish_failed"}
    assert gc.status().get("cleanup_attempted") is True
    first_retry = routing._parse(gc.status()["retry_at"])
    routing._now.now = first_retry - timedelta(seconds=1)
    assert gc.publish() == {"ok": False, "error": "copy_retry_pending"}
    assert calls == [True]
    routing._now.now = first_retry + timedelta(seconds=1)
    assert gc.publish() == {"ok": False, "error": "copy_publish_failed"}
    assert calls == [True, True]
    assert gc.status()["failures"] == 2
    second_retry = routing._parse(gc.status()["retry_at"])
    assert second_retry > first_retry + timedelta(seconds=1)
    monkeypatch.setattr(db, "connect", good)
    routing._now.now = second_retry + timedelta(seconds=1)
    assert gc.publish() == {"ok": True, "disabled": True, "deleted": True}
    after_delete = local_files()
    for _ in range(3):
        routing._now.now += timedelta(seconds=400)
        assert gc.publish() == {"ok": True, "disabled": True}
    assert local_files() == after_delete


def test_unconfigured_hub_check_is_cached_for_a_bounded_time(gw, monkeypatch):
    never_published(gw)
    calls = []
    def check():
        calls.append(True)
        return False
    monkeypatch.setattr(routing, "_hub_configured", check)
    before = local_files()
    for _ in range(4):
        assert gc.publish() == {"ok": True, "disabled": True}
        routing._now.now += timedelta(seconds=60)
    assert calls == [True]
    assert local_files() == before
    routing._now.now += timedelta(seconds=600)
    assert gc.publish() == {"ok": True, "disabled": True}
    assert calls == [True, True]


def test_cleanup_retry_stops_at_the_cap_and_keeps_the_error(gw, monkeypatch):
    never_published(gw)
    monkeypatch.setattr(routing, "_hub_configured", lambda: True)
    calls = []

    def down(**kwargs):
        calls.append(True)
        raise OSError("configured hub offline")

    monkeypatch.setattr(db, "connect", down)
    for _ in range(20):
        result = gc.publish()
        routing._now.now += timedelta(seconds=400)
    assert result == {"ok": False, "error": "copy_publish_failed"}
    assert gc.status().get("error") == "copy_publish_failed"
    assert gc.status().get("cleanup_attempted") is True
    stopped_at = len(calls)
    assert 0 < stopped_at <= 6
    for _ in range(3):
        assert gc.publish() == {"ok": False, "error": "copy_publish_failed"}
        routing._now.now += timedelta(seconds=400)
    assert len(calls) == stopped_at


def test_cleanup_retry_stops_when_the_hub_is_removed(gw, monkeypatch):
    never_published(gw)
    monkeypatch.setattr(routing, "_hub_configured", lambda: True)
    calls = []

    def down(**kwargs):
        calls.append(True)
        raise OSError("configured hub offline")

    monkeypatch.setattr(db, "connect", down)
    assert gc.publish() == {"ok": False, "error": "copy_publish_failed"}
    assert calls == [True]
    monkeypatch.setattr(routing, "_hub_configured", lambda: False)
    routing._now.now += timedelta(seconds=400)
    assert gc.publish() == {"ok": True, "disabled": True}
    assert calls == [True]
    routing._now.now += timedelta(seconds=400)
    assert gc.publish() == {"ok": True, "disabled": True}
    assert calls == [True]


def test_cleanup_retry_recovers_before_the_cap_then_goes_idle(gw, monkeypatch):
    never_published(gw)
    monkeypatch.setattr(routing, "_hub_configured", lambda: True)
    deleted = []

    class Down:
        def delete(self, publisher):
            raise OSError("configured hub offline")

    class Up:
        def delete(self, publisher):
            deleted.append(publisher)

    assert gc.publish(hub=Down()) == {"ok": False, "error": "copy_publish_failed"}
    routing._now.now += timedelta(seconds=400)
    assert gc.publish(hub=Up()) == {"ok": True, "disabled": True, "deleted": True}
    assert deleted == gc.status()["cleanup_publishers"]
    for _ in range(3):
        routing._now.now += timedelta(seconds=400)
        assert gc.publish(hub=Up()) == {"ok": True, "disabled": True}
    assert len(deleted) == 1


def test_known_publisher_delete_honours_retry_without_file_churn(gw, gateway_hub, monkeypatch):
    gateway_tests.seed(gw)
    assert gc.publish(force=True)["published"]
    gateway_tests.configure(gw, routing_cloud_copy=False)
    calls = []
    good = db.connect
    def down(**kwargs):
        calls.append(True)
        raise OSError("hub offline during revocation")
    monkeypatch.setattr(db, "connect", down)
    assert gc.publish() == {"ok": False, "error": "copy_publish_failed"}
    retry_at = routing._parse(gc.status()["retry_at"])
    before_retry = local_files()
    routing._now.now = retry_at - timedelta(seconds=1)
    assert gc.publish() == {"ok": False, "error": "copy_retry_pending"}
    assert calls == [True]
    assert local_files() == before_retry
    monkeypatch.setattr(db, "connect", good)
    routing._now.now = retry_at + timedelta(seconds=1)
    assert gc.publish()["deleted"]
    assert gc.PostgresHub().newest() is None


def test_real_publication_resets_one_off_cleanup_marker(gw, monkeypatch):
    never_published(gw)
    monkeypatch.setattr(routing, "_hub_configured", lambda: True)
    good = db.connect
    calls = []
    def down(**kwargs):
        calls.append(True)
        raise OSError("synthetic hub offline")
    monkeypatch.setattr(db, "connect", down)
    assert not gc.publish()["ok"]
    monkeypatch.setattr(db, "connect", good)
    routing._now.now += timedelta(seconds=400)
    assert gc.publish() == {"ok": True, "disabled": True, "deleted": True}
    gateway_tests.configure(gw, routing_cloud_copy=True)
    gateway_tests.seed(gw)
    assert gc.publish(force=True)["published"]
    assert not gc.status().get("cleanup_attempted")
    gateway_tests.configure(gw, routing_cloud_copy=False)
    monkeypatch.setattr(db, "connect", down)
    assert not gc.publish()["ok"]
    assert len(calls) == 2
    retry_at = routing._parse(gc.status()["retry_at"])
    assert gc.publish() == {"ok": False, "error": "copy_retry_pending"}
    monkeypatch.setattr(db, "connect", good)
    routing._now.now = retry_at + timedelta(seconds=1)
    assert gc.publish()["deleted"]
    assert gc.PostgresHub().newest() is None
