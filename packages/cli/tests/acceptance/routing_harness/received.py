"""Read model input, never hook diagnostics or assistant claims.

Readers deliberately recognize a small set of native transcript envelopes.
Unknown envelopes cannot silently turn a negative assertion into a pass.
Raw transcripts stay in the session's private evidence directory.
"""
from __future__ import annotations

import json
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
        if channel == "tool":
            # A result recorded after the final model step might never have
            # been consumed. Require a subsequent native model step too.
            observed = "\n".join(item.text for item in self.inputs
                                 if item.channel == "tool" and item.line < self.last_step
                                 and (not before_first or item.line < self.first_step))
        if not sentinel or sentinel not in observed:
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
