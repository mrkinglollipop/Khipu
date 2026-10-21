"""Review regressions: publisher revocation, lock contention and bounded receipts."""
import json
from datetime import timedelta

import pytest

from khipu import routing, routing_gateway_copy as gc, routing_replication
from tests import routing_gateway_fixture as gateway_fixtures
from tests import test_routing_s12_gateway as gateway_tests

gateway_hub = gateway_fixtures.gateway_hub
hub = gateway_fixtures.hub
pg_cluster = gateway_fixtures.pg_cluster
gw = gateway_tests.gw


def publishers(hub):
    with hub.connect() as conn:
        return {row[0] for row in conn.execute("SELECT publisher FROM routing_gateway_copies")}


def test_restore_deletes_prior_row_and_off_revokes_every_used_id(gw, gateway_hub):
    gateway_tests.seed(gw)
    used = set()
    for _ in range(3):
        assert gc.publish(force=True)["published"]
        current = gc.status()["publisher"]
        used.add(current)
        assert publishers(gateway_hub) == {current}
        if len(used) < 3:
            routing_replication.restore()
    assert set(gc.status()["publishers"]) == used
    projection = gc.PostgresHub().newest()["projection"]
    # Replaying any retired row must still be covered by switch-off revocation.
    for publisher in used | {"unrelated-publisher"}:
        gc.PostgresHub().publish(publisher, projection, 600)
    gc._path().write_text("{broken publication state")
    gateway_tests.configure(gw, routing_cloud_copy=False)
    assert gc.publish(force=True)["deleted"]
    assert publishers(gateway_hub) == {"unrelated-publisher"}
    assert set(gc.status()["publishers"]) == used


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
@pytest.mark.parametrize("lose_journal", [False, True])
def test_off_recomputes_id_without_publication_state(gw, gateway_hub, damage, lose_journal):
    gateway_tests.seed(gw)
    assert gc.publish(force=True)["published"]
    if damage == "missing":
        gc._path().unlink()
    else:
        gc._path().write_text("{broken")
    if lose_journal:
        journal = routing.routing_dir() / "gateway-publishers.json"
        journal.unlink(missing_ok=True)
    gateway_tests.configure(gw, routing_cloud_copy=False)
    result = gc.publish(force=True)
    assert result.get("deleted"), result
    assert publishers(gateway_hub) == set()
    assert gc.answer(identity=None) is None


def test_busy_lock_skips_tick_without_changing_healthy_evidence(gw):
    gateway_tests.seed(gw)
    assert gc.publish(force=True)["published"]
    before = gc._path().read_bytes()
    with routing_replication._sync_lock():
        result = gc.publish(force=True)
    assert result == {"ok": True, "skipped": "busy"}
    assert gc._path().read_bytes() == before


def insert_receipt(conn, identity, now, *, flight=False):
    state = dict(copy_epoch="synthetic", copy_revision=1, copy_revision_at=routing._iso(now),
                 last_delivery_point=routing._iso(now), pending_points=0, expired=0)
    if flight:
        state["flight"] = dict(batch="live-batch", until=routing._iso(routing._now() + timedelta(seconds=60)))
    conn.execute("INSERT INTO routing_gateway_delivery(identity, state) VALUES (%s, %s::jsonb)",
                 (identity, json.dumps(state)))


def test_health_prunes_expired_receipts_but_retains_boundary_and_live_flight(gw, gateway_hub):
    now = routing._now()
    with gateway_hub.connect() as conn:
        insert_receipt(conn, "expired", now - timedelta(days=7, seconds=1))
        insert_receipt(conn, "boundary", now - timedelta(days=7))
        insert_receipt(conn, "active", now)
        insert_receipt(conn, "live-flight", now - timedelta(days=8), flight=True)
    rows = gc.PostgresHub().health_rows()
    with gateway_hub.connect() as conn:
        left = {r[0] for r in conn.execute("SELECT identity FROM routing_gateway_delivery")}
    assert left == {"boundary", "active", "live-flight"}
    assert {row["identity"] for row in rows} == {"active"}


def test_health_readback_has_live_window_cap_and_explicit_truncation(gw, gateway_hub, monkeypatch):
    gateway_tests.seed(gw)
    now = routing._now()
    monkeypatch.setattr(gc, "HEALTH_ROW_LIMIT", 2, raising=False)
    with gateway_hub.connect() as conn:
        for i in range(4):
            insert_receipt(conn, f"live-{i}", now - timedelta(seconds=i))
        insert_receipt(conn, "inactive", now - timedelta(hours=13))
    assert gc.publish(force=True)["published"]
    state = gc.status()
    assert len(state["gateway_rows"]) == 2
    assert state["gateway_rows_truncated"] is True
    assert {r["identity"] for r in state["gateway_rows"]} == {"live-0", "live-1"}
    from khipu import routing_health
    health = routing_health.doctor_block(routing.read_view(), now, routing.settings())
    assert "gateway delivery snapshot truncated" in health["missing"]


def test_quiet_health_snapshot_is_not_read_back_on_every_publish(gw, monkeypatch):
    gateway_tests.seed(gw)
    assert gc.publish(force=True)["published"]
    calls = []
    original = gc.PostgresHub.health_rows
    def record(self, **kwargs):
        calls.append(True)
        return original(self, **kwargs)
    monkeypatch.setattr(gc.PostgresHub, "health_rows", record)
    gateway_tests.configure(gw, routing_cloud_copy_debounce=0)
    routing._now.now += timedelta(seconds=1)
    routing.set_note("a new revision", via="app")
    assert gc.publish()["published"]
    assert calls == []
    routing._now.now += timedelta(seconds=60)
    assert gc.publish()["published"]
    assert calls == [True]



def test_failed_retired_id_delete_keeps_history_and_refuses_new_publication(gw, gateway_hub):
    gateway_tests.seed(gw)
    assert gc.publish(force=True)["published"]
    previous = gc.status()["publisher"]
    routing_replication.restore()
    class Interrupted(gc.PostgresHub):
        def delete(self, publisher):
            super().delete(publisher)
            raise OSError("synthetic response lost after delete committed")
    assert gc.publish(force=True, hub=Interrupted()) == {"ok": False, "error": "copy_publish_failed"}
    assert gc.status()["publisher"] == previous
    assert publishers(gateway_hub) == set()
    assert previous in gc.status()["publishers"] and len(gc.status()["publishers"]) == 2
    gc._path().unlink()
    assert gc.publish(force=True)["published"]
    current = gc.status()["publisher"]
    assert current != previous and publishers(gateway_hub) == {current}
    gateway_tests.configure(gw, routing_cloud_copy=False)
    assert gc.publish(force=True)["deleted"]
    assert publishers(gateway_hub) == set()


def test_legacy_state_without_journal_keeps_previous_id_across_restore(gw, gateway_hub):
    gateway_tests.seed(gw)
    assert gc.publish(force=True)["published"]
    state = gc.status()
    previous = state["publisher"]
    state.pop("publishers")
    gc._save(state)
    (routing.routing_dir() / "gateway-publishers.json").unlink()
    routing_replication.restore()
    assert gc.publish(force=True)["published"]
    assert set(gc.status()["publishers"]) == {previous, gc.status()["publisher"]}
    assert publishers(gateway_hub) == {gc.status()["publisher"]}


def test_gateway_activity_prunes_receipts_in_bounded_batches(gw, gateway_hub, monkeypatch):
    gateway_tests.seed(gw)
    assert gc.publish(force=True)["published"]
    monkeypatch.setattr(gc, "DELIVERY_PRUNE_LIMIT", 2)
    with gateway_hub.connect() as conn:
        for i in range(5):
            insert_receipt(conn, f"expired-{i}", routing._now() - timedelta(days=8))
    for remaining in (3, 1, 0):
        body = gc.answer(identity="synthetic-gateway")
        assert body["available"]
        with gateway_hub.connect() as conn:
            count = conn.execute("SELECT count(*) FROM routing_gateway_delivery WHERE identity LIKE 'expired-%%'").fetchone()[0]
        assert count == remaining
    batch = body["routing_changes"]["batch"]
    acked = gc.answer(identity="synthetic-gateway", ack=batch)
    assert acked["delivery"]["acked_through"] == body["version"]
