"""khipu.usage DeepSeek balance reader: measured spend from balance decreases.

Hermetic: the HTTP transport is faked (``OpenerDirector.open``, or the
handlers' ``do_open`` where the real redirect and proxy machinery must run),
the history file lives under tmp_path, and no test reaches DeepSeek or reads a
real key. The fake key is a sentinel searched for in every output.
"""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import threading
import time
import urllib.error
import urllib.request
import urllib.response
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.message import Message

import pytest

from khipu import cli, config, keychain, routing, routing_advice, routing_pace, routing_providers, routing_sources, usage

KEY = "SENTINEL-deepseek-key-0123456789abcdef.do-not-leak"
OTHER_KEY = "SENTINEL-deepseek-other-account-key"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
ACCOUNT = {"label": "DeepSeek", "harness": "deepseek", "home": None}


def _body(total, currency="USD", available=True):
    return json.dumps({"is_available": available, "balance_infos": [
        {"currency": currency, "total_balance": str(total), "granted_balance": "0.00",
         "topped_up_balance": str(total)}]}).encode()


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
def ds(monkeypatch, tmp_path):
    seen = []

    class D:
        reply = _Response(_body("30.00"))
        error = None
        now = NOW
        history = tmp_path / "data" / "state" / "deepseek-balance.json"
        key = KEY

    def opener_open(self, request, data=None, timeout=None):  # noqa: ARG001 - OpenerDirector.open stand-in
        seen.append({"url": request.full_url, "headers": dict(request.header_items())})
        if D.error is not None:
            raise D.error
        return D.reply

    monkeypatch.setattr(usage, "_deepseek_key", lambda: D.key)
    monkeypatch.setattr(usage, "deepseek_history_file", lambda: D.history)
    monkeypatch.setattr(usage, "_now", lambda: D.now)
    monkeypatch.setattr(routing, "_telemetry_streams", lambda view: [])
    monkeypatch.setattr("urllib.request.OpenerDirector.open", opener_open)
    D.seen = seen
    return D


def _read(ds, total, when=None, **kw):
    ds.reply = _Response(_body(total, **kw))
    ds.error = None
    if when is not None:
        ds.now = when
    return usage.probe_account(dict(ACCOUNT))


def _ago(days=0.0, minutes=0.0, seconds=0.0):
    return NOW - timedelta(days=days, minutes=minutes, seconds=seconds)


def _entry(usd=35.0, days=7, harness="deepseek"):
    caps = [{"kind": "spend", "usd": usd, "days": days, "revision": 1, "window": None, "purpose": "job"}]
    return {"key": "deepseek|acct", "label": "DeepSeek", "harness": harness, "caps": caps}


def _cap(ds, usd=35.0, days=7, view=None):
    ds.now = NOW
    (status,) = routing.cap_statuses(view or {}, _entry(usd, days), NOW)
    return status


# ---- the request ------------------------------------------------------------


def test_request_is_a_bearer_get_to_the_balance_endpoint(ds):
    row = _read(ds, "30.00")
    assert row["ok"] is True
    (request,) = ds.seen
    assert request["url"] == "https://api.deepseek.com/user/balance"
    assert request["headers"]["Authorization"] == f"Bearer {KEY}"


def _redirecting_transport(monkeypatch, target):
    """Fake only the socket layer: urllib's own redirect and proxy handling runs."""
    hops = []

    def do_open(self, http_class, req, **kw):  # noqa: ARG001 - handler stand-in
        hops.append({"url": req.full_url, "host": req.host,
                     "has_key": KEY in (req.get_header("Authorization") or "")})
        headers = Message()
        if len(hops) == 1:
            headers["Location"] = target
            response = urllib.response.addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
            response.msg = "Found"
        else:
            response = urllib.response.addinfourl(io.BytesIO(_body("50")), headers, req.full_url, 200)
            response.msg = "OK"
        return response

    monkeypatch.setattr(urllib.request.HTTPSHandler, "do_open", do_open)
    monkeypatch.setattr(urllib.request.HTTPHandler, "do_open", do_open)
    monkeypatch.setattr(urllib.request, "_opener", None)
    return hops


@pytest.mark.parametrize("target", ["https://attacker.invalid/collect", "http://attacker.invalid/collect",
                                    "https://api.deepseek.com/elsewhere"])
def test_a_redirect_is_refused_and_the_key_never_leaves_the_first_request(monkeypatch, target):
    # Codex P1 (usage.py:1516): urllib's default handler forwarded Authorization
    # to another host and over plain HTTP.
    hops = _redirecting_transport(monkeypatch, target)
    with pytest.raises(usage.ProbeError) as raised:
        usage._deepseek_request(KEY, 1.0)
    assert raised.value.code == usage.E_REDIRECT
    assert [h["url"] for h in hops] == ["https://api.deepseek.com/user/balance"]


def test_an_inherited_proxy_is_never_used(monkeypatch):
    hops = _redirecting_transport(monkeypatch, "https://api.deepseek.com/user/balance")
    monkeypatch.setenv("https_proxy", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    with contextlib.suppress(usage.ProbeError):
        usage._deepseek_request(KEY, 1.0)
    assert hops and all(h["host"] == "api.deepseek.com" for h in hops)


# ---- measured spend --------------------------------------------------------------


def test_decreases_inside_the_window_are_measured_spend(ds):
    _read(ds, "30.00", _ago(days=8))
    _read(ds, "30.00", _ago(days=3))
    _read(ds, "27.50", _ago(days=2))
    row = _read(ds, "24.00", _ago(days=1))
    assert row["ok"] is True
    assert row["spend"]["last_7d_at_least_usd"] == 6.0
    assert row["spend"]["balance_usd"] == 24.0
    assert row["spend"]["partial"] is False


def test_measured_spend_above_the_cap_is_over(ds):
    _read(ds, "60.00", _ago(days=8))
    _read(ds, "60.00", _ago(days=6))
    _read(ds, "40.00", _ago(days=2))
    _read(ds, "22.50", _ago(minutes=5))
    status = _cap(ds)
    assert status["verdict"] == "over" and status["usd"] == 37.5
    assert status["used_pct"] == round(37.5 / 35 * 100, 1)


def test_measured_spend_over_the_cap_counts_with_partial_history(ds):
    _read(ds, "80.00", _ago(days=1))
    _read(ds, "40.00", _ago(minutes=5))
    status = _cap(ds)
    assert status["verdict"] == "over" and status["usd"] == 40.0 and status["partial"] is True


def test_full_history_measured_spend_can_read_under(ds):
    _read(ds, "100.00", _ago(days=8))
    _read(ds, "100.00", _ago(days=6))
    _read(ds, "95.00", _ago(minutes=5))
    status = _cap(ds)
    assert status["verdict"] == "under" and status["usd"] == 5.0 and status["used_pct"] == 14.3
    assert status["partial"] is False
    assert status["why"] is None


def test_only_observed_decreases_count_as_measured_spend(ds):
    # Spend netted against a top-up within one interval cannot be recovered
    # from the balance endpoint; the measured decrease remains $10.
    _read(ds, "100.00", _ago(days=8))
    _read(ds, "100.00", _ago(days=1))
    _read(ds, "90.00", NOW)
    status = _cap(ds)
    assert status["verdict"] == "under" and status["usd"] == 10.0


def test_an_unchanged_balance_has_zero_measured_spend(ds):
    _read(ds, "100.00", _ago(days=8))
    _read(ds, "100.00", _ago(days=1))
    _read(ds, "100.00", NOW)
    assert _cap(ds)["verdict"] == "under"


def test_a_top_up_is_never_negative_spend(ds):
    _read(ds, "10.00", _ago(days=8))
    _read(ds, "8.00", _ago(days=2))
    _read(ds, "58.00", _ago(days=1))  # topped up by 50
    row = _read(ds, "55.00", _ago(minutes=1))
    # Both observed decreases count; the +50 contributes nothing.
    assert row["spend"]["last_7d_usd"] == 5.0


def test_a_decrease_is_attributed_to_the_read_that_measured_it(ds):
    # A rolling window uses the time a decrease was measured; it cannot
    # reconstruct the provider's individual debit times within an interval.
    _read(ds, "100.00", _ago(days=8))
    _read(ds, "50.00", _ago(days=6))
    _read(ds, "50.00", NOW)
    status = _cap(ds)
    assert status["verdict"] == "over" and status["usd"] == 50.0


def test_the_window_rolls(ds):
    _read(ds, "30.00", _ago(days=9))
    _read(ds, "30.00", _ago(days=6, minutes=1))
    _read(ds, "20.00", _ago(days=6))      # -10, inside 7 days
    row = _read(ds, "19.00", _ago(days=1))  # -1
    assert row["spend"]["last_7d_at_least_usd"] == 11.0
    row = _read(ds, "18.00", NOW + timedelta(days=2))
    # The window now starts 5 days ago: the -10 measurement is out, while
    # both later $1 measurements remain in the rolling window.
    assert row["spend"]["last_7d_usd"] == 2.0


def test_reader_row_has_no_window_or_cap_of_its_own(ds):
    row = _read(ds, "30.00")
    assert row["ok"] is True and row["windows"] == []
    assert "cap_usd" not in row["spend"]


def test_the_cap_setting_is_gone(ds):
    assert not hasattr(usage, "_deepseek_cap_usd")
    assert "deepseek_weekly_cap_usd" not in open(usage.__file__).read()
    assert "deepseek_weekly_cap_usd" not in open(config.__file__).read()


def test_other_accounts_spend_caps_are_untouched(ds):
    _read(ds, "100.00", _ago(days=8))
    _read(ds, "40.00", _ago(days=1))
    (status,) = routing.cap_statuses({}, _entry(harness="glm"), NOW)
    assert status["verdict"] == "unknown" and "used_pct" not in status and "at_least_usd" not in status


# ---- partial and owner-facing figures ----------------------------------------


def test_the_first_read_reports_zero_measured_spend_as_partial(ds):
    # The first read is zero measured spend since now, never full coverage.
    report = usage.report(accounts=[dict(ACCOUNT)], fresh=True)
    spend = report["accounts"][0]["spend"]
    assert spend["partial"] is True
    assert spend["last_7d_usd"] == 0


def test_history_covers_long_windows(ds):
    # Codex P2 (usage.py:1568): 21-day trimming made a 30-day cap's coverage
    # impossible.
    _read(ds, "100.00", _ago(days=31))
    _read(ds, "100.00", NOW)
    total = usage.deepseek_spend_total(NOW, 30, 35.0)
    assert total["partial"] is False


def test_history_at_the_poll_cadence_covers_the_window(ds, monkeypatch):
    # Codex P2: 4,000 samples at 120 s cover only 5.55 days.
    step = 120
    count = 7 * 86400 // step + 60
    start = NOW - timedelta(seconds=step * count)
    balance = 1000.0
    for i in range(count + 1):
        balance -= 0.001
        _read(ds, f"{balance:.4f}", start + timedelta(seconds=step * i))
    total = usage.deepseek_spend_total(NOW, 7, 35.0)
    assert total["partial"] is False
    assert total["at_least_usd"] == pytest.approx(7 * 86400 / step * 0.001, abs=0.05)
    stored = json.loads(ds.history.read_text())
    assert len(stored["buckets"]) <= count
    assert total["usd"] == pytest.approx(7 * 86400 / step * 0.001)


def test_buckets_stay_bounded_over_a_year(ds):
    data = usage._deepseek_new(KEY, 0.0, 1e6)
    t, balance = 0.0, 1e6
    while t < 400 * 86400:
        t += 1800
        balance -= 0.01
        usage._deepseek_add(data, t, balance)
    assert len(data["buckets"]) <= usage.DEEPSEEK_HISTORY_MAX
    assert data["buckets"][0][1] > t - usage.DEEPSEEK_HISTORY_DAYS * 86400


# ---- failed, unavailable, missing or changed key -----------------------------


@pytest.mark.parametrize("fail", ["http500", "badjson", "timeout", "401"])
def test_a_failed_read_stores_no_sample_and_is_unknown(ds, fail):
    # Codex P2 (usage.py:1605): fresh history kept a $0 'under' after a failure.
    _read(ds, "30.00", _ago(days=8))
    _read(ds, "30.00", _ago(minutes=2))
    if fail == "http500":
        ds.reply = _Response(b"{}", 500)
    elif fail == "badjson":
        ds.reply = _Response(b"not json")
    elif fail == "timeout":
        ds.error = TimeoutError()
    else:
        ds.error = urllib.error.HTTPError("https://api.deepseek.com/user/balance", 401, "x", {}, io.BytesIO(b""))
    ds.now = _ago(minutes=1)
    row = usage.probe_account(dict(ACCOUNT))
    assert row["ok"] is False and row["error"]
    assert not row.get("spend") and not row.get("windows")
    status = _cap(ds)
    assert status["verdict"] == "unknown" and "failed" in status["why"]
    stored = json.loads(ds.history.read_text())
    assert stored["last"][1] == 30.0 and stored["attempt"]["ok"] is False


def test_an_unavailable_account_is_a_failed_read(ds):
    # Codex P2 (usage.py:1538): is_available false still produced ok and 'under'.
    _read(ds, "100.00", _ago(days=8))
    _read(ds, "100.00", _ago(days=1))
    row = _read(ds, "100.00", NOW, available=False)
    assert row["ok"] is False and row["error"] == usage.E_UNAVAILABLE
    assert "can't be used" in row["note"]
    assert _cap(ds)["verdict"] == "unknown"


def test_a_removed_key_is_unknown(ds):
    _read(ds, "100.00", _ago(days=8))
    _read(ds, "60.00", _ago(minutes=1))
    ds.key = None
    status = _cap(ds)
    assert status["verdict"] == "unknown" and status["at_least_usd"] is None


def test_a_changed_key_starts_a_new_history(ds):
    # Codex P2 (usage.py:1491): a key for another account manufactured $99 of spend.
    _read(ds, "100.00", _ago(days=8))
    _read(ds, "100.00", _ago(minutes=1))
    ds.key = OTHER_KEY
    assert _cap(ds)["verdict"] == "unknown"  # before any read with the new key
    row = _read(ds, "1.00", NOW)
    assert row["spend"]["last_7d_at_least_usd"] == 0.0 and row["spend"]["partial"] is True
    status = _cap(ds)
    assert status["verdict"] == "under" and status["usd"] == 0.0


def test_more_than_one_deepseek_account_is_refused(ds):
    definitions = {"providers": {"deepseek": {"id": "deepseek"}},
                   "accounts": {"a": {"provider": "deepseek"}, "b": {"provider": "deepseek"}}}
    accounts = usage.deepseek_accounts(definitions)
    assert len(accounts) == 2 and all(a.get("invalid") for a in accounts)
    _read(ds, "100.00", _ago(days=8))
    _read(ds, "10.00", _ago(minutes=1))
    status = _cap(ds, view={"definitions": definitions})
    assert status["verdict"] == "unknown"


# ---- damaged, future and conflicting history ---------------------------------


@pytest.mark.parametrize("raw", [
    {"samples": None}, {"samples": 3}, {"samples": [[0, 100.0]]}, [], "x", None,
])
def test_a_damaged_history_is_unknown_and_never_crashes_routing(ds, raw):
    # Codex P2 (usage.py:1559): samples null/3 raised TypeError from cap_statuses.
    ds.history.parent.mkdir(parents=True, exist_ok=True)
    ds.history.write_text(json.dumps(raw))
    os.chmod(ds.history, 0o600)
    assert _cap(ds)["verdict"] == "unknown"


@pytest.mark.parametrize("corruption", ["since", "last", "bucket", "attempt", "nested"])
def test_corrupt_numeric_or_nested_history_heals_on_next_probe(ds, corruption):
    _read(ds, "100.00", _ago(days=8))
    _read(ds, "90.00", _ago(seconds=1))
    data = json.loads(ds.history.read_text())
    huge = 10 ** 400
    if corruption == "since":
        data["since"] = huge
    elif corruption == "last":
        data["last"][0] = huge
    elif corruption == "bucket":
        data["buckets"][0][0] = huge
    elif corruption == "attempt":
        data["attempt"]["at"] = huge
    raw = "[" * 10000 + "0" + "]" * 10000 if corruption == "nested" else json.dumps(data)
    ds.history.write_text(raw)
    assert _cap(ds)["verdict"] == "unknown"

    # A successful provider response must replace damaged history with its
    # actual balance, rather than keep crashing or invent any earlier spend.
    ds.reply = _Response(_body("80.00"))
    row = usage.probe_deepseek()
    assert row["ok"] is True
    assert row["spend"]["last_7d_at_least_usd"] == 0.0
    healed = json.loads(ds.history.read_text())
    assert healed["since"] == NOW.timestamp()
    assert healed["last"] == [NOW.timestamp(), 80.0]
    assert healed["buckets"] == []
    status = _cap(ds)
    assert status["verdict"] == "under" and status["usd"] == 0.0
    assert status["why"] is None
    again = _read(ds, "80.00", NOW + timedelta(seconds=1))
    assert again["ok"] is True and again["spend"]["last_7d_at_least_usd"] == 0.0


def test_a_negative_balance_in_history_is_damaged(ds):
    _read(ds, "100.00", _ago(days=8))
    data = json.loads(ds.history.read_text())
    data["last"] = [data["last"][0], -100.0]
    ds.history.write_text(json.dumps(data))
    assert _cap(ds)["verdict"] == "unknown" and "damaged" in _cap(ds)["why"]


def test_a_future_reading_is_not_fresh_evidence(ds):
    # Codex P2: a reading stamped 3 days ahead (a clock step) masked staleness.
    _read(ds, "100.00", _ago(days=8))
    _read(ds, "100.00", NOW + timedelta(days=3))
    assert _cap(ds)["verdict"] == "unknown"


def test_a_future_decrease_is_not_counted(ds):
    _read(ds, "100.00", _ago(days=8))
    _read(ds, "100.00", _ago(seconds=1))
    _read(ds, "0.00", NOW + timedelta(days=3))
    assert _cap(ds)["verdict"] == "unknown"


def test_a_far_future_reading_never_crashes(ds):
    _read(ds, "100.00", _ago(days=8))
    _read(ds, "100.00", datetime(9999, 1, 1, tzinfo=timezone.utc))
    assert _cap(ds)["verdict"] == "unknown"


def test_conflicting_readings_at_one_instant_are_dropped(ds):
    # Codex P2: equal times sorted by balance and lost the decrease.
    _read(ds, "50.00", _ago(days=8))
    _read(ds, "100.00", NOW)
    _read(ds, "50.00", NOW)
    assert _cap(ds)["verdict"] == "under"
    assert json.loads(ds.history.read_text())["last"][1] == 100


def test_an_older_reading_landing_late_is_dropped(ds, monkeypatch):
    # Codex P2 (usage.py:1633), the ordering half: a slower poll sent before a
    # faster one must not overwrite the faster one's decrease.
    _read(ds, "100.00", _ago(days=8))
    _read(ds, "100.00", _ago(minutes=2))
    clock = iter([_ago(minutes=1), NOW, NOW, NOW, NOW, NOW])
    monkeypatch.setattr(usage, "_now", lambda: next(clock, NOW))
    newer_done = threading.Event()

    def request(key, timeout):  # noqa: ARG001
        if threading.current_thread().name == "older":
            assert newer_done.wait(5)
            return 200, _body("100.00")
        return 200, _body("90.00")

    monkeypatch.setattr(usage, "_deepseek_request", request)
    older = threading.Thread(name="older", target=lambda: usage.probe_account(dict(ACCOUNT)))
    older.start()
    time.sleep(0.05)
    newer = threading.Thread(name="newer", target=lambda: (usage.probe_account(dict(ACCOUNT)), newer_done.set()))
    newer.start()
    newer.join(5)
    older.join(5)
    total = usage.deepseek_spend_total(NOW, 7, 5.0)
    assert total["status"] == "known" and total["usd"] == 10.0


def test_a_poll_waits_for_the_history_lock(ds):
    # Codex P2 (usage.py:1633), the race half: the read/append/replace cycle
    # was unlocked, so a concurrent poll could overwrite a recorded decrease.
    _read(ds, "100.00", _ago(days=8))
    lock = ds.history.with_name(usage.DEEPSEEK_LOCK_NAME)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    before = ds.history.read_text()
    worker = threading.Thread(target=_read, args=(ds, "90.00", NOW))
    try:
        worker.start()
        time.sleep(0.3)
        assert ds.history.read_text() == before
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    worker.join(5)
    assert json.loads(ds.history.read_text())["last"][1] == 90.0


# ---- files: no symlinks, owner-only ------------------------------------------


def test_a_symlinked_state_folder_is_never_written_through(ds, tmp_path):
    # Codex P2 (usage.py:1572): a symlinked state folder moved the write outside.
    outside = tmp_path / "outside"
    outside.mkdir()
    ds.history.parent.parent.mkdir(parents=True, exist_ok=True)
    ds.history.parent.symlink_to(outside, target_is_directory=True)
    _read(ds, "40.00")
    assert list(outside.iterdir()) == []


def test_a_symlinked_history_file_is_never_read(ds, tmp_path):
    # Codex P2 (usage.py:1555): the history file's symlink was followed on read.
    _read(ds, "100.00", _ago(days=8))
    _read(ds, "100.00", _ago(days=6))
    _read(ds, "10.00", _ago(minutes=1))
    victim = tmp_path / "victim.json"
    victim.write_text(ds.history.read_text())
    os.chmod(victim, 0o600)
    ds.history.unlink()
    ds.history.symlink_to(victim)
    status = _cap(ds)
    assert status["verdict"] == "unknown" and status["at_least_usd"] is None


def test_a_group_readable_history_is_damaged(ds):
    _read(ds, "100.00", _ago(days=8))
    _read(ds, "10.00", _ago(minutes=1))
    os.chmod(ds.history, 0o644)
    assert _cap(ds)["verdict"] == "unknown"


@pytest.fixture
def key_env(monkeypatch):
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(keychain, "get_password", lambda *a, **k: None)


def test_a_symlinked_key_file_is_never_read(key_env, monkeypatch, tmp_path):
    # Codex P2 (keychain.py:292): a key-file symlink sent an unrelated file as Authorization.
    victim = tmp_path / "victim"
    victim.write_text("FAKE-UNRELATED-PRIVATE-FILE")
    os.chmod(victim, 0o600)
    link = tmp_path / "key-link"
    link.symlink_to(victim)
    monkeypatch.setenv("KHIPU_DEEPSEEK_KEY_FILE", str(link))
    with pytest.raises(RuntimeError):
        keychain.resolve_deepseek_key()


def test_a_shared_key_file_is_never_read(key_env, monkeypatch, tmp_path):
    path = tmp_path / "key"
    path.write_text("FAKE-SHARED-KEY")
    os.chmod(path, 0o644)
    monkeypatch.setenv("KHIPU_DEEPSEEK_KEY_FILE", str(path))
    with pytest.raises(RuntimeError):
        keychain.resolve_deepseek_key()
    os.chmod(path, 0o600)
    assert keychain.resolve_deepseek_key() == "FAKE-SHARED-KEY"


def test_the_documented_secrets_command_accepts_deepseek(monkeypatch, capsys):
    # Codex P3 (keychain.py:318, cli.py:3143): argparse rejected deepseek_api_key.
    stored = {}
    monkeypatch.setattr(keychain, "set_password", lambda account, value, **k: stored.__setitem__(account, value))
    monkeypatch.setattr(keychain, "secrets_status", lambda: {"deepseek_in_keychain": bool(stored)})
    monkeypatch.setattr("sys.stdin", io.StringIO("FAKE-ONLY\n"))
    assert cli.main(["secrets", "--set", "deepseek_api_key"]) == 0
    assert stored == {"deepseek_api_key": "FAKE-ONLY"}
    assert json.loads(capsys.readouterr().out)["stored"] is True


def test_secrets_status_reports_deepseek_presence(key_env):
    status = keychain.secrets_status()
    assert status["deepseek_in_keychain"] is False and "deepseek_env" in status


# ---- key hygiene and setup ---------------------------------------------------


def test_key_never_appears_anywhere(ds, capsys, caplog):
    rows = [_read(ds, "30.00", _ago(days=2)), _read(ds, "29.00", _ago(days=1))]
    ds.error = urllib.error.URLError(f"boom {KEY}")
    rows.append(usage.probe_account(dict(ACCOUNT)))
    ds.error = RuntimeError(f"boom {KEY}")
    rows.append(usage.probe_account(dict(ACCOUNT)))
    out = capsys.readouterr()
    blob = json.dumps(rows) + out.out + out.err + caplog.text + ds.history.read_text()
    assert KEY not in blob and "SENTINEL" not in blob
    assert oct(ds.history.stat().st_mode & 0o777) == "0o600"
    assert set(json.loads(ds.history.read_text())) == {
        "version", "salt", "fingerprint", "since", "last", "buckets", "topups", "attempt"}


def test_non_usd_is_unsupported_not_zero(ds):
    row = _read(ds, "100.00", currency="CNY")
    assert row["ok"] is False and row["error"] == usage.E_CURRENCY
    assert not row.get("spend")
    assert "USD" in row["note"]
    assert not ds.history.exists()


def test_usage_setup_clears_when_the_reader_is_available(ds):
    assert routing_providers.usage_setup("deepseek", {"id": "deepseek"}) is None


def test_usage_setup_still_asks_when_there_is_no_key(ds):
    ds.key = None
    assert routing_providers.usage_setup("deepseek", {"id": "deepseek"}) == (
        "no usage reader set up; confirm its quota host in Settings → Accounts")


@pytest.mark.parametrize("purpose", ["job", "check", "orchestration"])
def test_usage_partial_cap_allows_advice_and_rank(ds, monkeypatch, purpose):
    _read(ds, "100", _ago(days=1))
    _read(ds, "90", NOW)
    entry = dict(_entry(), state="open")
    view = {"accounts": [entry]}
    monkeypatch.setattr(routing, "account_id", lambda key: "fake-account")
    advice = routing_advice.account_advice(view, entry, NOW, purpose=purpose)
    assert advice["verdict"] == "go"
    assert advice["reason"] != "cap status unknown"
    assert advice["caps"][0]["partial"] is True
    assert routing_advice.rank(view, NOW, purpose=purpose)[0]["verdict"] == "go"
    public = routing._public_account(entry, NOW, view)
    assert public["advice"]["verdict"] == "go"


@pytest.mark.parametrize("harness", ["deepseek", "glm"])
def test_usage_no_cap_and_no_reader_keep_go(ds, monkeypatch, harness):
    entry = dict(_entry(harness=harness), caps=[], state="open")
    ds.key = None
    monkeypatch.setattr(routing, "account_id", lambda key: "fake-account")
    assert routing_advice.account_advice({}, entry, NOW)["verdict"] == "go"


@pytest.mark.parametrize("kind", ["pace_target", "concurrency"])
def test_usage_unknown_job_only_cap_does_not_hold_orchestration(ds, monkeypatch, kind):
    entry = dict(_entry(), state="open")
    monkeypatch.setattr(routing, "account_id", lambda key: "fake-account")
    monkeypatch.setattr(routing, "cap_statuses", lambda *a: [{"kind": kind, "verdict": "unknown"}])
    assert routing_advice.account_advice({}, entry, NOW)["verdict"] == "go"
    assert routing_advice.account_advice({}, entry, NOW, purpose="orchestration")["verdict"] == "go"


@pytest.mark.parametrize("purpose", ["job", "check", "orchestration"])
@pytest.mark.parametrize("harness,kind", [
    ("claude", "pace_target"), ("codex", "pace_target"),
    ("deepseek", "pace_target"), ("deepseek", "per_day"),
    ("deepseek", "concurrency"), ("deepseek", "share"),
])
def test_usage_unknown_non_floor_caps_keep_go_with_unknown_evidence(ds, monkeypatch, harness, kind, purpose):
    entry = {"key": f"{harness}|acct", "harness": harness, "state": "open",
             "caps": [{"kind": kind, "window": "weekly", "pct": 50, "n": 1}]}
    monkeypatch.setattr(routing, "account_id", lambda key: "fake-account")
    # Exercise real cap evaluation with missing pace or runner readings.
    (status,) = routing.cap_statuses({}, entry, NOW)
    assert status["kind"] == kind and status["verdict"] == "unknown"
    advice = routing_advice.account_advice({}, entry, NOW, purpose=purpose)
    assert advice["verdict"] == "go"
    assert advice["evidence"] == "unknown"


@pytest.mark.parametrize("purpose", ["job", "check", "orchestration"])
@pytest.mark.parametrize("has_key", [False, True], ids=["no-key", "no-history"])
def test_usage_unknown_deepseek_spend_without_evidence_holds_all_purposes(ds, monkeypatch, purpose, has_key):
    ds.key = KEY if has_key else None
    entry = dict(_entry(), state="open")
    monkeypatch.setattr(routing, "account_id", lambda key: "fake-account")
    (status,) = routing.cap_statuses({}, entry, NOW)
    assert status["verdict"] == "unknown" and status["at_least_usd"] is None
    advice = routing_advice.account_advice({}, entry, NOW, purpose=purpose)
    assert advice["verdict"] == "hold_back" and advice["evidence"] == "unknown"


def _runner_stream(usd):
    return {"runner": "fake-runner", "account": "deepseek|acct", "silent": False,
            "incomplete": False, "accumulating_since": _ago(days=8),
            "buckets": [{"at": _ago(minutes=10), "usd": usd, "n": 1, "ids": ["fake-event"]}],
            "corrections": [], "dropped": [], "active": [], "launches": [],
            "coverage": [(_ago(days=7), NOW)]}


@pytest.mark.parametrize("case", ["normal", "missing_key", "multiple_accounts"])
@pytest.mark.parametrize("runner_usd", [10, 20, 35, 40])
def test_usage_account_balance_is_authoritative_over_runner_telemetry(ds, monkeypatch, case, runner_usd):
    _read(ds, "100", _ago(days=1))
    _read(ds, "90", NOW)
    monkeypatch.setattr(routing, "_telemetry_streams", lambda view: [_runner_stream(runner_usd)])
    view = {}
    if case == "missing_key":
        ds.key = None
    elif case == "multiple_accounts":
        view = {"definitions": {"accounts": {"a": {"provider": "deepseek"},
                                               "b": {"provider": "deepseek"}}}}
    status = _cap(ds, view=view)
    assert status["verdict"] == ("under" if case == "normal" else "unknown")
    assert status["usd"] == (10 if case == "normal" else None)


def test_usage_runner_and_balance_spend_are_never_added(ds, monkeypatch):
    _read(ds, "100", _ago(days=1))
    _read(ds, "80", NOW)
    monkeypatch.setattr(routing, "_telemetry_streams", lambda view: [_runner_stream(10)])
    status = _cap(ds, usd=25)
    assert status["verdict"] == "under" and status["usd"] == 20


@pytest.mark.parametrize("component", ["root", "ancestor"])
def test_usage_history_ancestor_symlinks_are_refused(ds, tmp_path, component):
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    ds.history = alias / ("state" if component == "root" else "data/state") / usage.DEEPSEEK_HISTORY_NAME
    _read(ds, "40")
    assert list(outside.iterdir()) == []
    assert _cap(ds)["verdict"] == "unknown"


def test_usage_key_ancestor_symlink_is_refused(key_env, tmp_path):
    outside = tmp_path / "private"
    outside.mkdir()
    token = outside / "token"
    token.write_text("FAKE-UNRELATED-PRIVATE-TEXT")
    token.chmod(0o600)
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    assert keychain.read_owner_only_key_file(alias / "token") is None


def test_usage_history_ancestor_symlink_cannot_read_a_valid_floor(ds, tmp_path):
    _read(ds, "100", _ago(days=1))
    _read(ds, "10", NOW)
    assert _cap(ds)["verdict"] == "over"
    alias = tmp_path / "alias"
    alias.symlink_to(ds.history.parent.parent, target_is_directory=True)
    ds.history = alias / "state" / usage.DEEPSEEK_HISTORY_NAME
    status = _cap(ds)
    assert status["verdict"] == "unknown" and status["at_least_usd"] is None


@pytest.mark.parametrize("version", [2, 3])
def test_usage_legacy_history_restarts_as_partial(ds, version):
    _read(ds, "100", _ago(days=1))
    _read(ds, "65", NOW)
    stored = json.loads(ds.history.read_text())
    stored["version"] = version
    ds.history.write_text(json.dumps(stored))
    assert _cap(ds)["verdict"] == "unknown"
    row = _read(ds, "64", NOW + timedelta(seconds=1))
    assert row["spend"]["last_7d_at_least_usd"] == 0


@pytest.mark.parametrize("cap", [0, -1])
def test_usage_nonpositive_spend_caps_are_rejected(cap):
    assert routing_pace.checked_cap({"kind": "spend", "usd": cap, "days": 7, "revision": "fake"}) is None


@pytest.mark.parametrize("target", ["ancestor", "state", "lock", "lock_fifo"])
def test_usage_history_rejects_writable_dirs_and_unsafe_locks(ds, target):
    _read(ds, "100", _ago(days=1))
    before = ds.history.read_bytes()
    lock = ds.history.with_name(usage.DEEPSEEK_LOCK_NAME)
    path = {"ancestor": ds.history.parent.parent, "state": ds.history.parent, "lock": lock,
            "lock_fifo": lock}[target]
    if target == "lock_fifo":
        lock.unlink()
        os.mkfifo(lock, 0o600)
    else:
        path.chmod(0o666 if target == "lock" else 0o777)
    with usage._deepseek_locked() as fd:
        assert fd is None
    assert ds.history.read_bytes() == before


@pytest.mark.parametrize("initial,final,cap,verdict", [
    ("100", "64.9951", 35.004, "over"),
    ("1", "0.9951", 0.004, "over"),
    ("100.00004", "65.000049", 35, "near"),
    ("1", "0.996", 0.004, "reached"),
    ("100.000000000000000001", "65", 35, "over"),
])
def test_usage_cap_compares_exact_money(ds, initial, final, cap, verdict):
    assert routing_pace.checked_cap({"kind": "spend", "usd": cap, "days": 7, "revision": "fake"})
    _read(ds, initial, _ago(days=1))
    _read(ds, final, NOW)
    assert _cap(ds, usd=cap)["verdict"] == verdict


def test_usage_floor_display_never_rounds_up(ds):
    _read(ds, "1", _ago(days=1))
    row = _read(ds, "0.994", NOW)
    assert Decimal(str(row["spend"]["last_7d_at_least_usd"])) <= Decimal("0.006")
    total = usage.deepseek_spend_total(NOW, 7, 35)
    assert Decimal(str(total["at_least_usd"])) <= Decimal("0.006")
    assert total["reason"] is None


# Owner's measured-spend contract (2026-10-22).

def test_usage_measured_topup_keeps_spend_on_both_sides(ds):
    _read(ds, "100", _ago(days=8))
    _read(ds, "100", _ago(minutes=6))
    _read(ds, "95", _ago(minutes=4))
    _read(ds, "145", _ago(minutes=2))
    row = _read(ds, "138", NOW)
    total = usage.deepseek_spend_total(NOW, 7, 35)
    assert total["status"] == "known" and total["usd"] == 12
    assert total["partial"] is False and total["topups_seen"] == 1
    assert total["topup_intervals"] == [{"from": usage._iso(_ago(minutes=4).timestamp()),
                                         "to": usage._iso(_ago(minutes=2).timestamp()),
                                         "increase_usd": "50.0", "seconds": 120.0}]
    assert total["possible_miss_seconds"] == 120
    assert row["spend"]["last_7d_usd"] == 12
    assert _cap(ds)["verdict"] == "under"
    assert routing.cap_line_for({}, _entry(), NOW) == (
        "DeepSeek: $12 spent in the last 7 days of $35 (top-ups seen: 1; "
        "possible missed spend within 120 seconds)")


@pytest.mark.parametrize("days,partial", [(8, False), (1, True)])
@pytest.mark.parametrize("purpose", ["job", "check", "orchestration"])
def test_usage_measured_under_and_partial_allow_advice(ds, monkeypatch, days, partial, purpose):
    _read(ds, "100", _ago(days=days))
    _read(ds, "100", _ago(minutes=2))
    _read(ds, "95", NOW)
    total = usage.deepseek_spend_total(NOW, 7, 35)
    assert total["status"] == ("partial" if partial else "known")
    assert total["usd"] == 5 and total["partial"] is partial
    status = _cap(ds)
    assert status["verdict"] == "under" and status["used_pct"] == 14.3
    assert status["since"] == usage._iso(_ago(days=days).timestamp())
    monkeypatch.setattr(routing, "account_id", lambda key: "fake-account")
    assert routing_advice.account_advice({}, dict(_entry(), state="open"), NOW, purpose=purpose)["verdict"] == "go"
    line = routing.cap_line_for({}, _entry(), NOW)
    assert "DeepSeek: $5 spent in the last 7 days of $35" in line
    assert ("while history fills" in line) is partial


@pytest.mark.parametrize("failure", ["failed", "stale"])
def test_usage_failed_or_stale_even_over_cap_stays_unknown_and_holds(ds, monkeypatch, failure):
    _read(ds, "100", _ago(days=8))
    _read(ds, "100", _ago(minutes=32))
    _read(ds, "10", _ago(minutes=31))
    if failure == "failed":
        ds.now = NOW
        ds.reply = _Response(b"{}", 500)
        assert usage.probe_account(dict(ACCOUNT))["ok"] is False
    assert _cap(ds)["verdict"] == "unknown"
    monkeypatch.setattr(routing, "account_id", lambda key: "fake-account")
    advice = routing_advice.account_advice({}, dict(_entry(), state="open"), NOW)
    assert advice["verdict"] == "hold_back" and advice["evidence"] == "unknown"


def test_usage_topups_are_exact_validated_and_windowed(ds):
    _read(ds, "1", _ago(days=9))
    _read(ds, "51", _ago(days=8))
    _read(ds, "50", _ago(minutes=4))
    _read(ds, "50.000000000000000001", _ago(minutes=2))
    _read(ds, "49", NOW)
    total = usage.deepseek_spend_total(NOW, 7, 35)
    assert total["topups_seen"] == 1
    assert total["topup_intervals"][0]["increase_usd"] == "1E-18"
    stored = json.loads(ds.history.read_text())
    stored["topups"][-1][2] = "-1"
    ds.history.write_text(json.dumps(stored))
    assert _cap(ds)["verdict"] == "unknown"


def test_usage_deepseek_due_within_two_minutes_on_shared_usage_poll(ds, monkeypatch):
    monkeypatch.setattr(usage, "_key", lambda account: "fake")
    cache = {"fake": {"last_attempt_at": usage._iso(_ago(seconds=60).timestamp())}}
    assert usage._due([ACCOUNT], cache, NOW, 600) == [ACCOUNT]
    assert usage._due([ACCOUNT], {"fake": {"last_attempt_at": usage._iso(_ago(seconds=59).timestamp())}}, NOW, 600) == []


def test_usage_poll_automatically_refreshes_deepseek_without_prompt_setup(ds, monkeypatch):
    from khipu import recall_prompt
    calls = []
    monkeypatch.setattr(usage, "http_quota_accounts", lambda: [dict(ACCOUNT)])
    monkeypatch.setattr(usage, "refresh_due", lambda **kwargs: calls.append(kwargs) or {"refreshed": 1})
    monkeypatch.setattr(routing, "poll_refold", lambda **kwargs: {"valid_until": None})
    monkeypatch.setattr(routing, "run_due_reset_checks", lambda **kwargs: [])
    monkeypatch.setattr(recall_prompt, "_view_snapshot", lambda routing: (None, None))
    monkeypatch.setattr(routing, "_load_view", lambda: None)
    monkeypatch.setattr(routing, "push_off", lambda moment: None)
    monkeypatch.setattr(routing, "settings", lambda: {"routing_reset_check": False})
    recall_prompt._poll_once(routing)
    assert calls == [{"accounts": [ACCOUNT], "max_age": 60, "timeout": 10}]


@pytest.mark.parametrize("spent,verdict", [(0, "under"), (29.75, "near"), (35, "reached"), (36, "over")])
@pytest.mark.parametrize("days", [1, 8])
def test_usage_measured_spend_cap_thresholds(ds, days, spent, verdict):
    _read(ds, "100", _ago(days=days))
    _read(ds, "100", _ago(minutes=2))
    _read(ds, str(100 - spent), NOW)
    status = _cap(ds)
    assert status["verdict"] == verdict
    assert status["usd"] == spent
    assert status["partial"] is (days == 1)


@pytest.mark.parametrize("corruption", ["missing", "not_list", "negative", "future", "overlap", "reordered"])
def test_usage_damaged_topup_intervals_fail_closed(ds, corruption):
    _read(ds, "100", _ago(minutes=6))
    _read(ds, "95", _ago(minutes=4))
    _read(ds, "145", _ago(minutes=2))
    _read(ds, "140", NOW)
    stored = json.loads(ds.history.read_text())
    if corruption == "missing":
        del stored["topups"]
    elif corruption == "not_list":
        stored["topups"] = {}
    elif corruption == "negative":
        stored["topups"][0][2] = -1
    elif corruption == "future":
        stored["topups"][0][1] = NOW.timestamp() + 1
    elif corruption == "overlap":
        stored["topups"][0][0] = _ago(minutes=5).timestamp()
    else:
        stored["topups"] *= 2
    ds.history.write_text(json.dumps(stored))
    assert _cap(ds)["verdict"] == "unknown"


def test_usage_late_topup_records_actual_gap_instead_of_target_cadence(ds):
    _read(ds, "100", _ago(minutes=10))
    _read(ds, "150", _ago(minutes=1))
    total = usage.deepseek_spend_total(NOW, 7, 35)
    assert total["status"] == "partial" and total["usd"] == 0
    assert total["topup_intervals"][0]["seconds"] == 540
    assert total["possible_miss_seconds"] == 540


def test_usage_poll_caps_long_source_poll_interval_for_deepseek(ds, monkeypatch):
    from khipu import recall_prompt
    monkeypatch.setattr(usage, "http_quota_accounts", lambda: [dict(ACCOUNT)])
    monkeypatch.setattr(usage, "refresh_due", lambda **kwargs: {"refreshed": 0})
    monkeypatch.setattr(routing, "poll_refold", lambda **kwargs: {"valid_until": None, "interval": 600})
    monkeypatch.setattr(routing, "push_off", lambda moment: None)
    monkeypatch.setattr(routing, "settings", lambda: {"routing_reset_check": False})
    assert recall_prompt._poll_once(routing)["interval"] == 60
