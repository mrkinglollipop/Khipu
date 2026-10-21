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


def test_never_on_configured_cleanup_attempts_once_then_stops_writing(gw, monkeypatch):
    never_published(gw)
    monkeypatch.setattr(routing, "_hub_configured", lambda: True)
    calls = []
    def down(**kwargs):
        calls.append(True)
        raise OSError("configured hub offline")
    monkeypatch.setattr(db, "connect", down)
    assert gc.publish() == {"ok": False, "error": "copy_publish_failed"}
    assert gc.status().get("cleanup_attempted") is True
    after_attempt = local_files()
    for _ in range(4):
        routing._now.now += timedelta(seconds=400)
        assert gc.publish() == {"ok": True, "disabled": True}
    assert gc.publish(force=True) == {"ok": True, "disabled": True}
    assert calls == [True]
    assert local_files() == after_attempt


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
    assert gc.publish() == {"ok": True, "disabled": True}
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
