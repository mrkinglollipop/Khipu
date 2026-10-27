"""Grok reading-gap fixes (diag 2026-10-26): attempt log, shorter due near
reset or after failure, pre-reset figure kept, routing block age + failure.

Hermetic: temp data dir, no real probe, no Grok CLI, no real HOME.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import routing, usage

NOW = datetime(2026, 10, 26, 23, 0, tzinfo=timezone.utc)
GROK = {"harness": "grok", "label": "Grok", "home": None}
CODEX = {"harness": "codex", "label": "Codex", "home": None}


def iso(at: datetime) -> str:
    return usage._iso(at.timestamp())


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": [GROK]}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setattr(usage, "_now", lambda: NOW)
    return data


def _week(used: float, reset: datetime) -> dict:
    return {"name": "weekly", "used_pct": used, "window_mins": 10080, "resets_at": iso(reset)}


def _row(account: dict, *, used: float = 10, reset: datetime | None = None, ok: bool = True,
         error: str | None = None, at: datetime | None = None, **extra) -> dict:
    when = at or NOW
    windows = [_week(used, reset or (NOW + timedelta(days=4)))] if used is not None else []
    row = dict(usage._blank_row(account), ok=ok, error=error, source="live",
               probed_at=iso(when), windows=windows, ms=1234)
    if ok:
        row["last_good_at"] = iso(when)
    row.update(extra)
    return row


def test_store_appends_attempt_log_without_secrets(isolated):
    good = _row(GROK, used=24, at=NOW - timedelta(minutes=1))
    bad = _row(GROK, used=None, ok=False, error=usage.E_CLI, at=NOW, ms=20001)
    bad["windows"] = []
    usage._store([GROK], [good])
    usage._store([GROK], [bad])
    path = Path(isolated) / "state" / usage.ATTEMPTS_NAME
    lines = [json.loads(line) for line in path.read_text().splitlines() if line]
    assert len(lines) == 2
    assert lines[0] == {"time": good["probed_at"], "key": "grok|", "ok": True, "error": None, "ms": 1234}
    assert lines[1] == {"time": bad["probed_at"], "key": "grok|", "ok": False, "error": usage.E_CLI, "ms": 20001}
    blob = path.read_text()
    assert "token" not in blob.lower() and "secret" not in blob.lower()
    assert "GROK_HOME" not in blob and "/Users/" not in blob


def test_attempt_log_rotates_when_over_cap(isolated, monkeypatch):
    monkeypatch.setattr(usage, "ATTEMPTS_MAX_BYTES", 80)
    usage._store([GROK], [_row(GROK, used=1, at=NOW - timedelta(seconds=2))])
    usage._store([GROK], [_row(GROK, used=2, at=NOW - timedelta(seconds=1))])
    usage._store([GROK], [_row(GROK, used=3, at=NOW)])
    active = Path(isolated) / "state" / usage.ATTEMPTS_NAME
    rotated = active.with_name(active.name + usage.ATTEMPTS_ROTATED_SUFFIX)
    assert rotated.is_file() and active.is_file()
    assert active.stat().st_size < usage.ATTEMPTS_MAX_BYTES or len(active.read_text().splitlines()) >= 1


def test_grok_due_sooner_after_failure_or_near_weekly_reset():
    failed = {"last_attempt_at": iso(NOW - timedelta(seconds=100)), "last_error": usage.E_CLI, "ok": False}
    assert usage._due([GROK], {"grok|": failed}, NOW, usage.PERIODIC_MAX_AGE_S) == [GROK]
    fresh_fail = {"last_attempt_at": iso(NOW - timedelta(seconds=30)), "last_error": usage.E_TIMEOUT, "ok": False}
    assert usage._due([GROK], {"grok|": fresh_fail}, NOW, usage.PERIODIC_MAX_AGE_S) == []

    near = {"last_attempt_at": iso(NOW - timedelta(seconds=100)), "ok": True,
            "windows": [_week(90, NOW + timedelta(minutes=45))]}
    assert usage._due([GROK], {"grok|": near}, NOW, usage.PERIODIC_MAX_AGE_S) == [GROK]
    far = {"last_attempt_at": iso(NOW - timedelta(seconds=100)), "ok": True,
           "windows": [_week(10, NOW + timedelta(days=3))]}
    assert usage._due([GROK], {"grok|": far}, NOW, usage.PERIODIC_MAX_AGE_S) == []
    # Codex keeps the five-minute age even near a reset.
    codex_near = {"last_attempt_at": iso(NOW - timedelta(seconds=100)), "ok": True,
                  "windows": [_week(90, NOW + timedelta(minutes=45))]}
    assert usage._due([CODEX], {"codex|": codex_near}, NOW, usage.PERIODIC_MAX_AGE_S) == []


def test_merge_keeps_prev_used_pct_when_weekly_window_rolls():
    previous = _row(GROK, used=98, reset=NOW - timedelta(minutes=1), at=NOW - timedelta(hours=1))
    previous["last_good_at"] = previous["probed_at"]
    fresh = _row(GROK, used=1, reset=NOW + timedelta(days=7), at=NOW)
    merged = usage._merge(previous, fresh)
    assert merged["ok"] is True and merged["windows"][0]["used_pct"] == 1
    assert merged["prev_used_pct"] == 98.0
    assert merged["prev_window_end"] == previous["windows"][0]["resets_at"]


def test_plan_fact_shows_prev_figure_and_failed_read_age():
    reset_at = NOW - timedelta(minutes=30)
    row = _row(GROK, used=1, reset=NOW + timedelta(days=7), at=NOW)
    row["prev_used_pct"] = 98
    row["prev_window_end"] = iso(reset_at)
    fact = routing.plan_fact(row, NOW)
    assert fact["readable"] is True and fact["prev_used_pct"] == 98
    assert fact["prev_window_end"] == iso(reset_at)
    assert "was 98% before reset" in routing.pace_words(fact, NOW)

    kept_at = NOW - timedelta(hours=2)
    failed = _row(GROK, used=25, reset=NOW + timedelta(days=2), ok=False, error=usage.E_CLI, at=kept_at)
    failed["last_good_at"] = iso(kept_at)
    failed["last_error"] = usage.E_CLI
    fact = routing.plan_fact(failed, NOW)
    assert fact["stale"] is True
    assert fact["words"] == "figures from 21:00Z (2 h ago), last read failed (cli_error)"
    assert "last read failed (cli_error)" in routing.pace_words(fact, NOW)

    old = iso(NOW - timedelta(minutes=82))
    stale = dict(_row(GROK, used=74, reset=NOW + timedelta(days=4)), probed_at=old, last_good_at=old)
    aged = routing.plan_fact(stale, NOW)
    assert aged["stale"] is True
    assert aged["words"] == "figures from 21:38Z (1 h 22 min ago); may be out of date"
