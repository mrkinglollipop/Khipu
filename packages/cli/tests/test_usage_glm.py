"""khipu.usage / discover / routing for the GLM Coding Plan (z.ai).

Hermetic: the HTTP call is faked (``urllib.request.urlopen`` is patched), no
test reaches z.ai, reads a real key or touches a real login folder. The fake
key is a sentinel the tests then search for in every output.
"""
from __future__ import annotations

import io
import json
import socket
import urllib.error
from datetime import timedelta

import pytest

from khipu import config, discover, routing, usage
from tests.test_discover import NOW, mac  # noqa: F401 - the temp-HOME fixture
from tests.test_routing_priority import T0, _cache, _row, _summary, _week, home  # noqa: F401

KEY = "SENTINEL-glm-key-0123456789abcdef.do-not-leak"

# The shape z.ai answers with (numbers fixed, values invented): unit 3 is the
# five-hour window, unit 6 the week; a request-count limit rides along.
REPLY = {"code": 200, "msg": "ok", "success": True, "data": {"level": "pro", "limits": [
    {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "usage": 800000000, "currentValue": 120000000,
     "remaining": 680000000, "percentage": 15, "nextResetTime": 1_800_018_000_000},
    {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "usage": 4000000000, "currentValue": 1000000000,
     "remaining": 3000000000, "percentage": 25, "nextResetTime": 1_800_500_000_000},
    {"type": "TIME_LIMIT", "unit": 5, "number": 1, "usage": 1000, "currentValue": 3, "percentage": 1,
     "nextResetTime": 1_801_000_000_000}]}}


class _Response:
    def __init__(self, body: bytes, status: int = 200):
        self.status, self._body = status, body

    def read(self, _n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def z(monkeypatch):
    """A stored key and a fake z.ai; ``z.seen`` holds every request made."""
    seen = []

    class Z:
        reply = _Response(json.dumps(REPLY).encode())
        error = None

    def urlopen(request, timeout=None):
        seen.append({"url": request.full_url, "headers": dict(request.header_items()), "timeout": timeout})
        if Z.error is not None:
            raise Z.error
        return Z.reply

    monkeypatch.setattr(usage, "_glm_key", lambda: KEY)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    Z.seen = seen
    return Z


def _probe(**kw) -> dict:
    return usage.probe_account({"label": "GLM", "harness": "glm", "home": None}, **kw)


def test_both_windows_parse_from_the_real_shape(z):
    row = _probe()
    assert row["ok"] is True and row["plan"] == "pro" and row["source"] == "live"
    assert row["windows"] == [
        {"name": "five_hour", "used_pct": 15.0, "resets_at": usage._iso(1_800_018_000), "window_mins": 300},
        {"name": "weekly", "used_pct": 25.0, "resets_at": usage._iso(1_800_500_000), "window_mins": 10080}]
    request = z.seen[0]
    assert request["url"] == "https://api.z.ai/api/monitor/usage/quota/limit"
    assert request["headers"]["Authorization"] == KEY  # raw key, no Bearer
    assert request["timeout"] == usage.GLM_TIMEOUT_S == 15.0


def test_percentage_falls_back_to_used_over_limit(z):
    reply = json.loads(json.dumps(REPLY))
    del reply["data"]["limits"][0]["percentage"]
    z.reply = _Response(json.dumps(reply).encode())
    assert _probe()["windows"][0]["used_pct"] == 15.0  # 120M of 800M


def test_a_key_with_no_plan_says_so(z):
    z.reply = _Response(json.dumps({"code": 1113, "msg": "no plan", "success": False}).encode())
    row = _probe()
    assert row["ok"] is False and row["error"] == usage.E_NO_PLAN
    assert row["note"] == "z.ai reports no GLM Coding Plan on this key"


def test_a_rejected_key_is_not_signed_in(z):
    z.reply = _Response(b'{"code": 1001, "msg": "bad header"}', 401)
    row = _probe()
    assert row["error"] == usage.E_BAD_KEY and "replace it in Settings" in row["note"]
    z.error = urllib.error.HTTPError(usage.GLM_QUOTA_URL, 401, "no", {}, io.BytesIO(b""))
    assert _probe()["error"] == usage.E_BAD_KEY


def test_no_key_is_a_clear_row_and_makes_no_request(z, monkeypatch):
    monkeypatch.setattr(usage, "_glm_key", lambda: None)
    row = _probe()
    assert row["error"] == usage.E_NO_KEY and not z.seen
    assert row["note"] == "Khipu has no GLM key; add it in Settings under Accounts and routing"


@pytest.mark.parametrize("error", [socket.timeout("timed out"), TimeoutError(),
                                   urllib.error.URLError(socket.timeout("timed out"))])
def test_a_timeout_is_a_timeout(z, error):
    z.error = error
    assert _probe()["error"] == usage.E_TIMEOUT


def test_other_failures_get_fixed_codes(z):
    z.error = urllib.error.URLError("dns")
    assert _probe()["error"] == usage.E_NO_ANSWER
    z.error = None
    z.reply = _Response(b"<html>", 200)
    assert _probe()["error"] == usage.E_BAD
    z.reply = _Response(json.dumps({"code": 200, "data": {"level": "pro", "limits": []}}).encode())
    assert _probe()["error"] == usage.E_NO_LIMITS
    z.reply = _Response(b'{"code": 500, "msg": "boom"}', 500)
    assert _probe()["error"] == usage.E_CLI


def test_the_key_never_appears_in_any_output(z, caplog, capsys):
    outputs = []
    # Errors that echo the key (a server body, an exception message) must not travel.
    z.reply = _Response(json.dumps({"code": 1001, "msg": f"bad {KEY}"}).encode(), 401)
    outputs.append(_probe())
    z.error = urllib.error.URLError(f"cannot reach with {KEY}")
    outputs.append(_probe())
    z.error = RuntimeError(f"surprise {KEY}")
    outputs.append(_probe())
    z.error = None
    z.reply = _Response(json.dumps(REPLY).encode())
    outputs.append(_probe())
    rows = usage.probe_all([{"label": "GLM", "harness": "glm", "home": None}])
    outputs += rows + [usage.public_row(r) for r in rows]
    outputs.append(usage.public_report({"accounts": rows}))
    outputs.append(usage.table({"accounts": rows}))
    for out in outputs:
        assert KEY not in json.dumps(out, default=str)
    assert KEY not in caplog.text and KEY not in "".join(capsys.readouterr())
    assert all(KEY not in json.dumps(r["headers"]) or r["headers"]["Authorization"] == KEY for r in z.seen)


def test_the_account_has_no_folder_and_is_one_login():
    assert usage.LABELS["glm"] == "GLM" and "glm" in usage.HARNESSES
    problem = usage._entry_problem("glm", "~/.claude-glm")
    assert problem and problem[0] == "glm_folder"
    assert usage.login_identity({"harness": "glm"}) == "glm|"
    assert usage._key({"harness": "glm", "home": None}) == "glm|"


def test_glm_is_read_by_default_only_with_a_key(mac, monkeypatch):  # noqa: F811
    monkeypatch.setattr(usage, "_glm_key", lambda: None)
    assert "glm" not in [a["harness"] for a in usage.configured_accounts()]
    monkeypatch.setattr(usage, "_glm_key", lambda: KEY)
    assert "glm" in [a["harness"] for a in usage.configured_accounts()]


def test_the_probe_all_budget_covers_glm(z, monkeypatch):
    calls = []
    monkeypatch.setattr(usage, "probe_account", lambda account, *, timeout: calls.append(timeout) or
                        dict(usage._blank_row(account), ok=True))
    usage.probe_all([{"label": "GLM", "harness": "glm", "home": None}])
    assert calls == [usage.TIMEOUT_S]  # the probe widens itself to GLM_TIMEOUT_S


# ---- discovery ------------------------------------------------------------------------------


def test_a_z_ai_claude_home_is_glm_not_claude(mac, monkeypatch):  # noqa: F811
    monkeypatch.setattr(usage, "_glm_key", lambda: None)
    mac.install("claude")
    mac.folder(".claude-glm")
    mac.folder(".claude-client")
    found, _tracked, _declined = discover._gather(NOW)
    paths = {rec["path"] for rec in found.values() if rec["cli"] == "claude"}
    assert not any("glm" in (p or "") for p in paths)
    assert any("client" in (p or "") for p in paths)
    glm = [rec for rec in found.values() if rec["cli"] == "glm"]
    assert len(glm) == 1 and glm[0]["id"] == "glm:default" and glm[0]["path"] is None
    assert glm[0]["name"] == "GLM"


def test_zai_home_is_known_by_its_name_and_nothing_inside_is_read(mac):  # noqa: F811
    assert discover.zai_home(mac.folder(".claude-glm"))
    assert not discover.zai_home(mac.folder(".claude-second"))
    assert not discover.zai_home(None)
    # A folder named .claude-glm that holds a settings.json: still decided by the name alone.
    (mac.home / ".claude-glm" / "settings.json").write_text("{not json")
    assert discover.zai_home(mac.home / ".claude-glm")


def test_a_z_ai_home_is_not_a_valid_claude_account(mac):  # noqa: F811
    problem = usage._entry_problem("claude", str(mac.home / ".claude-glm"))
    assert problem and problem[0] == "zai_home"
    assert usage._entry_problem("claude", str(mac.folder(".claude-ok"))) is None


def test_a_glm_candidate_is_checked_by_the_quota_read(mac, z):  # noqa: F811
    with config.mutate_config() as data:
        data["usage_accounts"] = [{"label": "Codex", "harness": "codex"}]
    found, _t, _d = discover._gather(NOW)
    rec = next(r for r in found.values() if r["cli"] == "glm")
    run = usage._Run()
    state, reason, plan = discover._check_one(rec, run=run, end=__import__("time").monotonic() + 20)
    assert (state, reason, plan) == (discover.SIGNED_IN, None, "pro")


# ---- routing --------------------------------------------------------------------------------


def test_the_routing_summary_lists_glm_by_pace(home):  # noqa: F811
    config = json.loads((home["data"] / "config.json").read_text())
    config["usage_accounts"].append({"label": "GLM", "harness": "glm"})
    (home["data"] / "config.json").write_text(json.dumps(config))
    rows = json.loads((home["data"] / "state" / "usage.json").read_text())["rows"]
    rows["glm|"] = _row("GLM", "glm", None, _week(5, T0 + timedelta(days=1)), plan="pro")
    _cache(home, rows)
    text = _summary(home)
    line = next(x for x in text.splitlines() if x.startswith("GLM"))
    assert "95% of its week left" in line
    lines = text.splitlines()
    head = lines.index("Accounts by how soon their allowance runs out unused (Khipu's arithmetic, not your "
                       "priority):")
    assert "GLM" in [x.split(":")[0] for x in lines[head + 1:]]
    assert routing.resolve_account("glm", routing.accounts()) == "glm|"
