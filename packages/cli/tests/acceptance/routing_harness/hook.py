"""Disposable hook wiring: actual routing entry points, synthetic state only."""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path


def main():
    root = Path(os.environ["HOME"]).resolve()
    data = Path(os.environ["KHIPU_DATA_DIR"]).resolve()
    if data != root / "data" or not (data / "config.json").is_file():
        raise RuntimeError("rig isolation missing")

    def forbidden(*args, **kwargs):
        (root / "forbidden-network").touch()
        raise RuntimeError("routing rig attempted network access")

    socket.socket.connect = forbidden
    socket.create_connection = forbidden
    import psycopg

    psycopg.connect = forbidden
    from khipu import routing, routing_delegation

    # A source refresh is outside received-context acceptance. The real fold,
    # summary, hook selection and output format remain under test.
    routing.kick_plan_refresh = lambda: False
    payload = json.load(sys.stdin)
    event = payload.get("hook_event_name") or payload.get("hookEventName")
    if event == "SessionStart":
        from khipu import recall_rule

        text = recall_rule._RoutingAtStart().begin().text() or ""
        # This independent calibrator proves the hook's attachment made it
        # into native received context even when routing itself is broken.
        text = os.environ["ROUTING_RIG_SENTINEL"] + "\n" + text
        out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    else:
        out, _, _ = routing_delegation.hook_main(json.dumps(payload))
    print(json.dumps(out))


if __name__ == "__main__":
    main()
