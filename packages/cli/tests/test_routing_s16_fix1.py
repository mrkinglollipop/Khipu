"""S16 review fixes: delivery evidence must describe what was rendered."""
from datetime import timedelta

from khipu import mcp_server, recall_prompt, routing, routing_delivery
from khipu import routing_health
from tests import test_routing_acceptance as acceptance


T0 = acceptance.T0
AUTHOR = acceptance.AUTHOR
replay = acceptance.replay


def _outcomes(replay, n=100):
    path = recall_prompt._log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = routing._iso(replay.now) + " [khipu-prompt-recall] "
    path.write_text("".join(prefix + "session=s1 reason=no_hits ms=1 hits=[] legs=[] degraded=None routing=none\n"
                            for _ in range(n)))


def _gateway_on(replay):
    replay.patch.setenv("KHIPU_HARNESS", "gateway")
    replay.patch.setenv(mcp_server.GATEWAY_ACTIVE_ENV, "1")
    replay.patch.setattr(mcp_server, "_GATEWAY_ACTIVE", True)


def _gateway_off(replay):
    replay.patch.delenv("KHIPU_HARNESS")
    replay.patch.delenv(mcp_server.GATEWAY_ACTIVE_ENV)
    replay.patch.setattr(mcp_server, "_GATEWAY_ACTIVE", False)


def test_hook_receipt_only_accepts_the_final_rendered_entities(replay):
    routing.report(account="Codex", state="open", author=AUTHOR)
    out, _context, info = replay.offer()
    _outcomes(replay)
    # The original case-15 ordering: a false final callback before its ack.
    assert recall_prompt.mark_routing_seen(
        "s1", version=out["version"], epoch=out["epoch"],
        owed=["not-shown"], shown=["not-shown"],
    )
    health = routing.doctor_block(now=replay.now)["routing_delivery"]
    assert "ack-unshown" in health["reasons"]
    assert routing_delivery.acknowledge("s1", info["pending"]["routing_changes"])
    # The exact list from the final offer is the legitimate recovery.
    assert recall_prompt.mark_routing_seen(
        "s1", version=out["version"], epoch=out["epoch"],
        owed=out["owed_entities"], shown=out["shown_entities"],
    )
    assert routing.doctor_block(now=replay.now)["routing_delivery"]["status"] == "green"
    # A later false claim cannot hide behind an already valid receipt.
    assert recall_prompt.mark_routing_seen(
        "s1", version=out["version"], epoch=out["epoch"],
        owed=["not-shown"], shown=["not-shown"],
    )
    assert "ack-unshown" in routing.doctor_block(now=replay.now)["routing_delivery"]["reasons"]


def test_gateway_health_tracks_revision_not_caller_idleness(replay):
    routing.report(account="Codex", state="open", author=AUTHOR)
    out, _context, info = replay.offer()
    replay.ack(info)
    _outcomes(replay)
    replay.configure(routing_cloud_copy=True)
    _gateway_on(replay)
    body = replay.pull(session_id="client:health")
    if body.get("routing_changes"):
        replay.pull(session_id="client:health", ack=body["routing_changes"]["batch"])
    _gateway_off(replay)
    replay.advance(601)
    routing.read_view(replay.now)  # a local read changes checked_at only
    out, _context, info = replay.offer()
    replay.ack(info)
    _outcomes(replay)
    assert routing.doctor_block(now=replay.now)["routing_delivery"]["gateway"]["status"] == "green"

    routing.report(account="Codex", state="limited", until="1h", author=AUTHOR)
    routing.read_view(replay.now)
    changed = routing.doctor_block(now=replay.now)["routing_delivery"]
    assert changed["gateway"]["status"] == "red"
    assert "gateway-stale" in changed["reasons"]

    _gateway_on(replay)
    body = replay.pull(session_id="client:health")
    if body.get("routing_changes"):
        replay.pull(session_id="client:health", ack=body["routing_changes"]["batch"])
    _gateway_off(replay)
    assert routing.doctor_block(now=replay.now)["routing_delivery"]["gateway"]["status"] == "green"

    replay.advance(13 * 3600)
    dropped = routing.doctor_block(now=replay.now)["routing_delivery"]
    assert dropped["gateway"]["status"] == "unknown"
    assert "gateway-stale" not in dropped["reasons"]


def test_unmatched_limit_age_starts_with_the_current_continuous_run(replay):
    routing.report(account="Codex", state="open", author=AUTHOR)
    out, _context, info = replay.offer()
    replay.ack(info)
    _outcomes(replay)

    def evidence(number, state="limited", until=None):
        return replay.event("evidence", number, runner="synthetic-runner", lane="unmapped", state=state,
                            source="runner", until=routing._iso(until or (T0 + timedelta(hours=5))))

    replay.write(evidence(1))
    replay.advance(50 * 60)
    replay.write(evidence(2))
    replay.advance(50 * 60)
    out, _context, info = replay.offer()
    replay.ack(info)
    _outcomes(replay)
    health = routing.doctor_block(now=replay.now)["routing_delivery"]
    assert "unmatched-limit" in health["reasons"]

    replay.write(evidence(3, "open"))
    assert "unmatched-limit" not in routing.doctor_block(now=replay.now)["routing_delivery"]["reasons"]
    replay.advance(1)
    replay.write(evidence(4))
    assert "unmatched-limit" not in routing.doctor_block(now=replay.now)["routing_delivery"]["reasons"]

    # A later report may shorten expiry.  Its expiration creates a new run;
    # the earlier five-hour limit must not bridge across it.
    replay.write(evidence(5, until=replay.now + timedelta(hours=5)))
    replay.advance(10 * 60)
    replay.write(evidence(6, until=replay.now + timedelta(minutes=10)))
    replay.advance(40 * 60)
    replay.write(evidence(7, until=replay.now + timedelta(hours=5)))
    lane = routing.read_view(replay.now)["unmatched"][0]
    assert lane["limited_since"] == routing._iso(replay.now)


def test_actual_clipped_identifying_lines_are_receipts(replay):
    text = "Synthetic account line with enough words to be clipped in a final delivery."
    item = dict(entity="account:synthetic", revision="r1", text=text, label="Synthetic account",
                whole=False, visible=True)
    plan = dict(epoch="e1", version=1, text=routing._clip(text, 20), delivery_plan=dict(
        items=[item], current_entities=[item["entity"]], at=routing._iso(replay.now), limit=400,
        page_limit=400, lease=60, heading="Routing", text=routing._clip(text, 20)))

    main = routing_delivery.offer(plan, session="clip-main")
    assert routing_delivery.acknowledge("clip-main", main["routing_changes"])
    assert routing_health.component_health(routing_delivery.snapshot("clip-main"), replay.now)["status"] == "green"

    start = routing_delivery.child_offer(plan, session="clip-child", component="agent:c", now=replay.now,
                                         cfg={"routing_child_chars": 8, "routing_line_chars": 12})
    assert not start["shown_entities"]
    page = routing_delivery.child_page(plan, session="clip-child", component="agent:c", now=replay.now,
                                       cfg={"routing_child_chars": 8, "routing_line_chars": 12})
    assert page and routing_delivery.acknowledge("clip-child", page["routing_changes"])
    assert routing_health.component_health(routing_delivery.snapshot("clip-child", "agent:c"), replay.now)["status"] == "green"


def test_completed_child_marker_keeps_pruned_render_proof(replay):
    routing.add_rule("Synthetic completed child rule.", via="app")
    child = routing.start_push(moment="delegation", child_component="agent:c", child_session="s1")
    assert child and child["shown_entities"]
    assert routing_delivery.acknowledge("s1", child["routing_changes"])
    assert routing_delivery.snapshot("s1", "agent:c")["done_shown"]
    assert routing_delivery.mark_shown("s1", child["shown_entities"], component="agent:c",
                                       version=child["version"], epoch=child["epoch"])
    assert recall_prompt.mark_routing_seen("s1", version=child["version"], epoch=child["epoch"],
                                           shown=child["shown_entities"], component="agent:c")
    assert routing_health.component_health(routing_delivery.snapshot("s1", "agent:c"), replay.now)["status"] == "green"


def test_gateway_receipts_use_decoded_final_projection_text(replay):
    routing.report(account="Codex", state="open", author=AUTHOR)
    routing.add_rule('Synthetic rule says "quoted words".\nThen another line.', via="app")
    replay.configure(routing_cloud_copy=True, routing_cloud_copy_rules=True)
    _gateway_on(replay)
    _outcomes(replay)
    body = replay.pull(session_id="client:quoted")
    for _ in range(10):
        changes = body.get("routing_changes")
        if not changes:
            break
        body = replay.pull(session_id="client:quoted", ack=changes["batch"])
    else:
        raise AssertionError("gateway did not finish its rendered rule")
    result = routing.doctor_block(now=replay.now)["routing_delivery"]
    assert result["status"] == "green", result
