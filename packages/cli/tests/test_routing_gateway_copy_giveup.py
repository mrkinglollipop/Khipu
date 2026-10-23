"""The cleanup give-up is visible and the owner's retry goes past the cap."""
import contextlib
from datetime import timedelta

import pytest

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
    assert gc.status()["cleanup_failures"] == 6
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
    assert gc.status()["cleanup_failures"] == 7
    assert gc.status()["cleanup_gave_up"] is True
    assert health()["gateway"]["status"] == "red"


def test_forced_failure_after_success_still_stops_at_six(gw, monkeypatch):
    calls = give_up(gw, monkeypatch)

    class Up:
        def delete(self, publisher):
            pass

    assert gc.publish(force=True, hub=Up())["deleted"]
    gc._save(["a" * 64], path=gc._publishers_path())  # a copy is known again
    before = len(calls)
    assert gc.publish(force=True)["ok"] is False
    for _ in range(20):
        routing._now.now += timedelta(seconds=400)
        gc.publish()
    assert len(calls) - before <= 6
    assert gc.status()["cleanup_gave_up"] is True


def test_publish_failures_do_not_count_as_cleanup_failures(gw, monkeypatch):
    monkeypatch.setattr(routing, "_hub_configured", lambda: True)
    gateway_tests.seed(gw)
    calls = []

    def down(**kwargs):
        calls.append(True)
        raise OSError("hub offline")

    monkeypatch.setattr(db, "connect", down)
    for _ in range(8):
        gc.publish(force=True)
    gateway_tests.configure(gw, routing_cloud_copy=False)
    assert not gc.status().get("cleanup_gave_up")
    assert "gateway-cleanup-stopped" not in health()["reasons"]
    before = len(calls)
    routing._now.now += timedelta(seconds=400)
    gc.publish()
    assert len(calls) > before


def test_cleanup_only_retry_never_publishes_while_copy_is_on(gw, monkeypatch):
    published = []

    class Spy:
        def publish(self, *a):
            published.append(a)

        def delete(self, publisher):
            published.append("delete")

    assert gc.publish(force=True, hub=Spy(), cleanup_only=True) == {"ok": False, "error": "copy_is_on"}
    assert published == []


class Up:
    """A reachable fake hub."""

    def __init__(self):
        self.deleted, self.published = [], []

    def delete(self, publisher):
        self.deleted.append(publisher)

    def publish(self, publisher, projection, stale_after):
        self.published.append(publisher)
        return routing._iso(routing._now())

    def health_rows(self, **kwargs):
        return []


def hub_down(monkeypatch):
    calls = []

    def down(**kwargs):
        calls.append(True)
        raise OSError("configured hub offline")

    monkeypatch.setattr(db, "connect", down)
    return calls


@pytest.mark.parametrize("legacy_failures, automatic_left", [(6, 0), (3, 3)])
def test_legacy_shared_counter_is_read_as_cleanup_failures(gw, monkeypatch, legacy_failures, automatic_left):
    # State written before the cleanup counter existed: one shared count.
    gateway_tests.configure(gw, routing_cloud_copy=False)
    routing._id_salt()
    with routing._lock():
        routing._mac_locked()
    monkeypatch.setattr(routing, "_hub_configured", lambda: True)
    gc._save(dict(cleanup_attempted=True, error="copy_publish_failed", failures=legacy_failures,
                  retry_at=routing._iso(routing._now() - timedelta(seconds=1))))
    calls = hub_down(monkeypatch)
    assert bool(gc.status().get("cleanup_gave_up")) is (automatic_left == 0)
    for _ in range(20):
        routing._now.now += timedelta(seconds=400)
        gc.publish()
    assert len(calls) == automatic_left
    assert gc.status()["cleanup_gave_up"] is True
    assert gc.status()["cleanup_failures"] == 6
    assert "gateway-cleanup-stopped" in health()["reasons"]


def published_mac(gw, monkeypatch):
    monkeypatch.setattr(routing, "_hub_configured", lambda: True)
    assert gc.publish(force=True, hub=Up())["published"]
    gateway_tests.configure(gw, routing_cloud_copy=False)


def test_successful_publication_starts_a_fresh_cleanup_budget(gw, monkeypatch):
    published_mac(gw, monkeypatch)
    calls = hub_down(monkeypatch)
    for _ in range(20):
        gc.publish()
        routing._now.now += timedelta(seconds=400)
    assert gc.status()["cleanup_gave_up"] is True
    gateway_tests.configure(gw, routing_cloud_copy=True)
    assert gc.publish(force=True, hub=Up())["published"]
    gateway_tests.configure(gw, routing_cloud_copy=False)
    before = len(calls)
    assert gc.publish() == {"ok": False, "error": "copy_publish_failed"}
    assert gc.status()["cleanup_failures"] == 1
    assert not gc.status().get("cleanup_gave_up")
    for _ in range(20):
        routing._now.now += timedelta(seconds=400)
        gc.publish()
    assert len(calls) - before == 6


# (step, hub calls it makes, cleanup_failures after, visible state after)
# Visible states: "" (fresh, retrying, cleaned), "stopped", "interrupted".
S, I = "stopped", "interrupted"
LIFECYCLE = (
    [("auto", 1, n, S if n == 6 else "") for n in range(1, 7)]  # fresh -> retrying -> stopped
    + [("auto", 0, 6, S), ("auto", 0, 6, S)]            # stopped: automatic ticks do nothing
    + [("rekey_pending", 0, 6, S)]                      # new id, no publication yet: still stopped
    + [("crash_marker", 0, 6, S)]                       # died before the delete: still stopped
    + [("force", 1, 7, S)]                              # owner retry passes the cap, fails
    + [("cleanup_only_on", 0, 7, S)]                    # copy on: no writes, no hub calls
    + [("publish_up", 0, 0, "")]                        # new publication: fresh budget
    + [("auto", 1, n, S if n == 6 else "") for n in range(1, 7)]
    + [("auto", 0, 6, S)]
    + [("force_up", 0, 0, "")]                          # owner retry succeeds: cleared
    + [("auto", 0, 0, "")]                              # cleaned: idle
    + [("lost_marker", 0, 0, I)]                        # first cleanup's outcome lost: visible
    + [("auto", 0, 0, I), ("auto", 0, 0, I)]            # never retried automatically
    + [("publish_up", 0, 0, "")]                        # a publication clears it
    + [("lost_marker", 0, 0, I)]
    + [("force", 1, 1, "")]                             # owner retry runs the delete: retrying
    + [("lost_marker", 0, 0, I)]
    + [("force_up", 0, 0, "")]                          # owner retry succeeds: cleared
)


def test_counter_lifecycle_table(gw, monkeypatch):
    published_mac(gw, monkeypatch)
    calls = hub_down(monkeypatch)
    for i, (step, made, cleanup_failures, shown) in enumerate(LIFECYCLE):
        before = len(calls)
        routing._now.now += timedelta(seconds=400)
        if step == "auto":
            gc.publish()
        elif step == "force":
            assert gc.publish(force=True, cleanup_only=True)["ok"] is False
        elif step == "force_up":
            assert gc.publish(force=True, cleanup_only=True, hub=Up())["deleted"]
        elif step == "publish_up":
            gateway_tests.configure(gw, routing_cloud_copy=True)
            assert gc.publish(force=True, hub=Up())["published"]
            gateway_tests.configure(gw, routing_cloud_copy=False)
        elif step == "rekey_pending":
            state = gc._load()
            gc._save(dict(state, publisher="b" * 64))
            gateway_tests.configure(gw, routing_cloud_copy=True)
            assert gc.publish(hub=Up()).get("pending") is True
            gateway_tests.configure(gw, routing_cloud_copy=False)
        elif step == "crash_marker":
            # The speculative cleanup's attempt marker, with the delete's outcome lost.
            state = gc._load()
            gc._publishers_path().unlink()
            gc._save(dict(cleanup_attempted=True, cleanup_publisher=state["publisher"],
                          **gc._counters(state)))
            gc.publish()
        elif step == "lost_marker":
            # A first speculative cleanup (no recorded failure) whose outcome was lost.
            state = gc._load()
            with contextlib.suppress(FileNotFoundError):
                gc._publishers_path().unlink()
            gc._save(dict(cleanup_attempted=True, cleanup_publisher=state.get("publisher")
                          or state.get("cleanup_publisher")))
            gc.publish()
        elif step == "cleanup_only_on":
            gateway_tests.configure(gw, routing_cloud_copy=True)
            files = gc._path().read_bytes()
            spy = Up()
            assert gc.publish(force=True, cleanup_only=True, hub=spy) == {"ok": False, "error": "copy_is_on"}
            assert gc._path().read_bytes() == files and not spy.deleted and not spy.published
            assert not gc.status().get("cleanup_gave_up")
            gateway_tests.configure(gw, routing_cloud_copy=False)
        state = gc.status()
        visible = S if state.get("cleanup_gave_up") else I if state.get("cleanup_interrupted") else ""
        assert (len(calls) - before, state.get("cleanup_failures", 0), visible) \
            == (made, cleanup_failures, shown), (i, step)
        reasons = health()["reasons"]
        assert ("gateway-cleanup-stopped" in reasons) is (shown == S), (i, step)
        assert ("gateway-cleanup-interrupted" in reasons) is (shown == I), (i, step)
        warned = " ".join(routing.doctor_block()["warnings"])
        assert ("stopped after 6" in warned) is (shown == S), (i, step)
        assert ("interrupted" in warned) is (shown == I), (i, step)
        if state.get("retry_at"):
            assert routing._parse(state["retry_at"]) - routing._now() <= timedelta(seconds=300)
