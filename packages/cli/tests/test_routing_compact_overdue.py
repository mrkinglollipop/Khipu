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
