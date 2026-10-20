"""khipu.usage http-quota adapter (slice 9b).

Hermetic: the HTTP call is faked at urllib.request.urlopen, the key is a
fake sentinel from a faked keychain.get_password, and nothing reaches a
real host, keychain or credential file.
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from khipu import keychain, usage

KEY = "SENTINEL-http-quota-key-do-not-leak"
ACCOUNT = {"label": "Synthetic", "harness": "synthetic-provider", "home": None,
           "probe": "http-quota", "host": "quota.invalid", "provider": "synthetic-provider",
           "quota_key": "synthetic:main"}
REPLY = {"windows": [{"used_pct": 42, "resets_at": 1_800_500_000, "window_mins": 10080},
                     {"percentage": 10, "reset_at": "2027-01-01T00:00:00Z", "window_mins": 300}]}


class _Response:
    def __init__(self, body: bytes, status: int = 200):
        self.status, self._body = status, body

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake(monkeypatch):
    seen = []

    class Fake:
        reply = _Response(json.dumps(REPLY).encode())
        error = None

    def urlopen(request, timeout=None):
        seen.append({"url": request.full_url, "headers": dict(request.header_items()), "timeout": timeout})
        if Fake.error is not None:
            raise Fake.error
        return Fake.reply

    monkeypatch.setattr(keychain, "get_password", lambda *a, **k: KEY)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    Fake.seen = seen
    return Fake


def _probe(**kw):
    return usage.probe_account(dict(ACCOUNT), **kw)


def test_windows_parse_and_key_goes_only_to_the_confirmed_host(fake):
    row = _probe()
    assert row["ok"] is True and row["source"] == "live"
    assert row["windows"] == [
        {"name": "weekly", "used_pct": 42, "resets_at": usage._iso(1_800_500_000), "window_mins": 10080},
        {"name": "five_hour", "used_pct": 10, "resets_at": "2027-01-01T00:00:00Z", "window_mins": 300}]
    assert len(fake.seen) == 1
    assert fake.seen[0]["url"] == "https://quota.invalid/"
    assert fake.seen[0]["headers"]["Authorization"] == KEY
    assert fake.seen[0]["timeout"] == usage.HTTP_QUOTA_TIMEOUT_S
    assert KEY not in json.dumps(row)


def test_glm_shaped_limits_parse(fake):
    fake.reply = _Response(json.dumps({"code": 200, "data": {"limits": [
        {"type": "TOKENS_LIMIT", "unit": 3, "percentage": 15, "nextResetTime": 1_800_018_000_000},
        {"type": "TOKENS_LIMIT", "unit": 6, "percentage": 25, "nextResetTime": 1_800_500_000_000}]}}).encode())
    row = _probe()
    assert [w["used_pct"] for w in row["windows"]] == [15, 25]
    assert [w["name"] for w in row["windows"]] == ["five_hour", "weekly"]


def test_a_returned_redirect_is_refused_and_never_followed(fake):
    fake.reply = _Response(KEY.encode(), status=302)
    row = _probe()
    assert row["error"] == usage.E_REDIRECT == "redirect_refused"
    assert len(fake.seen) == 1 and KEY not in json.dumps(row)


def test_a_raised_redirect_is_refused_without_reading_its_body(fake):
    fake.error = urllib.error.HTTPError("https://quota.invalid/", 302, "found", {},
                                        io.BytesIO(KEY.encode()))
    row = _probe()
    assert row["error"] == usage.E_REDIRECT and KEY not in json.dumps(row)


def test_no_key_makes_no_request(fake, monkeypatch):
    monkeypatch.setattr(keychain, "get_password", lambda *a, **k: None)
    row = _probe()
    assert row["error"] == usage.E_NO_KEY and not fake.seen


def test_a_bad_host_makes_no_request(fake):
    row = usage.probe_account({**ACCOUNT, "host": "https://evil.invalid/path"})
    assert row["error"] == usage.E_INVALID and not fake.seen


def test_statuses_get_closed_codes_and_bodies_never_travel(fake):
    fake.reply = _Response(json.dumps({"msg": KEY}).encode(), status=401)
    assert _probe()["error"] == usage.E_BAD_KEY
    fake.reply = _Response(KEY.encode(), status=500)
    row = _probe()
    assert row["error"] == usage.E_CLI and KEY not in json.dumps(row)
    fake.error = RuntimeError(f"surprise {KEY}")
    row = _probe()
    assert row["error"] == usage.E_NO_ANSWER and KEY not in json.dumps(row)


def test_an_unreadable_or_empty_reply_is_a_closed_code(fake):
    fake.reply = _Response(b"<html>not json</html>")
    assert _probe()["error"] == usage.E_BAD
    fake.reply = _Response(json.dumps({"windows": []}).encode())
    assert _probe()["error"] == usage.E_NO_LIMITS
