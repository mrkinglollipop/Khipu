#!/usr/bin/env python3
"""Extract pace_fixture_v1 rows from real routing blocks in a transcript.

Deterministic and local: reads one Claude Code transcript JSONL, takes only
user-message text (including a string content) and ``attachment`` /
``hook_additional_context`` rows (tool arguments and tool results never
supply a reading, even when they embed a decoy block), and prints the
readings as ``pace_fixture_v1`` JSON on stdout. Times are seconds relative
to the first routing block's heading time, so blocks minutes apart keep a
real time axis. A malformed transcript is rejected with a fixed error on
stderr, exit 2 and nothing on stdout; input is never echoed.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone

ERROR = "fixture_rejected: malformed transcript"

HEADING = re.compile(r"## Routing at (\d{2}):(\d{2})Z")
READING = re.compile(
    r"read from .+? (?:(\d+) h )?(\d+) min ago; "
    r"(\d+(?:\.\d+)?)% of its (week|five-hour) left, resets (.+?) \([^)]+\)")
# The witness age is rounded to the minute and the block heading is precise
# to the minute, so a reading's time is known within about ninety seconds.
T_ERR = 90
WEEKDAYS = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
          "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
WINDOWS = {"week": "weekly", "five-hour": "five_hour"}


class Malformed(Exception):
    pass


def _reset_at(clock: str, origin: datetime) -> datetime | None:
    """The instant a rendered reset clock names, next after ``origin``."""
    match = re.fullmatch(r"(\d{2}):(\d{2})Z", clock)
    if match:
        if int(match[1]) > 23 or int(match[2]) > 59:
            raise Malformed
        at = origin.replace(hour=int(match[1]), minute=int(match[2]), second=0, microsecond=0)
        return at if at > origin else at + timedelta(days=1)
    match = re.fullmatch(r"([A-Z][a-z]{2}) (\d{2}):(\d{2})Z", clock)
    if match and match[1] in WEEKDAYS:
        if int(match[2]) > 23 or int(match[3]) > 59:
            raise Malformed
        at = origin.replace(hour=int(match[2]), minute=int(match[3]), second=0, microsecond=0)
        at += timedelta(days=(WEEKDAYS[match[1]] - origin.weekday()) % 7)
        return at if at > origin else at + timedelta(days=7)
    match = re.fullmatch(r"([A-Z][a-z]{2}) (\d{2}) (\d{2}):(\d{2})Z", clock)
    if match and match[1] in MONTHS:
        if int(match[3]) > 23 or int(match[4]) > 59:
            raise Malformed
        try:
            at = origin.replace(month=MONTHS[match[1]], day=int(match[2]), hour=int(match[3]),
                                minute=int(match[4]), second=0, microsecond=0)
            return at if at > origin else at.replace(year=at.year + 1)
        except ValueError:
            return None
    return None


def _stamp(row: dict) -> datetime:
    stamp = row.get("timestamp")
    if not isinstance(stamp, str) or not stamp:
        raise Malformed
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        raise Malformed from None


def _readings(text: str, stamp: datetime, epoch: datetime | None) -> tuple[list[dict], datetime | None]:
    heading = HEADING.search(text)
    if not heading:
        return [], epoch
    if int(heading[1]) > 23 or int(heading[2]) > 59:
        raise Malformed
    try:
        origin = stamp.replace(hour=int(heading[1]), minute=int(heading[2]), second=0, microsecond=0)
    except ValueError:
        raise Malformed from None
    if epoch is None:
        epoch = origin
    out = []
    for line in text.splitlines():
        match = READING.search(line)
        if not match:
            continue
        reset = _reset_at(match[5].strip(), origin)
        if reset is None:
            continue
        age = int(match[1] or 0) * 3600 + int(match[2]) * 60
        label = line.split(":", 1)[0].strip()
        out.append({"label": label, "window": WINDOWS[match[4]],
                    "t": int((origin - timedelta(seconds=age) - epoch).total_seconds()),
                    "t_err": T_ERR, "used": 100.0 - float(match[3]),
                    "reset": int((reset - epoch).total_seconds())})
    return out, epoch


def _texts(row) -> list[str]:
    """The reading-bearing texts of one transcript row, or Malformed."""
    if not isinstance(row, dict):
        raise Malformed
    kind = row.get("type")
    if kind == "attachment":
        attachment = row.get("attachment")
        if not isinstance(attachment, dict):
            raise Malformed
        if attachment.get("type") != "hook_additional_context":
            return []
        content = attachment.get("content")
        if isinstance(content, str):
            return [content]
        if isinstance(content, list) and all(isinstance(part, str) for part in content):
            return list(content)
        raise Malformed
    if kind != "user":
        return []
    message = row.get("message")
    if not isinstance(message, dict):
        raise Malformed
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        raise Malformed
    texts = []
    for block in content:
        if not isinstance(block, dict):
            raise Malformed
        if block.get("type") == "text":
            if not isinstance(block.get("text"), str):
                raise Malformed
            texts.append(block["text"])
    return texts


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(ERROR, file=sys.stderr)
        return 2
    try:
        lines = open(argv[1], encoding="utf-8").read().splitlines()
        rows = [json.loads(line) for line in lines if line.strip()]
        found: list[dict] = []
        epoch = None
        for row in rows:
            texts = _texts(row)
            if not texts:
                continue
            stamp = None
            for text in texts:
                if HEADING.search(text):
                    if stamp is None:
                        stamp = _stamp(row)
                    readings, epoch = _readings(text, stamp, epoch)
                    found.extend(readings)
    except (OSError, ValueError, Malformed):
        print(ERROR, file=sys.stderr)
        return 2
    accounts: dict[str, str] = {}
    windows: dict[str, str] = {}
    out, seen = [], set()
    for row in found:
        accounts.setdefault(row["label"], f"a{len(accounts) + 1}")
        windows.setdefault(row["window"], f"w{len(windows) + 1}")
        item = {"acct": accounts[row["label"]], "win": windows[row["window"]], "t": row["t"],
                "t_err": row["t_err"], "used": row["used"], "reset": row["reset"]}
        key = json.dumps(item, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(item)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
