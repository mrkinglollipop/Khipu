"""Reusable routing slice 4 changed-log benchmark; it has no timing assertions.

Run from ``packages/cli`` with the target checkout first on ``PYTHONPATH``.
It writes only a temporary routing record, warms a view, then runs the exact
five raw-append / one-second advance / ``prompt_push`` workload used by the
slice review.  The old evidence shape is deliberate so the same runner works
against the pre-fix baseline.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


T0 = datetime(2026, 10, 20, 14, tzinfo=timezone.utc)


def build(count: int, routing) -> list[dict]:
    accounts = ("codex|", "grok|", "claude|")
    rows = []
    for index in range(count):
        at = routing._iso(T0 - timedelta(seconds=(count - index) * 30))
        if index % 50 == 0:
            rows.append({"type": "rule_add", "id": f"r{index}", "n": index // 50 + 1,
                         "text": f"rule {index} about Codex", "source": "added by you", "date": "2026-10-01",
                         "seeded": False, "written_at": at, "written_by": {"kind": "owner"}, "via": "app"})
        else:
            rows.append({"type": "evidence", "id": f"e{index}", "account": accounts[index % len(accounts)],
                         "state": "open" if index % 7 else "limited", "source": "report", "written_at": at,
                         "written_by": {"kind": "conversation", "harness": "codex", "project": "p",
                                        "session": f"s{index % 40}"}, "via": "mcp",
                         **({"until": routing._iso(T0 - timedelta(seconds=(count - index) * 30 - 600))}
                            if index % 7 == 0 else {})})
    return rows


def median_ms(call, repeats: int = 5) -> float:
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        call()
        samples.append((time.perf_counter() - start) * 1000)
    return sorted(samples)[len(samples) // 2]


def run(count: int = 20_000, repeats: int = 5) -> dict:
    root = Path(tempfile.mkdtemp(prefix="khipu-routing-s4-cost-"))
    os.environ["HOME"] = str(root)
    os.environ["KHIPU_DATA_DIR"] = str(root / "data")
    os.environ["KHIPU_CAPTURE_HOME"] = str(root / "capture")
    os.environ["KHIPU_KEYCHAIN"] = "0"
    from khipu import recall_prompt, routing

    now = T0
    with mock.patch.object(routing, "_now", side_effect=lambda: now), \
            mock.patch.object(routing, "kick_plan_refresh", lambda: False):
        data = root / "data"
        data.mkdir(parents=True)
        accounts = [{"label": "Grok", "harness": "grok"}, {"label": "Codex", "harness": "codex"},
                    {"label": "Claude", "harness": "claude"}]
        (data / "config.json").write_text(json.dumps({"usage_accounts": accounts, "routing_reset_check": False,
                                                         "routing_prompt_chars": 400, "routing_prompt_budget_ms": 1000}))
        reset = T0 + timedelta(days=6)
        state = {"ok": True, "observed_at": routing._iso(now), "probed_at": routing._iso(now),
                 "last_good_at": routing._iso(now), "windows": [{"name": "weekly", "window_mins": 10080,
                 "used_pct": 4, "resets_at": routing._iso(reset)}]}
        usage = data / "state" / "usage.json"
        usage.parent.mkdir(exist_ok=True)
        usage.write_text(json.dumps({"version": 2, "rows": {"codex|": state}}))
        path = routing.events_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in build(count, routing)))
        start = time.perf_counter()
        view = routing.read_view(now=now)
        warm_ms = (time.perf_counter() - start) * 1000
        cached_ms = median_ms(lambda: routing.read_view(now=now), repeats)
        push_ms = median_ms(lambda: routing.prompt_push(recall_prompt.routing_marker("s1"), budget=1.0, now=now), repeats)
        changed = []
        changed_results = []
        for index in range(repeats):
            with path.open("a") as stream:
                stream.write(json.dumps({"type": "evidence", "id": f"changed-{index}", "account": "grok|",
                                         "state": "open", "source": "report", "written_at": routing._iso(now),
                                         "written_by": {"kind": "conversation", "harness": "codex", "project": "p",
                                                        "session": "z"}, "via": "mcp"}) + "\n")
            now += timedelta(seconds=1)
            start = time.perf_counter()
            pushed = routing.prompt_push(recall_prompt.routing_marker("s1"), budget=1.0, now=now)
            changed.append((time.perf_counter() - start) * 1000)
            changed_results.append({"kind": pushed["kind"], "version": pushed.get("version")})
    return {"lines": count, "warm_ms": round(warm_ms, 1), "cached_ms": round(cached_ms, 1),
            "prompt_push_ms": round(push_ms, 1), "changed_push_median_ms": round(sorted(changed)[len(changed) // 2], 1),
            "changed_push_samples_ms": [round(sample, 1) for sample in changed],
            "changed_results": changed_results, "version": view["version"]}


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True))
