"""The cleanup give-up is visible and the owner's retry goes past the cap."""
import contextlib
import json
from datetime import timedelta

import pytest

from khipu import db, routing, routing_gateway_copy as gc, routing_health
from khipu import routing_replication as replication
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
    + [("lost_marker", 0, 0, I)]
    + [("restore", 0, 0, I)]                            # identity recovery keeps the old target
    + [("auto", 0, 0, I)]
    + [("force_up", 0, 0, "")]                          # deletes the old target and the new id
    + [("auto", 0, 0, "")]
)


def recorded_targets():
    """Every publisher id the local files name, by raw field."""
    state = json.loads(gc._path().read_text())
    ids = {state.get("publisher"), state.get("cleanup_publisher")}
    ids.update(state.get("publishers") or [], state.get("cleanup_publishers") or [])
    with contextlib.suppress(OSError, ValueError):
        ids.update(json.loads(gc._publishers_path().read_text()))
    return {i for i in ids if isinstance(i, str)}


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
            targets, up = recorded_targets(), Up()
            assert gc.publish(force=True, cleanup_only=True, hub=up)["deleted"]
            assert targets <= set(up.deleted), (i, step, targets - set(up.deleted))
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
        elif step == "restore":
            assert replication.restore()["mac"]
            gc.publish()
        elif step == "lost_marker":
            # A first speculative cleanup (no recorded failure) whose outcome was lost.
            state = gc._load()
            with contextlib.suppress(FileNotFoundError):
                gc._publishers_path().unlink()
            gc._save(dict(cleanup_attempted=True, cleanup_publisher=state.get("publisher")
                          or state.get("cleanup_publisher") or gc._publisher(create=False)))
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


class RefusesCurrent:
    """Deletes every id except this Mac's current one, which fails."""

    def __init__(self):
        self.deleted = []

    def delete(self, publisher):
        self.deleted.append(publisher)
        if publisher == gc._publisher(create=False):
            raise OSError("current row busy")


def never_on_mac(gw, monkeypatch):
    gateway_tests.configure(gw, routing_cloud_copy=False)
    routing._id_salt()
    with routing._lock():
        routing._mac_locked()
    monkeypatch.setattr(routing, "_hub_configured", lambda: True)


CAP = 16


def test_restore_failed_retry_cycles_keep_a_bounded_visible_target_list(gw, monkeypatch):
    assert getattr(gc, "_CLEANUP_MAX_TARGETS", CAP) == CAP
    never_on_mac(gw, monkeypatch)
    hub_down(monkeypatch)
    first = []
    for n in range(32):
        assert replication.restore()["mac"]
        first.append(gc._publisher(create=False))
        assert gc.publish(force=True, cleanup_only=True)["ok"] is False
    targets = gc._cleanup_targets(gc._load())
    assert len(targets) == CAP
    assert targets == set(first[:CAP])  # every admitted id kept
    state = gc.status()
    assert state["cleanup_full"] is True
    assert "gateway-cleanup-full" in health()["reasons"]
    assert any("too many unresolved" in w.lower() for w in routing.doctor_block()["warnings"])


def test_each_target_leaves_the_list_after_its_own_delete(gw, monkeypatch):
    never_on_mac(gw, monkeypatch)
    hub = RefusesCurrent()
    for n in range(128):
        assert replication.restore()["mac"]
        gc.publish(force=True, cleanup_only=True, hub=hub)
        assert len(gc._cleanup_targets(gc._load())) <= 1, n
    assert len(hub.deleted) <= 2 * 128
    assert not gc.status().get("cleanup_full")
    # The last id clears once its delete succeeds.
    assert gc.publish(force=True, cleanup_only=True, hub=Up())["deleted"]
    assert gc._cleanup_targets(gc._load()) == set()


class Crash(BaseException):
    """The process dies: nothing after this point runs."""


class Hub:
    """A fake hub holding rows; deletes can fail or kill the process."""

    def __init__(self, rows=()):
        self.rows, self.deleted, self.configured, self.fail = set(rows), [], True, False
        self.published, self.unconfirmed = [], set()  # unconfirmed: committed, then the process died
        self.crash = None  # None, "before" or "after", for the next delete only

    def delete(self, publisher):
        if not self.configured or self.fail:
            raise OSError("hub offline")
        crash, self.crash = self.crash, None
        if crash == "before":
            raise Crash()
        self.rows.discard(publisher)
        self.deleted.append(publisher)
        if crash == "after":
            self.unconfirmed.add(publisher)
            raise Crash()

    def publish(self, publisher, projection, stale_after):
        if not self.configured or self.fail:
            raise OSError("hub offline")
        self.rows.add(publisher)
        self.published.append(publisher)
        return routing._iso(routing._now())

    def health_rows(self, **kwargs):
        return []


def test_unconfigured_hub_is_never_evidence_of_cleanup(gw, monkeypatch):
    never_on_mac(gw, monkeypatch)
    target = gc._publisher(create=False)
    hub = Hub([target])
    hub.fail = True
    for _ in range(8):
        gc.publish(hub=hub)
        routing._now.now += timedelta(seconds=400)
    assert gc.status()["cleanup_gave_up"] is True
    (routing.routing_dir() / "mac-id").unlink()  # the current identity is gone
    monkeypatch.setattr(routing, "_hub_configured", lambda: False)
    hub.fail, hub.configured = False, False
    out = gc.publish(force=True, cleanup_only=True, hub=hub)
    assert out == {"ok": False, "error": "cleanup_waiting_for_hub"}
    assert gc._cleanup_targets(gc._load()) == {target}
    assert gc.status()["cleanup_gave_up"] is True
    monkeypatch.setattr(routing, "_hub_configured", lambda: True)
    hub.configured = True
    assert gc.publish(force=True, cleanup_only=True, hub=hub)["deleted"]
    assert hub.rows == set() and hub.deleted == [target]


def test_a_confirmed_delete_is_not_redone_after_a_crash(gw, monkeypatch):
    never_on_mac(gw, monkeypatch)
    old = gc._publisher(create=False)
    hub = Hub([old])
    hub.fail = True
    gc.publish(hub=hub)  # the first speculative cleanup fails: old is pending
    while True:
        replication.restore()
        current = gc._publisher(create=False)
        if current < old:  # the current id is deleted first
            break
    hub.fail, hub.crash = False, None
    hub_crash_on = old

    class CrashOnOld(Hub):
        def delete(self, publisher):
            if publisher == hub_crash_on:
                raise Crash()
            super().delete(publisher)

    crashing = CrashOnOld([old])
    with pytest.raises(Crash):
        gc.publish(force=True, cleanup_only=True, hub=crashing)
    assert crashing.deleted == [current]
    assert gc._cleanup_targets(gc._load()) == {old}
    retry = Hub([old])
    assert gc.publish(force=True, cleanup_only=True, hub=retry)["deleted"]
    assert retry.deleted == [old]


OPS = ("tick", "tick", "tick", "force", "hub_ok", "hub_fail", "crash_before", "crash_after",
       "restore", "unconfigure", "configure", "switch_on", "switch_off", "publish")


def journal():
    return set(gc._journal())


def recorded(state):
    return gc._pending(state)


@pytest.mark.parametrize("seed", range(40))
def test_random_cleanup_sequences_keep_the_invariant(gw, monkeypatch, seed):
    import random

    rng = random.Random(seed)
    configured = {"on": True}
    monkeypatch.setattr(routing, "_hub_configured", lambda: configured["on"])
    hub = Hub()
    # Start published (a row on the hub), then switched off.
    assert gc.publish(force=True, hub=hub)["published"]
    gateway_tests.configure(gw, routing_cloud_copy=False)
    on = False
    episode_deleted = set()  # ids confirmed deleted in this cleanup (since switch-off)
    for step in range(60):
        op = rng.choice(OPS)
        before_state, deletes, pubs = gc._load(), len(hub.deleted), len(hub.published)
        before_pending = recorded(before_state)
        routing._now.now += timedelta(seconds=rng.choice((1, 60, 400)))
        try:
            if op == "tick":
                gc.publish(hub=hub)
            elif op == "force":
                gc.publish(force=True, cleanup_only=True, hub=hub)
            elif op == "publish" and on:
                if gc.publish(force=True, hub=hub).get("published"):
                    episode_deleted.clear()
            elif op in ("hub_ok", "hub_fail"):
                hub.fail = op == "hub_fail"
            elif op in ("crash_before", "crash_after"):
                hub.crash = op.split("_")[1]
            elif op == "restore":
                replication.restore()
            elif op in ("unconfigure", "configure"):
                configured["on"] = hub.configured = op == "configure"
            elif op in ("switch_on", "switch_off"):
                on = op == "switch_on"
                gateway_tests.configure(gw, routing_cloud_copy=on)
                if on:
                    # Switching on ends the cleanup: the copy's id is journaled
                    # again before any upload, so a later cleanup starts anew.
                    episode_deleted.clear()
        except Crash:
            hub.crash = None
        state, confirmed = gc._load(), set(hub.deleted[deletes:])
        episode_deleted -= set(hub.published[pubs:])  # published again: a new row
        where = (seed, step, op)
        # Every row this Mac may own is recorded, or is its current id.
        assert hub.rows <= recorded(state) | {gc._publisher(create=False)}, where
        if not on:
            # A target leaves the pending set only after its own confirmed delete.
            assert before_pending - recorded(state) <= confirmed, where
            # Nothing re-adds a target: no id is deleted twice in one cleanup.
            assert not (confirmed & episode_deleted), where
            # A delete whose process died before recording it is not
            # confirmed: repeating it is the only safe move.
            episode_deleted |= confirmed - hub.unconfirmed
            # Cleaned means nothing pending and nothing left on the hub.
            if gc._cleaned(state):
                assert not recorded(state) and not hub.rows, where
            # The visible states are pure functions of the pending set and counts.
            shown = gc.status()
            assert bool(shown.get("cleanup_gave_up")) == gc.gave_up(state), where
            assert bool(shown.get("cleanup_full")) == gc.cleanup_full(state), where
            if not shown.get("cleanup_gave_up"):
                assert bool(shown.get("cleanup_interrupted")) == gc.interrupted(state), where
        episode_deleted -= hub.unconfirmed
        hub.unconfirmed.clear()
    # Liveness: with the hub back, automatic ticks remove every row, or it is shown.
    gateway_tests.configure(gw, routing_cloud_copy=False)
    configured["on"] = hub.configured = True
    hub.fail, hub.crash = False, None
    for _ in range(30):
        routing._now.now += timedelta(seconds=400)
        gc.publish(hub=hub)
    shown = gc.status()
    visible = shown.get("cleanup_gave_up") or shown.get("cleanup_interrupted") or shown.get("cleanup_full")
    assert not hub.rows or visible, (seed, hub.rows, shown)
    if visible:
        assert gc.publish(force=True, cleanup_only=True, hub=hub)["ok"] is True
        assert not hub.rows, seed
