"""Deterministic routing replay sanitizer. Input is explicit stdin, never a log search.

The output is a test interchange format, not an events.jsonl replacement.
Identifiers are relational aliases, clocks are relative seconds, and text is
replaced with equal-length synthetic characters. Unknown fields fail closed.
Run: python scripts/routing_fixture_sanitize.py < explicitly-selected.jsonl
"""
from __future__ import annotations

import json
import math
import re
import sys
from datetime import datetime


class FixtureError(ValueError):
    """Errors carry a closed code, never input values or field names."""


def closed(value, fields, required=()):
    if type(value) is not dict or set(value) - set(fields) or set(required) - set(value):
        raise FixtureError("fixture_schema")


def number(value, *, integer=False):
    if type(value) not in ((int,) if integer else (int, float)) or not math.isfinite(value):
        raise FixtureError("fixture_number")


def alias(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(prefix + r"[1-9][0-9]*", value):
        raise FixtureError("fixture_identifier")


def load_pace(rows):
    """pace_fixture_v1: no extension bag, strings, booleans or nonfinite numbers."""
    if type(rows) is not list:
        raise FixtureError("fixture_schema")
    for row in rows:
        fields = {"acct", "win", "t", "t_err", "used", "reset"}
        closed(row, fields, fields)
        alias(row["acct"], "a")
        alias(row["win"], "w")
        for key in ("t", "t_err", "reset"):
            number(row[key], integer=True)
        number(row["used"])
        if row["t_err"] < 0 or not 0 <= row["used"] <= 100:
            raise FixtureError("fixture_range")
    return rows


KINDS = {"evidence", "hold", "hold_end", "rule_add", "rule_edit", "priority_set"}
STATES = {"open", "limited", "down"}
HARNESS = {"codex", "claude_code", "grok_build", "shell", "unknown"}
EVENT_FIELDS = {"type", "id", "account", "state", "source", "written_at", "written_by", "via",
                "scope", "reason", "text", "until", "owner", "pending", "n", "target", "entries",
                "owner_request"}
TEXT_FIELDS = {"text", "owner_request", "role"}


def load_replay(value):
    closed(value, {"schema", "events"}, {"schema", "events"})
    if value["schema"] != "routing_replay_v1" or type(value["events"]) is not list:
        raise FixtureError("fixture_schema")
    for row in value["events"]:
        closed(row, EVENT_FIELDS, {"type", "id", "written_at", "written_by", "via"})
        if row["type"] not in KINDS or row["via"] not in {"mcp", "app", "cli"}:
            raise FixtureError("fixture_enum")
        alias(row["id"], "e")
        number(row["written_at"], integer=True)
        if "until" in row:
            number(row["until"], integer=True)
        if "account" in row:
            alias(row["account"], "a")
        if "target" in row:
            alias(row["target"], "e")
        for key in TEXT_FIELDS & row.keys():
            if not isinstance(row[key], str) or not re.fullmatch("x*", row[key]):
                raise FixtureError("fixture_text")
        for key in ("owner", "pending"):
            if key in row and type(row[key]) is not bool:
                raise FixtureError("fixture_schema")
        if "n" in row:
            number(row["n"], integer=True)
        if "state" in row and row["state"] not in STATES:
            raise FixtureError("fixture_enum")
        if "source" in row and row["source"] != "report":
            raise FixtureError("fixture_enum")
        if "reason" in row and row["reason"] != "keep":
            raise FixtureError("fixture_enum")
        by = row["written_by"]
        closed(by, {"kind", "harness", "project", "session"}, {"kind"})
        if by["kind"] not in {"owner", "conversation"} or by.get("harness", "unknown") not in HARNESS:
            raise FixtureError("fixture_enum")
        for key, prefix in (("project", "p"), ("session", "s")):
            if key in by:
                alias(by[key], prefix)
        if "scope" in row:
            scope = row["scope"]
            closed(scope, {"kind", "account"}, {"kind"})
            if scope["kind"] not in {"all", "account"}:
                raise FixtureError("fixture_enum")
            if scope["kind"] == "account":
                alias(scope.get("account"), "a")
            elif "account" in scope:
                raise FixtureError("fixture_schema")
        if "entries" in row:
            if type(row["entries"]) is not list:
                raise FixtureError("fixture_schema")
            for entry in row["entries"]:
                closed(entry, {"account", "label", "role"}, {"account"})
                alias(entry["account"], "a")
                if "label" in entry:
                    alias(entry["label"], "a")
                if "role" in entry and (not isinstance(entry["role"], str)
                                        or not re.fullmatch("x*", entry["role"])):
                    raise FixtureError("fixture_text")
    return value


def sanitize(events):
    """Preserve relationships, intervals, nesting and text lengths; copy no prose."""
    if type(events) is not list:
        raise FixtureError("fixture_schema")
    names = {prefix: {} for prefix in ("a", "e", "p", "s")}

    def named(value, prefix):
        if not isinstance(value, str):
            raise FixtureError("fixture_schema")
        mapping = names[prefix]
        return mapping.setdefault(value, f"{prefix}{len(mapping) + 1}")

    origin = None

    def time(value):
        nonlocal origin
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.utcoffset() is None:
                raise ValueError
            seconds = int(stamp.timestamp())
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise FixtureError("fixture_time") from None
        if origin is None:
            origin = seconds
        return seconds - origin

    def words(value):
        if not isinstance(value, str):
            raise FixtureError("fixture_schema")
        return "x" * len(value)

    out = []
    for raw in events:
        closed(raw, EVENT_FIELDS, {"type", "id", "written_at", "written_by", "via"})
        row = {"type": raw["type"], "id": named(raw["id"], "e"),
               "written_at": time(raw["written_at"]), "via": raw["via"]}
        by = raw["written_by"]
        closed(by, {"kind", "harness", "project", "session"}, {"kind"})
        row["written_by"] = {k: named(v, {"project": "p", "session": "s"}[k])
                             if k in {"project", "session"} else v for k, v in by.items()}
        for key, value in raw.items():
            if key in row:
                continue
            if key in TEXT_FIELDS:
                row[key] = words(value)
            elif key in {"account", "target"}:
                row[key] = named(value, "a" if key == "account" else "e")
            elif key == "until":
                row[key] = time(value)
            elif key == "scope":
                closed(value, {"kind", "account"}, {"kind"})
                row[key] = {k: named(v, "a") if k == "account" else v for k, v in value.items()}
            elif key == "entries":
                if type(value) is not list:
                    raise FixtureError("fixture_schema")
                row[key] = []
                for entry in value:
                    closed(entry, {"account", "label", "role"}, {"account"})
                    row[key].append({k: words(v) if k == "role" else named(entry["account"], "a")
                                     for k, v in entry.items()})
            else:
                row[key] = value
        out.append(row)
    return load_replay({"schema": "routing_replay_v1", "events": out})


def main():
    try:
        rows = [json.loads(line) for line in sys.stdin if line.strip()]
        result = sanitize(rows)
    except (ValueError, TypeError, KeyError, OverflowError):
        print("fixture_rejected", file=sys.stderr)
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
