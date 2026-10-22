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

from khipu import routing_providers, usage

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
        cap = None
        history = tmp_path / "state" / "deepseek-balance.json"

    def urlopen(request, timeout=None):
        seen.append({"url": request.full_url, "headers": dict(request.header_items())})
        if D.error is not None:
            raise D.error
        return D.reply

    monkeypatch.setattr(usage, "_deepseek_key", lambda: KEY)
    monkeypatch.setattr(usage, "deepseek_history_file", lambda: D.history)
    monkeypatch.setattr(usage, "_deepseek_cap_usd", lambda: D.cap)
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
    assert row["spend"]["week_usd"] == 6.0
    assert row["spend"]["balance_usd"] == 24.0


def test_a_top_up_mid_week_is_not_spend_and_never_negative(ds):
    _read(ds, "10.00", _at(19, 8))
    _read(ds, "8.00", _at(20, 8))
    _read(ds, "58.00", _at(20, 9))  # topped up by 50
    row = _read(ds, "55.00", _at(21, 8))
    assert row["spend"]["week_usd"] == 5.0  # 2 + 3, the +50 ignored
    assert row["spend"]["week_usd"] >= 0


def test_week_rollover_resets_spend(ds):
    _read(ds, "30.00", _at(15, 8))
    _read(ds, "20.00", _at(16, 8))
    row = _read(ds, "20.00", _at(19, 8))  # Monday, nothing spent since
    assert row["spend"]["week_usd"] == 0.0
    row = _read(ds, "19.00", _at(20, 8))
    assert row["spend"]["week_usd"] == 1.0


def test_cap_gives_a_weekly_window_with_used_pct(ds):
    ds.cap = 35.0
    _read(ds, "40.00", _at(19, 8))
    row = _read(ds, "22.50", _at(20, 8))
    window = next(w for w in row["windows"] if w["name"] == "weekly")
    assert window["used_pct"] == 50.0 and window["window_mins"] == 10080
    assert window["resets_at"] == usage._iso(datetime(2026, 10, 26, tzinfo=timezone.utc).timestamp())


def test_no_cap_means_no_percentage_window(ds):
    row = _read(ds, "30.00")
    assert row["ok"] is True and row["windows"] == []


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
