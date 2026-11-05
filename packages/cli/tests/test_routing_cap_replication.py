"""An owner cap must arrive on the other Mac whole: a cap with no revision is dropped by the receiver's fold."""
from __future__ import annotations

from khipu import routing, routing_pace
from tests.test_routing_acceptance import AUTHOR  # noqa: F401

pytest_plugins = ("tests.test_routing_acceptance",)


def test_a_cap_keeps_its_revision_when_it_leaves_the_mac(replay):
    routing.set_cap("Codex", window="weekly", kind="share", pct=95, via="app")
    routing.set_cap("Codex", kind="spend", usd=35, days=7, via="app")
    local = [e for e in routing.read_events()[0] if e.get("type") == "cap_set"]
    assert len(local) == 2 and all(e["revision"].startswith("cap-") for e in local)
    bindings, _forwards, _aliases = routing.account_identity.state(routing._in_write_order(routing.read_events()[0]), routing.accounts())
    sent = routing.replication_payload(local, bindings=bindings)
    assert len(sent) == 2
    for event, payload in zip(local, sent):
        assert payload["revision"] == event["revision"]
        fields = {k: payload.get(k) for k in ("kind", "pct", "slack", "usd", "days", "n", "revision", "purpose") if k in payload}
        assert routing_pace.checked_cap(fields) is not None, payload
