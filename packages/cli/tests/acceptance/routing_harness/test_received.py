"""Approved section 4 matrix, measured only after sentinel calibration."""
import json
import re

import pytest

from tests.test_routing_acceptance import expect, future

from .received import ReceiptError, rules_received_in_order
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
    blocks = [item.text.replace(scenario.child_sentinel + "\n", "", 1) for item in child.inputs
              if item.line < child.first_step and ROUTE_OPENING in item.text]
    expect(bool(blocks) and all(len(block.encode("utf-16-le")) // 2 <= 4000 for block in blocks), True,
           "7a: child block exceeds the configured bound")
    deferred = [item.text for item in child.inputs if item.channel == "tool" and item.line < child.last_step]
    expect(OVERSIZED_RULES[0] in opening, True, "7a: first fitting rule was not whole in the initial block")
    expect(rules_received_in_order(deferred, OVERSIZED_RULES[1:]), True,
           "7a: deferred rules missing or truncated in child's tool results")


def test_codex_child_receives_subagent_start_routing(received):
    scenario = received("codex")
    child = scenario.children[0]
    if ROUTE_OPENING not in scenario.parent.text(channel="developer"):
        raise ReceiptError("parent did not receive synthetic routing; cannot test child receipt")
    assert scenario.child_sentinel in child.text(channel="developer", before_first=True)
    assert ROUTE_OPENING in child.text(channel="developer", before_first=True)
    # The PreToolUse calibrator is "<sentinel>-PRETOOL" and fires on the child's own tool calls too;
    # only the bare SessionStart token would mean the parent's start context reached the child.
    leaked = re.search(re.escape(scenario.sentinel) + r"(?![-\w])", child.text())
    assert not leaked, "parent SessionStart context leaked into child"


@future("8b", "7b: bounded child block and missing-hook routing-free brief advisory")
def test_7b_codex_missing_brief_advisory(received):
    scenario = received("codex")
    child = scenario.children[0]
    if ROUTE_OPENING not in scenario.parent.text(channel="developer"):
        raise ReceiptError("parent did not receive synthetic routing; cannot test child receipt")
    unhooked = scenario.children[1]
    assert scenario.child_sentinel + "-NOHOOK" in unhooked.text(channel="developer", before_first=True)
    if ROUTE_OPENING in unhooked.text(channel="developer", before_first=True):
        raise ReceiptError("7b: absent routing hook unexpectedly delivered routing")
    opening = child.text(channel="developer", before_first=True)
    expect(ROUTE_OPENING in opening, True, "7b: SubagentStart routing missing")
    blocks = [item.text.replace(scenario.child_sentinel + "\n", "", 1) for item in child.inputs
              if item.line < child.first_step and ROUTE_OPENING in item.text]
    expect(bool(blocks) and all(len(block.encode("utf-16-le")) // 2 <= 4000 for block in blocks), True,
           "7b: child routing block exceeds the configured bound")
    expect(ADVISORY in scenario.parent.text(), True,
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
    first_args, last_args = route_args[0] or {}, route_args[-1] or {}
    session_id = first_args.get("session_id")
    expect(isinstance(session_id, str) and session_id.startswith("t3:"), True,
           "7c: initial route call lacks a T3 session identity")
    expect(session_id, last_args.get("session_id"), "7c: provider switch changed session identity")
    expect(bool(batch), True, "7c: initial route result lacks a routing batch")
    expect(last_args.get("ack"), batch, "7c: switched provider did not acknowledge the original batch")
    expect(bool(batch and last.get("delivery", {}).get("acked_through")), True,
           "7c: switched T3 provider cannot acknowledge the original attachment's batch")
    expect(not last.get("routing_changes", {}).get("items"), True,
           "7c: acknowledged routing was re-owed after provider switch")
    expect(any(all(word in text.lower() for word in ("codex", "hooks", "missing")) for text in doctors), True,
           "7c: received doctor result does not name missing child-provider hooks")
