"""Doctor delivery health stays unknown only on evidence about live work."""
import json
from datetime import timedelta

from khipu import routing, routing_delivery as delivery, routing_gateway_copy, routing_health as health
from tests import test_routing_acceptance as acceptance
from tests.test_routing_s16_health import planned

T0 = acceptance.T0
replay = acceptance.replay

LEDGER_GAPS = ("last delivery point", "current attachment capability", "parent attachment evidence")


def _write_ledger(session, data):
    path = delivery._path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _component(at):
    return {"entries": {}, "delivery_points": 1, "last_delivery_point": routing._iso(at)}


def test_ledgers_outside_the_live_window_add_no_attachment_gaps(replay):
    old = T0 - timedelta(days=3)
    _write_ledger("no-attachment", {"main": _component(old)})
    _write_ledger("agents-only", {"agent:a": _component(old)})
    _write_ledger("attached-gone", {"mcp": _component(old),
                                    "attachment": {"component": "main", "at": routing._iso(old)}})
    result = health.doctor_block({}, T0, routing.settings())
    assert not set(LEDGER_GAPS) & set(result["missing"])


def test_live_ledgers_keep_their_attachment_gaps(replay):
    recent = T0 - timedelta(minutes=5)
    _write_ledger("no-attachment", {"main": _component(recent)})
    _write_ledger("agents-only", {"agent:a": _component(recent)})
    _write_ledger("attached-gone", {"mcp": _component(recent),
                                    "attachment": {"component": "main", "at": routing._iso(recent)}})
    result = health.doctor_block({}, T0, routing.settings())
    assert set(LEDGER_GAPS) <= set(result["missing"])


def _gateway_copy(refreshed_at, rows=()):
    routing_gateway_copy._save(dict(epoch="e1", revision=1, folded_at=routing._iso(T0),
                                    published_at=routing._iso(T0), gateway_rows=list(rows),
                                    gateway_rows_truncated=False, health_refreshed_at=routing._iso(refreshed_at)))


VIEW = {"epoch": "e1", "version": 1, "folded_at": routing._iso(T0)}


def test_a_quiet_gateway_with_a_fresh_empty_client_list_is_green(replay):
    replay.configure(routing_cloud_copy=True)
    _gateway_copy(T0 - timedelta(seconds=30))
    result = health.doctor_block(VIEW, T0, routing.settings())
    assert result["gateway"]["status"] == "green"
    assert result["gateway"]["quiet"] is True
    assert "gateway delivered copy revision" not in result["missing"]


def test_a_quiet_gateway_without_a_fresh_client_list_stays_unknown(replay):
    replay.configure(routing_cloud_copy=True)
    _gateway_copy(T0 - timedelta(hours=13))
    result = health.doctor_block(VIEW, T0, routing.settings())
    assert result["gateway"]["status"] == "unknown"
    assert "gateway delivered copy revision" in result["missing"]


def _strip_receipts(session):
    """An entry acknowledged before shown receipts existed carries none."""
    with delivery._transaction(session) as data:
        for entry in data["main"]["entries"].values():
            entry.pop("shown_offset", None)


def test_a_pre_receipt_ack_is_delivered_again_to_earn_its_receipt(replay):
    out = planned()
    answer = delivery.offer(out, session="s1")
    assert delivery.acknowledge("s1", answer["routing_changes"], now=T0)
    _strip_receipts("s1")
    assert health.component_health(delivery.snapshot("s1"), T0)["missing"] == ["shown receipt"]
    again = delivery.offer(out, session="s1")
    assert again["routing_changes"]["items"], "a receiptless ack must be owed again"
    assert delivery.acknowledge("s1", again["routing_changes"], now=T0)
    state = delivery.snapshot("s1")
    assert all(isinstance(e.get("shown_offset"), int) for e in state["entries"].values())
    assert health.component_health(state, T0)["status"] == "green"


def test_a_redelivered_ack_without_a_render_stays_visible(replay):
    out = planned()
    answer = delivery.offer(out, session="s1")
    assert delivery.acknowledge("s1", answer["routing_changes"], now=T0)
    _strip_receipts("s1")
    again = delivery.offer(out, session="s1")
    with delivery._transaction("s1") as data:
        data["main"]["flight"]["shown"] = {}
    assert delivery.acknowledge("s1", again["routing_changes"], now=T0)
    assert "ack-unshown" in health.component_health(delivery.snapshot("s1"), T0)["reasons"]
