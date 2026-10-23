"""Gateway privacy regressions: fake Mac state and disposable loopback PostgreSQL."""
import json
import threading

import pytest
from khipu import routing, routing_gateway_copy as gc, agent_view
from tests import routing_gateway_fixture as gf
from tests import test_routing_s12_gateway as gt

gateway_hub, hub, pg_cluster = gf.gateway_hub, gf.hub, gf.pg_cluster
gw = gt.gw
SWITCHES = ("routing_cloud_copy", "routing_cloud_copy_rules", "routing_cloud_copy_hold_words")


def test_gateway_cuts_paths_before_storage_and_serving(gw):
    paths = ["/opt/company/restricted/merger-plan.txt", "./restricted/merger-plan.txt",
             r"C:\Sensitive\merger-plan.txt", "/etc/company/restricted.conf",
             "../restricted/merger-plan.txt", r"\\server\restricted\plan.txt",
             "restricted/merger-plan.txt", "%2Fopt%2Fcompany%2Frestricted.txt"]
    ordinary = "Keep CI/CD and/or 3/4 and ordinary owner words."
    note = ordinary + " " + " ; ".join(paths)
    routing.set_setting("routing_hold_chars", 0)
    routing.set_note(note, via="app")
    routing.add_rule(note, via="app")
    routing.add_hold(note, reason="other", scope="all", owner=True, via="app")
    assert gc.publish(force=True)["published"]
    stored = gc.PostgresHub().newest()["projection"]
    gw["autopublish"] = False
    error, served = gt.call(gw, session_id="test:paths", label="test-token")
    assert not error
    for body in (stored, served):
        for text in (body["note"]["text"], body["rules"][0]["text"], body["holds"][0]["text"]):
            assert ordinary in text
            assert not any(path in text for path in paths), text
            assert "merger-plan.txt" not in text and "restricted.conf" not in text
    assert routing.read_view()["note"]["text"] == note


@pytest.mark.parametrize("switch", SWITCHES)
def test_revocation_acknowledges_after_inflight_upload(gw, switch):
    routing.set_note("SYNTHETIC_OWNER_NOTE", via="app")
    routing.add_hold("SYNTHETIC_OWNER_HOLD", reason="other", scope="all", owner=True, via="app")
    arrived, release, acknowledged, attempted = (threading.Event() for _ in range(4))
    result, errors, order = {}, [], []
    original_acquire = routing._acquire

    def acquire(fd):
        if threading.current_thread().name == "owner-revocation":
            attempted.set()
        return original_acquire(fd)
    gw["patch"].setattr(routing, "_acquire", acquire)

    class PausedRealHub(gc.PostgresHub):
        def publish(self, publisher, projection, stale_after):
            arrived.set()
            assert release.wait(10), "test did not release publication"
            stamp = super().publish(publisher, projection, stale_after)
            order.append("committed")
            return stamp

    def upload():
        try:
            result.update(gc.publish(force=True, hub=PausedRealHub()))
        except BaseException as exc:
            errors.append(exc)

    def revoke():
        try:
            routing.set_setting(switch, False)
            order.append("acknowledged")
            acknowledged.set()
        except BaseException as exc:
            errors.append(exc)

    publisher = threading.Thread(target=upload)
    owner = threading.Thread(target=revoke, name="owner-revocation")
    publisher.start()
    try:
        assert arrived.wait(10)
        owner.start()
        assert attempted.wait(10)
        # A correct setter waits, so the controller releases publication.
        acknowledged.wait(0.2)
    finally:
        release.set()
        publisher.join(10)
        if owner.ident is not None:
            owner.join(10)
    assert not publisher.is_alive() and not owner.is_alive()
    assert not errors, errors
    assert result.get("published"), result
    assert order == ["committed", "acknowledged"]
    assert acknowledged.is_set() and routing.settings()[switch] is False
    assert gc.publish(force=True)["ok"]
    stored = gc.PostgresHub().newest()
    if switch == "routing_cloud_copy":
        assert stored is None
    else:
        text = json.dumps(stored["projection"])
        sentinel = "SYNTHETIC_OWNER_HOLD" if switch.endswith("hold_words") else "SYNTHETIC_OWNER_NOTE"
        assert sentinel not in text


@pytest.mark.parametrize("switch", SWITCHES)
def test_revocation_before_publish_boundary_refuses_stale_projection(gw, switch):
    routing.set_note("SYNTHETIC_OWNER_NOTE", via="app")
    routing.add_hold("SYNTHETIC_OWNER_HOLD", reason="other", scope="all", owner=True, via="app")
    original = agent_view.scrub_strict

    def revoke_after_projection(*args, **kwargs):
        projection = original(*args, **kwargs)
        routing.set_setting(switch, False)
        return projection

    gw["patch"].setattr(agent_view, "scrub_strict", revoke_after_projection)
    result = gc.publish(force=True)
    assert routing.settings()[switch] is False
    assert not result.get("published"), result
    assert gc.PostgresHub().newest() is None
