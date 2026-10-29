"""Fix round 3 on the every-window pace branch (Codex re-check, three P2s).

A cap on a window with no reading yet is an unknown verdict, never an
exception; report rows keep the account identity routing gives them on the
way to ``khipu_usage``; "Read no accounts" sends no request, and GLM read both
as a configured account and as an http-quota definition is one row read with
one key, in the Keys card's order (env, Keychain, file).
Hermetic: the routing replay fixture and faked transports, no network.
"""
from __future__ import annotations

import pytest

from khipu import keychain, mcp_server, routing, routing_pace, usage
from tests.test_routing_acceptance import replay  # noqa: F401 - the routing replay fixture
from tests.test_usage_kimi import _define_kimi, kimi  # noqa: F401 - the Kimi transport fixture

# ---- a cap with no reading -----------------------------------------------------------


def test_a_monthly_cap_with_no_reading_is_unknown_not_a_crash(replay, kimi):  # noqa: F811
    _define_kimi(replay)
    routing.set_cap("kimi:main", window="monthly", kind="pace_target", pct=100, slack=0, via="app")
    entry = next(e for e in replay.view()["accounts"] if e["key"] == "kimi:main")
    (month,) = [p for p in entry["pace"] if p["window"] == "monthly"]
    assert month["plan"]["verdict"] == "unknown" and month["pace_target"]["verdict"] == "unknown"
    assert isinstance(routing.summary(now=replay.now)["text"], str)


def test_compute_pace_of_an_unsized_window_is_unknown():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    pace = routing_pace.compute_pace([], [{"kind": "share", "pct": 50, "revision": "r"}], now, "monthly")
    assert pace["plan"]["verdict"] == "unknown" and pace["window"] == "monthly"
    assert routing_pace.next_change([], now, "monthly") is None


# ---- identity through khipu_usage ------------------------------------------------------


def test_public_rows_keep_the_routing_account_id(replay, kimi, monkeypatch):  # noqa: F811
    _define_kimi(replay)
    monkeypatch.setattr(usage, "configured_accounts", lambda config=None: [])
    monkeypatch.setattr(usage, "_now", lambda: replay.now)
    rows = [r for r in usage.public_report(usage.report(fresh=True))["accounts"] if r["harness"] == "kimi"]
    assert [r["id"] for r in rows] == [routing.account_id("kimi:main")] == ["kimi:main"]
    monkeypatch.setattr(mcp_server, "_via_https_gateway", lambda: False)
    monkeypatch.setattr(usage, "_start_refresh", lambda *_: True)
    out = mcp_server._tool_usage({})
    assert [(r["id"], r["harness"]) for r in out["accounts"]] == [("kimi:main", "kimi")]


def test_a_deepseek_row_is_exported_under_its_own_id():
    row = dict(usage._blank_row({"harness": "deepseek", "label": "DeepSeek"}), key="deepseek:main", ok=True)
    out = usage.public_row(row)
    assert out["id"] == routing.account_id("deepseek:main") and out["harness"] == "deepseek"


# ---- what the report reads ---------------------------------------------------------------


def test_read_no_accounts_sends_no_request(replay, kimi, monkeypatch):  # noqa: F811
    from khipu import config

    _define_kimi(replay)
    monkeypatch.setattr(config, "load_config", lambda *a, **k: {"usage_accounts_none": True})
    assert usage.report(fresh=True)["accounts"] == []
    assert usage.refresh_due(max_age=0)["refreshed"] == 0
    assert not usage.kick_refresh(max_age=0, spawn=lambda argv: pytest.fail("spawned a read"))
    assert kimi.seen == []


def _define_glm_quota(replay):  # noqa: F811
    replay.write(replay.event("provider_set", id="glm", label="GLM", family="Zhipu", login="none",
                              probe="http-quota", host="quota.invalid", accounts=["glm:main"]))


@pytest.fixture
def glm_wire(monkeypatch):
    """Every GLM request either path makes, by the key it carried."""
    sent = []
    monkeypatch.setattr(usage, "_glm_request", lambda key, timeout: (sent.append(key), (401, b""))[1])
    monkeypatch.setattr(usage, "_http_quota_request", lambda key, host, timeout: (sent.append(key), (401, b""))[1])
    monkeypatch.setattr(keychain, "get_password", lambda name, **k: "KEYCHAIN-FAKE" if name == "glm_api_key" else None)
    monkeypatch.setattr(keychain, "get_glm_key", lambda: "KEYCHAIN-FAKE")
    monkeypatch.setenv("GLM_API_KEY", "ENV-FAKE")
    return sent


def test_glm_configured_and_defined_reads_once_with_the_env_key(replay, glm_wire, monkeypatch):  # noqa: F811
    _define_glm_quota(replay)
    monkeypatch.setattr(usage, "configured_accounts",
                        lambda config=None: [{"label": "GLM", "harness": "glm", "home": None}])
    monkeypatch.setattr(usage, "_now", lambda: replay.now)
    rows = [r for r in usage.report(fresh=True)["accounts"] if r["harness"] == "glm"]
    assert len(rows) == 1
    assert glm_wire == ["ENV-FAKE"]


def test_an_http_quota_glm_alone_takes_the_env_key_first(replay, glm_wire, monkeypatch):  # noqa: F811
    _define_glm_quota(replay)
    monkeypatch.setattr(usage, "configured_accounts", lambda config=None: [])
    monkeypatch.setattr(usage, "_now", lambda: replay.now)
    rows = [r for r in usage.report(fresh=True)["accounts"] if r["harness"] == "glm"]
    assert len(rows) == 1 and glm_wire == ["ENV-FAKE"]
