"""khipu.usage DeepSeek balance reader: weekly spend from balance decreases.

Hermetic: the HTTP call is faked (``urllib.request.urlopen`` is patched), the
history file lives under tmp_path, and no test reaches DeepSeek or reads a
real key. The fake key is a sentinel searched for in every output.
"""
from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timezone

import pytest

from khipu import config, routing, routing_providers, usage

KEY = "SENTINEL-deepseek-key-0123456789abcdef.do-not-leak"
# Wednesday; the week began Monday 2026-10-19 00:00Z.
WED = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
MON = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)


def _body(total, currency="USD", available=True):
    return json.dumps({"is_available": available, "balance_infos": [
        {"currency": currency, "total_balance": str(total), "granted_balance": "0.00",
         "topped_up_balance": str(total)}]}).encode()


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
def ds(monkeypatch, tmp_path):
    seen = []

    class D:
        reply = _Response(_body("30.00"))
        error = None
        now = WED
        history = tmp_path / "state" / "deepseek-balance.json"

    def urlopen(request, timeout=None):
        seen.append({"url": request.full_url, "headers": dict(request.header_items())})
        if D.error is not None:
            raise D.error
        return D.reply

    monkeypatch.setattr(usage, "_deepseek_key", lambda: KEY)
    monkeypatch.setattr(usage, "deepseek_history_file", lambda: D.history)
    monkeypatch.setattr(usage, "_now", lambda: D.now)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    D.seen = seen
    return D


def _read(ds, total, when=None, **kw):
    ds.reply = _Response(_body(total, **kw))
    if when is not None:
        ds.now = when
    return usage.probe_account({"label": "DeepSeek", "harness": "deepseek", "home": None})


def _at(day, hour=12):
    return datetime(2026, 10, day, hour, 0, tzinfo=timezone.utc)


def test_request_is_a_bearer_get_to_the_balance_endpoint(ds):
    _read(ds, "30.00")
    request = ds.seen[0]
    assert request["url"] == "https://api.deepseek.com/user/balance"
    assert request["headers"]["Authorization"] == f"Bearer {KEY}"


def test_decreases_sum_to_weekly_spend(ds):
    _read(ds, "30.00", _at(19, 8))
    _read(ds, "27.50", _at(20, 8))
    row = _read(ds, "24.00", _at(21, 8))
    assert row["ok"] is True
    assert row["spend"]["last_7d_usd"] == 6.0
    assert row["spend"]["balance_usd"] == 24.0


def test_a_top_up_mid_week_is_not_spend_and_never_negative(ds):
    _read(ds, "10.00", _at(19, 8))
    _read(ds, "8.00", _at(20, 8))
    _read(ds, "58.00", _at(20, 9))  # topped up by 50
    row = _read(ds, "55.00", _at(21, 8))
    assert row["spend"]["last_7d_usd"] == 5.0  # 2 + 3, the +50 ignored
    assert row["spend"]["last_7d_usd"] >= 0


def test_spend_is_rolling_seven_days_not_since_monday(ds):
    _read(ds, "30.00", _at(15, 8))   # last week
    _read(ds, "20.00", _at(16, 8))   # -10, Friday 16th
    row = _read(ds, "19.00", _at(21, 8))  # -1, Wednesday 21st
    assert row["spend"]["last_7d_usd"] == 11.0  # the 16th is inside 7 days
    row = _read(ds, "18.00", _at(24, 9))  # -1; the 16th and 21st-8am: 16th is out
    assert row["spend"]["last_7d_usd"] == 2.0


def test_reader_row_has_no_window_or_cap_of_its_own(ds):
    row = _read(ds, "30.00")
    assert row["ok"] is True and row["windows"] == []
    assert "cap_usd" not in row["spend"]


def test_the_cap_setting_is_gone(ds):
    assert not hasattr(usage, "_deepseek_cap_usd")
    assert "deepseek_weekly_cap_usd" not in open(usage.__file__).read()
    assert "deepseek_weekly_cap_usd" not in open(config.__file__).read()


def _entry(cap=True, harness="deepseek"):
    caps = [{"kind": "spend", "usd": 35.0, "days": 7, "revision": 1, "window": None, "purpose": "job"}] if cap else []
    return {"key": "deepseek|acct", "label": "DeepSeek", "harness": harness, "caps": caps}


def _history(ds, pairs):
    ds.history.parent.mkdir(parents=True, exist_ok=True)
    ds.history.write_text(json.dumps({"samples": [[t.timestamp(), b] for t, b in pairs]}))


def test_used_pct_comes_from_the_routing_spend_cap(ds):
    # tracked since before the window, 17.5 spent in the last 7 days
    _history(ds, [(_at(10, 8), 60.0), (_at(19, 8), 40.0), (_at(21, 8), 22.5)])
    ds.now = _at(21, 12)
    (status,) = routing.cap_statuses({}, _entry(), ds.now)
    assert status["kind"] == "spend" and status["cap_usd"] == 35.0 and status["days"] == 7
    assert status["usd"] == 37.5 and status["used_pct"] == round(37.5 / 35 * 100, 1)
    assert status["verdict"] == "over"


def test_cap_window_is_rolling_days(ds):
    _history(ds, [(_at(5, 8), 100.0), (_at(8, 8), 60.0), (_at(14, 8), 50.0), (_at(20, 8), 40.0), (_at(21, 8), 40.0)])
    ds.now = _at(21, 12)  # window starts the 14th 12:00; the 14th 08:00 pair is out
    (status,) = routing.cap_statuses({}, _entry(), ds.now)
    assert status["usd"] == 10.0 and status["verdict"] == "under"


def test_no_spend_cap_means_dollars_only(ds):
    _history(ds, [(_at(10, 8), 60.0), (_at(20, 8), 40.0)])
    ds.now = _at(21, 12)
    assert routing.cap_statuses({}, _entry(cap=False), ds.now) == []
    row = _read(ds, "39.00")
    assert row["spend"]["last_7d_usd"] == 21.0 and row["windows"] == []


def test_history_younger_than_the_window_is_unknown_unless_already_over(ds):
    _history(ds, [(_at(20, 8), 40.0), (_at(21, 8), 38.0)])
    ds.now = _at(21, 12)
    (status,) = routing.cap_statuses({}, _entry(), ds.now)
    assert status["verdict"] == "unknown" and status["usd"] is None and status["used_pct"] is None
    _history(ds, [(_at(20, 8), 80.0), (_at(21, 8), 40.0)])
    (status,) = routing.cap_statuses({}, _entry(), ds.now)
    assert status["verdict"] == "over" and status["usd"] == 40.0


def test_a_stale_last_read_is_unknown(ds):
    _history(ds, [(_at(5, 8), 100.0), (_at(14, 8), 50.0), (_at(15, 8), 40.0)])
    ds.now = _at(21, 12)
    (status,) = routing.cap_statuses({}, _entry(), ds.now)
    assert status["verdict"] == "unknown" and status["usd"] is None


def test_other_accounts_spend_caps_are_untouched(ds):
    _history(ds, [(_at(5, 8), 100.0), (_at(20, 8), 40.0)])
    ds.now = _at(21, 12)
    (status,) = routing.cap_statuses({}, _entry(harness="glm"), ds.now)
    assert status["verdict"] == "unknown" and "used_pct" not in status


@pytest.mark.parametrize("fail", ["http500", "badjson", "timeout", "401"])
def test_a_failed_read_is_unknown_not_zero_spent(ds, fail):
    _read(ds, "30.00", _at(19, 8))
    _read(ds, "28.00", _at(20, 8))
    if fail == "http500":
        ds.reply = _Response(b"{}", 500)
    elif fail == "badjson":
        ds.reply = _Response(b"not json")
    elif fail == "timeout":
        ds.error = TimeoutError()
    else:
        ds.error = urllib.error.HTTPError("https://api.deepseek.com/user/balance", 401, "x", {}, io.BytesIO(b""))
    ds.now = _at(21, 8)
    row = usage.probe_account({"label": "DeepSeek", "harness": "deepseek", "home": None})
    assert row["ok"] is False and row["error"]
    assert not row.get("spend") and not row.get("windows")
    stored = json.loads(ds.history.read_text())
    assert len(stored["samples"]) == 2  # the failure wrote no sample


def test_non_usd_is_unsupported_not_zero(ds):
    row = _read(ds, "100.00", currency="CNY")
    assert row["ok"] is False and row["error"] == usage.E_CURRENCY
    assert not row.get("spend")
    assert "USD" in row["note"]
    assert not ds.history.exists()


def test_key_never_appears_anywhere(ds, capsys, caplog):
    ds.cap = 35.0
    rows = [_read(ds, "30.00", _at(19, 8)), _read(ds, "29.00", _at(20, 8))]
    ds.error = urllib.error.URLError(f"boom {KEY}")
    rows.append(usage.probe_account({"label": "DeepSeek", "harness": "deepseek", "home": None}))
    ds.error = RuntimeError(f"boom {KEY}")
    rows.append(usage.probe_account({"label": "DeepSeek", "harness": "deepseek", "home": None}))
    out = capsys.readouterr()
    blob = json.dumps(rows) + out.out + out.err + caplog.text + ds.history.read_text()
    assert KEY not in blob and "SENTINEL" not in blob
    assert oct(ds.history.stat().st_mode & 0o777) == "0o600"
    assert set(json.loads(ds.history.read_text())) == {"samples"}


def test_usage_setup_clears_when_the_reader_is_available(ds):
    provider = {"id": "deepseek"}
    assert routing_providers.usage_setup("deepseek", provider) is None


def test_usage_setup_still_asks_when_there_is_no_key(ds, monkeypatch):
    monkeypatch.setattr(usage, "_deepseek_key", lambda: None)
    assert routing_providers.usage_setup("deepseek", {"id": "deepseek"}) == (
        "no usage reader set up; confirm its quota host in Settings → Accounts")
