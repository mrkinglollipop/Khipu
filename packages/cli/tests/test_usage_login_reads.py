"""khipu.usage reading the tools' own logins (Grok, Cursor, OpenCode Go) and
``muse usage --json``.

Hermetic: a temp HOME holds fake login files, the HTTP transport is faked
(``OpenerDirector.open``), the Keychain read is faked, and Muse is a fake
binary. No test reads a real login, the real Keychain or the network. Each
fake token is a sentinel the tests search for in every output.
"""
from __future__ import annotations

import io
import json
import logging
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import keychain, routing, routing_pace, usage, usage_logins

GROK_TOKEN = "SENTINEL-grok-login-token-4f1c.do-not-leak"
CURSOR_TOKEN = "SENTINEL-cursor-keychain-token-9a2e.do-not-leak"
OPENCODE_TOKEN = "SENTINEL-opencode-go-key-77b0.do-not-leak"
TOKENS = (GROK_TOKEN, CURSOR_TOKEN, OPENCODE_TOKEN)

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


GROK_REPLY = {"config": {"creditUsagePercent": 42.5,
                         "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "start": _z(NOW - timedelta(days=3)),
                                           "end": _z(NOW + timedelta(days=4))}}}
CURSOR_END = NOW + timedelta(days=10)
CURSOR_REPLY = {"billingCycleEnd": str(int(CURSOR_END.timestamp() * 1000)),
                "planUsage": {"totalPercentUsed": 61.0, "autoPercentUsed": 20.5, "apiPercentUsed": 80.0}}
OPENCODE_REPLY = {"usage": {"rolling": {"percent": 12, "resetsAt": _z(NOW + timedelta(hours=2))},
                            "weekly": {"percent": 125, "resetsAt": _z(NOW + timedelta(days=3))},
                            "monthly": {"percent": 30, "resetsAt": _z(NOW + timedelta(days=12))}}}


class _Response:
    def __init__(self, body: bytes, status: int = 200):
        self.status, self._body = status, io.BytesIO(body)

    def read(self, n=-1):
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _write_private(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)
    return path


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A temp HOME with Grok's and OpenCode's login files, an existing
    install's config file, a fake Cursor Keychain item and a fake network.
    ``env.seen`` holds every request; ``env.replies`` maps a host to a reply."""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(home / ".config" / "khipu"))
    for name in ("GROK_HOME", "XDG_DATA_HOME", "XAI_API_KEY", "GROK_AUTH"):
        monkeypatch.delenv(name, raising=False)
    data = home / ".config" / "khipu"
    data.mkdir(parents=True)
    # An install that already had a config file before this change.
    (data / "config.json").write_text("{}\n")
    grok_file = _write_private(home / ".grok" / "auth.json", {
        "https://auth.x.ai::b1a00492-073a-47ea-816f-4c329264a828": {"key": GROK_TOKEN, "auth_mode": "oauth",
                                                                    "email": "owner@example.invalid"}})
    opencode_file = _write_private(home / ".local" / "share" / "opencode" / "auth.json",
                                   {"opencode-go": {"type": "api", "key": OPENCODE_TOKEN}})
    seen = []

    class E:
        replies = {"cli-chat-proxy.grok.com": (GROK_REPLY, 200), "api2.cursor.sh": (CURSOR_REPLY, 200),
                   "opencode.ai": (OPENCODE_REPLY, 200)}
        keychain_reads = []

    E.seen, E.home, E.grok_file, E.opencode_file = seen, home, grok_file, opencode_file

    def opener_open(self, request, data=None, timeout=None):  # noqa: ARG001 - OpenerDirector.open stand-in
        seen.append({"url": request.full_url, "method": request.get_method(),
                     "headers": dict(request.header_items()), "body": request.data})
        host = request.full_url.split("/")[2]
        body, status = E.replies[host]
        return _Response(body if isinstance(body, bytes) else json.dumps(body).encode(), status)

    def get_password(account, *, service=keychain.SERVICE):
        E.keychain_reads.append((service, account))
        return CURSOR_TOKEN if (service, account) == ("cursor-access-token", "cursor-user") else None

    monkeypatch.setattr("urllib.request.OpenerDirector.open", opener_open)
    monkeypatch.setattr(keychain, "get_password", get_password)
    monkeypatch.setattr(usage, "_probe_grok_build", lambda *a, **k: {"ok": False, "error": usage.E_NOT_INSTALLED})
    monkeypatch.setattr(usage, "_command", lambda harness: None)
    return E


def _set(key: str, value) -> None:
    from khipu.config import mutate_config

    with mutate_config() as data:
        data[key] = value


def _cursor_on():
    _set("routing_usage_login_cursor", True)


def _probe(harness: str, **kw) -> dict:
    return usage.probe_account({"label": usage.LABELS[harness], "harness": harness, "home": None}, **kw)


def _snapshot(*paths: Path) -> list:
    return [(p.read_bytes(), p.stat().st_mtime_ns, stat.S_IMODE(p.stat().st_mode)) for p in paths]


# ---- the switches and their defaults ---------------------------------------------------


def test_existing_install_with_a_login_defaults_on_and_cursor_defaults_off(env):
    assert usage_logins.login_switch("grok") == (True, "default")
    assert usage_logins.login_switch("opencode") == (True, "default")
    assert usage_logins.login_switch("cursor") == (False, "default")
    status = routing.settings_status()
    assert status["routing_usage_login_grok"]["value"] is True
    assert status["routing_usage_login_cursor"]["value"] is False
    assert status["routing_usage_login_grok"]["login_found"] is True


def test_a_provider_with_no_login_here_defaults_off(env):
    env.opencode_file.unlink()
    assert usage_logins.login_switch("opencode") == (False, "default")


def test_a_new_install_asks_once_instead_of_reading(env):
    from khipu.config import config_file

    config_file().unlink()
    assert usage_logins.login_switch("grok") == (False, "default")
    _set("usage_auto_track", True)  # Khipu creates the config file: a new install
    assert config_file().with_name(usage_logins.LOGIN_ASK_NAME).exists()
    assert usage_logins.login_switch("grok") == (False, "default")
    assert routing.settings_status()["routing_usage_login_grok"]["ask"] is True
    routing.set_setting("routing_usage_login_grok", "on")
    assert usage_logins.login_switch("grok") == (True, "file")
    assert routing.settings_status()["routing_usage_login_grok"]["ask"] is False


def test_a_conversations_waiting_change_never_turns_a_switch_on(env):
    routing.set_setting("routing_usage_login_cursor", "on", request={"request": "turn it on", "by": None, "at": None})
    assert usage_logins.login_switch("cursor") == (False, "default")


@pytest.mark.parametrize("harness, key", [("grok", "routing_usage_login_grok"),
                                          ("cursor", "routing_usage_login_cursor"),
                                          ("opencode", "routing_usage_login_opencode")])
def test_switch_off_sends_zero_requests_and_reads_no_login(env, harness, key):
    _set(key, False)
    _probe(harness)
    assert env.seen == []
    assert env.keychain_reads == []


def test_switch_off_leaves_opencode_out_and_cursor_is_never_an_account(env):
    _set("routing_usage_login_opencode", False)
    harnesses = [a["harness"] for a in usage.all_accounts()]
    assert "cursor" not in harnesses and "opencode" not in harnesses
    _cursor_on()
    _set("routing_usage_login_opencode", True)
    harnesses = [a["harness"] for a in usage.all_accounts()]
    # Cursor's reading is a second source for the Grok account, never a row.
    assert "cursor" not in harnesses and "opencode" in harnesses


# ---- the reads ---------------------------------------------------------------------


def test_grok_reads_its_credits_period_with_a_length_for_pace(env):
    row = _probe("grok")
    assert row["ok"] is True
    (window,) = row["windows"]
    assert window["name"] == "weekly" and window["used_pct"] == 42.5 and window["window_mins"] == 7 * 1440
    (request,) = env.seen
    assert request["url"] == "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
    assert request["method"] == "GET" and request["headers"]["Authorization"] == f"Bearer {GROK_TOKEN}"


GROK_BUILD_ROW = {"ok": True, "plan": None, "windows": [{"name": "weekly", "used_pct": 10, "resets_at": None}],
                  "credits": None, "version": None, "source": "live"}


@pytest.mark.parametrize("config", [{}, {"creditUsagePercent": None}])
def test_grok_with_no_percent_is_a_failed_read_that_falls_back(env, monkeypatch, config):
    period = GROK_REPLY["config"]["currentPeriod"]
    env.replies["cli-chat-proxy.grok.com"] = ({"config": dict(config, currentPeriod=period)}, 200)
    with pytest.raises(usage.ProbeError):
        usage_logins.parse_grok_billing(200, json.dumps({"config": dict(config, currentPeriod=period)}).encode())
    monkeypatch.setattr(usage, "_probe_grok_build", lambda *a, **k: dict(GROK_BUILD_ROW))
    row = _probe("grok")
    assert len(env.seen) == 1
    assert row["ok"] is True and row["windows"][0]["used_pct"] == 10  # Grok Build's, never a synthetic 0%


HUGE = int("9" * 400)


@pytest.mark.parametrize("value", [HUGE, -HUGE, 10**7])
def test_grok_hostile_percent_is_rejected_and_falls_back(env, monkeypatch, value):
    env.replies["cli-chat-proxy.grok.com"] = (
        json.dumps({"config": dict(GROK_REPLY["config"], creditUsagePercent=value)}).encode(), 200)
    monkeypatch.setattr(usage, "_probe_grok_build", lambda *a, **k: dict(GROK_BUILD_ROW))
    row = usage.probe_grok(None)
    assert row["ok"] is True and row["windows"][0]["used_pct"] == 10


@pytest.mark.parametrize("harness, host, body", [
    ("cursor", "api2.cursor.sh", {"billingCycleEnd": str(HUGE), "planUsage": {"totalPercentUsed": HUGE}}),
    ("cursor", "api2.cursor.sh", {"billingCycleEnd": HUGE, "planUsage": {"totalPercentUsed": 20}}),
    ("cursor", "api2.cursor.sh", {"billingCycleEnd": HUGE, "billingCycleStart": -HUGE,
                                  "planUsage": {"totalPercentUsed": 20}}),
    ("opencode", "opencode.ai", {"usage": {"rolling": {"percent": HUGE, "resetsAt": HUGE}}}),
    ("opencode", "opencode.ai", {"usage": {"weekly": {"percent": 5, "resetsAt": HUGE}}}),
])
def test_cursor_and_opencode_hostile_numbers_never_raise(env, harness, host, body):
    _cursor_on()
    if harness == "cursor":
        _set("routing_usage_login_grok", False)
    env.replies[host] = (json.dumps(body).encode(), 200)
    row = _probe("grok" if harness == "cursor" else harness)
    assert isinstance(row, dict) and len(env.seen) == 1
    if row.get("ok"):
        assert all(0 <= w["used_pct"] <= 100 for w in row["windows"])
    elif harness == "cursor":
        assert row["error"] == usage.E_NOT_INSTALLED and "via" not in row  # Grok's own (Grok Build) stands
    else:
        assert row["error"] in (usage.E_BAD, usage.E_NO_PLAN)


@pytest.mark.parametrize("variable", ["XAI_API_KEY", "GROK_OIDC_ISSUER"])
def test_grok_signed_in_another_way_is_not_read_from_its_file(env, monkeypatch, variable):
    monkeypatch.setenv(variable, "x")
    _probe("grok")
    assert env.seen == []


def test_grok_config_with_its_own_auth_section_is_not_read(env):
    (env.home / ".grok" / "config.toml").write_text("[endpoints]\napi = 'x'\n")
    _probe("grok")
    assert env.seen == []


def test_grok_api_key_mode_is_not_read(env):
    _write_private(env.grok_file, {"https://accounts.x.ai/sign-in": {"key": GROK_TOKEN, "auth_mode": "api_key"}})
    _probe("grok")
    assert env.seen == []


def test_cursor_parses_its_cycle_and_two_pools(env):
    row = usage_logins.parse_cursor(200, json.dumps(CURSOR_REPLY).encode())
    assert [(w["name"], w["used_pct"], w.get("model")) for w in row["windows"]] == [
        ("monthly", 61.0, None), ("monthly_cursor_models", 20.5, "Cursor models"),
        ("monthly_other_models", 80.0, "Other models")]
    assert all(w["resets_at"] == _z(CURSOR_END) and w["window_mins"] > 27 * 1440 for w in row["windows"])


def test_cursor_posts_to_its_dashboard_with_the_keychain_login_for_the_grok_row(env):
    _cursor_on()
    _set("routing_usage_login_grok", False)
    row = _probe("grok")
    assert row["ok"] is True and row["harness"] == "grok" and row["via"] == "cursor"
    # The overall figure only: the same limit, never added to Grok's.
    assert [(w["name"], w["used_pct"]) for w in row["windows"]] == [("monthly", 61.0)]
    (request,) = env.seen
    assert request["url"] == usage_logins.CURSOR_USAGE_URL and request["method"] == "POST" and request["body"] == b"{}"
    assert request["headers"]["Authorization"] == f"Bearer {CURSOR_TOKEN}"
    assert request["headers"]["Connect-protocol-version"] == "1"
    assert env.keychain_reads == [("cursor-access-token", "cursor-user")]


def test_opencode_reads_three_windows_clamped_to_100(env):
    row = _probe("opencode")
    assert [(w["name"], w["used_pct"], w["window_mins"]) for w in row["windows"]][:2] == [
        ("five_hour", 12.0, 300), ("weekly", 100.0, 10080)]
    assert row["windows"][2]["name"] == "monthly" and row["windows"][2]["window_mins"] > 27 * 1440
    (request,) = env.seen
    assert request["url"] == "https://opencode.ai/zen/go/v1/usage"
    assert request["headers"]["Authorization"] == f"Bearer {OPENCODE_TOKEN}"


def test_opencode_403_is_no_go_plan(env):
    env.replies["opencode.ai"] = (b"", 403)
    row = _probe("opencode")
    assert row["ok"] is False and row["error"] == usage.E_NO_PLAN


# ---- read-only: a rejected login is "can't read", never a refresh -----------------------


def test_401_says_cant_read_retries_nothing_and_touches_no_file(env):
    env.replies["opencode.ai"] = (b"", 401)
    before = _snapshot(env.grok_file, env.opencode_file)
    row = _probe("opencode")
    assert row["ok"] is False and row["error"] == usage.E_BAD_KEY
    assert row["note"].startswith("Khipu can't read")
    assert len(env.seen) == 1
    assert _snapshot(env.grok_file, env.opencode_file) == before


def test_grok_401_falls_back_to_grok_build_and_touches_no_file(env, monkeypatch):
    env.replies["cli-chat-proxy.grok.com"] = (b"", 401)
    fallback = {"ok": True, "plan": None, "windows": [{"name": "weekly", "used_pct": 10, "resets_at": None}],
                "credits": None, "version": None, "source": "live"}
    monkeypatch.setattr(usage, "_probe_grok_build", lambda *a, **k: dict(fallback))
    before = _snapshot(env.grok_file)
    row = _probe("grok")
    assert row["ok"] is True and row["windows"][0]["used_pct"] == 10
    assert len(env.seen) == 1
    assert _snapshot(env.grok_file) == before


def test_a_failed_read_after_a_good_one_is_never_shown_open(env, monkeypatch):
    accounts = [{"label": "OpenCode Go", "harness": "opencode", "home": None}]
    usage.report(fresh=True, accounts=accounts)
    env.replies["opencode.ai"] = (b"", 500)
    monkeypatch.setattr(usage, "_login_read_young", lambda *a: False)
    monkeypatch.setattr(usage_logins, "login_read_due", lambda *a, **k: True)
    (row,) = usage.report(fresh=True, accounts=accounts)["accounts"]
    assert row["ok"] is False and row["stale"] is True
    assert [w["used_pct"] for w in row["windows"]] == [12.0, 100.0, 30.0]  # the last figures, marked stale


def test_no_login_file_is_a_clear_row_and_no_request(env):
    env.opencode_file.unlink()
    _set("routing_usage_login_opencode", True)
    row = _probe("opencode")
    assert row["ok"] is False and row["error"] == usage.E_NO_KEY and env.seen == []


def test_a_login_file_others_can_write_is_not_read(env):
    os.chmod(env.opencode_file, 0o666)
    row = _probe("opencode")
    assert row["error"] == usage.E_NO_KEY and env.seen == []


# ---- cadence ---------------------------------------------------------------------


def test_one_login_read_per_five_minutes_whatever_asks(env):
    accounts = [{"label": "OpenCode Go", "harness": "opencode", "home": None}]
    usage.report(fresh=True, accounts=accounts)
    usage.report(fresh=True, accounts=accounts)
    usage.refresh_due(max_age=0, accounts=accounts)
    assert len(env.seen) == 1
    _probe("grok")
    _probe("grok")
    assert len(env.seen) == 2  # the second Grok read is Grok Build's


def test_two_callers_racing_the_cadence_send_one_request(env, monkeypatch):
    import threading
    import time as real_time
    import types

    # Each caller pauses right after reading the clock for its stamp check
    # until the other has too (or 0.5 s pass: a lock keeps the other out).
    barrier, local = threading.Barrier(2, timeout=0.5), threading.local()

    def paused_time():
        now = real_time.time()
        if not getattr(local, "paused", False):
            local.paused = True
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return now

    monkeypatch.setattr(usage_logins, "time", types.SimpleNamespace(time=paused_time))
    rows = []
    threads = [threading.Thread(target=lambda: rows.append(_probe("opencode"))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(env.seen) == 1
    assert sorted(bool(r.get("ok")) for r in rows) == [False, True]


# ---- pace --------------------------------------------------------------------------


@pytest.mark.parametrize("harness", ["grok", "grok via cursor", "opencode"])
def test_every_window_has_a_length_and_reset_the_pace_engine_takes(env, harness):
    _cursor_on()
    if harness == "grok via cursor":
        _set("routing_usage_login_grok", False)
    row = _probe(harness.split()[0])
    assert row["windows"]
    for window in row["windows"]:
        reset = usage._parse_time(window["resets_at"])
        assert routing_pace.window_params(window["name"], window["window_mins"] * 60, reset) is not None
        assert usage.even_pace(window, datetime.now(timezone.utc)) is not None


def test_opencode_report_rows_carry_their_pace(env):
    rep = usage.report(fresh=True, accounts=[{"label": "OpenCode Go", "harness": "opencode", "home": None}])
    for row in rep["accounts"]:
        assert row["ok"] is True
        assert all(w["pace"]["words"] for w in row["windows"])
    public = usage.public_report(rep)
    assert [r["harness"] for r in public["accounts"]] == ["opencode"]
    assert all("pace" in w for r in public["accounts"] for w in r["windows"])


def test_routing_sees_cursor_only_as_the_grok_account(env):
    from khipu import routing_sources

    _cursor_on()
    _set("routing_usage_login_grok", False)
    rep = usage.report(fresh=True, accounts=[{"label": "Grok", "harness": "grok", "home": None}])
    (row,) = rep["accounts"]
    assert row["via"] == "cursor" and usage.public_report(rep)["accounts"][0]["via"] == "cursor"
    items, _status = routing_sources.usage_evidence(None)
    assert not [i for i in items if i["account"].startswith("cursor")]


# ---- the token never leaves its request -------------------------------------------------


def test_no_token_in_any_output_log_exception_report_or_payload(env, caplog, capsys, monkeypatch):
    caplog.set_level(logging.DEBUG)
    _cursor_on()
    accounts = [{"label": "Grok", "harness": "grok", "home": None},
                {"label": "OpenCode Go", "harness": "opencode", "home": None}]
    texts = []
    rep = usage.report(fresh=True, accounts=accounts)
    texts += [json.dumps(rep, default=str), json.dumps(usage.public_report(rep), default=str)]
    for host in env.replies:
        env.replies[host] = (b"", 401)
    monkeypatch.setattr(usage, "_login_read_young", lambda *a: False)
    monkeypatch.setattr(usage_logins, "login_read_due", lambda *a, **k: True)
    rep = usage.report(fresh=True, accounts=accounts)
    texts += [json.dumps(rep, default=str), json.dumps(usage.public_report(rep), default=str)]
    texts.append(json.dumps(routing.settings_status(), default=str))
    texts.append(json.dumps(usage_logins.login_switches_status(), default=str))
    for parse in (usage_logins.parse_grok_billing, usage_logins.parse_cursor, usage_logins.parse_opencode):
        for status, body in ((200, b"not json " + GROK_TOKEN.encode()), (500, b""), (302, b"")):
            try:
                parse(status, body)
            except usage.ProbeError as exc:
                texts += [str(exc), repr(exc)]
    out = capsys.readouterr()
    texts += [out.out, out.err, caplog.text]
    for data in (usage.cache_file().parent, usage_logins.login_state_dir()):
        texts += [p.read_text(errors="replace") for p in data.rglob("*") if p.is_file()]
    blob = "\n".join(texts)
    for token in TOKENS:
        assert token not in blob


# ---- Cursor: a second source for the Grok account (Matt 2026-10-29) -------------------------
#
# One xAI Ultra weekly limit covers Grok Bot, Grok Build and Cursor, so
# Cursor's reading is the Grok account's, never a row or account of its own.

GROK_START, GROK_END = NOW - timedelta(days=3), NOW + timedelta(days=4)


def _cursor_reply(pct, end=GROK_END, start=GROK_START):
    reply = {"billingCycleEnd": str(int(end.timestamp() * 1000)), "planUsage": {"totalPercentUsed": pct}}
    if start is not None:
        reply["billingCycleStart"] = str(int(start.timestamp() * 1000))
    return reply


def _grok_build_row(pct, *, age_s=0):
    row = {"ok": True, "plan": None, "windows": [{"name": "weekly", "used_pct": pct, "resets_at": _z(GROK_END),
                                                   "window_mins": 10080}],
           "credits": None, "version": None, "source": "live"}
    if age_s:
        row.update(source="last_observed", observed_at=_z(datetime.now(timezone.utc) - timedelta(seconds=age_s)))
    return row


def test_cursor_off_reads_no_keychain_and_leaves_grok_alone(env):
    row = _probe("grok")
    assert row["windows"][0]["used_pct"] == 42.5 and "via" not in row
    assert env.keychain_reads == [] and [r["url"].split("/")[2] for r in env.seen] == ["cli-chat-proxy.grok.com"]


def test_login_false_never_reads_cursor(env):
    _cursor_on()
    _probe("grok", login=False)
    assert env.keychain_reads == [] and env.seen == []


def test_cursor_feeds_grok_when_grok_read_fails(env):
    _cursor_on()
    env.replies["cli-chat-proxy.grok.com"] = (b"", 401)
    env.replies["api2.cursor.sh"] = (_cursor_reply(30), 200)
    row = _probe("grok")
    assert row["ok"] is True and row["via"] == "cursor"
    # Its cycle is seven days with no Grok window to match: Grok's weekly.
    assert [(w["name"], w["used_pct"], w["window_mins"]) for w in row["windows"]] == [("weekly", 30.0, 10080)]


def test_agreeing_readings_keep_grok_own_and_never_add(env):
    _cursor_on()
    env.replies["api2.cursor.sh"] = (_cursor_reply(44), 200)
    row = _probe("grok")
    assert len(env.seen) == 2 and "via" not in row and not row.get("source_note")
    assert [(w["name"], w["used_pct"]) for w in row["windows"]] == [("weekly", 42.5)]


def test_disagreeing_readings_keep_the_fresher_and_say_so(env, monkeypatch):
    _cursor_on()
    _set("routing_usage_login_grok", False)
    monkeypatch.setattr(usage, "_probe_grok_build", lambda *a, **k: _grok_build_row(10, age_s=1200))
    env.replies["api2.cursor.sh"] = (_cursor_reply(61), 200)
    row = _probe("grok")
    assert row["via"] == "cursor"
    assert [(w["name"], w["used_pct"], w["window_mins"]) for w in row["windows"]] == [("weekly", 61.0, 10080)]
    assert "61%" in row["source_note"] and "10%" in row["source_note"] and "Cursor" in row["source_note"]


def test_a_saved_cursor_reading_older_than_grok_own_loses_and_is_noted(env):
    _cursor_on()
    _set("routing_usage_login_grok", False)
    env.replies["api2.cursor.sh"] = (_cursor_reply(61), 200)
    assert _probe("grok")["via"] == "cursor"  # read and kept; Grok Build is not installed
    _set("routing_usage_login_grok", True)
    row = _probe("grok")  # Cursor not due again: its saved reading competes
    assert [r["url"].split("/")[2] for r in env.seen] == ["api2.cursor.sh", "cli-chat-proxy.grok.com"]
    assert "via" not in row and row["windows"][0]["used_pct"] == 42.5
    assert "61%" in row["source_note"] and "42.5%" in row["source_note"]


def test_a_saved_cursor_reading_expires(env, monkeypatch):
    _cursor_on()
    _set("routing_usage_login_grok", False)
    env.replies["api2.cursor.sh"] = (_cursor_reply(61), 200)
    _probe("grok")
    monkeypatch.setattr(usage_logins, "CURSOR_KEEP_S", -1)
    row = _probe("grok")
    assert row["ok"] is False and "via" not in row


def test_a_cycle_that_does_not_line_up_keeps_its_own_name_and_says_so(env):
    _cursor_on()
    row = _probe("grok")  # CURSOR_REPLY: a cycle ending in ten days, Grok's week in four
    assert "via" not in row and [w["name"] for w in row["windows"]] == ["weekly"]
    assert "line up" in row["source_note"]


def test_the_login_read_lock_is_host_local_and_never_waited_on(env, monkeypatch, tmp_path):
    import fcntl
    import threading

    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "synced"))
    lock = usage_logins.login_state_dir() / "usage.login-opencode.stamp.lock"
    assert usage_logins.login_state_dir().is_relative_to(Path.home() / "Library" / "Caches")
    assert not lock.is_relative_to(usage.cache_file().parent)
    lock.parent.mkdir(parents=True, exist_ok=True)
    holder = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)  # a live holder, suspended
    try:
        result = []
        worker = threading.Thread(target=lambda: result.append(usage_logins.login_read_due("opencode")), daemon=True)
        worker.start()
        worker.join(2)
        assert result == [False]  # busy: this cycle is skipped, never waited out
    finally:
        os.close(holder)
    assert usage_logins.login_read_due("opencode") is True


# ---- Muse: `muse usage --json` first, from 1.4.13 ------------------------------------------

MUSE_CLI = {"schemaVersion": 1, "allowance": {"asOfMs": int(NOW.timestamp() * 1000), "tier": "power",
                                              "window": {"usedPercent": 33, "resetsAtMs": int((NOW + timedelta(hours=3)).timestamp() * 1000)},
                                              "weekly": {"usedPercent": 51, "resetsAtMs": int((NOW + timedelta(days=4)).timestamp() * 1000)}}}


@pytest.fixture
def muse(monkeypatch, tmp_path):
    calls = []

    class M:
        version = "1.4.13 (1.4.13-R7000.1)"
        usage_out = json.dumps(MUSE_CLI)

    def run(binary, args, env):  # noqa: ARG001
        calls.append(args)
        return M.version if args == ["--version"] else M.usage_out

    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "khipu"))
    monkeypatch.setattr(usage, "_muse_run", run)
    monkeypatch.setattr(usage, "_command", lambda harness: "/fake/muse")
    M.calls = calls
    return M


def test_muse_1_4_13_reads_its_allowance_from_the_cli_with_no_turn(muse, monkeypatch):
    monkeypatch.setattr(usage, "_converse", lambda *a, **k: pytest.fail("no MSP host when the CLI answered"))
    row = usage.probe_muse()
    assert row["ok"] is True and row["plan"] == "power" and row["version"] == "1.4.13"
    assert [(w["name"], w["used_pct"], w["window_mins"]) for w in row["windows"]] == [
        ("five_hour", 33.0, 300), ("weekly", 51.0, 10080)]
    assert muse.calls == [["--version"], ["usage", "--json"]]


def test_muse_1_4_10_keeps_the_turn_path(muse, monkeypatch):
    muse.version = "1.4.10 (1.4.10-R6427.1)"
    reached = []
    monkeypatch.setattr(usage, "_converse", lambda *a, **k: reached.append(1) or [])
    usage.probe_muse(turn=False)
    assert reached and muse.calls == [["--version"]]


@pytest.mark.parametrize("out", [
    json.dumps({"schemaVersion": 1, "allowance": None, "reason": "server_sent_no_usage"}),
    json.dumps({"schemaVersion": 2, "allowance": MUSE_CLI["allowance"]}),
    json.dumps({"schemaVersion": 1, "allowance": {"window": "full"}}),
    "not json", None])
def test_muse_cli_unknown_shapes_fall_back(muse, monkeypatch, out):
    muse.usage_out = out
    reached = []
    monkeypatch.setattr(usage, "_converse", lambda *a, **k: reached.append(1) or [])
    usage.probe_muse(turn=False)
    assert reached


def test_only_the_login_module_names_a_login_file():
    """usage.py still reads no credential by name (test_usage's trap); the
    token readers are confined to usage_logins."""
    for name in ("usage.py", "routing_sources.py", "config.py"):
        text = (Path(usage.__file__).parent / name).read_text()
        assert "auth.json" not in text and "cursor-access-token" not in text, name
