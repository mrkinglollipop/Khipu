"""No agent-facing output carries a login-folder path (settings-clarity plan,
slice 9A "fix now"). ``khipu_usage``, ``khipu_route``, ``khipu_status``'s
plan warnings, ``khipu route --summary`` and the "Routing at" pushes go to
conversations, whose text reaches other model providers; the owner's
``khipu usage --json`` (the app's own command) keeps the full rows.

Hermetic: a temp data dir names the accounts, HOME is a temp folder, the
probes are replaced, nothing starts a CLI or reaches the hub.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

import pytest

from khipu import mcp_server, routing, usage

FOLDER = "claude-zqprivate"  # the login folder's own name: must never show
HOME_ENTRY = f"~/.{FOLDER}"
INVALID_ENTRY = f"{FOLDER}-relative"  # no "~/": an invalid entry whose reason names it


@pytest.fixture
def owner(tmp_path, monkeypatch):
    user = tmp_path / "Users" / "someone"
    (user / f".{FOLDER}").mkdir(parents=True)
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": [
        {"label": "Claude zqprivate", "harness": "claude", "home": HOME_ENTRY},
        {"label": "Claude broken", "harness": "claude", "home": INVALID_ENTRY},
    ]}))
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.delenv(mcp_server.GATEWAY_ACTIVE_ENV, raising=False)
    monkeypatch.setattr(mcp_server, "_GATEWAY_ACTIVE", False)
    monkeypatch.setattr(usage, "_command", lambda harness: None)
    return {"user": user, "expanded": str(user / f".{FOLDER}")}


def _strings(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)
    elif value is not None:
        yield str(value)


def _leaks(value, owner) -> list[str]:
    bad = (FOLDER, "~/", owner["expanded"], str(owner["user"]))
    return [s for s in _strings(value) if any(b in s for b in bad)]


def _good(account: dict, used: float = 42) -> dict:
    now = usage._iso(time.time())
    return {"harness": account["harness"], "account": account["label"], "home": account.get("home"),
            "plan": "max", "ok": True, "error": None, "note": None, "credits": {"balance": 3},
            "windows": [{"name": "weekly", "used_pct": used,
                         "resets_at": usage._iso(time.time() + 2 * 86400), "window_mins": 10080}],
            "version": "2.1.0", "source": "live", "observed_at": None, "probed_at": now, "ms": 9}


def _work() -> dict:
    return next(a for a in usage.configured_accounts() if not a.get("invalid"))


def _raw(kind: str, monkeypatch) -> dict:
    """``usage.report`` as it is, for one kind of row about the account whose
    login folder is ``HOME_ENTRY`` (or, for ``invalid``, ``INVALID_ENTRY``)."""
    work = _work()
    if kind == "cached":
        usage._save_cache({usage._key(work): dict(_good(work), last_attempt_at=usage._iso(time.time()),
                                                  last_good_at=usage._iso(time.time()), last_error=None)})
        return usage.report(accounts=[work], background=True)
    if kind == "fresh":
        monkeypatch.setattr(usage, "probe_all", lambda accounts, *, timeout: [_good(a) for a in accounts])
        return usage.report(accounts=[work], fresh=True)
    if kind == "failed":
        monkeypatch.setattr(usage, "probe_all",
                            lambda accounts, *, timeout: [usage._failed_row(a, usage.E_SIGNIN) for a in accounts])
        return usage.report(accounts=[work], fresh=True)
    if kind == "pending":
        monkeypatch.setattr(usage, "_start_refresh", lambda accounts, timeout: True)
        return usage.report(accounts=[work], background=True)
    if kind == "invalid":
        return usage.report(accounts=[a for a in usage.configured_accounts() if a.get("invalid")])
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["cached", "fresh", "failed", "pending", "invalid"])
def test_khipu_usage_never_carries_a_login_folder(owner, monkeypatch, kind):
    raw = _raw(kind, monkeypatch)
    assert _leaks(raw, owner), "the owner's report carries the folder, so this test can see a leak"
    monkeypatch.setattr(usage, "report", lambda **kw: raw)
    out = mcp_server.TOOL_FUNCS["khipu_usage"]({})
    assert _leaks(out, owner) == []
    row = out["accounts"][0]
    assert "home" not in row
    assert row["account"] == ("Claude broken" if kind == "invalid" else "Claude zqprivate")
    assert row["harness"] == "claude"
    if kind == "invalid":
        assert row["id"].startswith("acct-")
    else:
        assert re.fullmatch(r"claude:a-[a-z2-7]{8}", row["id"])
        assert row["id"] == routing.account_id(usage._key(_work()))
        assert "zqprivate" not in row["id"]
        assert FOLDER not in row["id"]
    assert row["ok"] is (kind in ("cached", "fresh"))
    assert out["summary_text"] and out["generated_at"] == raw["generated_at"]
    if kind in ("cached", "fresh"):
        assert row["windows"][0]["used_pct"] == 42 and row["plan"] == "max"
        assert row["credits"] == {"balance": 3}
    if kind == "failed":
        assert row["error"] == usage.E_SIGNIN and "sign in again" in row["note"]
    if kind == "invalid":
        assert row["error"] == usage.E_INVALID and row["note"]


def test_the_id_is_the_one_khipu_route_gives(owner):
    work = _work()
    row = usage.public_row(_good(work))
    assert row["id"] == routing.account_id(usage.account_key(work))


def test_the_owners_cli_json_keeps_the_full_rows(owner, monkeypatch, capsys):
    from khipu import cli

    raw = _raw("fresh", monkeypatch)
    monkeypatch.setattr(usage, "report", lambda **kw: raw)
    assert cli.main(["usage", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == json.loads(json.dumps(raw))
    assert raw["accounts"][0]["home"] == HOME_ENTRY


def test_routing_surfaces_never_carry_a_login_folder(owner, monkeypatch):
    # khipu_route, khipu route --summary, the session-start and per-prompt
    # pushes and khipu_status's plan warnings, with the usage cache holding a
    # row for the account (one window at 95%, so a warning is written).
    work = _work()
    stamp = usage._iso(time.time())
    usage._save_cache({usage._key(work): dict(_good(work, used=95), last_attempt_at=stamp, last_good_at=stamp,
                                              last_error=None)})
    now = datetime.now(timezone.utc).replace(microsecond=0)
    monkeypatch.setattr(routing, "_now", lambda: now)
    routing.conversation_write({"action": "report", "state": "limited", "account": "Claude zqprivate", "until": "1h"},
                               author={"harness": "claude-code"})
    outputs = {
        "khipu_route": routing.conversation_view(),
        "khipu_route one": routing.conversation_view(account="Claude zqprivate"),
        "khipu_route since": routing.conversation_view(since_version="0"),
        "summary": routing.summary(),
        "brief summary": routing.summary(brief=True),
        "start push": routing.light_status(start=True),
        "prompt push": routing.prompt_push(None, budget=5, now=now + timedelta(seconds=1)),
    }
    payload: dict = {}
    mcp_server._attach_plan_warnings(payload)
    outputs["khipu_status warnings"] = payload
    assert any("95%" in w for w in payload.get("warnings") or [])
    for name, out in outputs.items():
        assert _leaks(out, owner) == [], name
