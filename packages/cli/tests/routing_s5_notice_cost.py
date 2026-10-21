"""Session-scoped notice cost on the review's 20,000-event workload.

Run with python3.11 -m tests.routing_s5_notice_cost. Only temporary state is
written. Timing is reported, while delivery kind and text are asserted.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from tests.routing_s4_cost_workload import T0, build, median_ms


def run(with_notice: bool) -> dict:
    from khipu import recall_prompt, routing

    with tempfile.TemporaryDirectory(prefix="khipu-s5-cost-") as folder:
        root = Path(folder)
        env = dict(HOME=folder, KHIPU_DATA_DIR=str(root / "data"), KHIPU_CAPTURE_HOME=str(root / "capture"),
                   KHIPU_KEYCHAIN="0", KHIPU_HARNESS="claude_code", KHIPU_SESSION_ID="bystander")
        with mock.patch.dict(os.environ, env), mock.patch.object(routing, "_now", lambda: T0), \
                mock.patch.object(routing, "kick_plan_refresh", lambda: False):
            data = root / "data"
            data.mkdir()
            (data / "config.json").write_text(json.dumps({"usage_accounts": [
                {"label": "Grok", "harness": "grok"}, {"label": "Codex", "harness": "codex"},
                {"label": "Claude", "harness": "claude"}], "routing_reset_check": False,
                "routing_prompt_chars": 400, "routing_prompt_budget_ms": 1000}))
            path = routing.events_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(json.dumps(row) + "\n" for row in build(20_000, routing)))
            if with_notice:
                for session in ("gone-a", "gone-b"):
                    os.environ["KHIPU_SESSION_ID"] = session
                    shown = routing.conversation_view()
                    routing.request_rule(f"P {session}", n=1, owner_request="x",
                                         base=shown["rules"][0]["owner_revision"])
                replaced = [p for p in routing.read_view()["proposals"].values() if p["status"] == "replaced"]
                assert len(replaced) == 1 and replaced[0]["notify"]["session"] == "gone-a"
                assert routing.summary(session_id="gone-a")["notices"] == [replaced[0]["id"]]
            os.environ["KHIPU_SESSION_ID"] = "bystander"
            out = routing.prompt_push(None, budget=1, now=T0, session_id="bystander")
            assert not out["notices"]
            assert recall_prompt.mark_routing_full(
                "bystander", version=out["version"], epoch=out["epoch"], at=routing._iso(T0),
                owed=out.get("owed_entities"), shown=out.get("shown_entities"), notices=out["notices"])
            marker = recall_prompt.routing_marker("bystander")
            kinds = []
            def push():
                pushed = routing.prompt_push(marker, budget=1, now=T0, session_id="bystander")
                kinds.append((pushed["kind"], len(pushed.get("text") or "")))
            elapsed = median_ms(push, 11)
            assert kinds == [("none", 0)] * 11, kinds
            return {"events": 20_000, "notice_for_departed_author": with_notice,
                    "median_ms": round(elapsed, 3), "prompts": len(kinds), "text_chars": 0, "kind": "none"}


if __name__ == "__main__":
    print(json.dumps([run(False), run(True)], indent=2))
