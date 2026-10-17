"""Owner direction, slice 5 of docs/plans/2026-10-16-owner-direction.md: the
feature is on by default. The real prompt-hook shim records the owner's turn
(no text kept, no token pushed); the agent's ``khipu_owner_act`` call with
only an action and its arguments acts on it; the routing record, the owner-actions log and the next routing block show it; an
undo by the app (and a second direction) puts it back. The setting is never
written: this runs on the shipped default. Hermetic: temp HOME, capture home
and data dir; the hook runs with no warm service."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from khipu import cli, owner_direction, owner_turns, routing
from khipu import mcp_server as ms
from khipu.mcp_server import handle_message

SHIM = Path(__file__).resolve().parents[1] / "bin" / "khipu-prompt-recall"
SESSION = "e2e-session-1"
ACCOUNTS = [{"label": "Claude", "harness": "claude"}, {"label": "Codex", "harness": "codex"}]


@pytest.fixture
def world(tmp_path, monkeypatch, khipu_app):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS}))
    home = tmp_path / "home"
    home.mkdir()
    for name in ("KHIPU_SESSION_ID", "CODEX_THREAD_ID", "GROK_HOOK_NAME", "GROK_HOOK_EVENT", "KHIPU_HARNESS",
                 "KHIPU_ISOLATED_DIR", "KHIPU_OWNER_TURN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    monkeypatch.setattr(ms, "_GATEWAY_ACTIVE", False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    khipu_app.as_shell("/opt/harness/claude")
    assert not (tmp_path / "capture" / "owner-turns" / "settings.json").exists()
    return tmp_path


def _hook(prompt: str) -> str:
    """The real shim, as the harness runs it; the turn id it recorded."""
    import os

    env = {**os.environ, "KHIPU_RECALL_DAEMON": "0", "KHIPU_PYTHON": sys.executable}
    payload = {"session_id": SESSION, "prompt": prompt, "cwd": os.environ["HOME"], "hook_event_name": "UserPromptSubmit"}
    done = subprocess.run(["/bin/sh", str(SHIM)], input=json.dumps(payload), capture_output=True, text=True,
                          env=env, timeout=60)
    # Nothing about the turn reaches the model: no token, no "Owner turn" line.
    assert "Owner turn" not in done.stdout and "KHIPU_OWNER_TURN" not in done.stdout, (done.stdout, done.stderr)
    # Run from here, the hook's issuer is this test's Python, which the check
    # refuses (a shim run by hand). Stand the harness in for it: the turn's
    # issuer becomes the one conftest puts above this process.
    from tests.conftest import FAKE_HOOK_ISSUER

    path = owner_turns.session_file(SESSION)
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert rows and "refused" in rows[-1]["issuer"], rows
    assert "text" not in rows[-1] and rows[-1]["typed"] is True, rows[-1]
    assert prompt not in path.read_text(encoding="utf-8")
    rows[-1]["issuer"] = dict(FAKE_HOOK_ISSUER)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return rows[-1]["turn"]


def _act(action: str, args: dict) -> tuple[bool, dict]:
    out = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "khipu_owner_act", "arguments": {"action": action, "args": args}}})
    result = out["result"]
    return result["isError"], json.loads(result["content"][0]["text"])


def _state(label: str) -> str:
    return next(a for a in routing.read_view()["accounts"] if a["label"] == label)["state"]


def _five_pm() -> str:
    """The owner's "until 5pm" as --until: the next 5 pm on this Mac."""
    now = datetime.now().astimezone()
    then = now.replace(hour=17, minute=0, second=0, microsecond=0)
    return (then if then > now else then + timedelta(days=1)).isoformat()


def _log() -> list[dict]:
    return owner_direction.read_log()


def test_it_is_on_by_default(world):
    assert owner_turns.settings()["enabled"] is True


def test_a_direction_goes_from_the_hook_to_the_record_and_back(world, khipu_app, monkeypatch):
    routing.report(account="Claude", state="open",
                   author={"kind": "conversation", "harness": "codex", "project": "p", "session": "s-other"})
    marker = routing.prompt_push(None, budget=5.0)
    seen = {"version": marker["version"], "epoch": marker["epoch"], "full_at": routing._iso(routing._now())}
    turn = _hook("pause Codex until 5pm")

    is_error, out = _act("route.pause", {"account": "Codex", "until": _five_pm()})
    assert not is_error and out["ok"] is True, out
    assert _state("Codex") == "paused"
    (line,) = [x for x in _log() if x.get("kind") == "action"]
    assert (line["action"], line["auth"], line["binding"], line["turn"], line["session"]) == \
        ("route.pause", "direction", "hook", turn, SESSION)
    assert "by your direction" in routing.prompt_push(seen, budget=5.0)["text"]

    # A conversation with no recent owner turn is refused, and changes nothing.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "e2e-session-never-typed")
    is_error, bad = _act("route.lift", {"account": "Codex"})
    assert is_error and bad["code"] == "no_recorded_turn" and _state("Codex") == "paused"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)

    # Undo from the app.
    khipu_app.as_app()
    rc = cli.main(["owner-direction", "undo", "--from-app", "--", line["id"]])
    assert rc == 0 and _state("Codex") != "paused"
    assert any(x.get("kind") == "undo" and x.get("undoes") == line["id"] for x in _log())


def test_a_second_direction_lifts_it(world):
    _hook("pause Codex until 5pm")
    assert not _act("route.pause", {"account": "Codex", "until": _five_pm()})[0]
    _hook("lift Codex now")
    is_error, out = _act("route.lift", {"account": "Codex"})
    assert not is_error and out["ok"] is True, out
    assert _state("Codex") != "paused"
    assert [x["action"] for x in _log() if x.get("kind") == "action"] == ["route.pause", "route.lift"]
