"""Routing freshness (2026-10-17): every conversation's routing lines carry
current plan figures and the owner's priority.

1. The usage cache is kept fresh without anyone asking: the five-minute
   queue-drain job reads what is older than five minutes, and the per-prompt
   routing read starts one detached read when it is older than ten, never
   waiting on it; both single-flight on the shared refresh lock.
2. Figures older than fifteen minutes are shown as possibly out of date, with
   no pace or verdict; a reset time already passed says the figures are out
   of date.
3. The owner's priority too long for a push arrives shortened, never as a
   pointer alone.
4. Grok's billing read waits for ``initialize`` to answer.

Hermetic: a data dir per test, a fake clock, fake probes; no CLI is started.
"""
# ruff: noqa: F811
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import timedelta

import pytest

from khipu import routing, usage
# The fixture ``home`` is the priority tests' own (a data dir, the figures, a fake clock).
from tests.test_routing_priority import FIGURES, T0, _cache, _row, _set, _summary, _week, home  # noqa: F401

# ---- 2. stale figures ------------------------------------------------------------------------


def _codex_line(text: str) -> str:
    return next(x for x in text.splitlines() if x.startswith("Codex"))


def test_figures_82_minutes_old_say_out_of_date_and_give_no_pace(home):
    old = routing._iso(T0 - timedelta(minutes=82))
    rows = dict(FIGURES, **{"codex|": dict(_row("Codex", "codex", None, _week(74, T0 + timedelta(days=4))),
                                           probed_at=old, last_good_at=old)})
    _cache(home, rows)
    fact = routing.plan_fact(rows["codex|"], T0)
    assert fact["stale"] is True and fact["pace_pct_per_hour"] is None and fact["hurry"] is False
    line = _codex_line(_summary(home))
    assert "26% of its week left when read 1 h 22 min ago" in line
    assert "may be out of date" in line
    assert "% an hour" not in line and "plenty of time" not in line and "little time" not in line
    # The fresh accounts keep their pace, and the pace ordering leaves the stale one last.
    text = _summary(home)
    assert "about 1.7% an hour" in text
    order = [x.split(":")[0] for x in text.splitlines() if x.split(":")[0] in ("Claude", "Claude second", "Codex")]
    assert order[-1] == "Codex"
    # The priority's own facts say the same.
    routing.set_priority([{"account": "Codex"}], via="app", expect=routing.priority_version())
    prio = next(x for x in _summary(home).splitlines() if x.startswith("Your priority"))
    assert "74% of its week used when read 1 h 22 min ago, may be out of date" in prio


def test_figures_inside_fifteen_minutes_keep_their_pace(home):
    recent = routing._iso(T0 - timedelta(minutes=14))
    row = dict(_row("Codex", "codex", None, _week(8, T0 + timedelta(days=6, hours=20))), probed_at=recent)
    fact = routing.plan_fact(row, T0)
    assert not fact.get("stale") and fact["words"] == "plenty of time"
    assert "when read" not in routing.pace_words(fact, T0)


def test_a_reset_time_already_passed_says_the_figures_are_out_of_date(home):
    # The live case: Codex read with its week resetting Thu; the time passed
    # (or it reset early) and nothing has read it since.
    row = _row("Codex", "codex", None, _week(74, T0 - timedelta(minutes=5)))
    fact = routing.plan_fact(row, T0)
    assert fact == {"readable": False, "why": routing.RESET_PASSED_WHY, "reset_passed": True}
    assert routing.pace_words(fact, T0) == "its reset time passed; figures out of date"
    # A shorter window still in force is no stand-in for the week.
    row = _row("Claude", "claude", None, {"name": "five_hour", "used_pct": 30, "window_mins": 300,
                                          "resets_at": routing._iso(T0 + timedelta(hours=1))},
               _week(95, T0 - timedelta(minutes=5)))
    assert routing.plan_fact(row, T0)["why"] == routing.RESET_PASSED_WHY
    _cache(home, dict(FIGURES, **{"codex|": _row("Codex", "codex", None, _week(74, T0 - timedelta(minutes=5)))}))
    line = _codex_line(_summary(home))
    assert line.endswith("; its reset time passed; figures out of date") and "% an hour" not in line


# ---- 3. the owner's priority, shortened --------------------------------------------------------

# The owner's priority as he set it on 2026-10-17 21:26Z, word for word.
REAL_PRIORITY = [
    {"account": "Claude second", "role": "First for all Claude work: orchestration, builds, fixes, designs, mocks, "
                                         "and reviews of Codex-written work."},
    {"account": "Codex", "role": "Normal priority: cross-family reviews and re-checks of Claude-built work, read-only "
                                 "surveys. Sol at medium effort by default; Astra only when Matt asks or a miss would "
                                 "be costly. Not the default builder."},
    {"account": "Muse", "role": "Small weekly allowance: second opinions on the riskiest work only."},
    {"account": "Grok", "role": "Paused by Matt until he says otherwise (see the hold)."},
    {"account": "Claude", "role": "Main account: not used at all until Matt says otherwise."},
]


def _five_accounts(home) -> None:
    _set(home, usage_accounts=[{"label": "Claude", "harness": "claude"},
                               {"label": "Claude second", "harness": "claude", "home": "/logins/claude-second"},
                               {"label": "Codex", "harness": "codex"}, {"label": "Grok", "harness": "grok"},
                               {"label": "Muse", "harness": "muse"}])


def _body(text: str) -> str:
    return text.split("\n", 1)[1] if "\n" in text else ""


def test_the_owners_real_priority_arrives_shortened_not_as_a_pointer(home):
    _five_accounts(home)
    _set(home, routing_prompt_chars=400)
    start = routing.read_view()
    routing.set_priority(REAL_PRIORITY, via="app", expect=routing.priority_version())
    view = routing.read_view()
    changed = routing.since(start["version"], view, now=home["clock"].now)
    text, _count = routing.changes_text(changed, view, home["clock"].now)
    body = _body(text)
    assert len(body) <= 400, body
    assert routing.OWNER_PRIORITY_POINTER not in body
    line = next(x for x in body.splitlines() if x.startswith("Your priority"))
    assert "shortened; the full words: `khipu_route`" in line
    for i, label in enumerate(["Claude second", "Codex", "Muse", "Grok", "Claude"], 1):
        assert f"{i}. {label}:" in line
    assert "1. Claude second: First for all Claude work" in line and "…" in line
    # With an account's news too, the news keeps its place and the priority still shows.
    routing.report(state="limited", account="Codex", until="2h",
                   author={"kind": "conversation", "harness": "codex", "project": "p", "session": "s"})
    later = routing.read_view()
    changed = routing.since(view["version"], later, now=home["clock"].now)
    body = _body(routing.changes_text(changed, later, home["clock"].now)[0])
    assert len(body) <= 400, body
    assert any(x.startswith("Codex: limit reached") for x in body.splitlines())
    assert "1. Claude second" in body and "5. Claude" in body
    assert routing.OWNER_PRIORITY_POINTER not in body


def test_the_order_alone_when_even_the_short_roles_do_not_fit(home):
    _five_accounts(home)
    _set(home, routing_prompt_chars=170)
    start = routing.read_view()
    routing.set_priority(REAL_PRIORITY, via="app", expect=routing.priority_version())
    view = routing.read_view()
    changed = routing.since(start["version"], view, now=home["clock"].now)
    body = _body(routing.changes_text(changed, view, home["clock"].now)[0])
    assert len(body) <= 170, body
    assert body.startswith("Your priority order (guidance, not a gate; roles: `khipu_route`): 1. Claude second; "
                           "2. Codex; 3. Muse; 4. Grok; 5. Claude."), body


def test_short_words_cut_at_a_word():
    assert routing._short_words("Main account: not used at all until Matt says otherwise.", 32) == \
        "Main account: not used at all…"
    assert routing._short_words("short", 20) == "short"


# ---- 1. keeping the cache fresh ---------------------------------------------------------------

ACCOUNTS = [{"label": "Claude", "harness": "claude", "home": None},
            {"label": "Codex", "harness": "codex", "home": None}]


@pytest.fixture
def cache(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "state").mkdir(parents=True)
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setattr(usage, "configured_accounts", lambda config=None: [dict(a) for a in ACCOUNTS])
    # The cache keeps only rows for the accounts the config lists.
    monkeypatch.setattr("khipu.routing_sources.configured_keys", lambda _cfg: {usage._key(a) for a in ACCOUNTS})
    return data


def _probed(minutes_ago: float) -> dict:
    at = usage._iso(time.time() - minutes_ago * 60)
    return {usage._key(a): {"harness": a["harness"], "account": a["label"], "ok": True, "windows": [],
                            "probed_at": at, "last_attempt_at": at, "last_good_at": at} for a in ACCOUNTS}


def _fake_rows(accounts):
    now = usage._iso(time.time())
    return [{"harness": a["harness"], "account": a["label"], "home": None, "ok": True, "error": None,
             "windows": [], "probed_at": now} for a in accounts]


def test_the_periodic_job_refreshes_a_stale_cache_once_under_concurrency(cache, monkeypatch):
    usage._save_cache(_probed(82))
    calls = []
    gate = threading.Event()

    def probe_all(accounts, *, timeout):
        calls.append([a["label"] for a in accounts])
        gate.wait(5)
        return _fake_rows(accounts)

    monkeypatch.setattr(usage, "probe_all", probe_all)
    results = []
    threads = [threading.Thread(target=lambda: results.append(usage.refresh_due())) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    gate.set()
    for t in threads:
        t.join(10)
    assert calls == [["Claude", "Codex"]]
    assert sorted(r["skipped"] or "ran" for r in results) == ["busy", "busy", "busy", "ran"]
    # Done: the cache is fresh now, and the next run reads nothing.
    assert usage.refresh_due() == {"refreshed": 0, "skipped": "fresh"}
    assert len(calls) == 1


def test_the_periodic_refresh_reads_only_what_is_older_than_five_minutes(cache, monkeypatch):
    rows = _probed(3)
    rows["codex|"]["last_attempt_at"] = usage._iso(time.time() - 6 * 60)
    usage._save_cache(rows)
    calls = []
    monkeypatch.setattr(usage, "probe_all", lambda accounts, *, timeout: calls.append(
        [a["label"] for a in accounts]) or _fake_rows(accounts))
    assert usage.refresh_due() == {"refreshed": 1, "skipped": None}
    assert calls == [["Codex"]]


def test_a_refresh_that_finished_while_waiting_for_the_lock_is_not_repeated(cache, monkeypatch):
    usage._save_cache(_probed(82))
    real_lock = usage._try_refresh_lock

    def lock_after_another_refresh():
        usage._save_cache(_probed(0))  # another process's refresh landed meanwhile
        return real_lock()

    monkeypatch.setattr(usage, "_try_refresh_lock", lock_after_another_refresh)
    monkeypatch.setattr(usage, "probe_all", lambda *_a, **_k: pytest.fail("read twice"))
    assert usage.refresh_due() == {"refreshed": 0, "skipped": "fresh"}


def test_the_drain_refreshes_only_while_the_routing_push_is_on(cache, monkeypatch):
    usage._save_cache(_probed(82))
    calls = []
    monkeypatch.setattr(usage, "probe_all", lambda accounts, *, timeout: calls.append(1) or _fake_rows(accounts))
    from khipu import features

    monkeypatch.setattr(features, "enabled", lambda name, **_kw: False)
    assert routing.refresh_plan_figures()["skipped"] == "routing push off" and calls == []
    monkeypatch.setattr(features, "enabled", lambda name, **_kw: name == "routing_push")
    assert routing.refresh_plan_figures() == {"refreshed": 2, "skipped": None} and calls == [1]


def test_the_drain_job_runs_the_refresh(cache, monkeypatch, capsys):
    from khipu import cli, session_capture

    monkeypatch.setattr(routing, "refresh_plan_figures", lambda: {"refreshed": 2, "skipped": None})
    monkeypatch.setattr(session_capture, "drain", lambda **_kw: {"failed": 0})
    monkeypatch.setattr("khipu.promote.kick_queued_retry", lambda: None)
    assert cli.main(["sessions", "drain"]) == 0
    assert json.loads(capsys.readouterr().out)["plan_usage"] == {"refreshed": 2, "skipped": None}


def test_the_prompt_kick_starts_one_detached_refresh_and_never_waits(cache, monkeypatch):
    usage._save_cache(_probed(11))
    started = []
    assert usage.kick_refresh(spawn=started.append) is True
    assert started == [[sys.executable, "-m", "khipu", "usage", "--refresh-due", "600", "--json"]]
    # Within a minute of the last kick: nothing more, however many prompts.
    assert usage.kick_refresh(spawn=started.append) is False and len(started) == 1


def test_the_prompt_kick_does_nothing_on_a_fresh_cache_or_beside_a_running_refresh(cache, monkeypatch):
    started = []
    usage._save_cache(_probed(9))
    assert usage.kick_refresh(spawn=started.append) is False
    usage._save_cache(_probed(11))
    fd = usage._try_refresh_lock()
    try:
        assert usage.kick_refresh(spawn=started.append) is False
    finally:
        usage._release(fd)
    assert started == []


def test_the_refresh_due_command(cache, monkeypatch, capsys):
    from khipu import cli

    usage._save_cache(_probed(20))
    monkeypatch.setattr(usage, "probe_all", lambda accounts, *, timeout: _fake_rows(accounts))
    assert cli.main(["usage", "--refresh-due", "600", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"refreshed": 2, "skipped": None}


# ---- 4. Grok's billing read waits for initialize -----------------------------------------------

_RACY_GROK = """
import json, os, sys, time
# Grok Build 1.0.52's race: a billing read that arrives before initialize has
# answered runs with no sign-in and fails. Unbuffered reads, so a request
# already waiting is seen.
time.sleep(0.3)
lines = [x for x in os.read(0, 65536).split(b"\\n") if x.strip()]
if len(lines) > 1:
    msg = json.loads(lines[1])
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": "Internal error",
          "data": "Billing service error: Invalid or expired credentials (upstream=PermissionDenied, "
                  "reason=no auth context)"}}), flush=True)
    sys.exit(0)
print(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1,
                  "_meta": {"agentVersion": "1.0.52"}}}), flush=True)
print(json.dumps({"jsonrpc": "2.0", "method": "_x.ai/models/update", "params": {}}), flush=True)
msg = json.loads(os.read(0, 65536).split(b"\\n")[0])
print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"subscription_tier": "SuperGrok", "config": {
    "creditUsagePercent": 12.5, "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY",
                                                  "end": "2026-10-19T23:52:22Z"}}}}), flush=True)
os.read(0, 1)
"""


def _grok(tmp_path, monkeypatch, body: str) -> None:
    script = tmp_path / "grok.py"
    script.write_text(body)
    wrapper = tmp_path / "grok"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setattr(usage, "_command", lambda harness: str(wrapper) if harness == "grok" else None)


def test_grok_billing_waits_for_initialize(tmp_path, monkeypatch):
    _grok(tmp_path, monkeypatch, _RACY_GROK)
    row = usage.probe_grok(str(tmp_path))
    assert row["ok"] is True, row
    assert row["plan"] == "SuperGrok" and row["windows"][0]["used_pct"] == 12.5


def test_a_grok_sign_in_failure_in_the_error_data_reads_not_signed_in(tmp_path, monkeypatch):
    _grok(tmp_path, monkeypatch, """
import json, sys
sys.stdin.readline()
print(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}}), flush=True)
sys.stdin.readline()
print(json.dumps({"jsonrpc": "2.0", "id": 2, "error": {"code": -32603, "message": "Internal error",
      "data": "Billing service error: Invalid or expired credentials"}}), flush=True)
sys.stdin.readline()
""")
    assert usage.probe_grok(str(tmp_path)) == {"ok": False, "error": usage.E_SIGNIN}


def test_a_failed_grok_initialize_is_the_probes_error(tmp_path, monkeypatch):
    _grok(tmp_path, monkeypatch, """
import json, sys
sys.stdin.readline()
print(json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}}), flush=True)
sys.stdin.readline()
""")
    started = time.monotonic()
    assert usage.probe_grok(str(tmp_path), timeout=4) == {"ok": False, "error": usage.E_CLI}
    assert time.monotonic() - started < 3  # answered at once, not at the timeout


def test_a_passed_account_week_is_out_of_date_even_with_a_model_week_ahead():
    from datetime import datetime, timedelta, timezone
    from khipu import routing
    now = datetime(2026, 10, 17, 21, 30, tzinfo=timezone.utc)
    row = {"ok": True, "windows": [
        {"name": "weekly", "used_pct": 74, "window_mins": 10080,
         "resets_at": (now - timedelta(hours=1)).isoformat()},
        {"name": "weekly_fable", "used_pct": 6, "window_mins": 10080, "model": "Fable",
         "resets_at": (now + timedelta(days=2)).isoformat()},
    ]}
    fact = routing.plan_fact(row, now)
    assert fact["readable"] is False and fact.get("reset_passed") is True


def test_a_role_whose_first_word_is_too_long_is_dropped_not_cut_mid_word():
    from khipu import routing
    assert routing._short_words("Supercalifragilisticexpialidocious duties", 20) == ""
    assert routing._short_words("First for all Claude work: builds", 20) == "First for all…"
