"""Read model input, never hook diagnostics or assistant claims.

Readers deliberately recognize a small set of native transcript envelopes.
Unknown envelopes cannot silently turn a negative assertion into a pass.
Raw transcripts stay in the session's private evidence directory.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


class ReceiptError(AssertionError):
    """The rig could not establish what the model received (not an xfail)."""


@dataclass(frozen=True)
class Input:
    line: int
    channel: str
    text: str
    tool: str = ""
    arguments: object = None


def text_blocks(value):
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ReceiptError("invalid received content")
    parts = []
    for block in value:
        if not isinstance(block, dict):
            raise ReceiptError("invalid received block")
        if block.get("type") in {"text", "input_text", "output_text"}:
            text = block.get("text")
            if not isinstance(text, str):
                raise ReceiptError("invalid received text")
            parts.append(text)
    return "\n".join(parts)


class Transcript:
    def __init__(self, inputs, first_step, last_step, *, format):
        self.inputs = inputs
        self.first_step = first_step
        self.last_step = last_step
        self.format = format
        if not inputs or first_step is None:
            raise ReceiptError("transcript lacks received input or a model step")

    def text(self, *, channel=None, before_first=False, tool=None):
        return "\n".join(item.text for item in self.inputs
                         if (channel is None or item.channel == channel)
                         and (not before_first or item.line < self.first_step)
                         and (tool is None or item.tool == tool))

    def calibrate(self, sentinel, *, channel, before_first=True):
        observed = self.text(channel=channel, before_first=before_first)
        if channel == "tool" or not before_first:
            # A result recorded after the final model step might never have
            # been consumed. Require a subsequent native model step too.
            observed = "\n".join(item.text for item in self.inputs
                                 if item.channel == channel and item.line < self.last_step
                                 and (not before_first or item.line < self.first_step))
        if not sentinel or sentinel not in observed.splitlines():
            raise ReceiptError("known sentinel absent from received context")
        return self


def read(path: Path, format: str) -> Transcript:
    if format not in {"claude", "codex", "messages"}:
        raise ReceiptError("unsupported received-context format")
    inputs, first_step, last_step, calls = [], None, None, {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        raise ReceiptError("received transcript unavailable") from None
    for number, line in enumerate(lines):
        try:
            row = json.loads(line)
        except ValueError:
            raise ReceiptError("malformed received transcript") from None
        if not isinstance(row, dict):
            raise ReceiptError("invalid received transcript row")
        if format == "codex":
            # event_msg and exec --json item.completed are UI/output streams,
            # not the response_item input envelopes in the persisted rollout.
            if row.get("type") != "response_item":
                continue
            msg = row.get("payload")
            if not isinstance(msg, dict):
                raise ReceiptError("invalid Codex response item")
            kind, role = msg.get("type"), msg.get("role")
            if kind in {"function_call", "custom_tool_call"}:
                calls[msg.get("call_id")] = (msg.get("name", ""), msg.get("arguments", msg.get("input")))
                if first_step is None:
                    first_step = number
                last_step = number
            elif kind in {"function_call_output", "custom_tool_call_output"}:
                name, arguments = calls.get(msg.get("call_id"), ("", None))
                inputs.append(Input(number, "tool", text_blocks(msg.get("output")), name, arguments))
            elif kind == "message" and role in {"developer", "system", "user"}:
                inputs.append(Input(number, role, text_blocks(msg.get("content"))))
            elif kind in {"reasoning", "message"} and (kind == "reasoning" or role == "assistant"):
                if first_step is None:
                    first_step = number
                last_step = number
        else:
            # Claude JSONL (also Grok's Messages-format received transcript).
            # An ACP update / hook_response / debug log is never input proof.
            if format == "claude" and row.get("type") == "attachment":
                attachment = row.get("attachment")
                if isinstance(attachment, dict) and attachment.get("type") == "hook_additional_context":
                    content = attachment.get("content")
                    if isinstance(content, list) and all(isinstance(part, str) for part in content):
                        content = "\n".join(content)
                    inputs.append(Input(number, "hook", text_blocks(content)))
                continue
            msg = row.get("message") if format == "claude" else row
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content")
            if role == "assistant":
                if first_step is None:
                    first_step = number
                last_step = number
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            calls[block.get("id")] = (block.get("name", ""), block.get("input"))
                continue
            if role not in {"user", "system", "developer"}:
                continue
            if isinstance(content, str):
                inputs.append(Input(number, role, content))
                continue
            if not isinstance(content, list):
                raise ReceiptError("invalid received message")
            for block in content:
                if not isinstance(block, dict):
                    raise ReceiptError("invalid received block")
                if block.get("type") == "tool_result":
                    name, arguments = calls.get(block.get("tool_use_id"), ("", None))
                    inputs.append(Input(number, "tool", text_blocks(block.get("content")), name, arguments))
                elif block.get("type") in {"text", "input_text"}:
                    inputs.append(Input(number, role, text_blocks([block])))
    return Transcript(inputs, first_step, last_step, format=format)


def rules_received_in_order(pages, rules, *, page_budget=2000):
    """Reassemble synthetic rule lines across pages, excluding page headings.

    The fixture rules have no newlines. Keep every line containing their
    alphabet, including damaged rule lines, so truncation and duplication
    cannot disappear when page headings are removed.
    """
    # Each routing result must be one bounded page with a sequential header.
    # Unrelated tool results are permitted between deliveries.
    delivered = []
    entity, number, total = None, 0, 0
    completed = set()
    for page in pages:
        if "~" not in page and "SYNTHETIC-ROUTING-" not in page:
            continue
        lines = page.splitlines()
        header = re.fullmatch(r"Rule (\d+) page (\d+)/(\d+)", lines[0]) if lines else None
        if header is None or len(page.encode("utf-16-le")) // 2 > page_budget:
            return False
        current, n, m = map(int, header.groups())
        if n < 1 or m < n:
            return False
        if current != entity:
            if (entity is not None and number != total) or current in completed or n != 1:
                return False
            if entity is not None:
                if current <= entity:
                    return False
                completed.add(entity)
            entity, number, total = current, 0, m
        if n != number + 1 or m != total:
            return False
        number = n
        delivered.append(page)
    if not delivered or number != total:
        return False
    if sum(len(rule.encode("utf-16-le")) // 2 for rule in rules) > page_budget and len(delivered) < 2:
        return False
    material = "".join(line for page in delivered for line in page.splitlines()
                       if "~" in line or "SYNTHETIC-ROUTING-" in line)
    if material.count("~") != sum(rule.count("~") for rule in rules):
        return False
    position = 0
    for rule in rules:
        if material.count(rule) != 1:
            return False
        start = material.find(rule, position)
        if start < 0:
            return False
        position = start + len(rule)
    return True
