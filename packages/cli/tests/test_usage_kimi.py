"""khipu.usage / routing for the Kimi Code plan (kimi.com membership).

Hermetic: the HTTP transport is faked (``OpenerDirector.open``), no test
reaches api.kimi.com, reads a real key or a real key file. The fake key is a
sentinel the tests then search for in every output.
"""
from __future__ import annotations

import io
import json
import socket
import urllib.error
from datetime import timedelta

import pytest

from khipu import cli, keychain, routing, routing_providers, usage
from tests.test_routing_acceptance import T0, replay  # noqa: F401 - the routing replay fixture

KEY = "SENTINEL-kimi-key-0123456789abcdef.do-not-leak"
ACCOUNT = {"label": "Kimi", "harness": "kimi", "home": None, "provider": "kimi", "quota_key": "kimi:main"}

# The shape GET /coding/v1/usages answers with (seen 2026-10-29; numbers and
# times invented): a five-hour window and the month, as ratios, plus the
# five-hour window again as a used/limit pair under ``limits``.
REPLY = {
    "limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                "detail": {"limit": "100", "used": "20", "resetTime": "2026-10-20T16:00:00.123456Z"}}],
    "usages": {"limit_5h": {"used_ratio": 0.2, "reset_time": "2026-10-20T16:00:00Z"},
               "limit_month_total": {"used_ratio": 0.35, "reset_time": "2026-11-28T00:00:00Z"},
               "limit_month_code": {"used_ratio": 0.3, "reset_time": "2026-11-28T00:00:00Z"}},
}
# 2026-10-28 to 2026-11-28: the calendar month that ends at the reset.
MONTH_MINS = 31 * 24 * 60


class _Response:
    def __init__(self, body: bytes, status: int = 200):
        self.status, self._body = status, io.BytesIO(body)

    def read(self, n=-1):
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def kimi(monkeypatch):
    """A stored key and a fake api.kimi.com; ``kimi.seen`` holds every request."""
    seen = []

    class K:
        reply = _Response(json.dumps(REPLY).encode())
        error = None
        key = KEY

    def opener_open(self, request, data=None, timeout=None):  # noqa: ARG001 - OpenerDirector.open stand-in
        seen.append({"url": request.full_url, "headers": dict(request.header_items()), "timeout": timeout})
        if K.error is not None:
            raise K.error
        return K.reply

    monkeypatch.setattr(usage, "_kimi_key", lambda: K.key)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", opener_open)
    K.seen = seen
    return K


def _probe(**kw) -> dict:
    return usage.probe_account(dict(ACCOUNT), **kw)


def _reply(kimi, body, status=200):
    kimi.reply = _Response(body if isinstance(body, bytes) else json.dumps(body).encode(), status)


# ---- the read ---------------------------------------------------------------------------


def test_both_windows_parse_from_the_real_shape(kimi):
    row = _probe()
    assert row["ok"] is True and row["source"] == "live"
    assert row["windows"] == [
        {"name": "five_hour", "used_pct": 20.0, "resets_at": "2026-10-20T16:00:00Z", "window_mins": 300},
        {"name": "monthly", "used_pct": 35.0, "resets_at": "2026-11-28T00:00:00Z", "window_mins": MONTH_MINS}]
    (request,) = kimi.seen
    assert request["url"] == "https://api.kimi.com/coding/v1/usages"
    assert request["headers"]["Authorization"] == f"Bearer {KEY}"
    assert 0 < request["timeout"] <= usage.KIMI_TIMEOUT_S


def test_the_month_is_the_tighter_of_its_two_limits(kimi):
    reply = json.loads(json.dumps(REPLY))
    reply["usages"]["limit_month_code"]["used_ratio"] = 0.5
    _reply(kimi, reply)
    assert [(w["name"], w["used_pct"]) for w in _probe()["windows"]] == [("five_hour", 20.0), ("monthly", 50.0)]


def test_a_spent_window_reads_as_full(kimi):
    reply = json.loads(json.dumps(REPLY))
    reply["usages"]["limit_5h"]["used_ratio"] = 1
    _reply(kimi, reply)
    assert _probe()["windows"][0]["used_pct"] == 100.0


def test_the_limits_list_stands_in_when_the_ratios_are_missing(kimi):
    _reply(kimi, {"limits": REPLY["limits"]})
    assert _probe()["windows"] == [
        {"name": "five_hour", "used_pct": 20.0, "resets_at": "2026-10-20T16:00:00Z", "window_mins": 300}]


@pytest.mark.parametrize("ratio", [-0.1, "high", True, float("nan")])
def test_a_figure_outside_the_plan_is_a_bad_reply(kimi, ratio):
    reply = json.loads(json.dumps(REPLY))
    reply["usages"]["limit_5h"]["used_ratio"] = ratio
    _reply(kimi, json.dumps(reply, allow_nan=True).encode())
    assert _probe()["error"] == usage.E_BAD


# ---- failures in plain words ---------------------------------------------------------------


def test_no_key_is_a_clear_row_and_makes_no_request(kimi):
    kimi.key = None
    row = _probe()
    assert row["error"] == usage.E_NO_KEY and not kimi.seen
    assert row["note"] == "Khipu has no Kimi key; add it in Settings under Accounts and routing"


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_says_replace_it(kimi, status):
    _reply(kimi, b"", status)
    row = _probe()
    assert row["error"] == usage.E_BAD_KEY
    assert row["note"] == "Kimi did not accept the key; replace it in Settings under Accounts and routing"
    kimi.error = urllib.error.HTTPError(usage.KIMI_USAGE_URL, status, "no", {}, io.BytesIO(b""))
    assert _probe()["error"] == usage.E_BAD_KEY


@pytest.mark.parametrize("body", [{}, {"usages": {}, "limits": []}])
def test_a_key_with_no_plan_says_so(kimi, body):
    _reply(kimi, body)
    row = _probe()
    assert row["ok"] is False and row["error"] == usage.E_NO_PLAN
    assert row["note"] == "Kimi reports no Kimi Code plan on this key"


@pytest.mark.parametrize("error,code", [
    (urllib.error.URLError("dns"), usage.E_NO_ANSWER),
    (socket.timeout("timed out"), usage.E_TIMEOUT),
    (TimeoutError(), usage.E_TIMEOUT),
    (urllib.error.URLError(socket.timeout("timed out")), usage.E_TIMEOUT),
])
def test_network_failures_say_kimi_cannot_be_reached(kimi, error, code):
    kimi.error = error
    row = _probe()
    assert row["error"] == code
    assert row["note"] == "Khipu can't reach Kimi from this Mac just now"


def test_other_failures_get_fixed_codes_and_words(kimi):
    _reply(kimi, b"<html>")
    row = _probe()
    assert row["error"] == usage.E_BAD and row["note"] == "Kimi answered in a way Khipu can't read"
    _reply(kimi, b"", 500)
    row = _probe()
    assert row["error"] == usage.E_CLI and row["note"] == "Kimi reported an error when asked"


def test_the_key_never_appears_in_any_output(kimi, caplog, capsys):
    outputs = []
    _reply(kimi, json.dumps({"error": f"bad {KEY}"}).encode(), 401)
    outputs.append(_probe())
    kimi.error = urllib.error.URLError(f"cannot reach with {KEY}")
    outputs.append(_probe())
    kimi.error = RuntimeError(f"surprise {KEY}")
    outputs.append(_probe())
    kimi.error = None
    _reply(kimi, {"usages": {"limit_5h": {"used_ratio": 0.1, "reset_time": KEY}}})
    outputs.append(_probe())
    _reply(kimi, REPLY)
    rows = usage.probe_all([dict(ACCOUNT)])
    outputs += rows + [usage.public_row(r) for r in rows]
    outputs.append(usage.public_report({"accounts": rows}))
    outputs.append(usage.table({"accounts": rows}))
    assert "month" in usage.table({"accounts": rows}) and "monthly" not in usage.table({"accounts": rows})
    for out in outputs:
        assert KEY not in json.dumps(out, default=str)
    assert KEY not in caplog.text and KEY not in "".join(capsys.readouterr())


# ---- where the key comes from ------------------------------------------------------------


@pytest.fixture
def key_env(monkeypatch):
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.setattr(keychain, "get_password", lambda *a, **k: None)


def test_the_key_resolves_from_env_then_keychain_then_an_owner_only_file(key_env, monkeypatch, tmp_path):
    with pytest.raises(RuntimeError):
        keychain.resolve_kimi_key()
    path = tmp_path / "Kimi API key..txt"
    path.write_text("FAKE-FILE-KEY\n")
    path.chmod(0o644)
    monkeypatch.setenv("KHIPU_KIMI_KEY_FILE", str(path))
    with pytest.raises(RuntimeError):
        keychain.resolve_kimi_key()  # a file others can read is never used
    path.chmod(0o600)
    assert keychain.resolve_kimi_key() == "FAKE-FILE-KEY"
    monkeypatch.setattr(keychain, "get_password", lambda account, **k: "FAKE-KEYCHAIN" if account == "kimi_api_key" else None)
    assert keychain.resolve_kimi_key() == "FAKE-KEYCHAIN"
    monkeypatch.setenv("KIMI_API_KEY", "FAKE-ENV")
    assert keychain.resolve_kimi_key() == "FAKE-ENV"


def test_secrets_status_reports_kimi_presence(key_env):
    status = keychain.secrets_status()
    assert status["kimi_in_keychain"] is False and "kimi_env" in status and "kimi_file_present" in status


def test_the_app_can_store_the_kimi_key(monkeypatch, capsys):
    stored = {}
    monkeypatch.setattr(keychain, "set_password", lambda account, value, **k: stored.__setitem__(account, value))
    monkeypatch.setattr(keychain, "secrets_status", lambda: {"kimi_in_keychain": bool(stored)})
    monkeypatch.setattr("sys.stdin", io.StringIO("FAKE-ONLY\n"))
    assert cli.main(["secrets", "--set", "kimi_api_key"]) == 0
    assert stored == {"kimi_api_key": "FAKE-ONLY"}
    assert json.loads(capsys.readouterr().out)["stored"] is True


# ---- routing -------------------------------------------------------------------------------


def _define_kimi(replay):  # noqa: F811
    replay.write(replay.event("provider_set", id="kimi", label="Kimi", family="Moonshot", login="none",
                              probe="none", accounts=["kimi:main"], models_allowed=["k3.1-pro"]))


def test_the_kimi_account_is_read_only_with_a_key(replay, kimi):  # noqa: F811
    _define_kimi(replay)
    assert [a["quota_key"] for a in usage.http_quota_accounts() if a["harness"] == "kimi"] == ["kimi:main"]
    assert routing_providers.usage_setup("kimi", {"id": "kimi"}) is None
    kimi.key = None
    assert [a for a in usage.http_quota_accounts() if a["harness"] == "kimi"] == []
    assert routing_providers.usage_setup("kimi", {"id": "kimi"}) == "add its key in Settings under Keys on this Mac"


def test_routing_shows_kimi_from_khipus_own_reading_with_pace(replay, kimi, monkeypatch):  # noqa: F811
    _define_kimi(replay)
    monkeypatch.setattr(usage, "_now", lambda: replay.now)
    reply = json.loads(json.dumps(REPLY))
    reply["usages"]["limit_5h"]["reset_time"] = routing._iso(T0 + timedelta(hours=2))
    for i, ratio in enumerate((0.1, 0.2, 0.3)):
        reply["usages"]["limit_5h"]["used_ratio"] = ratio
        _reply(kimi, reply)
        assert usage.refresh_due(accounts=usage.http_quota_accounts(), max_age=0)["refreshed"] == 1
        routing.record_pace_samples(replay.now)
        replay.advance(900)
    entry = next(e for e in replay.view()["accounts"] if e["key"] == "kimi:main")
    assert entry["state"] == routing.OPEN
    assert "usage_setup" not in entry
    paces = {p["window"]: p for p in entry.get("pace") or []}
    assert "five_hour" in paces and paces["five_hour"]["rate_pct_per_h"] is not None
    assert KEY not in json.dumps(replay.view(), default=str)


def test_a_spent_five_hours_reads_as_limited_until_its_reset(replay, kimi, monkeypatch):  # noqa: F811
    _define_kimi(replay)
    monkeypatch.setattr(usage, "_now", lambda: replay.now)
    reply = json.loads(json.dumps(REPLY))
    reset = T0 + timedelta(hours=3)
    reply["usages"]["limit_5h"] = {"used_ratio": 1, "reset_time": routing._iso(reset)}
    _reply(kimi, reply)
    usage.refresh_due(accounts=usage.http_quota_accounts(), max_age=0)
    entry = next(e for e in replay.view()["accounts"] if e["key"] == "kimi:main")
    assert entry["state"] == routing.LIMITED and entry["until"] == routing._iso(reset)


def test_a_passed_reset_is_checked_by_khipu_not_called_untracked(replay, kimi, monkeypatch):  # noqa: F811
    _define_kimi(replay)
    monkeypatch.setattr(usage, "_now", lambda: replay.now)
    reply = json.loads(json.dumps(REPLY))
    reset = T0 + timedelta(hours=1)
    reply["usages"]["limit_5h"] = {"used_ratio": 1, "reset_time": routing._iso(reset)}
    _reply(kimi, reply)
    usage.refresh_due(accounts=usage.http_quota_accounts(), max_age=0)
    replay.advance(2 * 3600)
    entry = next(e for e in replay.view()["accounts"] if e["key"] == "kimi:main")
    assert entry["configured"] is True
    assert routing._NOT_CONFIGURED not in routing.summary(now=replay.now)["text"]
    reply["usages"]["limit_5h"] = {"used_ratio": 0.05, "reset_time": routing._iso(T0 + timedelta(hours=6))}
    _reply(kimi, reply)
    out = routing.check_account("kimi:main", now=replay.now, in_child=True, load=lambda: 0.0)
    assert out["outcome"] == "open"
    assert next(e for e in replay.view()["accounts"] if e["key"] == "kimi:main")["state"] == routing.OPEN
