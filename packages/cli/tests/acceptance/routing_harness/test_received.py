"""Approved section 4 matrix, measured only after sentinel calibration."""
import json

import pytest

from tests.test_routing_acceptance import expect, future

from .received import ReceiptError
from .run import ADVISORY, OVERSIZED_RULES, ROUTE_OPENING


pytestmark = pytest.mark.routing_received


@pytest.mark.parametrize("harness", ["claude", "codex", "t3", "grok", "aegis"])
def test_rig_reads_known_received_sentinels(received, harness):
    # Session.get calibrates every parent and child before returning it.
    scenario = received(harness)
    assert scenario.parent.inputs
    for child in scenario.children:
        assert child.inputs


@future("8a", "7a: bounded oversized child block before its first step, remainder on its own tool results")
def test_7a_claude_child_oversized_block_and_deferred_pages(received):
    scenario = received("claude")
    child, = scenario.children
    opening = child.text(before_first=True)
    expect(ROUTE_OPENING in opening, True, "7a: oversized routing missing before child's first model step")
    blocks = [item.text for item in child.inputs if item.line < child.first_step and ROUTE_OPENING in item.text]
    assert blocks and all(len(block.encode("utf-16-le")) // 2 <= 4000 for block in blocks)
    deferred = child.text(channel="tool")
    assert OVERSIZED_RULES[0] in opening, "7a: first fitting rule was not whole in the initial block"
    expect(all(rule in deferred for rule in OVERSIZED_RULES[1:]), True,
           "7a: deferred rules missing or truncated in child's tool results")


def test_codex_child_receives_no_implicit_hook_routing(received):
    scenario = received("codex")
    child, = scenario.children
    if ROUTE_OPENING not in scenario.parent.text(channel="developer"):
        raise ReceiptError("parent did not receive synthetic routing; cannot test child non-delivery")
    assert ROUTE_OPENING not in child.text(), "matrix disproved: Codex native child received hook routing"
    assert scenario.sentinel not in child.text(), "matrix disproved: parent hook context reached Codex child"


@future("8b", "7b: routing-free spawn_agent brief produces advisory on the parent's received result")
def test_7b_codex_missing_brief_advisory(received):
    scenario = received("codex")
    child, = scenario.children
    if ROUTE_OPENING not in scenario.parent.text(channel="developer"):
        raise ReceiptError("parent did not receive synthetic routing; cannot test child non-delivery")
    assert ROUTE_OPENING not in child.text(), "matrix disproved: Codex native child received hook routing"
    expect(ADVISORY in scenario.parent.text(channel="tool"), True,
           "7b: parent's received spawn_agent result has no missing-routing advisory")


@future("8c", "7c: T3 provider switch retains one ledger and doctor names an unhooked child provider")
def test_7c_t3_provider_switch_and_child_doctor(received):
    scenario = received("t3")
    # A T3 driver must perform the switch within ONE scenario and preserve
    # both providers' received MCP bodies in the native input transcript.
    route_bodies, route_args, doctors = [], [], []
    for item in (item for attachment in scenario.attachments for item in attachment.inputs):
        if item.channel != "tool":
            continue
        if item.tool.endswith("khipu_route"):
            route_bodies.append(json.loads(item.text))
            route_args.append(json.loads(item.arguments) if isinstance(item.arguments, str) else item.arguments)
        arguments = item.arguments if isinstance(item.arguments, str) else json.dumps(item.arguments)
        if item.tool in {"Bash", "exec", "exec_command", "shell", "shell_command"} and "khipu doctor" in arguments:
            doctors.append(item.text)
    if len(route_bodies) < 2 or not scenario.children:
        raise ReceiptError("T3 scenario lacks both switched-provider MCP inputs or a calibrated child")
    first, last = route_bodies[0], route_bodies[-1]
    batch = first.get("routing_changes", {}).get("batch")
    assert route_args[0]["session_id"].startswith("t3:")
    assert route_args[0]["session_id"] == route_args[-1]["session_id"]
    assert route_args[-1].get("ack") == batch
    expect(bool(batch and last.get("delivery", {}).get("acked_through")), True,
           "7c: switched T3 provider cannot acknowledge the original attachment's batch")
    assert not last.get("routing_changes", {}).get("items"), "acknowledged routing was re-owed after provider switch"
    assert len(scenario.attachments) == 2
    expect(any(all(word in text.lower() for word in ("codex", "hooks", "missing")) for text in doctors), True,
           "7c: received doctor result does not name missing child-provider hooks")
