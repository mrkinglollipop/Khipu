"""A slow candidate discovery keeps the last good scan snapshot; the compact route summary shows closed advice."""
from __future__ import annotations

import json
import time

from khipu import routing, routing_scan_report as scan
from tests.test_routing_scan_deadlines import setup_scan


def test_a_discovery_timeout_keeps_the_last_good_scan_snapshot(tmp_path, monkeypatch):
    setup_scan(tmp_path, monkeypatch)
    good = scan.refresh()
    assert good["hits"], good
    monkeypatch.setattr(scan, "_discovery_result", lambda: time.sleep(1.0) or {"entries": [], "skipped": []})
    scan.refresh()
    after = scan.report()
    assert after["hits"], f"one slow discovery replaced the good snapshot with an empty one: {after}"


def test_the_compact_summary_shows_an_account_whose_advice_is_closed():
    answer = {"accounts": [{
        "label": "acct-a", "state": "open", "until": None,
        "pace": [{"window": "weekly", "used_pct": 91, "cycle_resets_at": None,
                  "plan": {"verdict": "on_pace"}}],
        "advice": {"verdict": "closed", "reason": "share cap_reached"},
        "cap_statuses": [{"kind": "share", "verdict": "cap_reached"}],
    }], "priority": None, "rules": [], "holds": []}
    summary = routing.project_route_view(answer, "summary")
    text = json.dumps(summary["accounts"])
    assert "closed" in text or "cap_reached" in text, f"summary row says open/on_pace only: {text}"
