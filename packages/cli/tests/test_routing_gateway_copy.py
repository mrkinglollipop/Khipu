"""Mac publisher and gateway against synthetic state and disposable PostgreSQL."""

from tests import routing_gateway_fixture as gateway_fixtures
from tests import test_routing_s12_gateway as gateway_tests
import copy
import json
from datetime import timedelta
import pytest
from khipu import routing, routing_gateway_copy as gc, mcp_server as ms
from tests.test_routing_s12_gateway import configure, seed_planted, string_leaves
from tests.test_routing_acceptance import assert_gateway_fields

gateway_hub = gateway_fixtures.gateway_hub
hub = gateway_fixtures.hub
pg_cluster = gateway_fixtures.pg_cluster
gw = gateway_tests.gw
seed = gateway_tests.seed
call = gateway_tests.call





def test_gateway_does_not_serve_its_local_routing_state(gw, monkeypatch):
    from khipu import db
    seed(gw)
    gw["autopublish"] = False
    def unavailable(**kwargs):
        raise OSError("synthetic unavailable hub")
    monkeypatch.setattr(db, "connect", unavailable)
    error, body = call(gw, session_id="client:s1", label="test")
    assert not error and body["available"] is False





def test_mac_to_disposable_hub_to_empty_gateway(gw, gateway_hub, monkeypatch):
    seed(gw)
    assert gc.publish(force=True)["published"]
    original = gc.PostgresHub().newest()["projection"]
    # A separate container has neither the Mac's switches nor its routing log.
    empty = gw["tmp"] / "container"
    empty.mkdir()
    monkeypatch.setenv("KHIPU_DATA_DIR", str(empty))
    monkeypatch.setenv("HOME", str(empty))
    monkeypatch.setattr(routing, "read_view", lambda *a, **k: pytest.fail("gateway read local view"))
    monkeypatch.setattr(routing, "settings", lambda: pytest.fail("gateway read local switches"))
    gw["autopublish"] = False
    error, body = call(gw, session_id="client:s1", label="test")
    assert not error and body["available"] is True
    assert_gateway_fields(body)
    assert body["accounts"] == original["accounts"]
    assert {a["label"] for a in body["accounts"]} == {"Claude", "Codex", "Grok"}
    assert body["as_of"] and not body["stale"]
    batch = body["routing_changes"]["batch"]
    # Recreate transport/server objects as a container replacement would.
    assert gc.PostgresHub().newest()
    error, again = call(gw, session_id="client:s1", label="test")
    assert not error and again["routing_changes"]["batch"] == batch
    error, acked = call(gw, session_id="client:s1", label="test", ack=batch)
    assert not error and acked["delivery"]["acked_through"] == original["version"]
    error, duplicate = call(gw, session_id="client:s1", label="test", ack=batch)
    assert not error and "routing_changes" not in duplicate


@pytest.mark.parametrize("words", [False, True])
@pytest.mark.parametrize("rules", [False, True])
def test_every_written_string_leaf_is_private_and_switch_gated(gw, words, rules):
    private = str(gw["tmp"] / "private-login")
    routing.set_note("owner note at " + private, via="app")
    seed_planted(gw)
    configure(gw, routing_cloud_copy_hold_words=words, routing_cloud_copy_rules=rules)
    raw_mac = routing._local_mac()
    # The pending proposal contains text no cloud switch authorizes.
    gw["patch"].setenv("KHIPU_SESSION_ID", "synthetic-proposal-session")
    shown = routing.conversation_view()
    target = next(r for r in shown["rules"] if not r.get("pending"))
    routing.request_rule("PROPOSALTEXT", n=target["n"], base=target["owner_revision"],
                         author={"kind": "conversation", "harness": "codex", "session": "SESSIONSECRET", "project": "PROJECTSECRET"})
    class InspectHub(gc.PostgresHub):
        def publish(self, publisher, projection, stale_after):
            written = dict(publisher=publisher, projection=projection, stale_after=stale_after)
            strings = list(string_leaves(written))
            forbidden = [private, str(gw["tmp"]), "projSECRETPROJ", "claude:s", "CONVHOLDWORDS",
                         "SESSIONSECRET", "PROJECTSECRET", "PROPOSALTEXT", raw_mac]
            if not words:
                forbidden += ["OWNERHOLDWORDS", "Hold words for maintenance"]
            for sentinel in forbidden:
                assert sentinel and not any(sentinel in leaf for leaf in strings), sentinel
            assert_gateway_fields(projection)
            if words:
                assert any("OWNERHOLDWORDS" in leaf for leaf in strings)
            if not rules:
                assert not (set(projection) & {"rules", "note", "priority"})
            return super().publish(publisher, projection, stale_after)
    # Assertion failures must escape the publisher's closed error response.
    inspection = InspectHub()
    observed = []
    original = inspection.publish
    def inspect(*args):
        try:
            result = original(*args)
        except AssertionError as exc:
            observed.append(exc)
            raise
        observed.append(True)
        return result
    inspection.publish = inspect
    result = gc.publish(force=True, hub=inspection)
    assert observed == [True], observed
    assert result["ok"]


def test_no_row_off_switch_delete_stale_and_newest_mac(gw, gateway_hub):
    gw["autopublish"] = False
    error, body = call(gw)
    assert not error and body["available"] is False and body["reason"] == ms.GATEWAY_COPY_OFF_REASON
    seed(gw)
    assert gc.publish(force=True)["published"]
    conn = gateway_hub.connect()
    with conn.cursor() as cur:
        cur.execute("UPDATE routing_gateway_copies SET published_at=clock_timestamp()-interval '11 minutes'")
    conn.commit()
    # Use hub clock for staleness; fake routing clock is deliberately older.
    with conn.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        real_now = cur.fetchone()[0]
    body = gc.answer(identity=None, now=real_now)
    assert body["stale"] and "this Mac has not reported since" in body["stale_message"]
    projection = copy.deepcopy(body)
    for key in ("as_of", "stale", "stale_message", "delivery", "routing_changes"):
        projection.pop(key, None)
    projection["accounts"][0]["label"] = "Newer Mac Account"
    routing._now.now = real_now
    gc.PostgresHub().publish("different-salted-publisher", projection, 600)
    assert gc.answer(identity=None)["accounts"][0]["label"] == "Newer Mac Account"
    configure(gw, routing_cloud_copy=False)
    assert gc.publish(force=True)["deleted"]
    with conn.cursor() as cur:
        cur.execute("SELECT publisher FROM routing_gateway_copies")
        assert cur.fetchall() == [("different-salted-publisher",)]


def test_debounce_last_change_retry_and_switch_off(gw):
    seed(gw)
    class Hub:
        def __init__(self):
            self.calls = []
            self.offline = False
        def publish(self, publisher, projection, stale_after):
            self.calls.append(copy.deepcopy(projection))
            if self.offline:
                raise OSError("PRIVATE_DSN_PATH")
            return routing._iso(routing._now())
        def delete(self, publisher):
            self.calls.append("deleted")
        def health_rows(self, **kwargs):
            return []
    hub = Hub()
    assert gc.publish(hub=hub)["pending"]
    routing._now.now += timedelta(seconds=20)
    routing.set_note("latest burst", via="app")
    assert gc.publish(hub=hub)["pending"]
    routing._now.now += timedelta(seconds=29)
    assert gc.publish(hub=hub)["pending"]
    assert not hub.calls
    routing._now.now += timedelta(seconds=1)
    assert gc.publish(hub=hub)["published"]
    assert hub.calls[-1]["note"]["text"] == "latest burst"
    assert gc.publish(hub=hub)["unchanged"]
    routing.set_note("offline", via="app")
    hub.offline = True
    result = gc.publish(force=True, hub=hub)
    assert result == {"ok": False, "error": "copy_publish_failed"}
    assert "PRIVATE_DSN_PATH" not in json.dumps(gc.status())
    hub.offline = False
    routing._now.now += timedelta(seconds=31)
    result = gc.publish(hub=hub)
    assert result.get("published"), result
    configure(gw, routing_cloud_copy=False)
    assert gc.publish(hub=hub)["deleted"]
    assert hub.calls[-1] == "deleted"


def test_ledger_failure_rolls_back_ack_and_never_claims_success(gw, gateway_hub, monkeypatch):
    seed(gw)
    assert gc.publish(force=True)["ok"]
    gw["autopublish"] = False
    error, body = call(gw, session_id="client:s1", label="test")
    assert not error
    batch = body["routing_changes"]["batch"]
    original = gc.PostgresHub.delivery
    def failed(self, identity, body, ack, now):
        raise OSError("synthetic receipt store unavailable")
    monkeypatch.setattr(gc.PostgresHub, "delivery", failed)
    error, refused = call(gw, session_id="client:s1", label="test", ack=batch)
    assert not error and refused == {"available": False, "reason": gc.UNAVAILABLE}
    monkeypatch.setattr(gc.PostgresHub, "delivery", original)
    error, again = call(gw, session_id="client:s1", label="test")
    assert not error and again["routing_changes"]["batch"] == batch
    error, acked = call(gw, session_id="client:s1", label="test", ack=batch)
    assert not error and acked["delivery"]["acked_through"] == body["version"]


def test_manual_cli_and_daemon_publish(gw, monkeypatch):
    from khipu import cli
    seen = []
    monkeypatch.setattr(gc, "publish", lambda **kw: seen.append(kw) or {"ok": True})
    args = cli.build_parser().parse_args(["route", "copy", "publish"])
    assert args.func(args) == 0
    routing.poll_refold()
    assert seen[0]["force"] is True and "force" not in seen[1]


def test_failed_commit_rolls_back_receipt(gw, gateway_hub, monkeypatch):
    import contextlib
    from khipu import db
    seed(gw)
    assert gc.publish(force=True)["ok"]
    gw["autopublish"] = False
    error, issued = call(gw, session_id="commit:s1", label="test")
    assert not error
    batch = issued["routing_changes"]["batch"]
    connections = []
    @contextlib.contextmanager
    def fail_receipt_commit(**kwargs):
        connections.append(True)
        with gateway_hub.connect() as conn:
            yield conn
            if len(connections) == 2:  # first connection reads the published copy
                raise OSError("synthetic commit failure")
    monkeypatch.setattr(db, "connect", fail_receipt_commit)
    error, body = call(gw, session_id="commit:s1", label="test", ack=batch)
    assert not error and body == {"available": False, "reason": gc.UNAVAILABLE}
    monkeypatch.setattr(db, "connect", lambda **kwargs: gateway_hub.connect())
    error, resent = call(gw, session_id="commit:s1", label="test")
    assert not error and resent["routing_changes"]["batch"] == batch
    error, acked = call(gw, session_id="commit:s1", label="test", ack=batch)
    assert not error and acked["delivery"]["acked_through"] == issued["version"]


def test_damaged_projection_cannot_consume_ack(gw, gateway_hub):
    seed(gw)
    assert gc.publish(force=True)["ok"]
    gw["autopublish"] = False
    error, issued = call(gw, session_id="damage:s1", label="test")
    assert not error
    conn = gateway_hub.connect()
    with conn.cursor() as cur:
        cur.execute("UPDATE routing_gateway_copies SET projection=jsonb_set(projection, '{accounts,0,history}', "
                    "'{\"session\":\"PRIVATESESSION\"}'::jsonb)")
    conn.commit()
    error, body = call(gw, session_id="damage:s1", label="test", ack=issued["routing_changes"]["batch"])
    assert error and "PRIVATESESSION" not in json.dumps(body)
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM routing_gateway_delivery")
        state = cur.fetchone()[0]
    assert state["flight"]["batch"] == issued["routing_changes"]["batch"]
    assert state.get("acked_through") is None



def test_health_uses_distinct_hub_receipts_and_published_revision(gw):
    from khipu import routing_health
    seed(gw)
    for session in ("first:s1", "second:s2"):
        error, body = call(gw, session_id=session, label="test")
        assert not error and body["available"]
        error, body = call(gw, session_id=session, label="test", ack=body["routing_changes"]["batch"])
        assert not error
    assert gc.publish(force=True)["ok"]  # copies receipt metadata back to the Mac
    result = routing_health.doctor_block(routing.read_view(), routing._now(), routing.settings())
    rows = [row for row in result["identities"] if row["harness"] == "gateway"]
    assert len(rows) == 2 and len({row["identity"] for row in rows}) == 2
    assert all(row["copy_epoch"] == body["epoch"] and row["copy_revision"] == body["version"]
               and row["copy_revision_at"] == body["folded_at"] for row in rows)
    assert result["gateway"]["status"] == "green"
    assert "first:s1" not in json.dumps(result) and "second:s2" not in json.dumps(result)
