"""Synthetic regressions for /tmp/auditusage/report.md; no provider IO."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import socket
import threading

import pytest

from khipu import config, routing, routing_pace, routing_sources, usage

NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
ACCOUNT = {"harness": "codex", "label": "Fake Codex", "home": None}
DS_ACCOUNT = {"harness": "deepseek", "label": "Fake DeepSeek", "home": None}
FAKE_KEY = "fake-key-usage-audit"


def iso(at):
    return usage._iso(at.timestamp())


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(usage, "_now", lambda: NOW)
    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden in usage audit regressions")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(usage, "_command", lambda *_: None)
    config = tmp_path / "data" / "config.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"usage_accounts": [ACCOUNT]}))


def row(used=15, at=NOW, source="live", observed=None, reset=None):
    return dict(usage._blank_row(ACCOUNT), ok=True, source=source,
                probed_at=iso(at), observed_at=iso(observed) if observed else None,
                windows=[{"name": "weekly", "used_pct": used, "window_mins": 10080,
                          "resets_at": iso(reset or NOW + timedelta(days=6))}])


def active_limit():
    return {"id": "old-limit", "account": "codex|", "state": "limited", "basis": "cli",
            "scope": "all", "source": "probe", "at": NOW - timedelta(minutes=5),
            "until": NOW + timedelta(hours=2), "seq": 1, "written_by": {"kind": "probe"}}


@pytest.mark.parametrize("source", ["rollout", "last_observed"])
@pytest.mark.parametrize("observed", [None, "invalid"])
def test_unknown_observation_cannot_reopen_limit(monkeypatch, source, observed):
    cached = row(source=source)
    cached["observed_at"] = observed
    monkeypatch.setattr(usage, "_load_cache", lambda: {"codex|": cached})
    events, _ = routing_sources.usage_evidence({"codex|"})
    folded = routing._fold_account(
        {"key": "codex|", "label": "Fake Codex", "harness": "codex"},
        [active_limit(), *[dict(e, seq=i + 2) for i, e in enumerate(events)]],
        {"clears": {}, "dismissed": set(), "pauses": {}, "lifts": {}}, NOW,
        {"routing_stale_after": 3600})
    assert folded["state"] == "limited"
    assert not events


def test_group_headroom_uses_oldest_window_observation(monkeypatch):
    cached = row(source="turn", observed=NOW)
    cached["windows"][0]["observed_at"] = iso(NOW - timedelta(hours=3))
    cached["windows"].append({"name": "five_hour", "used_pct": 20,
                                "observed_at": iso(NOW), "resets_at": iso(NOW + timedelta(hours=1))})
    monkeypatch.setattr(usage, "_load_cache", lambda: {"codex|": cached})
    events, _ = routing_sources.usage_evidence({"codex|"})
    assert len(events) == 1
    assert events[0]["at"] == NOW - timedelta(hours=3)


def test_failed_live_read_preserves_newer_good_figures(monkeypatch):
    usage._save_cache({"codex|": usage._merge(None, row(80))})
    fallback = row(10, NOW + timedelta(minutes=3), "rollout", NOW - timedelta(hours=3))
    fallback.update(live_error=usage.E_TIMEOUT, note=usage.ROLLOUT_NOTE)
    monkeypatch.setattr(usage, "_now", lambda: NOW + timedelta(minutes=3))
    monkeypatch.setattr(usage, "probe_all", lambda *_a, **_kw: [fallback])
    out = usage.report(fresh=True, accounts=[ACCOUNT])["accounts"][0]
    assert out["windows"][0]["used_pct"] == 80
    assert out["source"] == "live" and out["last_good_at"] == iso(NOW)
    assert out["last_attempt_at"] == iso(NOW + timedelta(minutes=3))
    assert out["last_error"] == usage.E_TIMEOUT and out["stale"] is True


def test_codex_selects_latest_event_not_latest_file(tmp_path):
    day = tmp_path / "sessions/2026/10/22"
    day.mkdir(parents=True)
    for name, stamp, used, mtime in [("older", "11:58", 5, 100), ("newer", "11:59", 100, 99)]:
        path = day / f"rollout-{name}.jsonl"
        path.write_text(json.dumps({"timestamp": f"2026-10-22T{stamp}:00Z", "type": "event_msg",
            "payload": {"type": "token_count", "rate_limits": {"primary": {
                "used_percent": used, "window_minutes": 300, "resets_at": "2026-10-22T15:00:00Z"}}}}) + "\n")
        os.utime(path, (mtime, mtime))
    out = usage.codex_rollout(str(tmp_path))
    assert out["windows"][0]["used_pct"] == 100
    assert out["observed_at"] == "2026-10-22T11:59:00Z"


def test_claude_no_turn_does_not_consume_allowance(monkeypatch):
    frames = [{"type": "control_response", "response": {"request_id": "usage", "subtype": "success",
               "response": {"rate_limits": None, "rate_limits_available": True, "subscription_type": "max"}}}]
    monkeypatch.setattr(usage, "_command", lambda *_: "fake-no-exec")
    monkeypatch.setattr(usage, "_login_folder_there", lambda *_: True)
    monkeypatch.setattr(usage, "probe_environment", lambda *_: {})
    monkeypatch.setattr(usage, "_converse", lambda *_a, **_kw: frames)
    calls = []
    monkeypatch.setattr(usage, "_turn_due", lambda *_: calls.append("allowance") or True)
    monkeypatch.setattr(usage, "_claude_turn", lambda *_: calls.append("turn") or {"ok": True, "windows": []})
    out = usage.probe_account({"harness": "claude", "label": "Fake Claude", "home": None}, rollout=False, turn=False)
    assert calls == []
    assert out["ok"] is False and out["error"] == usage.E_NO_FIGURES


@pytest.mark.parametrize("payload", [
    {"kind": "message", "role": "user", "text": "{reason}"},
    {"kind": "run", "event": {"kind": "started", "reason": "{reason}"}},
    {"kind": "run", "event": {"kind": "failed", "reason": "unrelated failure"}, "text": "{reason}"},
])
def test_muse_quoted_quota_text_is_not_refusal(monkeypatch, tmp_path, payload):
    reason = "Subscription quota exhausted. Your usage window resets at 2026-10-23T12:00:00Z."
    path = tmp_path.joinpath(*usage.MUSE_SESSIONS, "2026/10/22/fake/session.jsonl")
    path.parent.mkdir(parents=True)
    payload = json.loads(json.dumps(payload).replace("{reason}", reason))
    path.write_text(json.dumps({"recorded_at": int(NOW.timestamp() * 1_000_000), "payload": payload}) + "\n")
    monkeypatch.setattr(usage.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(usage, "_muse_routing_limit", lambda *_: False)
    refusal = usage._muse_log_refusal()
    assert refusal is None
    assert usage._muse_limit(None, refusal, NOW) is None
    assert not usage._muse_carry({"ok": True, "windows": [], "source": "last_observed"}, None, refusal, NOW)["windows"]


@pytest.fixture
def ds_store(monkeypatch):
    start = NOW.timestamp()
    store = {"history": usage._deepseek_new(FAKE_KEY, start - 8 * 86400, 100)}
    usage._deepseek_add(store["history"], start - 20, 95)
    mutex = threading.Lock()
    @contextmanager
    def locked():
        with mutex:
            yield 42
    def read(*_):
        data = deepcopy(store["history"])
        assert usage._deepseek_valid(data, start + 100)
        return "ok", data
    def write(_, data):
        assert usage._deepseek_valid(data, start + 100)
        store["history"] = deepcopy(data)
    monkeypatch.setattr(usage, "_deepseek_key", lambda: FAKE_KEY)
    monkeypatch.setattr(usage, "_deepseek_locked", locked)
    monkeypatch.setattr(usage, "_deepseek_read", read)
    monkeypatch.setattr(usage, "_deepseek_write", write)
    monkeypatch.setattr(usage, "_deepseek_precision_days", lambda: 7)
    return store


def balance_body():
    return json.dumps({"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "90"}]}).encode()


def test_deepseek_batch_waits_for_reader_budget(monkeypatch, ds_store):
    # Scale the same production-default budget relationship, keeping the real threads.
    monkeypatch.setattr(usage, "TIMEOUT_S", .02)
    monkeypatch.setattr(usage, "GLM_TIMEOUT_S", .5)
    monkeypatch.setattr(usage, "OVERALL_GRACE_S", .02)
    release = threading.Event()
    budgets = []
    def request(_key, timeout):
        budgets.append(timeout)
        assert release.wait(2)
        return 200, balance_body()
    monkeypatch.setattr(usage, "_deepseek_request", request)
    timer = threading.Timer(.15, release.set)
    timer.start()
    try:
        out = usage.probe_all([DS_ACCOUNT], timeout=usage.TIMEOUT_S)
    finally:
        release.set()
        timer.join()
    assert out[0]["ok"] is True
    assert budgets == [.5]
    assert ds_store["history"]["last"] == [NOW.timestamp(), 90]


@pytest.mark.parametrize("fails", [False, True])
def test_cancelled_deepseek_worker_cannot_write_history(monkeypatch, ds_store, fails):
    monkeypatch.setattr(usage, "OVERALL_GRACE_S", .01)
    release, finished = threading.Event(), threading.Event()
    original = usage.probe_account
    def request(*_):
        assert release.wait(2)
        if fails:
            raise usage.ProbeError(usage.E_TIMEOUT)
        return 200, balance_body()
    def probe(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()
    monkeypatch.setattr(usage, "_deepseek_request", request)
    monkeypatch.setattr(usage, "probe_account", probe)
    before = deepcopy(ds_store["history"])
    try:
        out = usage.probe_all([DS_ACCOUNT], timeout=.02)
        assert out[0]["error"] == usage.E_TIMEOUT
    finally:
        release.set()
        assert finished.wait(2)
    assert ds_store["history"] == before


@pytest.mark.parametrize("older_fails", [True, False])
def test_deepseek_attempt_verdict_follows_request_start(monkeypatch, ds_store, older_fails):
    entered, newer_done = threading.Event(), threading.Event()
    outcomes = {}
    def request(*_):
        older = threading.current_thread().name == "audit-older"
        if older:
            entered.set()
            assert newer_done.wait(2)
        if older == older_fails:
            raise usage.ProbeError(usage.E_TIMEOUT)
        return 200, balance_body()
    def now():
        return NOW - timedelta(seconds=10 if threading.current_thread().name == "audit-older" else 5)
    def work(name):
        try:
            outcomes[name] = usage.probe_account(DS_ACCOUNT)
        finally:
            if name == "newer":
                newer_done.set()
    monkeypatch.setattr(usage, "_now", now)
    monkeypatch.setattr(usage, "_deepseek_request", request)
    older = threading.Thread(name="audit-older", target=work, args=("older",))
    newer = threading.Thread(name="audit-newer", target=work, args=("newer",))
    older.start()
    assert entered.wait(2)
    newer.start()
    older.join(3)
    newer.join(3)
    assert not older.is_alive() and not newer.is_alive()
    assert outcomes["older"]["ok"] is not older_fails
    assert outcomes["newer"]["ok"] is older_fails
    assert ds_store["history"]["attempt"] == {
        "at": (NOW - timedelta(seconds=5)).timestamp(), "ok": older_fails,
        "error": None if older_fails else usage.E_TIMEOUT}
    monkeypatch.setattr(usage, "_deepseek_state", lambda *_: ("ok", deepcopy(ds_store["history"])))
    total = usage.deepseek_spend_total(NOW, 7, 35)
    assert total["status"] == ("known" if older_fails else "unknown")
    assert total["verdict"] == ("under" if older_fails else "unknown")


def test_stale_pace_target_is_unknown():
    pace = routing_pace.compute_pace(
        [{"t": NOW - timedelta(minutes=80), "used": 80, "reset": NOW + timedelta(days=6),
          "mac": "fake", "source": "probe", "t_err": 0}],
        [{"kind": "pace_target", "pct": 100, "slack": 0, "revision": "r1"}], NOW, "weekly")
    assert pace["pace_target"]["verdict"] == "unknown"
    assert pace["pace_target"]["why"] == pace["why"]
    assert "last reading 1.3 h old" in routing_pace.pace_line("Fake", pace, NOW)
    assert "ahead of" not in routing_pace.pace_line("Fake", pace, NOW)


def test_backward_clock_is_stale_and_due(monkeypatch):
    cache = {"codex|": usage._merge(None, row(50))}
    usage._save_cache(cache)
    now = NOW - timedelta(hours=1)
    monkeypatch.setattr(usage, "_now", lambda: now)
    monkeypatch.setattr(usage, "_start_refresh", lambda *_: True)
    out = usage.report(accounts=[ACCOUNT], background=True)["accounts"][0]
    assert out["stale"] is True and out["refreshing"] is True
    assert usage._due([ACCOUNT], cache, now, 600) == [ACCOUNT]
    monkeypatch.setattr(usage, "configured_accounts", lambda config=None: [ACCOUNT])
    monkeypatch.setattr(config, "load_config", lambda: {"usage_accounts": [ACCOUNT]})
    assert usage.doctor_block(now=now)["accounts"][0]["stale"] is True


def test_passed_reset_is_unknown_and_due(monkeypatch):
    cache = {"codex|": usage._merge(None, row(100, reset=NOW + timedelta(seconds=20)))}
    usage._save_cache(cache)
    now = NOW + timedelta(seconds=30)
    monkeypatch.setattr(usage, "_now", lambda: now)
    monkeypatch.setattr(usage, "_start_refresh", lambda *_: True)
    report = usage.report(accounts=[ACCOUNT], max_age=120, background=True)
    out = report["accounts"][0]
    assert out["stale"] is True and out["refreshing"] is True
    assert out["windows"][0]["used_pct"] is None
    assert out["windows"][0]["expired"] is True
    assert "reset passed" in report["summary_text"][0]
    assert usage._due([ACCOUNT], cache, now, 600) == [ACCOUNT]
    # Expiring a presentation must preserve the original observation on disk.
    assert usage._load_cache()["codex|"]["windows"][0]["used_pct"] == 100


def test_backward_clock_does_not_reject_new_valid_read(monkeypatch):
    usage._save_cache({"codex|": usage._merge(None, row(80))})
    corrected = NOW - timedelta(hours=1)
    monkeypatch.setattr(usage, "_now", lambda: corrected)
    monkeypatch.setattr(usage, "probe_all", lambda *_a, **_kw: [row(40, at=corrected)])
    out = usage.report(fresh=True, accounts=[ACCOUNT])["accounts"][0]
    assert out["windows"][0]["used_pct"] == 40
    assert out["stale"] is False


@pytest.mark.parametrize("future_field", ["last_attempt_at", "probed_at", "window"])
def test_future_attempt_or_window_is_stale(monkeypatch, future_field):
    cached = usage._merge(None, row(50, at=NOW - timedelta(seconds=10)))
    if future_field == "window":
        cached["windows"][0]["observed_at"] = iso(NOW + timedelta(hours=1))
        cached["windows"].append({"name": "five_hour", "used_pct": 10,
                                  "observed_at": iso(NOW - timedelta(seconds=5)),
                                  "resets_at": iso(NOW + timedelta(hours=2))})
    else:
        cached[future_field] = iso(NOW + timedelta(hours=1))
        if future_field == "probed_at":
            cached.pop("last_attempt_at")
            cached["observed_at"] = iso(NOW - timedelta(seconds=10))
    usage._save_cache({"codex|": cached})
    monkeypatch.setattr(usage, "_start_refresh", lambda *_: True)
    out = usage.report(background=True, accounts=[ACCOUNT])["accounts"][0]
    assert out["stale"] is True
    assert usage._due([ACCOUNT], {"codex|": cached}, NOW, 600) == [ACCOUNT]
