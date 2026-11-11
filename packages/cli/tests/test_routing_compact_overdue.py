"""Doctor says plainly when the routing record is overdue for compaction and why."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from khipu import features, jobs, routing, routing_compact as rc

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


def _state(monkeypatch, *, push=True, loaded=True):
    monkeypatch.setattr(features, "enabled", lambda name, *a, **k: push)
    monkeypatch.setattr(jobs, "_plist_loaded", lambda label: loaded)


def test_a_recent_attempt_is_not_overdue(monkeypatch):
    _state(monkeypatch)
    assert rc._overdue({"attempted_at": routing._iso(NOW - timedelta(hours=3))}, NOW) is None


def test_overdue_names_the_drain_job_when_it_is_not_loaded(monkeypatch):
    _state(monkeypatch, loaded=False)
    why = rc._overdue({}, NOW)
    assert "queue-drain job" in why and "not installed or not loaded" in why and "no attempt is recorded" in why


def test_overdue_names_the_switched_off_push(monkeypatch):
    _state(monkeypatch, push=False)
    assert "routing push is switched off" in rc._overdue({"attempted_at": routing._iso(NOW - timedelta(days=3))}, NOW)


def test_overdue_with_the_drain_loaded_says_it_has_not_tried(monkeypatch):
    _state(monkeypatch)
    why = rc._overdue({"attempted_at": routing._iso(NOW - timedelta(days=3))}, NOW)
    assert "loaded but has not attempted" in why


def _health(monkeypatch, lines, *, push=True, loaded=True, last=None):
    _state(monkeypatch, push=push, loaded=loaded)
    monkeypatch.setattr(rc, "log_size", lambda: {"lines": lines, "bytes": lines * 100})
    monkeypatch.setattr(rc, "_droppable", lambda now=None: (100, 100))
    monkeypatch.setattr(rc, "_stored_rows", lambda: ([], []))
    monkeypatch.setattr(rc, "_read_state", lambda: dict(last or {}))
    return rc.health(NOW)


def test_green_overdue_warning_is_unchanged(monkeypatch):
    out = _health(monkeypatch, 2500, loaded=False)
    assert out["level"] == "green"
    assert out["warning"] == ("The routing record holds 2,500 lines (250,000 bytes) and is overdue for "
                              f"compaction: {out['overdue_why']}")


def test_yellow_overdue_with_the_job_not_loaded_says_why_not_nightly(monkeypatch):
    out = _health(monkeypatch, 3500, loaded=False)
    assert out["level"] == "yellow"
    assert out["warning"] == ("The routing record holds 3,500 lines (350,000 bytes), past the 2,000 lines or "
                              f"{rc.BYTES_TRIGGER:,} bytes where it is compacted, and compaction is overdue: "
                              f"{out['overdue_why']}")
    assert "not installed or not loaded" in out["warning"] and "after the plan figures" not in out["warning"]


def test_yellow_overdue_with_the_job_loaded_but_no_attempt_says_so(monkeypatch):
    out = _health(monkeypatch, 3500, last={"attempted_at": routing._iso(NOW - timedelta(days=3))})
    assert "loaded but has not attempted it" in out["warning"]
    assert "nightly and after the plan figures" not in out["warning"]


def test_red_overdue_with_the_job_not_loaded_says_why(monkeypatch):
    out = _health(monkeypatch, 4500, loaded=False)
    assert out["level"] == "red"
    assert out["warning"] == ("The routing record holds 4,500 lines (450,000 bytes), past 4,000 lines or "
                              f"{rc.BYTES_RED:,} bytes, and compaction is overdue: {out['overdue_why']} "
                              "Compaction has not run under this code yet. It has not run under this code yet; "
                              "the next run time is not known.")
    assert "not installed or not loaded" in out["warning"]


def test_yellow_and_red_that_are_not_overdue_read_as_before(monkeypatch):
    recent = {"attempted_at": routing._iso(NOW - timedelta(hours=3))}
    for lines in (3500, 4500):
        out = _health(monkeypatch, lines, last=recent)
        assert "overdue" not in out
        assert "nightly and after the plan figures refresh" in out["warning"]
